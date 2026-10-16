"""清掉"零 token 会话"：从没花过一次真机调用的那些 run。

判据只有一条：整个会话的 work_prompt_tokens + compressor_prompt_tokens 合计为 0。
这只可能是 fake / oracle / SMOKE 这类不真的打 API 的模型跑出来的 ——
删掉不损失任何真机证据，重跑就有。真机会话哪怕只有 1 轮也一律留着。

同时删掉同名工作区 state/workspaces/<sid>（会话没了，工作区就是无主目录）。
代码本身在 state/sessions/<sid>/snapshots/NNN.zip 里另有一份，但那也随会话一起删。

用法：
    python scripts/_purge_zero_token_sessions.py            # 只列，不动
    python scripts/_purge_zero_token_sessions.py --apply    # 真删
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _tokens(session_dir: Path) -> int:
    f = session_dir / "rounds.jsonl"
    if not f.is_file():
        return 0
    total = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            usage = (json.loads(line).get("usage") or {})
        except json.JSONDecodeError:
            continue
        total += int(usage.get("work_prompt_tokens") or 0)
        total += int(usage.get("compressor_prompt_tokens") or 0)
    return total


def _size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(x.stat().st_size for x in path.rglob("*") if x.is_file()) / 1024 / 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = Path(args.repo)
    sessions = root / "state" / "sessions"
    workspaces = root / "state" / "workspaces"

    doomed: list[tuple[str, int, float]] = []
    kept: list[tuple[str, int]] = []
    for d in sorted(sessions.iterdir()):
        if not d.is_dir():
            continue
        tok = _tokens(d)
        if tok == 0:
            doomed.append((d.name, len(_lines(d)), _size_mb(d) + _size_mb(workspaces / d.name)))
        else:
            kept.append((d.name, tok))

    for sid, rounds, mb in doomed:
        print(f"{'删' if args.apply else '待删'} {sid}  轮={rounds}  {mb:.2f} MB")
    print(f"\n零 token 会话 {len(doomed)} 个 / {sum(x[2] for x in doomed):.1f} MB")
    print(f"真机会话 {len(kept)} 个（保留）：" + ", ".join(f"{s}({t})" for s, t in kept))

    if args.apply:
        for sid, _, _ in doomed:
            shutil.rmtree(sessions / sid, ignore_errors=True)
            shutil.rmtree(workspaces / sid, ignore_errors=True)
        left = [x.name for x in sessions.iterdir() if x.is_dir()]
        print(f"\n清完：state/sessions 剩 {len(left)} 个；"
              f"state 现在 {_size_mb(root / 'state'):.1f} MB")
    return 0


def _lines(d: Path) -> list[str]:
    f = d / "rounds.jsonl"
    if not f.is_file():
        return []
    return [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
