"""把「压缩器是位置名、职责是事实链提取」这条口径钉住。

用户口径：它坐在 O_i 与 C 之间（所以叫 compression 没问题），
但它的**职责是事实链提取**，不是压缩文本。

这条区分不是措辞：衡量标准不同（无据声明率/锚点可解析率/作废正确性 vs 体积比）、
失败模式不同（丢事实是**错**，不是有损）、来源必须可区分（server 可验证 vs 模型声称）。
文档会被后来的人删掉，测试不会 —— 所以口径也要有测试守着：
口径一旦只写在 README 里，下一个人按"压缩"的直觉去优化它，方向就是错的。
"""

from __future__ import annotations

from pathlib import Path

from pciso.render import build_compressor_prompt

ROOT = Path(__file__).resolve().parents[1]


def prompt() -> str:
    return build_compressor_prompt(
        o_i="本轮做了点事。\n```file:pkg/a.py\nx = 1\n```\n",
        round_index=2,
        prior_nodes=["N1 接口 f 已定义"],
        prior_failures=["试 X：因 Y 否决"],
        c_max_tokens=2000,
        units=["pkg/a.py"],
    )


def test_prompt_states_the_responsibility_is_extraction() -> None:
    text = prompt()
    assert "提取事实链" in text
    assert "不是压缩文本" in text


def test_prompt_says_a_wrong_fact_is_worse_than_a_missing_one() -> None:
    """提取器的第一原则：宁可少一条，不可错一条。"""
    assert "一条错的事实比少一条事实更糟" in prompt()


def test_prompt_frames_dropping_as_a_cost_not_a_free_tradeoff() -> None:
    """按"压缩"的直觉，丢内容是无害的取舍；按事实链的口径，丢事实有正确性代价。

    预算耗尽是提取器最可能"随手丢"的时刻，所以这条必须写在 prompt 里。
    """
    text = prompt()
    assert "丢弃是有代价的" in text
    assert "优先丢" in text
    # 丢的优先级必须与"能否从磁盘重建"挂钩，而不是与新旧挂钩
    assert "从代码里还能重新看出来" in text
    # 被迫丢掉重要事实时要显式上报，而不是静默
    assert "SET_UNRESOLVED" in text


def test_prompt_forbids_fabrication_and_requires_anchors() -> None:
    text = prompt()
    assert "必须来自 O_i，不能编造" in text or "不能编造" in text
    assert "锚点" in text


def test_readme_documents_the_ruling_and_its_consequences() -> None:
    """留档检查：口径与三条后果都必须在 README 里。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "位置名" in readme
    assert "职责是**事实链提取**" in readme
    for consequence in ("衡量标准不同", "失败模式不同", "来源必须可区分"):
        assert consequence in readme, consequence
    # 主指标必须是无据声明率，而不是压缩比
    assert "无据声明率" in readme


def test_module_and_config_docstrings_carry_the_ruling() -> None:
    """代码里最容易被按"压缩"直觉改的两个地方也要写清楚。"""
    from pciso import compressors, config

    assert "事实链提取器" in (compressors.__doc__ or "")
    assert "不是" in (compressors.__doc__ or "")
    assert "事实链提取器" in (config.CompressionConfig.__doc__ or "")
