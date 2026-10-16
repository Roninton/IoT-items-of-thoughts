"""真机冒烟：推理编排原型（configs/chat_think.yaml，compression.scope=thinking）。

两轮：
  1. 一个简单任务（写 hh:mm:ss 小工具 + 一条测试）—— 观察 ACT 节点是否由 server 渲染出来、
     THINK 节点是否真的只留推理骨架。
  2. "先别动文件，看看你 C 里的 THINK 节点合不合理" —— 让模型**自述**。

每轮后探 /health /session/{id}/state /session/{id}/turns /sessions，把状态码记进 CRASH-PROBE
（聊天"崩没崩"要有凭据，不能靠感觉）。全部落到 out/think/<sid>/。

用法： python scripts/_smoke_think.py [每轮最大等待秒数]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # GBK 控制台会把中文正文打成 UnicodeEncodeError

BASE = "http://127.0.0.1:8099"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "think"

TURNS = [
    (
        "写一个小工具：把一个非负整数秒数格式化成 hh:mm:ss（例如 3725 -> 01:02:05）。"
        "只用标准库，放在 tools/duration.py；再配一条测试 tools/test_duration.py。"
    ),
    (
        "先不要动任何文件。看清你上下文里的事实链：标着 ⟨THINK +⟩ / ⟨THINK -⟩ 的那些是**推理节点**，"
        "标着 ⟨ACT⟩ 的是 server 记的动作。逐条判断这些 THINK 节点：哪条是废话、哪条多余、"
        "哪条说错了或者太模糊。直说，不用留面子。"
    ),
]


def call(method: str, path: str, body: dict | None = None, timeout: float = 2400.0):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")}


def probe(sid: str) -> dict:
    """"聊天没崩"的凭据：四个读端点在这一轮结束时都还能正常回。"""
    out = {}
    for path in ("/health", f"/session/{sid}/state", f"/session/{sid}/turns", "/sessions"):
        status, _ = call("GET", path, timeout=60)
        out[path] = status
    return out


def main() -> int:
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 2400.0
    status, health = call("GET", "/health")
    print(f"[health] {status} work_model_ready={health.get('work_model_ready')}")
    status, created = call(
        "POST",
        "/session/create",
        {
            "project": "general",
            "prefix_path": "prefixes/general",
            "config_path": "configs/chat_think.yaml",
            # 每次冒烟用干净工作区：复用同一个目录时上一轮的文件还在盘上，
            # 新一轮会看到"已经有了"，观察到的行为就不是从零开始的行为。
            "overrides": {"workspace_path": "../think-scratch4"},
        },
    )
    if status != 200:
        print(f"[create] 失败 {status} {created}")
        return 1
    sid = created["session_id"]
    print(f"[create] {status} sid={sid} arm={created.get('arm')} ws={created.get('workspace')}")
    dest = OUT / sid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "create.json").write_text(
        json.dumps(created, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report: list[dict] = []
    for i, message in enumerate(TURNS, start=1):
        t0 = time.time()
        status, res = call("POST", f"/session/{sid}/say", {"message": message}, timeout=budget)
        took = round(time.time() - t0, 1)
        entry: dict = {"turn": i, "status": status, "elapsed_s": took, "message": message}
        if status != 200:
            entry["error"] = res
        else:
            entry.update(
                {
                    "round_index": res.get("round_index"),
                    "verdict": res.get("verdict"),
                    "verdict_reasons": res.get("verdict_reasons"),
                    "files_changed": res.get("files_changed"),
                    "c_tokens": res.get("c_tokens"),
                    "prompt_tokens": res.get("prompt_tokens"),
                    "hidden": res.get("hidden"),
                    "reply": res.get("reply"),
                }
            )
        entry["probe"] = probe(sid)
        report.append(entry)
        (dest / f"round-{i}.json").write_text(
            json.dumps({"request": message, "response": res}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[round {i}] {status} {took}s verdict={entry.get('verdict')} "
              f"files={entry.get('files_changed')} probe={entry['probe']}")
        print(f"  reply: {(entry.get('reply') or '')[:160]}")

    # C 的真实形态（ACT / THINK 分列）+ 每轮 telemetry
    status, state = call("GET", f"/session/{sid}/state", timeout=120)
    (dest / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    frames = state.get("fact_chain") if isinstance(state, dict) else None
    if frames is None and isinstance(state, dict):
        frames = (state.get("state") or {}).get("fact_chain")
    status, turns = call("GET", f"/session/{sid}/turns", timeout=120)
    (dest / "turns.json").write_text(json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"sid={sid}", f"C 形态: {json.dumps(state, ensure_ascii=False)[:200]}"]
    if isinstance(frames, list):
        for f in frames:
            kind = f.get("node_kind") or "fact"
            pol = f.get("polarity") or ""
            lines.append(f"  [{f.get('id')}] {kind}{(' ' + pol) if pol else ''} "
                         f"sal={f.get('salience')} {f.get('statement')} 锚={f.get('anchor')}")
    # 每轮的 telemetry 摘要（think_kept / dropped / act_nodes / 压缩器用量）
    rounds_file = ROOT / "state" / "sessions" / sid / "rounds.jsonl"
    if rounds_file.exists():
        for line in rounds_file.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            tel = rec.get("telemetry") or {}
            keys = [k for k in tel if k.startswith(("think", "act_nodes", "compressor"))]
            lines.append(f"  轮{rec.get('round_index')} verdict={rec.get('judgement')} "
                         f"usage={rec.get('usage')} telemetry={ {k: tel[k] for k in keys} }")
    (dest / "SUMMARY.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:60]))
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
