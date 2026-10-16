"""OpenAI 兼容入口的回归测试（`/v1/models`、`/v1/chat/completions`）。

这一层最容易被误当成"加个路由"——真正的难点全在**协议无状态 vs pciso 有状态**那条接缝上，
所以这里测的是它，不是某个字段：

  - 会话身份三层（显式头 / `user` 字段 / 首条 user 消息指纹）各自真的能分开会话；
  - **客户端发来的历史不回流**（这是"用事实链替换全量历史"落到协议上的样子）；
  - 错误体是 OpenAI 形状；
  - `stream: true` 吐的是合法 SSE 分块。

离线跑：配置里 `work_model.endpoint: fake` —— 这个文件里的每一轮都不该花钱。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import pciso.api as api

ROOT = Path(__file__).resolve().parents[1]
CFG = {
    "session": {
        "project": "one_sentence_edit",
        "arm": "chat",
        "prefix_path": "prefixes/one_sentence_edit",
        "fake_oracle": "one_sentence_edit",
        "max_rounds": 5,
        # 这是对话档的关键开关：允许"这一轮只回话、不写文件"（见 render.CHAT_DIRECTIVE_BODY）
        "chat_driven": True,
    },
    "compression": {"mode": "fact_chain", "model": "heuristic"},
    "work_model": {"endpoint": "fake"},
    # 脚本 oracle 的产物是本平台自带的示例项目，不可能过快照里的 pytest 验收；
    # 这里要测的是协议接缝，所以把最慢的那一类检查关掉（不是绕过，是换对照面）。
    "validator": {"tests": "off"},
}


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(api, "DEFAULT_STATE_DIR", str(tmp_path / "state"))
    cfg_file = tmp_path / "openai_test.yaml"
    cfg_file.write_text(yaml.safe_dump(CFG, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PCISO_OPENAI_CONFIG", str(cfg_file))
    # 这个文件测的是"服务端跑一整轮"那条路；代理模式（现在的默认）另有 test_proxy_api.py
    monkeypatch.setenv("PCISO_OPENAI_MODE", "round")
    api._SESSIONS.clear()
    return TestClient(api.app)


def _post(client: TestClient, messages: list[dict], **kw):
    return client.post("/v1/chat/completions", json={"model": "pciso", "messages": messages, **kw})


def _sid(resp) -> str:
    sid = resp.headers.get("x-pciso-session")
    assert sid, "回传头里必须带 X-PCISO-Session —— 客户端靠它把会话钉死"
    return sid


# --------------------------------------------------------------------------- #
# 模型表 + 前缀选择
# --------------------------------------------------------------------------- #


def test_models_endpoint_lists_pciso_and_prefixes(client: TestClient) -> None:
    data = client.get("/v1/models").json()
    ids = [m["id"] for m in data["data"]]
    assert "pciso" in ids
    assert "pciso@general" in ids, "默认前缀（通用助手）必须能被点名"
    assert any(i.startswith("pciso@") for i in ids)


def test_model_name_selects_the_prefix(client: TestClient) -> None:
    resp = _post(client, [{"role": "user", "content": "只回一句话：你是谁"}], model="pciso@one_sentence_edit")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-pciso-prefix"] == "one_sentence_edit"
    assert resp.json()["model"] == "pciso@one_sentence_edit", "客户端给的模型名要原样回"


# --------------------------------------------------------------------------- #
# 会话身份：三层
# --------------------------------------------------------------------------- #


def test_first_user_message_identifies_the_session(client: TestClient) -> None:
    first = _post(client, [{"role": "user", "content": "第一句话：你好"}])
    again = _post(client, [{"role": "user", "content": "第一句话：你好"}])
    assert first.status_code == again.status_code == 200
    assert _sid(first) == _sid(again), "同一个对话（首条 user 消息不变）必须落到同一个会话"
    assert first.headers["x-pciso-session-created"] == "1"
    assert again.headers["x-pciso-session-created"] == "0"


def test_a_different_first_message_is_a_different_session(client: TestClient) -> None:
    a = _post(client, [{"role": "user", "content": "第一句话：甲"}])
    b = _post(client, [{"role": "user", "content": "第一句话：乙"}])
    assert _sid(a) != _sid(b), "不同对话必须分开 —— 这是用户点名要的那条"


def test_explicit_session_header_pins_it(client: TestClient) -> None:
    """显式头最可靠：两个**第一句话完全不同**的对话也钉在同一个会话上。"""
    hdr = {"X-PCISO-Session": "chat-42"}
    first = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "开头甲"}]},
        headers=hdr,
    )
    second = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "后来乙"}]},
        headers=hdr,
    )
    assert _sid(first) == _sid(second)
    assert second.json()["x_pciso"]["round_index"] == 1, "第二轮必须接在第一轮后面（C 前进）"


def test_echoing_the_returned_session_id_continues_it(client: TestClient) -> None:
    """客户端把回传头原样发回来 = 接着同一个会话。

    这条必须成立，否则"照抄回传头"会被当成新 key，每轮悄悄开一个新会话 ——
    而客户端完全看不出来（它以为自己一直在同一个会话里）。
    """
    first = _post(client, [{"role": "user", "content": "开头丙"}])
    sid = _sid(first)
    again = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "接着丙说"}]},
        headers={"X-PCISO-Session": sid},
    )
    assert again.status_code == 200, again.text
    assert _sid(again) == sid
    assert again.json()["x_pciso"]["round_index"] == 1


def test_experiment_sessions_are_not_appendable(client: TestClient, tmp_path) -> None:
    """实验臂的会话**不能**从 OpenAI 入口追加轮次。

    arm_a..g 是对照数据。让对话轮次混进去，那条轨迹就变成"半对话半实验"，
    而且事后看不出来是哪几轮被污染的。头里点它的 id 也不行。
    """
    assert client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "别碰实验"}]},
        headers={"X-PCISO-Session": "chat-not-an-experiment"},
    ).status_code == 200
    # 手搓一条 arm 不是 chat 的会话，再拿它的 id 当会话头
    exp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "先开一个"}]},
        headers={"X-PCISO-Session": "chat-maybe-experiment"},
    )
    exp_sid = _sid(exp)
    session = api._SESSIONS[exp_sid]
    session.cfg = session.cfg.model_copy(update={"arm": "arm_c_fact_chain"})
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "偷偷插一轮"}]},
        headers={"X-PCISO-Session": exp_sid},
    )
    assert resp.status_code == 200
    assert _sid(resp) != exp_sid, "实验会话必须被拒，另开一个 chat 会话"


def test_user_field_pins_the_session(client: TestClient) -> None:
    a = _post(client, [{"role": "user", "content": "甲"}], user="terminal-7")
    b = _post(client, [{"role": "user", "content": "乙"}], user="terminal-7")
    c = _post(client, [{"role": "user", "content": "甲"}], user="terminal-8")
    assert _sid(a) == _sid(b)
    assert _sid(a) != _sid(c)


def test_new_session_header_forks_the_session(client: TestClient) -> None:
    hdr = {"X-PCISO-Session": "chat-9"}
    first = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "开一个"}]},
        headers=hdr,
    )
    forked = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "另一个"}]},
        headers={**hdr, "X-PCISO-New": "1"},
    )
    assert _sid(forked) != _sid(first)
    assert forked.json()["x_pciso"]["round_index"] == 0, "新会话必须从第 0 轮起"


# --------------------------------------------------------------------------- #
# 核心：历史不回流
# --------------------------------------------------------------------------- #


def test_client_history_is_not_replayed(client: TestClient) -> None:
    """客户端发的整段历史**只取最后一条 user 消息**，之前发生什么由 C 代表。

    用一条显眼的标记句来证明：它必须**不在**这一轮真正发给模型的 prompt 里。
    这是"上下文模式=自动用事实链替换全量历史"唯一可机器核对的形式。
    """
    marker = "IGNORE-ALL-PREVIOUS-MARKER-7f3a"
    resp = _post(
        client,
        [
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": f"{marker} 第一轮"},
            {"role": "assistant", "content": f"{marker} 我上一轮说过的话"},
            {"role": "user", "content": "现在只回一句话"},
        ],
    )
    assert resp.status_code == 200, resp.text
    sid = _sid(resp)
    rounds = (Path(api._state_dir()) / "sessions" / sid / "rounds.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(rounds.splitlines()[-1])
    assert "现在只回一句话" in record["prompt"], "这一轮那句话必须进 prompt"
    assert marker not in record["prompt"], "历史里的旧消息绝不能回流进 prompt"
    assert record["prompt"].count("现在只回一句话") == 1


def test_the_users_own_words_come_back_verbatim(client: TestClient) -> None:
    """本会话里用户自己说过的话，下一轮原样带回（**不压缩用户指令**）。

    跟上面那条不冲突：客户端发来的整段历史仍然只取最后一条；而"上一轮我让你只用标准库"
    这类话必须回流 —— C 只装模型输出的压缩，不装用户指令，不回流就等于凭空消失。
    真机证据（会话 caf989977f5f4269）见 render.USER_WORDS_PREAMBLE。
    """
    hdr = {"X-PCISO-Session": "chat-words"}
    marker = "WORDMARK-1a2b 只用标准库"
    first = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": marker}]},
        headers=hdr,
    )
    assert first.status_code == 200, first.text
    sid = _sid(first)
    second = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "再说一句"}]},
        headers=hdr,
    )
    assert second.status_code == 200, second.text
    assert _sid(second) == sid, "同一个头必须接同一个会话"
    rounds = (Path(api._state_dir()) / "sessions" / sid / "rounds.jsonl").read_text(
        encoding="utf-8"
    )
    lines = rounds.splitlines()
    assert len(lines) == 2, "两轮必须都落盘"
    prompt = json.loads(lines[-1])["prompt"]
    assert "用户原话（逐字，不压缩）" in prompt
    assert prompt.count(marker) == 1
    # 关键：它由「用户原话」段承载，不是被压进了 C
    before_user_words = prompt.split("用户原话（逐字，不压缩）")[0]
    assert marker not in before_user_words, "用户指令不许出现在 C（或 P）那一段里"
    # 模型自己上一轮的输出照旧不回流：C 代表它
    assert "历史轮次输出" not in prompt


def test_round_advances_the_fact_chain(client: TestClient) -> None:
    hdr = {"X-PCISO-Session": "chat-c"}
    first = client.post(
        "/v1/chat/completions",
        json={"model": "pciso", "messages": [{"role": "user", "content": "随便说一句"}]},
        headers=hdr,
    )
    assert first.status_code == 200, first.text
    assert first.json()["choices"][0]["message"]["content"].strip()
    state = client.get(f"/session/{_sid(first)}/state").json()
    assert state["c_current"]["version"] == 1, "一轮跑完 C 必须前进（否则等于没压缩）"


# --------------------------------------------------------------------------- #
# 协议形状
# --------------------------------------------------------------------------- #


def test_error_shape_is_openai(client: TestClient) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "pciso", "messages": []})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["message"] and err["type"] == "invalid_request_error"


def test_streaming_speaks_openai_chunks(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "pciso",
            "messages": [{"role": "user", "content": "流式说一句"}],
            "stream": True,
        },
        headers={"X-PCISO-Session": "chat-s"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    payloads = [json.loads(ln[6:]) for ln in lines if ln[6:].strip() != "[DONE]"]
    assert lines[-1] == "data: [DONE]"
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    assert any(p["choices"][0]["delta"].get("content") for p in payloads), "正文必须真发出去"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
