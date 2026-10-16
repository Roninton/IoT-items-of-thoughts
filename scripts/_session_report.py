"""给最新那个 pciso 会话出一份简报：thinking 压缩比、工具调用、缓存命中、有没有报错。

用法：python scripts/_session_report.py [session_id | 留空取最新]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "sessions"


def load(sid: str) -> tuple[list[dict], list[dict], dict]:
    base = STATE / sid
    events = [
        json.loads(line)
        for line in (base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rounds = [
        json.loads(line)
        for line in (base / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = json.loads((base / "session.json").read_text(encoding="utf-8"))
    return events, rounds, meta


def main() -> int:
    if len(sys.argv) > 1:
        sid = sys.argv[1]
    else:
        dirs = sorted(
            [p for p in STATE.glob("*") if (p / "events.jsonl").exists()],
            key=lambda p: (p / "events.jsonl").stat().st_mtime,
        )
        sid = dirs[-1].name
    events, rounds, meta = load(sid)

    thinks = [e for e in events if e.get("kind") == "proxy_think"]
    calls = [e for e in events if e.get("kind") == "proxy_call"]
    print(f"会话 {sid}（label={meta.get('label')}）")
    print(f"  台账：{len(calls)} 次模型调用、{len(thinks)} 块思考被编排、{len(rounds)} 条轮次记录")

    raw_chars = sum(t.get("chars") or 0 for t in thinks)
    kept_chars = sum(t.get("item_chars") or 0 for t in thinks)
    kept = sum(t.get("think_kept") or 0 for t in thinks)
    acts = sum(t.get("act_nodes") or 0 for t in thinks)
    errors = [t for t in thinks if t.get("error")]
    print(
        f"  思考：原文 {raw_chars} 字 → 紧凑 {kept_chars} 字"
        f"（比 {kept_chars / raw_chars:.2f}，{kept} 条 THINK + {acts} 条 ACT）"
        if raw_chars
        else "  思考：没有可编排的块"
    )
    if errors:
        print(f"  编排报错 {len(errors)} 次：{errors[0].get('error')}")

    prompt = sum((c.get("usage") or {}).get("prompt_tokens") or 0 for c in calls)
    cached = sum(((c.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0 for c in calls)
    completion = sum((c.get("usage") or {}).get("completion_tokens") or 0 for c in calls)
    share = cached / prompt if prompt else 0
    print(f"  用量：prompt {prompt}（命中缓存 {cached} = {share:.0%}）、completion {completion}")
    tool_calls = sum(len(c.get("tool_calls") or []) for c in calls)
    print(f"  工具：{tool_calls} 次调用，最近几次 {[c.get('tool_calls') for c in calls[-4:]]}")

    blocks = [(c.get("think_blocks"), c.get("think_emit_bytes"), c.get("bytes")) for c in calls]
    print(f"  紧凑版发出位置（块数 / 发出时上游已到字节 / 总字节）：{blocks[-5:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
