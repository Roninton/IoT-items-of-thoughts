"""合并器与判定器：确定性、可预测、不靠模型理解语义。"""

from __future__ import annotations

from pciso.config import SessionConfig
from pciso.merge import apply_delta, dependents_of, find_cycles, judge_round
from pciso.models import (
    CheckResult,
    CompressedState,
    ConstraintStatus,
    DeltaC,
    DeltaOp,
    FactNode,
    MergeReport,
    ValidationResult,
)

CONSTRAINTS = {"C1": "原文一", "C2": "原文二"}


def _state(**kw) -> CompressedState:
    base: dict = {"constraints": [ConstraintStatus(id="C1", text="原文一")]}
    base.update(kw)
    return CompressedState(**base)


def _apply(prev: CompressedState, ops: list[DeltaOp], *, c_max: int = 100000):
    return apply_delta(
        prev,
        DeltaC(ops=ops),
        round_index=1,
        c_max_tokens=c_max,
        constraint_texts=CONSTRAINTS,
        est_tokens_fn=lambda s: len(s.fact_chain) * 10,
    )


# --------------------------------------------------------------------------- #
# 事实链
# --------------------------------------------------------------------------- #


def test_add_fact_assigns_id_and_anchor() -> None:
    state, report = _apply(
        _state(),
        [
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "statement": "接口 a.f 已定义",
                    "anchor": {"path": "pkg/a.py", "symbol": "f"},
                },
            )
        ],
    )
    assert state.fact_chain[0].id == "N1"
    assert state.fact_chain[0].anchor is not None
    assert state.fact_chain[0].unanchored is False
    assert report.applied == ["ADD_FACT:N1"]


def test_fact_without_anchor_is_marked_unanchored() -> None:
    state, _ = _apply(_state(), [DeltaOp(op="ADD_FACT", payload={"statement": "无锚点事实"})])
    assert state.fact_chain[0].unanchored is True


def test_restatement_is_not_a_new_node() -> None:
    """每轮重写同一个文件不能灌进重复节点 —— 否则 C 预算假性爆掉。"""
    state, _ = _apply(
        _state(),
        [
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "statement": "接口 a.f 已定义",
                    "anchor": {"path": "pkg/a.py", "symbol": "f", "signature": "f() -> int"},
                },
            )
        ],
    )
    state, report = _apply(
        state,
        [
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "statement": "接口 a.f 已定义",
                    "anchor": {"path": "pkg/a.py", "symbol": "f", "signature": "f() -> int"},
                },
            )
        ],
    )
    assert len(state.fact_chain) == 1
    assert any("restated" in x for x in report.applied)


def test_supersede_marks_old_and_creates_new() -> None:
    prev = _state()
    prev.fact_chain.append(FactNode(id="N1", statement="旧"))
    state, _ = _apply(
        prev,
        [
            DeltaOp(
                op="SUPERSEDE_FACT",
                target="N1",
                payload={"statement": "新", "id": "N2"},
            )
        ],
    )
    assert state.fact("N1").status == "superseded"
    assert state.fact("N1").superseded_by == "N2"
    assert state.fact("N2").statement == "新"


def test_supersede_cascades_to_dependents() -> None:
    prev = _state()
    prev.fact_chain = [
        FactNode(id="N1", statement="基础"),
        FactNode(id="N2", statement="依赖 N1", deps=["N1"]),
        FactNode(id="N3", statement="传递依赖", deps=["N2"]),
    ]
    assert dependents_of(prev, "N1") == ["N2", "N3"]
    _, report = _apply(prev, [DeltaOp(op="SUPERSEDE_FACT", target="N1", payload={"id": "N9"})])
    assert any("N1 ⇒ N2" in x for x in report.correction_cascades)
    assert any("N1 ⇒ N3" in x for x in report.correction_cascades)


def test_invalidate_target_missing_is_rejected_not_crash() -> None:
    state, report = _apply(_state(), [DeltaOp(op="INVALIDATE_FACT", target="N404")])
    assert state.fact_chain == []
    assert report.rejected


# --------------------------------------------------------------------------- #
# 约束原文不可改写
# --------------------------------------------------------------------------- #


def test_constraint_text_mutation_is_rejected() -> None:
    state, report = _apply(
        _state(),
        [DeltaOp(op="SET_CONSTRAINT", payload={"id": "C1", "text": "偷偷改宽一点", "status": "satisfied"})],
    )
    assert report.constraint_text_mutations
    assert state.constraints[0].text == "原文一"  # 没被改
    assert state.constraints[0].status == "unverified"


def test_constraint_status_update_is_allowed() -> None:
    state, report = _apply(
        _state(),
        [DeltaOp(op="SET_CONSTRAINT", payload={"id": "C1", "status": "violated", "evidence": "e"})],
    )
    assert state.constraints[0].status == "violated"
    assert state.constraints[0].violated_rounds == [1]
    assert not report.constraint_text_mutations


def test_unknown_constraint_id_rejected() -> None:
    _, report = _apply(_state(), [DeltaOp(op="SET_CONSTRAINT", payload={"id": "C99"})])
    assert report.rejected


# --------------------------------------------------------------------------- #
# 依赖图
# --------------------------------------------------------------------------- #


def test_find_cycles_detects_simple_cycle() -> None:
    assert find_cycles([("a", "b"), ("b", "a")]) == [["a", "b"]]


def test_find_cycles_ignores_dag() -> None:
    assert find_cycles([("a", "b"), ("b", "c"), ("a", "c")]) == []


def test_add_dep_rejects_self_loop() -> None:
    _, report = _apply(_state(), [DeltaOp(op="ADD_DEP", payload={"src": "a", "dst": "a"})])
    assert report.rejected


def test_dep_cycle_is_recorded_as_conflict() -> None:
    _, report = _apply(
        _state(),
        [
            DeltaOp(op="ADD_DEP", payload={"src": "a", "dst": "b"}),
            DeltaOp(op="ADD_DEP", payload={"src": "b", "dst": "a"}),
        ],
    )
    assert report.cycles
    assert report.dirty()


def test_drop_dep() -> None:
    state, _ = _apply(
        _state(), [DeltaOp(op="ADD_DEP", payload={"src": "a", "dst": "b"})]
    )
    state, _ = _apply(state, [DeltaOp(op="DROP_DEP", payload={"src": "a", "dst": "b"})])
    assert state.deps == []


# --------------------------------------------------------------------------- #
# 预算截断
# --------------------------------------------------------------------------- #


def test_budget_trims_and_records() -> None:
    prev = _state()
    prev.fact_chain = [FactNode(id=f"N{i}", statement=f"事实 {i}") for i in range(1, 8)]
    state, report = _apply(prev, [], c_max=30)
    assert len(state.fact_chain) < 7
    assert report.trimmed
    assert state.truncated is True


def test_trim_drops_invalidated_first() -> None:
    prev = _state()
    prev.fact_chain = [
        FactNode(id="N1", statement="活的"),
        FactNode(id="N2", statement="死的", status="invalidated"),
    ]
    state, report = _apply(prev, [], c_max=10)
    assert [f.id for f in state.fact_chain] == ["N1"]
    assert report.trimmed == ["N2(invalidated)"]


def test_trim_drops_low_salience_before_high_salience() -> None:
    """超长一轮把预算顶爆时，要丢"最不重要"的，而不是"最老的"。

    这里 N1 最老（轮 1）但最显著；N2/N3 是最新一轮刚确立的低显著度事实。
    按新旧丢会先把 N1 扔掉 —— 那正是"压缩把刚确立的结论抹掉"的经典事故。
    """
    prev = _state()
    prev.fact_chain = [
        FactNode(id="N1", statement="关键的接口决定", salience=90, source_round=1),
        FactNode(id="N2", statement="可从代码重新看出来的", salience=20, source_round=9),
        FactNode(id="N3", statement="同上", salience=20, source_round=8),
    ]
    state, report = _apply(prev, [], c_max=15)
    assert [f.id for f in state.fact_chain] == ["N1"]
    assert any("salience=20" in t for t in report.trimmed)


def test_trim_never_drops_a_referenced_node() -> None:
    """被别的节点依赖的事实不能被裁掉（否则依赖图当场断裂）。"""
    prev = _state()
    prev.fact_chain = [
        FactNode(id="N1", statement="基础", salience=10),
        FactNode(id="N2", statement="依赖基础", deps=["N1"], salience=90),
        FactNode(id="N3", statement="无关", salience=10),
    ]
    state, _ = _apply(prev, [], c_max=20)
    assert any(f.id == "N1" for f in state.fact_chain)


# --------------------------------------------------------------------------- #
# 失败链去重
# --------------------------------------------------------------------------- #


def test_duplicate_failure_is_recorded_not_appended() -> None:
    ops = [DeltaOp(op="ADD_FAILURE", payload={"attempt": "用 ffmpeg 子进程", "reason": "依赖泄漏"})]
    state, _ = _apply(_state(), ops)
    state, report = _apply(state, ops)
    assert len(state.failure_chain) == 1
    assert report.duplicate_attempts
    assert state.failure_chain[0].round_index_seen == [1, 1]


# --------------------------------------------------------------------------- #
# 判定器
# --------------------------------------------------------------------------- #


def _cfg(**kw) -> SessionConfig:
    return SessionConfig(**kw)


def _val(round_index: int, *checks: CheckResult) -> ValidationResult:
    return ValidationResult(round_index=round_index, checks=list(checks))


def test_judge_reverts_on_fresh_hard_violation() -> None:
    cur = _val(2, CheckResult(id="frozen_tests", passed=False, count=1))
    j = judge_round(
        prev=_val(1), cur=cur, merge_report=MergeReport(), cfg=_cfg(), round_index=1
    )
    assert j.decision == "revert"
    assert "frozen_tests" in j.regressions


def test_judge_accepts_when_no_hard_regression() -> None:
    cur = _val(2, CheckResult(id="docstring_coverage", passed=False, count=3, constraint_id="C4"))
    j = judge_round(prev=_val(1), cur=cur, merge_report=MergeReport(), cfg=_cfg(), round_index=1)
    assert j.decision == "accept"


def test_soft_regression_is_recorded_but_does_not_revert() -> None:
    """软约束退化是**要度量的信号**；连它一起回退，曲线就被摁平成零了。"""
    prev = _val(1, CheckResult(id="docstring_coverage", passed=True, count=0, constraint_id="C4"))
    cur = _val(2, CheckResult(id="docstring_coverage", passed=False, count=3, constraint_id="C4"))
    j = judge_round(prev=prev, cur=cur, merge_report=MergeReport(), cfg=_cfg(), round_index=1)
    assert j.decision == "accept"
    assert "docstring_coverage(软)" in j.regressions


def test_persistent_hard_violation_does_not_revert_forever() -> None:
    """持续篡改只回退一次，否则永远推进不下去（工作区每轮已被恢复）。"""
    bad = CheckResult(id="frozen_tests", passed=False, count=1)
    j = judge_round(
        prev=_val(1, bad), cur=_val(2, bad), merge_report=MergeReport(), cfg=_cfg(), round_index=1
    )
    assert j.decision == "accept"


def test_hard_set_can_come_from_prefix() -> None:
    cur = _val(2, CheckResult(id="my_custom_check", passed=False, count=1))
    j = judge_round(
        prev=_val(1),
        cur=cur,
        merge_report=MergeReport(),
        cfg=_cfg(),
        round_index=1,
        hard_checks={"my_custom_check"},
    )
    assert j.decision == "revert"


def test_constraint_text_mutation_triggers_revert() -> None:
    report = MergeReport(constraint_text_mutations=["C1 试图改写约束原文（禁止）"])
    j = judge_round(prev=_val(1), cur=_val(2), merge_report=report, cfg=_cfg(), round_index=1)
    assert j.decision == "revert"


def test_revert_can_be_disabled() -> None:
    cur = _val(2, CheckResult(id="import_cycles", passed=False, count=1))
    j = judge_round(
        prev=_val(1),
        cur=cur,
        merge_report=MergeReport(),
        cfg=_cfg(revert_on_hard_regression=False),
        round_index=1,
    )
    assert j.decision == "accept"
