"""逐轮 git 提交：实验生成的代码与人工改动之间的边界必须**机器可查**。

目标里写的是「server 逐轮以带 PCISO-Round trailer 的 git 提交写入目标仓库」。
这条如果只是"实现过"，就等于没验证：trailer 少了、回退轮也提交了、或者
`--invert-grep` 那条查询根本分不出人工改动 —— 任何一种都会让边界失效，
而边界失效之后"哪些代码是模型写的"就再也回答不了。

所以这里用一个**一次性仓库**把行为锁死（绝不用交付仓库当试验场）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout.strip()


def make_git_session(tmp_path: Path, *, rounds: int, fault: str = "none", fault_after: int = 1):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    state_dir = tmp_path / "state"

    def build(session_rounds: int, name: str) -> Session:
        cfg = {
            "project": "drawing_video_toolchain",
            "arm": "GIT-TEST",
            "prefix_path": str(PREFIX),
            "state_dir": str(state_dir / name),
            "max_rounds": session_rounds,
            "workspace_path": str(repo),
            "fake_oracle": "drawing_video",
            "compression": {"mode": "fact_chain", "model": "heuristic"},
            "work_model": {"endpoint": "fake", "fake_fault": fault, "fake_fault_after": fault_after},
        }
        return Session.create(
            SessionCreateRequest(
                project="drawing_video_toolchain",
                prefix_path=str(PREFIX),
                config=cfg,
                arm="GIT-TEST",
            ),
            base_dir=tmp_path,
            state_dir=str(state_dir / name),
        )

    return build(rounds, "a"), repo, build


def test_each_accepted_round_becomes_one_trailer_commit(tmp_path: Path) -> None:
    session, repo, _ = make_git_session(tmp_path, rounds=3)
    for _ in range(3):
        res = session.round(RoundRequest())
        assert res.judge.decision == "accept"

    log = git(repo, "log", "--format=%s%n%b%n---")
    assert log.count("PCISO-Round:") == 3, log
    for n in (1, 2, 3):
        assert f"PCISO-Round: {n}" in log
    assert "PCISO-Arm: GIT-TEST" in log
    assert "PCISO-Prefix-Hash:" in log
    assert "PCISO-Judgement: accept" in log

    # 目标仓库的工作区里确实有产物
    assert (repo / "drawvid" / "core.py").is_file()
    assert (repo / ".git").is_dir()


def test_trailers_are_parseable_as_trailers_not_just_text(tmp_path: Path) -> None:
    """**"文本里有"不等于"是 trailer"。**

    实测：正文与 trailer 块之间没有空行时，`--grep='^PCISO-Round:'` 照样能搜到，
    但 `%(trailers:key=PCISO-Round,valueonly)` 返回空字符串 ——
    任何按 trailer 取值的工具（含 scripts/verify_rehearsal.py）都会看到空。
    这条测试存在的理由就是：只测 --grep 会让"逐轮可溯源"变成文本上的巧合。
    """
    session, repo, _ = make_git_session(tmp_path, rounds=2)
    for _ in range(2):
        session.round(RoundRequest())

    shas = git(repo, "log", "--format=%H").split()
    assert len(shas) == 2
    parsed = {}
    for sha in shas:
        out = git(repo, "log", "-1", "--format=%(trailers:key=PCISO-Round,valueonly)", sha)
        parsed[out.strip()] = sha
    assert set(parsed) == {"1", "2"}, f"trailer 没被 git 解析出来：{parsed}"

    arm = git(
        repo, "log", "-1", "--format=%(trailers:key=PCISO-Arm,valueonly)", shas[0]
    ).strip()
    assert arm == "GIT-TEST"
    # 正文与 trailer 块之间确实有空行
    body = git(repo, "log", "-1", "--format=%B", shas[0])
    assert "\n\nPCISO-Round:" in body.replace("\r\n", "\n")


def test_reverted_round_leaves_no_commit(tmp_path: Path) -> None:
    """回退的那一轮不能提交：否则"模型写的代码"里会混进被判定为不合格的版本。"""
    session, repo, _ = make_git_session(
        tmp_path, rounds=3, fault="touch_frozen", fault_after=2
    )
    decisions = [session.round(RoundRequest()).judge.decision for _ in range(3)]
    assert decisions[1] == "revert", decisions

    log = git(repo, "log", "--format=%s")
    assert log.count("[round") == 2, log  # 只提交了第 1、3 轮
    assert "PCISO-Round: 2" not in git(repo, "log", "--format=%b")


def test_boundary_query_separates_experiment_from_manual_edits(tmp_path: Path) -> None:
    """README 里承诺的那两条查询必须真的分得开。"""
    session, repo, _ = make_git_session(tmp_path, rounds=2)
    for _ in range(2):
        session.round(RoundRequest())

    # 模拟"实验之后的人工改动"
    (repo / "post").mkdir(exist_ok=True)
    (repo / "post" / "patch.py").write_text('"""人工加固。"""\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=human", "-c", "user.email=h@localhost", "commit",
        "-q", "-m", "fix: 人工修复一个边界情况")

    experiment = git(repo, "log", "--grep=^PCISO-Round:", "--format=%s")
    manual = git(repo, "log", "--grep=^PCISO-Round:", "--invert-grep", "--no-merges",
                 "--format=%s", "--", ".")
    assert experiment.count("[round") == 2
    assert "[round" not in manual
    assert "人工修复" in manual
