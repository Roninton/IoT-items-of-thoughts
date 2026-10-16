"""看代理模式压出来的事实链（C）：最新那个代理会话的最后一轮 c_i。

代理模式没有"轮"的语义（轮次由客户端驱动），所以 C 长什么样只能从落盘的
`state/sessions/<sid>/rounds.jsonl` 里看 —— 每来一条新的 user 消息，服务端就把上一轮
（含 tool_calls 与工具结果）压成 ΔC 追一条记录。

用法：python scripts/_inspect_proxy_c.py [sid]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"


def newest_proxy_sid() -> str:
    reg = STATE / "openai_sessions.json"
    if not reg.is_file():
        return ""
    data = json.loads(reg.read_text(encoding="utf-8"))
    rows = sorted(data.values(), key=lambda r: r.get("created_at", 0))
    for row in reversed(rows):
        sid = row.get("session_id", "")
        if (STATE / "sessions" / sid / "rounds.jsonl").is_file():
            return sid
    return ""


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else newest_proxy_sid()
    if not sid:
        print("没有代理会话")
        return 1
    rounds_path = STATE / "sessions" / sid / "rounds.jsonl"
    rounds = [json.loads(x) for x in rounds_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"会话 {sid}：{len(rounds)} 条吸收记录")
    for rec in rounds:
        state = rec.get("c_i") or {}
        usage = rec.get("usage") or {}
        print(f"\n=== 第 {rec.get('round_index')} 次吸收（kind={rec.get('kind')}）")
        print(f"  o_i {rec.get('o_i_chars', 0)} 字 → C {rec.get('c_tokens_est', 0)} tokens"
              f"（压缩器 prompt {usage.get('compressor_prompt_tokens', 0)}"
              f"/completion {usage.get('compressor_completion_tokens', 0)}）")
        print(f"  ΔC ops：{[o.get('op') for o in (rec.get('delta_c') or {}).get('ops') or []]}")
        for fact in state.get("fact_chain") or []:
            anchor = fact.get("anchor") or {}
            kind = fact.get("node_kind", "fact")
            print(f"  [{fact.get('id')} {kind}] {fact.get('statement', '')[:110]}"
                  f"  @{anchor.get('path', '')}")
        for fail in state.get("failure_chain") or []:
            print(f"  [{fail.get('id')} 失败] {fail.get('reason', '')[:100]}")
        if state.get("unresolved"):
            print(f"  未解决：{state['unresolved']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
