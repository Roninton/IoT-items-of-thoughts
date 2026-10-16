"""P / C / O_i / ΔC 的数据契约。

这里是整个实验的"照妖镜"：任何语义含糊都会在这一层暴露。
关键裁定（写死在类型里，不靠约定）：

1. O_i 是工作模型的**单发原始输出文本**，不含工具调用轨迹。
   server 负责把其中的 file 块落盘，然后跑验证器。
2. C 里的每个事实节点**必须挂机器可解的锚点** (path, symbol)。
   解析不到的锚点 -> 节点降级为 unanchored，可溯源率据此客观计数，幻觉不再是"回退"而是"丢弃"。
3. ΔC 是**结构化操作**，不是散文。压缩器只声明意图，合并器只做机械应用。
   这样"更正传播"才是确定性代码能做到的事。
4. 约束文本 verbatim 来自 P，C 只能改 status/evidence。改文本 = 违约。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

FactStatus = Literal["active", "superseded", "invalidated"]
ConstraintState = Literal["satisfied", "partial", "violated", "unverified"]
HistoryMode = Literal["full", "summary", "state", "none"]
CompressionMode = Literal["off", "summary", "fact_chain"]
JudgementDecision = Literal["accept", "revert"]
AnchorKind = Literal["file", "func", "class", "method", "attr", "auto"]

OpKind = Literal[
    "ADD_FACT",
    "SUPERSEDE_FACT",
    "INVALIDATE_FACT",
    "ADD_FAILURE",
    "SET_PROGRESS",
    "SET_UNRESOLVED",
    "SET_CONSTRAINT",
    "ADD_DEP",
    "DROP_DEP",
]

# 违反即回退的硬约束类别（validator 用它决定 revert）
HARD_CHECK_IDS = frozenset(
    {
        "frozen_tests",
        "interface_contract",
        "import_cycles",
        "layer_violation",
        "parse_output",
    }
)


# --------------------------------------------------------------------------- #
# 检查点（Checkpoint）—— 注意：它**不是** Anchor
# --------------------------------------------------------------------------- #
# Anchor    = 一条事实的**机器可解出处** (path, symbol)。要求可被 AST 解析验证。
# Checkpoint= O_i 内部的一个**工作单位边界**（改完一个文件 / 跑过一次验证 / 一次调研）。
#
# 关键设计：边界分两种来源，可信度天差地别。
#   derived  由 server 从**可验证的状态变化**推导（文件 sha256、验证器结果）-> 高可信
#   declared 由工作模型自己声明（@@UNIT 标记）-> 低可信，**只用于测量，不用于控制**
#
# 为什么模型声明的边界不能用来控制压缩：
#   整个平台的前提就是不再相信模型的自我陈述（所以才要客观验证器）。
#   如果让模型决定"何时切分/何时压缩"，压缩时机就变成模型相关的变量，
#   实验的自变量被污染；而且它会自然倾向于宣称"我完成了"。
# 但它作为**因变量**极有价值：宣称完成却没有对应状态变化 = 自我报告注水率，可客观计数。
CheckpointKind = Literal["file", "validate", "note"]


class Checkpoint(BaseModel):
    kind: CheckpointKind
    target: str = ""
    source: Literal["declared", "derived"] = "declared"
    # True=有据可查；False=宣称但无对应变化；None=不可验证（如一次调研）
    backed: bool | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 锚点 / 事实链 / 失败链
# --------------------------------------------------------------------------- #


class Anchor(BaseModel):
    """事实节点的机器可解锚点。validator 会在真实工作区里解析它。"""

    path: str
    symbol: str | None = None
    kind: AnchorKind = "auto"
    signature: str | None = None
    resolved: bool | None = None
    detail: str = ""
    # 解析失败时区分两种情形（渲染成两个不同的标记）：False = 文件不在（`[未落地]`），
    # True = 文件在盘上、但没有这个符号名（`[符号未见]`）。真机自述（会话
    # 5f24337359ac4c39）把后者读成"计划过但没落地"，于是以为测试用例没写进文件。
    symbol_missing: bool = False

    def key(self) -> str:
        return f"{self.path}::{self.symbol or ''}"


class FactNode(BaseModel):
    id: str
    statement: str
    deps: list[str] = Field(default_factory=list)
    anchor: Anchor | None = None
    source_round: int = 0
    status: FactStatus = "active"
    superseded_by: str | None = None
    unanchored: bool = False  # 锚点解析失败 / 压缩器未给锚点
    # 节点类别（README §1.3 的压缩范围裁定）：
    #   fact   整轮 O_i 提取出来的事实（七个实验臂的口径）
    #   act    server 从 telemetry 确定性渲染的**动作事实**（谁写了哪个文件、sha、退出码）
    #   think  模型把 thinking 块压出来的**关键推理节点**
    node_kind: str = "fact"
    # 只对 node_kind == "think" 有意义：`+` = 被采纳的路线与它为什么成立，
    # `-` = 被否掉的路线与它为什么被否。负节点是长程任务里最贵的一类事实
    # （它拦住的是"重新走一遍已经否掉的路"），裁剪时给它保底 —— 见 merge._trim_to_budget。
    polarity: str = ""
    # 只对 node_kind == "think" 有意义：这条推理**定了没有**。
    #   settled  已经定下来的结论
    #   open     还在犹豫/待确认（真机自述第 2 条：模型把"要不要测 True"的摆动
    #            写成"覆盖 True"，下一轮读到的是一条比事实更硬的结论 —— 过度确定
    #            比缺失更危险，因为它不会被重新检查）
    # 渲染成正文里的 `⟨待定⟩`，让下一轮一眼看出这条不能当依据用。
    assertion: str = "settled"
    # **依据**。提示词从第一版起就在要 `why`（"写清选了什么/否了什么 + 依据"），
    # 而服务端从来没有这个字段 —— 它被静默吃掉了。后果正对着模型自述的扣分项：
    # "只知道结论，不知道当初为什么这么定"，于是同一个权衡下一轮会重走一遍。
    why: str = ""
    # **已知风险**（决定可以是已定的、同时又带一个隐患）："无 __init__.py 时
    # unittest discover 可能递归不进 tools/" 这类。它是第三种状态 ——
    # 既不是"没定"（assertion=open），也不是"定得很干净"。裁剪时同样保底。
    risk: str = ""
    # 显著度 0-100：预算不够时**按它**从低到高丢弃，而不是按新旧。
    # 一轮可能超长（O_i 顶满 max_tokens），压缩器输出的 ΔC 就可能超预算；
    # 此时"丢最不重要的"远优于"丢最老的"。
    salience: int = 50

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.fullmatch(r"N\d+", v):
            raise ValueError(f"事实节点 id 必须是 N<数字>，收到 {v!r}")
        return v

    @field_validator("assertion")
    @classmethod
    def _assertion_shape(cls, v: str) -> str:
        """只认 `settled` / `open`：压缩器写出别的词（"maybe"/"unknown"）一律当 settled。

        刻意**不**对未知值报错：压缩器是模型，写错一个词就整批 ΔC 被拒收的代价
        远大于把它当默认值（真机里 ΔC 丢一轮 = 一整轮的成果不进 C，下一轮从头再来）。
        """
        return "open" if str(v).strip().lower() == "open" else "settled"


class FailureEntry(BaseModel):
    """被否决的设计方案。只留否决理由 —— 这是相对 Self-Refine 的实质增量。

    `source` 把来源分开：`extractor` = 提取器从 O_i 里读出来的；
    `server` = server 客观判定的（例如"写 ../escape.py，因越界被拒"）。
    按事实链的口径来源必须可区分，否则会把"server 确定的事实"和
    "模型声称的结论"混成一锅。
    """

    id: str
    attempt: str
    reason: str
    source: str = "extractor"  # extractor | server
    round: int = 0
    round_index_seen: list[int] = Field(default_factory=list)  # 重复尝试检测

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.fullmatch(r"F\d+", v):
            raise ValueError(f"失败链 id 必须是 F<数字>，收到 {v!r}")
        return v

    def signature(self) -> str:
        """用于重复尝试检测的规范化指纹。"""
        norm = lambda s: re.sub(r"\s+", "", s.lower())  # noqa: E731
        return f"{norm(self.attempt)}|{norm(self.reason)}"


class ConstraintStatus(BaseModel):
    id: str
    text: str  # verbatim 来自 P；C 不得修改
    status: ConstraintState = "unverified"
    evidence: str = ""
    violated_rounds: list[int] = Field(default_factory=list)


class DependencyEdge(BaseModel):
    src: str
    dst: str
    kind: str = "import"


# --------------------------------------------------------------------------- #
# 循环状态 C
# --------------------------------------------------------------------------- #


class CompressedState(BaseModel):
    version: int = 0
    fact_chain: list[FactNode] = Field(default_factory=list)
    failure_chain: list[FailureEntry] = Field(default_factory=list)
    progress: str = ""
    unresolved: list[str] = Field(default_factory=list)
    constraints: list[ConstraintStatus] = Field(default_factory=list)
    deps: list[DependencyEdge] = Field(default_factory=list)
    summary: str = ""  # 仅 history_mode=summary 使用
    est_tokens: int = 0
    truncated: bool = False

    # -- 视图 ------------------------------------------------------------- #

    def active_facts(self) -> list[FactNode]:
        return [f for f in self.fact_chain if f.status == "active"]

    def fact(self, fid: str) -> FactNode | None:
        return next((f for f in self.fact_chain if f.id == fid), None)

    def next_fact_id(self) -> str:
        nums = [int(f.id[1:]) for f in self.fact_chain if re.fullmatch(r"N\d+", f.id)]
        return f"N{max(nums, default=0) + 1}"

    def next_failure_id(self) -> str:
        nums = [int(f.id[1:]) for f in self.failure_chain if re.fullmatch(r"F\d+", f.id)]
        return f"F{max(nums, default=0) + 1}"

    def anchor_resolvable_rate(self) -> float | None:
        anchored = [f for f in self.fact_chain if f.anchor is not None]
        if not anchored:
            return None
        ok = [f for f in anchored if f.anchor and f.anchor.resolved]
        return len(ok) / len(anchored)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# ΔC：结构化操作
# --------------------------------------------------------------------------- #


class DeltaOp(BaseModel):
    op: OpKind
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DeltaC(BaseModel):
    """一轮的**事实链增量**。

    不是"O_i 的压缩版"：它是提取出来的事实/失败/依赖操作序列，
    每一条都能被验证（锚点可解析）或被推翻（SUPERSEDE / INVALIDATE）。
    所以它的质量看**无据声明率与作废正确性**，不看体积比 —— 见 README §1.2。
    """

    ops: list[DeltaOp] = Field(default_factory=list)
    model: str = "heuristic"
    est_tokens: int = 0
    truncated: bool = False
    # 因为输出被 max_tokens 硬截断而**重试过一次**（重试成功也置 True）。
    # 它不表示失败，表示"这一轮碰到过截断风险" —— 压缩器预算是本方案的主要成本项，
    # 这种风险必须能在事后数据里被数出来，否则"截断"永远是隐形的。
    retried: bool = False
    raw: str = ""
    error: str | None = None
    # 压缩器这一次调用的真实用量。必须由压缩器自己带上：压缩开销是本方案的**主要成本项**，
    # 如果只在 usage 里留着字段却没人写，"压缩器开销"会永远显示 0 ——
    # 那等于在成本表里把方案的最大代价抹掉，而且不会有任何报错。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 这次压缩器调用**命中前缀缓存**的输入 token 数（API 的 prompt_cache_hit_tokens）。
    # 只记总量、不记命中量，等于把成本模型砍掉一半。
    cached_tokens: int = 0

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for op in self.ops:
            counts[op.op] = counts.get(op.op, 0) + 1
        body = ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())) or "empty"
        return f"ΔC[{self.model}] {body} tokens={self.est_tokens}" + (
            " TRUNCATED" if self.truncated else ""
        ) + (" RETRIED" if self.retried else "")


class MergeReport(BaseModel):
    applied: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    correction_cascades: list[str] = Field(default_factory=list)
    dangling_deps: list[str] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    duplicate_attempts: list[str] = Field(default_factory=list)
    constraint_text_mutations: list[str] = Field(default_factory=list)
    trimmed: list[str] = Field(default_factory=list)

    def dirty(self) -> bool:
        return bool(
            self.rejected
            or self.conflicts
            or self.cycles
            or self.constraint_text_mutations
        )


# --------------------------------------------------------------------------- #
# 验证
# --------------------------------------------------------------------------- #


class CheckResult(BaseModel):
    id: str
    constraint_id: str | None = None
    passed: bool | None = None  # None = 工具缺失 / 不适用
    count: int | None = None  # 客观计数（违反数）
    detail: str = ""
    findings: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    round_index: int = 0
    checks: list[CheckResult] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)  # constraint ids
    errors: list[str] = Field(default_factory=list)

    def check(self, cid: str) -> CheckResult | None:
        return next((c for c in self.checks if c.id == cid), None)

    def violation_count(self) -> int:
        """P0 核心指标的分子：客观计数之和（不是"哪几条红了"）。"""
        return sum(c.count or 0 for c in self.checks if c.passed is False)

    def fresh_violations(self, prev: ValidationResult | None) -> list[str]:
        before = set(prev.violations) if prev else set()
        return [v for v in self.violations if v not in before]


class RoundJudgement(BaseModel):
    decision: JudgementDecision = "accept"
    reasons: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    reverted_files: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 计量
# --------------------------------------------------------------------------- #


class Usage(BaseModel):
    """名义 token 会骗人 —— 同时记录 API 返回的 cached_tokens。

    ⚠️ 加字段时**三处都要改**：这里、`merge()`、以及 session.py 里 `_accumulate_usage`
    的调用点。少改一处不会报错 —— `_accumulate_usage` 遇到不认识的键会直接丢掉
    （实测：`compressor_cached_tokens` 就这么被吞了两轮，报告里显示 0 命中）。
    """

    work_prompt_tokens: int = 0
    work_completion_tokens: int = 0
    work_cached_tokens: int = 0
    compressor_prompt_tokens: int = 0
    compressor_completion_tokens: int = 0
    compressor_cached_tokens: int = 0
    est_prompt_tokens: int = 0

    def total(self) -> int:
        return (
            self.work_prompt_tokens
            + self.work_completion_tokens
            + self.compressor_prompt_tokens
            + self.compressor_completion_tokens
        )

    def merge(self, other: Usage) -> Usage:
        return Usage(
            work_prompt_tokens=self.work_prompt_tokens + other.work_prompt_tokens,
            work_completion_tokens=self.work_completion_tokens
            + other.work_completion_tokens,
            work_cached_tokens=self.work_cached_tokens + other.work_cached_tokens,
            compressor_prompt_tokens=self.compressor_prompt_tokens
            + other.compressor_prompt_tokens,
            compressor_completion_tokens=self.compressor_completion_tokens
            + other.compressor_completion_tokens,
            compressor_cached_tokens=self.compressor_cached_tokens
            + other.compressor_cached_tokens,
            est_prompt_tokens=self.est_prompt_tokens + other.est_prompt_tokens,
        )


# --------------------------------------------------------------------------- #
# 工作区产物
# --------------------------------------------------------------------------- #


class FileWrite(BaseModel):
    path: str
    content: str
    sha256: str = ""
    bytes: int = 0

    @classmethod
    def make(cls, path: str, content: str) -> FileWrite:
        blob = content.encode("utf-8")
        return cls(
            path=path,
            content=content,
            sha256=hashlib.sha256(blob).hexdigest(),
            bytes=len(blob),
        )


class ArtifactIndex(BaseModel):
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    hashes: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 请求 / 响应
# --------------------------------------------------------------------------- #


class SessionCreateRequest(BaseModel):
    project: str = "drawing_video_toolchain"
    prefix_path: str
    config_path: str | None = None
    config: dict[str, Any] | None = None
    # 在已加载配置之上做深合并覆盖。这是"切换模型只改一个字段"的落地方式，
    # 也让前端可以在不复制整份配置的情况下切到脚本 oracle 排练。
    overrides: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None
    seed: int | None = None
    arm: str | None = None


class SessionCreateResponse(BaseModel):
    session_id: str
    project: str
    arm: str | None = None
    prefix_hash: str
    max_rounds: int
    stages: list[str] = Field(default_factory=list)
    history_mode: HistoryMode = "state"
    # 把工作区明说出来：前端要能显示"这一轮的文件写到哪去了"，
    # 尤其是在脚本 oracle 被守卫清空 workspace_path 之后。
    workspace: str = ""
    workspace_is_deliverable: bool = False


class RoundRequest(BaseModel):
    """o_i 可选 —— 这是消融/回放路径与产品路径的统一 seam。

    缺省: server 自己调工作模型（产品路径）
    传入: 直接用（回放、注入、离线复现）
    """

    o_i: str | None = None
    round_index: int | None = None  # 期望轮次（幂等断言），不匹配 -> 409
    stage: int | None = None
    extra_instruction: str | None = None
    dry_run: bool = False  # 只组装 prompt，不调模型、不提交


class RoundResponse(BaseModel):
    session_id: str
    round_index: int
    stage: int
    committed: bool
    prompt: str = ""
    prompt_tokens_est: int = 0
    o_i: str = ""
    delta_c: DeltaC
    c_i: CompressedState
    merge_report: MergeReport
    validation: ValidationResult
    judge: RoundJudgement
    next_prompt: str = ""
    usage: Usage = Field(default_factory=Usage)
    artifacts: ArtifactIndex = Field(default_factory=ArtifactIndex)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class SessionStateResponse(BaseModel):
    session_id: str
    project: str
    arm: str | None = None
    prefix_hash: str
    round_index: int
    status: str
    c_current: CompressedState
    fact_chain: list[FactNode]
    failure_chain: list[FailureEntry]
    dependency_graph: list[DependencyEdge]
    constraint_status: list[ConstraintStatus]
    metrics: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 聊天视图（前端用）
# --------------------------------------------------------------------------- #
# 设计要点：**隐藏的是被压缩掉的东西，不是"给用户看的部分"**。
#   - 隐藏：思考过程、工具/检索往返、文件内容、ΔC 原始文本 —— 这些是压缩器的输入，
#           也是 C 的替代对象。界面上只暴露它们的**体量**（多少字、几条操作）。
#   - 可见：本轮回复（O_i 的自由叙述）、改了哪些文件、判定结果、C 的大小。
# 审计视图（include_hidden=true）会给出全文，但那是**给人看的**，不会回流进极简模式。


class HiddenSummary(BaseModel):
    """被压缩掉的体量。只给数字，不给内容。"""

    thinking_chars: int = 0
    thinking_fallback: bool = False
    file_content_chars: int = 0
    delta_ops: int = 0
    delta_raw_chars: int = 0
    prompt_chars: int = 0
    retrieval_rounds: int = 0


class AskRequest(BaseModel):
    """只对话：给当前 C，拿一段回答。不推进、不写文件、不提交。"""

    message: str = ""
    # 0 = 跟会话配置走（C 组是 32000）。不写死一个小值是因为
    # "思考吃光预算、正文为空"这个失败模式正是小预算造成的。
    max_tokens: int = 0
    # 不传 = 跟会话配置走（C 组 thinking 开）。思考开不开**不影响可见性**：
    # 聊天窗口只显示正文，思考既不给内容也不给字数。
    thinking: bool | None = None


class AskResponse(BaseModel):
    """检视通道的回复。

    它不是循环的一部分：**不产生 ΔC、不落 rounds.jsonl、C 不前进、工作区不动**，
    所以它不会污染实验轨迹。存在的理由是"人要看一眼当前压缩状态是否说得通" ——
    跟审计视图同一类东西。每一轮推进那种 1~2 分钟的开销不该用来回答一句"你好"。
    """

    reply: str
    # 思考被当成正文回退过。这条路径**不把思考摊进窗口**：回退发生时 reply
    # 会是一句"没写正文"，而不是把思考当成回答。
    thinking_fallback: bool = False
    thinking_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    c_tokens: int = 0
    rounds_done: int = 0
    elapsed_s: float = 0.0
    facts: int = 0
    failures: int = 0
    error: str | None = None


class SayRequest(BaseModel):
    message: str = ""
    stage: int | None = None
    include_hidden: bool = False


class SayResponse(BaseModel):
    session_id: str
    round_index: int
    stage: int
    reply: str
    reply_kind: Literal["narrative", "empty", "thinking_fallback"]
    files_changed: list[str] = Field(default_factory=list)
    verdict: str = "accept"
    verdict_reasons: list[str] = Field(default_factory=list)
    constraints_satisfied: list[str] = Field(default_factory=list)
    constraints_violated: list[str] = Field(default_factory=list)
    c_tokens: int = 0
    prompt_tokens: int = 0
    hidden: HiddenSummary = Field(default_factory=HiddenSummary)
    upstream: dict[str, Any] = Field(default_factory=dict)
    hidden_detail: dict[str, Any] | None = None
    # 本轮工作模型的真实用量（不是字符数估计）—— `/v1` 用它填 OpenAI 的 `usage`。
    usage: Usage | None = None
    # 本轮的思考原文（落盘在 state/sessions/<sid>/thinking/<n>.txt）。
    # 客户端要就给它当 `reasoning_content`；不回流进上下文。
    thinking: str = ""
