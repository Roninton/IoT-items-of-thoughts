"""外部合并器 + 改进判定器。

两个设计要点（都是对原方案的收紧）：

1. 合并器必须是**确定性代码**，所以"更正传播"不能靠它理解语义。
   语义判断被前移到压缩器的**结构化操作**里（SUPERSEDE / INVALIDATE），
   合并器只做机械应用 + 依赖图上的级联标记。这是原方案里"更正沿依赖传播"能落地的唯一方式。
2. 判定器采用 accept/revert：硬约束回归就回退工作区。
   于是核心指标从"累积违反数"升级为**回退率** —— 更客观、跨组可比，
   而且直接测量"这套配置能不能把模型摁在轨上"。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .config import SessionConfig
from .models import (
    HARD_CHECK_IDS,
    CompressedState,
    DeltaC,
    DeltaOp,
    DependencyEdge,
    FactNode,
    FailureEntry,
    MergeReport,
    RoundJudgement,
    ValidationResult,
)

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def find_cycles(edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """在模块依赖图上找环（DFS 三色法）。返回去重后的环列表。"""
    graph: dict[str, set[str]] = {}
    for src, dst in edges:
        graph.setdefault(src, set()).add(dst)
        graph.setdefault(dst, set())

    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def dfs(node: str) -> None:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt, WHITE) == WHITE:
                dfs(nxt)
            elif color.get(nxt) == GREY and nxt in stack:
                cyc = stack[stack.index(nxt) :]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(cyc))
        stack.pop()
        color[node] = BLACK

    for n in sorted(graph):
        if color[n] == WHITE:
            dfs(n)
    return cycles


def dependents_of(state: CompressedState, node_id: str) -> list[str]:
    """直接 + 传递依赖该节点的事实节点。"""
    direct = {f.id for f in state.fact_chain if node_id in f.deps}
    out = set(direct)
    frontier = list(direct)
    while frontier:
        cur = frontier.pop()
        for f in state.fact_chain:
            if cur in f.deps and f.id not in out:
                out.add(f.id)
                frontier.append(f.id)
    return sorted(out)


# --------------------------------------------------------------------------- #
# 合并器
# --------------------------------------------------------------------------- #


def apply_delta(
    prev: CompressedState,
    delta: DeltaC,
    *,
    round_index: int,
    c_max_tokens: int,
    constraint_texts: dict[str, str],
    est_tokens_fn,
) -> tuple[CompressedState, MergeReport]:
    state = prev.model_copy(deep=True)
    report = MergeReport()

    for op in delta.ops:
        try:
            _apply_op(state, op, round_index, report, constraint_texts)
        except Exception as exc:
            report.rejected.append(f"{op.op}:{op.target or '-'} -> {exc}")

    # ---- 依赖图完整性 -------------------------------------------------- #
    known = {e.src for e in state.deps} | {e.dst for e in state.deps}
    for e in state.deps:
        for side in (e.src, e.dst):
            if side not in known:  # pragma: no cover - known 由自身构造
                report.dangling_deps.append(side)

    module_set = {f"{e.src}->{e.dst}" for e in state.deps}
    cycles = find_cycles({(e.src, e.dst) for e in state.deps})
    if cycles:
        report.cycles.extend(cycles)
        report.conflicts.extend(f"循环依赖: {' -> '.join(c)}" for c in cycles)
    del module_set

    # ---- 事实链自检 ---------------------------------------------------- #
    ids = {f.id for f in state.fact_chain}
    for f in state.fact_chain:
        for dep in f.deps:
            if dep not in ids:
                report.dangling_deps.append(f"{f.id}->{dep}")
                report.conflicts.append(f"{f.id} 依赖不存在的事实节点 {dep}")
        if f.status == "superseded" and not f.superseded_by:
            report.conflicts.append(f"{f.id} 被取代但未记录 superseded_by")

    # ---- 预算截断（记录截断事件，而不是静默丢弃） ---------------------- #
    #
    # `c_max_tokens=0` 表示**这一路不做预算裁剪**（IoT 编排路径就是这样：一个 thinking
    # 块一次编排，C 的规模由"块数 × 每块几条"天然决定）。之前 `est > 0` 就必然触发裁剪，
    # 于是 0 会被理解成"预算为零"，把每一条事实当场裁掉 —— 实测就是这样把事实链裁空的
    # （`trimmed: N3(budget, salience=85)`，连显著度 85 的事实都没保住）。
    state.version = round_index
    state.est_tokens = est_tokens_fn(state)
    if c_max_tokens > 0 and state.est_tokens > c_max_tokens:
        report.trimmed = _trim_to_budget(state, c_max_tokens, est_tokens_fn)
    # truncated 的语义是"发生过截断"（=压缩损耗已发生），不是"此刻仍超预算"
    state.truncated = bool(report.trimmed) or (c_max_tokens > 0 and state.est_tokens > c_max_tokens)

    return state, report


def _find_restatement(
    state: CompressedState, statement: str, anchor_raw: dict | None, node_kind: str = "fact"
) -> str | None:
    """同一陈述，或同一 (path, symbol) 且签名一致 —— 视为复述，返回既有节点 id。

    **只在同一 node_kind 内比较。** ACT（server 记的物证）与 THINK（模型记的"为什么"）
    天然挂在同一个路径上，它们不是彼此的复述；混着比会把 ACT 节点当重复吞掉，
    于是"这一轮真的写了这个文件"从 C 里消失 —— 那正是这套机制要根治的那件事。

    **按锚点去重只对 `fact` 生效。** THINK 节点的身份是**它那句话说了什么**，不是它挂在哪个文件上：
    同一个文件上并列三条独立推理（含一条"否掉 X"）是完全正常的。真机实测（会话 910b395b69da4180）：
    一轮里 3 条正节点只活下来 2 条，另一轮里 4 条自我评估节点（含 2 条负节点）**全被吞**，
    C 上一轮之后什么都没变 —— 而负节点正是最贵的那一类（它拦住"重走已否掉的路"）。
    """
    stmt_norm = normalize_text(statement)
    sym = None
    if isinstance(anchor_raw, dict):
        sym = f"{anchor_raw.get('path', '')}::{anchor_raw.get('symbol') or ''}"
    for f in state.fact_chain:
        if (f.node_kind or "fact") != node_kind:
            continue
        if stmt_norm and normalize_text(f.statement) == stmt_norm:
            return f.id
        if (
            node_kind == "fact"
            and sym
            and f.anchor is not None
            and f.anchor.key() == sym
            and (f.anchor.signature or "") == str((anchor_raw or {}).get("signature") or "")
        ):
            return f.id
    return None


def _apply_op(
    state: CompressedState,
    op: DeltaOp,
    round_index: int,
    report: MergeReport,
    constraint_texts: dict[str, str],
) -> None:
    kind = op.op
    payload = op.payload or {}

    if kind == "ADD_FACT":
        fid = str(payload.get("id") or state.next_fact_id())
        if state.fact(fid):
            report.rejected.append(f"ADD_FACT:{fid} 已存在")
            return
        statement = str(payload.get("statement") or payload.get("text") or "")
        anchor_raw = payload.get("anchor")
        # 去重：同一陈述或同一锚点被再次断言 = 旧事实的复述，不新增节点。
        # 否则每轮重写同一个文件都会灌进一批重复节点，C 的 token 预算会假性爆掉。
        restated = _find_restatement(
            state, statement, anchor_raw, str(payload.get("node_kind") or "fact")
        )
        if restated is not None:
            report.applied.append(f"ADD_FACT(restated):{restated}")
            return
        node = FactNode(
            id=fid,
            statement=statement,
            deps=[str(d) for d in (payload.get("deps") or [])],
            source_round=round_index,
            unanchored=not anchor_raw,
            salience=_clamp_salience(payload.get("salience", 50)),
            node_kind=str(payload.get("node_kind") or "fact"),
            polarity=str(payload.get("polarity") or ""),
            # 未定 / 已定：真机自述的"过度确定"（把"要不要测 True"的犹豫写成"覆盖 True"）。
            # FactNode 的 validator 只认 `open`，其余一律 settled。
            assertion=str(payload.get("assertion") or "settled"),
            # 依据与风险也是节点的一部分：只有 statement 的链，下一轮"知道结论、不知道理由"。
            why=str(payload.get("why") or ""),
            risk=str(payload.get("risk") or ""),
        )
        if anchor_raw:
            from .models import Anchor

            try:
                node.anchor = Anchor(**anchor_raw) if isinstance(anchor_raw, dict) else None
            except Exception:
                node.anchor = None
                node.unanchored = True
        if not node.statement:
            report.rejected.append(f"ADD_FACT:{fid} 缺 statement")
            return
        state.fact_chain.append(node)
        report.applied.append(f"ADD_FACT:{fid}")

    elif kind in ("SUPERSEDE_FACT", "INVALIDATE_FACT"):
        target = str(op.target or payload.get("id") or "")
        node = state.fact(target)
        if node is None:
            report.rejected.append(f"{kind}:{target} 目标不存在")
            return
        cascades = dependents_of(state, target)
        if kind == "SUPERSEDE_FACT":
            new_id = str(payload.get("id") or state.next_fact_id())
            replacement = payload.get("statement")
            node.status = "superseded"
            node.superseded_by = new_id
            if replacement:
                state.fact_chain.append(
                    FactNode(
                        id=new_id,
                        statement=str(replacement),
                        deps=[str(d) for d in (payload.get("deps") or [])],
                        source_round=round_index,
                        unanchored=not payload.get("anchor"),
                        # salience / 节点类别必须一起继承：丢掉它们会让"旧动作被新动作取代"
                        # 这条老化规则每次把节点降级成默认显著度 50、类别 fact ——
                        # 于是 ACT 节点在裁剪排序里与普通事实同档，刚写的文件先被裁掉。
                        salience=_clamp_salience(payload.get("salience", node.salience)),
                        node_kind=str(payload.get("node_kind") or node.node_kind),
                        polarity=str(payload.get("polarity") or node.polarity),
                        assertion=str(payload.get("assertion") or node.assertion),
                        why=str(payload.get("why") or node.why),
                        risk=str(payload.get("risk") or node.risk),
                    )
                )
            report.applied.append(f"SUPERSEDE_FACT:{target}->{new_id}")
        else:
            node.status = "invalidated"
            report.applied.append(f"INVALIDATE_FACT:{target}")
        for cid in cascades:
            report.correction_cascades.append(f"{target} ⇒ {cid}")

    elif kind == "ADD_FAILURE":
        sig_attempt = normalize_text(str(payload.get("attempt") or ""))
        sig_reason = normalize_text(str(payload.get("reason") or ""))
        if not sig_attempt or not sig_reason:
            report.rejected.append("ADD_FAILURE 缺 attempt/reason")
            return
        dup = next(
            (
                f
                for f in state.failure_chain
                if normalize_text(f.attempt) == sig_attempt
                and normalize_text(f.reason) == sig_reason
            ),
            None,
        )
        if dup is not None:
            dup.round_index_seen.append(round_index)
            report.duplicate_attempts.append(f"{dup.id} 在轮 {round_index} 被重复尝试")
            report.applied.append(f"ADD_FAILURE(dup):{dup.id}")
            return
        fid = str(payload.get("id") or state.next_failure_id())
        if any(f.id == fid for f in state.failure_chain):
            fid = state.next_failure_id()
        state.failure_chain.append(
            FailureEntry(
                id=fid,
                attempt=str(payload["attempt"]),
                reason=str(payload["reason"]),
                # 来源必须带下去：server 客观判定的（例如"写 ../escape.py 被拒"）
                # 与提取器从 O_i 读出来的，不该混成同一锅。
                source=str(payload.get("source") or "extractor"),
                round=round_index,
                round_index_seen=[round_index],
            )
        )
        report.applied.append(f"ADD_FAILURE:{fid}")

    elif kind == "SET_PROGRESS":
        state.progress = str(payload.get("text") or "")
        report.applied.append("SET_PROGRESS")

    elif kind == "SET_UNRESOLVED":
        state.unresolved = [str(x) for x in (payload.get("items") or [])][:3]
        report.applied.append("SET_UNRESOLVED")

    elif kind == "SET_CONSTRAINT":
        cid = str(payload.get("id") or op.target or "")
        cur = next((c for c in state.constraints if c.id == cid), None)
        if cur is None:
            report.rejected.append(f"SET_CONSTRAINT:{cid} 不在 P 的约束清单里")
            return
        new_text = payload.get("text")
        if new_text is not None and str(new_text) != constraint_texts.get(cid, cur.text):
            report.constraint_text_mutations.append(
                f"{cid} 试图改写约束原文（禁止）"
            )
            return
        status = str(payload.get("status") or "unverified")
        if status not in ("satisfied", "partial", "violated", "unverified"):
            report.rejected.append(f"SET_CONSTRAINT:{cid} 非法 status {status!r}")
            return
        cur.status = status  # type: ignore[assignment]
        cur.evidence = str(payload.get("evidence") or "")
        if status == "violated" and round_index not in cur.violated_rounds:
            cur.violated_rounds.append(round_index)
        report.applied.append(f"SET_CONSTRAINT:{cid}={status}")

    elif kind == "ADD_DEP":
        src, dst = str(payload.get("src") or ""), str(payload.get("dst") or "")
        if not src or not dst:
            report.rejected.append("ADD_DEP 缺 src/dst")
            return
        if src == dst:
            report.rejected.append(f"ADD_DEP 自依赖 {src}")
            return
        edge = DependencyEdge(src=src, dst=dst, kind=str(payload.get("kind") or "import"))
        if edge not in state.deps:
            state.deps.append(edge)
        report.applied.append(f"ADD_DEP:{src}->{dst}")

    elif kind == "DROP_DEP":
        src, dst = str(payload.get("src") or ""), str(payload.get("dst") or "")
        before = len(state.deps)
        state.deps = [e for e in state.deps if not (e.src == src and e.dst == dst)]
        report.applied.append(f"DROP_DEP:{src}->{dst} ({before - len(state.deps)})")

    else:  # pragma: no cover
        report.rejected.append(f"未知操作 {kind}")


def _clamp_salience(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, n))


def _trim_to_budget(state: CompressedState, c_max_tokens: int, est_tokens_fn) -> list[str]:
    """确定性裁剪：先丢失效节点，再丢无锚点，最后**按显著度从低到高**丢。

    最后一档刻意按 salience 而不是按 source_round 排序：一轮可能超长，
    ΔC 一进来就顶爆预算，"丢最新的"会把本轮刚确立的结论直接扔掉。
    """
    trimmed: list[str] = []

    def over() -> bool:
        return est_tokens_fn(state) > c_max_tokens

    for status in ("invalidated", "superseded"):
        if not over():
            break
        for node in [f for f in state.fact_chain if f.status == status]:
            if not over():
                break
            state.fact_chain.remove(node)
            trimmed.append(f"{node.id}({status})")

    if over():
        for node in [f for f in state.fact_chain if f.unanchored]:
            if not over():
                break
            state.fact_chain.remove(node)
            trimmed.append(f"{node.id}(unanchored)")

    while over():
        referenced = {d for f in state.fact_chain for d in f.deps}
        candidates = [
            f for f in state.fact_chain if f.status == "active" and f.id not in referenced
        ]
        if not candidates:
            break
        # **负推理节点有保底**（README §1.3）：它拦住的是"下一轮重新走一遍已经否掉的路"，
        # 所以排序键里先按"是不是负节点"分组，负节点只在没有别的东西可丢时才被丢。
        # 第二档保 `assertion == "open"` 的节点：它们是"还没定、下一轮要拍板"的少数几条，
        # 字数极小而价值最高（丢了就会把上一轮的犹豫当成已定结论接着往下走）。
        # 中间再加一档保**带已知风险**的节点：结论已定但埋着坑，丢了就会踩进去。
        # 排序键是 `min` 的键 —— True 排在后面 = 更晚被丢。
        victim = min(
            candidates,
            key=lambda f: (
                f.node_kind == "think" and f.polarity == "-",
                f.assertion == "open",
                bool(f.risk),
                f.salience,
                -f.source_round,
                f.id,
            ),
        )
        state.fact_chain.remove(victim)
        trimmed.append(f"{victim.id}(budget,salience={victim.salience})")

    state.est_tokens = est_tokens_fn(state)
    return trimmed


# --------------------------------------------------------------------------- #
# 判定器
# --------------------------------------------------------------------------- #


def judge_round(
    *,
    prev: ValidationResult | None,
    cur: ValidationResult,
    merge_report: MergeReport,
    cfg: SessionConfig,
    round_index: int,
    hard_checks: set[str] | None = None,
) -> RoundJudgement:
    """accept / revert 判定。

    只有**硬约束**回归才回退。软约束的退化是要**度量**的信号，
    如果连它一起回退，那张"违反数随轮数"的曲线就永远是被摁平的零 —— 测量目标被消掉了。
    """
    hard = set(hard_checks or HARD_CHECK_IDS)
    reasons: list[str] = []
    regressions: list[str] = []

    def hard_failed(v: ValidationResult | None) -> set[str]:
        if v is None:
            return set()
        return {c.id for c in v.checks if c.passed is False and c.id in hard}

    fresh_hard = sorted(hard_failed(cur) - hard_failed(prev))
    if fresh_hard:
        reasons.append(f"硬约束新违反: {', '.join(fresh_hard)}")
        regressions.extend(fresh_hard)

    # 软约束的 pass -> fail 只记账，不触发回退（它是曲线本身的信号）
    if prev:
        for c in cur.checks:
            if c.id in hard or c.passed is not False or not (c.count or 0) > 0:
                continue
            old = prev.check(c.id)
            if old is not None and old.passed is True:
                regressions.append(f"{c.id}(软)")

    if cfg.revert_on_merge_conflict and merge_report.conflicts:
        reasons.append(f"合并冲突 {len(merge_report.conflicts)} 条")
        regressions.extend(merge_report.conflicts)

    if merge_report.constraint_text_mutations:
        reasons.append("压缩器试图改写约束原文")
        regressions.extend(merge_report.constraint_text_mutations)

    decision = "revert" if (cfg.revert_on_hard_regression and reasons) else "accept"
    if not reasons:
        reasons.append("无硬约束回归")

    return RoundJudgement(
        decision=decision,  # type: ignore[arg-type]
        reasons=reasons,
        regressions=regressions,
    )
