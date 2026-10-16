"""真机冒烟：`/v1` 代理模式到底会不会转发工具、什么时候替换上下文。

模拟 DSH 的 agent loop（它自己做工具循环，pciso 只转发）：

  1) 带 tools 发第一条 user 消息 → 上游必须真的回 `tool_calls`（工具被吞掉就没有它了）
  2) 把 assistant(tool_calls) + tool 结果回传，继续同一轮 → **不该**压缩（`c_rounds` 不涨）
  3) 再发一条**新的** user 消息 → 这时才把上一轮压成事实链（`c_rounds` 涨、rounds.jsonl 有 c_i）

用法：python scripts/_smoke_proxy_tools.py
（server 要在 8099 上跑，且它有 DEEPSEEK_API_KEY）
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

URL = "http://127.0.0.1:8099/v1/chat/completions"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文件的正文",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径"}},
                "required": ["path"],
            },
        },
    }
]
FORCE = "auto"  # 不能指定具体函数：上游 thinking 模式会 400「Thinking mode does not support this tool_choice」


def post(messages: list[dict], *, headers: dict[str, str] | None = None) -> tuple[dict, dict]:
    body = json.dumps(
        {"model": "pciso", "messages": messages, "tools": TOOLS, "tool_choice": FORCE}
    ).encode()
    req = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": "application/json", "X-PCISO-New": "1", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # 上游/服务端的错误体要看得见，否则只能猜
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:1200]}")
        raise


def show(step: str, headers: dict, out: dict) -> str:
    choice = out["choices"][0]
    message = choice["message"]
    usage = out.get("usage") or {}
    extra = out.get("x_pciso") or {}
    calls = [
        f"{c['function']['name']}({c['function']['arguments']})"
        for c in (message.get("tool_calls") or [])
    ]
    print(f"--- {step}")
    print(f"    sid={headers.get('X-PCISO-Session')} absorbed={headers.get('X-PCISO-Absorbed')} "
          f"c_rounds={extra.get('c_rounds')} c_ops={extra.get('c_ops')}")
    print(f"    tool_calls={calls or '无'}")
    print(f"    reasoning={len(message.get('reasoning_content') or '')} 字  "
          f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    text = (message.get("content") or "").strip().replace("\n", " ")
    print(f"    正文：{text[:120]!r}")
    return choice.get("finish_reason") or ""


def main() -> int:
    ask = "必须调用 read_file 工具（不要凭记忆作答）读一下 README.md，然后用一句话说你看到了什么。"
    headers, first = post([{"role": "user", "content": ask}])
    finish = show("1) 首轮：工具必须可见", headers, first)
    message = first["choices"][0]["message"]
    ok_tools = bool(message.get("tool_calls"))
    print(f"    → finish_reason={finish}  工具透传：{'PASS' if ok_tools else 'FAIL'}")
    if not ok_tools:
        return 1

    call = message["tool_calls"][0]
    history = [
        {"role": "user", "content": ask},
        {"role": "assistant", "content": message.get("content") or "", "tool_calls": message["tool_calls"]},
        {"role": "tool", "tool_call_id": call["id"], "name": "read_file",
         "content": "# README\n这是一个实验平台的说明文件。\n"},
    ]
    sid = headers.get("X-PCISO-Session", "")
    headers2, second = post(history, headers={"X-PCISO-Session": sid})
    show("2) 同一轮的工具往返：不该压缩", headers2, second)
    mid = second.json if hasattr(second, "json") else second
    rounds_mid = (mid.get("x_pciso") or {}).get("c_rounds")
    print(f"    → c_rounds={rounds_mid}（应保持 0：这一轮还没结束）")

    answer = (second["choices"][0]["message"].get("content") or "").strip()
    history.extend([
        {"role": "assistant", "content": answer},
        {"role": "user", "content": "好，那再帮我总结成一句话。"},
    ])
    headers3, third = post(history, headers={"X-PCISO-Session": sid})
    show("3) 新的 user 消息：这时才替换上下文", headers3, third)
    extra3 = third.get("x_pciso") or {}
    ok_absorb = (extra3.get("c_rounds") or 0) >= 1 and (extra3.get("c_ops") or 0) >= 1
    print(f"    → 吸收上一轮：{'PASS' if ok_absorb else 'FAIL'}（c_rounds={extra3.get('c_rounds')}）")

    print(f"\n会话 {sid}：看 state/sessions/{sid}/rounds.jsonl（c_i 就是替换掉历史的那个事实链）")
    print(f"结论：工具透传 {'PASS' if ok_tools else 'FAIL'}｜同一轮不压缩 {'PASS' if not rounds_mid else 'FAIL'}"
          f"｜换轮才压缩 {'PASS' if ok_absorb else 'FAIL'}")
    return 0 if (ok_tools and not rounds_mid and ok_absorb) else 1


if __name__ == "__main__":
    raise SystemExit(main())
