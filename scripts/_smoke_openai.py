"""真机冒烟：`/v1/chat/completions` 端到端跑一遍（真花钱，一轮一次调用）。

验三件事，都是"协议无状态 vs pciso 有状态"那条接缝上的：
  1. 两个不同的客户端会话 -> 两个不同的 pciso 会话；
  2. 把回传的 `X-PCISO-Session` 原样发回来 -> 接着同一个会话（轮次递增）；
  3. 每一轮的回复非空、且 `x_pciso` 里带得出轮次/判定/文件变更。

跑完把两个冒烟会话删掉（它是运行数据，不是实验轨迹）。证据留在 out/smoke_openai.json。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8099"
OUT = Path(__file__).resolve().parents[1] / "out" / "smoke_openai.json"
PROMPT = "只回一句话，不要动任何文件、不要写 file 块：你现在的循环状态 C 里有什么？"


def call(message: str, *, session: str = "", model: str = "pciso") -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": message}]}).encode(
        "utf-8"
    )
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if session:
        headers["X-PCISO-Session"] = session
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, headers=headers)
    started = time.time()
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return {
            "sid": resp.headers.get("X-PCISO-Session"),
            "created": resp.headers.get("X-PCISO-Session-Created"),
            "prefix": resp.headers.get("X-PCISO-Prefix"),
            "elapsed_s": round(time.time() - started, 1),
            "content": (data["choices"][0]["message"]["content"] or "")[:400],
            "x_pciso": data.get("x_pciso"),
            "usage": data.get("usage"),
        }


def delete(sid: str) -> int:
    req = urllib.request.Request(f"{BASE}/session/{sid}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    log: dict[str, object] = {}
    a = call(PROMPT, session="smoke-a")
    b = call(PROMPT, session="smoke-b")
    c = call("只回一句话，不要动任何文件：我上一条问的是什么？", session=str(a["sid"]))
    log = {"first": a, "second_client": b, "echoed_sid": c}
    checks = {
        "两个客户端会话 -> 两个 pciso 会话": a["sid"] != b["sid"],
        "回传 sid 接着同一个会话": c["sid"] == a["sid"],
        "轮次递增 0 -> 1": a["x_pciso"]["round_index"] == 0 and c["x_pciso"]["round_index"] == 1,
        "回复都非空": all(x["content"].strip() for x in (a, b, c)),
        "前缀是默认 general": a["prefix"] == "general",
    }
    log["checks"] = checks
    print(json.dumps(log, ensure_ascii=False, indent=2))
    print("---- checks ----")
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    ok = all(checks.values())
    # 冒烟会话是运行数据，不是实验轨迹：留证据，删本体
    print("deleted:", {str(s["sid"]): delete(str(s["sid"])) for s in (a, b, c)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
