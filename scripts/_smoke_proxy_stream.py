"""真机冒烟：DSH 走的是**流式**，代理的 SSE 里必须有 tool_calls 增量与思考。

DSH 的 agent loop 从 assistant 的 `tool-call` 块里拿工具名与参数，而流式下这些是
`delta.tool_calls[*].function.{name,arguments}` 的分片增量 —— 代理只要少拼一块，
它就看不见工具。这个脚本就是把这一条在真机上验掉。

用法：python scripts/_smoke_proxy_stream.py
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


def stream(messages: list[dict]) -> str:
    body = json.dumps(
        {
            "model": "pciso",
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()
    req = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": "application/json", "X-PCISO-New": "1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return "".join(chunk.decode("utf-8", "replace") for chunk in resp)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}")
        raise


def main() -> int:
    sse = stream([{"role": "user", "content": "必须调用 read_file 工具读 README.md，不要凭记忆回答。"}])
    payloads = [
        json.loads(line[5:].strip())
        for line in sse.splitlines()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")
    ]
    text_parts: list[str] = []
    think_parts: list[str] = []
    call_name = ""
    call_args = ""
    finish = ""
    usage: dict = {}
    for data in payloads:
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            text_parts.append(delta.get("content") or "")
            think_parts.append(delta.get("reasoning_content") or "")
            for call in delta.get("tool_calls") or []:
                fn = call.get("function") or {}
                call_name += fn.get("name") or ""
                call_args += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    text = "".join(text_parts)
    think = "".join(think_parts)
    print(f"SSE 分块 {len(payloads)} 个，收尾 [DONE]：{'[DONE]' in sse}")
    print(f"finish_reason={finish}  finish_reason 出现过 tool_calls：{'tool_calls' in sse}")
    print(f"工具调用增量：name={call_name!r} args={call_args!r}")
    print(f"思考增量 {len(think)} 字，正文增量 {len(text)} 字")
    print(f"usage：{usage}")
    ok = call_name == "read_file" and "tool_calls" in finish + sse and bool(think) and "[DONE]" in sse
    print(f"结论：{'PASS' if ok else 'FAIL'}（流式下工具调用与思考都要能被 DSH 拼出来）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
