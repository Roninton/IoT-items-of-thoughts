"""真机评估：**提纯后的推理节点链** vs **原始 thinking** —— 让工作模型自己判"够不够用"。

用户的初心：不让有价值的信息淹没在长 thinking 文本里（提纯），并且降低长程任务的上下文成本（降本）。
"提纯到底丢没丢要紧的东西"这个问题，只能拿原文对照着问 —— 所以：

  轮 1：简单任务（写 hh:mm:ss 工具 + 一条测试）。这一轮的 thinking 原文落盘到
        `state/sessions/<sid>/thinking/000.txt`（由 `SessionStore.write_thinking` 写）。
  轮 2：把**原文全文**塞回消息里，同一条消息里同时有"原文"和"提纯结果"（C 里那几条 ⟨THINK⟩），
        让它回答三件事：链够不够接着干、原文里哪些有价值信息没进链、哪些丢得对。

判据（不给它看，只看它说了什么）：
  - "够用" 但举不出缺失 → 说明它没真对照；
  - 举出的缺失若是**事实性**的（文件/接口/边界值）→ 那是提纯的真损失，要进 ACT 或改提示词；
  - 举出的缺失若是**语气性**的（"没写我在犹豫"）→ 属于设计上就要丢的填充。

用法： python scripts/_think_eval.py [每轮最大等待秒数]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # GBK 控制台打不了中文

BASE = "http://127.0.0.1:8099"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "think"

TASK = (
    "写一个小工具：把一个非负整数秒数格式化成 hh:mm:ss（例如 3725 -> 01:02:05）。"
    "只用标准库，放在 tools/duration.py；再配一条测试 tools/test_duration.py。"
)

EVAL_TMPL = """这一轮不要动任何文件，只回答一个问题 —— 我们要的是你的**判断**，不是代码。

下面这段是你上一轮（第 1 轮）的 thinking 原文全文，{raw_chars} 字。
你的上下文里现在有 {nodes} 条 ⟨THINK⟩ 节点和 {acts} 条 ⟨ACT⟩ 节点 ——
那是服务端的"推理编排器"从这段原文里**提纯**出来的结果（原文本身不进你的上下文）。

请对照原文回答四件事。直说，不用留面子，说"够用"要有理由、说"丢了"要举具体的：

1. **够不够**：只按现在这几条节点（不看下面的原文），你能不能接着把这个活正确做下去？
   如果做不下去，具体缺哪一条信息才会卡住？
2. **丢了什么**：原文里有哪些**有价值**的信息没有进节点？逐条列，并说明为什么它值钱
   （会让你下一轮走错、重做、或踩同一个坑）。没有就直说没有。
3. **丢对了什么**：哪些内容被丢掉是对的（填充 / 自我复述 / 来回犹豫 / 复述任务）？
   粗略估一下这部分占原文多大比例。
4. **提纯与降本**：我们的目标是"不让有价值信息淹没在长 thinking 里（提纯）"+
   "降低长程任务的上下文成本（降本）"。按这个目标，这条链做到了几分（0-10）？缺的那部分是
   提示词能修的，还是机制（谁来决定留什么）的问题？

两条针对性的追问，请分别回答"原文里有没有 / 有的话进节点了吗"：

A. 原文里有没有**"决定不做某件事"的克制**（不新建某个文件、不引某个依赖、不改某个既有文件、
   不做某类优化）？如果有，它们进节点了吗？节点的语气读起来像不像一条明确的约束？
B. 原文里有没有**"还没定下来"的摇摆**（还在权衡、只是先试试、等验证再说）？如果有，
   节点里是怎么呈现的 —— 读起来是"已经定了"还是"待定"？

判断口径：**只算"推理骨架"这一层**（你做的决定 / 依据 / 否掉的路线 / 还没定的地方）。
P（冻结需求）与平台约束每轮都原封不动在你的上下文里，文件内容在盘上，
两者都不算"节点该装而没装"的东西 —— 不要把它们记成损失。

=== thinking 原文开始 ===
{raw}
=== thinking 原文结束 ===
"""


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


def node_lines(state: dict) -> list[str]:
    frames = state.get("fact_chain") or []
    out = []
    for f in frames:
        kind = f.get("node_kind") or "fact"
        pol = f.get("polarity") or ""
        pending = " 待定" if (f.get("assertion") or "") == "open" else ""
        out.append(
            f"  [{f.get('id')}] {kind}{(' ' + pol) if pol else ''}{pending} "
            f"sal={f.get('salience')} {f.get('statement')}"
        )
    return out


def main() -> int:
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 2400.0
    # 每次评估换一个干净工作区（argv[2]，默认 think-eval3）：复用旧目录会看到
    # "文件已经在盘上"，观察到的就不是从零开始的行为。
    # 默认落在仓库里的 experiment/（已 gitignore）；给绝对路径就按给的来。
    ws = sys.argv[2] if len(sys.argv) > 2 else "think-eval3"
    ws_path = ws if (ws.startswith("experiment") or ":" in ws) else f"experiment/{ws}"
    status, health = call("GET", "/health")
    print(f"[health] {status} work_model_ready={health.get('work_model_ready')}")
    status, created = call(
        "POST",
        "/session/create",
        {
            "project": "general",
            "prefix_path": "prefixes/general",
            "config_path": "configs/chat_think.yaml",
            "overrides": {"workspace_path": ws_path},
        },
    )
    if status != 200:
        print(f"[create] 失败 {status} {created}")
        return 1
    sid = created["session_id"]
    print(f"[create] {status} sid={sid} ws={created.get('workspace')}")
    dest = OUT / sid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "create.json").write_text(
        json.dumps(created, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 轮 1：干活
    t0 = time.time()
    status, res1 = call("POST", f"/session/{sid}/say", {"message": TASK}, timeout=budget)
    took1 = round(time.time() - t0, 1)
    print(f"[round 1] {status} {took1}s verdict={res1.get('verdict')} files={res1.get('files_changed')}")
    (dest / "round-1.json").write_text(
        json.dumps({"request": TASK, "response": res1}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if status != 200:
        print(f"[round 1] 失败：{res1}")
        return 1

    raw_path = ROOT / "state" / "sessions" / sid / "thinking" / "000.txt"
    raw = raw_path.read_text(encoding="utf-8") if raw_path.exists() else ""
    if not raw.strip():
        print(f"[abort] 没拿到 thinking 原文（{raw_path}）—— 评估做不了，先查落盘")
        return 1

    status, state = call("GET", f"/session/{sid}/state", timeout=120)
    (dest / "state-after-1.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    frames = state.get("fact_chain") or []
    thinks = [f for f in frames if (f.get("node_kind") or "") == "think"]
    acts = [f for f in frames if (f.get("node_kind") or "") == "act"]
    node_chars = sum(
        len(str(f.get("statement") or "")) + len(str(f.get("why") or "")) for f in frames
    )
    print(f"[raw] thinking {len(raw)} 字；C 节点 {len(thinks)} THINK + {len(acts)} ACT，共 {node_chars} 字")

    # 轮 2：拿原文对照着问
    message = EVAL_TMPL.format(
        raw=raw, raw_chars=len(raw), nodes=len(thinks), acts=len(acts)
    )
    t0 = time.time()
    status, res2 = call("POST", f"/session/{sid}/say", {"message": message}, timeout=budget)
    took2 = round(time.time() - t0, 1)
    print(f"[round 2] {status} {took2}s verdict={res2.get('verdict')}")
    (dest / "round-2.json").write_text(
        json.dumps({"request": message, "response": res2}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest / "round-2.reply.txt").write_text(str(res2.get("reply") or ""), encoding="utf-8")
    (dest / "round-1.thinking.txt").write_text(raw, encoding="utf-8")

    lines = [
        f"sid={sid}",
        f"thinking 原文 {len(raw)} 字；C 提纯后 {len(thinks)} THINK + {len(acts)} ACT = {node_chars} 字",
        f"压缩比（节点字/原文字）≈ {node_chars / max(len(raw), 1):.2f}",
        *node_lines(state),
    ]
    rounds_file = ROOT / "state" / "sessions" / sid / "rounds.jsonl"
    if rounds_file.exists():
        for line in rounds_file.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            tel = rec.get("telemetry") or {}
            keys = [k for k in tel if k.startswith(("think", "act_nodes", "compressor", "work"))]
            lines.append(
                f"  轮{rec.get('round_index')} verdict={rec.get('judgement')} "
                f"usage={rec.get('usage')} telemetry={ {k: tel[k] for k in keys} }"
            )
    lines.append(f"probe={probe(sid)}")
    (dest / "SUMMARY.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[已就绪] 模型自述： out/think/{sid}/round-2.reply.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
