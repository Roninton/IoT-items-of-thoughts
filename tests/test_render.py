"""prompt 组装：P/C 隔离必须体现在**产出的文本**里，而不是只体现在文档里。"""

from __future__ import annotations

from pathlib import Path

from pciso.config import CompressionConfig, SessionConfig
from pciso.models import Anchor, CompressedState, ConstraintStatus, FactNode
from pciso.prefix import PrefixBundle
from pciso.render import (
    build_compressor_prompt,
    build_prompt,
    est_tokens,
    render_state_block,
    render_user_words,
)

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / "prefixes" / "drawing_video"


def _prefix() -> PrefixBundle:
    return PrefixBundle.load(PREFIX)


def _state() -> CompressedState:
    return CompressedState(
        version=2,
        fact_chain=[
            FactNode(id="N1", statement="接口 a.f 已定义"),
            FactNode(id="N2", statement="旧的", status="superseded", superseded_by="N3"),
        ],
        failure_chain=[],
        progress="完成了绘图核心",
        unresolved=["接视频层"],
        constraints=[ConstraintStatus(id="C2", text="原文", status="satisfied", evidence="ok")],
    )


def _cfg(mode: str, **kw) -> SessionConfig:
    return SessionConfig(
        prefix_path=str(PREFIX),
        compression=CompressionConfig(mode=mode),  # type: ignore[arg-type]
        **kw,
    )


def test_state_block_excludes_superseded_facts() -> None:
    block = render_state_block(_state())
    assert "[N1]" in block
    assert "[N2]" not in block  # 失效节点不进 prompt


def test_state_block_sections_are_all_present() -> None:
    block = render_state_block(_state())
    for section in ("【事实链】", "【失败链】", "【当前进度】", "【未解决】", "【约束状态】"):
        assert section in block


def test_state_mode_prompt_contains_prefix_and_c() -> None:
    prompt = build_prompt(
        prefix=_prefix(), state=_state(), cfg=_cfg("fact_chain"), round_index=2, stage_index=3
    )
    assert "项目冻结前缀 P" in prompt
    assert "【事实链】" in prompt
    assert "本轮输出格式" in prompt


def test_chat_prompt_carries_the_users_own_words_verbatim() -> None:
    """C 不记用户指令（用户裁定），所以用户原话必须逐字回流 —— 否则上一轮的约束凭空消失。

    真机证据（会话 caf989977f5f4269）：第 1 轮说「只用标准库」，第 2 轮让模型自评提纯质量，
    它答"唯一真缺口就是这条约束没进任何节点"。
    """
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("fact_chain", chat_driven=True),
        round_index=2,
        stage_index=3,
        extra_instruction="改成 1 小时 2 分 5 秒",
        user_words=["只用标准库，别引第三方依赖", "产物固定叫 current-view.jpg"],
    )
    assert "用户原话（逐字，不压缩）" in prompt
    assert "1. 只用标准库，别引第三方依赖" in prompt
    assert "2. 产物固定叫 current-view.jpg" in prompt
    assert "改成 1 小时 2 分 5 秒" in prompt
    # 用户原话不能混进 C —— C 只放模型输出的压缩
    assert "只用标准库" not in render_state_block(_state())


def test_chat_prompt_keeps_the_section_when_there_are_no_earlier_words() -> None:
    """骨架不变量：空的时候给占位，不是把段落删掉。"""
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("fact_chain", chat_driven=True),
        round_index=0,
        stage_index=1,
    )
    assert "用户原话（逐字，不压缩）" in prompt
    assert "这是本会话的第一条用户消息" in prompt


def test_experiment_arms_do_not_get_the_user_words_section() -> None:
    """七个实验臂的 prompt 一个字节都不许变 —— 它们是对照数据。"""
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("fact_chain"),
        round_index=2,
        stage_index=3,
        user_words=["这句话不该出现在实验臂里"],
    )
    assert "用户原话" not in prompt
    assert "这句话不该出现在实验臂里" not in prompt


def test_user_words_are_trimmed_from_the_oldest_end() -> None:
    """预算只砍最旧的：新要求优先，且要写明丢了几条。"""
    words = [f"第 {i} 句" + "字" * 900 for i in range(1, 8)]
    body = render_user_words(words)
    assert "第 7 句" in body
    assert "第 1 句" not in body
    assert "因预算省略" in body


def _act(node_id: str, statement: str, round_no: int) -> FactNode:
    return FactNode(
        id=node_id,
        statement=statement,
        node_kind="act",
        source_round=round_no,
        anchor=Anchor(path="tools/duration.py", resolved=True),
    )


def test_verification_state_is_aggregated_into_one_line() -> None:
    """真机自述："验证状态散在几条节点里，状态层略薄" —— 汇成一行，且必须是推导出来的。"""
    state = CompressedState(
        fact_chain=[
            _act("N7", "写入 tools/duration.py（824B，sha256 abc）；本轮未跑测试", 1),
            _act("N8", "写入 tools/test_duration.py（1375B，sha256 def）；本轮未跑测试", 1),
        ]
    )
    text = render_state_block(state)
    assert "【验证状态】第 1 轮的物证 2 条：未跑测试" in text
    assert "N7、N8" in text


def test_verification_line_tracks_the_latest_round_only() -> None:
    """旧轮的 ACT 还挂在链上，但状态只按最近一轮说。"""
    state = CompressedState(
        fact_chain=[
            _act("N7", "写入 tools/a.py（1B，sha256 a）；本轮未跑测试", 1),
            _act("N9", "写入 tools/b.py（1B，sha256 b）；测试通过（4 passed）", 3),
        ]
    )
    line = next(ln for ln in render_state_block(state).splitlines() if "【验证状态】" in ln)
    assert "第 3 轮的物证 1 条：已跑测试且通过" in line
    assert "N9" in line and "N7" not in line


def test_no_act_nodes_means_no_verification_line() -> None:
    """这轮只回话不落文件 —— 不该凭空冒出一行验证状态。"""
    state = CompressedState(fact_chain=[FactNode(id="N1", statement="只回了话")])
    assert "【验证状态】" not in render_state_block(state)


def test_stateless_mode_prompt_has_neither_history_nor_c() -> None:
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("off", stateless=True),
        round_index=5,
        stage_index=6,
        history=["我上一轮写了很多东西"],
    )
    assert "项目冻结前缀 P" in prompt
    assert "【事实链】" not in prompt
    assert "我上一轮写了很多东西" not in prompt


def test_full_history_mode_includes_raw_history() -> None:
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("off"),
        round_index=2,
        stage_index=3,
        history=["第一轮的完整输出"],
    )
    assert "第一轮的完整输出" in prompt
    assert "【事实链】" not in prompt


def test_summary_mode_keeps_recent_raw_and_summary() -> None:
    state = _state()
    state.summary = "前情摘要内容"
    cfg = SessionConfig(
        prefix_path=str(PREFIX),
        compression=CompressionConfig(mode="summary", summary_keep_recent=1),
    )
    prompt = build_prompt(
        prefix=_prefix(),
        state=state,
        cfg=cfg,
        round_index=3,
        stage_index=4,
        history=["第一轮", "第二轮"],
    )
    assert "前情摘要内容" in prompt
    assert "第二轮" in prompt
    assert "### 第 1 轮输出" not in prompt  # 只保留最近 k 轮


def test_prefix_frozen_off_withholds_p_content_but_keeps_the_section() -> None:
    """不冻结 P 时：P 的正文不进 prompt，但**段落仍在**（用固定占位代替）。

    段落保留是刻意的：骨架必须逐轮一致，否则"违反数随轮数的变化"无法归因。
    段落的**内容**才是本组的自变量。
    """
    prompt = build_prompt(
        prefix=_prefix(),
        state=_state(),
        cfg=_cfg("off", prefix_frozen=False),
        round_index=2,
        stage_index=3,
        history=["历史"],
    )
    assert "项目冻结前缀 P" in prompt  # 段落标题还在
    assert "滑出上下文窗口" in prompt  # 换成了固定占位
    assert "唯一事实来源：时间轴" not in prompt  # 但 P 的正文确实没进来


def test_stage_directive_follows_prefix_stages() -> None:
    prompt = build_prompt(
        prefix=_prefix(), state=_state(), cfg=_cfg("fact_chain"), round_index=3, stage_index=4
    )
    assert "视频生成接口" in prompt


def test_compressor_prompt_is_prefix_isolated_but_has_prior_structure() -> None:
    """压缩器看不到 P（前缀隔离），但能看到 C_{i-1} 的**结构头部** —— 更正传播的前提。"""
    prompt = build_compressor_prompt(
        o_i="本轮草稿",
        round_index=3,
        prior_nodes=["N1 接口 a.f 已定义"],
        prior_failures=["F1 试用 ffmpeg"],
        c_max_tokens=800,
    )
    assert "项目需求与架构约束" not in prompt  # 没有 P
    assert "N1" in prompt and "F1" in prompt  # 有结构头部
    assert "SUPERSEDE_FACT" in prompt  # 更正可以被表达
    assert "不能编造" in prompt


def test_est_tokens_is_cjk_aware() -> None:
    assert est_tokens("中文四个字") >= 5
    assert est_tokens("abcdefgh") == 2
    assert est_tokens("") == 0


# --------------------------------------------------------------------------- #
# 锚点没落地时，渲染必须说**真实的原因**
# --------------------------------------------------------------------------- #
# 真机事故（会话 46aa73d7e5b94ffa）：README.md 与 docs/ARCHITECTURE.md 确实在盘上，
# 只是压缩器给一份 markdown 编了符号锚点（`README.md::drawvid`），解析不到符号。
# 而渲染把"锚点没解析"一律写成"文件不存在"—— 于是模型读到"这两份文档不在盘上"，
# 据此判断"C5 的翻转没有可证明的因"，并把这套推理记成四条事实（N6–N10）。
# 下一轮它继续讨论这些噪声，真正的工作被挤掉。一句话的错，烧掉三轮。


def _state_with_anchor(
    path: str, symbol: str, detail: str, *, symbol_missing: bool = False
) -> CompressedState:
    return CompressedState(
        version=1,
        fact_chain=[
            FactNode(
                id="N3",
                statement="README.md 已落盘",
                anchor={
                    "path": path,
                    "symbol": symbol,
                    "resolved": False,
                    "detail": detail,
                    "symbol_missing": symbol_missing,
                },
            )
        ],
    )


def test_state_block_reports_symbol_missing_not_file_missing() -> None:
    block = render_state_block(_state_with_anchor("README.md", "drawvid", "符号不存在"))
    assert "[未落地]" in block
    assert "符号不存在" in block
    assert "文件不存在" not in block, "别把'符号不存在'渲染成'文件不存在' —— 那是另一件事"


def test_state_block_marks_a_missing_symbol_differently_from_a_missing_file() -> None:
    """文件在、符号名对不上 → `[符号未见]`，不是 `[未落地]`。

    真机自述（会话 `5f24337359ac4c39`）：N4 的用例清单被锚到一个不存在的方法名上，
    渲染成"未落地"之后，它读成"这些用例只是计划、没真写进文件" —— 而文件是写了的。
    """
    state = _state_with_anchor(
        "tools/test_duration.py",
        "test_format_duration",
        "文件在盘上，但里面没有 test_format_duration 这个名字（可能需要核对真名）",
        symbol_missing=True,
    )
    block = render_state_block(state)
    assert "[符号未见]" in block
    assert "[未落地]" not in block


def test_state_block_still_says_file_missing_when_it_really_is() -> None:
    block = render_state_block(_state_with_anchor("drawvid/core.py", "Size", "文件不存在"))
    assert "文件不存在" in block


def test_resolve_anchors_drops_symbols_on_non_python_paths(tmp_path: Path) -> None:
    """非 .py 文件上的符号锚点永远解析不到 —— 服务端必须把它丢掉，只按路径判定。

    否则一份真实存在的文档会被永久标成"未落地"，下一轮据此认为它没写。
    """
    from pciso.session import resolve_anchors

    (tmp_path / "README.md").write_text("# 项目\n", encoding="utf-8")
    state = _state_with_anchor("README.md", "drawvid", "")
    report = resolve_anchors(tmp_path, state)
    node = state.fact_chain[0]
    assert node.anchor.resolved is True
    assert node.anchor.symbol == ""
    assert report["unresolved"] == []
    assert "[未落地]" not in render_state_block(state)


def test_resolve_anchors_flags_missing_symbol_in_python(tmp_path: Path) -> None:
    from pciso.session import resolve_anchors

    (tmp_path / "core.py").write_text('"""核心。"""\n\n\ndef g() -> int:\n    """有。"""\n    return 1\n', encoding="utf-8")
    state = _state_with_anchor("core.py", "not_here", "")
    report = resolve_anchors(tmp_path, state)
    assert state.fact_chain[0].anchor.resolved is False
    assert "not_here" in " ".join(report["unresolved"])
    # 文件在、符号不在 → 用 `[符号未见]` 这个专用标记，别写成"未落地"
    # （真机自述把它读成"计划过但没落地"，而文件其实是写了的）。
    block = render_state_block(state)
    assert "[符号未见]" in block
    assert "not_here" in block


def test_resolve_anchors_accepts_a_module_level_constant(tmp_path: Path) -> None:
    """常量也是真实存在的符号。

    `TOOLS_DIR = Path(__file__).parent` 被判成"符号不存在"时，渲染会写 `[未落地]`
    + 符号不存在 —— 下一轮据此以为测试文件没有 sys.path 兼容逻辑、白找一轮
    （真机会话 `5914fb8d00184172`，模型自己把这条列为"会卡住的地方"第一条）。
    """
    from pciso.session import resolve_anchors

    (tmp_path / "test_x.py").write_text(
        "import sys\nfrom pathlib import Path\n\n"
        "TOOLS_DIR = Path(__file__).parent\n\n\n"
        'class T:\n    """用例。"""\n',
        encoding="utf-8",
    )
    state = _state_with_anchor("test_x.py", "TOOLS_DIR", "")
    report = resolve_anchors(tmp_path, state)
    assert state.fact_chain[0].anchor.resolved is True
    assert report["unresolved"] == []
    assert "[未落地]" not in render_state_block(state)


def test_compressor_prompt_carries_the_verifiable_landing_list() -> None:
    """压缩器必须拿到 server 侧的**真实落盘清单**：那是唯一可信的"已完成"依据。

    模型的正文会说"已落盘"，而盘上没有（真机踩过两次）——只靠提示词求它别把计划
    记成事实是不够的，得给它一份物证。
    """
    prompt = build_compressor_prompt(
        o_i="我先写 README.md，然后再写 docs/ARCHITECTURE.md",
        round_index=2,
        c_max_tokens=2000,
        landed=["README.md"],
    )
    assert "真实落盘清单" in prompt
    # 注意：这几个字在**固定段**里也出现了一次（"文末有 server 给出的真实落盘清单"），
    # 所以要按落盘清单那一段自己的标题切，而不是按第一次出现的位置切。
    landed_section = prompt.split("（逐字来自写盘结果", 1)[1].split("=== O_i 开始 ===")[0]
    assert "- README.md" in landed_section
    assert "- docs/ARCHITECTURE.md" not in landed_section, "清单里没有的路径不能被列成'已完成'"
    # 三条新规则：非 .py 不给符号 / 不记元评论 / 不记空转陈述
    assert "`symbol` 只对 `.py` 路径给" in prompt
    assert "元评论" in prompt
    assert "空转陈述" in prompt
    # 落盘清单属于**每轮都变**的部分，必须排在固定指令之后（前缀缓存的前提）
    assert prompt.index("真实落盘清单") > prompt.index("可用操作：")
