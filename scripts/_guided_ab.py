"""「有引导 vs 无引导」压缩对照：同一段 thinking，三只提示词各压一次，同一个标尺量损失。

问题（用户提）：**我们的引导（规则 + 先验结构 + 落盘清单）到底有没有用？**

三臂（同模型、同参数、同输出预算、同一份最小输出 schema）：
  guided      现行 `render.build_thinking_prompt`（先验节点 + 落盘清单 + 全套规则 + 服务端兜底说明）
  facts_only  **只给信息不给规则**：落盘清单 + 原文（用来把"信息"和"规则"分开算账）
  plain       只给原文 + 最小输出 schema（经典"把这段总结成节点"）

同一条标尺：`scripts/_think_audit.py` 的 `coverage()`（候选信息点覆盖）+ 服务端硬规则闸门
`filter_think_ops`（两边都过，公平），另记成本与字段使用（负极性 / 待定 / risk 有没有被用）。

用法：
    python scripts/_guided_ab.py [--limit 4] [--arms guided,facts_only,plain] [--index 0]
结果落 out/guided-ab/<时间戳>/：report.md + results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _think_audit import _constraint_texts, _records, _written_sources, coverage  # noqa: E402

from pciso.actnodes import COMPLETION_CLAIM_RE, filter_think_ops, is_grounded  # noqa: E402
from pciso.compressors import _to_op  # noqa: E402
from pciso.config import SessionConfig  # noqa: E402
from pciso.models import CompressedState, DeltaOp  # noqa: E402
from pciso.oparse import extract_json_object, extract_prior_heads  # noqa: E402
from pciso.render import build_thinking_prompt, est_tokens  # noqa: E402
from pciso.workmodels import ModelClient  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

# 样本：真机跑过的会话（轮 0 = 真干活那轮），工作区里还留着它写出去的文件。
SAMPLES: list[tuple[str, str]] = [
    ("087854b478ab4b0a", "think-eval9"),
    ("d8bf4b45a8db4827", "think-eval10"),
    ("5f24337359ac4c39", "think-eval12"),
    ("da5f070d64014d7b", "think-eval13"),
    ("7b3e5f7c935f49b4", "think-eval14"),
    ("a5797846d2684d17", "think-eval15"),
]

_OUT_SCHEMA = (
    '{"ops": [{"op": "ADD_FACT", "target": null, "payload": {"id": "N1", "statement": "一句话陈述", '
    '"node_kind": "think", "polarity": "+", "assertion": "settled", "why": "", "risk": "", '
    '"salience": 60, "anchor": {"path": "相对路径"}}}]}'
)


def _plain_prompt(thinking: str, landed: list[str], *, with_facts: bool) -> str:
    """最小基线：只给输出形状（不给它形状它就产不出可比的 ops），其余一概不给。"""
    parts = [
        "把下面这段模型的推理原文压缩成若干条关键节点，输出 JSON：",
        "",
        _OUT_SCHEMA,
        "",
        "只输出 JSON，不要别的话。",
    ]
    if with_facts:
        parts += ["", "这一轮真实写出去的文件（锚点只能用这些）："]
        parts += [f"- {p}" for p in landed] or ["- （本轮没有写文件）"]
    parts += ["", "=== 原文开始 ===", thinking, "=== 原文结束 ==="]
    return "\n".join(parts)


def _nodes_of(ops: list[DeltaOp]) -> list[dict]:
    out: list[dict] = []
    for op in ops:
        payload = dict(op.payload or {})
        if not str(payload.get("statement") or "").strip():
            continue
        out.append({"id": payload.get("id"), **payload})
    return out


def _measure(
    *,
    arm: str,
    thinking: str,
    res,
    ops: list[DeltaOp],
    kept: list[DeltaOp],
    stats: dict[str, int],
    nodes: list[dict],
    sources: dict[str, str],
    constraints: list[str],
) -> dict:
    raw_statements = [str((op.payload or {}).get("statement") or "") for op in ops]
    got, tot, lines = coverage(nodes, thinking, sources, constraints)
    neg = sum(1 for op in ops if str((op.payload or {}).get("polarity") or "") == "-")
    open_ = sum(1 for op in ops if str((op.payload or {}).get("assertion") or "") == "open")
    why = sum(1 for op in ops if str((op.payload or {}).get("why") or "").strip())
    risk = sum(1 for op in ops if str((op.payload or {}).get("risk") or "").strip())
    unanchored = sum(1 for op in ops if not (op.payload or {}).get("anchor"))
    claims = sum(1 for s in raw_statements if COMPLETION_CLAIM_RE.search(s))
    ungrounded = sum(1 for s in raw_statements if s.strip() and not is_grounded(s, thinking))
    kept_chars = sum(len(str((op.payload or {}).get("statement") or "")) for op in kept)
    return {
        "arm": arm,
        "prompt_tokens": res.prompt_tokens,
        "completion_tokens": res.completion_tokens,
        "cached_tokens": res.cached_tokens,
        "error": res.error,
        "ops_raw": len(ops),
        "ops_kept": len(kept),
        "stats": stats,
        "coverage": f"{got}/{tot}",
        "coverage_hit": got,
        "coverage_total": tot,
        "coverage_lines": lines,
        "negative": neg,
        "open": open_,
        "why": why,
        "risk": risk,
        "unanchored": unanchored,
        "completion_claims": claims,
        "ungrounded": ungrounded,
        "kept_chars": kept_chars,
        "kept_est_tokens": est_tokens(" ".join(str((op.payload or {}).get("statement") or "") for op in kept)),
    }


def run(samples: list[tuple[str, str]], *, arms: list[str], index: int, outdir: Path) -> int:
    cfg = SessionConfig.from_mapping(
        {
            "session": {
                "project": "general",
                "arm": "chat",
                "prefix_path": "prefixes/general",
                "chat_driven": True,
            },
            "compression": {"mode": "fact_chain", "model": "deepseek-flash", "max_tokens": 1800},
            "work_model": {"endpoint": "openai", "model": "deepseek-flash"},
        },
        base_dir=ROOT,
    )
    client = ModelClient(
        base_url=cfg.work_model.base_url or "https://api.deepseek.com/v1",
        model=cfg.work_model.model,
        api_key_env=cfg.work_model.api_key_env,
    )
    outdir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for sid, ws_name in samples:
        ws = (ROOT / ".." / ws_name).resolve()
        recs = _records(sid)
        if index >= len(recs) or not ws.exists():
            print(f"-- 跳过 {sid}（轮次或工作区缺失）")
            continue
        rec = recs[index]
        tpath = ROOT / "state" / "sessions" / sid / "thinking" / f"{index:03d}.txt"
        if not tpath.exists():
            print(f"-- 跳过 {sid}（没有 thinking 原文）")
            continue
        thinking = tpath.read_text(encoding="utf-8")
        sources = _written_sources(rec, ws)
        landed = list(sources)
        constraints = _constraint_texts(rec)
        state_here = CompressedState.model_validate(rec.get("c_i") or {})
        prior = CompressedState.model_validate(recs[index - 1].get("c_i") or {}) if index else CompressedState()
        prior_nodes, _fails = extract_prior_heads(prior)
        print(f"\n== {sid}（{ws_name}，轮 {index}）：thinking {len(thinking)} 字，落盘 {len(landed)} 个文件")

        prompts: dict[str, str] = {
            "guided": build_thinking_prompt(
                thinking=thinking,
                round_index=index,
                prior_nodes=prior_nodes,
                c_max_tokens=2000,
                out_max_tokens=1800,
                landed=landed,
                act_units=(),
            ),
            "facts_only": _plain_prompt(thinking, landed, with_facts=True),
            "plain": _plain_prompt(thinking, [], with_facts=False),
        }
        for arm in arms:
            t0 = time.time()
            res = client.chat(
                prompts[arm],
                max_tokens=1800,
                temperature=0.0,
                thinking=False,
                response_format="json_object",
            )
            raw_ops: list[DeltaOp] = []
            blob = extract_json_object(res.text)
            if res.error is None and blob:
                try:
                    data = json.loads(blob)
                    for item in (data or {}).get("ops") or []:
                        op = _to_op(item)
                        if op is not None:
                            raw_ops.append(op)
                except json.JSONDecodeError:
                    pass
            kept, stats = filter_think_ops(
                raw_ops, allowed_paths=landed, source=thinking, constraint_texts=constraints
            )
            row = _measure(
                arm=arm,
                thinking=thinking,
                res=res,
                ops=raw_ops,
                kept=kept,
                stats=stats,
                nodes=_nodes_of(kept),
                sources=sources,
                constraints=constraints,
            )
            row |= {
                "sid": sid,
                "workspace": ws_name,
                "thinking_chars": len(thinking),
                "landed": landed,
                "elapsed_s": round(time.time() - t0, 1),
                "kept_statements": [str((op.payload or {}).get("statement") or "") for op in kept],
            }
            results.append(row)
            print(
                f"   [{arm}] {row['elapsed_s']}s ops {row['ops_raw']}->{row['ops_kept']} "
                f"覆盖 {row['coverage']} 负/待定/why/risk {row['negative']}/{row['open']}/{row['why']}/{row['risk']} "
                f"完成态断言 {row['completion_claims']} tokens {row['prompt_tokens']}/{row['completion_tokens']}"
                + (f" ERROR {row['error']}" if row["error"] else "")
            )
            payload = {"state_after": state_here.model_dump(), "rows": results}
            (outdir / "results.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    _report(results, outdir)
    return 0


def _report(results: list[dict], outdir: Path) -> None:
    if not results:
        print("没有结果")
        return
    by_arm: dict[str, list[dict]] = {}
    for r in results:
        by_arm.setdefault(r["arm"], []).append(r)
    lines = ["# guided vs 无引导（同一段 thinking、同一标尺）", ""]
    lines.append("| 臂 | 覆盖 | 节点(留/产) | 负极性 | 待定 | why | risk | 完成态断言 | 无据 | 节点字数 | prompt/comp |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm, rows in by_arm.items():
        hit = sum(r["coverage_hit"] for r in rows)
        tot = sum(r["coverage_total"] for r in rows)
        lines.append(
            f"| {arm} | {hit}/{tot} | {sum(r['ops_kept'] for r in rows)}/{sum(r['ops_raw'] for r in rows)} "
            f"| {sum(r['negative'] for r in rows)} | {sum(r['open'] for r in rows)} "
            f"| {sum(r['why'] for r in rows)} | {sum(r['risk'] for r in rows)} "
            f"| {sum(r['completion_claims'] for r in rows)} | {sum(r['ungrounded'] for r in rows)} "
            f"| {sum(r['kept_chars'] for r in rows)} "
            f"| {sum(r['prompt_tokens'] for r in rows)}/{sum(r['completion_tokens'] for r in rows)} |"
        )
    lines += ["", "## 逐样本", ""]
    for sid in dict.fromkeys(r["sid"] for r in results):
        lines.append(f"### {sid}（thinking {next(r['thinking_chars'] for r in results if r['sid'] == sid)} 字）")
        for r in [x for x in results if x["sid"] == sid]:
            lines.append(
                f"- **{r['arm']}** 覆盖 {r['coverage']}、节点 {r['ops_kept']} 条、"
                f"{r['kept_chars']} 字、{r['elapsed_s']}s"
            )
            for miss in [x for x in r["coverage_lines"] if x.startswith("[✗ 缺")]:
                lines.append(f"    - {miss}")
            for stat_line in [x for x in r["coverage_lines"] if x.startswith("[P 已覆盖")]:
                lines.append(f"    - {stat_line}")
        lines.append("")
    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:14]))
    print(f"\n-> {outdir / 'report.md'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(SAMPLES))
    ap.add_argument("--arms", default="guided,facts_only,plain")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--outdir", default="")
    args = ap.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    stamp = args.outdir or time.strftime("out/guided-ab/%Y%m%d-%H%M%S")
    outdir = ROOT / stamp
    return run(SAMPLES[: args.limit], arms=arms, index=args.index, outdir=outdir)


if __name__ == "__main__":
    raise SystemExit(main())
