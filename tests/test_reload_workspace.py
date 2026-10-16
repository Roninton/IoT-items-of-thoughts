"""重载会话时工作区必须解析到同一个地方 —— 否则续跑会写到错的目录。

**这条是真机踩出来的。** `Session.create` 会把相对的 `workspace_path`
（配置里写的是 `../drawing-video-toolchain`）解析成绝对路径，但 `Session.load`
直接 `SessionConfig(**meta["config"])`、既不解析也不要求 base_dir ——
于是它**静默回退**到 `state/workspaces/<sid>` 这个空临时目录：

    cfg.workspace_path : ../drawing-video-toolchain
    store.workspace    : ...\\state\\workspaces\5fd7a064a2e0460c   ← 空的
    drawvid/__init__.py: False

后果很贵：`pciso resume`（就是为"跑挂了不用从头再来"造的）会接着往空目录里写，
而 git 提交要么落到别处要么直接失败 —— 七组 x 8 轮里断一次，续跑的那一段数据就废了。

所以修法是两条：配置解析时把 workspace_path 一并绝对化；
重载时若仍是相对路径而调用方没给 base_dir，**直接报错**，绝不静默换工作区。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso.config import SessionConfig
from pciso.models import SessionCreateRequest
from pciso.session import Session, SessionConflict

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


def make_session(tmp_path: Path) -> Session:
    """workspace_path 故意写成**相对** base_dir 的形式（真实配置文件就是这样）。"""
    (tmp_path / "target").mkdir()
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "REL",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": 2,
        "workspace_path": "target",  # 相对 base_dir
        "branch_per_arm": False,
        "fake_oracle": "drawing_video",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="REL",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


def test_create_resolves_relative_workspace(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    assert Path(session.cfg.workspace_path or "").is_absolute()
    assert session.store.workspace == (tmp_path / "target").resolve()


def test_stored_config_is_absolute(tmp_path: Path) -> None:
    """存进 session.json 的就该是绝对路径 —— 否则重载时无从解析。"""
    session = make_session(tmp_path)
    import json

    meta = json.loads((session.store.root / "session.json").read_text(encoding="utf-8"))
    assert Path(meta["config"]["workspace_path"]).is_absolute()


def test_reload_with_base_dir_lands_on_the_same_workspace(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    session.round(__import__("pciso.models", fromlist=["RoundRequest"]).RoundRequest())

    again = Session.load(session.store.session_id, str(tmp_path / "state"), base_dir=tmp_path)
    assert again.store.workspace == session.store.workspace
    assert (again.store.workspace / "drawvid").is_dir(), "重载后看不到上一轮的产物"


def test_reload_without_base_dir_refuses_instead_of_silently_relocating(
    tmp_path: Path,
) -> None:
    """**核心断言**：宁可报错，也不静默换到另一个工作区。

    旧实现的行为是"悄悄用 state/workspaces/<sid>"，那会把续跑写到错的地方，
    而且不会有任何报错 —— 属于最坏的一类失败。
    """
    session = make_session(tmp_path)
    # 手工把记录里的路径改回相对形式，模拟**已经存在**的旧会话
    import json

    meta_path = session.store.root / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["config"]["workspace_path"] = "target"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SessionConflict) as exc:
        Session.load(session.store.session_id, str(tmp_path / "state"))
    assert "相对路径" in str(exc.value)
    assert "静默" in str(exc.value)


def test_reload_of_an_old_relative_record_upgrades_it(tmp_path: Path) -> None:
    """给了 base_dir 就顺手把记录里的相对路径升级成绝对，避免下次再踩。"""
    session = make_session(tmp_path)
    import json

    meta_path = session.store.root / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["config"]["workspace_path"] = "target"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    again = Session.load(session.store.session_id, str(tmp_path / "state"), base_dir=tmp_path)
    assert again.store.workspace == (tmp_path / "target").resolve()
    upgraded = json.loads(meta_path.read_text(encoding="utf-8"))
    assert Path(upgraded["config"]["workspace_path"]).is_absolute()


def test_from_mapping_leaves_absolute_paths_alone(tmp_path: Path) -> None:
    absolute = (tmp_path / "elsewhere").resolve()
    cfg = SessionConfig.from_mapping(
        {"prefix_path": str(PREFIX), "workspace_path": str(absolute)}, base_dir=tmp_path
    )
    assert cfg.workspace_path == str(absolute)
