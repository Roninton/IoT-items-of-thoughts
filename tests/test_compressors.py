"""压缩器：结构性事实靠 AST（零幻觉），语义性内容靠规则或模型。"""

from __future__ import annotations

from pciso.compressors import (
    HeuristicFactChainCompressor,
    ModelFactChainCompressor,
    NullCompressor,
    SummaryCompressor,
    build_compressor,
)
from pciso.config import CompressionConfig, SessionConfig
from pciso.models import CompressedState, FactNode, FailureEntry
from pciso.workmodels import ChatResult

O_I = '''## 第 4 轮：视频生成接口

设计取舍：曾考虑用 ffmpeg 子进程封装编码器，因引入外部进程依赖而否决。

未解决：
- 补时间轴与容器编码
- 补文档

```file:pkg/video.py
"""视频抽象。"""

from .core import Point


class FrameSequence:
    """有序帧序列。"""

    def append(self, frame: int) -> int:
        """追加一帧。"""
        return 0


def build(a: int) -> int:
    """构建。"""
    return a
```

```file:tests/test_video.py
"""自测。"""


def test_append():
    """用例。"""
    assert True
```
'''


def _prior() -> CompressedState:
    return CompressedState(
        fact_chain=[
            FactNode(id="N1", statement="接口 pkg.core.Point 已定义"),
            FactNode(id="N2", statement="过时的", status="superseded", superseded_by="N3"),
        ],
        failure_chain=[FailureEntry(id="F1", attempt="用 PIL 写 GIF", reason="不支持矢量")],
    )


# --------------------------------------------------------------------------- #
# heuristic
# --------------------------------------------------------------------------- #


def test_heuristic_extracts_structural_facts_with_anchors() -> None:
    delta = HeuristicFactChainCompressor().compress(
        O_I, prior_state=_prior(), round_index=3, c_max_tokens=1000
    )
    facts = [op for op in delta.ops if op.op == "ADD_FACT"]
    statements = " ".join(op.payload["statement"] for op in facts)
    assert "pkg.video.FrameSequence" in statements
    assert "pkg.video.build" in statements
    # 结构性事实必须带锚点，且签名与 AST 一致
    build_op = next(op for op in facts if "build" in op.payload["statement"])
    assert build_op.payload["anchor"]["path"] == "pkg/video.py"
    assert build_op.payload["anchor"]["signature"] == "build(a: int) -> int"


def test_heuristic_assigns_salience_by_kind() -> None:
    """接口/类型高显著度；测试存在性低显著度（下一轮 AST 还能重新看出来）。"""
    delta = HeuristicFactChainCompressor().compress(
        O_I, prior_state=_prior(), round_index=3, c_max_tokens=1000
    )
    facts = [op for op in delta.ops if op.op == "ADD_FACT"]
    iface = next(op for op in facts if "FrameSequence" in op.payload["statement"])
    test_fact = next(op for op in facts if "test_append" in op.payload["statement"])
    assert iface.payload["salience"] == 80
    assert test_fact.payload["salience"] == 30


def test_heuristic_extracts_dependency_edges() -> None:
    delta = HeuristicFactChainCompressor().compress(
        O_I, prior_state=_prior(), round_index=3, c_max_tokens=1000
    )
    deps = [op for op in delta.ops if op.op == "ADD_DEP"]
    assert any(op.payload["src"] == "pkg.video" and op.payload["dst"] == "pkg.core" for op in deps)


def test_heuristic_captures_rejected_design() -> None:
    delta = HeuristicFactChainCompressor().compress(
        O_I, prior_state=_prior(), round_index=3, c_max_tokens=1000
    )
    failures = [op for op in delta.ops if op.op == "ADD_FAILURE"]
    assert len(failures) == 1
    assert "ffmpeg" in failures[0].payload["attempt"]
    assert "外部进程依赖" in failures[0].payload["reason"]


def test_heuristic_records_progress_and_unresolved() -> None:
    delta = HeuristicFactChainCompressor().compress(
        O_I, prior_state=_prior(), round_index=3, c_max_tokens=1000
    )
    kinds = {op.op for op in delta.ops}
    assert "SET_PROGRESS" in kinds
    unresolved = next(op for op in delta.ops if op.op == "SET_UNRESOLVED")
    assert len(unresolved.payload["items"]) == 2


def test_heuristic_does_not_invent_facts() -> None:
    """零幻觉：O_i 里没有的东西不能出现。"""
    delta = HeuristicFactChainCompressor().compress(
        "只说了几句话，没有任何文件块。",
        prior_state=CompressedState(),
        round_index=1,
        c_max_tokens=1000,
    )
    assert [op for op in delta.ops if op.op == "ADD_FACT"] == []


# --------------------------------------------------------------------------- #
# off / summary
# --------------------------------------------------------------------------- #


def test_null_compressor_emits_nothing() -> None:
    delta = NullCompressor().compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert delta.ops == []
    assert delta.model == "null"


def test_summary_compressor_accumulates_and_caps() -> None:
    comp = SummaryCompressor(max_chars=200)
    state = CompressedState(summary="早先的事。")
    delta = comp.compress(O_I, prior_state=state, round_index=2, c_max_tokens=1000)
    assert delta.raw
    assert any(op.op == "SET_PROGRESS" for op in delta.ops)
    state.summary = delta.raw
    delta2 = comp.compress(O_I, prior_state=state, round_index=3, c_max_tokens=1000)
    assert len(delta2.raw) <= 200


# --------------------------------------------------------------------------- #
# 模型压缩器
# --------------------------------------------------------------------------- #


class StubClient:
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def chat(self, prompt: str, **kw) -> ChatResult:
        self.prompts.append(prompt)
        return ChatResult(text=self.text, completion_tokens=42)


def test_model_compressor_parses_flat_ops() -> None:
    client = StubClient(
        '{"ops": [{"op": "ADD_FACT", "statement": "x", '
        '"anchor": {"path": "a.py", "symbol": "f"}, "salience": 77}]}'
    )
    delta = ModelFactChainCompressor(client=client).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert delta.error is None
    assert delta.ops[0].payload["salience"] == 77  # 平铺字段不能被丢掉
    assert delta.ops[0].payload["anchor"]["symbol"] == "f"


def test_model_compressor_accepts_nested_payload() -> None:
    client = StubClient(
        '{"ops": [{"op": "ADD_FAILURE", "payload": {"attempt": "A", "reason": "B"}}]}'
    )
    delta = ModelFactChainCompressor(client=client).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert delta.ops[0].payload == {"attempt": "A", "reason": "B"}


def test_model_compressor_prompt_is_prefix_isolated_and_has_prior_structure() -> None:
    client = StubClient('{"ops": []}')
    ModelFactChainCompressor(client=client).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    prompt = client.prompts[0]
    assert "N1" in prompt  # C_{i-1} 的结构头部
    assert "SUPERSEDE_FACT" in prompt  # 更正可被表达
    assert "Point" not in prompt or "接口 pkg.core.Point" in prompt


def test_model_compressor_reports_unparseable_output() -> None:
    delta = ModelFactChainCompressor(client=StubClient("我拒绝输出 JSON。")).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert delta.error
    assert delta.ops == []


def test_model_compressor_reports_transport_error() -> None:
    class Broken:
        def chat(self, prompt: str, **kw):
            return ChatResult(error="TRANSPORT: boom")

    delta = ModelFactChainCompressor(client=Broken()).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert delta.error and "TRANSPORT" in delta.error


def test_model_compressor_skips_malformed_ops() -> None:
    client = StubClient('{"ops": [{"statement": "缺 op"}, {"op": "ADD_FACT", "statement": "ok"}]}')
    delta = ModelFactChainCompressor(client=client).compress(
        O_I, prior_state=_prior(), round_index=1, c_max_tokens=1000
    )
    assert len(delta.ops) == 1


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def test_factory_dispatch() -> None:
    def cfg(mode: str, model: str = "heuristic") -> SessionConfig:
        return SessionConfig(compression=CompressionConfig(mode=mode, model=model))  # type: ignore[arg-type]

    assert isinstance(build_compressor(cfg("off")), NullCompressor)
    assert isinstance(build_compressor(cfg("summary")), SummaryCompressor)
    assert isinstance(build_compressor(cfg("fact_chain")), HeuristicFactChainCompressor)
