"""命令行：起 server，或者直接跑一组实验。

    pciso serve [--host 127.0.0.1] [--port 8099]
    pciso arms
    pciso run --config configs/arm_c_fact_chain.yaml [--rounds 8] [--report out/c.json]
    pciso metrics --session-id <sid>
    pciso state --session-id <sid>

`run` 就是你以后画曲线用的入口：一组配置跑一遍，把 metrics 落成 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import SessionConfig
from .models import RoundRequest, SessionCreateRequest
from .session import Session, SessionConflict, write_round_report

ROOT = Path(__file__).resolve().parents[2]


def _fmt_round(res) -> str:
    v = res.validation
    hard = [c.id for c in v.checks if c.passed is False and c.id in {"interface_contract", "import_cycles", "layer_violation", "frozen_tests", "stale_callers", "test_weakening", "parse_output"}]
    return (
        f"轮 {res.round_index + 1} 阶段 {res.stage} | {res.judge.decision:<6} | "
        f"违反数 {v.violation_count():<3} 约束 {','.join(v.violations) or '-':<18} | "
        f"C {res.c_i.est_tokens:>4}tok 事实 {len(res.c_i.active_facts()):>2} 失败 {len(res.c_i.failure_chain):>2} | "
        f"prompt≈{res.prompt_tokens_est:>6}tok | 硬违反 {','.join(hard) or '-'}"
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("pciso.api:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_arms(args: argparse.Namespace) -> int:
    for p in sorted((ROOT / "configs").glob("*.yaml")):
        cfg = SessionConfig.load(p, base_dir=ROOT)
        print(json.dumps({"config": p.name, **cfg.experiment_row()}, ensure_ascii=False))
    return 0


def _drive(session: Session, rounds: int, *, prefix: str = "") -> int:
    """跑若干轮并逐轮打印。返回实际跑了几轮。

    轮次是幂等的（`round_index` = rounds.jsonl 的行数），所以中断后续跑不会重复计费 ——
    这是七组 x 8 轮真机跑里最值钱的一条性质。
    """
    done = 0
    for _ in range(rounds):
        try:
            res = session.round(RoundRequest())
        except SessionConflict as exc:
            print(f"{prefix}[stop] {exc}")
            break
        done += 1
        print(prefix + _fmt_round(res))
    return done


def _require_credentials(cfg: SessionConfig, arm: str) -> str:
    """真实模型跑之前先确认凭据在位。

    不然会在"建完会话、跑完第 1 轮的工作模型调用"才炸 —— 那时已经产生了半截会话，
    排查起来比提前报错麻烦得多。返回空串表示没问题。
    """
    needs: list[str] = []
    if cfg.work_model.endpoint not in ("fake", "oracle"):
        needs.append(cfg.work_model.api_key_env)
    if cfg.compression.mode == "fact_chain" and cfg.compression.model not in (
        "heuristic",
        "fake",
        "oracle",
        "",
    ):
        needs.append(cfg.work_model.api_key_env)
    missing = sorted({name for name in needs if not os.environ.get(name)})
    if missing:
        return (
            f"{arm}: 缺少凭据环境变量 {missing}（compression={cfg.compression.mode}, "
            f"work_model={cfg.work_model.endpoint}）。"
            f"先导出凭据，或加 --offline 用脚本 oracle 排练。"
        )
    return ""


def cmd_run(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (ROOT / cfg_path).resolve()
    cfg = SessionConfig.load(cfg_path, base_dir=ROOT)
    if args.rounds:
        cfg.max_rounds = args.rounds
    if getattr(args, "max_tokens", 0):
        cfg.max_total_tokens = args.max_tokens
    if args.workspace:
        cfg.workspace_path = str(Path(args.workspace).resolve())
    if args.arm:
        cfg.arm = args.arm
    if args.fake_fault:
        cfg.work_model.fake_fault = args.fake_fault
    if args.offline:
        cfg.work_model.endpoint = "fake"
        cfg.compression.model = "heuristic"
        if cfg.work_model.retrieval is False and cfg.arm == "G":
            cfg.work_model.retrieval = True
        if cfg.workspace_path:
            print("[guard] 离线模式清空 workspace_path（拒绝 oracle 写交付仓库）")
            cfg.workspace_path = None
    else:
        problem = _require_credentials(cfg, str(cfg.arm))
        if problem:
            print(f"[abort] {problem}", file=sys.stderr)
            return 3

    req = SessionCreateRequest(
        project=cfg.project,
        prefix_path=cfg.prefix_path,
        config=cfg.model_dump(mode="json"),
        arm=cfg.arm,
        label=cfg.label,
    )
    session = Session.create(req, base_dir=ROOT, state_dir=args.state_dir or str(ROOT / "state"))
    meta = session.store.read_meta()
    print(f"session_id = {session.store.session_id}")
    print(f"arm={meta.get('arm')} history_mode={meta.get('history_mode')} "
          f"max_rounds={meta.get('max_rounds')} prefix_hash={meta['prefix_hash'][:16]}")
    print(f"workspace = {session.store.workspace}")

    for _ in range(cfg.max_rounds):
        try:
            res = session.round(RoundRequest())
        except SessionConflict as exc:
            print(f"[stop] {exc}")
            break
        print(_fmt_round(res))

    _report_and_summarize(session, args.report)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """续跑一个已存在的会话。

    为什么必须有：七组 x 8 轮真机跑要花二十分钟和真钱，中途挂掉（网络、限流、机器重启）
    是常态。轮次是幂等的，所以从第 k 轮接着跑不会重复计费 —— 但前提是有这条命令。
    """
    state_dir = args.state_dir or str(ROOT / "state")
    try:
        session = Session.load(args.session_id, state_dir, base_dir=ROOT)
    except KeyError as exc:
        print(f"[abort] 找不到会话：{exc}", file=sys.stderr)
        return 4
    except SessionConflict as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 5

    if getattr(args, "max_tokens", 0):
        session.cfg.max_total_tokens = args.max_tokens
        session.store.patch_meta(
            config=session.cfg.model_dump(mode="json"),
            experiment_row=session.cfg.experiment_row(),
        )

    if getattr(args, "offline", False):
        # 前几轮可能是真模型跑的。换工作模型会让同一组的轨迹混进两种模型，
        # 而"每组的模型"是实验的自变量 —— 这种污染不报错，但结论直接作废。
        print(
            "[abort] 不支持对已有会话做 --offline 续跑：换工作模型会让这一组混进两种模型",
            file=sys.stderr,
        )
        return 6
    problem = _require_credentials(session.cfg, str(session.cfg.arm))
    if problem:
        print(f"[abort] {problem}", file=sys.stderr)
        return 3
    if session.cfg.workspace_path and not session.cfg.branch_per_arm:
        print("[warn] 本会话未按组分支，续跑会继续在当前位置写", file=sys.stderr)

    done_before = session.store.round_index()
    remaining = max(0, session.cfg.max_rounds - done_before)
    if args.rounds:
        remaining = min(remaining, args.rounds)
    print(f"session_id = {session.store.session_id}")
    print(
        f"arm={session.cfg.arm} 已完成 {done_before}/{session.cfg.max_rounds} 轮，"
        f"续跑 {remaining} 轮　累计 token={session.total_tokens()}　"
        f"预算={session.cfg.max_total_tokens or '不限'}"
    )
    _drive(session, remaining)
    _report_and_summarize(session, args.report)
    return 0


def _report_and_summarize(session: Session, report: str) -> None:
    metrics = session.metrics()
    print(
        f"\n总轮数 {metrics['rounds']} 回退 {metrics['reverts']} "
        f"(回退率 {metrics['revert_rate']:.0%}) "
        f"违反总数 {metrics['violation_count_total']} "
        f"token 合计 {metrics['tokens']['total']}"
    )
    if report:
        out = Path(report)
        if not out.is_absolute():
            out = (ROOT / out).resolve()
        write_round_report(session, out)
        print(f"报告 -> {out}")
    return 0


def _git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return (proc.stdout or "").strip()


def cmd_workspace(args: argparse.Namespace) -> int:
    """检查（并可选清理）实验仓库的状态。

    为什么需要这条命令：**一次跑挂的会话照样会建出分支、并把仓库留在 arm-X 上**。
    实测就踩了两次 —— 交付仓库里残留了 4 个空分支，而且下一次 `git clone`
    直接克隆到错的分支（克隆默认检出源的 HEAD）。
    这种状态不报错，但会让后续的每一件事都建立在错的前提上。
    """
    repo = Path(args.repo).resolve()
    if not (repo / ".git").is_dir():
        print(f"[abort] {repo} 不是 git 仓库", file=sys.stderr)
        return 2

    base = args.base
    if not _git_out(repo, "rev-parse", "--verify", "--quiet", base):
        print(f"[abort] 基线分支 {base!r} 不存在", file=sys.stderr)
        return 3

    head = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = [x for x in _git_out(repo, "status", "--porcelain").splitlines() if x.strip()]
    branches = [b for b in _git_out(repo, "branch", "--format=%(refname:short)").splitlines() if b]

    print(f"仓库     : {repo}")
    print(f"当前分支 : {head}{'  ⚠ 不在基线分支上' if head != base else ''}")
    print(f"未提交   : {len(dirty)} 项")
    print("分支     :")
    empty_arms: list[str] = []
    for b in branches:
        ahead = _git_out(repo, "rev-list", "--count", f"{base}..{b}") or "0"
        mark = ""
        if b.startswith("arm-") and ahead == "0" and b != base:
            empty_arms.append(b)
            mark = "  ← 空实验分支（可清理）"
        print(f"  {b:<12} 相对 {base} 独有提交 {ahead}{mark}")

    if not args.prune:
        if empty_arms:
            print(f"\n提示：加 --prune 可删掉这些空实验分支并回到 {base}")
        return 0

    if head != base:
        _git_out(repo, "checkout", "-q", base)
        print(f"\n已切回 {base}")
    for b in empty_arms:
        _git_out(repo, "branch", "-q", "-D", b)
        print(f"已删除空分支 {b}")
    print(f"清理后 HEAD: {_git_out(repo, 'rev-parse', '--abbrev-ref', 'HEAD')}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    session = Session.load(args.session_id, args.state_dir or str(ROOT / "state"), base_dir=ROOT)
    print(json.dumps(session.metrics(), ensure_ascii=False, indent=2))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    session = Session.load(args.session_id, args.state_dir or str(ROOT / "state"), base_dir=ROOT)
    print(json.dumps(session.state_response().model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def cmd_matrix(args: argparse.Namespace) -> int:
    """跑多组对照并产出曲线。

    默认 `--offline`：把工作模型换成假 oracle、压缩器换成确定性实现，并且**强制清空
    workspace_path** —— 脚本 oracle 绝不允许写进交付仓库 `drawing-video-toolchain`。
    离线矩阵验证的是**管线与成本曲线**；违反数曲线在离线态是退化的
    （oracle 的输出与上下文无关，所以能产出文件的组得分必然相同）。
    要跑真正的对照，去掉 --offline 并配好凭据。
    """
    from .report import (
        ArmResult,
        build_panels,
        build_report,
        dump_json,
        render_csv,
        render_markdown,
        render_svg,
    )

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    patterns = args.configs.split(",") if args.configs else ["arm_*.yaml"]
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(sorted((ROOT / "configs").glob(pat.strip())))
    if not paths:
        print("没有匹配到任何配置", file=sys.stderr)
        return 2

    state_dir = str(Path(args.state_dir).resolve()) if args.state_dir else str(ROOT / "state")
    results: list[ArmResult] = []
    offline = args.offline
    workspace_override = str(Path(args.workspace).resolve()) if args.workspace else ""

    # 交付仓库的绝对路径。离线彩排**默认拒绝**写它 —— 脚本 oracle 不许碰交付物。
    deliverable = (ROOT / ".." / "drawing-video-toolchain").resolve()
    if workspace_override:
        target = Path(workspace_override)
        if target == deliverable and not args.allow_deliverable:
            print(
                f"[abort] --workspace 指向交付仓库 {deliverable}。"
                f"离线彩排不许写交付物；确实想这么做就加 --allow-deliverable。",
                file=sys.stderr,
            )
            return 5

    # 真实模型矩阵之前的前置检查。两条都必须在这里做，否则代价是十几分钟甚至一轮预算：
    #   1. 凭据：缺了要在**开跑前**中止，而不是逐组跑挂再继续（那会建出一串空会话）
    #   2. 分支：七组写同一个交付仓库时，每组必须有自己的分支并复位到基线，
    #      否则 B 组从 A 组成品接着做 —— 曲线照样漂亮，结论一条都不成立。
    if not offline:
        problems: list[str] = []
        shared: dict[str, list[str]] = {}
        for path in paths:
            probe = SessionConfig.load(path, base_dir=ROOT)
            problems.append(_require_credentials(probe, str(probe.arm)))
            if probe.workspace_path:
                shared.setdefault(str(probe.workspace_path), []).append(str(probe.arm))
                if not probe.branch_per_arm:
                    problems.append(
                        f"{probe.arm}: 共用工作区却关掉了 branch_per_arm，"
                        f"七组会互相继承成品"
                    )
        blocking = [p for p in problems if p]
        if blocking:
            for line in blocking:
                print(f"[abort] {line}", file=sys.stderr)
            return 3
        for ws, arms in shared.items():
            if len(arms) > 1:
                print(
                    f"[plan] 工作区 {ws}：{len(arms)} 组将各用一条分支 arm-<X>（从 main 复位）"
                )

    for path in paths:
        cfg = SessionConfig.load(path, base_dir=ROOT)
        if args.rounds:
            cfg.max_rounds = args.rounds
        if getattr(args, "max_tokens", 0):
            cfg.max_total_tokens = args.max_tokens
        if offline:
            cfg.work_model.endpoint = "fake"
            cfg.work_model.fake_fault = args.fake_fault or "none"
            # 离线态下压缩器一律换成确定性实现（summary 模式会退化成规则摘要），
            # 否则 E 组会去要 API key 并把整轮跑挂。
            cfg.compression.model = "heuristic"
            if cfg.work_model.retrieval is False and cfg.arm == "G":
                cfg.work_model.retrieval = True
            if workspace_override:
                # 显式给了彩排仓库：走完整的分支 + trailer 流程。
                # 这是**唯一**能离线验证"七组逐轮写同一个仓库且互不污染"的办法。
                cfg.workspace_path = workspace_override
                print(f"[rehearsal] {cfg.arm}: 写入彩排仓库 {workspace_override}（分支 arm-{cfg.arm}）")
            elif cfg.workspace_path:
                print(f"[guard] {cfg.arm}: 离线模式清空 workspace_path（拒绝 oracle 写交付仓库）")
                cfg.workspace_path = None
        elif cfg.workspace_path:
            print(f"[warn] {cfg.arm}: 将写入真实工作区 {cfg.workspace_path}")

        req = SessionCreateRequest(
            project=cfg.project,
            prefix_path=cfg.prefix_path,
            config=cfg.model_dump(mode="json"),
            arm=cfg.arm,
            label=cfg.label,
        )
        session = Session.create(req, base_dir=ROOT, state_dir=state_dir)
        rounds_done = 0
        error = ""
        try:
            for _ in range(cfg.max_rounds):
                session.round(RoundRequest())
                rounds_done += 1
        except SessionConflict as exc:
            print(f"[{cfg.arm}] 提前结束：{exc}", file=sys.stderr)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"[{cfg.arm}] 运行失败：{error}", file=sys.stderr)
        m = session.metrics()
        results.append(
            ArmResult(
                arm=str(cfg.arm or path.stem),
                config=path.name,
                label=str(cfg.label or ""),
                session_id=session.store.session_id,
                metrics=m,
                error=error,
            )
        )
        print(
            f"[{cfg.arm}] 轮数={rounds_done} 回退={m['reverts']} "
            f"违反总数={m['violation_count_total']} "
            f"prompt首/末={m['per_round'][0]['prompt_tokens_est'] if m['per_round'] else 0}"
            f"/{m['per_round'][-1]['prompt_tokens_est'] if m['per_round'] else 0} "
            f"sid={session.store.session_id}" + (f"  ERROR={error}" if error else "")
        )
        if error:
            # 轮次是幂等的，所以这一组可以从断点接着跑，**不会重复计费**。
            # 真机跑里出错时最该看到的就是这一行。
            print(
                f"[resume] {cfg.arm}: pciso resume --session-id {session.store.session_id}",
                file=sys.stderr,
            )

    title = args.title or "P/C 隔离长程编码实验 · 跨组对照"
    meta = {
        "offline": offline,
        "state_dir": state_dir,
        "rounds_override": args.rounds or None,
        "arms": [p.name for p in paths],
        "note": (
            "离线矩阵（假 oracle + 确定性压缩器）：成本曲线与遥测有效，"
            "违反数曲线退化 —— oracle 输出与上下文无关。"
            if offline
            else "真实模型矩阵。"
        ),
    }
    payload = build_report(results, title=title, meta=meta)
    dump_json(payload, out_dir / "matrix.json")
    (out_dir / "curves.csv").write_text(render_csv(results), encoding="utf-8")
    (out_dir / "matrix.md").write_text(
        render_markdown(payload["rows"], title=title) + "\n", encoding="utf-8"
    )
    (out_dir / "curves.svg").write_text(
        render_svg(build_panels(results), title=title), encoding="utf-8"
    )

    print()
    print(render_markdown(payload["rows"], title=title))
    print()
    print(f"产出目录: {out_dir}")
    for name in ("matrix.json", "matrix.md", "curves.csv", "curves.svg"):
        print(f"  - {(out_dir / name).name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pciso", description="P/C 隔离长程编码实验 server")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="起 HTTP server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8099)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("arms", help="列出实验设置表（configs/*.yaml）")
    s.set_defaults(func=cmd_arms)

    s = sub.add_parser("run", help="按配置跑一组实验")
    s.add_argument("--config", required=True)
    s.add_argument("--rounds", type=int, default=0)
    s.add_argument("--workspace", default="")
    s.add_argument("--arm", default="")
    s.add_argument("--report", default="")
    s.add_argument("--state-dir", default="")
    s.add_argument("--fake-fault", default="", help="endpoint=fake 时的定点故障注入")
    s.add_argument(
        "--offline",
        action="store_true",
        help="用脚本 oracle 排练这一组（不花钱，且拒绝写交付仓库）",
    )
    s.add_argument("--max-tokens", type=int, default=0, help="累计 token 预算，0=不限")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="续跑一个已存在的会话（轮次幂等，不重复计费）")
    s.add_argument("--session-id", required=True)
    s.add_argument("--rounds", type=int, default=0, help="最多再跑几轮，0=跑到 max_rounds")
    s.add_argument("--report", default="")
    s.add_argument("--state-dir", default="")
    s.add_argument("--max-tokens", type=int, default=0)
    s.add_argument(
        "--offline",
        action="store_true",
        help="仅用于明确确认：对已有会话做离线续跑会被拒绝",
    )
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("matrix", help="跑多组对照并产出曲线（默认离线）")
    s.add_argument("--configs", default="", help="逗号分隔的 glob，默认 arm_*.yaml")
    s.add_argument("--rounds", type=int, default=0)
    s.add_argument("--out", default="out/matrix")
    s.add_argument("--state-dir", default="")
    s.add_argument("--title", default="")
    s.add_argument("--fake-fault", default="")
    s.add_argument("--max-tokens", type=int, default=0, help="每组累计 token 预算，0=不限")
    s.add_argument(
        "--workspace",
        default="",
        help="离线彩排：把七组都写进这个仓库（走完整的分支 + trailer 流程）",
    )
    s.add_argument(
        "--allow-deliverable",
        action="store_true",
        help="明确允许把彩排写进交付仓库（默认拒绝）",
    )
    s.add_argument(
        "--online",
        dest="offline",
        action="store_false",
        help="用真实模型跑（会写 workspace_path，需凭据）",
    )
    s.set_defaults(func=cmd_matrix, offline=True)

    s = sub.add_parser("workspace", help="检查/清理实验仓库的分支与残留状态")
    s.add_argument("--repo", required=True, help="实验仓库路径")
    s.add_argument("--base", default="main", help="基线分支名")
    s.add_argument("--prune", action="store_true", help="删掉空的 arm-* 分支并回到基线")
    s.set_defaults(func=cmd_workspace)

    s = sub.add_parser("metrics", help="打印某会话的指标")
    s.add_argument("--session-id", required=True)
    s.add_argument("--state-dir", default="")
    s.set_defaults(func=cmd_metrics)

    s = sub.add_parser("state", help="打印某会话的 C 状态")
    s.add_argument("--session-id", required=True)
    s.add_argument("--state-dir", default="")
    s.set_defaults(func=cmd_state)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SessionConflict as exc:
        print(f"[conflict] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
