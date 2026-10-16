"""`/v1` 入口对消息与用量的处理：包装层要剥掉，用量要是真 token，思考要回传。

三条都是实测踩出来的（DSH 那头的真实对话）：
  · DSH 会把技能目录当 user 消息塞进来（`<system-reminder>` 块）—— 它被当成本轮任务
    发给了工作模型（整轮跑偏），还被用来算会话指纹（一个对话开了两个会话）；
  · `usage` 之前拿 `delta_raw_chars`（字符数）冒充 completion_tokens，界面显示 0 tok；
  · 思考被压缩器吞掉后没有回传，DSH 的「已思考」块是空的。
"""

from __future__ import annotations

import json

from pciso.openaiapi import (
    ChatCompletionRequest,
    _message_of,
    _noop_response,
    _session_key,
    _strip_reminders,
    _usage_of,
)

REMINDER = (
    "<system-reminder>\nA skill is a reusable set of task-specific instructions.\n"
    "</system-reminder>"
)


class _FakeRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


def test_a_system_reminder_is_not_the_users_message() -> None:
    assert _strip_reminders(REMINDER) == ""
    assert _strip_reminders(f"{REMINDER}\n你好") == "你好"
    assert _strip_reminders(f"你好\n{REMINDER}") == "你好"


def test_an_unterminated_reminder_is_dropped() -> None:
    assert _strip_reminders("<system-reminder>\n技能目录……") == ""


def test_the_reminder_does_not_move_the_session_key() -> None:
    with_reminder = ChatCompletionRequest(
        model="pciso",
        messages=[
            {"role": "user", "content": REMINDER},
            {"role": "user", "content": "你好"},
        ],
    )
    without = ChatCompletionRequest(model="pciso", messages=[{"role": "user", "content": "你好"}])
    assert _session_key(with_reminder, _FakeRequest()) == _session_key(without, _FakeRequest())


def test_usage_reports_real_tokens_not_character_counts() -> None:
    out = {
        "usage": {
            "work_prompt_tokens": 1234,
            "work_completion_tokens": 567,
            "work_cached_tokens": 890,
            "compressor_prompt_tokens": 100,
            "compressor_completion_tokens": 40,
        },
        "prompt_tokens": 9,
        "hidden": {"delta_raw_chars": 4000},
        "upstream": {"history_mode": "state"},
    }
    usage = _usage_of(out)
    assert usage["prompt_tokens"] == 1234
    assert usage["completion_tokens"] == 567
    assert usage["total_tokens"] == 1801
    assert usage["prompt_tokens_details"]["cached_tokens"] == 890
    assert usage["x_pciso_compressor_tokens"] == 140


def test_usage_falls_back_to_the_estimate_when_telemetry_is_missing() -> None:
    usage = _usage_of({"prompt_tokens": 42, "hidden": {}, "upstream": {}})
    assert usage["prompt_tokens"] == 42
    assert usage["completion_tokens"] == 0


def test_the_assistant_message_carries_the_thinking() -> None:
    message = _message_of({"reply": "好", "thinking": "先看清单再动手", "verdict": "accept"})
    assert message["content"] == "好"
    assert message["reasoning_content"] == "先看清单再动手"


def test_a_rejected_round_says_so_and_an_empty_thinking_is_omitted() -> None:
    message = _message_of(
        {"reply": "写完了", "thinking": "  ", "verdict": "revert", "verdict_reasons": ["C12"]}
    )
    assert "revert" in message["content"]
    assert "reasoning_content" not in message


def test_a_reminder_only_turn_is_a_noop_and_costs_nothing() -> None:
    """只剩包装层的消息不能回退去跑上一句真话 —— 那会把上一轮的活再干一遍。"""
    req = ChatCompletionRequest(model="pciso", messages=[{"role": "user", "content": REMINDER}])
    response = _noop_response(req, "pciso", "剥完是空的")
    body = json.loads(bytes(response.body))
    assert response.headers["X-PCISO-Skipped"] == "1"
    assert body["choices"][0]["message"]["content"] == ""
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 0
    assert body["x_pciso"]["skipped"] == "剥完是空的"


def test_the_noop_turn_answers_a_streaming_client_with_sse() -> None:
    req = ChatCompletionRequest(
        model="pciso", messages=[{"role": "user", "content": REMINDER}], stream=True
    )
    response = _noop_response(req, "pciso", "剥完是空的")
    assert response.media_type == "text/event-stream"
    assert response.headers["X-PCISO-Skipped"] == "1"
