"""彩排/实验结果的**独立性体检**：在相信那张曲线之前先跑这个。

为什么单独写一个脚本：报告是聚合出来的，而"七组其实是同一条流水线的七个阶段"
这种错误**在聚合数字里看不出来** —— 实测踩过一次（三处静默失败叠加：
七组 x 8 轮零提交、后五组跑在前一组成品之上，而报告完美无缺）。

所以每次跑完实验，都要用 git 与轨迹日志**逐组**核对下面这些不变量：

1. 每组都有自己的分支，且**第一轮提交的父提交就是基线**（没继承别的组）
2. 每组的轮次提交数 == 该组轨迹日志里的轮数
3. 每一轮的记录里 git_sha 非 null（不存在"提交失败但继续跑"）
4. **所有组第 1 轮的违反数必须相等** —— 同一套冻结 P + 确定性起点，
   第 1 轮的结果只能有一个值；不一致就说明某组没从干净基线出发
   （继承会让它突然变成 0，实测就是这样露出来的）
5. trailer 的 PCISO-Arm 与分支一致
6. `post/`（实验后人工加固区）在所有分支上都没被实验碰过

用法：
    python scripts/verify_rehearsal.py <repo> [--state-dir state-rehearsal] [--base main]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Windows 上 stdout 的编码跟着控制台/管道走（中文系统 = GBK），而这张表里有
# ✓ / ✗ / ❌ / ✅ —— 不改 UTF-8 就会在打印那一刻崩掉，崩出来的样子是
# "断言里一堆乱码 + 一个 UnicodeEncodeError 尾巴"，看不出根因（实测踩过：
# 7 个用例一起红，报出来的却是字符串不匹配）。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")


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


def trailer(repo: Path, sha: str, key: str) -> str:
    """取某个提交的 trailer 值（git 解析过的，不是文本搜索）。"""
    return git(repo, "log", "-1", f"--format=%(trailers:key={key},valueonly)", sha).strip()


def read_rounds(
    state_dir: Path,
    *,
    arms: set[str] | None = None,
    model: str = "any",
) -> dict[str, list[dict]]:
    """按 arm 归拢轨迹日志：**每组只认最新的那一次会话**。

    两处过滤都是被真实误报逼出来的：

    - **只认最新会话**：状态目录里会留着上一轮作废的会话（实测踩过），
      按 arm 合并会让"轨迹轮数 == git 提交数"这类断言报假警。
    - **按模型过滤**：同一个 `state/` 里既有离线彩排（脚本 oracle）又有真机跑。
      不过滤时，脚本会把离线那批的第 1 轮违反数（19）当成"同一次运行的另一组"，
      于是真机的 16 被报成"起点不一致" —— 这是纯粹的假警报。
      `model` 取 `real` / `scripted` / `any`。
    """

    def wanted(meta: dict) -> bool:
        if model == "any":
            return True
        row = meta.get("experiment_row") or {}
        scripted = bool(row.get("work_is_scripted"))
        return scripted if model == "scripted" else not scripted

    sessions = state_dir / "sessions"
    if not sessions.is_dir():
        return {}

    newest: dict[str, tuple[float, list[dict]]] = {}
    for session in sorted(sessions.iterdir()):
        meta_path = session / "session.json"
        rounds_path = session / "rounds.jsonl"
        if not (meta_path.is_file() and rounds_path.is_file()):
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        arm = str(meta.get("arm") or "?")
        if arms is not None and arm not in arms:
            continue
        if not wanted(meta):
            continue
        created = float(meta.get("created_at") or session.stat().st_mtime)
        records = [
            json.loads(line)
            for line in rounds_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records.sort(key=lambda r: r["round_index"])
        if arm not in newest or created > newest[arm][0]:
            newest[arm] = (created, records)
    return {arm: records for arm, (_, records) in sorted(newest.items()) if records}


def main() -> int:
    ap = argparse.ArgumentParser(description="检查各组的独立性不变量")
    ap.add_argument("repo", help="实验仓库路径")
    ap.add_argument("--state-dir", default="state-rehearsal")
    ap.add_argument("--base", default="main")
    ap.add_argument(
        "--arms",
        default="",
        help="只看这些组（逗号分隔）。跑单组时用它，否则会因为别的组没有分支而报假错",
    )
    ap.add_argument(
        "--model",
        default="any",
        choices=["any", "real", "scripted"],
        help="按工作模型过滤会话：real=真机，scripted=离线彩排。同一个 state/ 里两者都有",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = ROOT / state_dir
    arms_filter = {a.strip() for a in args.arms.split(",") if a.strip()} or None

    by_arm = read_rounds(state_dir, arms=arms_filter, model=args.model)
    if not by_arm:
        print(f"[abort] 在 {state_dir} 里没找到符合条件的轨迹日志", file=sys.stderr)
        return 2

    base_head = git(repo, "rev-parse", args.base)
    print(f"仓库   {repo}")
    print(f"基线   {args.base} = {base_head[:12]}")
    print(f"轨迹   {state_dir}")
    print()

    fresh = {arm: recs[0]["violation_count"] for arm, recs in by_arm.items() if recs}
    fresh_consistent = len(set(fresh.values())) <= 1
    reference = next(iter(fresh.values())) if fresh else None

    problems: list[str] = []
    print(f"{'组':<5}{'轨迹轮':<8}{'git提交':<9}{'首轮违反':<10}{'父=基线':<10}{'sha非空':<10}{'post未动':<10}")
    print("-" * 66)

    for arm in sorted(by_arm):
        recs = by_arm[arm]
        branch = f"arm-{arm}"
        if not git(repo, "rev-parse", "--verify", "--quiet", branch):
            problems.append(f"{arm}: 没有分支 {branch}")
            print(f"{arm:<5}{len(recs):<8}{'—':<9}")
            continue

        # 用 trailer **精确**挑出属于本组的提交。
        #
        # 不能靠"branch~n_commits 等于基线"来判父提交：继承会让提交数虚高，
        # 而那个算式照样落在基线上 —— 实测里这个弱点是被测试夹具逼出来的。
        #
        # 也不用把 trailer 塞进 --format 里一行取两个：`valueonly` 的值**自带换行**，
        # 按行切分会散架（这正是上一版返回 0 个提交的原因）。逐条查更稳。
        own: list[tuple[str, str]] = []
        foreign: set[str] = set()
        for sha in git(repo, "log", "--format=%H", f"{args.base}..{branch}").split():
            arm_v = trailer(repo, sha, "PCISO-Arm")
            if arm_v == arm:
                own.append((sha, trailer(repo, sha, "PCISO-Round")))
            elif arm_v:
                foreign.add(arm_v)

        n_commits = len(own)
        if foreign:
            problems.append(
                f"{arm}: 分支上有属于别组的提交（{sorted(foreign)}）—— 这一组继承了别组的产物"
            )
        if n_commits != len(recs):
            problems.append(f"{arm}: 本组 git 提交数 {n_commits} != 轨迹轮数 {len(recs)}")

        parent_ok = False
        first = next((r for r in own if r[1] == "1"), None)
        if first:
            first_parent = git(repo, "rev-parse", f"{first[0]}~1")
            parent_ok = first_parent == base_head
            if not parent_ok:
                problems.append(
                    f"{arm}: 第 1 轮提交的父提交 {first_parent[:12]} 不是基线 {base_head[:12]}"
                    f" —— 这一组从别组的产物上出发"
                )
        elif n_commits:
            problems.append(f"{arm}: 分支上找不到 PCISO-Round=1 的提交")

        sha_ok = all(r.get("git_sha") for r in recs)
        if not sha_ok:
            problems.append(f"{arm}: 有轮次的 git_sha 为空 —— 提交失败却没停下")

        for r in recs:
            sha = r.get("git_sha")
            if not sha:
                continue
            if trailer(repo, sha, "PCISO-Arm") != arm:
                problems.append(f"{arm}: 第 {r['round_index'] + 1} 轮的 PCISO-Arm 不对")
                break

        post_ok = not git(repo, "diff", "--name-only", f"{args.base}..{branch}", "--", "post")
        if not post_ok:
            problems.append(f"{arm}: post/ 被实验改动过（应只属于人工加固）")

        value = fresh.get(arm)
        match = value == reference
        if not match:
            problems.append(
                f"{arm}: 第 1 轮违反数 {value} 与其它组（{reference}）不一致"
                f" —— 起点不一样，对照不成立"
            )
        print(
            f"{arm:<5}{len(recs):<8}{n_commits:<9}{value!s:<10}"
            f"{_mark(parent_ok):<10}{_mark(sha_ok):<10}{_mark(post_ok):<10}"
        )

    print()
    if fresh_consistent:
        print(f"起点一致：所有组第 1 轮违反数 = {reference}")
    if problems:
        print(f"\n❌ {len(problems)} 项不变量被破坏：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("\n✅ 全部不变量通过：每组独立从基线出发、逐轮有提交、post/ 未被碰过")
    return 0


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


if __name__ == "__main__":
    raise SystemExit(main())
