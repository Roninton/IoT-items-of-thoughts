"""思考块的**边界**：起点/终点按位置取，且绝不按文本判。

用户口径（原话）：「你要找的是思考块结束这个稳定的出发锚点。并且避免误匹配正文的
"thinking" block 样式的输出。」「思考块开始，记一个位置；结束时可以直接获取起止 string 位置」
"""

from __future__ import annotations

import json

from pciso.proxyapi import StreamScan, _strip_events


def _event(delta: dict, *, finish=None) -> bytes:
    payload = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def test_a_block_is_sliced_out_by_its_positions() -> None:
    scan = StreamScan()
    scan.feed(_event({"reasoning_content": "先看仓库"}))
    scan.feed(_event({"reasoning_content": "，再决定改哪"}))
    thought = "先看仓库，再决定改哪"
    assert scan.thinking == thought  # 还开着
    scan.feed(_event({"content": "好"}))
    assert scan.take_closed_block() == thought
    assert scan.spans == [(0, len(thought))]  # [start, end) —— 位置就是 _raw 的下标
    assert scan.take_closed_block() == ""


def test_two_blocks_in_one_response_are_both_taken_in_order() -> None:
    """工具往返之间可以再来一段思考：第二块也必须被编排，不能吞掉。"""
    first, second = "第一段想清楚", "第二段：读完再想"
    scan = StreamScan()
    scan.feed(_event({"reasoning_content": first}))
    scan.feed(_event({"tool_calls": [{"index": 0, "function": {"name": "read", "arguments": "{}"}}]}))
    scan.feed(_event({"reasoning_content": second}))
    scan.feed(_event({"content": "开始写"}))
    assert scan.take_closed_block() == first
    assert scan.take_closed_block() == second
    assert scan.take_closed_block() == ""
    assert scan.spans == [(0, len(first)), (len(first), len(first) + len(second))]
    assert scan.tools == ["read"]


def test_a_mixed_delta_closes_the_block_and_keeps_the_thought_in_it() -> None:
    scan = StreamScan()
    scan.feed(_event({"reasoning_content": "想完了", "content": "答案"}))
    assert scan.take_closed_block() == "想完了"  # 这一段的思考属于被它关掉的那一块
    assert scan.content == "答案"
    assert scan.thinking == ""


def test_a_block_still_open_at_the_end_is_taken() -> None:
    """只有思考、没有正文的那一块（流到 [DONE] 就结束）。"""
    thought = "只想了，没说"
    scan = StreamScan()
    scan.feed(_event({"reasoning_content": thought}))
    assert scan.take_closed_block() == ""
    assert scan.take_open_block() == thought
    assert scan.take_open_block() == ""
    assert scan.spans == [(0, len(thought))]


def test_text_in_the_content_that_looks_like_a_thinking_block_is_not_one() -> None:
    """正文里写"思考块样式"的输出（甚至逐字引用字段名）不能被误判成思考。"""
    lookalike = '上游发的是 {"reasoning_content": "…"} 这样的 delta；<thinking> 标签只是我举的例子'
    scan = StreamScan()
    scan.feed(_event({"content": lookalike}))
    assert scan.content == lookalike
    assert scan.spans == []
    assert scan.take_closed_block() == ""
    assert scan.take_open_block() == ""
    # 转发层同样只按 JSON 字段处理：正文字节一个都不许动
    out = _strip_events([_event({"content": lookalike}).decode().strip()])
    assert len(out) == 1
    forwarded = json.loads(out[0].decode()[6:])
    assert forwarded["choices"][0]["delta"]["content"] == lookalike


def test_a_content_event_that_merely_mentions_the_key_is_forwarded() -> None:
    """事件里出现 `reasoning_content` 这个词，不等于它是思考字段。"""
    text = '文档里写 reasoning_content 和 reasoning_text 两个键名'
    out = _strip_events([f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}'.strip()])
    assert out and text in out[0].decode()
