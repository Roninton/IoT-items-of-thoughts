"""只喂事实链、不给工具：让工作模型自己复盘 keyframe-tool 到哪一步了。

这是"复盘"这条验收的硬测试 —— 这次调用里**没有 tools**，它读不了盘，
答案只可能来自 P + C。用法：python scripts/_probe_recap.py <pciso-session-id>
"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

SID = sys.argv[1]
URL = "http://127.0.0.1:8099/v1/chat/completions"
ASK = (
    "不要读任何文件（这次你也没有工具）。只凭你手上已有的上下文（需求前缀 + 事实链）复盘："
    "keyframe-tool 现在有哪些模块、哪些验收已经过了、还缺什么没做。用几句话说完，别改成写代码。"
)


def main() -> int:
    body = json.dumps(
        {"model": "pciso", "messages": [{"role": "user", "content": ASK}]}, ensure_ascii=False
    ).encode()
    request = urllib.request.Request(
        URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-PCISO-Session": SID,
        },
    )
    with urllib.request.urlopen(request, timeout=900) as resp:
        out = json.loads(resp.read().decode())
    print("x_pciso:", json.dumps(out.get("x_pciso"), ensure_ascii=False))
    print("usage:", json.dumps(out.get("usage"), ensure_ascii=False))
    print("--- 它说的话 ---")
    print(out["choices"][0]["message"]["content"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
