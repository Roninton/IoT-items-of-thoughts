"""压缩器被 max_tokens 硬截断时的行为：必须重试一次，且**绝不安静地丢一整轮**。

真机证据（会话 03151aaae4c84363，笔刷载体 6 轮）：其中 2 轮的压缩器 completion_tokens
正好顶到 1200 上限，输出 JSON 断在字符串中间；解析失败却返回 `ops=[]` / `error=None` /
`truncated=False` —— 于是那两轮的成果永远没进 C，下一轮只能把同样的文件重写一遍。
根因之一是 prompt 里报的预算（c_max_tokens=2000）**大于**这次调用的真实上限（1200）。
"""

from __future__ import annotations

import json

from pciso.compressors import ModelFactChainCompressor
from pciso.models import Anchor, CompressedState, FactNode
from pciso.render import CHAT_DIRECTIVE_BODY, build_compressor_prompt, render_state_block
from pciso.workmodels import ChatResult

GOOD_OP = {
    "op": "ADD_FACT",
    "payload": {
        "id": "N1",
        "statement": "brushkit.canvas.Canvas 已定义",
        "deps": [],
        "anchor": {"path": "brushkit/canvas.py", "symbol": "Canvas"},
        "salience": 80,
    },
}


class CutClient:
    """第一次（recover=False 时每次）都吐"被截断"的输出：用量顶到上限、JSON 断在半句上。"""

    def __init__(self, *, recover: bool) -> None:
        self.recover = recover
        self.prompts: list[str] = []
        self.budgets: list[int] = []
        self.calls = 0

    def chat(self, prompt: str, **kwargs: object) -> ChatResult:
        self.prompts.append(prompt)
        budget = int(kwargs.get("max_tokens") or 0)
        self.budgets.append(budget)
        self.calls += 1
        if self.calls == 1 or not self.recover:
            return ChatResult(
                text='{"ops":[{"op":"ADD_FACT","payload":{"id":"N1","statement":"半句',
                prompt_tokens=11,
                completion_tokens=budget,
            )
        return ChatResult(
            text=json.dumps({"ops": [GOOD_OP]}, ensure_ascii=False),
            prompt_tokens=11,
            completion_tokens=64,
        )


def _compress(client: CutClient):
    comp = ModelFactChainCompressor(client=client)  # type: ignore[arg-type]
    return comp.compress(
        "O_i 正文", prior_state=CompressedState(), round_index=0, c_max_tokens=2000
    )


def test_truncated_output_is_retried_with_a_bigger_budget() -> None:
    client = CutClient(recover=True)
    delta = _compress(client)
    assert client.budgets == [1000, 2400], "截断后没有把输出预算翻倍重试"
    assert delta.retried is True
    assert delta.error is None
    assert [op.op for op in delta.ops] == ["ADD_FACT"]


def test_retry_prompt_states_the_budget_it_is_actually_allowed() -> None:
    """prompt 里报的预算不许大于这次调用的真实上限 —— 原来就是差在这上面。"""
    client = CutClient(recover=True)
    _compress(client)
    assert "1000 tokens 以内" in client.prompts[0]
    assert "1000 tokens 以内" not in client.prompts[1], "重试时预算没往上抬"
    assert "硬上限" in client.prompts[0]


def test_still_truncated_after_retry_is_loud() -> None:
    client = CutClient(recover=False)
    delta = _compress(client)
    assert delta.ops == []
    assert delta.retried is True
    assert delta.truncated is True
    assert delta.error and "截断" in delta.error
    assert "ΔC 丢失" in delta.error


def test_rendered_state_marks_facts_whose_anchor_is_missing() -> None:
    state = CompressedState(
        fact_chain=[
            FactNode(
                id="N1",
                statement="导出层每次覆盖写同一个 current-view.jpg",
                anchor=Anchor(
                    path="brushkit/export.py",
                    symbol="write_current_view",
                    resolved=False,
                    detail="文件不存在",
                ),
            ),
        ]
    )
    text = render_state_block(state)
    assert "[未落地]" in text
    assert "文件不存在" in text


def test_compressor_prompt_states_the_real_output_cap() -> None:
    prompt = build_compressor_prompt(
        o_i="x", round_index=0, c_max_tokens=2000, out_max_tokens=1200
    )
    assert "1200 tokens 以内" in prompt


def test_chat_directive_says_there_are_no_tools() -> None:
    """聊天档必须明说"你没有工具" —— 真机上出现过整轮写 ```bash ls/find/cat``` 然后白烧。"""
    assert "你没有工具" in CHAT_DIRECTIVE_BODY
    assert "bash" in CHAT_DIRECTIVE_BODY
    assert "[未落地]" in CHAT_DIRECTIVE_BODY
