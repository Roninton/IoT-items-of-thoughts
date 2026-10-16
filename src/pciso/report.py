"""跨组报告：曲线 + 实验设置表 + 原始数字。

不引入 matplotlib —— 手写 SVG 足够画折线，而且零依赖、可 diff、能直接进论文附录。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# 固定配色，保证跨组颜色稳定（A..G 一一对应）
PALETTE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#17becf",
    "#7f7f7f",
]


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@dataclass
class ArmSeries:
    """一个组的一条曲线。"""

    arm: str
    label: str
    xs: list[int] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    color: str = "#1f77b4"


@dataclass
class Panel:
    title: str
    series: list[ArmSeries] = field(default_factory=list)
    y_min: float | None = None
    y_max: float | None = None
    # 离散事件（回退）用竖线标出来：哪一轮、哪个组
    markers: dict[str, list[int]] = field(default_factory=dict)
    note: str = ""


# --------------------------------------------------------------------------- #
# SVG 折线图
# --------------------------------------------------------------------------- #

_PAD_L, _PAD_R, _PAD_T, _PAD_B = 62, 150, 34, 40


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, count)
    mag = 10 ** int(_floor_log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if raw <= step:
            break
    start = _floor(lo / step) * step
    ticks: list[float] = []
    v = start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.5:
            ticks.append(round(v, 6))
        v += step
    return ticks


def _floor_log10(x: float) -> float:
    import math

    return math.floor(math.log10(x)) if x > 0 else 0.0


def _floor(x: float) -> float:
    import math

    return math.floor(x)


def render_panel(panel: Panel, *, x: int, y: int, w: int, h: int) -> str:
    xs_all = [v for s in panel.series for v in s.xs]
    ys_all = [v for s in panel.series for v in s.ys]
    if not xs_all or not ys_all:
        return f'<text x="{x}" y="{y + 20}" font-size="13" fill="#888">（无数据）</text>'

    x_lo, x_hi = min(xs_all), max(xs_all)
    if x_hi == x_lo:
        x_hi = x_lo + 1

    y_lo = panel.y_min if panel.y_min is not None else min(0.0, min(ys_all))
    y_hi = panel.y_max if panel.y_max is not None else max(ys_all)
    if y_hi <= y_lo:
        y_hi = y_lo + 1
    ticks = _nice_ticks(y_lo, y_hi)
    if ticks:
        y_lo, y_hi = min(y_lo, ticks[0]), max(y_hi, ticks[-1])

    def sx(v: float) -> float:
        return x + (v - x_lo) / (x_hi - x_lo) * w

    def sy(v: float) -> float:
        return y + h - (v - y_lo) / (y_hi - y_lo) * h

    out: list[str] = [f'<g><text x="{x}" y="{y - 10}" font-size="13" font-weight="600">{_esc(panel.title)}</text>']
    if panel.note:
        out.append(
            f'<text x="{x + w}" y="{y - 10}" font-size="11" fill="#888" text-anchor="end">'
            f"{_esc(panel.note)}</text>"
        )

    # y 轴网格 + 刻度
    for t in ticks:
        yy = sy(t)
        out.append(
            f'<line x1="{x:.1f}" y1="{yy:.1f}" x2="{x + w:.1f}" y2="{yy:.1f}" '
            f'stroke="#e8e8e8" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{x - 8}" y="{yy + 4:.1f}" font-size="11" fill="#666" '
            f'text-anchor="end">{t:g}</text>'
        )

    # x 轴刻度（轮次是整数）
    for xi in range(int(x_lo), int(x_hi) + 1):
        if x_hi - x_lo > 12 and xi % 2:
            continue
        xx = sx(xi)
        out.append(
            f'<text x="{xx:.1f}" y="{y + h + 18}" font-size="11" fill="#666" '
            f'text-anchor="middle">{xi}</text>'
        )
    out.append(
        f'<line x1="{x}" y1="{y + h}" x2="{x + w}" y2="{y + h}" stroke="#bbb" stroke-width="1"/>'
    )

    # 回退标记
    for arm, rounds in panel.markers.items():
        series = next((s for s in panel.series if s.arm == arm), None)
        if series is None:
            continue
        for r in rounds:
            xx = sx(r)
            out.append(
                f'<line x1="{xx:.1f}" y1="{y}" x2="{xx:.1f}" y2="{y + h}" '
                f'stroke="{series.color}" stroke-width="1" stroke-dasharray="3,3" opacity="0.55"/>'
            )

    # 折线 + 点
    for s in panel.series:
        if not s.ys:
            continue
        pts = " ".join(f"{sx(xv):.1f},{sy(yv):.1f}" for xv, yv in zip(s.xs, s.ys, strict=False))
        out.append(
            f'<polyline points="{pts}" fill="none" stroke="{s.color}" '
            f'stroke-width="2" stroke-linejoin="round"/>'
        )
        for xv, yv in zip(s.xs, s.ys, strict=False):
            out.append(
                f'<circle cx="{sx(xv):.1f}" cy="{sy(yv):.1f}" r="2.6" fill="{s.color}"/>'
            )

    # 图例（放在右侧，避免遮挡）
    ly = y + 2
    for s in panel.series:
        out.append(
            f'<line x1="{x + w + 12}" y1="{ly}" x2="{x + w + 30}" y2="{ly}" '
            f'stroke="{s.color}" stroke-width="2"/>'
        )
        out.append(
            f'<text x="{x + w + 36}" y="{ly + 4}" font-size="11">{_esc(s.arm)} '
            f"{_esc(s.label[:12])}</text>"
        )
        ly += 17

    out.append("</g>")
    return "\n".join(out)


def render_svg(panels: Sequence[Panel], *, title: str = "", width: int = 1020) -> str:
    plot_w = width - _PAD_L - _PAD_R
    panel_h = 150
    gap = 52
    header = 46
    height = header + len(panels) * (panel_h + gap) + 20

    body: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif, system-ui, '
        f"'Segoe UI', 'Microsoft YaHei', sans-serif\">",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    if title:
        body.append(
            f'<text x="{_PAD_L}" y="28" font-size="17" font-weight="700">{_esc(title)}</text>'
        )
    y = header + panel_h
    for p in panels:
        body.append(
            render_panel(p, x=_PAD_L, y=y - panel_h + 10, w=plot_w, h=panel_h - 20)
        )
        y += panel_h + gap
    body.append("</svg>")
    return "\n".join(body)


# --------------------------------------------------------------------------- #
# 从会话 metrics 汇总
# --------------------------------------------------------------------------- #


@dataclass
class ArmResult:
    arm: str
    config: str
    label: str
    session_id: str
    metrics: dict[str, Any]
    error: str = ""

    def rounds(self) -> list[dict[str, Any]]:
        return list(self.metrics.get("per_round") or [])

    def series(self, key: str) -> tuple[list[int], list[float]]:
        xs, ys = [], []
        for p in self.rounds():
            xs.append(int(p["round"]) + 1)
            ys.append(float(p.get(key) or 0))
        return xs, ys


def build_panels(results: Sequence[ArmResult]) -> list[Panel]:
    """四张图，对应三个场景的核心量。

    1. 约束违反数 / 轮 —— P0 核心，一张图定生死
    2. 违反的约束条数 / 轮 —— 分辨率更高（同一条约束里可能有多个 viol）
    3. prompt token / 轮 —— P1 核心：B/D 线性增长，C/F/G 应恒定
    4. C 的 token / 轮 —— 工作记忆是否稳定有界
    """
    specs = [
        ("violation_count", "约束违反数 / 轮（P0 核心）", "客观计数之和"),
        ("violations", "被违反的约束条数 / 轮", "去重后的约束数"),
        ("prompt_tokens_est", "每轮输入 prompt token（P1 核心）", "B/D 线性增长，C 应恒定"),
        ("c_tokens", "循环状态 C 的 token / 轮", "工作记忆是否稳定有界"),
    ]
    panels: list[Panel] = []
    for idx, (key, title, note) in enumerate(specs):
        series: list[ArmSeries] = []
        markers: dict[str, list[int]] = {}
        for i, res in enumerate(results):
            xs, ys = res.series(key)
            if not xs:
                continue
            series.append(
                ArmSeries(
                    arm=res.arm,
                    label=res.label,
                    xs=xs,
                    ys=ys,
                    color=PALETTE[i % len(PALETTE)],
                )
            )
            rev = [int(p["round"]) + 1 for p in res.rounds() if p.get("judgement") == "revert"]
            if rev:
                markers[res.arm] = rev
        panels.append(Panel(title=title, series=series, markers=markers, note=note))
        _ = idx
    return panels


def matrix_rows(results: Sequence[ArmResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for res in results:
        m = res.metrics
        rounds = res.rounds()
        tok = m.get("tokens", {})
        rows.append(
            {
                "arm": res.arm,
                "config": res.config,
                "label": res.label,
                "session_id": res.session_id,
                "rounds": m.get("rounds", 0),
                "reverts": m.get("reverts", 0),
                "revert_rate": round(float(m.get("revert_rate") or 0), 3),
                "violations_total": m.get("violation_count_total", 0),
                "violations_by_round": "/".join(str(p["violation_count"]) for p in rounds),
                "prompt_tok_first": rounds[0]["prompt_tokens_est"] if rounds else 0,
                "prompt_tok_last": rounds[-1]["prompt_tokens_est"] if rounds else 0,
                "prompt_growth": (
                    round(rounds[-1]["prompt_tokens_est"] / max(1, rounds[0]["prompt_tokens_est"]), 2)
                    if rounds
                    else 0
                ),
                "c_tok_max": (m.get("compression_quality") or {}).get("c_tokens_max", 0),
                "anchor_rate": (m.get("compression_quality") or {}).get("anchor_resolvable_rate"),
                "work_prompt_total": tok.get("work_prompt_total", 0),
                "compressor_total": tok.get("compressor_total", 0),
                "unbacked_claims": (m.get("self_report") or {}).get("unbacked_claims_total", 0),
                "invalid_read_rate": (m.get("retrieval") or {}).get("invalid_read_rate"),
                "redundant_read_rate": (m.get("retrieval") or {}).get("redundant_read_rate"),
                "read_bytes": (m.get("retrieval") or {}).get("bytes_total", 0),
                "extra_calls": (m.get("retrieval") or {}).get("extra_calls_total", 0),
                "error": res.error,
            }
        )
    return rows


def render_markdown(rows: Sequence[dict[str, Any]], *, title: str = "") -> str:
    if not rows:
        return "（无数据）"
    headers = [
        ("arm", "组"),
        ("rounds", "轮"),
        ("reverts", "回退"),
        ("violations_total", "违反总数"),
        ("violations_by_round", "逐轮违反数"),
        ("prompt_tok_first", "prompt首"),
        ("prompt_tok_last", "prompt末"),
        ("prompt_growth", "增长x"),
        ("c_tok_max", "C峰值"),
        ("anchor_rate", "锚点率"),
        ("work_prompt_total", "工作token"),
        ("compressor_total", "压缩token"),
    ]
    extra = [
        ("unbacked_claims", "无据声明"),
        ("invalid_read_rate", "无效检索"),
        ("redundant_read_rate", "冗余检索"),
        ("read_bytes", "读取字节"),
        ("extra_calls", "额外调用"),
    ]
    lines: list[str] = []
    if title:
        lines += [f"## {title}", ""]
    lines.append("| " + " | ".join(h for _, h in headers + extra) + " |")
    lines.append("|" + "---|" * (len(headers) + len(extra)))
    for r in rows:
        cells = []
        for key, _ in headers + extra:
            v = r.get(key)
            if isinstance(v, float):
                cells.append(f"{v:.3f}")
            elif v is None:
                cells.append("—")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_csv(results: Sequence[ArmResult]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "arm",
            "round",
            "judgement",
            "violation_count",
            "violations",
            "prompt_tokens_est",
            "c_tokens",
            "facts",
            "failures",
            "deps",
            "work_calls",
            "reads_served",
            "reads_denied",
            "reads_redundant",
            "read_bytes",
            "declared_units",
            "derived_units",
            "unbacked_claims",
        ]
    )
    for res in results:
        for p in res.rounds():
            w.writerow(
                [
                    res.arm,
                    int(p["round"]) + 1,
                    p.get("judgement"),
                    p.get("violation_count"),
                    len(p.get("violation_ids") or []),
                    p.get("prompt_tokens_est"),
                    p.get("c_tokens"),
                    p.get("facts"),
                    p.get("failures"),
                    p.get("deps"),
                    p.get("work_calls"),
                    p.get("reads_served"),
                    p.get("reads_denied"),
                    p.get("reads_redundant"),
                    p.get("read_bytes"),
                    p.get("declared_units"),
                    p.get("derived_units"),
                    len(p.get("unbacked_claims") or []),
                ]
            )
    return buf.getvalue()


def build_report(
    results: Sequence[ArmResult], *, title: str, meta: dict[str, Any]
) -> dict[str, Any]:
    return {
        "title": title,
        "meta": meta,
        "rows": matrix_rows(results),
        "per_round": {r.arm: r.rounds() for r in results},
        "experiment_rows": {
            r.arm: (r.metrics.get("experiment_row") or {}) for r in results
        },
    }


def dump_json(payload: dict[str, Any], path) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
