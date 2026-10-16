"""每组一条分支，并从基线复位 —— 七组共用同一个目标仓库时的**可比性前提**。

如果七组都在一条分支上往下写，B 组会从 A 组的成品接着做、C 组再继承 B 组。
那样跑出来的违反数曲线看起来完全正常，但它量的是"流水线的第几阶段"，
不是"配置的差异" —— 结论一条都不成立，而且没有任何报错。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


def seed_repo(repo: Path) -> None:
    """建一个有基线的仓库：main 上只有实验前脚手架。"""
    (repo / "post").mkdir(parents=True)
    (repo / "post" / "README.md").write_text("实验后加固区。\n", encoding="utf-8")
    (repo / ".gitignore").write_text("out/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "seed")
    git(repo, "config", "user.email", "seed@localhost")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: 实验前脚手架")


def start_arm(tmp_path: Path, repo: Path, arm: str, rounds: int) -> Session:
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": arm,
        "prefix_path": str(PREFIX),
        "state_dir": str(tmp_path / f"state-{arm}"),
        "max_rounds": rounds,
        "workspace_path": str(repo),
        "fake_oracle": "drawing_video",
        "branch_per_arm": True,
        "arm_base_branch": "main",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm=arm,
        ),
        base_dir=tmp_path,
        state_dir=str(tmp_path / f"state-{arm}"),
    )


def test_each_arm_branches_off_the_same_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_repo(repo)
    base = git(repo, "rev-parse", "HEAD")

    session_a = start_arm(tmp_path, repo, "A", rounds=3)
    assert session_a.store.read_meta()["branch"]["branch"] == "arm-A"
    for _ in range(3):
        session_a.round(RoundRequest())
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "arm-A"

    # A 组做到第 3 阶段，drawvid/drawing.py 已经出现
    assert (repo / "drawvid" / "drawing.py").is_file()

    session_b = start_arm(tmp_path, repo, "B", rounds=1)
    assert session_b.store.read_meta()["branch"]["branch"] == "arm-B"
    # 复位到基线：A 组的产物必须已经从工作区消失
    assert not (repo / "drawvid").exists(), "切分支后 A 组的产物仍在，复位没生效"
    session_b.round(RoundRequest())

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "arm-B"
    # B 组第 1 轮提交的父提交就是 main 的 HEAD —— 证明它没有继承 A
    assert git(repo, "rev-parse", "arm-B~1") == base
    # 而 A 组的历史与 B 组互不相干
    assert git(repo, "log", "--format=%s", "arm-A").count("[round") == 3
    assert git(repo, "log", "--format=%s", "arm-B").count("[round") == 1
    # B 只做到第 1 阶段，不该有第 2 阶段的文件
    assert not (repo / "drawvid" / "drawing.py").exists()

    branches = git(repo, "branch", "--format=%(refname:short)")
    assert "arm-A" in branches and "arm-B" in branches
    assert "main" in branches


def test_branch_prepare_removes_leftover_untracked_files(tmp_path: Path) -> None:
    """上一组被回退的轮次可能留下未跟踪残骸，切分支时要清掉。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_repo(repo)

    session_a = start_arm(tmp_path, repo, "A", rounds=1)
    session_a.round(RoundRequest())
    # 模拟一个没被提交的残骸
    (repo / "leftover_from_arm_a.py").write_text("x = 1\n", encoding="utf-8")
    assert (repo / "leftover_from_arm_a.py").is_file()

    start_arm(tmp_path, repo, "B", rounds=1)
    assert not (repo / "leftover_from_arm_a.py").exists()


def test_branch_prepare_keeps_ignored_artifacts(tmp_path: Path) -> None:
    """只清未跟踪，不动 .gitignore 覆盖的缓存 —— `clean -fd` 而不是 `-fdx`。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_repo(repo)

    session_a = start_arm(tmp_path, repo, "A", rounds=1)
    session_a.round(RoundRequest())
    (repo / "out").mkdir(exist_ok=True)
    (repo / "out" / "render_cache.bin").write_bytes(b"\x00\x01")

    start_arm(tmp_path, repo, "B", rounds=1)
    assert (repo / "out" / "render_cache.bin").is_file()


def test_branch_prepare_never_destroys_an_uncommitted_workspace(tmp_path: Path) -> None:
    """**空仓库（或没有 main）时绝不动手**。

    这条是真被踩出来的：原实现在这种情况下会退化成了"就地建分支 + clean -fd"，
    而空仓库里所有文件都是未跟踪的 —— clean 把整个工作区删空了。
    真机上交付仓库的脚手架是已提交的，所以没炸；但那是运气，不是设计。
    """
    repo = tmp_path / "repo"
    (repo / "drawvid").mkdir(parents=True)
    (repo / "drawvid" / "core.py").write_text('"""别删我。"""\n', encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")  # 建了仓库但**没有任何提交**

    session = start_arm(tmp_path, repo, "Z", rounds=1)
    branch = session.store.read_meta()["branch"]
    assert branch["ok"] is False
    assert branch["cleaned"] is False
    assert "unborn" in branch["base"]
    # 文件必须原样还在
    assert (repo / "drawvid" / "core.py").read_text(encoding="utf-8") == '"""别删我。"""\n'


def test_branch_per_arm_is_recorded_in_meta(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_repo(repo)
    session = start_arm(tmp_path, repo, "C", rounds=1)
    branch = session.store.read_meta()["branch"]
    assert branch["ok"] is True
    assert branch["base"] == "main"
    assert branch["head"]


# --------------------------------------------------------------------------- #
# 真实现场：已有仓库 + 环境里没有 git 身份
# --------------------------------------------------------------------------- #


def seed_repo_without_identity(repo: Path) -> None:
    """一个**已存在**的仓库，有基线提交，但仓库与全局都没有 git 身份。

    这是真实交付仓库的样子（克隆来的仓库不会带 user.name，而本机全局身份是空的）。
    """
    (repo / "post").mkdir(parents=True)
    (repo / "post" / "README.md").write_text("加固区。\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    # 只用行内身份提交基线，**不写进仓库配置** —— 精确复现"没有身份"的仓库
    git(
        repo,
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@localhost",
        "commit",
        "-q",
        "-m",
        "chore: 基线",
    )
    assert git(repo, "config", "user.name") == ""
    assert git(repo, "log", "--oneline")


def test_branching_and_commits_work_without_a_git_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这条是真被踩出来的。**

    原实现有两处静默失败叠加：`git_init` 在已有仓库上提前返回、从不设置提交者身份，
    而 `git_commit` 失败时只 `return None`。结果是七组 x 8 轮**零提交**且毫无报错；
    更糟的是失败前 `git add -A` 把整份成品留在了索引里，下一组的 checkout 会把它带过去
    —— 实测里后面五组全都跑在了前一组的成品之上（第 1 轮违反数从 19 变成 0），
    分支隔离形同虚设，而报告看起来完美无缺。

    这里把全局/系统 git 配置指到空文件，从而**确定性地**复现"环境里没有身份"。
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))

    repo = tmp_path / "repo"
    repo.mkdir()
    seed_repo_without_identity(repo)

    # A 组跑 2 轮：必须真的产生 2 个提交
    session_a = start_arm(tmp_path, repo, "A", rounds=2)
    for _ in range(2):
        session_a.round(RoundRequest())
    assert git(repo, "log", "--format=%s", "arm-A").count("[round") == 2, (
        "提交没产生 —— 又是身份缺失那条路"
    )
    records = session_a.store.read_rounds()
    assert all(r["git_sha"] for r in records), "rounds.jsonl 里的 git_sha 不能是 null"

    # B 组必须从基线干净出发：A 组第 2 阶段才产出的文件不该在
    session_b = start_arm(tmp_path, repo, "B", rounds=1)
    assert session_b.store.read_meta()["branch"]["ok"] is True
    assert not (repo / "drawvid").exists(), "B 组继承了 A 组的产物 —— 分支隔离失效"
    assert git(repo, "status", "--porcelain") == "", "复位后工作区必须干净"

    session_b.round(RoundRequest())
    # B 组第 1 轮提交的父提交就是基线，而不是 A 的提交
    assert git(repo, "rev-list", "--count", "arm-B~1..arm-B") == "1"
    assert git(repo, "rev-list", "--count", "arm-A..arm-B") == "1"
