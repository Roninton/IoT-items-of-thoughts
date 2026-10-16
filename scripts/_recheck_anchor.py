"""拿真机那轮的 C 复算锚点：验证"常量锚点被判成未落地"已修好。

用法： python scripts/_recheck_anchor.py <sid> <workspace> [节点id...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pciso.models import CompressedState  # noqa: E402
from pciso.session import resolve_anchors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sid = sys.argv[1]
    workspace = Path(sys.argv[2])
    want = set(sys.argv[3:])
    rounds = [
        json.loads(line)
        for line in (ROOT / "state" / "sessions" / sid / "rounds.jsonl").read_text("utf-8").splitlines()
    ]
    state = CompressedState.model_validate(rounds[-1]["c_i"])
    report = resolve_anchors(workspace, state)
    for node in state.fact_chain:
        if not want or node.id in want:
            a = node.anchor
            print(
                f"{node.id} symbol={a.symbol!r} resolved={a.resolved} detail={a.detail!r} "
                f"unanchored={node.unanchored}"
            )
    print("unresolved:", "; ".join(report["unresolved"]) or "（空）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
