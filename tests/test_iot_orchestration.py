"""编排流水线（IoT 的核心机制）：一个 thinking 块 → 思考节点；工具往返 → ACT 节点。

离线跑：压缩器换成 stub（真压缩器的口径另有 `tests/test_thinking_nodes.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pciso.config import SessionConfig
from pciso.merge import apply_delta
from pciso.models import CompressedState, DeltaC, DeltaOp
from pciso.prefix import PrefixBundle
from pciso.session import Session
from pciso.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
RAW = "嗯……先看 tools/duration.py 现在是什么样，还是先写测试？先写实现吧，后面再补测试。"
CFG = {
    "session": {
        "project": "general",
        "arm": "chat",
        "prefix_path": "prefixes/general",
        "max_rounds": 100000,
        "c_max_tokens": 0,
        "chat_driven": True,
    },
    "compression": {"mode": "fact_chain", "model": "heuristic", "scope": "thinking"},
    "work_model": {"endpoint": "openai", "model": "deepseek-flash", "thinking": True},
    "validator": {"tests": "off", "docs": "off", "constraints": "off"},
}


def _think_op(statement: str, *, path: str = "tools/duration.py", polarity: str = "+") -> dict:
    return {
        "op": "ADD_FACT",
        "payload": {
            "id": None,  # 由 _ops() 按序补号
            "statement": statement,
            "node_kind": "think",
            "polarity": polarity,
            "assertion": "settled",
            "salience": 80,
            "anchor": {"path": path},
        },
    }


class StubCompressor:
    """只回答"这个块被提成了几条节点"，其余交给真流水线。"""

    def __init__(self, ops: list[dict] | None = None) -> None:
        self.raw_ops = list(ops or [_think_op("先写实现再补测试")])
        self.calls: list[dict] = []

    def compress(
        self,
        o_i: str,
        *,
        prior_state,
        round_index: int,
        c_max_tokens: int,
        checkpoints=(),
        landed=(),
        thinking: str = "",
    ) -> DeltaC:
        self.calls.append(
            {
                "o_i": o_i,
                "thinking": thinking,
                "c_max_tokens": c_max_tokens,
                "landed": tuple(landed),
                "round_index": round_index,
            }
        )
        ops = []
        for i, raw in enumerate(self.raw_ops, start=1):
            payload = dict(raw.get("payload") or {})
            payload.setdefault("id", f"N{i}")
            ops.append(DeltaOp(op=raw["op"], target=raw.get("target"), payload=payload))
        return DeltaC(ops=ops, model="stub", completion_tokens=17, cached_tokens=3)


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    cfg_file = tmp_path / "proxy.yaml"
    cfg_file.write_text(yaml.safe_dump(CFG, allow_unicode=True), encoding="utf-8")
    cfg = SessionConfig.load(cfg_file, base_dir=ROOT)
    prefix = PrefixBundle.load(ROOT / "prefixes/general")
    store = SessionStore(tmp_path / "state", "sid-iot")
    built = Session(cfg, prefix, store)
    tools = store.workspace / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "duration.py").write_text(
        "def format_duration(total_seconds: int) -> str:\n    return ''\n", encoding="utf-8"
    )
    return built


# --------------------------------------------------------------------------- #
# 一个 thinking 块 → 思考节点
# --------------------------------------------------------------------------- #


def test_a_thinking_block_becomes_think_nodes(session) -> None:
    session.compressor = StubCompressor()
    out = session.absorb_thinking_block(RAW, extra_allowed_paths=["tools/duration.py"])
    assert out["compressed"] is True
    assert out["items"] == ["先写实现再补测试"]  # 正向节点不挂前缀
    assert session.store.round_index() == 1
    record = session.store.last_round()
    assert record["kind"] == "thinking_block"
    assert record["judgement"] == "accept"
    assert record["violations"] == []
    node = record["c_i"]["fact_chain"][0]
    assert node["node_kind"] == "think"
    assert node["anchor"]["path"] == "tools/duration.py"
    assert node["anchor"]["resolved"] is True  # 盘上真有这个文件


def test_a_block_the_orchestrator_keeps_nothing_from_has_no_items(session) -> None:
    """编排器说"没什么值得记的"（0 条）⇒ 空列表；客户端那块**退回原文**（不压缩）。

    别在这儿塞一行"原文摘要"充数：那会把"没提纯"伪装成"提纯过了"
    （真机现场：英文块被硬截成 `… I alre…`，看着像提纯结果）。
    """

    empty = StubCompressor()
    empty.raw_ops = []  # 编排器返回 {"ops": []}
    session.compressor = empty
    out = session.absorb_thinking_block(RAW, extra_allowed_paths=["tools/duration.py"])
    assert out["items"] == []
    assert "overview" not in out


def test_item_lines_only_mark_what_needs_marking() -> None:
    """面板里那一行不是 C 的记账格式：正向不挂前缀，只有否掉/待定/落地才标。"""

    from pciso.actnodes import item_line
    from pciso.models import FactNode

    def node(**kw) -> FactNode:
        base = {"id": "N1", "statement": "先写实现再补测试", "node_kind": "think", "polarity": "+"}
        return FactNode(**{**base, **kw})

    assert item_line(node()) == "先写实现再补测试"
    assert item_line(node(polarity="-")) == "[否掉] 先写实现再补测试"
    assert item_line(node(assertion="open")) == "[待定] 先写实现再补测试"
    assert item_line(node(node_kind="act", polarity="")) == "[落地] 先写实现再补测试"


def test_one_line_overview_is_gone() -> None:
    """兜底概述已被撤掉（用户裁定：0 条就退回不压缩），别有人再把它加回来。"""

    import pciso.actnodes as actnodes

    assert not hasattr(actnodes, "one_line_overview")


def test_the_compressor_gets_the_thinking_and_nothing_else(session) -> None:
    stub = StubCompressor()
    session.compressor = stub
    session.absorb_thinking_block(RAW, landed=[("tools/duration.py", "a" * 64, 812)])
    call = stub.calls[0]
    assert call["thinking"] == RAW  # 提纯的原料就是这一块思考
    assert call["o_i"] == ""  # 正文/工具往返不进压缩器
    assert call["c_max_tokens"] == 0  # 0 = 不裁
    assert call["landed"] == (("tools/duration.py", "a" * 64, 812),)


def test_the_raw_thinking_is_persisted_for_audit(session) -> None:
    session.compressor = StubCompressor()
    session.absorb_thinking_block(RAW, extra_allowed_paths=["tools/duration.py"])
    assert session.store.read_thinking(0) == RAW


def test_an_empty_block_is_a_noop(session) -> None:
    stub = StubCompressor()
    session.compressor = stub
    assert session.absorb_thinking_block("   \n ") == {"compressed": False, "reason": "空的 thinking 块"}
    assert session.store.round_index() == 0
    assert stub.calls == []


def test_a_zero_budget_keeps_every_node(session) -> None:
    """`c_max_tokens=0` 是"不裁"。当预算零来理解的话，一条事实都留不下（真踩过）。"""
    ops = [_think_op(f"第 {i} 条推理") for i in range(5)]
    session.compressor = StubCompressor(ops)
    session.absorb_thinking_block(RAW, extra_allowed_paths=["tools/duration.py"])
    record = session.store.last_round()
    assert len(record["c_i"]["fact_chain"]) == 5
    assert record["merge_report"]["trimmed"] == []
    assert record["c_i"]["truncated"] is False


def test_a_zero_budget_is_not_a_zero_budget_in_merge() -> None:
    state = CompressedState(version=0)
    delta = DeltaC(
        ops=[
            DeltaOp(
                op="ADD_FACT",
                payload={"id": "N1", "statement": "一条很占地方的推理", "node_kind": "think"},
            )
        ]
    )
    kept, report = apply_delta(
        state, delta, round_index=1, c_max_tokens=0, constraint_texts=[], est_tokens_fn=lambda s: 10**6
    )
    assert [f.id for f in kept.fact_chain] == ["N1"]
    assert report.trimmed == []


# --------------------------------------------------------------------------- #
# 工具往返 → ACT 节点（零模型调用）
# --------------------------------------------------------------------------- #


def test_tool_writes_become_act_nodes(session) -> None:
    out = session.absorb_actions(written=[("tools/duration.py", "a" * 64, 812)])
    assert out["act_nodes"] == 1
    record = session.store.last_round()
    assert record["kind"] == "tool_actions"
    node = record["c_i"]["fact_chain"][0]
    assert node["node_kind"] == "act"
    assert "sha256 aaaaaaaaaaaa" in node["statement"]


def test_a_tool_call_that_wrote_nothing_records_nothing(session) -> None:
    assert session.absorb_actions(written=[]) == {"act_nodes": 0}
    assert session.store.round_index() == 0


def test_actions_do_not_call_the_compressor(session) -> None:
    stub = StubCompressor()
    session.compressor = stub
    session.absorb_actions(written=[("tools/duration.py", "b" * 64, 12)])
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# 记账：编排出来的轮次必须能被 /metrics、/state 读
# --------------------------------------------------------------------------- #


def test_an_orchestrated_round_has_the_shape_metrics_reads(session) -> None:
    session.compressor = StubCompressor()
    session.absorb_thinking_block(RAW, extra_allowed_paths=["tools/duration.py"])
    session.absorb_actions(written=[("tools/duration.py", "c" * 64, 812)])
    metrics = session.metrics()
    assert [row["round"] for row in metrics["per_round"]] == [1, 2]
    assert metrics["per_round"][0]["c_tokens"] > 0
    assert metrics["per_round"][1]["files_written"] == 0  # 编排轮次不走落盘校验
    assert session.state().version == 2  # C 前进了两格


# --------------------------------------------------------------------------- #
# 工具报错 → 失败链 + [否掉] 负节点（报错不能只活在这一轮的消息里）
# --------------------------------------------------------------------------- #


def test_a_failing_tool_becomes_a_failure_and_a_negative_node(session) -> None:
    out = session.absorb_tool_failures(
        errors=[("read_image", 3, 'Error: cannot read "x.png" as an image')]
    )
    assert out["error_nodes"] == 1
    record = session.store.last_round()
    assert record["kind"] == "tool_errors"
    assert record["judgement"] == "accept"
    state = session.state()
    assert state.failure_chain[0].attempt == "调用 read_image"
    assert state.failure_chain[0].source == "tool"
    node = state.fact_chain[0]
    assert node.node_kind == "think"
    assert node.polarity == "-"
    assert "不再调用 read_image" in node.statement
    assert "连续 3 次报错" in node.why
    # /metrics 与 /state 读得动这条编排轮次（硬读 judgement/violations/usage）
    assert session.metrics()["per_round"][0]["round"] == 1
    assert session.state().version == 1


def test_no_tool_errors_records_nothing(session) -> None:
    assert session.absorb_tool_failures() == {"error_nodes": 0}
    assert session.store.round_index() == 0
