"""`/v1` IoT 代理：原样转发 + 历史替换 + **块级替换（压缩后返回）**。

全部离线跑：上游被 monkeypatch 掉，编排出 `flush_thinking` 直接被替换成假结果
（真编排的流水线另有 `tests/test_iot_orchestration.py`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

import pciso.api as api
import pciso.proxyapi as proxyapi
from pciso.models import CompressedState

ROOT = Path(__file__).resolve().parents[1]
CFG = {
    "session": {
        "project": "general",
        "arm": "chat",
        "prefix_path": "prefixes/general",
        "max_rounds": 100000,
        "c_max_tokens": 0,
        "chat_driven": True,
    },
    "compression": {"mode": "fact_chain", "model": "heuristic", "scope": "thinking"},
    "work_model": {"endpoint": "openai", "model": "deepseek-flash", "thinking": True},
    "validator": {"tests": "off", "docs": "off", "constraints": "off"},
}

SYSTEM = {"role": "system", "content": "你在 DSH 里当助手"}
REMINDER = {"role": "user", "content": "<system-reminder>\n技能目录……\n</system-reminder>"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "读一个文件",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]
RAW_THINKING = "我先看看仓库里有什么，嗯……要不要先读文件？还是先建骨架？总之先读吧。"
ITEMS = "- [THINK +] 先读现有文件再决定骨架（依据：仓库状态未知）"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("PCISO_PROXY_CONTEXT", "iot")
    monkeypatch.setattr(api, "DEFAULT_STATE_DIR", str(tmp_path / "state"))
    cfg_file = tmp_path / "proxy.yaml"
    cfg_file.write_text(yaml.safe_dump(CFG, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PCISO_OPENAI_PROXY_CONFIG", str(cfg_file))
    api._SESSIONS.clear()
    return TestClient(api.app)


def _fake_session(*, words=(), state=None, items=None):
    """`forward_payload` 只用到 prefix.render / state / user_words / store.read_meta / cfg。"""
    prefix = SimpleNamespace(render=lambda: "【需求前缀】只做被要求的事")
    work_model = SimpleNamespace(model="deepseek-flash", thinking=True)
    meta = {"proxy_items": items} if items else {}
    return SimpleNamespace(
        prefix=prefix,
        state=lambda: state or CompressedState(version=0),
        user_words=lambda: list(words),
        store=SimpleNamespace(read_meta=lambda: dict(meta)),
        cfg=SimpleNamespace(work_model=work_model),
    )


def _ok(content: str = "好", reasoning: str = "") -> tuple[int, dict]:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return (
        200,
        {
            "id": "chatcmpl-upstream",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        },
    )


def _chunk(delta: dict, *, finish=None, extra: dict | None = None) -> bytes:
    payload: dict = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if extra:
        payload.update(extra)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _fake_flush(items_text: str = ITEMS, **extra):
    calls: list[str] = []

    def fake(session, block, *, turn_paths=(), root=None):
        calls.append(block)
        return {"compressed": True, "ops": 1, "items_text": items_text, **extra}

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


# --------------------------------------------------------------------------- #
# 纯函数：剥离思考字段、块级替换
# --------------------------------------------------------------------------- #


def test_a_reasoning_only_event_is_dropped() -> None:
    event = _chunk({"reasoning_content": "嗯……"}).decode().strip()
    assert proxyapi._strip_events([event]) == []


def test_a_mixed_event_keeps_only_the_content() -> None:
    event = _chunk({"reasoning_content": "嗯……", "content": "正文"}).decode().strip()
    out = proxyapi._strip_events([event])
    assert len(out) == 1
    assert b"reasoning" not in out[0]
    assert json.loads(out[0].decode()[6:])["choices"][0]["delta"] == {"content": "正文"}


def test_a_tool_call_event_is_untouched() -> None:
    delta = {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": "{}"}}]}
    event = _chunk(delta).decode().strip()
    assert b"tool_calls" in proxyapi._strip_events([event])[0]


def test_usage_is_kept_and_done_is_left_to_the_wrapper() -> None:
    usage = _chunk({}, extra={"usage": {"prompt_tokens": 7}}).decode().strip()
    assert b"prompt_tokens" in proxyapi._strip_events([usage])[0]
    assert proxyapi._strip_events(["data: [DONE]"]) == []


def test_the_compact_event_looks_like_an_upstream_thinking_delta() -> None:
    event = proxyapi._reasoning_event("条目").decode()
    data = json.loads(event.strip()[6:])
    assert data["choices"][0]["delta"]["reasoning_content"] == "条目"
    assert data["object"] == "chat.completion.chunk"


def test_swap_block_covers_the_four_modes() -> None:
    key = proxyapi.block_key(RAW_THINKING)
    ledger = {key: ITEMS}
    assert proxyapi.swap_block(RAW_THINKING, ledger, "iot") == ITEMS
    assert proxyapi.swap_block(RAW_THINKING, ledger, "raw") == RAW_THINKING
    assert proxyapi.swap_block(RAW_THINKING, ledger, "drop") == ""
    # 拿不到紧凑版：回原文，不静默吞掉一段思考
    assert proxyapi.swap_block(RAW_THINKING, {}, "iot") == RAW_THINKING


# --------------------------------------------------------------------------- #
# 组装：历史替换 + 块级替换
# --------------------------------------------------------------------------- #


def _past_and_now() -> list[dict]:
    return [
        SYSTEM,
        {"role": "user", "content": "第一轮的活"},
        {"role": "assistant", "content": "干完了", "reasoning_content": "上一轮的思考"},
        {"role": "tool", "tool_call_id": "c0", "content": "结果"},
        {"role": "user", "content": "第二轮：接着干"},
    ]


def test_the_whole_past_is_replaced_by_prefix_state_and_words() -> None:
    payload = proxyapi.forward_payload(
        {"model": "pciso", "messages": _past_and_now(), "tools": TOOLS},
        _fake_session(words=["第一轮的活", "第二轮：接着干"]),
        mode="raw",
    )
    sent = payload["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "system"]
    assert "【需求前缀】" in sent[0]["content"]
    assert "事实链" in sent[-1]["content"]  # C 挂在最后一块
    assert "第二轮：接着干" in sent[-1]["content"]
    assert payload["tools"] == TOOLS
    assert payload["model"] == "deepseek-flash"


def test_the_volatile_block_goes_last_so_the_prefix_stays_cached() -> None:
    """会变的那块（C + 用户原话）必须在**请求末尾**：放前面会让它后面整条尾巴全部 miss。

    实测（tmp/_cache_layout_ab.py）：插头部 27.5% / 挂末尾 91.3% / 不带它 98.3%。
    """

    payload = proxyapi.forward_payload(
        {"model": "pciso", "messages": _past_and_now()}, _fake_session(words=["第一轮的活"]), mode="raw"
    )
    sent = payload["messages"]
    assert sent[-1]["role"] == "system"
    assert "【事实链】" in sent[-1]["content"]  # C 挂在最后一块
    # 固定块（说明 + P）还是留在头部，且里面不许出现会变的内容
    assert "【需求前缀】" in sent[0]["content"]
    assert "【事实链】" not in sent[0]["content"]
    assert "第一轮的活" not in sent[0]["content"]


# DSH 的真实形状（实测 request dump）：人的提问在前，运行时上下文/技能目录当 **user 消息追加在后面**。
DSH_SHAPE = [
    {"role": "system", "content": "你是 agent"},
    {"role": "user", "content": "读 PROBE.md 并照做"},
    {"role": "user", "content": "<system-reminder>\nworkspace 指令……\n</system-reminder>"},
    {"role": "user", "content": "Current runtime context. This snapshot supersedes earlier ones.\n策略：danger-full-access"},
    {"role": "user", "content": "<system-reminder>\n技能目录……\n</system-reminder>"},
]


def test_dsh_injected_context_does_not_hide_the_real_prompt() -> None:
    """注入的上下文不能当本轮起点 —— 否则真提问被当成"历史"丢掉（模型会说"没看到任务"）。"""
    assert proxyapi.last_user_index(DSH_SHAPE) == 1


def test_the_injected_context_is_still_forwarded_with_the_turn() -> None:
    payload = proxyapi.forward_payload(
        {"model": "pciso", "messages": DSH_SHAPE, "tools": TOOLS}, _fake_session(), mode="raw"
    )
    sent = payload["messages"]
    assert "读 PROBE.md 并照做" in sent[1]["content"]
    assert len(sent) == len(DSH_SHAPE) + 1  # 尾部原样转发，注入的那几条一起带出去，末尾再加会变的那块


def test_injected_context_is_not_counted_as_the_users_own_words() -> None:
    assert proxyapi._user_words_in(DSH_SHAPE) == ["读 PROBE.md 并照做"]


def test_full_mode_is_the_standard_agent_baseline() -> None:
    body = {"model": "pciso", "messages": _past_and_now(), "tools": TOOLS}
    payload = proxyapi.forward_payload(body, _fake_session(), mode="full")
    assert payload["messages"] == body["messages"]  # 一条都不动，只有 model 名换掉


def test_drop_mode_takes_the_block_out_of_the_tail() -> None:
    messages = [SYSTEM, {"role": "user", "content": "干活"}, {"role": "assistant", "content": "好", "reasoning_content": RAW_THINKING}]
    payload = proxyapi.forward_payload({"model": "pciso", "messages": messages}, _fake_session(), mode="drop")
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert "reasoning_content" not in assistant
    assert assistant["content"] == "好"


def test_iot_mode_swaps_the_block_for_its_items() -> None:
    messages = [SYSTEM, {"role": "user", "content": "干活"}, {"role": "assistant", "content": "好", "reasoning_content": RAW_THINKING}]
    session = _fake_session(items={proxyapi.block_key(RAW_THINKING): ITEMS})
    payload = proxyapi.forward_payload({"model": "pciso", "messages": messages}, session, mode="iot")
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == ITEMS
    assert RAW_THINKING not in json.dumps(payload, ensure_ascii=False)


def test_no_real_user_message_is_passed_through() -> None:
    body = {"model": "pciso", "messages": [SYSTEM, REMINDER]}
    payload = proxyapi.forward_payload(body, _fake_session(), mode="iot")
    assert payload["messages"] == body["messages"]


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #


def test_non_stream_returns_the_compact_block(client, monkeypatch) -> None:
    monkeypatch.setattr(proxyapi, "flush_thinking", _fake_flush())
    monkeypatch.setattr(proxyapi, "upstream_json", lambda p, s: _ok("正文", RAW_THINKING))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["choices"][0]["message"]["reasoning_content"] == ITEMS
    assert out["choices"][0]["message"]["content"] == "正文"
    assert out["x_pciso"]["think_ops"] == 1
    assert out["model"] == "pciso"
    assert resp.headers["X-PCISO-Mode"] == "proxy"


def test_the_stream_returns_the_compact_block_not_the_raw_one(client, monkeypatch) -> None:
    chunks = [
        _chunk({"reasoning_content": RAW_THINKING[:12]}),
        _chunk({"reasoning_content": RAW_THINKING[12:]}),
        _chunk({"content": "正文"}),
        _chunk({}, finish="stop", extra={"usage": {"prompt_tokens": 9}}),
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(proxyapi, "upstream_sse", lambda p, s: iter(chunks))
    monkeypatch.setattr(proxyapi, "flush_thinking", _fake_flush())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    text = resp.text
    assert RAW_THINKING[:12] not in text  # 原文一个字节都没回
    assert ITEMS in text  # 回的是紧凑版
    assert "正文" in text
    assert "prompt_tokens" in text  # 其他块一个字段都不动
    assert text.rstrip().endswith("data: [DONE]")  # [DONE] 由收口统一发


def test_the_compact_block_comes_before_the_content(client, monkeypatch) -> None:
    chunks = [
        _chunk({"reasoning_content": RAW_THINKING}),
        _chunk({"content": "正文"}),
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(proxyapi, "upstream_sse", lambda p, s: iter(chunks))
    monkeypatch.setattr(proxyapi, "flush_thinking", _fake_flush())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert resp.text.index(ITEMS) < resp.text.index("正文")


def test_the_block_is_orchestrated_once_per_block(client, monkeypatch) -> None:
    chunks = [
        _chunk({"reasoning_content": RAW_THINKING}),
        _chunk({"content": "正文"}),
        b"data: [DONE]\n\n",
    ]
    flush = _fake_flush()
    monkeypatch.setattr(proxyapi, "upstream_sse", lambda p, s: iter(chunks))
    monkeypatch.setattr(proxyapi, "flush_thinking", flush)
    client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert flush.calls == [RAW_THINKING]


def test_a_failed_orchestration_falls_back_to_the_raw_block(client, monkeypatch) -> None:
    def boom(session, block, *, turn_paths=(), root=None):
        raise RuntimeError("编排器炸了")

    chunks = [_chunk({"reasoning_content": RAW_THINKING}), _chunk({"content": "正文"}), b"data: [DONE]\n\n"]
    monkeypatch.setattr(proxyapi, "upstream_sse", lambda p, s: iter(chunks))
    monkeypatch.setattr(proxyapi, "flush_thinking", boom)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert RAW_THINKING in resp.text  # 宁可多花 token，也不吞掉思考


def test_an_upstream_error_keeps_its_status_and_body(client, monkeypatch) -> None:
    body = {"error": {"message": "Thinking mode does not support this tool_choice"}}

    def fake(payload, session):
        return 400, body

    monkeypatch.setattr(proxyapi, "upstream_json", fake)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert resp.status_code == 400
    assert resp.json() == body


def test_a_failing_upstream_stream_reports_the_reason(client, monkeypatch) -> None:
    def boom(payload, session):
        raise RuntimeError("连接断了")
        yield b""  # pragma: no cover

    monkeypatch.setattr(proxyapi, "upstream_sse", boom)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert "连接断了" in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")


def test_a_block_with_nothing_to_keep_is_not_re_sent_as_raw(client, monkeypatch) -> None:
    """编排成功但一条都没留 → 这一块不发 thinking（原文只在"编排失败"时才回退）。"""

    def empty(session, block, *, turn_paths=(), root=None):
        return {"ops": 0, "items_text": "", "error": None}

    chunks = [
        _chunk({"reasoning_content": RAW_THINKING}),
        _chunk({"content": "正文"}),
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(proxyapi, "upstream_sse", lambda p, s: iter(chunks))
    monkeypatch.setattr(proxyapi, "flush_thinking", empty)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "stream": True, "messages": [SYSTEM, {"role": "user", "content": "干活"}]},
    )
    assert "reasoning_content" not in resp.text
    assert "正文" in resp.text


def test_the_upstream_stream_is_read_decoded_not_raw(monkeypatch) -> None:
    """上游要是压了，`iter_raw()` 拿到的是 gzip —— 字面转发就是把 gzip 当 SSE 发（真踩过）。"""

    class FakeStream:
        status_code = 200

        def __init__(self) -> None:
            self.enter = False

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def iter_bytes(self):
            yield b"data: ok\n\n"

        def iter_raw(self):  # pragma: no cover
            raise AssertionError("不能用 iter_raw：那会把压缩字节当 SSE 转发")

    class FakeClient:
        def __init__(self, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def stream(self, *a, **kw) -> FakeStream:
            return FakeStream()

    monkeypatch.setattr(proxyapi.httpx, "Client", FakeClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    session = _fake_session()
    out = b"".join(proxyapi.upstream_sse({"model": "x"}, session))
    assert out == b"data: ok\n\n"


# --------------------------------------------------------------------------- #
# 路径归一化：工具的绝对路径 vs 模型写的相对 anchor
# --------------------------------------------------------------------------- #


def test_the_same_file_is_recognised_no_matter_which_side_wrote_it(tmp_path) -> None:
    """工具给绝对路径、模型写相对路径 —— 不归一化就永远对不上，ACT 一条都落不下来。"""
    root = tmp_path / "ws"
    (root / "oneline" / "out").mkdir(parents=True)
    target = root / "oneline" / "out" / "x.png"
    target.write_bytes(b"png")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps({"file_path": str(target)}, ensure_ascii=False),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "created"},
    ]
    assert proxyapi.write_calls(messages, root) == [("c1", "oneline/out/x.png")]
    triples = proxyapi._written_triples(root, [str(target)])
    assert triples[0][0] == "oneline/out/x.png"
    assert len(triples[0][1]) == 64
    # 根没配的时候，绝对路径照样能核对：别因为少一个环境变量就把 ACT 通道静默关掉
    assert proxyapi._written_triples(None, [str(target)])[0][0] == str(target).replace("\\", "/")


# --------------------------------------------------------------------------- #
# 循环检测（零模型调用）
# --------------------------------------------------------------------------- #


def test_result_is_error_only_looks_at_exit_codes_and_known_strings() -> None:
    assert proxyapi.result_is_error("[exit code: 1]\nFAILED tests/test_x.py") is True
    assert proxyapi.result_is_error("Error: boom") is True
    assert proxyapi.result_is_error('permission denied: cannot open file') is True
    assert proxyapi.result_is_error("2\nEXITCODE=0") is False
    assert proxyapi.result_is_error("") is False
    assert proxyapi.result_is_error("我这就去读图") is False


def _failing_image_call(index: int) -> list[dict]:
    args = json.dumps({"path": "x.png"})
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"c{index}", "type": "function", "function": {"name": "read_image", "arguments": args}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"c{index}",
            "content": 'Error: cannot read "x.png" as an image: model "pciso" does not declare image input',
        },
    ]


def test_a_tool_that_keeps_failing_is_reported_as_a_loop() -> None:
    messages: list[dict] = [SYSTEM, {"role": "user", "content": "干活"}]
    for i in range(3):
        messages.extend(_failing_image_call(i))
    report = proxyapi.loop_report(messages)
    assert report["failing"] == [
        ("read_image", 3, 'Error: cannot read "x.png" as an image: model "pciso" does not declare image input')
    ]
    assert report["repeated"], "同一组调用连发三次也算循环"
    assert report["ok"] == 0
    text = proxyapi.loop_warning(report)
    assert "【循环告警】" in text
    assert "read_image" in text
    assert "不要再调用它" in text


def test_planning_without_acting_raises_the_stall_warning() -> None:
    messages: list[dict] = [SYSTEM, {"role": "user", "content": "干活"}]
    for i in range(4):
        messages.append({"role": "assistant", "content": f"第 {i} 步我这就去读文件"})
    report = proxyapi.loop_report(messages)
    assert report["plan_only_steps"] == 4
    text = proxyapi.loop_warning(report)
    assert "【停滞告警】" in text
    assert "不要写计划" in text


def test_a_working_agent_gets_no_warning() -> None:
    messages: list[dict] = [SYSTEM, {"role": "user", "content": "干活"}]
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c9", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c9", "content": "内容"},
        ]
    )
    assert proxyapi.loop_warning(proxyapi.loop_report(messages)) == ""


def test_the_loop_warning_rides_in_the_volatile_block() -> None:
    messages: list[dict] = [SYSTEM, {"role": "user", "content": "干活"}]
    for i in range(3):
        messages.extend(_failing_image_call(i))
    block = proxyapi.volatile_block(_fake_session(), messages)
    assert "【循环告警】" in block
    assert "【事实链】" in block, "C 还在，告警只是加在最前面"
    assert block.index("【循环告警】") < block.index("【事实链】")
