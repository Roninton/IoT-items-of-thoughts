"""跨组报告生成器：曲线、表格、CSV。

重点验证的不是"画得好看"，而是：
  - 数字没错位、没有 NaN 泄漏进 SVG
  - 图例/标题里的特殊字符被转义（否则 SVG 直接坏掉）
  - 空数据退化时给出可读提示而不是崩
"""

from __future__ import annotations

from pciso.report import (
    ArmResult,
    ArmSeries,
    Panel,
    build_panels,
    build_report,
    matrix_rows,
    render_csv,
    render_markdown,
    render_panel,
    render_svg,
)


def _per_round(arm: str, viols: list[int], prompts: list[int], c: list[int]) -> list[dict]:
    out = []
    for i, (v, p, ct) in enumerate(zip(viols, prompts, c, strict=True)):
        out.append(
            {
                "round": i,
                "judgement": "revert" if i == 2 else "accept",
                "violation_count": v,
                "violation_ids": ["C7"] * min(v, 3),
                "prompt_tokens_est": p,
                "c_tokens": ct,
                "facts": i,
                "failures": 0,
                "deps": i,
                "work_calls": 2 if arm == "G" else 1,
                "reads_served": i,
                "reads_denied": 1 if arm == "G" else 0,
                "reads_redundant": 1 if arm == "G" else 0,
                "read_bytes": 100 * i,
                "declared_units": i,
                "derived_units": i,
                "unbacked_claims": [],
            }
        )
    return out


def _result(arm: str, viols: list[int], prompts: list[int], c: list[int]) -> ArmResult:
    rounds = _per_round(arm, viols, prompts, c)
    metrics = {
        "rounds": len(rounds),
        "reverts": sum(1 for r in rounds if r["judgement"] == "revert"),
        "revert_rate": 0.125,
        "violation_count_total": sum(viols),
        "per_round": rounds,
        "tokens": {"work_prompt_total": sum(prompts), "compressor_total": 42},
        "compression_quality": {"c_tokens_max": max(c), "anchor_resolvable_rate": 1.0},
        "self_report": {"unbacked_claims_total": 0},
        "retrieval": {
            "invalid_read_rate": 0.25 if arm == "G" else None,
            "redundant_read_rate": 0.375 if arm == "G" else None,
            "bytes_total": 300,
            "extra_calls_total": 8 if arm == "G" else 0,
        },
        "experiment_row": {"arm": arm},
    }
    return ArmResult(arm=arm, config=f"arm_{arm}.yaml", label=f"第{arm}组", session_id="sid", metrics=metrics)


# --------------------------------------------------------------------------- #
# 面板与曲线
# --------------------------------------------------------------------------- #


def test_build_panels_has_four_scenarios() -> None:
    results = [_result("C", [5, 4, 3], [100, 100, 100], [50, 60, 70])]
    panels = build_panels(results)
    assert len(panels) == 4
    titles = " ".join(p.title for p in panels)
    assert "P0" in titles and "P1" in titles


def test_prompt_growth_is_visible_in_p1_panel() -> None:
    """B 组的 prompt 应该线性涨，C 组应恒定 —— 这是 P1 的核心断言。"""
    b = _result("B", [1, 1, 1], [100, 200, 300], [0, 0, 0])
    c = _result("C", [1, 1, 1], [100, 100, 100], [50, 60, 70])
    panels = build_panels([b, c])
    p1 = next(p for p in panels if "P1" in p.title)
    series = {s.arm: s.ys for s in p1.series}
    assert series["B"] == [100, 200, 300]
    assert series["C"] == [100, 100, 100]


def test_revert_markers_are_collected() -> None:
    results = [_result("C", [5, 4, 3], [100] * 3, [1] * 3)]
    panels = build_panels(results)
    assert panels[0].markers.get("C") == [3]


def test_svg_is_well_formed_and_escapes_labels() -> None:
    panel = Panel(
        title='含 <危险> & "字符" 的标题',
        series=[ArmSeries(arm="A", label="x<y", xs=[1, 2], ys=[1, 2], color="#000")],
    )
    svg = render_svg([panel], title="t < 1")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "<危险>" not in svg  # 未转义的尖括号会让 SVG 坏掉
    assert "&lt;危险&gt;" in svg
    assert "nan" not in svg.lower()


def test_svg_handles_single_point() -> None:
    panel = Panel(title="单点", series=[ArmSeries(arm="A", label="a", xs=[1], ys=[7], color="#000")])
    svg = render_panel(panel, x=0, y=0, w=100, h=100)
    assert "polyline" in svg
    assert "nan" not in svg.lower()


def test_svg_handles_empty_panel() -> None:
    svg = render_panel(Panel(title="空"), x=0, y=0, w=100, h=100)
    assert "无数据" in svg


def test_svg_constant_series_does_not_divide_by_zero() -> None:
    panel = Panel(title="常数", series=[ArmSeries(arm="A", label="a", xs=[1, 2, 3], ys=[5, 5, 5], color="#000")])
    svg = render_panel(panel, x=0, y=0, w=100, h=100)
    assert "nan" not in svg.lower()
    assert "inf" not in svg.lower()


# --------------------------------------------------------------------------- #
# 表格与 CSV
# --------------------------------------------------------------------------- #


def test_matrix_rows_carry_the_headline_numbers() -> None:
    rows = matrix_rows([_result("C", [5, 4, 3], [100, 150, 200], [50, 60, 70])])
    r = rows[0]
    assert r["arm"] == "C"
    assert r["rounds"] == 3
    assert r["violations_total"] == 12
    assert r["violations_by_round"] == "5/4/3"
    assert r["prompt_tok_first"] == 100
    assert r["prompt_tok_last"] == 200
    assert r["prompt_growth"] == 2.0


def test_markdown_table_has_a_row_per_arm_and_dash_for_none() -> None:
    rows = matrix_rows(
        [_result("F", [1], [100], [0]), _result("G", [1], [100], [0])]
    )
    md = render_markdown(rows, title="对照")
    assert "## 对照" in md
    assert md.count("\n|") >= 3
    assert "—" in md  # F 组没有检索率，应显示占位而不是 None
    assert "None" not in md


def test_csv_has_one_row_per_round() -> None:
    csv_text = render_csv([_result("C", [1, 2], [10, 20], [1, 2])])
    lines = [ln for ln in csv_text.strip().splitlines() if ln]
    assert len(lines) == 3  # 表头 + 2 轮
    assert lines[0].startswith("arm,round,judgement")


def test_build_report_shape() -> None:
    payload = build_report(
        [_result("C", [1], [10], [1])], title="t", meta={"offline": True}
    )
    assert payload["title"] == "t"
    assert payload["meta"]["offline"] is True
    assert "C" in payload["per_round"]
    assert payload["experiment_rows"]["C"]["arm"] == "C"


def test_markdown_rows_have_consistent_column_counts() -> None:
    """表格结构不能被内容里的竖线或长文本破坏。"""
    rows = matrix_rows([_result("C", [1], [10], [1]), _result("F", [2], [20], [2])])
    md = render_markdown(rows)
    lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    widths = {ln.count("|") for ln in lines}
    assert len(widths) == 1, f"表格列数不一致: {widths}"
    assert len(lines) == 4  # 表头 + 分隔行 + 2 个组
