"""G 组：磁盘检索。

这一组的价值全在**可审计**上：读盘由 server 执行，于是三类失败能被计数 ——
无效检索（申请不存在的路径）、冗余检索（重读已给过的文件）、检索上下文成本（字节数）。
"磁盘不如在上下文中"这个论断，如果成立，就该在这三个数上显形。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso.models import RoundRequest, SessionCreateRequest
from pciso.oparse import parse_read_requests
from pciso.render import READ_RESULT_HEADER, render_workspace_manifest
from pciso.session import Session

# G 组每轮都要真跑一遍检索往返 + 验证，属于端到端级别 —— 标 slow。
pytestmark = pytest.mark.slow

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_parse_read_requests_preserves_order_and_duplicates() -> None:
    """重复项要保留：同一轮把同一路径要两遍是"没有记忆"的症状，得能被记成冗余检索。"""
    o_i = "@@READ a.py\n@@READ b.py\n@@READ a.py\n"
    assert parse_read_requests(o_i) == ["a.py", "b.py", "a.py"]


def test_parse_read_requests_rejects_traversal() -> None:
    assert parse_read_requests("@@READ ../secret.py\n@@READ /etc/passwd\n") == []


def test_parse_read_requests_ignores_other_markers() -> None:
    assert parse_read_requests("@@UNIT file a.py\n@@READ b.py\n") == ["b.py"]


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #


def test_manifest_lists_paths_and_sizes_but_no_content(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text('"""secret body"""\n', encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    manifest = render_workspace_manifest(tmp_path)
    assert "pkg/a.py" in manifest
    assert "secret body" not in manifest  # 只给路径与大小，内容要申请
    assert ".git" not in manifest


def test_empty_workspace_manifest(tmp_path: Path) -> None:
    assert "工作区为空" in render_workspace_manifest(tmp_path)


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


def make_retrieval_session(tmp_path: Path, *, mode: str, rounds: int = 3) -> Session:
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "G-TEST",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": rounds,
        "stateless": True,
        # prefix 复制到临时目录后目录名会变，显式指回正确的 oracle
        "fake_oracle": "drawing_video",
        "compression": {"mode": "off"},
        "work_model": {
            "endpoint": "fake",
            "retrieval": True,
            "retrieval_max_cycles": 2,
            "retrieval_max_files": 8,
            "retrieval_max_bytes": 60000,
            "fake_retrieval_mode": mode,
        },
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="G-TEST",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


def test_retrieval_round_takes_extra_model_calls(tmp_path: Path) -> None:
    """先申请、再产出 —— 一轮至少两次调用，这是 G 组的成本特征。"""
    session = make_retrieval_session(tmp_path, mode="good", rounds=2)
    first = session.round(RoundRequest())
    assert first.telemetry["o_i_source"] == "work_model_retrieval"
    assert first.telemetry["work_calls"] == 2
    assert first.committed
    # 仍然产出了产物（第二趟必须真的落盘，不能停在"只有检索申请"）
    assert first.telemetry["files_written"]


def test_denied_reads_are_counted(tmp_path: Path) -> None:
    """第 1 轮工作区是空的：申请什么都读不到，必须记成无效检索而不是静默失败。"""
    session = make_retrieval_session(tmp_path, mode="good", rounds=1)
    res = session.round(RoundRequest())
    reads = res.telemetry["reads"]
    assert reads["served"] == []
    assert reads["denied"]
    assert reads["redundant"] == []  # good 模式不该制造冗余
    assert session.metrics()["retrieval"]["invalid_read_rate"] == 1.0


def test_sloppy_mode_records_denied_reads(tmp_path: Path) -> None:
    """故意申请不存在的路径 —— 必须被记成无效检索，而不是静默失败。"""
    session = make_retrieval_session(tmp_path, mode="sloppy", rounds=2)
    session.round(RoundRequest())
    session.round(RoundRequest())
    r = session.metrics()["retrieval"]
    assert r["denied_total"] >= 1  # 故意申请的 does_not_exist.py
    assert r["invalid_read_rate"] and r["invalid_read_rate"] > 0
    assert r["extra_calls_total"] >= 2


def test_cross_round_reread_is_the_memoryless_signature(tmp_path: Path) -> None:
    """无记忆的代价：同一个文件被跨轮重读。

    这正是"磁盘检索会重复劳动"的可测量形式 —— 也是 H（上下文里的精炼记忆）
    应当占优的地方。数值在假模型上只是示意，机制才是要点。
    """
    session = make_retrieval_session(tmp_path, mode="good", rounds=3)
    for _ in range(3):
        session.round(RoundRequest())
    per_round = session.metrics()["per_round"]
    assert any(p["reads_redundant"] > 0 for p in per_round[1:]), per_round


def test_read_budget_is_enforced(tmp_path: Path) -> None:
    """预算必须严格：连截断提示语本身也占额度，不许虚报读过的字节数。"""
    session = make_retrieval_session(tmp_path, mode="good", rounds=2)
    session.cfg.work_model.retrieval_max_bytes = 1
    session.round(RoundRequest())
    session.round(RoundRequest())
    r = session.metrics()["retrieval"]
    assert r["bytes_total"] <= 1


def test_manifest_is_in_the_prompt_but_contents_are_not(tmp_path: Path) -> None:
    """G 组的起点只有清单（路径+大小），内容必须靠 @@READ 申请。"""
    session = make_retrieval_session(tmp_path, mode="good", rounds=2)
    session.round(RoundRequest())
    prompt = session.build_current_prompt()
    assert "磁盘检索" in prompt
    assert "drawvid/core.py" in prompt  # 清单里有路径
    assert READ_RESULT_HEADER not in prompt  # 但内容没有被塞进来


def test_non_retrieval_arm_has_no_read_protocol(tmp_path: Path) -> None:
    """F 组（无状态、无检索）连清单都不该有 —— 否则 F 与 G 的差别被抹掉。"""
    state_dir = tmp_path / "state2"
    session = Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config={
                "prefix_path": str(PREFIX),
                "state_dir": str(state_dir),
                "max_rounds": 1,
                "stateless": True,
                "fake_oracle": "drawing_video",
                "compression": {"mode": "off"},
                "work_model": {"endpoint": "fake"},
            },
            arm="F-TEST",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )
    prompt = session.build_current_prompt()
    assert READ_RESULT_HEADER not in prompt
    assert "@@READ" not in prompt


@pytest.mark.slow
def test_retrieval_session_reaches_the_same_ceiling_as_memory(tmp_path: Path) -> None:
    """检索组也必须能把项目做出来（否则测的是"坏掉了"而不是"检索弱"）。"""
    session = make_retrieval_session(tmp_path, mode="good", rounds=8)
    for _ in range(8):
        session.round(RoundRequest())
    status = {c.id: c.status for c in session.state().constraints}
    assert status["C15"] == "satisfied"  # 契约全部实现
    assert status["C10"] == "satisfied"  # 冻结测试完好
