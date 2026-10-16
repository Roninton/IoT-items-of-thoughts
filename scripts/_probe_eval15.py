"""一次性核查：eval15 自评里那几条"丢了"的说法，逐条对原文与落盘文件对账。

用法：python scripts/_probe_eval15.py <sid> <workspace>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else "a5797846d2684d17"
    ws = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"D:\_playground\workspace\IoT-items-of-thoughts\experiment\think-eval15")
    tdir = ROOT / "state" / "sessions" / sid / "thinking"
    recs = [json.loads(x) for x in (ROOT / "state" / "sessions" / sid / "rounds.jsonl").read_text(encoding="utf-8").splitlines()]
    th = (tdir / "000.txt").read_text(encoding="utf-8")

    print("== C 里 N4（导入方式那条）的完整 payload")
    for n in (recs[0].get("c_i") or {}).get("fact_chain") or []:
        if n.get("id") == "N4":
            print(json.dumps(n, ensure_ascii=False, indent=2))
    print("\n== C 的【未解决】")
    for u in (recs[0].get("c_i") or {}).get("unresolved") or []:
        print(" -", u)

    print("\n== 原文里'吞掉/吞'的上下文")
    for key in ("吞", "ImportError"):
        for i, line in enumerate(th.splitlines()):
            if key in line:
                print(f"  [{key}] L{i}: {line.strip()[:220]}")
    print("\n== 原文里 3599 / 86400 / 86399 出现处")
    for i, line in enumerate(th.splitlines()):
        if any(k in line for k in ("3599", "86400")):
            print(f"  L{i}: {line.strip()[:200]}")
    print("\n== 落盘测试文件里出现的数字")
    tf = ws / "tools" / "test_duration.py"
    if tf.exists():
        text = tf.read_text(encoding="utf-8")
        print("  size", len(text.encode("utf-8")))
        for i, line in enumerate(text.splitlines()):
            if any(ch.isdigit() for ch in line):
                print(f"  L{i}: {line.strip()[:160]}")
    else:
        print("  （没有测试文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
