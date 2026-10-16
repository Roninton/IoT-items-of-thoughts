"""提示词里那个"硬上限"不能写错 —— 两个坑都真踩过（见 render._out_cap 的 docstring）。"""

from __future__ import annotations

from pciso.render import _out_cap, build_compressor_prompt, build_thinking_prompt


def test_the_output_cap_is_the_real_call_limit() -> None:
    # C 的容量 2000、压缩器上限 1200 → 提示词必须写 1200（写 2000 会被硬截断）
    assert _out_cap(2000, 1200) == 1200
    assert _out_cap(2000, 0) == 2000


def test_a_zero_c_token_budget_is_not_a_zero_output_budget() -> None:
    """编排路径用 `c_max_tokens=0` 表示"不裁 C"；它绝不能被当成"输出 0 token"。

    真踩过：提示词里成了"必须控制在 0 tokens 以内"，模型老老实实回了 `{"ops": []}`，
    一整块思考一条节点都没提。
    """
    assert _out_cap(0, 1800) == 1800
    assert _out_cap(0, 0) == 1200


def test_the_thinking_prompt_never_asks_for_zero_tokens() -> None:
    prompt = build_thinking_prompt(
        thinking="嗯……先写实现还是先写测试？先写实现。", round_index=0, c_max_tokens=0, out_max_tokens=1800
    )
    assert "必须控制在 1800 tokens 以内" in prompt
    assert "必须控制在 0 tokens 以内" not in prompt


def test_the_compressor_prompt_never_asks_for_zero_tokens() -> None:
    prompt = build_compressor_prompt(
        o_i="写了 tools/duration.py", round_index=0, c_max_tokens=0, out_max_tokens=1800
    )
    assert "必须控制在 1800 tokens 以内" in prompt
    assert "必须控制在 0 tokens 以内" not in prompt
