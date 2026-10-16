"""读 `state/sessions/<sid>/rounds.jsonl`，算「模型信不信它自己的事实链（C）」的可观测指标。

用户提的假设是：每轮用事实链 C 替换全量历史之后，工作模型可能**不认 C**，
于是重新去读 / 重新去盘点 / 把已经做完的东西再写一遍。它没有读盘通道（chat 档 retrieval=False），
所以「重新读」在这套机制里只能以三种形态露出来：

  A. 幻觉工具调用：正文里写 ```bash ls -la \n find . \n cat ...``` ——「我先摸清仓库实际状态」，
     而这些命令不会被执行，本轮就白烧；
  B. 重写已完成的东西：本轮 file: 块里的路径，前面某轮已经写过（rewrite 率）；
  C. C 说的与磁盘不符：C 里事实的 (path, symbol) 锚点在磁盘上不存在 / 磁盘上有 C 没记的文件 ——
     这种情况模型不信 C 是**对的**，要跟它分开算。

顺带算两个归因用的量：
  D. 语言漂移：正文里英文词占比（中文 P + 中文 C 却用英文答，是上下文形态问题的典型征兆）；
  E. 自我否定：ΔC 里对该轮之前 C 已有事实的 INVALIDATE / SUPERSEDE 比率（C 反复推翻自己）。

用法：
    python scripts/_trust_check.py --sid <会话 id> [--repo <仓库根>] [-v]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FILE_RE = re.compile(r"^```file:\s*(\S+)", re.M)
SHELL_FENCE_RE = re.compile(r"^```(bash|sh|shell|console|zsh|cmd|powershell)\b", re.I | re.M)
FAKE_CMD_RE = re.compile(r"^\s*(?:ls|find|cat|grep|sed|head|tail|tree|pwd|python -c)\b", re.M)
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
PATH_IN_TEXT_RE = re.compile(r"\b[\w./-]+\.py\b")

HEDGES = [
    "我先看", "先看一下", "盘点", "摸清", "确认一下", "核对", "重新给出", "重新写",
    "完整给出", "据 C", "C 里", "事实链", "inspect the current", "current state of the repo",
    "let me check", "I need to check", "actually check", "verify", "re-read", "reread",
]
DISTRUST_PROBE = ("摸清", "盘点", "我先看", "inspect the current", "current state of the repo")


def _c(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def analyse(rounds: list[dict], workspace: Path | None, verbose: bool) -> dict:
    seen: set[str] = set()
    prev_fact_ids: set[str] = set()
    rows: list[dict] = []
    totals = {
        "rounds": 0, "revert": 0, "fake_shell_rounds": 0, "rewrite_hits": 0, "files": 0,
        "missing_anchors": 0, "facts": 0, "invalidate_prior": 0, "fact_ops": 0, "unbacked": 0,
        "c_errors": 0, "c_retried": 0,
    }
    for r in rounds:
        o_i = r.get("o_i") or ""
        tel = r.get("telemetry") or {}
        files = list(dict.fromkeys(FILE_RE.findall(o_i)))
        rewrites = [f for f in files if f in seen]
        seen.update(files)

        narrative = CODE_FENCE_RE.sub(" ", o_i)
        cjk = len(CJK_RE.findall(narrative))
        en = len(EN_WORD_RE.findall(narrative))
        shell_blocks = len(SHELL_FENCE_RE.findall(o_i))
        fake_cmds = len(FAKE_CMD_RE.findall(o_i))
        hedges = {h: o_i.count(h) for h in HEDGES if h in o_i}

        c_i = r.get("c_i") or {}
        facts = c_i.get("fact_chain") or []
        ids_now = {f.get("id") for f in facts if isinstance(f, dict)}
        missing: list[str] = []
        for f in facts:
            if not isinstance(f, dict):
                continue
            anchor = f.get("anchor") or {}
            p = anchor.get("path") or ""
            if p and workspace is not None and not (workspace / p).exists():
                missing.append(f"{f.get('id')}->{p}")

        ops = (r.get("delta_c") or {}).get("ops") or []
        inval_prior = 0
        for op in ops:
            name = (op.get("op") or "").upper()
            if "INVALIDATE" in name or "SUPERSEDE" in name or "REVISE" in name:
                tgt = op.get("target")
                if tgt in prev_fact_ids:
                    inval_prior += 1

        row = {
            "r": r.get("round_index", 0) + 1,
            "judgement": r.get("judgement"),
            "reasons": r.get("judgement_reasons"),
            "files": files,
            "rewrites": rewrites,
            "shell_blocks": shell_blocks,
            "fake_cmds": fake_cmds,
            "cjk": cjk,
            "en": en,
            "en_ratio": round(en / max(cjk + en, 1), 3),
            "facts": len(facts),
            "missing_anchors": missing,
            "ops": [op.get("op") for op in ops],
            "invalidate_prior": inval_prior,
            "unbacked": len(tel.get("unbacked_claims") or []),
            "c_error": (r.get("delta_c") or {}).get("error"),
            "c_retried": bool((r.get("delta_c") or {}).get("retried")),
            "reasoning_chars": tel.get("reasoning_chars"),
            "reverted": tel.get("reverted"),
            "prompt_tokens": r.get("prompt_tokens_est"),
            "c_tokens": r.get("c_tokens_est"),
            "violations": r.get("violations"),
            "hedges": hedges,
        }
        rows.append(row)

        totals["rounds"] += 1
        totals["revert"] += 1 if r.get("judgement") != "accept" else 0
        totals["fake_shell_rounds"] += 1 if (shell_blocks or fake_cmds) else 0
        totals["rewrite_hits"] += len(rewrites)
        totals["files"] += len(files)
        totals["missing_anchors"] += len(missing)
        totals["facts"] += len(facts)
        totals["invalidate_prior"] += inval_prior
        totals["fact_ops"] += len(ops)
        totals["unbacked"] += len(tel.get("unbacked_claims") or [])
        totals["c_errors"] += 1 if row["c_error"] else 0
        totals["c_retried"] += 1 if row["c_retried"] else 0
        prev_fact_ids = ids_now

    disk_files: set[str] = set()
    if workspace is not None and workspace.is_dir():
        disk_files = {
            p.relative_to(workspace).as_posix()
            for p in workspace.rglob("*.py")
            if ".git" not in p.parts
        }
    orphans = sorted(disk_files - seen)
    return {"rows": rows, "totals": totals, "disk_files": sorted(disk_files),
            "orphans": orphans, "verbose": verbose}


def _workspace_of(repo: Path, sid: str) -> Path | None:
    """会话真正的工作区。

    优先 `meta.config.workspace_path`（相对仓库根解析）—— 配置里指到仓库外时，
    `state/workspaces/<sid>` 是不存在的，早先按它算出来的"锚点缺失/孤儿文件"全是假的。
    """
    meta_path = repo / "state" / "sessions" / sid / "session.json"
    candidates: list[Path] = []
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = str((meta.get("config") or {}).get("workspace_path") or "")
        if raw:
            p = Path(raw)
            candidates.append(p if p.is_absolute() else (repo / p).resolve())
    candidates.append(repo / "state" / "workspaces" / sid)
    for c in candidates:
        if c.is_dir():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo)
    path = repo / "state" / "sessions" / args.sid / "rounds.jsonl"
    if not path.is_file():
        print(f"没有 {path}")
        return 2
    rounds = [json.loads(x) for x in _c(path).strip().splitlines() if x.strip()]
    ws = _workspace_of(repo, args.sid)
    exists = ws is not None and ws.is_dir()
    out = analyse(rounds, ws if exists else None, args.verbose)

    print(f"会话 {args.sid}   工作区 {ws}（{'存在' if exists else '不存在'}）")
    print(f"{'轮':>2} {'判定':<6} {'写文件':>4} {'重写':>4} {'假shell':>6} {'中文':>5} {'英文':>5} "
          f"{'英文比':>6} {'C事实':>5} {'锚点缺':>5} {'ΔC操作':>6} {'ΔC异常':>6} {'无据':>4} {'思考字':>7}")
    for row in out["rows"]:
        cstat = "丢失" if row["c_error"] else ("重试" if row["c_retried"] else "-")
        print(
            f"{row['r']:>2} {row['judgement']!s:<6} {len(row['files']):>4} {len(row['rewrites']):>4} "
            f"{row['shell_blocks']:>6} {row['cjk']:>5} {row['en']:>5} {row['en_ratio']:>6} "
            f"{row['facts']:>5} {len(row['missing_anchors']):>5} {len(row['ops']):>6} "
            f"{cstat:>6} {row['unbacked']:>4} {row['reasoning_chars']!s:>7}"
        )
        if cstat != "-":
            print(f"     ΔC: {row['c_error']}")
        if args.verbose:
            if row["rewrites"]:
                print(f"     重写: {row['rewrites']}")
            if row["missing_anchors"]:
                print(f"     C 指向不存在的文件: {row['missing_anchors'][:6]}")
            if row["hedges"]:
                print(f"     不信任措辞: {row['hedges']}")
            if row["reasons"]:
                print(f"     判定理由: {row['reasons']} | 违反: {row['violations']}")

    t = out["totals"]
    print("\n=== 小结 ===")
    print(f"轮数 {t['rounds']}，revert {t['revert']}；假 shell/盘点 出现在 {t['fake_shell_rounds']} 轮")
    print(f"file: 块共 {t['files']} 个，其中 {t['rewrite_hits']} 个是**重写前面已写过的路径**"
          f"（重写率 {t['rewrite_hits'] / max(t['files'], 1):.0%}）")
    print(f"C 事实 {t['facts']} 条，锚点指向磁盘上不存在的文件 {t['missing_anchors']} 条；"
          f"ΔC 操作 {t['fact_ops']} 条，其中推翻**本轮之前**已有事实 {t['invalidate_prior']} 条；"
          f"无据声明累计 {t['unbacked']}")
    print(f"ΔC 硬失败（整轮成果丢失）{t['c_errors']} 轮；截断重试 {t['c_retried']} 轮")
    print(f"磁盘上 .py {len(out['disk_files'])} 个，从未被任何一轮 file: 块声明过的 {len(out['orphans'])} 个")
    if out["orphans"]:
        print(f"  孤儿文件（例）: {out['orphans'][:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
