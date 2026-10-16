"""把一个 smoke 会话某轮的回复正文导成 .txt（GBK 控制台打不出中文，写文件再用 read 看）。

用法： python scripts/_dump_reply.py <sid> <轮号>   # 轮号从 1 开始
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sid = sys.argv[1]
    turn = sys.argv[2] if len(sys.argv) > 2 else "1"
    src = ROOT / "out" / "think" / sid / f"round-{turn}.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    dest = src.with_suffix(".reply.txt")
    dest.write_text(str(data["response"].get("reply") or ""), encoding="utf-8")
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
