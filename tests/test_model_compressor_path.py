"""模型压缩器的**真实代码路径**：这是 C 组的核心，也是此前测得最薄的一环。

离线矩阵全部用确定性压缩器，所以 `ModelFactChainCompressor` 从构建 prompt、
发请求、解析 JSON、到把 op 交给合并器这一整条链，只被 stub 单测覆盖过。
而 C 组（目标方案）真机上跑的正是这条路 —— 它一旦有错，是一轮一轮烧钱才发现的。

这里用一个脚本化客户端把它整条走通，并锁住四件事：
  1. 结构化 op 真的被合并器应用（含 SUPERSEDE 的级联更正）
  2. 非法 op 被拒并记账，而不是把整轮搞挂
  3. 压缩器改写约束原文会被拦下
  4. **压缩开销真的记进 usage** —— 这一项曾经因为漏了累加而恒为 0
"""

from __future__ import annotations

import json
from pathlib import Path

from pciso.compressors import ModelFactChainCompressor
from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session
from pciso.workmodels import ChatResult

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"

ROUND_1 = {
    "ops": [
        {
            "op": "ADD_FACT",
            "id": "N1",
            "statement": "接口 drawvid.core.Point 已定义",
            "anchor": {"path": "drawvid/core.py", "symbol": "Point"},
            "salience": 90,
        },
        {
            "op": "ADD_FACT",
            "id": "N2",
            "statement": "Rect 依赖 Point",
            "deps": ["N1"],
            "anchor": {"path": "drawvid/core.py", "symbol": "Rect"},
            "salience": 80,
        },
        {"op": "ADD_FAILURE", "attempt": "用 Pillow", "reason": "引入第三方依赖"},
        {"op": "SET_PROGRESS", "text": "搭好 core 层"},
        {"op": "SET_UNRESOLVED", "items": ["补绘图基元"]},
    ]
}

ROUND_2 = {
    "ops": [
        {
            "op": "SUPERSEDE_FACT",
            "target": "N1",
            "payload": {
                "id": "N3",
                "statement": "Point 改成了 frozen dataclass",
                "anchor": {"path": "drawvid/core.py", "symbol": "Point"},
                "salience": 95,
            },
        }
    ]
}

ROUND_3 = {
    "ops": [
        {"op": "ADD_FACT", "statement": ""},  # 空陈述：必须被拒
        {"op": "NONSENSE_OP", "payload": {}},  # 未知操作：必须被拒
        {"op": "INVALIDATE_FACT", "target": "N404"},  # 目标不存在：必须被拒
        {"op": "ADD_DEP", "payload": {"src": "drawvid.drawing", "dst": "drawvid.core"}},
    ]
}

# 这一轮会触发**回退**（压缩器试图改写约束原文），所以必须单独一轮：
# 否则它会把同一轮里合法的 op 一起丢掉，测试就分不清"被拒"与"被回退"。
ROUND_4 = {
    "ops": [
        {
            "op": "SET_CONSTRAINT",
            "payload": {"id": "C4", "text": "偷偷把约束改宽", "status": "satisfied"},
        }
    ]
}


class ScriptedClient:
    """按轮次脚本吐 JSON 的假客户端 —— 不联网，但走真实的压缩器代码。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    def chat(self, prompt: str, **kwargs: object) -> ChatResult:
        self.prompts.append(prompt)
        payload = self.script.pop(0) if self.script else {"ops": []}
        return ChatResult(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=321,
            completion_tokens=87,
        )


def make_session(tmp_path: Path, client: ScriptedClient) -> Session:
    compressor = ModelFactChainCompressor(client=client)  # type: ignore[arg-type]
    # session 在 __init__ 里调 build_compressor(cfg)，这里把它换成走真实模型路径的压缩器
    import pciso.session as session_mod

    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "MODEL-COMP",
        "prefix_path": str(PREFIX),
        "state_dir": str(tmp_path / "state"),
        "max_rounds": 4,
        "c_max_tokens": 2000,
        "fake_oracle": "drawing_video",
        "compression": {"mode": "fact_chain", "model": "some-real-endpoint"},
        "work_model": {"endpoint": "fake"},
    }
    original = session_mod.build_compressor
    session_mod.build_compressor = lambda _cfg: compressor  # type: ignore[assignment]
    try:
        return Session.create(
            SessionCreateRequest(
                project="drawing_video_toolchain",
                prefix_path=str(PREFIX),
                config=cfg,
                arm="MODEL-COMP",
            ),
            base_dir=tmp_path,
            state_dir=str(tmp_path / "state"),
        )
    finally:
        session_mod.build_compressor = original  # type: ignore[assignment]


def test_model_compressor_ops_reach_the_merger(tmp_path: Path) -> None:
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3])
    session = make_session(tmp_path, client)

    first = session.round(RoundRequest())
    assert first.committed
    ids = [f.id for f in first.c_i.fact_chain]
    assert ids == ["N1", "N2"]
    assert first.c_i.progress == "搭好 core 层"
    assert first.c_i.unresolved == ["补绘图基元"]
    assert [f.attempt for f in first.c_i.failure_chain] == ["用 Pillow"]
    # 锚点必须真的解析得到（真实代码里的 Point/Rect）
    assert all(f.anchor is not None and f.anchor.resolved for f in first.c_i.fact_chain)
    assert first.c_i.fact("N1").salience == 90


def test_supersede_from_the_compressor_cascades(tmp_path: Path) -> None:
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3])
    session = make_session(tmp_path, client)
    session.round(RoundRequest())
    second = session.round(RoundRequest())

    assert second.c_i.fact("N1").status == "superseded"
    assert second.c_i.fact("N1").superseded_by == "N3"
    assert second.c_i.fact("N3").statement == "Point 改成了 frozen dataclass"
    # N2 依赖 N1 —— 更正必须沿依赖级联标记出来
    assert any("N1 ⇒ N2" in c for c in second.merge_report.correction_cascades)


def test_compressor_prompt_carries_prior_structure(tmp_path: Path) -> None:
    """无状态压缩器要能表达"推翻旧节点"，就必须看到旧节点的 id。"""
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3])
    session = make_session(tmp_path, client)
    session.round(RoundRequest())
    session.round(RoundRequest())

    assert "N1" in client.prompts[0] or "（无）" in client.prompts[0]  # 首轮无先验
    assert "N1" in client.prompts[1], "第 2 轮的压缩器 prompt 没带上先验节点"
    assert "SUPERSEDE_FACT" in client.prompts[1]


def test_bad_ops_are_rejected_not_fatal(tmp_path: Path) -> None:
    """非法 op 被逐条拒掉并记账，同轮里合法的 op 照常生效，整轮不失败。"""
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3, ROUND_4])
    session = make_session(tmp_path, client)
    session.round(RoundRequest())
    session.round(RoundRequest())
    third = session.round(RoundRequest())

    assert third.committed, "非法 op 不该让整轮失败"
    assert third.judge.decision == "accept", third.judge.reasons
    rejected = " ".join(third.merge_report.rejected)
    assert "ADD_FACT" in rejected  # 空陈述
    assert "INVALIDATE_FACT" in rejected  # 目标不存在
    # 未知 op 在压缩器侧就被丢弃（连 DeltaOp 都构造不出来），合法的那条必须生效
    assert any(e.src == "drawvid.drawing" for e in third.c_i.deps)


def test_constraint_text_rewrite_is_blocked_and_reverts(tmp_path: Path) -> None:
    """压缩器改写约束原文 = 硬违规：文本不动，而且这一轮要**回退**（C 不前进）。"""
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3, ROUND_4])
    session = make_session(tmp_path, client)
    session.round(RoundRequest())
    session.round(RoundRequest())
    accepted = session.round(RoundRequest())
    assert accepted.judge.decision == "accept"
    c_before = accepted.c_i.model_dump(mode="json")

    last = session.round(RoundRequest())
    assert last.merge_report.constraint_text_mutations
    assert last.judge.decision == "revert", last.judge.reasons
    assert "C4 试图改写约束原文（禁止）" in last.judge.regressions
    c4 = next(c for c in last.c_i.constraints if c.id == "C4")
    assert "偷偷把约束改宽" not in c4.text
    assert c4.text == "每个公开接口必须有 docstring"
    # 回退意味着 C 停在改写之前 —— 否则"约束原文不可改写"就只是句口号
    assert last.c_i.model_dump(mode="json") == c_before


def test_compressor_cost_is_actually_recorded(tmp_path: Path) -> None:
    """回归测试：压缩开销曾经因为漏了累加而在报告里恒为 0。

    那个数字等于把本方案最大的成本项从成本表里抹掉，而且不会有任何报错 ——
    P1 的结论会因此完全反过来。
    """
    client = ScriptedClient([ROUND_1, ROUND_2, ROUND_3, ROUND_4])
    session = make_session(tmp_path, client)
    for _ in range(4):
        session.round(RoundRequest())

    rows = session.metrics()["per_round"]
    assert all(r["compressor_tokens"] > 0 for r in rows), rows
    assert all(r["compressor_tokens"] == 321 + 87 for r in rows)
    assert session.metrics()["tokens"]["compressor_total"] == 4 * (321 + 87)


def test_compressor_error_does_not_block_the_round(tmp_path: Path) -> None:
    """压缩器返回垃圾时：代码照样提交（它是真的、也验证过了），但错误要记账。"""

    class Broken(ScriptedClient):
        def chat(self, prompt: str, **kwargs: object) -> ChatResult:
            self.prompts.append(prompt)
            return ChatResult(text="我拒绝输出 JSON。", prompt_tokens=10, completion_tokens=5)

    client = Broken([])
    session = make_session(tmp_path, client)
    res = session.round(RoundRequest())

    assert res.committed
    assert res.telemetry.get("compressor_error")
    assert res.c_i.fact_chain == []
    # 即便压缩失败，成本也照记 —— 花了就是花了
    assert res.usage.compressor_prompt_tokens == 10


# --------------------------------------------------------------------------- #
# 事实节点 id 的格式：真机踩过的那个坑
# --------------------------------------------------------------------------- #

# 真实模型自由发挥出来的 id 前缀（实测：F1/A1/P1/R1 都出现过）
WRONG_IDS = {
    "ops": [
        {
            "op": "ADD_FACT",
            "id": "F1",
            "statement": "assets 层拆成 image/png 两个文件",
            "anchor": {"path": "drawvid/core.py", "symbol": "Point"},
            "salience": 90,
        },
        {
            "op": "ADD_FACT",
            "id": "A1",
            "statement": "Image 用 bytearray 行主序存 RGBA",
            "anchor": {"path": "drawvid/core.py", "symbol": "Rect"},
            "salience": 90,
        },
        {"op": "SET_PROGRESS", "text": "推进了"},
    ]
}


def test_wrong_fact_ids_are_rejected_and_loudly_counted(tmp_path: Path) -> None:
    """**这条是真机跑出来的。**

    提示词原先没写事实节点 id 的格式，真实模型就自己发明了 F1/A1/P1/R1；
    而 `FactNode` 只接受 `N<数字>` —— 于是每轮 9 条事实被拒 6~8 条，**事实链几轮都是空的**。

    更糟的是没有任何东西报警：违反数曲线照常演进（约束状态由验证器独立写入），
    而离线矩阵用的 heuristic 提取器只生成 N1/N2，永远测不到这条路。

    所以三重保障都要有：拒收有明确原因、遥测标记提取器失效、**拒收率进指标**。
    """
    client = ScriptedClient([WRONG_IDS])
    session = make_session(tmp_path, client)
    res = session.round(RoundRequest())

    # 提取器确实产出了 op（不是没提取）
    assert [o.op for o in res.delta_c.ops].count("ADD_FACT") == 2
    # 但一条都没进事实链
    assert res.c_i.fact_chain == []
    # 拒收原因必须说清是格式问题
    rejected = " ".join(res.merge_report.rejected)
    assert "ADD_FACT" in rejected
    assert "N" in rejected
    # 遥测与判定理由都要出声
    assert res.telemetry.get("extractor_failed") is True
    assert res.telemetry["fact_ops_rejected"] == 2
    assert res.telemetry["fact_ops_applied"] == 0
    assert any("提取器失效" in r for r in res.judge.reasons), res.judge.reasons

    # 指标：拒收率必须是一个能一眼看到的数
    quality = session.metrics()["compression_quality"]
    assert quality["fact_ops_rejected"] == 2
    assert quality["fact_ops_applied"] == 0
    assert quality["fact_acceptance_rate"] == 0.0


def test_correct_ids_still_work_and_are_counted(tmp_path: Path) -> None:
    """对照：正确的 id 一切照旧，且接受率是 1.0（别把正常路径也拦了）。"""
    client = ScriptedClient([ROUND_1])
    session = make_session(tmp_path, client)
    res = session.round(RoundRequest())

    assert [f.id for f in res.c_i.fact_chain] == ["N1", "N2"]
    assert res.telemetry["fact_ops_applied"] == 2
    assert res.telemetry["fact_ops_rejected"] == 0
    assert "extractor_failed" not in res.telemetry
    quality = session.metrics()["compression_quality"]
    assert quality["fact_acceptance_rate"] == 1.0


def test_extraction_prompt_states_the_id_format() -> None:
    """根因修复：格式必须写在提示词里，而且要给一个合法示例。

    只在合并器那边校验格式、嘴里不说，等于把"模型猜对"当成了实现正确性。
    """
    from pciso.render import build_compressor_prompt

    prompt = build_compressor_prompt(o_i="x", round_index=2, c_max_tokens=2000)
    assert "加数字" in prompt
    assert "直接拒收" in prompt
    assert '"id": "N1"' in prompt, "缺一个合法的 op 示例"


def test_structured_output_is_wired_from_config(tmp_path: Path) -> None:
    """`response_format` 必须从配置一路传到请求体。

    **实测结论**：本 endpoint 不支持 `json_schema`（所有变体一律 HTTP 400），
    只支持 `json_object` —— 它保证"是合法 JSON"，**不保证字段格式**。
    所以事实节点 id 那类约束仍然靠 提示词 + 服务端校验 + 拒收率度量 三层；
    但"输出根本不是 JSON"这一类失败可以被它结构性消掉，那就该用上。

    这条测试防的是"配置里写了、实际没发出去"这种静默脱节。
    """
    from pciso.compressors import build_compressor
    from pciso.config import SessionConfig

    def compressor(response_format: str) -> object:
        cfg = SessionConfig.from_mapping(
            {
                "prefix_path": str(PREFIX),
                "compression": {
                    "mode": "fact_chain",
                    "model": "some-real-endpoint",
                    "response_format": response_format,
                },
            }
        )
        return build_compressor(cfg)

    assert compressor("json_object").response_format == "json_object"  # type: ignore[attr-defined]
    assert compressor("none").response_format is None  # type: ignore[attr-defined]


def test_json_object_is_actually_sent(tmp_path: Path) -> None:
    """真的进了 payload，而不只是挂在对象属性上。"""
    import os

    import pciso.workmodels as wm
    from pciso.workmodels import ModelClient

    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"ops": []}'}}], "usage": {}}

    class FakeHttp:
        def __enter__(self) -> FakeHttp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, headers: dict | None = None, json: dict | None = None) -> FakeResp:
            captured.update(json or {})
            return FakeResp()

    original = wm.httpx.Client
    wm.httpx.Client = lambda **kw: FakeHttp()  # type: ignore[assignment]
    os.environ["PCISO_TEST_KEY"] = "sk-test"
    try:
        client = ModelClient(
            base_url="https://example.invalid/v1", model="m", api_key_env="PCISO_TEST_KEY"
        )
        client.chat("hi", response_format="json_object")
        assert captured["response_format"] == {"type": "json_object"}
        captured.clear()
        client.chat("hi")
        assert "response_format" not in captured
    finally:
        wm.httpx.Client = original  # type: ignore[assignment]
