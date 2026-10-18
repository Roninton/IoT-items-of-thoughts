"""前缀缓存命中率：工作模型 + 压缩器，逐轮与合计。

为什么值得单独看：
- 骨架刻意设计成"P 放最前、标题与段序逐轮不变"，**就是为了让前缀缓存命中**；
  命中率不测，"设计意图有没有兑现"就只是个说法。
- 压缩开销是本方案的主要成本项（`docs/principle.md` §1.2），所以压缩器那一路也要算命中率。

用法：
    python scripts/_cache_report.py --sid <会话 id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _pct(hit: int, total: int) -> str:
    return f"{hit / total:.1%}" if total else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    args = ap.parse_args()
    path = Path(args.repo) / "state" / "sessions" / args.sid / "rounds.jsonl"
    if not path.is_file():
        print(f"没有 {path}")
        return 2
    rounds = [
        json.loads(x) for x in path.read_text(encoding="utf-8").strip().splitlines() if x.strip()
    ]

    print(f"{'轮':>2} | {'工作 prompt':>10} {'命中':>7} {'命中率':>7} | "
          f"{'压缩 prompt':>10} {'命中':>7} {'命中率':>7} | C tokens")
    tot = {"wp": 0, "wh": 0, "cp": 0, "ch": 0}
    for r in rounds:
        u = r.get("usage") or {}
        wp, wh = int(u.get("work_prompt_tokens") or 0), int(u.get("work_cached_tokens") or 0)
        cp, ch = int(u.get("compressor_prompt_tokens") or 0), int(u.get("compressor_cached_tokens") or 0)
        tot["wp"] += wp
        tot["wh"] += wh
        tot["cp"] += cp
        tot["ch"] += ch
        print(
            f"{r.get('round_index', 0) + 1:>2} | {wp:>10} {wh:>7} {_pct(wh, wp):>7} | "
            f"{cp:>10} {ch:>7} {_pct(ch, cp):>7} | {r.get('c_tokens_est')}"
        )
    print(
        f"\n合计：工作模型 {tot['wh']}/{tot['wp']} = {_pct(tot['wh'], tot['wp'])}；"
        f"压缩器 {tot['ch']}/{tot['cp']} = {_pct(tot['ch'], tot['cp'])}"
    )
    print(
        f"输入 token 总量 {tot['wp'] + tot['cp']}，其中命中缓存 {tot['wh'] + tot['ch']} "
        f"= {_pct(tot['wh'] + tot['ch'], tot['wp'] + tot['cp'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
