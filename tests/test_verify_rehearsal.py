"""体检脚本自己也要被测。

`scripts/verify_rehearsal.py` 是我在相信那张曲线之前唯一会跑的检查。
它要是漏报，我拿到的就是"看起来正常但七组其实是同一条流水线"的数据 ——
实测真的发生过一次（三处静默失败叠加）。所以这里用**人为构造的坏现场**
证明它抓得住那两种失效：继承前一组成品、提交失败还继续跑。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_rehearsal.py"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (proc.stdout or "").strip()


def commit(repo: Path, message: str, **trailers: object) -> str:
    # 正文与 trailer 块之间必须空一行，否则 git 不把它们当 trailer（实测过）
    body = message
    if trailers:
        body += "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items())
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "-m", body)
    return git(repo, "rev-parse", "HEAD")


def seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "post").mkdir(parents=True)
    (repo / "post" / "README.md").write_text("加固区。\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "chore: 基线")
    return repo


def write_session(
    state_dir: Path,
    sid: str,
    arm: str,
    rounds: list[dict],
    created: float,
    *,
    scripted: bool | None = None,
) -> None:
    d = state_dir / "sessions" / sid
    d.mkdir(parents=True)
    meta: dict = {"arm": arm, "created_at": created}
    if scripted is not None:
        meta["experiment_row"] = {"work_is_scripted": scripted}
    (d / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "rounds.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rounds) + "\n", encoding="utf-8"
    )


def run_checker(repo: Path, state_dir: Path, *extra: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), "--state-dir", str(state_dir), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_healthy_rehearsal_passes(tmp_path: Path) -> None:
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    for arm in ("A", "B"):
        git(repo, "checkout", "-q", "-B", f"arm-{arm}", base)
        sha = None
        for i in range(2):
            (repo / f"{arm}_{i}.py").write_text("x = 1\n", encoding="utf-8")
            sha = commit(
                repo, f"[round {i + 1}] 阶段 {i + 1}", **{"PCISO-Round": i + 1, "PCISO-Arm": arm}
            )
        write_session(
            state,
            f"sid-{arm}",
            arm,
            [{"round_index": i, "violation_count": 19, "git_sha": sha} for i in range(2)],
            created=1.0,
        )
    git(repo, "checkout", "-q", "main")

    rc, out = run_checker(repo, state)
    assert rc == 0, out
    assert "全部不变量通过" in out
    assert "起点一致" in out


def test_inherited_branch_is_caught(tmp_path: Path) -> None:
    """B 组从 A 组的尖端接着做 —— 这正是实测里静默发生过的那个失效。"""
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-A", base)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha_a = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "A"})

    # B 组的分支从 A 的尖端长出来（分支复位没生效）
    git(repo, "checkout", "-q", "-B", "arm-B", "arm-A")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "B"})
    git(repo, "checkout", "-q", "main")

    write_session(
        state, "sid-A", "A",
        [{"round_index": 0, "violation_count": 19, "git_sha": sha_a}], created=1.0,
    )
    write_session(
        state, "sid-B", "B",
        [{"round_index": 0, "violation_count": 0, "git_sha": git(repo, "rev-parse", "arm-B")}],
        created=2.0,
    )

    rc, out = run_checker(repo, state)
    assert rc == 1, out
    assert "继承了别组的产物" in out
    assert "起点不一样" in out


def test_missing_git_sha_is_caught(tmp_path: Path) -> None:
    """提交失败却继续跑 —— 轨迹轮数会比 git 提交数多。"""
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-A", base)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "A"})
    git(repo, "checkout", "-q", "main")

    write_session(
        state, "sid-A", "A",
        [
            {"round_index": 0, "violation_count": 19, "git_sha": sha},
            {"round_index": 1, "violation_count": 14, "git_sha": None},  # 提交失败
        ],
        created=1.0,
    )

    rc, out = run_checker(repo, state)
    assert rc == 1, out
    assert "git 提交数 1 != 轨迹轮数 2" in out
    assert "git_sha 为空" in out


def test_post_is_never_touched_by_the_experiment(tmp_path: Path) -> None:
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-A", base)
    (repo / "post" / "patch.py").write_text("人工加固\n", encoding="utf-8")
    sha = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "A"})
    git(repo, "checkout", "-q", "main")
    write_session(
        state, "sid-A", "A",
        [{"round_index": 0, "violation_count": 19, "git_sha": sha}], created=1.0,
    )

    rc, out = run_checker(repo, state)
    assert rc == 1, out
    assert "post/ 被实验改动过" in out


def test_only_the_newest_session_per_arm_is_used(tmp_path: Path) -> None:
    """状态目录里可能留着上一轮作废的会话 —— 那不该造成假警。"""
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-A", base)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "A"})
    git(repo, "checkout", "-q", "main")

    # 旧会话：同样是 arm A，但轮数是旧的、git_sha 为空
    write_session(
        state, "sid-old", "A",
        [{"round_index": i, "violation_count": 0, "git_sha": None} for i in range(5)],
        created=1.0,
    )
    write_session(
        state, "sid-new", "A",
        [{"round_index": 0, "violation_count": 19, "git_sha": sha}], created=99.0,
    )

    rc, out = run_checker(repo, state)
    assert rc == 0, out
    assert "全部不变量通过" in out


# --------------------------------------------------------------------------- #
# 按运行范围过滤：同一个 state/ 里既有离线彩排又有真机跑
# --------------------------------------------------------------------------- #


def test_model_filter_separates_real_from_scripted(tmp_path: Path) -> None:
    """**这条也是真机跑完体检时踩出来的。**

    `state/sessions` 里同时躺着离线矩阵的会话（脚本 oracle，第 1 轮违反数 19）
    和真机的会话（第 1 轮 16）。不过滤时脚本会把 19 当成"同一次运行的另一组"，
    于是真机的 16 被报成"起点不一致" —— 纯粹的假警报。
    """
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-C", base)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "C"})
    git(repo, "checkout", "-q", "main")

    rounds = [{"round_index": 0, "violation_count": 16, "git_sha": sha}]
    write_session(state, "sid-scripted", "C", rounds, created=1.0, scripted=True)
    write_session(state, "sid-real", "C", rounds, created=2.0, scripted=False)

    for extra in ((), ("--model", "real"), ("--model", "scripted")):
        rc, out = run_checker(repo, state, *extra)
        assert rc == 0, (extra, out)


def test_arms_filter_avoids_false_errors_for_single_arm_runs(tmp_path: Path) -> None:
    """跑单组时不该因为"别的组没有分支"而报错。"""
    repo = seed_repo(tmp_path)
    state = tmp_path / "state"
    base = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "-B", "arm-C", base)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha = commit(repo, "[round 1] 阶段 1", **{"PCISO-Round": 1, "PCISO-Arm": "C"})
    git(repo, "checkout", "-q", "main")
    write_session(
        state, "sid-c", "C",
        [{"round_index": 0, "violation_count": 16, "git_sha": sha}], created=1.0,
    )
    write_session(
        state, "sid-b", "B",
        [{"round_index": 0, "violation_count": 19, "git_sha": None}], created=1.0,
    )

    # 不带过滤：B 没有分支、且第 1 轮违反数与 C 不同 -> 报错（这是**对的**）
    rc, out = run_checker(repo, state)
    assert rc == 1, out
    assert "没有分支 arm-B" in out

    # 只看 C：不报错
    rc, out = run_checker(repo, state, "--arms", "C")
    assert rc == 0, out
    assert "arm-B" not in out
