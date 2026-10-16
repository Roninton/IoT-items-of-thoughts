"""事实链提取器：把 O_i 变成 ΔC。

**名字与职责要分开读。** 文件名/配置键里的 "compress" 指的是**位置**
（它坐在 O_i 与 C 之间），职责是**从 O_i 里提取事实链**，不是"压缩文本"。
这两者衡量标准不同（无据声明率 vs 体积比）、失败模式不同（丢事实是**错**，不是有损）、
并且提取必须区分来源（server 可验证的 vs 模型声称的）。
完整口径见 README §1.2 —— 按"压缩"的直觉去优化这个组件，方向是错的。

三种模式（对应实验的 compression 配置）：
  off         不提取 —— 由 history_mode=full 直接拼历史，C 只保留验证器写的约束状态
  summary     滚动摘要（E 组）：**唯一一个真的在做"压缩"的模式**，它是本方案的对照
  fact_chain  事实链 + 失败链 + 依赖图（C 组，本方案）

两种实现（对应 compression.model）：
  heuristic   确定性离线：**结构性事实用 AST 抽取，语义性内容用规则抽取**。
              它不是为了冒充模型，而是 stage 0 的离线可跑件；
              同时它给出一条重要的设计结论：能被 AST 抽取的东西，不该花 token 让模型去幻觉。
  <endpoint>  模型提取器：走 build_compressor_prompt，输出结构化 ops。

提取器**不产生约束状态**：那由验证器客观写入。
压缩器可以自评（self_reported），自评与客观结果的偏差本身就是一个指标：
"压缩器的过度乐观程度"。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

from .config import SessionConfig
from .models import Checkpoint, CompressedState, DeltaC, DeltaOp
from .oparse import extract_json_object, extract_prior_heads, parse_o_i
from .pyast import (
    find_test_functions,
    import_targets,
    module_level_public_defs,
    parse_source,
    path_to_module,
)
from .render import build_compressor_prompt, build_thinking_prompt, est_tokens
from .workmodels import ChatResult, ModelClient

# 失败链抽取：明确表达"考虑过 X，因 Y 否决"
_FAIL_RE = re.compile(
    r"(?:曾考虑|试|尝试)(?P<attempt>.{2,60}?)[，,]\s*因(?P<reason>.{2,80}?)(?:而)?(?:否决|排除|放弃)"
)
_NEGATIVE_RE = re.compile(
    r"(?P<attempt>.{2,40}?)(?:方案)?[，,]?\s*(?:被)?否决[，,]?\s*原因(?:是|为)?[：:]?\s*(?P<reason>.{2,80})"
)


class Compressor(Protocol):
    name: str

    def compress(
        self,
        o_i: str,
        *,
        prior_state: CompressedState,
        round_index: int,
        c_max_tokens: int,
        checkpoints: Sequence[Checkpoint] = (),
        landed: Sequence[str] = (),
        thinking: str = "",
    ) -> DeltaC: ...


def _unit_lines(checkpoints: Sequence[Checkpoint]) -> list[str]:
    """给压缩器的单位边界描述。只用 **derived**（可验证）的，declared 仅作参考标注。"""
    lines: list[str] = []
    for cp in checkpoints:
        if cp.source == "derived":
            if cp.kind == "file":
                lines.append(f"[有据] 改完文件 {cp.target}")
            elif cp.kind == "validate":
                lines.append("[有据] 跑过一次验证")
        elif cp.kind == "file":
            mark = "有据" if cp.backed else "**无据声明**"
            lines.append(f"[{mark}] 模型自称改完 {cp.target}")
        elif cp.kind == "note":
            lines.append(f"[不可验证] 调研/结论：{cp.target}")
    return lines


# --------------------------------------------------------------------------- #
# 空压缩器（compression=off）
# --------------------------------------------------------------------------- #


class NullCompressor:
    """off：不压缩。C 只由验证器维护约束状态，事实链交给完整历史。"""

    name = "null"

    def compress(
        self,
        o_i: str,
        *,
        prior_state: CompressedState,
        round_index: int,
        c_max_tokens: int,
        checkpoints: Sequence[Checkpoint] = (),
        landed: Sequence[str] = (),
        thinking: str = "",
    ) -> DeltaC:
        return DeltaC(ops=[], model=self.name)


# --------------------------------------------------------------------------- #
# 滚动摘要（E 组）
# --------------------------------------------------------------------------- #


class SummaryCompressor:
    """滚动摘要：把历史压成一段越来越长的散文。"""

    name = "summary"

    def __init__(self, *, max_chars: int = 1200, client: ModelClient | None = None):
        self.max_chars = max_chars
        self.client = client

    def compress(
        self,
        o_i: str,
        *,
        prior_state: CompressedState,
        round_index: int,
        c_max_tokens: int,
        checkpoints: Sequence[Checkpoint] = (),
        landed: Sequence[str] = (),
        thinking: str = "",
    ) -> DeltaC:
        prior = prior_state.summary.strip()
        parsed = parse_o_i(o_i)
        narrative = parsed.narrative or o_i
        used = ChatResult()
        if self.client is not None:
            body, used = self._model_summary(prior, narrative)
        else:
            body = self._rule_summary(narrative, parsed.paths())

        merged = (prior + "\n" + body).strip() if prior else body
        if len(merged) > self.max_chars:
            merged = merged[-self.max_chars :]
        return DeltaC(
            ops=[DeltaOp(op="SET_PROGRESS", payload={"text": body[:400]})],
            model=self.name,
            est_tokens=est_tokens(body),
            raw=merged,
            prompt_tokens=used.prompt_tokens,
            completion_tokens=used.completion_tokens,
        )

    def _rule_summary(self, narrative: str, paths: list[str]) -> str:
        head = " ".join(narrative.split())[:300]
        return f"轮次产出: {', '.join(paths[:12])}。{head}"

    def _model_summary(self, prior: str, narrative: str) -> tuple[str, ChatResult]:
        prompt = (
            "把下面这段开发草稿压成不超过 120 字的中文摘要，只保留已完成的事与遗留问题。\n"
            f"已有摘要:\n{prior}\n\n本轮草稿:\n{narrative[:20000]}"
        )
        res = self.client.chat(prompt, max_tokens=400, temperature=0.1, thinking=False)
        text = res.text.strip() if not res.error else f"（摘要失败: {res.error}）"
        return text, res


# --------------------------------------------------------------------------- #
# 事实链 + 失败链（C 组）
# --------------------------------------------------------------------------- #


class HeuristicFactChainCompressor:
    """确定性实现：结构靠 AST，语义靠规则。离线可跑，且零幻觉。"""

    name = "heuristic"

    def compress(
        self,
        o_i: str,
        *,
        prior_state: CompressedState,
        round_index: int,
        c_max_tokens: int,
        checkpoints: Sequence[Checkpoint] = (),
        landed: Sequence[str] = (),
        thinking: str = "",
    ) -> DeltaC:
        parsed = parse_o_i(o_i)
        ops: list[DeltaOp] = []
        seen_modules: set[str] = set()

        for fw in parsed.files:
            module = path_to_module(fw.path)
            tree = parse_source(fw.content)
            if tree is None:
                continue

            if "tests" in fw.path.split("/"):
                for tname in find_test_functions(tree):
                    ops.append(
                        DeltaOp(
                            op="ADD_FACT",
                            payload={
                                "statement": f"测试 {tname} 已存在于 {fw.path}",
                                "anchor": {"path": fw.path, "symbol": tname, "kind": "func"},
                                # 测试存在性下一轮用 AST 就能重新看出来 -> 低显著度
                                "salience": 30,
                            },
                        )
                    )
                continue

            for sym in module_level_public_defs(tree):
                target = f"{module}.{sym.name}"
                if sym.kind == "class":
                    stmt = f"类 {target} 已定义"
                else:
                    stmt = f"接口 {target} 已定义 {sym.signature}"
                ops.append(
                    DeltaOp(
                        op="ADD_FACT",
                        payload={
                            "statement": stmt,
                            "anchor": {
                                "path": fw.path,
                                "symbol": sym.name,
                                "kind": "class" if sym.kind == "class" else "func",
                                "signature": sym.signature,
                            },
                            # 接口/类型是"丢了就会做错下一步"的那一类 -> 高显著度
                            "salience": 80,
                        },
                    )
                )

            if module not in seen_modules:
                seen_modules.add(module)
                for tgt in import_targets(tree, module):
                    if tgt.split(".")[0] == module.split(".")[0] and tgt != module:
                        ops.append(
                            DeltaOp(op="ADD_DEP", payload={"src": module, "dst": tgt})
                        )

        # 失败链：明确表达"考虑过 X，因 Y 否决"
        narrative = parsed.narrative
        for regex in (_FAIL_RE, _NEGATIVE_RE):
            for m in regex.finditer(narrative):
                ops.append(
                    DeltaOp(
                        op="ADD_FAILURE",
                        payload={
                            "attempt": " ".join(m.group("attempt").split()),
                            "reason": " ".join(m.group("reason").split()),
                        },
                    )
                )

        first_line = ""
        for line in narrative.splitlines():
            if line.strip() and not line.strip().startswith("#"):
                first_line = " ".join(line.split())
                break
        if first_line:
            ops.append(DeltaOp(op="SET_PROGRESS", payload={"text": first_line}))

        unresolved: list[str] = []
        grab = False
        for line in narrative.splitlines():
            s = line.strip()
            if s.startswith(("未解决", "下一步")):
                grab = True
                continue
            if grab and s.startswith("-"):
                unresolved.append(s.lstrip("- ").strip())
            elif grab and s and not s.startswith("-"):
                break
        if unresolved:
            ops.append(DeltaOp(op="SET_UNRESOLVED", payload={"items": unresolved[:3]}))

        return DeltaC(ops=ops, model=self.name, est_tokens=est_tokens(o_i))


def _to_op(raw_op: Any) -> DeltaOp | None:
    """把模型给出的原始操作规范化。

    模型经常把字段平铺在操作上而不是塞进 payload —— 两种都收，
    否则 salience / statement / anchor 会被静默丢掉。
    非法操作返回 None，由调用方过滤（不抛异常，单条坏操作不该毁掉整轮）。
    """
    if not isinstance(raw_op, dict) or "op" not in raw_op:
        return None
    payload = {k: v for k, v in raw_op.items() if k not in ("op", "target", "payload")}
    payload.update(raw_op.get("payload") or {})
    try:
        return DeltaOp(op=raw_op["op"], target=raw_op.get("target"), payload=payload)
    except (TypeError, ValueError):
        return None


# 输出被 max_tokens 硬截断时的重试提示。**必须显式重试**：截断的 JSON 断在半句上，
# 解析必然失败；而失败如果是安静的，一整轮的 ΔC 就凭空消失了（真机实测两轮）。
RETRY_NOTE = (
    "\n\n注意：上一次输出被长度上限**硬截断**了，JSON 断在半句上，那一轮的事实全部丢失。"
    "这次只输出一个 JSON 对象，不要任何解释文字；每条事实的 statement 用一句话写短，"
    "能从代码里重新看出来的、重复的，一律不要写。宁少勿多。\n"
)


class ModelFactChainCompressor:
    """模型压缩器：输出结构化 ops，不写散文。"""

    name = "model"

    def __init__(
        self,
        *,
        client: ModelClient,
        thinking: bool = False,
        max_tokens: int = 1000,
        response_format: str | None = "json_object",
    ):
        self.client = client
        self.thinking = thinking
        self.max_tokens = max_tokens
        # "none"/"off"/"" 都表示"不发 response_format"。
        # 注意不能用 `response_format or None`：字符串 'none' 是真值，会被当成要发出去。
        self.response_format = (
            None if response_format in (None, "", "none", "off") else response_format
        )

    def _text(self, o_i: str, thinking: str) -> str:
        """送进提取器的原文。默认是整轮 O_i；THINK 档由子类改成 thinking 块。"""
        return o_i

    def _make_prompt(
        self,
        text: str,
        *,
        round_index: int,
        prior_nodes: Sequence[str],
        prior_failures: Sequence[str],
        c_max_tokens: int,
        units: Sequence[str],
        landed: Sequence[str],
        out_budget: int,
        note: str,
    ) -> str:
        return (
            build_compressor_prompt(
                o_i=text,
                round_index=round_index,
                prior_nodes=prior_nodes,
                prior_failures=prior_failures,
                c_max_tokens=c_max_tokens,
                units=units,
                out_max_tokens=out_budget,
                landed=landed,
            )
            + note
        )

    def compress(
        self,
        o_i: str,
        *,
        prior_state: CompressedState,
        round_index: int,
        c_max_tokens: int,
        checkpoints: Sequence[Checkpoint] = (),
        landed: Sequence[str] = (),
        thinking: str = "",
    ) -> DeltaC:
        text = self._text(o_i, thinking)
        if not text.strip():
            # 没有可编排的输入（例如这一轮模型没产出 thinking）。空输入 -> 空 ΔC 是**正确**结果，
            # 不是失败：所以 error 保持 None，别让它看起来像"压缩器坏了"。
            return DeltaC(ops=[], model=self.name)
        nodes, fails = extract_prior_heads(prior_state)

        def _mk(out_budget: int, note: str = "") -> str:
            # 提示词里报的预算必须是**这次调用真实的上限**（见 render.build_compressor_prompt）。
            return self._make_prompt(
                text,
                round_index=round_index,
                prior_nodes=nodes,
                prior_failures=fails,
                c_max_tokens=c_max_tokens,
                units=_unit_lines(checkpoints),
                landed=landed,
                out_budget=out_budget,
                note=note,
            )

        budget = self.max_tokens
        prompt = _mk(budget)
        retried = False
        res = None
        data = None
        for attempt in (1, 2):
            res = self.client.chat(
                prompt,
                max_tokens=budget,
                temperature=0.0,
                thinking=self.thinking,
                response_format=self.response_format,
            )
            if res.error:
                return DeltaC(
                    ops=[],
                    model=self.name,
                    error=res.error,
                    raw="",
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    cached_tokens=res.cached_tokens,
                )
            # 截断的判据是**真实用量顶到上限**：completion_tokens == max_tokens 时
            # finish_reason 必然是 length（ChatResult 不带 finish_reason，用量是等价信号）。
            cut = bool(budget) and res.completion_tokens >= budget
            blob = extract_json_object(res.text)
            parse_error: str | None = None
            if blob is None:
                parse_error = "压缩器未输出可解析的 JSON"
            else:
                try:
                    data = json.loads(blob)
                except json.JSONDecodeError as exc:
                    data = None
                    parse_error = f"JSON 解析失败: {exc}"
            if data is not None and not cut:
                break
            why = parse_error or f"输出被 max_tokens={budget} 截断"
            if cut and parse_error:
                # 说清"为什么解析失败"：用量顶到上限 => 是截断，不是模型拒绝输出。
                why += f"（用量顶到 max_tokens={budget}，是截断导致）"
            if attempt == 2:
                # **绝不安静地丢掉一整轮**：宁可让这轮带一条错误记录，
                # 也不要返回 ops=[] 且 error=None 的 ΔC —— 那看起来跟"这轮没什么可记的"一模一样，
                # 而实际后果是那一轮的成果永远没进 C，下一轮只能重写。
                return DeltaC(
                    ops=[],
                    model=self.name,
                    truncated=cut,
                    retried=True,
                    error=f"{why}（重试一次后仍失败，预算 {budget}；本轮 ΔC 丢失）",
                    raw=res.text[:4000],
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    cached_tokens=res.cached_tokens,
                )
            budget = max(self.max_tokens * 2, 2400)
            prompt = _mk(budget, RETRY_NOTE)
            retried = True

        ops: list[DeltaOp] = []
        malformed = 0
        for raw_op in (data or {}).get("ops") or []:
            op = _to_op(raw_op)
            if op is None:
                malformed += 1
            else:
                ops.append(op)

        truncated = est_tokens(res.text) > c_max_tokens
        return DeltaC(
            ops=ops,
            model=self.name,
            est_tokens=res.completion_tokens or est_tokens(res.text),
            truncated=truncated,
            retried=retried,
            raw=res.text[:4000],
            error=(f"{malformed} 条操作不合法（已丢弃）" if malformed and not ops else None),
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            cached_tokens=res.cached_tokens,
        )


class ThinkingNodeCompressor(ModelFactChainCompressor):
    """推理编排器：只吃**一个 thinking 块**，产出正负推理节点（README §1.3）。

    与父类（整轮 O_i 提取器）的三点不同：

    1. **输入是 thinking，不是 O_i。** 正文与代码块不进这里 —— 已发生的动作由 server 的
       ACT 节点承担，尚未发生的正文留在窗口里。用户口径：只压 thinking 块，
       "全部压缩可能会导致失真、误判、丢失细节"。
    2. **提示词换成 build_thinking_prompt**：丢掉 `let me…/嗯…/用户想要我…` 这类填充与自我对话，
       留下正负推理节点（负节点更贵：它拦住的是"重走已否掉的路"）。
    3. **空 thinking → 空 ΔC**（`_text` 把整轮 O_i 换成 thinking）。
    """

    name = "thinking"

    def _text(self, o_i: str, thinking: str) -> str:
        return thinking

    def _make_prompt(
        self,
        text: str,
        *,
        round_index: int,
        prior_nodes: Sequence[str],
        prior_failures: Sequence[str],
        c_max_tokens: int,
        units: Sequence[str],
        landed: Sequence[str],
        out_budget: int,
        note: str,
    ) -> str:
        return (
            build_thinking_prompt(
                thinking=text,
                round_index=round_index,
                prior_nodes=prior_nodes,
                c_max_tokens=c_max_tokens,
                out_max_tokens=out_budget,
                landed=landed,
                act_units=units,
            )
            + note
        )


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def build_compressor(cfg: SessionConfig) -> Compressor:
    mode = cfg.compression.mode
    if mode == "off":
        return NullCompressor()
    if mode == "summary":
        client = _maybe_client(cfg)
        return SummaryCompressor(client=client)
    if mode == "fact_chain":
        if cfg.compression.model in ("heuristic", "fake", "oracle"):
            return HeuristicFactChainCompressor()
        client = _client(cfg)
        cls = ThinkingNodeCompressor if cfg.compression.scope == "thinking" else ModelFactChainCompressor
        return cls(
            client=client,
            thinking=cfg.compression.thinking,
            max_tokens=cfg.compression.max_tokens,
            response_format=cfg.compression.response_format,
        )
    raise ValueError(f"未知压缩模式 {mode!r}")  # pragma: no cover


def _client(cfg: SessionConfig) -> ModelClient:
    endpoint = cfg.compression.model
    base_url = cfg.work_model.base_url or "https://api.deepseek.com/v1"
    model = cfg.work_model.model
    api_key_env = cfg.work_model.api_key_env
    # 允许 "base_url|model|api_key_env" 形式为压缩器单独指定端点
    if "|" in endpoint:
        parts = endpoint.split("|")
        base_url = parts[0] or base_url
        model = parts[1] if len(parts) > 1 and parts[1] else model
        api_key_env = parts[2] if len(parts) > 2 and parts[2] else api_key_env
    elif endpoint not in ("", "default"):
        model = endpoint
    return ModelClient(base_url=base_url, model=model, api_key_env=api_key_env)


def _maybe_client(cfg: SessionConfig) -> ModelClient | None:
    if cfg.compression.model in ("heuristic", "fake", "oracle", ""):
        return None
    return _client(cfg)
