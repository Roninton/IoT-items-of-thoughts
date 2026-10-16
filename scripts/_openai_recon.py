"""看看 /v1 入口到底把什么交给了工作模型：注册表 + 最新会话 + 第一轮 prompt 头部。

用途是回答「DSH 那头的对话，pciso 这边收到了什么、回了什么、有没有把 DSH 的系统提示
（技能目录 / 工具说明）灌进冻结前缀的上下文里」。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"


def main() -> int:
    reg_path = STATE / "openai_sessions.json"
    if reg_path.exists():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        print(f"== openai_sessions.json：{len(reg)} 条")
        for key, val in list(reg.items())[-6:]:
            print(f"   {key} -> {json.dumps(val, ensure_ascii=False)[:220]}")
    else:
        print("== 没有 openai_sessions.json")

    sessions = STATE / "sessions"
    dirs = sorted(
        (p for p in sessions.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    print(f"== 最新会话（共 {len(dirs)}）")
    for p in dirs[:6]:
        chat = p / "chat.jsonl"
        print(
            f"   {time.strftime('%H:%M:%S', time.localtime(p.stat().st_mtime))}  "
            f"{p.name}  chat={chat.exists()}"
        )

    newest = dirs[0]
    sess = newest / "session.json"
    if sess.exists():
        cfg = json.loads(sess.read_text(encoding="utf-8"))
        print(f"== {newest.name}/session.json  arm={cfg.get('arm')} prefix={cfg.get('prefix_path')}")
    chat = newest / "chat.jsonl"
    if chat.exists():
        print(f"== {newest.name}/chat.jsonl")
        for line in chat.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            print(f"   #{row.get('round_index')} 我: {str(row.get('message'))[:70]!r}")
            print(
                f"      回: {str(row.get('reply'))[:70]!r}  verdict={row.get('verdict')} "
                f"files={row.get('files_changed')}"
            )
    rounds = newest / "rounds.jsonl"
    if rounds.exists():
        record = json.loads(rounds.read_text(encoding="utf-8").splitlines()[0])
        prompt = record.get("prompt", "") or ""
        print(f"== 第一轮 prompt 长 {len(prompt)}；头 600 字：")
        print(prompt[:600])
        print("== prompt 里的注入痕迹计数")
        for marker in ("技能", "skill", "工具", "AGENTS", "sandbox", "qqbot", "DSH", "用户原话"):
            print(f"   {marker}: {prompt.count(marker)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
