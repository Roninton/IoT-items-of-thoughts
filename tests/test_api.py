"""HTTP 接口：api.py 是交付物的一部分，但它此前**一条测试都没有**。

用 FastAPI 的 TestClient 打真接口。这里覆盖的是"接口层"的行为：
状态码、幂等断言、删除后的 404、日志端点默认不带全文 ——
这些错了，用 API 驱动实验的人会在最难排查的地方卡住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pciso.api as api

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"

INJECTED = """\
本轮注入一个额外模块，不调用任何模型。

```file:drawvid/extra.py
\"\"\"额外模块。\"\"\"


def helper(value: int) -> int:
    \"\"\"返回原值。\"\"\"
    return value
```

```file:tests/test_extra.py
\"\"\"extra 的自测。\"\"\"

from drawvid.extra import helper


def test_helper():
    \"\"\"用例。\"\"\"
    assert helper(3) == 3
```
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api, "DEFAULT_STATE_DIR", str(tmp_path / "state"))
    api._SESSIONS.clear()
    return TestClient(api.app)


def create(client: TestClient, **overrides: object) -> dict:
    payload = {
        "project": "drawing_video_toolchain",
        "prefix_path": str(PREFIX),
        "config": {
            "project": "drawing_video_toolchain",
            "arm": overrides.pop("arm", "API"),
            "prefix_path": str(PREFIX),
            "max_rounds": int(overrides.pop("max_rounds", 3)),
            "fake_oracle": "drawing_video",
            "compression": {"mode": "fact_chain", "model": "heuristic"},
            "work_model": {"endpoint": "fake"},
        },
        "arm": "API",
    }
    res = client.post("/session/create", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["state_dir"]


def test_arms_lists_the_seven_groups(client: TestClient) -> None:
    arms = {row["arm"] for row in client.get("/arms").json()["arms"]}
    assert {"A", "B", "C", "D", "E", "F", "G"} <= arms


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def test_create_returns_identity_and_stages(client: TestClient) -> None:
    body = create(client)
    assert body["session_id"]
    assert body["prefix_hash"]
    assert len(body["stages"]) == 8
    assert body["history_mode"] == "state"
    assert body["max_rounds"] == 3


def test_create_with_bad_prefix_is_400(client: TestClient) -> None:
    res = client.post(
        "/session/create",
        json={"project": "x", "prefix_path": str(PREFIX / "nope")},
    )
    assert res.status_code == 400


def test_injected_round_commits_and_validates(client: TestClient) -> None:
    sid = create(client)["session_id"]
    res = client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["committed"] is True
    assert body["telemetry"]["o_i_source"] == "injected"
    assert "drawvid/extra.py" in body["telemetry"]["files_written"]
    assert body["validation"]["checks"], "验证器必须真的跑过"
    assert body["next_prompt"], "必须给出下一轮的 prompt"
    assert body["round_index"] == 0


def test_round_index_assertion_conflicts_with_409(client: TestClient) -> None:
    sid = create(client)["session_id"]
    res = client.post(f"/session/{sid}/round", json={"o_i": INJECTED, "round_index": 5})
    assert res.status_code == 409
    assert "轮次错位" in res.json()["detail"]


def test_dry_run_does_not_commit(client: TestClient) -> None:
    sid = create(client)["session_id"]
    body = client.post(f"/session/{sid}/round", json={"dry_run": True}).json()
    assert body["committed"] is False
    assert body["prompt"]
    assert client.get(f"/session/{sid}/state").json()["round_index"] == 0


def test_max_rounds_is_enforced_with_409(client: TestClient) -> None:
    sid = create(client, max_rounds=1)["session_id"]
    assert client.post(f"/session/{sid}/round", json={"o_i": INJECTED}).status_code == 200
    res = client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    assert res.status_code == 409
    assert "max_rounds" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def test_state_exposes_c_and_metrics(client: TestClient) -> None:
    sid = create(client)["session_id"]
    client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    body = client.get(f"/session/{sid}/state").json()
    assert body["round_index"] == 1
    assert body["prefix_hash"]
    assert body["c_current"]["constraints"], "约束状态必须随 C 一起暴露"
    assert len(body["constraint_status"]) == 15
    assert body["metrics"]["rounds"] == 1
    # 三个场景的原始量都要在
    assert "per_round" in body["metrics"]
    assert "tokens" in body["metrics"]
    assert "compression_quality" in body["metrics"]


def test_artifacts_carry_hashes(client: TestClient) -> None:
    sid = create(client)["session_id"]
    client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    body = client.get(f"/session/{sid}/artifacts").json()
    assert "drawvid/extra.py" in body["files"]
    assert body["hashes"]["drawvid/extra.py"]
    assert "tests/test_extra.py" in body["tests"]


def test_metrics_endpoint_matches_state(client: TestClient) -> None:
    sid = create(client)["session_id"]
    client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    metrics = client.get(f"/session/{sid}/metrics").json()
    assert metrics["rounds"] == 1
    assert len(metrics["per_round"]) == 1
    assert metrics["per_round"][0]["judgement"] in {"accept", "revert"}


def test_log_hides_full_text_by_default(client: TestClient) -> None:
    """日志端点默认不带 prompt / o_i 全文 —— 否则一次请求能拖回几百 KB。"""
    sid = create(client)["session_id"]
    client.post(f"/session/{sid}/round", json={"o_i": INJECTED})

    slim = client.get(f"/session/{sid}/log").json()
    assert slim["rounds"]
    assert "o_i" not in slim["rounds"][0]
    assert "prompt" not in slim["rounds"][0]
    assert slim["events"]

    full = client.get(f"/session/{sid}/log?include_prompts=true").json()
    assert full["rounds"][0]["o_i"] == INJECTED
    assert "本轮输出格式" in full["rounds"][0]["prompt"]


def test_revert_restores_workspace(client: TestClient) -> None:
    sid = create(client)["session_id"]
    client.post(f"/session/{sid}/round", json={"o_i": INJECTED})
    assert "drawvid/extra.py" in client.get(f"/session/{sid}/artifacts").json()["files"]

    body = client.post(f"/session/{sid}/revert", json={"to_index": 0}).json()
    assert body["to_index"] == 0
    assert "drawvid/extra.py" not in client.get(f"/session/{sid}/artifacts").json()["files"]


def test_delete_then_state_is_404(client: TestClient) -> None:
    sid = create(client)["session_id"]
    assert client.delete(f"/session/{sid}").json()["deleted"] == sid
    assert client.get(f"/session/{sid}/state").status_code == 404


def test_unknown_session_is_404(client: TestClient) -> None:
    assert client.get("/session/deadbeef/state").status_code == 404
    assert client.get("/session/deadbeef/metrics").status_code == 404
    assert client.get("/session/deadbeef/artifacts").status_code == 404
    assert client.delete("/session/deadbeef").status_code == 404


def test_injected_marker_is_visible_in_state(client: TestClient) -> None:
    """G 组的 @@READ 由 server 执行，接口层不该把它当成非法输入。"""
    sid = create(client)["session_id"]
    body = client.post(
        f"/session/{sid}/round",
        json={"o_i": "@@READ drawvid/core.py\n\n" + INJECTED},
    ).json()
    assert body["committed"] is True
    assert body["telemetry"]["o_i_source"] == "injected"


# --------------------------------------------------------------------------- #
# 聊天视图（前端）
# --------------------------------------------------------------------------- #


def test_chat_page_is_served(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    page = res.text
    # 页面是**纯净的对话窗口**：一条对话流 + 一个输入框。
    assert "composer" in page and "<textarea" in page
    # 实验台的东西一个都不许混进来（模式开关、arm 下拉、离线/交付勾选、
    # 指标行、约束 chip、审计面板、试跑/看成片按钮）—— 它们在 CLI 与接口里，
    # 混进聊天窗口会让人分不清"在跟模型说话"还是在跑实验。
    for junk in ("审计", "试跑", "看成片", "离线排练", "stat-line", "/arms", "DELIVERABLE"):
        assert junk not in page, f"聊天页里不该出现 {junk}"
    # 说的每一句都走**真的一轮**（/say：落盘、验证、ΔC、判定），不是只读的检视通道
    # （/ask：不产生 ΔC、不落盘）—— 窗口要能派活，而不是只能看一眼当前 C。
    assert "/say" in page and "/ask" not in page
    # 长会话用对话驱动那份配置（轮数不封顶），不是实验用的 arm C（8 轮锁死）。
    assert "configs/chat.yaml" in page


def test_create_resolves_config_path_against_repo_root(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置路径不能跟着 server 的 cwd 走。

    页面发的是 `configs/chat.yaml` 这种仓库相对路径。server 从 `src/` 起时
    （README 里的起法之一）cwd 不是仓库根，那一版会直接 400
    `配置无效: No such file or directory` —— 而界面上只表现为"开新对话失败"，
    看起来像前端坏了。顺带把"对话不该被实验臂那 8 轮锁死"钉住。
    """
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root / "src")
    res = client.post(
        "/session/create",
        json={
            "project": "one_sentence_edit",
            "prefix_path": "prefixes/one_sentence_edit",
            "config_path": "configs/chat.yaml",
            "overrides": {
                "work_model": {"endpoint": "fake"},
                "compression": {"model": "heuristic"},
            },
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["max_rounds"] > 8
    assert body["workspace_is_deliverable"] is False


def say_session(client: TestClient, **overrides: object) -> str:
    """建一个走脚本 oracle 的会话（本机无凭据，前端默认也是这条路径）。"""
    payload = {
        "project": "drawing_video_toolchain",
        "prefix_path": str(PREFIX),
        "config_path": "configs/arm_c_fact_chain.yaml",
        "overrides": {"work_model": {"endpoint": "fake"},
                      "compression": {"model": "heuristic"}},
    }
    payload.update(overrides)
    res = client.post("/session/create", json=payload)
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


def test_say_returns_visible_part_only(client: TestClient) -> None:
    sid = say_session(client)
    body = client.post(f"/session/{sid}/say", json={"message": "把 core 层搭起来"}).json()

    assert body["reply"], "必须给一段可见回复"
    assert body["reply_kind"] in {"narrative", "empty", "thinking_fallback"}
    # 文件内容**不能**出现在可见回复里 —— 那是被压缩掉的东西
    assert "```file:" not in body["reply"]
    assert body["files_changed"], "改了哪些文件要可见"
    assert body["hidden"]["file_content_chars"] > 0, "隐藏面体量要给出"
    assert body["hidden"]["delta_ops"] >= 0
    assert body["hidden"]["prompt_chars"] > 0
    assert body["upstream"]["is_fake"] is True, "必须自报用的是脚本 oracle"
    assert body["hidden_detail"] is None, "默认不给审计全文"


def test_say_include_hidden_gives_audit_text(client: TestClient) -> None:
    sid = say_session(client)
    body = client.post(
        f"/session/{sid}/say", json={"message": "随便做点", "include_hidden": True}
    ).json()
    assert body["hidden_detail"] is not None
    assert "本轮输出格式" in body["hidden_detail"]["prompt"]
    assert "```file:" in body["hidden_detail"]["o_i"]


def test_say_carries_the_task_into_a_fixed_section(client: TestClient) -> None:
    """用户这句话落在固定段落上，而不是被拼到别处。"""
    sid = say_session(client)
    body = client.post(
        f"/session/{sid}/say", json={"message": "只改字幕带高度", "include_hidden": True}
    ).json()
    prompt = body["hidden_detail"]["prompt"]
    assert "# ===== 4. 本轮任务 =====" in prompt
    assert "只改字幕带高度" in prompt


def test_turns_endpoint_restores_the_conversation(client: TestClient) -> None:
    sid = say_session(client)
    client.post(f"/session/{sid}/say", json={"message": "第一件小事"})
    client.post(f"/session/{sid}/say", json={"message": "第二件小事"})

    turns = client.get(f"/session/{sid}/turns").json()["turns"]
    assert len(turns) == 2
    assert [t["round_index"] for t in turns] == [0, 1]
    assert all(t["reply"] for t in turns)
    assert turns[0]["hidden"]["file_content_chars"] > 0


def test_audit_endpoint_lists_failed_checks(client: TestClient) -> None:
    sid = say_session(client)
    client.post(f"/session/{sid}/say", json={"message": "起步"})
    audit = client.get(f"/session/{sid}/audit/0").json()
    assert audit["prompt"]
    assert audit["o_i"]
    assert isinstance(audit["failed_checks"], list)
    assert client.get(f"/session/{sid}/audit/99").status_code == 404


def test_overrides_switch_the_work_model(client: TestClient) -> None:
    """切模型只改一个字段 —— 前端靠它做"离线排练"开关。"""
    sid = say_session(client)
    row = client.get(f"/session/{sid}/state").json()["metrics"]["experiment_row"]
    assert row["work_model_endpoint"] == "fake"
    assert row["work_is_scripted"] is True, "设置表必须自报这组是脚本跑的"
    # 而配置表里的 C 组仍然是真实模型
    arms = {a["arm"]: a for a in client.get("/arms").json()["arms"]}
    assert arms["C"]["work_model_endpoint"] == "openai"
    assert arms["C"]["work_is_scripted"] is False


def test_say_on_missing_session_is_404(client: TestClient) -> None:
    assert client.post("/session/nope/say", json={"message": "x"}).status_code == 404
    assert client.get("/session/nope/turns").status_code == 404


def test_scripted_oracle_never_writes_the_deliverable_repo(client: TestClient) -> None:
    """脚本 oracle 绝不许写交付仓库 —— CLI 里有这道闸，接口这条路径原先没有。

    arm 配置里的 workspace_path 指向交付仓库；前端离线排练时若照单全收，
    罐头输出会被写进交付物、还会在那儿建出 arm-* 分支。
    """
    res = client.post(
        "/session/create",
        json={
            "project": "one_sentence_edit",
            "prefix_path": str(
                Path(__file__).resolve().parents[1] / "prefixes" / "one_sentence_edit"
            ),
            "config_path": "configs/arm_c_fact_chain.yaml",
            "overrides": {"work_model": {"endpoint": "fake"},
                          "compression": {"model": "heuristic"}},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["workspace_is_deliverable"] is False
    assert "drawing-video-toolchain" not in body["workspace"], body["workspace"]
    # 会话确实建出来了，只是工作区落在 state 下
    assert body["arm"] == "C"
    assert body["workspace"]


def test_explicit_scripted_workspace_is_respected(client: TestClient, tmp_path: Path) -> None:
    """显式指定工作区时照办（预览/彩排要靠它），交付仓库除外。"""
    ws = tmp_path / "scratch"
    ws.mkdir()
    res = client.post(
        "/session/create",
        json={
            "project": "one_sentence_edit",
            "prefix_path": str(
                Path(__file__).resolve().parents[1] / "prefixes" / "one_sentence_edit"
            ),
            "config_path": "configs/arm_c_fact_chain.yaml",
            "overrides": {
                "work_model": {"endpoint": "fake"},
                "compression": {"model": "heuristic"},
                "workspace_path": str(ws),
                "branch_per_arm": False,
            },
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["workspace"] == str(ws.resolve())
