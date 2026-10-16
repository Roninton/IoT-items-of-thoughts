"""探针：把某个会话每一轮的「判定 / 落盘 / 事实链锚点 / 约束读数」摊平。

用途是核对"事实链里的噪声"这类指控 —— 它们必须落到具体某一轮的具体字段上，
不能靠读模型的自述（自述本身也是被压缩的对象，而且会说谎）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(r"D:\_playground\workspace\IoT-items-of-thoughts\state")


def main(sid: str) -> int:
    root = STATE / "sessions" / sid
    ws = STATE / "workspaces" / sid
    rows = [
        json.loads(line)
        for line in (root / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(f"== session {sid}  rounds={len(rows)}  workspace={ws}")
    print("-- workspace 现有文件:", sorted(
        p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()
    )[:40] if ws.is_dir() else "（无）")
    for i, r in enumerate(rows):
        tel = r.get("telemetry") or {}
        c = r.get("c_i") or {}
        facts = c.get("fact_chain") or []
        cst = {x.get("id"): x for x in (c.get("constraints") or [])}
        print(f"\n--- round {i}  verdict={r.get('judgement')}  files={tel.get('files_written')}")
        print(f"    C version={c.get('version')} facts={len(facts)}")
        for f in facts:
            a = f.get("anchor") or {}
            mark = "" if a.get("resolved") else f"  <<UNRESOLVED {a.get('detail')!r}"
            print(f"      {f.get('id')} [{f.get('status')}] {(f.get('statement') or '')[:90]!r} "
                  f"@ {a.get('path')}::{a.get('symbol')}{mark}")
        for cid in ("C2", "C5", "C15"):
            x = cst.get(cid)
            if x:
                print(f"    {cid} {x.get('status')}: {str(x.get('evidence'))[:120]!r}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "46aa73d7e5b94ffa"))
