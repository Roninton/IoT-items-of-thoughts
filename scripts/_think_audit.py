"""压缩损失审计：**不靠模型的回忆**，直接从 thinking 原文里列候选信息点，看 C 覆盖了没有。

为什么要这个：模型的自评是探针不是依据（README §1.3）。它会记错（把原文有的说成没有、
把自己写错的当成压缩器丢的），也会笼统地说"没有严重丢失"。要判断损失能不能接受，
得有一份**逐条可核**的清单：原文里那些"高风险低字数"的信息点（未定、克制、被否的备选），
在 C 里各有没有落点。

用法：
    python scripts/_think_audit.py <sid> <workspace> [--simulate]
      sid        会话 id（state/sessions/<sid>/）
      workspace  该会话的工作区目录（用来读这一轮真写出去的文件正文）
      --simulate 用**当前代码**的规则重跑一遍标签判定与备选补充，报告改进后的覆盖
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pciso.actnodes import (  # noqa: E402
    _ALT_PAIR_RE,
    _LANDED_FALLBACK_RE,
    _NEGATIVE_CLAUSE_RE,
    _TRY_FALLBACK_RE,
    UNCERTAINTY_RE,
    _grounding_tokens,
    build_alternative_ops,
    build_dropped_import_ops,
    build_rejected_route_ops,
    filter_think_ops,
    is_p_restatement,
)
from pciso.models import CompressedState, DeltaOp  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def _constraint_texts(record: dict) -> list[str]:
    """P 的约束原文：C 里每条 ConstraintStatus 的 `text` 就是逐字来自 P 的那一份。"""
    state = CompressedState.model_validate(record.get("c_i") or {})
    return [c.text for c in state.constraints if c.text]


def _records(sid: str) -> list[dict]:
    path = ROOT / "state" / "sessions" / sid / "rounds.jsonl"
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _ascii_tokens(text: str) -> list[str]:
    from pciso.actnodes import _CONCRETE_TOKEN_RE

    return [t for t in _CONCRETE_TOKEN_RE.findall(text) if len(t) >= 2]


_GENERIC = {
    "import",
    "from",
    "return",
    "class",
    "print",
    "value",
    "name",
    "text",
    "true",
    "false",
    "none",
    "python",
    "tools",
    "tests",
    "self",
    "list",
    "dict",
    "json",
    "http",
    "string",
    "type",
}


def _strong_hit(text: str, tokens: list[str]) -> bool:
    """**两个共享具体物**或**一个够长的辨识性标识符** —— 与机制的判据一致。

    只共享一个 `from`/`import`/`tools` 就算覆盖，审计会替压缩器"虚报"（第一版就是这样：
    把 `from __future__ import annotations` 那条误判成被 N6 覆盖了）。
    """
    hits = [t for t in tokens if t.lower() not in _GENERIC and t in text]
    if len(hits) >= 2:
        return True
    return any(len(t) >= 5 for t in hits)


def _covered(
    nodes: list[dict], tokens: list[str], frag: str, *, need_open: bool = False, need_neg: bool = False
) -> str:
    """返回命中的节点 id（逗号分隔），没命中返回空串。

    有 ASCII 具体物就比具体物；纯中文的片段退回中文二元组，且要求**至少共享两段**——
    只共享一段（"依赖""测试"这种）太容易误判成覆盖。
    """
    frag_bigrams = [g for g in _grounding_tokens(frag) if not g.isascii()]
    hits: list[str] = []
    for n in nodes:
        if need_open and n.get("assertion") != "open":
            continue
        if need_neg and n.get("polarity") != "-":
            continue
        text = str(n.get("statement") or "")
        if tokens:
            ok = _strong_hit(text, tokens)
        else:
            ok = len(set(frag_bigrams) & set(_grounding_tokens(text))) >= 2
        if ok:
            hits.append(str(n.get("id")))
            continue
        # 具体物对不上时退回中文重合（≥3 段）并标"弱"：原文写 `%02d`、节点里写
        # `f"{hours:02d}"`，同一件事的字面不同，不该被算成丢失。
        if frag_bigrams and len(set(frag_bigrams) & set(_grounding_tokens(text))) >= 3:
            hits.append(f"{n.get('id')}（弱）")
    return ",".join(hits[:3])


def _candidates(
    thinking: str, sources: dict[str, str] | None = None
) -> list[tuple[str, str, list[str], bool, bool]]:
    """(类别, 原文片段, 定位 token, 要看 open, 要看负极性)

    `sources` 是这一轮真写出去的文件正文（路径 -> 内容）。它用来做**落盘判定**：
    草稿里出现过的结构如果已经在落盘文件里，那就不是"被否掉的路线"，不该按负节点找
    （真机假阳性：eval15 的 try/except 兜底导入是**被采纳**的方案，审计却按 `__route__` 记了一个 MISS）。
    """
    rows: list[tuple[str, str, list[str], bool, bool]] = []
    landed_fallback = any(_LANDED_FALLBACK_RE.search(s) for s in (sources or {}).values())
    for m in UNCERTAINTY_RE.finditer(thinking):
        window = thinking[max(0, m.start() - 160) : m.end() + 120]
        # 自问**即刻自答**的疑问不算未定：`是否需要处理非常大的 int？ Python 无问题。`
        # 它下一句就给了结论，答案以普通节点进 C（真机 eval15 的假 MISS）。
        if _RESOLVED_INLINE_RE.search(thinking[m.end() : m.end() + 40]):
            continue
        rows.append(("未定信号", window.strip().replace("\n", " ")[-90:], _ascii_tokens(window), True, False))
    for m in _NEGATIVE_CLAUSE_RE.finditer(thinking):
        line = thinking[max(0, m.start() - 60) : m.end() + 60].strip().replace("\n", " ")
        if _PLATFORM_RE.search(line):
            continue
        if _SELF_ASKED_RE.search(thinking[max(0, m.start() - 2) : m.end() + 2]):
            # 「负数需不需要处理？」这种是**自问自答的疑问**，不是被否掉的路线；
            # 它的答案会以普通节点（"非负整数，非法输入报错"）落进 C，不该按负节点找。
            continue
        rows.append(("克制/否掉", line, _ascii_tokens(line), False, True))
    for m in _ALT_PAIR_RE.finditer(thinking):
        left, right = m.group(1), m.group(2)
        # `python -m unittest X 或者 python X.py` 这种是"两种跑法"，不是"选了 A 否了 B"：
        # 有一边是 `python`/`unittest` 这类通用词就跳过（真机里它被算成一个 MISS）。
        if left.lower() in _GENERIC or right.lower() in _GENERIC:
            continue
        rows.append(("__pair__", m.group(0), [left, right], False, True))
    if not landed_fallback:
        for m in _TRY_FALLBACK_RE.finditer(thinking):
            # 第三种形态：草稿里试过又放弃的**代码结构**（真机实例：try/except 兜底导入被否）。
            # 落盘文件里已经有这个结构就跳过 —— 那是被采纳的方案，不是被否的路线。
            window = thinking[max(0, m.start() - 60) : m.end() + 60].strip().replace("\n", " ")
            rows.append(("__route__", window[-90:], [], False, True))
    return rows


# 平台格式规则（"只写跑过的 @@UNIT validate"、file: 块格式、TODO 禁令）不算**项目推理**损失：
# 它们每一轮都在 P/directive 里逐字重述，丢了也不会做错事（真机自述里模型把这条列进来过）。
_PLATFORM_RE = re.compile(
    r"@@UNIT|validate|file:|TODO|FIXME|G1|G2|G3|只做这一轮被要求"
    # 「正文里要不要写 shell 命令 / 要不要写 bash 块」是**平台 directive**（CHAT_DIRECTIVE_BODY
    # 每轮逐字在 prompt 里写着"不要写 bash 块"），不是项目推理损失（真机 eval13/eval15 的假 MISS）。
    r"|bash|shell|命令块|No shell|```"
    # 「回复里要不要写 note / 写不写叙述」是**回复格式**的取舍，不是项目推理（真机 eval13 的假 MISS）。
    r"|note|叙述里|正文里"
)

# 自问自答的疑问（"需不需要处理负数？"）不是"被否掉的路线"：它紧接着就被原文自己
# 答掉，答案以普通节点进 C。真机实例：`负数/非整数需不需要处理？要求非负整数秒数…`
# 曾被当成一个 MISS，其实 N3 就是它的结论。
_SELF_ASKED_RE = re.compile(r"需不需要|要不要|是否需要|是否要|是否应该")

# 疑问句**下一句就自答**的信号（"是否需要处理非常大的 int？ Python 无问题。"）。
# 只认明确的"不用管"措辞，别放宽成"可以"——那会把真丢的未定项一起吞掉。
_RESOLVED_INLINE_RE = re.compile(
    r"无问题|没问题|不需要|无需|不用管|不影响|不处理|可以不加|可以不写|可以拒绝|Python 自带|现成"
)


def _written_sources(record: dict, workspace: Path) -> dict[str, str]:
    tele = record.get("telemetry") or {}
    out: dict[str, str] = {}
    for rel in tele.get("files_written") or []:
        p = workspace / rel
        try:
            out[str(rel)] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def coverage(
    nodes: list[dict],
    thinking: str,
    sources: dict[str, str] | None,
    constraints: list[str],
    *,
    verbose: bool = False,
) -> tuple[int, int, list[str]]:
    """候选信息点 -> 覆盖数 / 总数 / 逐条明细。

    抽成函数是为了让**同一个标尺**既能审真机会话（`audit()`），又能给
    `scripts/_guided_ab.py` 的「有引导 vs 无引导」对照用 —— 两边的分差必须来自提示词，
    不能来自两套判据。
    """
    rows = _candidates(thinking, sources)
    covered = 0
    lines: list[str] = []
    for kind, frag, tokens, need_open, need_neg in rows:
        if is_p_restatement(frag, constraints):
            # 用户给的准入判据：从 P 推得出来的不算损失（P 每轮逐字在 prompt 里）。
            covered += 1
            lines.append(f"[P 已覆盖] {kind}: {frag[:78]}")
            continue
        if kind == "__route__":
            hit = ""
            for n in nodes:
                st = str(n.get("statement") or "")
                if "try/except" in st or "兜底导入" in st:
                    hit = f"已记被否路线 {n.get('id')}"
                    break
        elif kind == "__pair__":
            left, right = tokens
            texts = [str(n.get("statement") or "") for n in nodes]
            hit = ""
            for chosen, rejected in ((left, right), (right, left)):
                if any(rejected in t for t in texts):
                    hit = f"已记 {rejected}"
                    break
                if any(chosen in t for t in texts):
                    hit = ""  # 只记了选中的那个 = 备选仍然没进链
                    break
        else:
            hit = _covered(nodes, tokens, frag, need_open=need_open, need_neg=need_neg)
            if not hit and kind == "未定信号":
                # 原文**自问自答**的疑问（"…后来支持命名空间? 不确定。用户只要求配一条测试，
                # 不要求 discover"）结论会以**负节点**（「不新建 __init__.py」）进 C，而不是
                # ⟨待定⟩ —— 只按 open 找会误报（真机 eval15）。
                hit = _covered(nodes, tokens, frag, need_open=False, need_neg=True)
        covered += 1 if hit else 0
        lines.append(f"[{'✓' if hit else '✗ 缺'}] {kind}: {frag[:78]}  -> {hit or '—'}")
    if verbose:
        for line in lines:
            print(f"   {line}")
    return covered, len(rows), lines


def audit(sid: str, workspace: Path, *, simulate: bool) -> int:
    records = _records(sid)
    tdir = ROOT / "state" / "sessions" / sid / "thinking"
    total = covered = 0
    print(f"== {sid}：{len(records)} 轮，工作区 {workspace}")
    for i, rec in enumerate(records):
        tpath = tdir / f"{i:03d}.txt"
        if not tpath.exists():
            continue
        thinking = tpath.read_text(encoding="utf-8")
        state = CompressedState.model_validate(rec.get("c_i") or {})
        nodes = [n.model_dump() for n in state.fact_chain]
        tele = rec.get("telemetry") or {}
        constraints = _constraint_texts(rec)
        if not (tele.get("files_written") or []):
            # 评估轮 / 纯问答轮的 thinking 是"对 C 的评论"，不是项目推理 ——
            # 拿它做损失审计只会列出一堆元议论（真机实测：一轮 149 个假候选）。
            print(f"-- 轮 {i}: 本轮没写文件，跳过（评估轮不参与损失审计）")
            continue
        sources = _written_sources(rec, workspace)
        if simulate:
            prior = CompressedState.model_validate(records[i - 1].get("c_i") or {}) if i else CompressedState()
            raw_ops = [DeltaOp.model_validate(o) for o in ((rec.get("delta_c") or {}).get("ops") or [])]
            # 与 session.py 同一条流水线：准入判据（复述 P 的负节点丢掉）也要一起模拟，
            # 否则审计会以为"被丢的那条"是漏记。
            kept, stats = filter_think_ops(
                raw_ops,
                allowed_paths=list(sources),
                source=thinking,
                constraint_texts=constraints,
            )
            known_extra = " ".join(
                str(v)
                for op in raw_ops
                for v in [
                    (op.payload or {}).get("statement"),
                    (op.payload or {}).get("why"),
                    (op.payload or {}).get("risk"),
                    (op.payload or {}).get("reason"),
                ]
                if v
            )
            extra = build_alternative_ops(
                prior_state=prior, sources=sources, thinking=thinking, known_extra=known_extra
            )
            dep_extra = build_dropped_import_ops(
                prior_state=prior,
                sources=sources,
                thinking=thinking,
                known_extra=known_extra
                + " "
                + " ".join(str((op.payload or {}).get("statement") or "") for op in extra),
            )
            route_extra = build_rejected_route_ops(
                prior_state=prior,
                sources=sources,
                thinking=thinking,
                known_extra=known_extra
                + " "
                + " ".join(
                    str((op.payload or {}).get("statement") or "") for op in extra + dep_extra
                ),
            )
            nodes = nodes + [
                {"id": (op.payload or {}).get("id"), **(op.payload or {})}
                for op in list(kept) + list(extra) + list(dep_extra) + list(route_extra)
            ]
            print(
                f"-- 轮 {i}: 服务端改标签 forced_open={stats['think_forced_open']} "
                f"forced_negative={stats['think_forced_negative']} 补备选={len(extra)} "
                f"补被砍依赖={len(dep_extra)} 补被否路线={len(route_extra)} "
                f"丢复述P={stats['think_dropped_p_restated']}"
            )
            was = {
                str((op.payload or {}).get("statement") or ""): str((op.payload or {}).get("assertion") or "")
                for op in raw_ops
            }
            for op in kept:
                st = str((op.payload or {}).get("statement") or "")
                if (op.payload or {}).get("assertion") == "open" and was.get(st) != "open":
                    print(f"     ↑ 降级为待定: {st[:70]}")
        rows = _candidates(thinking, sources)
        if not rows:
            continue
        print(f"-- 轮 {i}: 候选信息点 {len(rows)} 个（thinking {len(thinking)} 字，C 节点 {len(nodes)} 条）")
        got, tot, _lines = coverage(nodes, thinking, sources, constraints, verbose=True)
        covered += got
        total += tot
    print(f"== 覆盖 {covered}/{total}")
    return 0 if total == 0 or covered == total else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("workspace")
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()
    ws = Path(args.workspace)
    if not ws.is_absolute():
        ws = (ROOT / ".." / args.workspace).resolve()
    return audit(args.sid, ws, simulate=args.simulate)


if __name__ == "__main__":
    raise SystemExit(main())
