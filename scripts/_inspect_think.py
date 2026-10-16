"""看一次会话的 ΔC 与 C 演变：模型给了哪些 op、哪些被 apply_delta 拒了、C 最后剩什么。

用法： python scripts/_inspect_think.py <sid>
（**不要**对 state/ 做全文 grep：rounds.jsonl 里存着每轮完整 prompt，一次能喷 30 万字节。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sid = sys.argv[1]
p = ROOT / "state" / "sessions" / sid / "rounds.jsonl"
if not p.exists():
    raise SystemExit(f"没有 {p}")

for line in p.read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    tel = rec.get("telemetry") or {}
    print(f"=== 轮 {rec.get('round_index')} verdict={rec.get('judgement')} "
          f"{rec.get('verdict_reasons') or ''}")
    print("  telemetry:", {k: tel[k] for k in tel if k.startswith(("think", "act", "compress"))})
    dc = rec.get("delta_c") or {}
    ops = dc.get("ops") or []
    print(f"  ΔC: model={dc.get('model')} ops={len(ops)} err={dc.get('error')} retried={dc.get('retried')}")
    for op in ops:
        pl = op.get("payload") or {}
        print(f"    {op.get('op')} target={op.get('target')} id={pl.get('id')} "
              f"kind={pl.get('node_kind')} pol={pl.get('polarity')} :: "
              f"{(pl.get('statement') or pl.get('text') or '')[:70]}")
    c = rec.get("c_i") or {}
    chain = c.get("fact_chain") or []
    print(f"  C[{len(chain)}]:", " ".join(
        f"{f['id']}{'/' + (f.get('node_kind') or 'fact')}{f.get('polarity') or ''}{'(' + f['status'] + ')' if f['status'] != 'active' else ''}"
        for f in chain))
    mr = rec.get("merge_report") or rec.get("merge") or {}
    if mr:
        print("  merge:", {k: mr[k] for k in ("applied", "rejected", "trimmed") if mr.get(k)})
