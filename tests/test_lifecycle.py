"""端到端生命周期：整条流水线在离线状态下必须真的跑通。

这些测试是骨架的验收标准 —— 它们证明的不只是"代码不崩"，而是：
  - C 有界且逐轮推进
  - 事实链的锚点在真实代码里解析得到（零结构性幻觉）
  - 冻结测试每轮被恢复
  - 硬约束回归真的触发回退，且回退时 C 不前进
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pciso.models import RoundRequest, SessionCreateRequest
from pciso.prefix import PrefixBundle
from pciso.session import Session, SessionConflict

# 这个文件真跑 8 轮端到端，每轮都起 pytest+ruff 子进程 —— 标 slow，
# 不进 TDD 快车道（`pytest -m "not slow"`），但提交前必须全量跑。
pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / "prefixes" / "drawing_video"


def make_config(
    state_dir: Path,
    *,
    max_rounds: int = 3,
    fault: str = "none",
    fault_after: int = 1,
    **overrides,
) -> dict:
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "TEST",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": max_rounds,
        "c_max_tokens": overrides.pop("c_max_tokens", 1000),
        "stateless": overrides.pop("stateless", False),
        "fake_oracle": "drawing_video",
        "compression": {"mode": overrides.pop("compression", "fact_chain"), "model": "heuristic"},
        "work_model": {"endpoint": "fake", "fake_fault": fault, "fake_fault_after": fault_after},
        "validator": {"tests": "run_on_each_round", "docs": "check_coverage"},
    }
    cfg.update(overrides)
    return cfg


def make_session(tmp_path: Path, **kw) -> Session:
    state_dir = tmp_path / "state"
    req = SessionCreateRequest(
        project="drawing_video_toolchain",
        prefix_path=str(PREFIX),
        config=make_config(state_dir, **kw),
        arm="TEST",
    )
    return Session.create(req, base_dir=ROOT, state_dir=str(state_dir))


def test_create_writes_meta_and_prefix_copy(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    meta = session.store.read_meta()
    assert meta["prefix_hash"] == PrefixBundle.load(PREFIX).hash
    assert (session.store.root / "prefix.md").exists()
    assert meta["experiment_row"]["compression"] == "fact_chain"
    assert len(meta["stages"]) == 8


def test_rounds_advance_and_c_stays_bounded(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=4)
    seen_tokens: list[int] = []
    for _ in range(4):
        res = session.round(RoundRequest())
        assert res.committed
        assert res.c_i.est_tokens <= 1000
        seen_tokens.append(res.c_i.est_tokens)
    assert session.store.round_index() == 4
    # C 有界，但事实链确实在长
    assert seen_tokens[-1] > seen_tokens[0]


def test_facts_are_anchored_and_resolvable(tmp_path: Path) -> None:
    """结构性事实零幻觉：AST 抽取的锚点必须全部解析得到。"""
    session = make_session(tmp_path, max_rounds=3)
    for _ in range(3):
        session.round(RoundRequest())
    state = session.state()
    assert state.fact_chain
    unresolved = [f.id for f in state.fact_chain if f.unanchored]
    assert unresolved == []
    assert state.anchor_resolvable_rate() == 1.0


def test_failure_chain_captures_rejected_designs(tmp_path: Path) -> None:
    """阶段 4 与 6 是人为制造的诱惑点，失败链必须真的接到。"""
    session = make_session(tmp_path, max_rounds=6)
    for _ in range(6):
        session.round(RoundRequest())
    failures = session.state().failure_chain
    assert len(failures) == 2
    text = " ".join(f.attempt + f.reason for f in failures)
    assert "ffmpeg" in text
    assert "合并" in text


def test_frozen_tests_are_restored_every_round(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=2)
    manifest = {}
    for p in sorted((PREFIX / "tests_frozen").rglob("*")):
        if p.is_file():
            manifest[p.relative_to(PREFIX / "tests_frozen").as_posix()] = hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
    for _ in range(2):
        session.round(RoundRequest())
        for rel, want in manifest.items():
            got = hashlib.sha256(
                (session.store.workspace / "tests" / "frozen" / rel).read_bytes()
            ).hexdigest()
            assert got == want


def test_hard_regression_triggers_revert_and_c_does_not_advance(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=3, fault="touch_frozen", fault_after=2)
    first = session.round(RoundRequest())
    assert first.judge.decision == "accept"
    c_after_first = first.c_i.model_dump(mode="json")

    second = session.round(RoundRequest())
    assert second.judge.decision == "revert"
    assert "frozen_tests" in second.judge.regressions
    assert second.judge.reverted_files
    # 回退的那一轮 C 不前进
    assert second.c_i.model_dump(mode="json") == c_after_first

    metrics = session.metrics()
    assert metrics["reverts"] == 1
    assert metrics["revert_rate"] > 0


def test_reverted_round_still_appears_in_trajectory(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=3, fault="todo", fault_after=2)
    session.round(RoundRequest())
    session.round(RoundRequest())
    rounds = session.store.read_rounds()
    assert len(rounds) == 2  # 轨迹日志是 append-only，回退轮也在
    assert rounds[-1]["judgement"] == "accept"  # todo 是软约束，不回退
    assert rounds[-1]["validation"]["violations"]


def test_round_index_assertion_conflicts(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=3)
    session.round(RoundRequest())
    with pytest.raises(SessionConflict):
        session.round(RoundRequest(round_index=7))


def test_max_rounds_is_enforced(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=1)
    session.round(RoundRequest())
    with pytest.raises(SessionConflict):
        session.round(RoundRequest())


def test_injected_o_i_avoids_calling_the_model(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=2)
    injected = (
        "注入的一轮。\n\n```file:pkg_x/api.py\n"
        '"""注入。"""\n\n\ndef g(a: int) -> int:\n    """注入的函数。"""\n    return a\n```\n'
    )
    res = session.round(RoundRequest(o_i=injected))
    assert res.telemetry["o_i_source"] == "injected"
    assert (session.store.workspace / "pkg_x" / "api.py").exists()


def test_dry_run_does_not_touch_workspace(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=2)
    res = session.round(RoundRequest(dry_run=True))
    assert res.committed is False
    assert res.prompt
    assert session.store.round_index() == 0
    assert not list(session.store.workspace.rglob("*.py"))


def test_declared_unit_without_state_change_is_flagged_as_unbacked(tmp_path: Path) -> None:
    """声称改完了某个文件、但根本没写它 —— 自我报告注水，客观可查。

    这正是"@@UNIT 声明只能当因变量、不能当控制信号"的可操作化：
    它不改变压缩时机，但它是一个可计数的诚实度指标。
    """
    session = make_session(tmp_path, max_rounds=2)
    injected = (
        "@@UNIT file drawvid/never_written.py\n"
        "@@UNIT file drawvid/core.py\n\n"
        "```file:drawvid/core.py\n"
        '"""核心。"""\n\n\nclass Point:\n    """点。"""\n    x = 0\n```\n'
    )
    session.round(RoundRequest(o_i=injected))
    report = session.metrics()["self_report"]
    assert report["declared_units_total"] == 2
    assert report["unbacked_claims_total"] == 1
    assert "drawvid/never_written.py" in report["unbacked_claims"]
    assert report["unbacked_claim_rate"] == 0.5
    # derived 单位（真实落盘 + 一次验证）也在，且与 declared 分开计量
    assert report["derived_units_total"] == 2


def test_round_without_file_delta_is_recorded(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=2)
    session.round(RoundRequest(o_i="这一轮只做调研，没有落盘。\n"))
    assert session.metrics()["self_report"]["rounds_without_file_delta"] == [1]


def test_prefix_hash_change_invalidates_session(tmp_path: Path) -> None:
    """冻结前缀是本案前提：P 改了就不许复用旧会话。"""
    prefix_copy = tmp_path / "prefix"
    shutil.copytree(PREFIX, prefix_copy)
    state_dir = tmp_path / "state"
    req = SessionCreateRequest(
        project="drawing_video_toolchain",
        prefix_path=str(prefix_copy),
        config=make_config(state_dir, prefix_path=str(prefix_copy)),
    )
    session = Session.create(req, base_dir=ROOT, state_dir=str(state_dir))
    sid = session.store.session_id
    (prefix_copy / "PROJECT.md").write_text("# 被偷改了\n", encoding="utf-8")
    with pytest.raises(SessionConflict):
        Session.load(sid, state_dir)


def test_metrics_shape_for_curve_plotting(tmp_path: Path) -> None:
    session = make_session(tmp_path, max_rounds=3)
    for _ in range(3):
        session.round(RoundRequest())
    m = session.metrics()
    assert m["rounds"] == 3
    assert len(m["per_round"]) == 3
    assert {"violation_count", "prompt_tokens_est", "c_tokens", "judgement"} <= set(
        m["per_round"][0]
    )
    assert m["compression_quality"]["anchor_resolvable_rate"] == 1.0
    assert "total" in m["tokens"]


@pytest.mark.slow
def test_eight_rounds_produce_a_runnable_project(tmp_path: Path) -> None:
    """完整 8 轮：脚本 oracle 的分数上限必须真的能跑。

    它同时是个**上限参照**：真实对照组的成绩如果低于它，说明验证器在工作；
    如果高于它，说明尺子坏了。
    """
    session = make_session(tmp_path, max_rounds=8)
    for _ in range(8):
        session.round(RoundRequest())

    ws = session.store.workspace
    assert (ws / "drawvid" / "pipeline.py").exists()
    assert (ws / "README.md").exists()
    assert (ws / "docs" / "ARCHITECTURE.md").exists()

    env_pythonpath = str(ws)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ws),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**dict(__import__("os").environ), "PYTHONPATH": env_pythonpath},
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    status = {c.id: c.status for c in session.state().constraints}
    assert status["C2"] == "satisfied"  # 接口契约不破坏
    assert status["C3"] == "satisfied"  # 无循环依赖
    assert status["C10"] == "satisfied"  # 冻结测试完好
    assert status["C12"] == "satisfied"  # 分层不违规
    assert status["C15"] == "satisfied"  # 契约全部实现
