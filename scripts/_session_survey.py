"""清点 state/sessions：谁是真机跑出来的，谁是可以再生的。

为什么需要它：测试与离线矩阵都会往 state/sessions 里建会话，跑几轮之后
"哪些目录是证据、哪些是垃圾"从目录名上看不出来。判据用**累计 token**：
fake/oracle 工作模型的会话 usage 全是 0，删掉不损失任何真机证据（再跑就有）。

用法：
    python scripts/_session_survey.py [--repo .]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _rounds(path: Path) -> list[dict]:
    f = path / "rounds.jsonl"
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    args = ap.parse_args()
    root = Path(args.repo) / "state" / "sessions"

    rows = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = {}
        f = d / "session.json"
        if f.is_file():
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        cfg = meta.get("config") or meta.get("session") or {}
        rounds = _rounds(d)
        tokens = sum(
            int((r.get("usage") or {}).get("work_prompt_tokens") or 0) for r in rounds
        )
        tokens += sum(
            int((r.get("usage") or {}).get("compressor_prompt_tokens") or 0) for r in rounds
        )
        size = sum(x.stat().st_size for x in d.rglob("*") if x.is_file())
        rows.append(
            {
                "sid": d.name,
                "rounds": len(rounds),
                "arm": cfg.get("arm") or "-",
                "model": (cfg.get("work_model") or {}).get("model") or "-",
                "prefix": cfg.get("prefix_path") or "-",
                "tokens": tokens,
                "mb": size / 1024 / 1024,
                "created": str(meta.get("created_at") or "")[:19],
            }
        )

    total = sum(r["mb"] for r in rows)
    real = [r for r in rows if r["tokens"] > 0]
    fake = [r for r in rows if r["tokens"] == 0]
    print(f"{'sid':<18}{'轮':>3} {'arm':<8}{'model':<16}{'prefix':<26}{'tokens':>9} {'MB':>6}  建")
    for r in sorted(rows, key=lambda x: (x["tokens"], x["sid"])):
        print(
            f"{r['sid']:<18}{r['rounds']:>3} {r['arm']:<8}{r['model']:<16}"
            f"{r['prefix']:<26}{r['tokens']:>9} {r['mb']:>6.2f}  {r['created']}"
        )
    print(
        f"\n合计 {len(rows)} 个会话 / {total:.1f} MB；"
        f"真机（token>0）{len(real)} 个 / {sum(r['mb'] for r in real):.1f} MB；"
        f"零 token（fake/oracle/测试留下的，可再生）{len(fake)} 个 / {sum(r['mb'] for r in fake):.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
