"""推理编排原型（README §1.3）：ACT 由 server 渲染，THINK 由模型提取，两条硬规则在服务端执行。

真机证据（会话 03151aaae4c84363，笔刷载体 6 轮）：
- 第 1 轮用户说"先别动文件"，模型讲的是**设计计划**，压缩器把计划记成了 12 条事实，
  锚点内部全标着 `resolved=False / 文件不存在`，渲染出来的正文读起来却像已经写完 ——
  下一轮它开始"盘点、重写"。
所以本文件守的第一件事：**完成状态只能由 ACT 节点给出**（ACT 只从写盘结果渲染，零模型调用）。
"""

from __future__ import annotations

import json

from pciso.actnodes import (
    build_act_ops,
    build_alternative_ops,
    build_dropped_import_ops,
    build_rejected_route_ops,
    build_shelved_risk_ops,
    describe_verification,
    filter_think_ops,
    think_anchor_paths,
    with_act_nodes,
)
from pciso.compressors import ThinkingNodeCompressor, build_compressor
from pciso.config import CompressionConfig, SessionConfig
from pciso.merge import apply_delta
from pciso.models import Anchor, CompressedState, DeltaC, DeltaOp, FactNode
from pciso.render import build_thinking_prompt, render_state_block
from pciso.store import SessionStore
from pciso.workmodels import ChatResult

WRITTEN = [("tools/duration.py", "a" * 64, 812)]


class KeepClient:
    """记下 prompt、回一段固定 JSON 的假客户端。"""

    def __init__(self, payload: dict | None = None) -> None:
        self.prompts: list[str] = []
        self.payload = payload or {"ops": []}

    def chat(self, prompt: str, **kwargs: object) -> ChatResult:
        self.prompts.append(prompt)
        return ChatResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            prompt_tokens=900,
            completion_tokens=48,
        )


class ExplodingClient:
    """不该被调用：空 thinking 时调它一次就是白花钱。"""

    def chat(self, prompt: str, **kwargs: object) -> ChatResult:  # pragma: no cover
        raise AssertionError("空 thinking 不该产生压缩器调用")


# --------------------------------------------------------------------------- #
# ACT：server 从写盘结果渲染
# --------------------------------------------------------------------------- #


def test_act_node_is_rendered_from_written_files() -> None:
    ops = build_act_ops(prior_state=CompressedState(), written=WRITTEN)
    assert [op.op for op in ops] == ["ADD_FACT"]
    payload = ops[0].payload
    assert payload["node_kind"] == "act"
    assert payload["anchor"] == {"path": "tools/duration.py"}
    assert "812B" in payload["statement"]
    assert "a" * 12 in payload["statement"], "ACT 必须带上物证（sha256 前 12 位）"


def test_act_node_supersedes_the_previous_state_of_the_same_path() -> None:
    """老化规则：同一路径再次写入 -> SUPERSEDE，而不是每写一次堆一条。"""
    prior = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="写入 tools/duration.py（700B，sha256 bbbbbbbbbbbb）",
                node_kind="act",
                anchor=Anchor(path="tools/duration.py"),
            )
        ]
    )
    ops = build_act_ops(prior_state=prior, written=WRITTEN)
    assert ops[0].op == "SUPERSEDE_FACT"
    assert ops[0].target == "N1"
    assert ops[0].payload["id"] != "N1"


def test_act_ids_do_not_collide_with_the_model_ops_in_the_same_delta() -> None:
    """两边都从 next_fact_id() 起编号的话，后到的那批会整批撞上"已存在"被拒收。"""
    model_op = DeltaOp(
        op="ADD_FACT",
        payload={
            "id": "N1",
            "statement": "选胶囊距离场画笔画：起点=终点、半径=r 就是一杖实心圆盘",
            "node_kind": "think",
            "polarity": "+",
            "anchor": {"path": "tools/duration.py"},
        },
    )
    delta, stats = with_act_nodes(
        DeltaC(ops=[model_op]), prior_state=CompressedState(), written=WRITTEN
    )
    assert stats["act_nodes"] == 1
    ids = [(op.payload or {}).get("id") for op in delta.ops]
    assert len(set(ids)) == len(ids), f"同一批里 id 撞号了: {ids}"

    state, report = apply_delta(
        CompressedState(),
        delta,
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    assert report.rejected == []
    kinds = {f.id: f.node_kind for f in state.fact_chain}
    assert kinds == {"N1": "think", "N2": "act"}


# --------------------------------------------------------------------------- #
# ACT 的锚点粒度：细到符号（真机自述的可执行版本）
# --------------------------------------------------------------------------- #

PY_SOURCE = '''"""tools/duration.py"""


def format_duration(seconds: int) -> str:
    """把秒数格式化。"""
    return ""


def parse_duration(text: str) -> int:
    return 0


class DurationError(ValueError):
    """解析失败的统一异常。"""
'''


def test_act_anchor_goes_down_to_the_symbol_level_for_python() -> None:
    """真机自述（会话 aae79de300504908）第 3 条：锚点写成 `path::`（符号位空着）时，
    "验证器无法据此确认符号是否存在"。所以 `.py` 按 AST 顶层公开符号逐个出节点。"""
    ops = build_act_ops(
        prior_state=CompressedState(),
        written=WRITTEN,
        sources={"tools/duration.py": PY_SOURCE},
    )
    assert [op.op for op in ops] == ["ADD_FACT"] * 3
    anchors = [op.payload["anchor"] for op in ops]
    assert [a["symbol"] for a in anchors] == ["format_duration", "parse_duration", "DurationError"]
    assert all(a["path"] == "tools/duration.py" for a in anchors)
    assert "(seconds: int) -> str" in ops[0].payload["statement"], "签名要进节点，下一轮才对得上契约"
    assert ops[0].payload["statement"].count("format_duration") == 1, "签名里已经有名字，别再粘一遍"
    # 物证在每条节点上都要自足（读的人不会回头找第一条）。
    assert all("812B" in op.payload["statement"] for op in ops)


def test_act_node_stays_path_level_without_source_text() -> None:
    """非 .py（或没拿到正文）：退回路径级一条 —— 别为了符号去读盘。"""
    ops = build_act_ops(prior_state=CompressedState(), written=[("out/current-view.jpg", "c" * 64, 30)])
    assert [op.op for op in ops] == ["ADD_FACT"]
    assert ops[0].payload["anchor"] == {"path": "out/current-view.jpg"}


def test_rewriting_a_file_invalidates_the_symbols_that_disappeared() -> None:
    """整份重写时旧符号可能已经不存在了：只取代"同名符号"会在 C 里留下幽灵节点。"""
    prior = CompressedState(
        fact_chain=[
            FactNode(
                id=f"N{i}",
                statement=f"写入 tools/duration.py::{name}",
                node_kind="act",
                anchor=Anchor(path="tools/duration.py", symbol=name),
            )
            for i, name in ((1, "format_duration"), (2, "parse_duration"), (3, "DurationError"))
        ]
    )
    shrunk = "def format_duration(seconds: int) -> str:\n    return ''\n"
    ops = build_act_ops(
        prior_state=prior,
        written=WRITTEN,
        sources={"tools/duration.py": shrunk},
    )
    assert [op.op for op in ops] == ["SUPERSEDE_FACT", "INVALIDATE_FACT", "INVALIDATE_FACT"]
    assert [op.target for op in ops] == ["N1", "N2", "N3"]
    assert ops[0].payload["anchor"]["symbol"] == "format_duration"


# --------------------------------------------------------------------------- #
# THINK：两条服务端硬规则
# --------------------------------------------------------------------------- #


def _think_op(statement: str, *, path: str | None = "tools/duration.py", polarity: str = "+"):
    payload: dict = {
        "id": "N9",
        "statement": statement,
        "node_kind": "think",
        "polarity": polarity,
    }
    if path:
        payload["anchor"] = {"path": path}
    return DeltaOp(op="ADD_FACT", payload=payload)


def test_think_node_without_an_anchor_is_kept_as_anchorless() -> None:
    """不碰文件的推理没有锚点可挂 —— 保留它，只是标成"没有物证"。

    真机实测（IoT 代理、纯推理题）：旧规则「无锚不留」把这块思考的 12 条节点**全丢了**
    （think_dropped_unanchored=12）→ 紧凑版为空 → 回退原文 → 机制等于没开。
    """
    kept, stats = filter_think_ops(
        [_think_op("先判是不是 epoch 数字，再判时区信息是自带还是缺失", path=None)],
        allowed_paths=["tools/duration.py"],
    )
    assert len(kept) == 1
    assert "anchor" not in (kept[0].payload or {})  # 没锚点就干脆不留这个字段（merge 会标 unanchored）
    assert stats["think_anchorless"] == 1
    assert stats["think_dropped_unanchored"] == 0


def test_an_absolute_allowed_path_keeps_a_relative_anchor() -> None:
    """工具报的是绝对路径、模型写的是相对路径 —— 这是同一个文件，不能判成"编路径"。

    真机（IoT 代理接 DSH）：DSH 传 `D:\\_playground\\workspace\\arms\\probe\\ok.txt`，
    模型写 `arms/probe/ok.txt`，字符串精确比较不相等 → 整块思考被丢（298 轮 kept=0、
    dropped_unanchored=27）。
    """
    from pciso.actnodes import filter_think_ops, path_matches

    allowed = [r"D:\_playground\workspace\oneline\out\frame.png"]
    assert path_matches("oneline/out/frame.png", allowed) is True
    assert path_matches("out/frame.png", allowed) is True  # 后缀命中（按路径边界）
    assert path_matches("other/frame.png", allowed) is False  # 不是路径边界
    assert path_matches("toneline/out/frame.png", allowed) is False
    kept, stats = filter_think_ops(
        [_think_op("先确认帧图是纯像素再比", path="oneline/out/frame.png")],
        allowed_paths=allowed,
    )
    assert len(kept) == 1
    assert stats["think_dropped_unanchored"] == 0
    assert stats["think_kept"] == 1


def test_think_node_on_a_path_without_any_real_action_is_dropped() -> None:
    kept, stats = filter_think_ops(
        [_think_op("打算新建 tools/palette.py 存颜色常量", path="tools/palette.py")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept == []
    assert stats["think_dropped_unanchored"] == 1


def test_a_path_that_really_exists_on_disk_is_not_a_fabrication(tmp_path) -> None:
    """锚点指向"这轮没写过、但盘上真有"的文件 —— 不是编造，必须留下。

    真机事故（会话 3881a53e1bd84ed1）：58 块里大半因此被判编路径，其中一块 27 条
    节点全丢、`kept=0`，客户端那块于是退回原文，机制等于没开。
    """

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "duration.py").write_text("x = 1\n", encoding="utf-8")
    kept, stats = filter_think_ops(
        [_think_op("duration 的 format_duration 用 divmod 拆小时", path="tools/duration.py")],
        allowed_paths=["tools/other.py"],
        root=tmp_path,
    )
    assert len(kept) == 1
    assert stats["think_anchor_real_file"] == 1
    assert stats["think_dropped_unanchored"] == 0


def test_a_file_to_be_created_in_a_real_directory_is_allowed(tmp_path) -> None:
    """要新建的文件：目录真实存在就不算编造（模型本来就在规划下一步写哪儿）。"""
    (tmp_path / "oneline").mkdir()
    kept, stats = filter_think_ops(
        [_think_op("下一步给 oneline/motion.py 加 --stride", path="oneline/motion.py")],
        allowed_paths=[],
        root=tmp_path,
    )
    assert len(kept) == 1
    assert stats["think_anchor_real_file"] == 1


def test_a_path_in_a_directory_that_does_not_exist_is_still_dropped(tmp_path) -> None:
    """连目录都是编的 —— 这才是"编锚点"，照旧丢。"""
    kept, stats = filter_think_ops(
        [_think_op("把颜色常量挪到 tools/palette/colors.py", path="tools/palette/colors.py")],
        allowed_paths=[],
        root=tmp_path,
    )
    assert kept == []
    assert stats["think_dropped_unanchored"] == 1


def test_think_node_claiming_completion_is_dropped() -> None:
    """推理里说"已实现 / 测试通过"是猜测，完成状态只能由 ACT 给出。"""
    kept, stats = filter_think_ops(
        [_think_op("duration.py 已实现，测试通过", polarity="+")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept == []
    assert stats["think_dropped_claim"] == 1


def test_think_node_reviewing_other_nodes_is_dropped_as_meta() -> None:
    """议论别的节点 = 元评论。真机实测：让它"评审 C"时它记了 5 条这种节点。"""
    kept, stats = filter_think_ops(
        [_think_op("否掉 N1 的表述方式：它没钉输出字符串契约", polarity="-")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept == []
    assert stats["think_dropped_meta"] == 1


def test_think_node_mentioning_a_word_with_n_and_digits_is_not_meta() -> None:
    """别把 "hh:mm:ss" / "3600" / "SHA256" 这类含数字的东西误判成节点引用。"""
    kept, stats = filter_think_ops(
        [_think_op("用 f-string 补零输出 hh:mm:ss，覆盖 3600 与 86400 边界", polarity="+")],
        allowed_paths=["tools/duration.py"],
    )
    assert stats["think_kept"] == 1 and kept
    assert stats["think_dropped_meta"] == 0


def test_think_metrics_expose_file_level_anchors_and_vague_statements() -> None:
    """自述第 2、3 条要能在事后数据里数出来：只给类别 / 锚点只到文件，都计成度量。"""
    ops = [
        _think_op("选单一入口：format_duration(seconds: int) -> str", path="tools/duration.py"),
        _think_op("覆盖边界情况", polarity="-"),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N8",
                "statement": "解析 parse_duration 只接受 hh:mm:ss 前缀",
                "node_kind": "think",
                "polarity": "+",
                "anchor": {"path": "tools/duration.py", "symbol": "parse_duration"},
            },
        ),
    ]
    kept, stats = filter_think_ops(ops, allowed_paths=["tools/duration.py"])
    assert len(kept) == 3
    assert stats["think_anchor_file_level"] == 2, "后两条没写 symbol"
    assert stats["think_vague"] == 1, "只有「覆盖边界情况」是真模糊"
    assert kept[2].payload["anchor"]["symbol"] == "parse_duration"


def test_thinking_prompt_carries_the_four_fixes_from_the_self_review() -> None:
    """自述提出的四条合理建议必须真的进了提示词（否则下一轮还犯）。"""
    prompt = build_thinking_prompt(thinking="草稿", round_index=1, c_max_tokens=2000)
    assert '"symbol"' in prompt, "锚点要能细到符号"
    assert "不许只给类别" in prompt, "反对只写类别（覆盖边界情况）"
    assert "一条节点只讲一件事" in prompt, "反对用 / 把两条路线并列在一句"
    assert "不记实现手段" in prompt, "实现手段不该单独占一条节点"


def test_thinking_text_is_persisted_per_round(tmp_path) -> None:
    """提纯率的原料：原文必须落盘。

    只记 `thinking_chars` 这个数字的话，"提纯出来的几条节点够不够"就只能靠模型自己回忆 ——
    而这一轮恰恰要拿原文跟提纯结果对照着问（脚本 `scripts/_think_eval.py`）。
    """
    store = SessionStore(tmp_path, "sid-test")
    assert store.read_thinking(0) == "", "没写过就是空，不是抛异常"
    path = store.write_thinking(0, "先想 A，再否掉 A……")
    assert path == store.thinking_dir / "000.txt"
    assert store.read_thinking(0) == "先想 A，再否掉 A……"
    assert store.read_thinking(1) == ""


def test_negative_think_node_survives_with_its_default_salience() -> None:
    kept, stats = filter_think_ops(
        [_think_op("否掉用 strftime：它依赖本地时区，同样输入会给出不同结果", polarity="-")],
        allowed_paths=["tools/duration.py"],
    )
    assert stats["think_kept"] == 1
    assert kept[0].payload["polarity"] == "-"
    assert kept[0].payload["salience"] >= 70, "负节点不许被当低显著度丢掉"
    # 度量键即使是 0 也要在场：报告要能区分"没记"和"这一轮没发生"。
    assert stats["think_anchor_file_level"] == 1, "这条只有 path，没有 symbol"
    assert stats["think_vague"] == 0


def test_negative_think_nodes_are_trimmed_last() -> None:
    """预算不够时先丢普通事实，负推理节点保底（它拦住的是"重走已否掉的路"）。"""
    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "普通事实" * 30,
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N2",
                "statement": "否掉 strftime：依赖本地时区" * 5,
                "node_kind": "think",
                "polarity": "-",
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, report = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=150,  # 2 条 100 token 的节点 -> 只够留一条
        constraint_texts={},
        est_tokens_fn=lambda s: len(s.fact_chain) * 100,
    )
    assert [f.id for f in state.fact_chain] == ["N2"], report.trimmed


# --------------------------------------------------------------------------- #
# 推理编排器：输入是 thinking，不是 O_i
# --------------------------------------------------------------------------- #


def test_thinking_prompt_is_built_from_the_thinking_block_only() -> None:
    client = KeepClient()
    comp = ThinkingNodeCompressor(client=client)  # type: ignore[arg-type]
    comp.compress(
        "这是正文，里面有 ```file: 块``` 和结论",
        prior_state=CompressedState(),
        round_index=3,
        c_max_tokens=2000,
        landed=["tools/duration.py"],
        thinking="让我想想……不对，还是先用 divmod，因为它不依赖时区。",
    )
    prompt = client.prompts[0]
    assert "divmod" in prompt
    assert "这是正文" not in prompt, "正文不许进推理编排器"
    assert "tools/duration.py" in prompt, "动作清单必须给它（无锚不留的依据）"


def test_empty_thinking_makes_no_call_and_is_not_an_error() -> None:
    comp = ThinkingNodeCompressor(client=ExplodingClient())  # type: ignore[arg-type]
    delta = comp.compress(
        "正文", prior_state=CompressedState(), round_index=0, c_max_tokens=2000, thinking="  "
    )
    assert delta.ops == []
    assert delta.error is None, "没有 thinking 是正常情况，不是压缩器失败"


def test_scope_thinking_selects_the_thinking_compressor() -> None:
    cfg = SessionConfig(
        project="general",
        compression=CompressionConfig(mode="fact_chain", model="deepseek-flash", scope="thinking"),
    )
    assert isinstance(build_compressor(cfg), ThinkingNodeCompressor)
    cfg.compression.scope = "full"
    assert not isinstance(build_compressor(cfg), ThinkingNodeCompressor)


def test_unknown_scope_is_rejected_loudly() -> None:
    """写错 scope 不许静默按 full 跑 —— 那会被当成"机制没效果"。"""
    try:
        CompressionConfig(scope="thingking")
    except ValueError as exc:
        assert "scope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("scope 拼错居然通过了校验")


def test_thinking_prompt_keeps_the_fixed_part_in_front() -> None:
    """前缀缓存不变量：逐字不变的文字全在前、每轮变的全在后。"""
    common = {"prior_nodes": (), "c_max_tokens": 2000, "out_max_tokens": 1200}
    a = build_thinking_prompt(thinking="甲", round_index=1, landed=["a.py"], act_units=["u1"], **common)
    b = build_thinking_prompt(thinking="乙", round_index=2, landed=["b.py"], act_units=["u2"], **common)
    first_diff = next(i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y)
    assert first_diff > 1000, f"可变内容出现得太早（第 {first_diff} 个字符）"


def test_rendered_state_marks_act_and_think_nodes() -> None:
    state = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="写入 tools/duration.py（812B，sha256 aaaaaaaaaaaa）",
                node_kind="act",
                anchor=Anchor(path="tools/duration.py"),
            ),
            FactNode(
                id="N2",
                statement="否掉 strftime：依赖本地时区",
                node_kind="think",
                polarity="-",
                anchor=Anchor(path="tools/duration.py"),
            ),
        ]
    )
    text = render_state_block(state)
    assert "⟨ACT⟩" in text
    assert "⟨THINK -⟩" in text


def test_two_think_nodes_on_the_same_file_are_both_kept() -> None:
    """THINK 的身份是"它说了什么"，不是它挂在哪个文件上。

    真机（会话 910b395b69da4180）：一轮 3 条正节点只活下来 2 条，另一轮 4 条自我评估节点
    （含 2 条负节点）**全被按锚点当复述吞掉** —— 而负节点正是最贵的那一类。
    """
    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "选 divmod 拆 3600/60",
                "node_kind": "think",
                "polarity": "+",
                "anchor": {"path": "tools/duration.py"},
            },
        ),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N2",
                "statement": "否掉 strftime：依赖本地时区，同样输入会给出不同结果",
                "node_kind": "think",
                "polarity": "-",
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, report = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    assert [f.id for f in state.fact_chain] == ["N1", "N2"]
    assert not [x for x in report.applied if "(restated)" in x]


def test_plain_facts_on_the_same_anchor_are_still_deduped() -> None:
    """按锚点去重对 fact 仍然生效（否则每轮重写同一个文件都会灌进重复节点）。"""
    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "format_duration 定义在 tools/duration.py",
                "anchor": {"path": "tools/duration.py", "symbol": "format_duration"},
            },
        ),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N2",
                "statement": "duration 模块给出 hh:mm:ss 字符串",
                "anchor": {"path": "tools/duration.py", "symbol": "format_duration"},
            },
        ),
    ]
    state, report = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    assert [f.id for f in state.fact_chain] == ["N1"]
    assert any("(restated)" in x for x in report.applied)


def test_think_anchor_paths_include_paths_with_prior_act_nodes() -> None:
    prior = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="写入 tools/duration.py（700B）",
                node_kind="act",
                anchor=Anchor(path="tools/duration.py"),
            )
        ]
    )
    allowed = think_anchor_paths(prior, [("tools/other.py", "b" * 64, 10)])
    assert allowed == {"tools/duration.py", "tools/other.py"}


# --------------------------------------------------------------------------- #
# 第四轮的裁定：给"不做 X"一条槽位；没定的事不许写成定了的事
# （来源：模型自己的对照自评，会话 5914fb8d00184172 —— 6.5/10，扣分全在这两条）
# --------------------------------------------------------------------------- #


def _open_op(statement: str, *, polarity: str = "+", **extra: object) -> DeltaOp:
    payload: dict = {
        "id": "N9",
        "statement": statement,
        "node_kind": "think",
        "polarity": polarity,
        "anchor": {"path": "tools/duration.py"},
        **extra,
    }
    return DeltaOp(op="ADD_FACT", payload=payload)


def test_an_unsettled_node_is_marked_open_and_counted() -> None:
    """`assertion: open` 要在节点上留下来，并进 telemetry —— "还没定"必须被数出来。"""
    kept, stats = filter_think_ops(
        [_open_op("是否把布尔值当合法输入：待定", assertion="open")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept[0].payload["assertion"] == "open"
    assert stats["think_open"] == 1


def test_assertion_defaults_to_settled_and_unknown_values_do_not_drop_the_node() -> None:
    """压缩器写出别的词不能导致整条节点被丢（丢一条节点比默认 settled 更贵）。"""
    kept, stats = filter_think_ops(
        [_open_op("选 divmod 拆秒", assertion="maybe"), _open_op("选 f-string 补零")],
        allowed_paths=["tools/duration.py"],
    )
    assert [op.payload["assertion"] for op in kept] == ["settled", "settled"]
    assert stats["think_open"] == 0


def test_open_think_node_is_rendered_with_a_pending_mark() -> None:
    """下一轮必须一眼看出这条不能当依据 —— 所以标记要出现在正文里，不只在字段里。"""
    state = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="是否测 True：待定",
                node_kind="think",
                polarity="+",
                assertion="open",
                anchor=Anchor(path="tools/duration.py", resolved=True),
            ),
            FactNode(
                id="N2",
                statement="选 divmod 拆秒",
                node_kind="think",
                polarity="+",
                anchor=Anchor(path="tools/duration.py", resolved=True),
            ),
        ]
    )
    text = render_state_block(state)
    assert "⟨待定⟩ 是否测 True：待定" in text
    assert "⟨待定⟩ 选 divmod" not in text


def test_settled_node_keeps_its_plain_tag() -> None:
    state = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="选 divmod 拆秒",
                node_kind="think",
                polarity="-",
                anchor=Anchor(path="tools/duration.py", resolved=True),
            )
        ]
    )
    text = render_state_block(state)
    assert "⟨THINK -⟩ 选 divmod 拆秒" in text
    assert "⟨待定⟩" not in text


def test_a_refusal_node_is_counted_as_a_negative_constraint() -> None:
    """提示词规则 10 是否生效，只能靠这条计数看出来（0 与"没记"是两件事）。"""
    kept, stats = filter_think_ops(
        [
            _open_op("不新建 tools/__init__.py（单文件工具不需要包结构）", polarity="-"),
            _open_op("否掉用 strftime：它依赖本地时区", polarity="-"),
        ],
        allowed_paths=["tools/duration.py"],
    )
    assert stats["think_kept"] == 2
    assert stats["think_negative_constraint"] == 1
    assert kept[0].payload["salience"] >= 80, "克制类节点不该是低显著度"


def test_open_think_nodes_are_trimmed_late() -> None:
    """预算不够时，未定节点排在普通事实后面被丢（丢了就把上一轮的犹豫当已定结论用）。"""
    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "普通事实" * 30,
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N2",
                "statement": "是否测 True：待定" * 5,
                "node_kind": "think",
                "polarity": "+",
                "assertion": "open",
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, report = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=150,
        constraint_texts={},
        est_tokens_fn=lambda s: len(s.fact_chain) * 100,
    )
    assert [f.id for f in state.fact_chain] == ["N2"], report.trimmed


def test_assertion_survives_a_supersede() -> None:
    """取代节点要继承 assertion：否则"待定"会在随后的每轮更新里被洗成"已定"。"""
    ops = [
        _open_op("侧向：先按边界情况写测试", assertion="open"),
        DeltaOp(
            op="SUPERSEDE_FACT",
            target="N9",
            payload={
                "id": "N10",
                "statement": "侧向：按边界值目录写测试",
                "node_kind": "think",
                "polarity": "+",
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, _ = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    assert state.fact("N10").assertion == "open"


def test_thinking_prompt_asks_for_both_fixes() -> None:
    """提示词是这两条修复的入口，写没写必须能测。"""
    prompt = build_thinking_prompt(thinking="……", round_index=1, c_max_tokens=2000)
    assert "不做 X" in prompt
    assert '"assertion": "open"' in prompt
    assert "⟨待定⟩" in prompt


def test_thinking_prompt_tells_the_model_to_anchor_intra_file_tradeoffs() -> None:
    """模型自述的第二个缺口：否掉导入路线/测试框架这类**同一文件内的**分歧没有节点。

    原因是它对"无锚不留"的理解过严 —— 提示词要点明这类取舍挂本文件即可。
    """
    prompt = build_thinking_prompt(thinking="……", round_index=1, c_max_tokens=2000)
    assert "同一个文件内部的方案分歧" in prompt
    assert "导入方式" in prompt


# --------------------------------------------------------------------------- #
# 验证状态由 server 渲染进 ACT 节点（模型自述第 3 条：测试没跑只记在【未解决】里）
# --------------------------------------------------------------------------- #


def test_verification_clause_says_tests_did_not_run() -> None:
    clause, state = describe_verification(None)
    assert clause == "；本轮未跑测试"
    assert state == "none"


def test_verification_clause_reports_a_passing_suite() -> None:
    clause, state = describe_verification(True, "4 passed")
    assert clause == "；测试通过（4 passed）"
    assert state == "passed"


def test_verification_clause_reports_a_failing_suite() -> None:
    clause, state = describe_verification(False, "1 failed")
    assert clause == "；测试未通过（1 failed）"
    assert state == "failed"


def test_act_node_carries_the_verification_state() -> None:
    """ACT 节点必须自己带上"验证到哪一步"——下一轮不能靠【未解决】去猜。"""
    clause, state = describe_verification(None)
    delta, stats = with_act_nodes(
        DeltaC(ops=[]),
        prior_state=CompressedState(),
        written=[("tools/duration.py", "a" * 64, 542)],
        verify=clause,
        verify_state=state,
    )
    assert stats["act_verified"] == "none"
    statement = delta.ops[0].payload["statement"]
    assert statement.startswith("写入 tools/duration.py")
    assert statement.endswith("；本轮未跑测试")
    assert "sha256 aaaaaaaaaaaa" in statement


# --------------------------------------------------------------------------- #
# 第五轮：`why`（提示词从第一版就在要，服务端从来没有这个字段）与 `risk`
# 来源：模型第二轮自述 7.5/10 —— "只知道结论，不知道当初为什么这么定"、
# "把风险这一层压平了，需要能表达'还没验证/可能踩坑'的槽位"。
# --------------------------------------------------------------------------- #


def test_why_and_risk_survive_apply_delta() -> None:
    state, _ = apply_delta(
        CompressedState(),
        DeltaC(
            ops=[
                _open_op(
                    "测试文件不建包，靠 sys.path 插自身目录",
                    why="从 tools/ 下跑和从仓库根跑都要能导入",
                    risk="没有 __init__.py 时 unittest discover 可能进不去",
                )
            ]
        ),
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    node = state.fact("N9")
    assert node.why.startswith("从 tools/ 下跑")
    assert node.risk.startswith("没有 __init__.py")


def test_why_and_risk_are_rendered_in_the_state_block() -> None:
    """只存不渲染等于没存 —— `why` 上一版就是这么被静默吃掉的。"""
    state = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="测试文件不建包",
                node_kind="think",
                polarity="+",
                why="两种运行入口都要能导入",
                risk="discover 可能递归不进 tools/",
                anchor=Anchor(path="tools/test_duration.py", resolved=True),
            )
        ]
    )
    text = render_state_block(state)
    assert "｜依据：两种运行入口都要能导入" in text
    assert "｜风险：discover 可能递归不进 tools/" in text


def test_a_risk_node_is_counted_and_trimmed_late() -> None:
    _kept, stats = filter_think_ops(
        [_open_op("测试文件不建包", risk="discover 可能进不去")],
        allowed_paths=["tools/duration.py"],
    )
    assert stats["think_risk"] == 1
    assert stats["think_open"] == 0, "有风险不等于未定"

    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "普通事实" * 30,
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N2",
                "statement": "测试文件不建包" * 8,
                "node_kind": "think",
                "polarity": "+",
                "risk": "discover 可能进不去",
                "salience": 1,
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, report = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=150,
        constraint_texts={},
        est_tokens_fn=lambda s: len(s.fact_chain) * 100,
    )
    assert [f.id for f in state.fact_chain] == ["N2"], report.trimmed


def test_why_and_risk_survive_a_supersede() -> None:
    ops = [
        _open_op("测试文件不建包", why="两种入口都要能导入", risk="discover 进不去"),
        DeltaOp(
            op="SUPERSEDE_FACT",
            target="N9",
            payload={
                "id": "N10",
                "statement": "测试文件不建包，改在测试里显式给路径",
                "node_kind": "think",
                "polarity": "+",
                "anchor": {"path": "tools/duration.py"},
            },
        ),
    ]
    state, _ = apply_delta(
        CompressedState(),
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=100000,
        constraint_texts={},
        est_tokens_fn=lambda s: 1,
    )
    assert state.fact("N10").risk == "discover 进不去"


def test_thinking_prompt_asks_for_why_and_risk() -> None:
    prompt = build_thinking_prompt(thinking="……", round_index=1, c_max_tokens=2000)
    assert "已知风险单独一个槽位" in prompt
    assert '"risk"' in prompt
    assert "`why` 是必填的" in prompt


def test_thinking_prompt_forbids_squeezing_the_contract_into_vague_words() -> None:
    """第六轮：模型把"负数 ValueError、非 int/bool TypeError"压成"拒绝非法输入"。

    根因是规则 9 被过度套用（它把异常类型当成了"实现手段"），所以规则 9 里必须点明
    对外契约包含异常类型与测试输入清单。
    """
    prompt = build_thinking_prompt(thinking="……", round_index=1, c_max_tokens=2000)
    assert "别把" in prompt and "对外契约" in prompt
    assert "异常类型" in prompt
    assert "测试覆盖了哪些具体输入" in prompt


# --------------------------------------------------------------------------- #
# 第七轮「只压不补」：编排器把原文没有的东西写成上一轮的态度
# 来源：会话 50693faf271b4fdd（原文只有 388 字、没提 __init__.py 也没提 bool/1.5，
# 节点里却多出一条"不新建 __init__.py"和一条"布尔值/1.5 待定"）。
# --------------------------------------------------------------------------- #


def test_an_invented_open_node_is_dropped_when_the_source_never_mentioned_it() -> None:
    source = "Just format. Keep it simple, stdlib only. Handle large values? presumably."
    kept, stats = filter_think_ops(
        [_open_op("布尔值与非整数的处置待定", assertion="open")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert kept == []
    assert stats["think_dropped_ungrounded"] == 1
    assert stats["think_ungrounded"] == 1


def test_a_grounded_open_node_survives() -> None:
    source = "Handle large values? Hours can exceed 99 presumably."
    kept, stats = filter_think_ops(
        [_open_op("超过 99 小时该不该报错：待定", assertion="open")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert stats["think_kept"] == 1
    assert stats["think_ungrounded"] == 0
    assert kept[0].payload["assertion"] == "open"


def test_an_ungrounded_plain_node_is_counted_but_not_dropped() -> None:
    """普通正节点只计数：绝大多少是改写措辞，为此丢节点代价更大。"""
    source = "Just format it cleanly."
    _kept, stats = filter_think_ops(
        [_open_op("用 divmod 拆小时分钟秒")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert stats["think_kept"] == 1
    assert stats["think_ungrounded"] == 1
    assert stats["think_dropped_ungrounded"] == 0


def test_grounding_check_is_off_when_no_source_is_given() -> None:
    """老调用方（不传 source）行为不变。"""
    _kept, stats = filter_think_ops(
        [_open_op("布尔值与非整数的处置待定", assertion="open")],
        allowed_paths=["tools/duration.py"],
    )
    assert stats["think_kept"] == 1
    assert stats["think_ungrounded"] == 0


def test_thinking_prompt_forbids_completing_what_the_source_never_said() -> None:
    prompt = build_thinking_prompt(thinking="……", round_index=1, c_max_tokens=2000)
    assert "只压不补" in prompt
    assert "空是合格答案" in prompt


# --------------------------------------------------------------------------- #
# 第八轮：标签由服务端兜底（提示词规则 10/11 的机制化版本）
# 来源：会话 ba902da26e6c4b20 的自述与我的逐条核验 —— 原文 L5 明写"不知道应否"，
# 节点里却是 `assertion: settled`；原文 L3 的"format_duration 或 format_seconds?"
# 那条被否掉的备选名根本没进 C。两条都是 prompt 已要求、实测没执行的情况。
# --------------------------------------------------------------------------- #


def test_an_undecided_clause_in_the_source_forces_the_node_open() -> None:
    source = "覆盖 0/59/3600。还有 bool? 根据实现 type is int，bool 会 TypeError。测试包括。不知道应否。"
    kept, stats = filter_think_ops(
        [_open_op("校验 type(seconds) is not int 抛 TypeError；用 type is int 以排除 bool")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert stats["think_kept"] == 1
    assert kept[0].payload["assertion"] == "open", "原文说了不知道应否，节点不许写成已定"
    assert stats["think_forced_open"] == 1
    assert stats["think_open"] == 1


def test_a_rhetorical_question_that_the_source_answers_does_not_force_open() -> None:
    """`是否`/`要不要`/`可能` 这类通常下一句就自己回答了 —— 收窄信号集就是为了不让它们误伤。"""
    source = "若 >359999，hours 是否仍然输出更多位数？用 f-string 自然三位。合理。要不要写兼容导入？"
    kept, stats = filter_think_ops(
        [_open_op("hours 超过 99 不截断，用 f-string 输出更多位数")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert kept[0].payload["assertion"] == "settled"
    assert stats["think_forced_open"] == 0


def test_one_generic_shared_token_is_not_enough_to_force_open() -> None:
    """`int`/`type` 这种通用词单独命中不算 —— 第一版就是这样一轮降级 6 条的。"""
    source = "还有 bool? 根据实现 type is int，bool 会 TypeError。测试包括。不知道应否。"
    kept, stats = filter_think_ops(
        [_open_op("选 format_duration(seconds: int) -> str 作为唯一入口")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert kept[0].payload["assertion"] == "settled"
    assert stats["think_forced_open"] == 0


def test_uncertainty_far_from_the_nodes_subject_does_not_leak() -> None:
    source = "不知道应否加个缓存。\n" + "无关内容 " * 40 + "\n输入校验用 type(seconds) is not int。"
    kept, stats = filter_think_ops(
        [_open_op("校验 type(seconds) is not int")],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    assert kept[0].payload["assertion"] == "settled", "窗口只覆盖话题附近，不跨段传染"
    assert stats["think_forced_open"] == 0


def test_the_window_covers_the_topics_own_cluster_but_not_the_rest_of_the_paragraph() -> None:
    """真机原文那一句：`bool` 在"不知道应否"前 13 字（该罩住），`unittest` 在 90 字外（不该罩）。

    第一版窗口给 ±160 字，这一轮 8 条节点被改成 7 条 ⟨待定⟩ —— 满屏待定等于没有待定。
    """
    source = (
        "测试用 unittest，覆盖 0、59、60、3599、3600、3725、大值、类型错误、负值。"
        "还有 bool? 根据实现 type is int，bool 会 TypeError。测试包括。不知道应否。"
    )
    kept, stats = filter_think_ops(
        [
            _open_op("校验 type(seconds) is not int 抛 TypeError，用 type is int 排除 bool"),
            _open_op("测试用 unittest 覆盖 0、59、60、3599、3600、3725、大值、类型错误、负值"),
        ],
        allowed_paths=["tools/duration.py"],
        source=source,
    )
    bool_node = next(op for op in kept if "bool" in op.payload["statement"])
    unittest_node = next(op for op in kept if "unittest" in op.payload["statement"])
    assert bool_node.payload["assertion"] == "open"
    assert unittest_node.payload["assertion"] == "settled", "同段但不同话题，不该被一起降级"
    assert stats["think_forced_open"] == 1


def test_a_purely_negative_statement_gets_negative_polarity_from_the_server() -> None:
    """提示词规则 10 要求这类节点自己标 `polarity="-"`；实测没标 —— 服务端改标签。"""
    kept, stats = filter_think_ops(
        [_think_op("不新建 tools/__init__.py（单文件工具不需要包结构）", polarity="+")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept[0].payload["polarity"] == "-"
    assert kept[0].payload["salience"] >= 80, "负节点要拿到『负节点保底』这条裁剪规则的保护"
    assert stats["think_forced_negative"] == 1
    assert stats["think_negative_clause"] == 1


def test_a_mixed_statement_keeps_its_polarity_but_gets_the_floor() -> None:
    """「测试用 unittest 覆盖 …；不引入 pytest」不是一条否决 —— 别整条改标签。"""
    kept, stats = filter_think_ops(
        [_think_op("测试用 unittest 覆盖 0/59/3600；不引入 pytest", polarity="+")],
        allowed_paths=["tools/duration.py"],
    )
    assert kept[0].payload["polarity"] == "+"
    assert kept[0].payload["salience"] >= 80
    assert stats["think_forced_negative"] == 0
    assert stats["think_negative_clause"] == 1


def test_a_rejected_alternative_becomes_a_negative_node() -> None:
    """原文"函数名 format_duration 或 format_seconds?"→ 服务端补一条「未采用 format_seconds」。"""
    thinking = "函数名 format_duration 或 format_seconds? 用户没指定。我们用 format_duration。"
    ops = build_alternative_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "def format_duration(seconds: int) -> str:\n    ...\n"},
        thinking=thinking,
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert payload["polarity"] == "-"
    assert payload["node_kind"] == "think"
    assert payload["salience"] >= 80
    assert "format_seconds" in payload["statement"]
    assert payload["anchor"] == {"path": "tools/duration.py"}


def test_an_alternative_already_mentioned_elsewhere_is_skipped() -> None:
    """`unittest 还是 pytest`：pytest 已经在节点里被管着了，别再造一条。"""
    prior = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="不引入 pytest 或任何第三方依赖，测试只用 unittest",
                polarity="-",
                node_kind="think",
            )
        ]
    )
    ops = build_alternative_ops(
        prior_state=prior,
        sources={"tools/test_duration.py": "import unittest\n"},
        thinking="可能使用 unittest 还是 pytest？为了只用标准库，写 unittest。",
    )
    assert ops == []


def test_a_pair_of_names_that_both_landed_is_not_a_rejection() -> None:
    ops = build_alternative_ops(
        prior_state=CompressedState(),
        sources={
            "tools/duration.py": "def format_duration() -> str: ...\n",
            "tools/legacy.py": "def format_seconds() -> str: ...\n",
        },
        thinking="用 format_duration 或 format_seconds 都可以，两个都写了。",
    )
    assert ops == []


def test_a_drafted_import_that_never_landed_becomes_a_negative_node() -> None:
    """真机那句「`from __future__ import annotations` 不必要。可以去掉」在原文里 —— C 里没有。

    这是"被否掉的具体物"里唯一**可完全验证**的一类：草稿写过、最终文件里没有。
    """
    thinking = "先起草：\nfrom __future__ import annotations\n\n它是 future，不必要。可以去掉。"
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "import sys\n\n\ndef format_duration(seconds: int) -> str:\n    ...\n"},
        thinking=thinking,
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert payload["polarity"] == "-"
    assert payload["node_kind"] == "think"
    assert payload["salience"] >= 80
    assert "__future__" in payload["statement"]
    assert "tools/duration.py" in payload["why"]
    assert payload["anchor"] == {"path": "tools/duration.py"}


def test_a_drafted_import_that_was_kept_is_not_a_node() -> None:
    """草稿里的 import 最终真写进去了 —— 那不是克制，别造负节点。"""
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "from __future__ import annotations\n\nimport sys\n"},
        thinking="`from __future__ import annotations` 要留着，类型注解会用到。",
    )
    assert ops == []


def test_a_drafted_import_already_in_the_model_ops_is_skipped() -> None:
    """模型自己已经记过这条依赖 —— 服务端不要重复造一条。"""
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "import sys\n"},
        thinking="起草里有 `annotations`，但：\nfrom __future__ import annotations\n\n不必要，去掉。",
        known_extra="不采用 __future__ 这个依赖",
    )
    assert ops == []


def test_an_inline_mention_of_an_import_is_not_a_drafted_import() -> None:
    """散文里提一句 import 不算起草 —— 放宽这条会凭空造出「未采用 X」的假节点。"""
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "import sys\n"},
        thinking="我参考了 `from __future__ import annotations` 这个写法，但没写进最终文件。",
    )
    assert ops == []


def test_a_dropped_import_is_anchored_to_the_file_it_was_drafted_in() -> None:
    """真机实例（会话 213cfe52ae2d4236 的 N11）：`import os` 写在**测试文件**的中间稿里，
    被锚到了 `tools/duration.py`，模型自述直接点出"这条信息本身可能锚错了"。

    归属按"这条 import 往前最近提到的文件"判：草稿是按文件顺序贴的。
    """
    sources = {
        "tools/duration.py": "import sys\n\n\ndef format_duration(seconds: int) -> str:\n    ...\n",
        "tools/test_duration.py": "import sys\nimport unittest\nfrom pathlib import Path\n",
    }
    thinking = (
        "先写 tools/duration.py：\n```python\nimport sys\n```\n\n"
        "再写 tools/test_duration.py，先想着用 os：\n```python\nimport os, sys\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n```\n"
        "后来换成 pathlib 更干净。\n"
    )
    ops = build_dropped_import_ops(
        prior_state=CompressedState(), sources=sources, thinking=thinking
    )
    assert len(ops) == 1
    assert ops[0].payload["anchor"] == {"path": "tools/test_duration.py"}
    assert "tools/test_duration.py" in ops[0].payload["why"]


def test_the_same_module_imported_two_ways_is_not_a_dropped_dependency() -> None:
    """真机实例（会话 5f24337359ac4c39 的 N9）：草稿写 `from tools.duration import …`，
    最终文件里是 `from duration import …` —— 同一个模块换了个写法，不是被砍的依赖。
    """
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={
            "tools/test_duration.py": "import sys\nfrom duration import format_duration\n"
        },
        thinking="先试 `from tools.duration import format_duration`，"
        "但直接跑脚本时根不在 sys.path，改成：\nfrom duration import format_duration\n",
    )
    assert ops == []


def test_dropped_import_ops_are_counted_by_with_act_nodes() -> None:
    """整条流水线（session.py 第 6 步）要真的把这类负节点接到 ΔC 上。"""
    delta, stats = with_act_nodes(
        DeltaC(ops=[]),
        prior_state=CompressedState(),
        written=[("tools/duration.py", "b" * 64, 812)],
        sources={"tools/duration.py": "import sys\n"},
        thinking="起草里有：\nfrom __future__ import annotations\n\n但不必要，去掉。",
    )
    assert stats["think_dropped_deps"] == 1
    kinds = [(op.payload or {}).get("node_kind") for op in delta.ops]
    assert "think" in kinds
    ids = [(op.payload or {}).get("id") for op in delta.ops]
    assert len(ids) == len(set(ids)), "id 不能撞号（撞号会被 apply_delta 拒收）"


def test_server_made_negative_nodes_read_like_constraints() -> None:
    """真机自述第 2 点：服务端补的负节点没有 `why`、写的是"未采用…"这种历史陈述，
    下一轮读起来不是"别再选它"，约束力不够。"""
    ops = build_alternative_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import unittest\n"},
        thinking="用 unittest 还是 pytest？为了只用标准库，写 unittest。",
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert "不再采用" in payload["statement"]
    assert payload["why"], "服务端生成的负节点必须带依据"

    dep = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "import sys\n"},
        thinking="起草里有：\nfrom __future__ import annotations\n\n但不必要，去掉。",
    )
    assert len(dep) == 1
    assert "不再引入" in dep[0].payload["statement"]
    assert "__future__" in dep[0].payload["why"]


def test_an_answered_question_moves_out_of_unresolved() -> None:
    """C 里"确认 tools/ 是否需要 __init__.py：当前按命名空间包处理，未新建"这种条目
    自己就带结论，留在【未解决】里会被下一轮当悬案去"确认"（真机自述第 1 条）。"""
    unresolved_op = DeltaOp(
        op="SET_UNRESOLVED",
        payload={
            "items": [
                "确认 tools/ 是否需要 __init__.py：当前按命名空间包处理，未新建",
                "测试还没跑过，需要确认 unittest 用例全绿",
            ]
        },
    )
    delta, stats = with_act_nodes(
        DeltaC(ops=[unresolved_op]),
        prior_state=CompressedState(),
        written=[("tools/duration.py", "c" * 64, 812)],
        sources={"tools/duration.py": "import sys\n"},
    )
    assert stats["unresolved_answered"] == 1
    # 留在【未解决】里的只剩真正没结论的那条
    tail = [op for op in delta.ops if op.op == "SET_UNRESOLVED"][-1]
    assert tail.payload["items"] == ["测试还没跑过，需要确认 unittest 用例全绿"]
    node = next(op for op in delta.ops if "不再悬置" in str((op.payload or {}).get("statement")))
    assert node.payload["polarity"] == "-"
    assert node.payload["salience"] >= 80
    assert node.payload["why"]


def test_an_open_question_stays_in_unresolved() -> None:
    """没有结论词的悬案一个字都不许动 —— 搬走它就是把"还没定"伪造成"已定"。"""
    unresolved_op = DeltaOp(
        op="SET_UNRESOLVED",
        payload={"items": ["确认 tools/ 是否需要 __init__.py"]},
    )
    delta, stats = with_act_nodes(
        DeltaC(ops=[unresolved_op]),
        prior_state=CompressedState(),
        written=[("tools/duration.py", "d" * 64, 812)],
        sources={"tools/duration.py": "import sys\n"},
    )
    assert stats["unresolved_answered"] == 0
    tail = [op for op in delta.ops if op.op == "SET_UNRESOLVED"][-1]
    assert tail.payload["items"] == ["确认 tools/ 是否需要 __init__.py"]


# ---- 准入判据（第十一轮）：负节点只留"从 P／盘上文件／别的节点推不出来"的那些 ----

P_CONSTRAINTS = [
    "只做这一轮被要求的事；不被要求的文件、模块、依赖一律不加",
    "不修改 tests/frozen 下的冻结测试，冻结测试只读",
    "回复与代码注释用中文",
]


def test_a_negative_node_that_only_restates_p_is_dropped() -> None:
    """复述 P 的负节点不占 C 的预算 —— P 每轮逐字都在 prompt 里。"""
    kept, stats = filter_think_ops(
        [_think_op("不修改 tests/frozen 下的冻结测试，冻结测试只读", polarity="-")],
        allowed_paths=["tools/duration.py"],
        constraint_texts=P_CONSTRAINTS,
    )
    assert kept == []
    assert stats["think_dropped_p_restated"] == 1


def test_a_negative_node_about_this_rounds_own_artifact_survives_the_p_gate() -> None:
    """具体到本轮产物的克制推不出来（G1 只说"不加没要求的东西"，没说 __init__.py 要不要）。"""
    kept, stats = filter_think_ops(
        [_think_op("不新建 tools/__init__.py（单文件工具不需要包结构）", polarity="-")],
        allowed_paths=["tools/duration.py"],
        constraint_texts=P_CONSTRAINTS,
    )
    assert len(kept) == 1
    assert stats["think_dropped_p_restated"] == 0


def test_the_p_gate_only_applies_to_negative_nodes() -> None:
    """丢正节点的信息损失不对称 —— 这条闸门只管负节点。"""
    kept, stats = filter_think_ops(
        [_think_op("不修改 tests/frozen 下的冻结测试，冻结测试只读", polarity="+")],
        allowed_paths=["tools/duration.py"],
        constraint_texts=P_CONSTRAINTS,
    )
    assert len(kept) == 1
    assert stats["think_dropped_p_restated"] == 0


# ---- 第三种形态：草稿里试过又放弃的**代码结构**（try/except 兜底导入）----

TRY_FALLBACK_THINKING = (
    "导入可以这样兜底：\n"
    "try:\n"
    "    from tools.duration import format_duration\n"
    "except ModuleNotFoundError:\n"
    "    from duration import format_duration\n\n"
    "不过最后还是先插 sys.path 再直接 import 更清楚。"
)


def test_a_drafted_try_except_fallback_that_never_landed_becomes_a_negative_node() -> None:
    """真机（会话 d8bf4b45a8db4827）自述里唯一可惜的丢失：try/except 兜底导入被否。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    ...\n"},
        thinking=TRY_FALLBACK_THINKING,
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert payload["polarity"] == "-"
    assert payload["node_kind"] == "think"
    assert payload["salience"] >= 80
    assert "try/except" in payload["statement"]
    assert "tools/test_duration.py" in payload["why"]
    assert payload["anchor"] == {"path": "tools/test_duration.py"}


def test_a_try_except_fallback_that_landed_is_not_a_node() -> None:
    """兜底真写进文件了 —— 那是采用，不是被否。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/test_duration.py": (
                "try:\n    from tools.duration import format_duration\nexcept ImportError:\n    pass\n"
            )
        },
        thinking=TRY_FALLBACK_THINKING,
    )
    assert ops == []


def test_a_route_hint_already_in_the_model_ops_is_skipped() -> None:
    """模型自己记过这条路线，服务端不要重复造。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import unittest\n"},
        thinking=TRY_FALLBACK_THINKING,
        known_extra="不采用 try/except 兜底导入，改用插 sys.path",
    )
    assert ops == []


def test_a_rejected_main_guard_becomes_a_negative_node() -> None:
    """真机原文："最好不要加 __main__ 功能，只做工具"，落盘的 tools/duration.py 里确实没有。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "def format_duration(seconds: int) -> str:\n    ...\n"},
        thinking='工具文件不加额外功能：\n    if __name__ == "__main__":\n        print(f)  # 可选？没有要求，不加。\n',
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert "__main__" in payload["statement"]
    assert payload["anchor"] == {"path": "tools/duration.py"}
    assert payload["polarity"] == "-"


def test_a_main_guard_that_landed_is_not_a_node() -> None:
    """入口块真写进文件了 —— 采用，不是被否。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/test_duration.py": 'if __name__ == "__main__":\n    unittest.main()\n'
        },
        thinking='测试文件里加不加 __main__？加上可以直接跑：\nif __name__ == "__main__":\n',
    )
    assert ops == []


def test_a_route_that_landed_elsewhere_is_not_a_node_for_the_other_file() -> None:
    """真机实例（会话 5f24337359ac4c39 的 N11）：入口块**真的写进了测试文件**，
    服务端只看到"差集里还剩 duration.py"，就造出「不再给 duration.py 加入口块」——
    而 duration.py 从头到尾没被讨论过。结构是按文件决定的，任何文件用了就不记。
    """
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/duration.py": "def format_duration(seconds: int) -> str:\n    ...\n",
            "tools/test_duration.py": 'import unittest\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
        },
        thinking='测试文件末尾应有：\nif __name__ == "__main__":\n    unittest.main()\n这样直接运行。\n',
    )
    assert ops == []


def test_a_route_is_attributed_to_the_nearest_named_file() -> None:
    """归属按"命中位置**往前**最近提到的带扩展名的文件名"：这里是 `tools/b.py`。

    归属判不出来（一个文件名都没提、且本轮写了不止一个文件）才不记：反例是
    `tools/a.py` 与 `tools/b.py` 都写了，原文只说"挑一个"。
    """
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/a.py": "x = 1\n",
            "tools/b.py": "y = 2\n",
        },
        thinking='tools/a.py 之后再定。tools/b.py 先试 try：\ntry:\n    import b\nexcept ImportError:\n    b = None\n',
    )
    assert len(ops) == 1
    assert ops[0].payload["anchor"] == {"path": "tools/b.py"}


def test_a_route_only_fires_for_a_file_the_source_talks_about() -> None:
    """多个候选文件时必须提到那个文件才记 —— 否则会为"原文没谈过的文件"造节点。"""
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/a.py": "x = 1\n",
            "tools/b.py": "y = 2\n",
        },
        thinking='挑一个：\nif __name__ == "__main__":\n    main()\n',
    )
    assert ops == []


def test_the_route_node_is_counted_by_with_act_nodes() -> None:
    delta, stats = with_act_nodes(
        DeltaC(ops=[]),
        prior_state=CompressedState(),
        written=[("tools/test_duration.py", "e" * 64, 812)],
        sources={"tools/test_duration.py": "import unittest\n"},
        thinking=TRY_FALLBACK_THINKING,
    )
    assert stats["think_rejected_routes"] == 1
    ids = [(op.payload or {}).get("id") for op in delta.ops]
    assert len(ids) == len(set(ids)), "id 不能撞号"


def test_a_route_the_model_already_recorded_is_not_duplicated() -> None:
    """真机实例：模型自己写了「不加 CLI / __main__ 入口」，服务端又补了一条 N12 同义节点。

    模型自述原话：「N4 和 N12 是同一件事拆成两条，冗余但无害」—— 判重不能用整条 label
    （label 是 `if __name__ == "__main__" 入口块`，模型正文里只有 `__main__`），要用特征串。
    """
    model_op = DeltaOp(
        op="ADD_FACT",
        payload={
            "id": "N4",
            "statement": "不加 CLI / __main__ 入口（用户只要求格式化函数，加 CLI 属范围蔓延）",
            "node_kind": "think",
            "polarity": "-",
            "why": "工具并不意味着要能直接跑，G1 说不加没被要求的东西",
        },
    )
    delta, stats = with_act_nodes(
        DeltaC(ops=[model_op]),
        prior_state=CompressedState(),
        written=[("tools/duration.py", "f" * 64, 812)],
        sources={"tools/duration.py": "def format_duration(seconds: int) -> str:\n    ...\n"},
        thinking='要不要加个入口？\nif __name__ == "__main__":\n    print(f(3725))\n就不加吧。\n',
    )
    assert stats["think_rejected_routes"] == 0
    assert not [
        op for op in delta.ops if "入口块" in str((op.payload or {}).get("statement") or "")
    ]


def test_the_route_marker_lookup_reads_the_models_why_field() -> None:
    """模型的理由写在 `why` 里（不是 `reason`）—— 判重要把 `why` 也算进"已记账"。"""
    model_op = DeltaOp(
        op="ADD_FACT",
        payload={
            "id": "N3",
            "statement": "导入方式上先插 sys.path 再直接 import",
            "node_kind": "think",
            "why": "试过 try/except 兜底导入，落盘文件里没有这条路",
        },
    )
    delta, stats = with_act_nodes(
        DeltaC(ops=[model_op]),
        prior_state=CompressedState(),
        written=[("tools/test_duration.py", "a" * 64, 812)],
        sources={"tools/test_duration.py": "import sys\nimport unittest\n"},
        thinking=TRY_FALLBACK_THINKING,
    )
    assert stats["think_rejected_routes"] == 0
    assert not [
        op for op in delta.ops if "兜底导入" in str((op.payload or {}).get("statement") or "")
    ]


# ---- 第三种形态的另一个入口：散文里写出来的方案对比「用 X，而不是 Y」----


def test_a_prose_alternative_becomes_a_negative_node() -> None:
    ops = build_alternative_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    ...\n"},
        thinking="测试框架上我决定用 unittest，而不是 pytest，因为约束要求只用标准库。",
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert payload["polarity"] == "-"
    assert "pytest" in payload["statement"]
    assert "tools/test_duration.py" in payload["why"]


def test_a_prose_comparison_where_both_names_landed_is_not_a_rejection() -> None:
    """两个名字都在落盘正文里出现 —— 不是二选一，别造负节点。"""
    ops = build_alternative_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import unittest\nimport pytest\n"},
        thinking="用 unittest，而不是 pytest 也行。",
    )
    assert ops == []


# ---- 散文里提到的 try/except 兜底（没贴代码块）也要认 ----
# 真机实例（会话 da5f070d64014d7b）：原文只写了一行"还把 `try/except ImportError` 这个方案
# 明确否掉了（'那更啰嗦'）"，没贴代码块，老正则认不出来，模型自述把它列进"方案层面的排除丢失"。


def test_a_prose_mention_of_try_except_fallback_is_recognised() -> None:
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/test_duration.py": "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n",
        },
        thinking="导入方案上，`try/except ImportError` 这个方案明确否掉了，太啰嗦。"
        "最后用 tools/test_duration.py 里的 Path 兜底。\n",
    )
    assert len(ops) == 1
    assert ops[0].payload["anchor"] == {"path": "tools/test_duration.py"}


def test_a_prose_mention_of_try_except_that_landed_is_not_a_node() -> None:
    ops = build_rejected_route_ops(
        prior_state=CompressedState(),
        sources={
            "tools/test_duration.py": "try:\n    from tools.duration import format_duration\nexcept ImportError:\n    from duration import format_duration\n",
        },
        thinking="`try/except ImportError` 这条路我留着，tools/test_duration.py 里就是它。\n",
    )
    assert ops == []


# ---- 识破但先搁置的风险 ----
# 真机自述（会话 da5f070d64014d7b 第 2 条）：「"如果别处也有 tools 包会不会冲突……不管了"，
# 这是已知风险 + 主动搁置，两个信息量都在。节点里完全没有。」


def test_a_shelved_risk_becomes_a_node() -> None:
    ops = build_shelved_risk_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import sys\n"},
        thinking="倒是如果别处也有 tools 包，`from tools.duration` 可能冲突。不管了，先这样。\n",
    )
    assert len(ops) == 1
    payload = ops[0].payload
    assert payload["node_kind"] == "think"
    assert "tools" in payload["risk"]
    assert "先搁置" in payload["statement"]
    assert payload["anchor"] == {"path": "tools/test_duration.py"}


def test_a_shelved_risk_already_recorded_is_skipped() -> None:
    ops = build_shelved_risk_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import sys\n"},
        thinking="如果别处也有 tools 包会不会冲突？不管了。\n",
        known_extra="已知风险：tools 包冲突不管了",
    )
    assert ops == []


def test_a_shelved_risk_without_a_concrete_subject_is_not_a_node() -> None:
    """"后面再说吧"这种没有具体物的搁置不记 —— 记了就是噪声。"""
    ops = build_shelved_risk_ops(
        prior_state=CompressedState(),
        sources={"tools/test_duration.py": "import sys\n"},
        thinking="嗯，还有一些细节，后面再说吧。\n",
    )
    assert ops == []


def test_shelved_risk_ops_are_counted_by_with_act_nodes() -> None:
    delta, stats = with_act_nodes(
        DeltaC(ops=[]),
        prior_state=CompressedState(),
        written=[("tools/test_duration.py", "c" * 64, 812)],
        sources={"tools/test_duration.py": "import sys\n"},
        thinking="如果别处也有 tools 包会不会冲突？不管了。\n",
    )
    assert stats["think_shelved_risks"] == 1
    kinds = [(op.payload or {}).get("risk") for op in delta.ops]
    assert any(k for k in kinds)


def test_the_same_file_written_twice_gets_one_act_node() -> None:
    """同一路径在一轮里被写了两遍 → 只出一条 ACT 节点。

    真机实例（会话 da5f070d64014d7b）：C 里出现两条字节数与 sha 完全相同的 ACT 节点。
    """
    ops = build_act_ops(
        prior_state=CompressedState(),
        written=[("tools/test_duration.py", "d" * 64, 812), ("tools/test_duration.py", "d" * 64, 812)],
        sources={"tools/test_duration.py": "import unittest\n"},
    )
    assert len(ops) == 1


def test_a_mechanism_made_node_quotes_the_source_for_its_reason() -> None:
    """服务端补的负节点，`why` 里要引**原文那句**，不能只写"比对落盘结果"。

    真机自述（会话 7b3e5f7c935f49b4 第 2 条）："N13/N14/N15 三条否定路线的依据写的是
    '服务端比对落盘结果'，等于机制在替推理做总结。它补上了'不该有的东西没出现'，
    但补不上'当时为什么否'。"
    """
    ops = build_dropped_import_ops(
        prior_state=CompressedState(),
        sources={"tools/duration.py": "import sys\n"},
        thinking="先起草：\nfrom __future__ import annotations\n\n它是 future，不必要。可以去掉。",
    )
    assert len(ops) == 1
    assert "原文：" in ops[0].payload["why"]
    assert "不必要" in ops[0].payload["why"]


def test_a_settled_rejection_is_never_marked_pending() -> None:
    """已定的否决不许被降级成 ⟨待定⟩ —— "不加 CLI"是决定，不是没定的事。

    真机实例（会话 7b3e5f7c935f49b4）："不加 CLI""不新建 __init__.py""不采用双路导入"
    三条已定的克制全被标成 `⟨THINK -⟩ ⟨待定⟩`（原文只是讨论过要不要加，最后明确不加）。
    """
    ops = [
        DeltaOp(
            op="ADD_FACT",
            payload={
                "id": "N1",
                "statement": "不加 CLI/argparse/main 入口",
                "anchor": {"path": "tools/duration.py"},
                "node_kind": "think",
                "polarity": "-",
            },
        )
    ]
    kept, stats = filter_think_ops(
        ops,
        allowed_paths=["tools/duration.py"],
        source="要不要加 CLI？用户没要求，不加。不确定要不要留着这个入口？不加，就这样。",
    )
    assert stats["think_forced_open"] == 0
    assert kept[0].payload["assertion"] == "settled"
    assert kept[0].payload["polarity"] == "-"
