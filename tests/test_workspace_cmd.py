"""实验仓库的状态检查与清理。

这条命令来自一次真实的踩坑：**一次跑挂的会话照样会建出分支、并把仓库留在 arm-X 上**。
交付仓库里因此残留过 4 个空分支，而且下一次 `git clone` 直接克隆到错的分支
（克隆默认检出源的 HEAD）—— 不报错，但后面每件事都建立在错的前提上。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pciso import cli


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


def seed(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# 实验前脚手架\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "seed")
    git(repo, "config", "user.email", "seed@localhost")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: 基线")
    # 一个空的实验分支（模拟"建了会话但每轮都失败"）
    git(repo, "branch", "arm-A", "main")
    # 一个真有产出的实验分支
    git(repo, "checkout", "-q", "-B", "arm-B", "main")
    (repo / "drawvid").mkdir()
    (repo / "drawvid" / "core.py").write_text('"""真产出。"""\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "[round 1] 阶段 1\n\nPCISO-Round: 1")
    git(repo, "checkout", "-q", "arm-A")  # 模拟跑挂后停在 arm-X 上
    return repo


def test_reports_state_without_touching_anything(
    tmp_path: Path, capsys
) -> None:
    repo = seed(tmp_path)
    rc = cli.main(["workspace", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "arm-A" in out and "空实验分支" in out
    assert "⚠ 不在基线分支上" in out
    # 没加 --prune 就不许动任何东西
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "arm-A"
    assert "arm-A" in git(repo, "branch")


def test_prune_removes_only_empty_arm_branches(tmp_path: Path, capsys) -> None:
    repo = seed(tmp_path)
    rc = cli.main(["workspace", "--repo", str(repo), "--prune"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已删除空分支 arm-A" in out
    branches = git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "arm-A" not in branches
    assert "arm-B" in branches, "有产出的分支不许删"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # arm-B 的产出还在
    assert git(repo, "show", "arm-B:drawvid/core.py")


def test_non_repo_is_rejected(tmp_path: Path, capsys) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["workspace", "--repo", str(plain)]) == 2
    assert "不是 git 仓库" in capsys.readouterr().err


def test_missing_base_is_rejected(tmp_path: Path, capsys) -> None:
    repo = seed(tmp_path)
    assert cli.main(["workspace", "--repo", str(repo), "--base", "nope"]) == 3
    assert "不存在" in capsys.readouterr().err
