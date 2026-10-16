"""端到端验一遍 `/v1`：包装层被剥掉、思考回传、用量是真 token。

用法：python scripts/_smoke_openai_v1.py
（要 server 已经在 8099 上跑：`python -m pciso.cli serve --port 8099`）
"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

URL = "http://127.0.0.1:8099/v1/chat/completions"
REMINDER = (
    "<system-reminder>\nA skill is a reusable set of task-specific instructions.\n"
    "Available skills: accelerator-backend, add-memory, sllm-ops.\n</system-reminder>"
)
ASK = "只回一句话：把这一轮我要求你做的事复述一遍。"


def call(messages: list[dict[str, str]], *, stream: bool = False) -> tuple[dict, str]:
    body = json.dumps({"model": "pciso", "messages": messages, "stream": stream}).encode()
    request = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": "application/json", "X-PCISO-New": "1", "X-PCISO-Session": "smoke-v1"},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        # 线上是 HTTPMessage，转成 dict 之后大小写就不再无关了 —— 统一折成小写再查。
        headers = {k.lower(): v for k, v in response.headers.items()}
        raw = response.read().decode()
    return headers, raw


def count_sessions() -> int:
    with urllib.request.urlopen(URL.replace("/v1/chat/completions", "/sessions"), timeout=60) as r:
        return len(json.loads(r.read().decode())["sessions"])


def check_noop() -> None:
    """只剩包装层的消息：应当 200 空回复、不跑轮、不建会话。"""
    before = count_sessions()
    headers, raw = call([{"role": "user", "content": REMINDER}])
    out = json.loads(raw)
    print(
        f"空轮：skipped={headers.get('x-pciso-skipped')} "
        f"content={out['choices'][0]['message']['content']!r} "
        f"prompt_tokens={out['usage']['prompt_tokens']} "
        f"没建新会话={count_sessions() == before}"
    )


def main() -> int:
    headers, raw = call(
        [
            {"role": "user", "content": REMINDER},
            {"role": "user", "content": ASK},
        ]
    )
    out = json.loads(raw)
    message = out["choices"][0]["message"]
    usage = out["usage"]
    print(f"会话：{headers.get('x-pciso-session')}  round={out['x_pciso']['round_index']}")
    print(f"用量：prompt={usage['prompt_tokens']} completion={usage['completion_tokens']} "
          f"cached={usage['prompt_tokens_details']['cached_tokens']}")
    thinking = message.get("reasoning_content") or ""
    print(f"思考：{len(thinking)} 字 —— {thinking[:80]!r}")
    print(f"正文：{message['content'][:300]!r}")
    leaks = [w for w in ("skill", "技能目录", "accelerator-backend") if w in message["content"]]
    print(f"包装层泄漏：{leaks or '无'}")
    print(f"reasoning 非空：{bool(thinking)}；completion_tokens 非零：{usage['completion_tokens'] > 0}")
    check_noop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
