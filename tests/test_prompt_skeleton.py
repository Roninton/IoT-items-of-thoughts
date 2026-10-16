"""prompt 骨架的**不变量**：形态固定，只有值变。

这条比"内容对不对"重要得多。如果喂进极简模式的内容**形态**每轮不同
（有时多一段临时说明、有时少一段），那模型行为的差异就不可归因 ——
你分不清它来自 P/C 还是来自某轮多塞的那段话。
骨架稳定，才谈得上"违反数随轮数的变化是配置造成的"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso.config import SessionConfig
from pciso.models import CompressedState, ConstraintStatus, FactNode
from pciso.prefix import PrefixBundle
from pciso.render import (
    EMPTY_TASK_PLACEHOLDER,
    build_prompt,
    extract_skeleton,
)

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "one_sentence_edit"


def bundle() -> PrefixBundle:
    return PrefixBundle.load(PREFIX)


def state(round_index: int) -> CompressedState:
    """随轮数变化的 C：事实变多、进度变长。"""
    return CompressedState(
        version=round_index,
        fact_chain=[
            FactNode(id=f"N{i}", statement=f"第 {i} 条事实", unanchored=True)
            for i in range(1, round_index + 2)
        ],
        constraints=[ConstraintStatus(id="C2", text="原文", status="satisfied")],
        progress="x" * (10 * round_index),
        unresolved=[f"待办 {round_index}"],
    )


def cfg_for(mode: str, **kw: object) -> SessionConfig:
    return SessionConfig(
        prefix_path=str(PREFIX),
        compression={"mode": mode},  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def minimal_prompts(mode: str, **kw: object) -> list[str]:
    prefix = bundle()
    cfg = cfg_for(mode, **kw)
    return [
        build_prompt(
            prefix=prefix,
            state=state(i),
            cfg=cfg,
            round_index=i,
            stage_index=min(i + 1, 8),
            history=[f"第 {k} 轮的输出" for k in range(1, i + 1)],
        )
        for i in range(4)
    ]


# --------------------------------------------------------------------------- #
# 骨架逐轮一致
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["fact_chain", "off"])
def test_skeleton_is_identical_across_rounds(mode: str) -> None:
    """极简模式（state / none）：段落序列逐轮逐字相同。"""
    prompts = minimal_prompts(mode, stateless=(mode == "off"))
    skeletons = {tuple(extract_skeleton(p)) for p in prompts}
    assert len(skeletons) == 1, f"骨架随轮数变了: {skeletons}"


@pytest.mark.parametrize("mode", ["summary"])
def test_skeleton_stable_even_when_content_grows(mode: str) -> None:
    """B/D/E 的内容本来就该增长（那是自变量），但骨架仍然必须固定。"""
    prompts = minimal_prompts(mode)
    assert len({tuple(extract_skeleton(p)) for p in prompts}) == 1


def test_skeleton_does_not_depend_on_task_length() -> None:
    """本轮任务从空到很长，只该改内容，不该改骨架。"""
    prefix = bundle()
    cfg = cfg_for("fact_chain")
    short = build_prompt(
        prefix=prefix, state=state(0), cfg=cfg, round_index=0, stage_index=1
    )
    long = build_prompt(
        prefix=prefix,
        state=state(0),
        cfg=cfg,
        round_index=0,
        stage_index=1,
        extra_instruction="做一件很长很长的额外任务。" * 40,
    )
    assert extract_skeleton(short) == extract_skeleton(long)


def test_skeleton_changes_only_with_config_not_with_round() -> None:
    """检索开关是配置项：它改变骨架，但只在同一配置内保持一致。"""
    without = minimal_prompts("off", stateless=True)
    retrieval_cfg = cfg_for("off", stateless=True)
    retrieval_cfg.work_model.retrieval = True
    prefix = bundle()
    with_retrieval = [
        build_prompt(
            prefix=prefix,
            state=state(i),
            cfg=retrieval_cfg,
            round_index=i,
            stage_index=min(i + 1, 8),
            workspace_manifest="- drawvid/core.py (10B)",
            retrieval=True,
        )
        for i in range(3)
    ]
    a = tuple(extract_skeleton(without[0]))
    b = tuple(extract_skeleton(with_retrieval[0]))
    assert a != b, "检索段应该体现在骨架上"
    assert len({tuple(extract_skeleton(p)) for p in with_retrieval}) == 1


# --------------------------------------------------------------------------- #
# 段落总是存在
# --------------------------------------------------------------------------- #


def test_every_section_always_appears_even_when_empty() -> None:
    """空段落用显式占位，而不是把段落删掉 —— 删掉就等于骨架变了。"""
    prefix = bundle()
    cfg = cfg_for("fact_chain")
    prompt = build_prompt(
        prefix=prefix,
        state=CompressedState(),
        cfg=cfg,
        round_index=0,
        stage_index=1,
    )
    skeleton = extract_skeleton(prompt)
    assert any("循环状态 C" in line for line in skeleton)
    assert any("本轮任务" in line for line in skeleton)
    assert any("本轮输出格式" in line for line in skeleton)
    assert EMPTY_TASK_PLACEHOLDER in prompt, "空任务必须给占位而不是省略段落"


def test_stateless_mode_keeps_a_memory_section() -> None:
    """F/G 组没有工作记忆，但**段落仍在**（写明本段恒为空），骨架才固定。"""
    prefix = bundle()
    cfg = cfg_for("off", stateless=True)
    prompt = build_prompt(
        prefix=prefix, state=state(2), cfg=cfg, round_index=2, stage_index=3
    )
    assert any("循环状态 C" in line for line in extract_skeleton(prompt))
    assert "本段恒为空" in prompt
    assert "【事实链】" not in prompt  # 但内容确实没有


def test_p_section_is_present_but_content_withheld_when_not_frozen() -> None:
    """P 不冻结时：段落标题保留（骨架稳定），内容换成固定占位。"""
    prefix = bundle()
    cfg = cfg_for("off", prefix_frozen=False)
    prompt = build_prompt(
        prefix=prefix,
        state=CompressedState(),
        cfg=cfg,
        round_index=3,  # 不是第 1 轮
        stage_index=4,
    )
    assert any("项目冻结前缀 P" in line for line in extract_skeleton(prompt))
    assert "滑出上下文窗口" in prompt
    # P 的正文确实没有进来
    assert "唯一事实来源：时间轴" not in prompt


# --------------------------------------------------------------------------- #
# 骨架与内容正交
# --------------------------------------------------------------------------- #


def test_changing_c_content_does_not_change_skeleton() -> None:
    prefix = bundle()
    cfg = cfg_for("fact_chain")
    a = build_prompt(prefix=prefix, state=CompressedState(), cfg=cfg, round_index=1, stage_index=2)
    b = build_prompt(prefix=prefix, state=state(3), cfg=cfg, round_index=1, stage_index=2)
    assert extract_skeleton(a) == extract_skeleton(b)
    assert a != b  # 但内容必须不同，否则 C 没起作用


def test_skeleton_env_is_exactly_the_expected_shape() -> None:
    """把骨架钉成字面量：以后谁不小心加了一段，这条会立刻红。"""
    prompt = minimal_prompts("fact_chain")[0]
    assert extract_skeleton(prompt) == [
        "# ===== 1. 项目冻结前缀 P（只读，不得修改） =====",
        "# ===== 2. 循环状态 C（工作记忆，唯一可迭代部分） =====",
        "# ===== 3. 本轮阶段指令 =====",
        "# ===== 4. 本轮任务 =====",
        "# ===== 5. 本轮输出格式（机器解析，必须严格遵守） =====",
    ]
