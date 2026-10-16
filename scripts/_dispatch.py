"""真机派轮：建会话 -> 逐条发消息 -> 每轮打一行结果，并把响应原文落盘备查。

用法（在 IoT-items-of-thoughts 根目录跑）：

    python scripts/_dispatch.py --config configs/chat_brush.yaml --prefix prefixes/brush_paint "第一句" "第二句"
    python scripts/_dispatch.py --sid <已有会话 id> "接着说"

每轮做四件事：
  1. POST /session/{sid}/say  —— 真跑一轮（写文件、验证、ΔC、判定）
  2. 把 SayResponse 原文写到 out/dispatch/<sid>/round-<n>.json（含全文 reply，供离线分析）
  3. 打一行摘要：判定 / 产物 / C 体量 / 耗时 / reply 前 200 字
  4. **健全性探针**：GET /health、/session/{sid}/state、/session/{sid}/turns、/sessions
     任何一个不 200 就记一行 CRASH —— 这是"聊天有没有崩"的客观记录

stdout 强制 UTF-8：GBK 控制台会把中文正文打成 UnicodeEncodeError（白花一轮钱）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8099"


def _call(method: str, path: str, body: dict | None = None, timeout: int = 900):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw, time.time() - t0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, raw, time.time() - t0
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", time.time() - t0


def health_probe(sid: str) -> list[str]:
    bad: list[str] = []
    for path in ("/health", f"/session/{sid}/state", f"/session/{sid}/turns", "/sessions"):
        status, raw, _ = _call("GET", path, timeout=60)
        if status != 200:
            bad.append(f"{path} -> {status} {raw[:200]}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("messages", nargs="+")
    ap.add_argument("--config", default="")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--sid", default="")
    ap.add_argument("--workspace", default=None)
    args = ap.parse_args()

    sid = args.sid
    if not sid:
        body = {
            "project": "brush_paint",
            "prefix_path": args.prefix,
            "config_path": args.config or None,
            "overrides": {},
        }
        if args.workspace is not None:
            body["overrides"]["workspace_path"] = args.workspace
        status, raw, dt = _call("POST", "/session/create", body)
        if status != 200:
            print(f"[create] 失败 {status}: {raw[:500]}")
            return 2
        info = json.loads(raw)
        sid = info["session_id"]
        print(
            f"[create] sid={sid} arm={info.get('arm')} history={info.get('history_mode')} "
            f"max_rounds={info.get('max_rounds')} ws={info.get('workspace')} "
            f"deliverable={info.get('workspace_is_deliverable')} ({dt:.1f}s)"
        )

    dump = ROOT / "out" / "dispatch" / sid
    dump.mkdir(parents=True, exist_ok=True)

    for i, msg in enumerate(args.messages, 1):
        print(f"\n=== [{i}/{len(args.messages)}] {msg[:80]}")
        status, raw, dt = _call("POST", f"/session/{sid}/say", {"message": msg})
        if status != 200:
            print(f"[say] HTTP {status} ({dt:.1f}s): {raw[:800]}")
            (dump / f"round-crash-{i}.txt").write_text(
                f"HTTP {status} after {dt:.1f}s\n{raw}\n", encoding="utf-8"
            )
            continue
        out = json.loads(raw)
        (dump / f"round-{i}.json").write_text(
            json.dumps({"sent": msg, "http_elapsed_s": dt, "response": out},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        reply = (out.get("reply") or "").strip().replace("\n", " ")
        print(
            f"[say] round={out.get('round_index')} verdict={out.get('verdict')} "
            f"files={out.get('files_changed')} C={out.get('c_tokens')}tok "
            f"prompt={out.get('prompt_tokens')}tok think={out.get('thinking_chars')}字 "
            f"revert={out.get('reverted')} {dt:.1f}s"
        )
        print(f"      reply: {reply[:200]}")
        bad = health_probe(sid)
        for line in bad:
            print(f"      CRASH-PROBE {line}")
        if not bad:
            print("      probe: ok (/health, /state, /turns, /sessions 全 200)")
    print(f"\n[done] sid={sid} dump={dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
