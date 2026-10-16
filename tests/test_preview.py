"""眼睛通道：工作模型看不到自己画的帧，人和视觉模型得能看到。

这条通道不依赖任何凭据，所以它能离线验证；而它验证的恰恰是最要紧的一件事 ——
**被测项目能不能真的出一支能播的片子**（用项目自己的 `pipeline.make_video` 跑）。

两条必须守住的约束：
  - 预览不许往被测项目里写文件（示例素材与输出都落在 state/previews/ 下）
  - blob 端点不许有路径穿越的余地（它把文件内容直接交给浏览器）
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pciso.api as api
from pciso.models import SessionCreateRequest
from pciso.session import Session
from pciso.workmodels import STAGES_ROOT

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "one_sentence_edit"

# 这个文件真跑一遍成片渲染（纯 Python 逐帧），属于端到端级别 —— 标 slow，
# 让它不进 TDD 快车道（`pytest -m "not slow"`）。
pytestmark = pytest.mark.slow


def assemble_oracle(prefix_name: str, dest: Path) -> None:
    """把某个载体的 8 个阶段叠加到 dest（后面覆盖前面）。"""
    stages = STAGES_ROOT / prefix_name / "stages"
    for stage in sorted(p for p in stages.iterdir() if p.is_dir()):
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            target = dest / path.relative_to(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


@pytest.fixture
def full_workspace(tmp_path: Path) -> Path:
    """一个已经被 oracle 填满的工作区 —— 预览要真的能出片才算通过。"""
    ws = tmp_path / "target"
    ws.mkdir()
    assemble_oracle("one_sentence_edit", ws)
    return ws


def make_session(tmp_path: Path, workspace: Path | None) -> Session:
    state_dir = tmp_path / "state"
    cfg = {
        "project": "one_sentence_edit",
        "arm": "PREVIEW",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": 1,
        "fake_oracle": "one_sentence_edit",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    if workspace is not None:
        cfg["workspace_path"] = str(workspace)
    return Session.create(
        SessionCreateRequest(
            project="one_sentence_edit",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="PREVIEW",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


# --------------------------------------------------------------------------- #
# 服务端那一半
# --------------------------------------------------------------------------- #


def test_preview_produces_a_playable_film(tmp_path: Path, full_workspace: Path) -> None:
    session = make_session(tmp_path, full_workspace)
    # 句子里的 "1 秒" 会被确定性的规划规则读成 1000ms -> 30 帧。
    # 之前用默认 12000ms = **360 帧**，每个测试真跑一遍纯 Python 渲染 ≈14s，
    # 三个测试就吃掉整套测试 18% 的时间。产物形状完全一样，没必要渲染那么多帧。
    result = session.render_preview("1 秒的片子", timeout_s=600)

    assert result["ok"] is True, result.get("stderr")
    assert result["frame_count"] > 0
    for name in ("movie.apng", "subs.vtt", "bgm.wav", "plan.json", "index.html"):
        assert name in result["files"], f"缺产物 {name}：{result}"
    assert result["player"] == "previews/latest/index.html"
    # 产物真的落盘了，而且不是空文件
    apng = session.store.root / "previews" / "latest" / "movie.apng"
    assert apng.is_file() and apng.stat().st_size > 1000


def test_preview_does_not_touch_the_target_workspace(
    tmp_path: Path, full_workspace: Path
) -> None:
    """预览往被测项目里写文件 = 直接污染实验。"""
    session = make_session(tmp_path, full_workspace)
    before = sorted(p.relative_to(full_workspace).as_posix() for p in full_workspace.rglob("*"))
    session.render_preview("1 秒的片子", timeout_s=600)
    after = sorted(p.relative_to(full_workspace).as_posix() for p in full_workspace.rglob("*"))
    assert after == before, "预览改了工作区"


def test_preview_fails_gracefully_on_an_empty_workspace(tmp_path: Path) -> None:
    """接口还没实现时，预览要给出清楚的原因，而不是抛异常把服务打挂。"""
    session = make_session(tmp_path, None)
    result = session.render_preview(timeout_s=120)
    assert result["ok"] is False
    assert "ModuleNotFoundError" in result["stderr"] or "No module named" in result["stderr"]


# --------------------------------------------------------------------------- #
# HTTP 那一层
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api, "DEFAULT_STATE_DIR", str(tmp_path / "state"))
    api._SESSIONS.clear()
    return TestClient(api.app)


def test_preview_and_blob_over_http(
    client: TestClient, tmp_path: Path, full_workspace: Path
) -> None:
    session = make_session(tmp_path, full_workspace)
    sid = session.store.session_id
    api._SESSIONS[sid] = session  # 走 HTTP 时用它已加载好的实例

    res = client.post(f"/session/{sid}/preview", json={"sentence": "1 秒的片子"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True, body.get("stderr")

    # 播放页能从 blob 端点取到
    page = client.get(f"/session/{sid}/blob/previews/latest/index.html")
    assert page.status_code == 200
    assert "<html" in page.text.lower()

    # 帧也能取到
    frame = client.get(f"/session/{sid}/blob/previews/latest/frames/frame_0000.png")
    assert frame.status_code == 200
    assert frame.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_blob_rejects_traversal(client: TestClient, tmp_path: Path) -> None:
    session = make_session(tmp_path, None)
    sid = session.store.session_id
    api._SESSIONS[sid] = session
    for evil in ("../../../../etc/passwd", "../../workspace/splash/README.md", ".git/config"):
        assert client.get(f"/session/{sid}/blob/{evil}").status_code == 404, evil


def test_blob_rejects_git_internals_even_inside_the_workspace(
    client: TestClient, tmp_path: Path
) -> None:
    """`.git/config` 就在工作区内，resolve 之后完全"合法" —— 但可能有带 token 的 remote URL。

    这是实测抓到的：`..` 穿越挡住了，`.git/config` 却返回了 200。
    """
    ws = tmp_path / "target"
    (ws / ".git").mkdir(parents=True)
    secret = '[remote "origin"]\n\turl = https://user:TOKEN@example.com/x.git\n'
    (ws / ".git" / "config").write_text(secret, encoding="utf-8")
    session = make_session(tmp_path, ws)
    sid = session.store.session_id
    api._SESSIONS[sid] = session

    for evil in (".git/config", ".git/HEAD", "./.git/config"):
        res = client.get(f"/session/{sid}/blob/{evil}")
        assert res.status_code == 404, f"{evil} 竟然可取：{res.status_code}"
        assert "TOKEN" not in res.text


def test_blob_missing_session_is_404(client: TestClient) -> None:
    assert client.get("/session/nope/blob/x.png").status_code == 404


# --------------------------------------------------------------------------- #
# 前端入口
# --------------------------------------------------------------------------- #


def test_chat_page_does_not_offer_the_eye(client: TestClient) -> None:
    """出片入口从界面撤了。

    眼睛通道本身在（`POST /session/{id}/preview`，本文件上面全覆盖），
    但它属于接口与脚本 —— 聊天窗口只负责"跟模型说话"，不该摆一个出片按钮。
    """
    page = client.get("/").text
    assert "看成片" not in page
    assert "/preview" not in page
