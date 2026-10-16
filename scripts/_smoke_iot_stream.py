"""真机验一遍 IoT 代理：块级替换（压缩后返回）+ 问模型"知不知道自己被压过"。

用法：python scripts/_smoke_iot_stream.py
（server 要在 8099 上跑；这条路会真花 token：两次请求，各一段长思考。）
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8099/v1/chat/completions"
KEY = "smoke-iot-" + time.strftime("%H%M%S")

TASK = (
    "不要调用任何工具，也不要写文件。用中文把'跨时区解析时间戳字符串'这件事想清楚之后，"
    "给出你的判断顺序：先判什么、再判什么、哪一步最容易出错、你会怎么验。"
    "要求：结论明确，不要罗列通用建议。"
)
FEEDBACK = (
    "上一轮你的思考过程在这段对话里是以「紧凑版」呈现给我的（原文被压成了几条条目）。"
    "请回答三件事：1) 你能看出来自己的思维链被压缩过吗，依据是什么；"
    "2) 只看这份紧凑版，你还能不能把上一轮的判断接着往下推，哪里会卡住；"
    "3) 丢了什么你认为有价值的东西（举具体例子），丢对了什么。"
)


def post(messages: list[dict], *, stream: bool = True) -> tuple[dict, str]:
    body = json.dumps({"model": "pciso", "messages": messages, "stream": stream}).encode()
    request = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": "application/json", "X-PCISO-Session": KEY},
    )
    with urllib.request.urlopen(request, timeout=2400) as response:
        # 头名大小写不保证（别用 dict(response.headers)[\"X-PCISO-Session\"] 这种写法）
        headers = {k.lower(): v for k, v in response.headers.items()}
        return headers, response.read().decode()


def parse_sse(text: str) -> dict:
    out: dict = {"reasoning": "", "content": "", "usage": {}, "done": False, "events": 0}
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            out["done"] = True
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        out["events"] += 1
        if data.get("usage"):
            out["usage"] = data["usage"]
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            out["reasoning"] += delta.get("reasoning_content") or ""
            out["content"] += delta.get("content") or ""
    return out


def main() -> int:
    print(f"会话 key = {KEY}")
    started = time.time()
    headers, raw = post([{"role": "user", "content": TASK}])
    sid = headers.get("x-pciso-session", "")
    if not sid:  # 兜底：拿不到头就按最新会话目录找
        dirs = sorted(
            (ROOT / "state" / "sessions").glob("*/rounds.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        sid = dirs[0].parent.name if dirs else ""
        print("（没拿到 X-PCISO-Session 头，按最新会话目录兜底）")
    first = parse_sse(raw)
    print(f"\n== 第 1 轮（{time.time() - started:.1f}s）sid={sid} ==")
    print(f"SSE 事件 {first['events']} 条，[DONE]={first['done']}，usage={first['usage'].get('prompt_tokens')}/"
          f"{first['usage'].get('completion_tokens')}")
    print(f"\n-- 客户端看到的 thinking 块（紧凑版，{len(first['reasoning'])} 字）：\n{first['reasoning']}")
    print(f"\n-- 正文（{len(first['content'])} 字）：\n{first['content'][:600]}")

    raw_file = ROOT / "state" / "sessions" / sid / "thinking" / "000.txt"
    raw_text = raw_file.read_text(encoding="utf-8") if raw_file.exists() else ""
    print(f"\n-- 原文（落盘，{len(raw_text)} 字）：{raw_text[:200]}…")
    leak = bool(raw_text) and raw_text[:24] in raw
    print(f"\n[检查] 原文是否泄漏回客户端：{'泄漏！' if leak else '没有（块被替换了）'}")
    print(f"[检查] 原文落盘可审计：{raw_file.exists()}；压缩比："
          f"{len(first['reasoning'])}/{len(raw_text)} = "
          f"{len(first['reasoning']) / max(1, len(raw_text)):.2f}")

    rounds = ROOT / "state" / "sessions" / sid / "rounds.jsonl"
    records = (
        [json.loads(line) for line in rounds.read_text(encoding="utf-8").splitlines() if line.strip()]
        if rounds.exists()
        else []
    )
    kinds = [r.get("kind") for r in records]
    nodes = [n for r in records for n in r["c_i"]["fact_chain"]]
    print(f"[检查] C 的账：{len(records)} 条编排轮次 kind={kinds}；"
          f"节点 {[(n['id'], n['node_kind']) for n in nodes]}")

    time.sleep(1)
    history = [
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": first["content"], "reasoning_content": first["reasoning"]},
        {"role": "user", "content": FEEDBACK},
    ]
    started = time.time()
    _headers2, raw2 = post(history)
    second = parse_sse(raw2)
    print(f"\n== 第 2 轮：问它『知不知道自己的思维链被压缩了』（{time.time() - started:.1f}s）==")
    print(f"（这一轮它看到的 thinking 块 {len(second['reasoning'])} 字；正文如下）\n")
    print(second["content"])

    out_dir = ROOT / "out" / "iot-smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{sid}.md").write_text(
        f"# IoT 冒烟 {sid}\n\n## 第 1 轮\n\n### 客户端看到的紧凑 thinking\n\n{first['reasoning']}\n\n"
        f"### 原文（{len(raw_text)} 字）\n\n{raw_text}\n\n### 正文\n\n{first['content']}\n\n"
        f"## 第 2 轮：反馈\n\n{second['content']}\n",
        encoding="utf-8",
    )
    print(f"\n落盘：{out_dir / f'{sid}.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
