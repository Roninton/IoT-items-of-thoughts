"""一次性冒烟：按页面的方式建一个对话会话，然后真发一句话（真机一轮）。

跑完就删；不属于测试面（不进 tests/），因为它会真的调模型、真的花钱。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# Windows 中文系统 stdout 是 GBK：模型正文里一个 ↔ 就能把脚本打成 UnicodeEncodeError，
# 而那时这一轮已经真跑完了（白花钱）。跟 verify_rehearsal.py 一样的处理。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8099"


def post(url: str, obj: dict, *, timeout: int = 300):
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    status, body = post(
        f"{BASE}/session/create",
        {
            "project": "one_sentence_edit",
            "prefix_path": "prefixes/one_sentence_edit",
            "config_path": "configs/chat.yaml",
            "overrides": {"workspace_path": None},
        },
    )
    print("create:", status)
    if status != 200:
        print(body)
        return 1
    sid = body["session_id"]
    print("sid=", sid, "arm=", body["arm"], "max_rounds=", body["max_rounds"])
    print("workspace=", body["workspace"], "deliverable=", body["workspace_is_deliverable"])
    if body["workspace_is_deliverable"]:
        print("!! 指向交付仓库，中止")
        return 1

    msg = sys.argv[1] if len(sys.argv) > 1 else "你好。先不要动任何文件：说说你现在记得什么"
    started = time.time()
    status, v = post(f"{BASE}/session/{sid}/say", {"message": msg})
    print(f"say: {status}  {time.time() - started:.1f}s")
    if status != 200:
        print(v)
        return 1
    print("round=", v["round_index"], "verdict=", v["verdict"], "kind=", v["reply_kind"])
    print("files_changed=", v["files_changed"])
    print("c_tokens=", v["c_tokens"], "delta_ops=", v["hidden"]["delta_ops"],
          "thinking_chars=", v["hidden"]["thinking_chars"])
    print("--- reply ---")
    print(v["reply"])
    print("--- chat.jsonl ---")
    print(json.dumps(get(f"{BASE}/session/{sid}/chat")["turns"], ensure_ascii=False)[:400])
    print("sid 留档:", sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
