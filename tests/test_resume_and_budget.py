"""真机跑的两道保险：预算闸与断点续跑。

七组 x 8 轮要花二十分钟和真钱，所以最贵的两种失败模式是：

1. **没人发现它一直在烧**（限流重试、循环、模型抽风）-> 预算闸
2. **中途挂掉就得从头再来**（网络、限流、机器重启）-> 断点续跑

而且续跑有两种污染方式必须挡住：换工作模型（同组混进两种模型）、
以及重复跑已完成的轮次（重复计费）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso import cli
from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session, SessionConflict

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


def make_session(tmp_path: Path, *, rounds: int = 8, budget: int = 0) -> Session:
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "BUDGET",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": rounds,
        "max_total_tokens": budget,
        "fake_oracle": "drawing_video",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="BUDGET",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


# --------------------------------------------------------------------------- #
# 预算闸
# --------------------------------------------------------------------------- #


def test_budget_guard_stops_the_session(tmp_path: Path) -> None:
    session = make_session(tmp_path, budget=1)
    session.round(RoundRequest())
    with pytest.raises(SessionConflict) as exc:
        session.round(RoundRequest())
    assert "预算耗尽" in str(exc.value)
    assert session.store.round_index() == 1, "越界的那一轮不能被提交"


def test_budget_zero_means_unlimited(tmp_path: Path) -> None:
    session = make_session(tmp_path, rounds=3, budget=0)
    for _ in range(3):
        session.round(RoundRequest())
    assert session.store.round_index() == 3


def test_total_tokens_falls_back_to_prompt_estimate(tmp_path: Path) -> None:
    """脚本 oracle 不报 usage —— 累计恒为 0 的话预算闸就是摆设。"""
    session = make_session(tmp_path, rounds=2)
    session.round(RoundRequest())
    session.round(RoundRequest())
    assert session.total_tokens() > 0
    # 与 metrics 里的名义合计不同：那是真实用量的和（这里是 0）
    assert session.metrics()["tokens"]["total"] == 0


def test_budget_is_recorded_in_the_experiment_row(tmp_path: Path) -> None:
    """预算是实验设置的一部分，必须进表 —— 否则事后无法判断某组为何少跑了几轮。"""
    session = make_session(tmp_path, budget=12345)
    assert session.cfg.experiment_row()["max_total_tokens"] == 12345
    assert session.store.read_meta()["experiment_row"]["max_total_tokens"] == 12345


def test_budget_stop_leaves_a_resumable_state(tmp_path: Path) -> None:
    session = make_session(tmp_path, rounds=5, budget=1)
    session.round(RoundRequest())
    with pytest.raises(SessionConflict):
        session.round(RoundRequest())
    # 提高预算后能接着跑，且不重复已完成的轮次
    session.cfg.max_total_tokens = 10**9
    res = session.round(RoundRequest())
    assert res.round_index == 1
    assert session.store.round_index() == 2


# --------------------------------------------------------------------------- #
# 断点续跑
# --------------------------------------------------------------------------- #


def test_reload_continues_from_the_next_round(tmp_path: Path) -> None:
    session = make_session(tmp_path, rounds=5)
    for _ in range(2):
        session.round(RoundRequest())
    sid = session.store.session_id

    again = Session.load(sid, str(tmp_path / "state"))
    assert again.store.round_index() == 2
    res = again.round(RoundRequest())
    assert res.round_index == 2, "必须从第 3 轮（下标 2）继续，而不是从头"
    assert again.store.round_index() == 3
    # 历史没有重复
    assert [r["round_index"] for r in again.store.read_rounds()] == [0, 1, 2]


def test_reload_keeps_the_same_c_state(tmp_path: Path) -> None:
    session = make_session(tmp_path, rounds=4)
    for _ in range(2):
        session.round(RoundRequest())
    before = session.state().model_dump(mode="json")
    again = Session.load(session.store.session_id, str(tmp_path / "state"))
    assert again.state().model_dump(mode="json") == before


def test_cli_resume_refuses_offline(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """换工作模型会让同一组的轨迹混进两种模型 —— 而那是实验的自变量。"""
    session = make_session(tmp_path, rounds=3)
    session.round(RoundRequest())
    rc = cli.main(
        [
            "resume",
            "--session-id",
            session.store.session_id,
            "--state-dir",
            str(tmp_path / "state"),
            "--offline",
        ]
    )
    assert rc == 6
    assert "混进两种模型" in capsys.readouterr().err


def test_cli_resume_missing_session(tmp_path: Path) -> None:
    rc = cli.main(
        ["resume", "--session-id", "deadbeef", "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 4


def test_cli_resume_continues_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    session = make_session(tmp_path, rounds=5)
    for _ in range(2):
        session.round(RoundRequest())

    # resume 会做凭据检查；写成 fake endpoint 就不需要凭据
    rc = cli.main(
        [
            "resume",
            "--session-id",
            session.store.session_id,
            "--state-dir",
            str(tmp_path / "state"),
            "--rounds",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "已完成 2/5 轮，续跑 2 轮" in out
    assert "轮 3 阶段 3" in out and "轮 4 阶段 4" in out

    again = Session.load(session.store.session_id, str(tmp_path / "state"))
    assert again.store.round_index() == 4


def test_cli_resume_respects_max_rounds(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    session = make_session(tmp_path, rounds=3)
    for _ in range(3):
        session.round(RoundRequest())
    rc = cli.main(
        ["resume", "--session-id", session.store.session_id, "--state-dir", str(tmp_path / "state")]
    )
    assert rc == 0
    assert "续跑 0 轮" in capsys.readouterr().out
