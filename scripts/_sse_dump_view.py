"""对着看两份 SSE dump：上游发来的 vs 回给客户端的。

用法：python scripts/_sse_dump_view.py [目录]
（dump 由 env PCISO_PROXY_SSE_DUMP=1 打开，落在 out/proxy-sse/）
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[1] / "out" / "proxy-sse"


def events(raw: str) -> list[dict]:
    out: list[dict] = []
    for chunk in raw.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data: "):
            continue
        payload = chunk[6:]
        if payload == "[DONE]":
            out.append({"kind": "done"})
            continue
        try:
            out.append(json.loads(payload))
        except ValueError:
            out.append({"kind": "unparsed", "raw": payload[:120]})
    return out


def describe(row: dict) -> str:
    if "kind" in row:
        return row["kind"]
    choice = (row.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    parts = []
    for key in ("role", "content", "reasoning_content", "reasoning", "tool_calls"):
        if key in delta:
            value = delta[key]
            if isinstance(value, str) and value:
                parts.append(f"{key}={value[:36]!r}")
            elif value:
                parts.append(f"{key}={json.dumps(value, ensure_ascii=False)[:60]}")
            else:
                parts.append(f"{key}=∅")
    if choice.get("finish_reason"):
        parts.append(f"finish={choice['finish_reason']}")
    if row.get("usage"):
        parts.append("usage")
    return ", ".join(parts) or "空"


def main() -> int:
    directory = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(directory.glob("*.sse"), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"{directory} 里没有 dump —— env PCISO_PROXY_SSE_DUMP=1 开着吗")
        return 1
    for path in files[-4:]:
        rows = events(path.read_text(encoding="utf-8", errors="replace"))
        kinds = Counter(describe(r) if "choices" not in r else describe(r).split("=")[0] for r in rows)
        reasoning = [r for r in rows if "choices" in r and (r["choices"][0].get("delta") or {}).get("reasoning_content")]
        print(f"\n=== {path.name}（{path.stat().st_size}B，{len(rows)} 个事件）")
        print("  事件类型计数：", dict(kinds.most_common(8)))
        print("  reasoning 事件：", len(reasoning))
        for row in reasoning[:4]:
            text = row["choices"][0]["delta"]["reasoning_content"]
            print(f"    （{len(text)} 字）{text[:70]!r}")
        print("  前 8 个事件：", [describe(r) for r in rows[:8]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
