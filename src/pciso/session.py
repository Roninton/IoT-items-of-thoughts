"""会话编排 —— 一轮的完整流水线。

    prompt 组装
      -> 工作模型产出 O_i（单发，无工具）
      -> 快照
      -> 解析 O_i 并落盘
      -> 冻结测试篡改检测（**恢复之前**测，否则测不出来）
      -> 恢复冻结测试
      -> 客观验证
      -> 压缩器产出 ΔC
      -> 确定性合并 + 更正级联
      -> 锚点解析（结构性事实零幻觉的落地）
      -> 验证器客观覆写约束状态
      -> 判定 accept / revert
      -> git 提交或回退工作区
      -> 落轨迹日志

轮次幂等：`round_index` 是 `rounds.jsonl` 的行数，追加即提交。
传 `expected_index` 不匹配就 409 —— 5 组 × 8 轮真实调用，中途必挂，必须能续跑。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .actnodes import (
    describe_verification,
    filter_think_ops,
    item_line,
    one_line_overview,
    think_anchor_paths,
    with_act_nodes,
)
from .checks import (
    CheckContext,
    check_frozen_tests,
    frozen_manifest,
    run_validator,
    take_snapshot,
)
from .compressors import build_compressor
from .config import SessionConfig
from .merge import apply_delta, judge_round
from .models import (
    ArtifactIndex,
    AskResponse,
    Checkpoint,
    CheckResult,
    CompressedState,
    ConstraintStatus,
    DeltaC,
    DeltaOp,
    MergeReport,
    RoundJudgement,
    RoundRequest,
    RoundResponse,
    SayResponse,
    SessionCreateRequest,
    SessionStateResponse,
    Usage,
    ValidationResult,
)
from .oparse import parse_declared_checkpoints, parse_o_i, parse_read_requests
from .prefix import PrefixBundle
from .pyast import module_level_names, parse_file, path_to_module, symbols_of
from .render import (
    READ_RESULT_HEADER,
    build_prompt,
    est_tokens,
    render_ask_prompt,
    render_state_block,
    render_workspace_manifest,
)
from .store import SessionStore, new_session_id
from .workmodels import ModelClient, build_work_model


class SessionConflict(Exception):
    """请求与当前会话状态冲突（轮次错位 / P 被改动 / 超出轮数）。"""


def _accumulate_usage(usage: Usage, delta: dict[str, int]) -> None:
    """多趟调用要**累加**而不是覆盖 —— 检索往返会让一次 round 有 2 次以上模型调用。

    键必须是 `Usage` 的字段：不认识就**当场报错**，绝不静默丢掉。
    静默丢弃已经坑过一次 —— `compressor_cached_tokens` 被吞掉，报告里显示"压缩器 0 命中"，
    而它真实是否命中根本没人知道（幸好这次逮到了）。
    """
    for key, value in (delta or {}).items():
        if not hasattr(usage, key):
            raise ValueError(
                f"Usage 没有字段 {key!r}：新开销字段要同时加到 models.Usage 和 Usage.merge()"
            )
        setattr(usage, key, getattr(usage, key) + int(value))


def _est_c_tokens(state: CompressedState) -> int:
    return est_tokens(render_state_block(state))


def _rejected_attempt(error: str) -> str:
    """从 parse 错误里提取出"尝试了什么"，用于失败链的 attempt 字段。

    形如 `非法路径 '../escape.py': 不允许 ..` -> `写 ../escape.py`
    """
    match = re.search(r"'([^']+)'", error)
    target = match.group(1) if match else "未知路径"
    if error.startswith("空文件块"):
        return f"写空文件 {target}"
    return f"写 {target}"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并覆盖层；两边都是 dict 才下钻，否则覆盖层赢。"""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# 轮次记录 -> 聊天视图
# --------------------------------------------------------------------------- #


def turn_view(record: dict[str, Any], cfg: SessionConfig) -> dict[str, Any]:
    """把一个已提交的轮次记录投影成聊天视图。

    隐藏面的定义在这里落地：思考、检索往返、文件内容、ΔC 原文都**只给体量**。
    这不是 UI 偏好 —— 那些东西正是被压缩掉的对象；把它们显示出来（更糟：回流进
    下一轮上下文）就不是压缩，是转录。
    """
    o_i = record.get("o_i", "")
    parsed = parse_o_i(o_i, require_files=not cfg.chat_driven)
    telemetry = record.get("telemetry", {}) or {}

    reply = parsed.narrative.strip()
    if telemetry.get("thinking_fallback"):
        kind = "thinking_fallback"
    elif reply:
        kind = "narrative"
    else:
        kind = "empty"

    constraints = (record.get("c_i", {}) or {}).get("constraints", []) or []
    violated = [c["id"] for c in constraints if c.get("status") == "violated"]
    satisfied = [c["id"] for c in constraints if c.get("status") == "satisfied"]

    return {
        "session_id": "",
        "round_index": int(record.get("round_index", 0)),
        "stage": int(record.get("stage", 0)),
        "reply": reply,
        "reply_kind": kind,
        "files_changed": list(telemetry.get("files_written") or []),
        "verdict": record.get("judgement", "accept"),
        "verdict_reasons": list(record.get("judgement_reasons") or []),
        "constraints_satisfied": satisfied,
        "constraints_violated": violated,
        "c_tokens": int(record.get("c_tokens_est", 0)),
        "prompt_tokens": int(record.get("prompt_tokens_est", 0)),
        "hidden": {
            "thinking_chars": int(telemetry.get("reasoning_chars") or 0),
            "thinking_fallback": bool(telemetry.get("thinking_fallback")),
            "file_content_chars": sum(len(f.content) for f in parsed.files),
            "delta_ops": len((record.get("delta_c") or {}).get("ops") or []),
            "delta_raw_chars": len((record.get("delta_c") or {}).get("raw") or ""),
            "prompt_chars": len(record.get("prompt", "")),
            "retrieval_rounds": max(0, int(telemetry.get("work_calls") or 1) - 1),
        },
        "upstream": {
            "work_model": cfg.work_model.endpoint,
            "model": cfg.work_model.model,
            "thinking": cfg.work_model.thinking,
            "compression": cfg.compression.mode,
            "compressor": cfg.compression.model,
            "history_mode": cfg.resolved_history_mode(),
            "is_fake": cfg.work_model.endpoint in ("fake", "oracle"),
        },
    }


# --------------------------------------------------------------------------- #
# 锚点解析
# --------------------------------------------------------------------------- #


def resolve_anchors(workspace: Path, state: CompressedState) -> dict[str, Any]:
    """在真实代码里解析事实节点锚点。

    这是"事实链不得幻觉"的落地方式：**解析不到就丢弃，而不是再问模型一次。**
    顺带产出 `可溯源率`，它与"依赖图与真实代码一致性"一起构成压缩质量的客观指标。
    """
    unresolved: list[str] = []
    newly_broken: list[str] = []
    checked = 0

    for node in state.fact_chain:
        if node.anchor is None:
            node.unanchored = True
            unresolved.append(f"{node.id}(无锚点)")
            continue
        checked += 1
        target = workspace / node.anchor.path
        if not target.is_file():
            node.anchor.resolved = False
            node.anchor.detail = "文件不存在"
            node.unanchored = True
            unresolved.append(f"{node.id}(文件缺失 {node.anchor.path})")
            continue
        if node.anchor.symbol and not node.anchor.path.endswith(".py"):
            # 符号锚点只对 Python 文件有意义。压缩器给 markdown 编过符号
            # （`README.md::drawvid`、`docs/ARCHITECTURE.md::架构`）—— 那种符号**永远**解析不到，
            # 于是每一份真实存在的文档都被永久标成"未落地"（detail='符号不存在'），
            # 而渲染又把 unresolved 一律说成"文件不存在"，下一轮据此认为文档没写。
            # 服务端兜底：非 .py 的锚点只按路径判定，符号直接丢掉。
            node.anchor.symbol = ""
        if not node.anchor.symbol:
            node.anchor.resolved = True
            node.anchor.symbol_missing = False
            node.unanchored = False
            continue

        tree = parse_file(target)
        found = None
        if tree is not None:
            module = path_to_module(node.anchor.path)
            key = f"{module}.{node.anchor.symbol}"
            for sym in symbols_of(tree, include_methods=True):
                if f"{module}.{sym.name}" == key:
                    found = sym
                    break
        if found is None:
            # 常量/别名也是真实存在的符号（`TOOLS_DIR = Path(__file__).parent`）。
            # 只按 def/class 判定会把它们标成"符号不存在"，渲染成 [未落地] ——
            # 真机实测：下一轮因此以为测试文件没有 sys.path 兼容逻辑、白找一轮
            # （会话 5914fb8d00184172，模型自己把这条列为"会卡住的地方"第一条）。
            if tree is not None and node.anchor.symbol in module_level_names(tree):
                node.anchor.resolved = True
                node.unanchored = False
                node.anchor.symbol_missing = False
                node.anchor.detail = ""  # 复算已解析过的状态时别留下上一次的"符号不存在"
                continue
            node.anchor.resolved = False
            node.anchor.detail = f"文件在盘上，但里面没有 {node.anchor.symbol} 这个名字（可能需要核对真名）"
            # 给渲染一个可判的标记：`[符号未见]` 与 `[未落地]`（文件真的不在）是两回事。
            node.anchor.symbol_missing = True
            node.unanchored = True
            unresolved.append(f"{node.id}(符号缺失 {node.anchor.symbol})")
        else:
            node.anchor.resolved = True
            node.unanchored = False
            node.anchor.symbol_missing = False
            if node.anchor.signature and found.signature and found.signature != node.anchor.signature:
                node.anchor.detail = f"签名漂移: {node.anchor.signature} -> {found.signature}"
                newly_broken.append(f"{node.id} 锚点签名漂移")

    return {
        "anchors_checked": checked,
        "unresolved": unresolved,
        "newly_broken": newly_broken,
    }


# --------------------------------------------------------------------------- #
# 约束状态：由验证器客观覆写
# --------------------------------------------------------------------------- #


def apply_validation_to_constraints(
    prefix: PrefixBundle, state: CompressedState, validation: ValidationResult
) -> None:
    """压缩器可以自评，但 status 一律以验证器为准。

    自评与客观结果的偏差本身是个指标：**压缩器的过度乐观程度**。
    """
    binding = {c.check: c.id for c in prefix.constraints}
    by_id = {c.id: c for c in state.constraints}
    for spec in prefix.constraints:
        cur = by_id.get(spec.id)
        if cur is None:
            cur = ConstraintStatus(id=spec.id, text=spec.text)
            state.constraints.append(cur)
        cur.text = spec.text  # verbatim，压不动
        result = validation.check(spec.check)
        if result is None or result.passed is None:
            cur.status = "unverified"
            cur.evidence = (result.detail if result else "无对应检查")[:120]
        elif result.passed:
            cur.status = "satisfied"
            cur.evidence = result.detail[:120]
        elif (result.count or 0) > 0:
            cur.status = "violated"
            cur.evidence = result.detail[:120]
        else:
            cur.status = "partial"
            cur.evidence = result.detail[:120]
        if cur.status == "violated" and validation.round_index not in cur.violated_rounds:
            cur.violated_rounds.append(validation.round_index)
    _ = binding  # 绑定关系由 run_validator 写入 CheckResult.constraint_id


def _override_check(
    validation: ValidationResult, override: CheckResult
) -> ValidationResult:
    """用"恢复之前"测得的冻结测试结果覆盖恢复之后的结果。"""
    checks = [c for c in validation.checks if c.id != override.id]
    checks.append(override)
    order = {c.id: i for i, c in enumerate(validation.checks)}
    checks.sort(key=lambda c: order.get(c.id, 999))
    violations = sorted({c.constraint_id for c in checks if c.passed is False and c.constraint_id})
    return ValidationResult(
        round_index=validation.round_index,
        checks=checks,
        violations=violations,
        errors=validation.errors,
    )


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class Session:
    def __init__(self, cfg: SessionConfig, prefix: PrefixBundle, store: SessionStore):
        self.cfg = cfg
        self.prefix = prefix
        self.store = store
        self.work_model = build_work_model(cfg, prefix)
        self.compressor = build_compressor(cfg)
        self._frozen_manifest = frozen_manifest(prefix)

    # -- 构造 ------------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        req: SessionCreateRequest,
        *,
        base_dir: Path | None = None,
        state_dir: str | None = None,
    ) -> Session:
        cfg = cls._config_from_request(req, base_dir=base_dir)
        if state_dir:
            cfg.state_dir = state_dir
        prefix = PrefixBundle.load(cfg.prefix_path)

        sid = new_session_id()
        workspace = None
        if cfg.workspace_path:
            workspace = Path(cfg.workspace_path)
            if not workspace.is_absolute() and base_dir:
                workspace = (base_dir / workspace).resolve()
        store = SessionStore(cfg.state_dir, sid, workspace)
        branch_info: dict[str, Any] = {}
        if cfg.workspace_path:
            store.git_init()
            arm = cfg.arm or req.arm
            if cfg.branch_per_arm and arm:
                # 每组从同一个基线复位后再开跑。共用仓库而不分分支的话，
                # 后面的组会继承前面组的成品 —— 那样七组就不是对照了。
                branch_info = store.git_prepare_arm_branch(
                    f"arm-{arm}", cfg.arm_base_branch
                )
                if not branch_info.get("ok"):
                    store.append_event("branch_prepare_failed", **branch_info)

        store.write_meta(
            {
                "created_at": time.time(),
                "status": "open",
                "project": cfg.project,
                "arm": cfg.arm or req.arm,
                "label": cfg.label or req.label,
                "seed": req.seed,
                "prefix_path": cfg.prefix_path,
                "prefix_hash": prefix.hash,
                "stages": [s.title for s in prefix.stages],
                "max_rounds": cfg.max_rounds,
                "history_mode": cfg.resolved_history_mode(),
                "experiment_row": cfg.experiment_row(),
                "branch": branch_info or None,
                "config": cfg.model_dump(mode="json"),
            }
        )
        (store.root / "prefix.md").write_text(prefix.render(), encoding="utf-8")
        store.append_event("session_created", prefix_hash=prefix.hash, branch=branch_info)
        return cls(cfg, prefix, store)

    @classmethod
    def load(
        cls, session_id: str, state_dir: str | Path, *, base_dir: Path | None = None
    ) -> Session:
        """重载一个会话。

        `base_dir` 是必需的（对相对路径的配置而言）：会话记录里可能存着
        **相对的** `workspace_path`（例如 `../drawing-video-toolchain`）。
        不解析它、又静默回退到 `state/workspaces/<sid>`，后果是
        **续跑会把代码写到错的目录**，而 git 提交要么落到别处要么直接失败 ——
        实测踩过：`pciso resume` 本来就是为了"跑挂了不用从头再来"，
        结果它会静默接着写一个空临时目录。所以这里宁可报错，也不静默回退。
        """
        store = SessionStore(state_dir, session_id)
        meta = store.read_meta()
        cfg = SessionConfig(**meta["config"])
        if cfg.workspace_path and not Path(cfg.workspace_path).is_absolute():
            if base_dir is None:
                raise SessionConflict(
                    f"会话记录的 workspace_path 是相对路径 {cfg.workspace_path!r}，"
                    f"但加载时没有给 base_dir —— 无法确定它指向哪里。"
                    f"宁可不加载，也不静默换一个工作区（那会把代码写错地方）。"
                )
            cfg.workspace_path = str((Path(base_dir) / cfg.workspace_path).resolve())
            # 顺手把记录里的相对路径升级成绝对路径，避免下次再踩
            store.patch_meta(config=cfg.model_dump(mode="json"))
        prefix = PrefixBundle.load(cfg.prefix_path)
        if prefix.hash != meta.get("prefix_hash"):
            raise SessionConflict(
                "P 已被改动（prefix_hash 不一致）。冻结前缀是本案的前提，"
                "请在**新会话**里跑，而不是复用旧会话。"
                f"\n  记录: {meta.get('prefix_hash')}\n  现在: {prefix.hash}"
            )
        store = SessionStore(cfg.state_dir, session_id, Path(cfg.workspace_path) if cfg.workspace_path else None)
        return cls(cfg, prefix, store)

    @staticmethod
    def preview_config(
        req: SessionCreateRequest, *, base_dir: Path | None = None
    ) -> SessionConfig:
        """只解析配置、不建会话。给调用方一个"开跑前先看一眼配置"的机会。

        用途之一：接口层要在**建会话之前**判断"这是不是脚本 oracle 在往交付仓库写"——
        建完再检查就已经晚了（分支已经建出来了）。
        """
        return Session._config_from_request(req, base_dir=base_dir)

    @staticmethod
    def _config_from_request(
        req: SessionCreateRequest, *, base_dir: Path | None
    ) -> SessionConfig:
        if req.config_path:
            cfg = SessionConfig.load(req.config_path, base_dir=base_dir)
        elif req.config:
            cfg = SessionConfig.from_mapping(req.config, base_dir=base_dir)
        else:
            cfg = SessionConfig(project=req.project, prefix_path=req.prefix_path)
        if req.overrides:
            # 经过模型往返一次再合并，避免手写 dict merge 漏掉嵌套段与校验
            merged = _deep_merge(cfg.model_dump(mode="json"), req.overrides)
            cfg = SessionConfig.from_mapping(merged, base_dir=base_dir)
        return cfg

    # -- 状态 ------------------------------------------------------------- #

    def state(self) -> CompressedState:
        last = self.store.last_round()
        if last is None:
            return CompressedState(
                version=0, constraints=self.prefix.initial_constraint_status()
            )
        return CompressedState(**last["c_i"])

    def history(self) -> list[str]:
        return [r.get("o_i", "") for r in self.store.read_rounds()]

    def user_words(self) -> list[str]:
        """本会话里**用户自己**说过的话（逐字）。

        模型输出由 C 代表，用户的话不进 C —— 所以它们必须原样回流，否则上一轮说的
        范围约束（"只用标准库"这类）在下一轮就凭空消失了（真机证据见 render.render_user_words）。
        读的是 `chat.jsonl`（人看的纸），不是 rounds：只有走对话通道的轮次才会写它。
        """
        out: list[str] = []
        for entry in self.store.read_chat():
            text = str(entry.get("message") or "").strip()
            if text:
                out.append(text)
        return out

    @property
    def hard_check_ids(self) -> set[str]:
        """硬约束集合：以 P 的 `hard: true` 为准，HARD_CHECK_IDS 作安全网。"""
        from .models import HARD_CHECK_IDS

        from_p = {c.check for c in self.prefix.constraints if c.hard}
        return from_p | set(HARD_CHECK_IDS)

    def stage_for(self, index: int) -> int:
        n = len(self.prefix.stages)
        return min(index + 1, n) if n else index + 1

    def assemble_prompt(
        self,
        *,
        index: int,
        stage: int | None = None,
        extra: str | None = None,
        extra_user_words: Sequence[str] = (),
    ) -> str:
        """唯一的一处 prompt 组装。

        `round()` 与 `build_current_prompt()`（API / dry-run / G 组检索往返都用它）
        必须走同一条路径 —— 之前两处各写一遍，结果 dry-run 看到的 prompt 少了检索段，
        这种"两条组装路径"迟早会分叉，而且分叉了没人会发现。

        `extra_user_words` 只给「下一轮 prompt」预览用：那一刻 `chat.jsonl` 还没写进
        本轮这句话（`say()` 是等 round 返回后才落纸的），而下一轮真正开跑时它已经在纸上了。
        """
        retrieval = self.cfg.work_model.retrieval
        manifest = render_workspace_manifest(self.store.workspace) if retrieval else ""
        words = self.user_words()
        words.extend(w.strip() for w in extra_user_words if w and w.strip())
        return build_prompt(
            prefix=self.prefix,
            state=self.state(),
            cfg=self.cfg,
            round_index=index,
            stage_index=stage or self.stage_for(index),
            history=self.history(),
            extra_instruction=extra,
            workspace_manifest=manifest,
            retrieval=retrieval,
            user_words=words,
        )

    def build_current_prompt(
        self, *, stage: int | None = None, extra: str | None = None
    ) -> str:
        index = self.store.round_index()
        return self.assemble_prompt(index=index, stage=stage, extra=extra)

    # -- G 组：磁盘检索 --------------------------------------------------- #

    def _run_with_retrieval(
        self, prompt: str, *, stage: int, index: int, usage: Usage, telemetry: dict[str, Any]
    ) -> str:
        """server 驱动的检索往返。

        为什么检索不由工作模型自己做：读盘一旦交给模型，越界、读错、读多少字节全都不可审计。
        这里 server 执行读取，于是三类失败变成可计数指标 ——
        **无效检索率**（申请了不存在的路径）、**冗余检索率**（重读已经给过的文件）、
        **检索上下文成本**（读了多少字节）。这正是"磁盘不如在上下文中"这一论断的可测量形式。
        """
        cfg = self.cfg.work_model
        prior_reads: set[str] = set()
        for r in self.store.read_rounds():
            reads = r.get("telemetry", {}).get("reads", {}) or {}
            prior_reads.update(reads.get("served", []))

        served: list[str] = []
        denied: list[str] = []
        redundant: list[str] = []
        read_blocks: list[str] = []
        bytes_read = 0
        calls = 0
        o_i = ""
        augmented = prompt
        thinking_chars = 0
        fallback = False
        system_chars = 0

        while calls < cfg.retrieval_max_cycles + 1:
            out = self.work_model.run(augmented, stage=stage, round_index=index + 1)
            calls += 1
            _accumulate_usage(usage, out.usage)
            o_i = out.text
            thinking_chars += out.reasoning_chars
            fallback = fallback or out.thinking_fallback
            system_chars = max(system_chars, out.system_chars)
            if calls == 1:
                telemetry["work_note"] = out.note

            requested = parse_read_requests(o_i)
            if not requested:
                break

            chunk, s, d, r, nbytes = self._serve_reads(
                requested,
                prior=prior_reads | set(served),
                budget=max(0, cfg.retrieval_max_bytes - bytes_read),
            )
            served.extend(s)
            denied.extend(d)
            redundant.extend(r)
            bytes_read += nbytes

            if chunk:
                read_blocks.append(chunk)
                augmented = (
                    f"{prompt}\n\n{READ_RESULT_HEADER}\n\n"
                    + "\n\n".join(read_blocks)
                    + "\n\n（以上是磁盘上的真实内容。现在请输出本轮结果。）\n"
                )
            else:
                # 申请全被拒或全是冗余：再给一次机会但明确告知没有新内容，
                # 否则最后一趟会停在"只有检索申请"，本轮就没有产物可落盘。
                augmented = (
                    f"{prompt}\n\n{READ_RESULT_HEADER}\n\n"
                    "（没有可提供的新内容：所申请的文件不存在，或者已经给过你了。"
                    "不要再 @@READ，请直接输出本轮结果。）\n"
                )

        telemetry["o_i_source"] = "work_model_retrieval"
        telemetry["reasoning_chars"] = thinking_chars
        telemetry["thinking_fallback"] = fallback
        telemetry["work_system_chars"] = system_chars
        telemetry["work_calls"] = calls
        telemetry["reads"] = {
            "served": served,
            "denied": denied,
            "redundant": redundant,
            "bytes": bytes_read,
            "prior_round_reads": len(prior_reads),
        }
        return o_i

    def _serve_reads(
        self, requested: list[str], *, prior: set[str], budget: int
    ) -> tuple[str, list[str], list[str], list[str], int]:
        served: list[str] = []
        denied: list[str] = []
        redundant: list[str] = []
        chunks: list[str] = []
        nbytes = 0
        ws = self.store.workspace.resolve()
        # 同一批里的重复申请也要算冗余 —— 所以 seen 在循环内累积，
        # 而不是只看"往轮给过没有"。
        seen: set[str] = set(prior)
        notice = "\n…（因检索预算截断）\n"

        for rel in requested[: self.cfg.work_model.retrieval_max_files]:
            if rel in seen:
                redundant.append(rel)
                continue
            seen.add(rel)
            try:
                target = (ws / rel).resolve()
            except OSError:
                denied.append(rel)
                continue
            if ws not in target.parents or not target.is_file():
                denied.append(rel)
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                denied.append(rel)
                continue
            room = budget - nbytes
            if len(text) + len(notice) > room:
                room -= len(notice)
                if room <= 0:
                    break  # 预算耗尽：不再服务，也不虚报读过
                text = text[:room] + notice
            nbytes += len(text)
            served.append(rel)
            chunks.append(f"### {rel}\n```python\n{text}\n```")
            if nbytes >= budget:
                break

        return "\n\n".join(chunks), served, denied, redundant, nbytes

    # -- 一轮 ------------------------------------------------------------- #

    def total_tokens(self) -> int:
        """本会话累计消耗的 token。

        真实用量优先；提供方**不报 usage** 时退回 prompt 估算。
        退回是必要的：脚本 oracle 与一部分 OpenAI 兼容端点都不返回 usage，
        若累计恒为 0，预算闸就形同虚设 —— 而它要防的正是"没人发现它一直在烧"。
        """
        total = 0
        for record in self.store.read_rounds():
            usage = record.get("usage", {}) or {}
            actual = (
                int(usage.get("work_prompt_tokens") or 0)
                + int(usage.get("work_completion_tokens") or 0)
                + int(usage.get("compressor_prompt_tokens") or 0)
                + int(usage.get("compressor_completion_tokens") or 0)
            )
            total += actual or int(record.get("prompt_tokens_est") or 0)
        return total

    def round(self, req: RoundRequest) -> RoundResponse:
        index = self.store.round_index()
        if index >= self.cfg.max_rounds:
            raise SessionConflict(f"已达 max_rounds={self.cfg.max_rounds}")
        if self.cfg.max_total_tokens > 0:
            spent = self.total_tokens()
            if spent >= self.cfg.max_total_tokens:
                raise SessionConflict(
                    f"预算耗尽：已用 {spent} token，上限 {self.cfg.max_total_tokens}。"
                    f"真机跑最贵的失败模式是「没人发现它一直在烧」，所以这里直接拒绝执行。"
                )
        if req.round_index is not None and req.round_index != index:
            raise SessionConflict(
                f"轮次错位：期望 {req.round_index}，当前 {index}（幂等断言）"
            )

        stage = req.stage or self.stage_for(index)
        prior_state = self.state()
        retrieval = self.cfg.work_model.retrieval
        prompt = self.assemble_prompt(
            index=index, stage=stage, extra=req.extra_instruction
        )
        prompt_est = est_tokens(prompt)

        if req.dry_run:
            return RoundResponse(
                session_id=self.store.session_id,
                round_index=index,
                stage=stage,
                committed=False,
                prompt=prompt,
                prompt_tokens_est=prompt_est,
                delta_c=DeltaC(ops=[]),
                c_i=prior_state,
                merge_report=MergeReport(),
                validation=ValidationResult(round_index=index),
                judge=RoundJudgement(decision="accept", reasons=["dry_run：未调用模型、未落盘"]),
            )

        usage = Usage(est_prompt_tokens=prompt_est)
        telemetry: dict[str, Any] = {"history_mode": self.cfg.resolved_history_mode()}

        # 1) 工作模型（或注入的 O_i）
        thinking_text = ""
        if req.o_i is not None:
            o_i = req.o_i
            telemetry["o_i_source"] = "injected"
        elif retrieval:
            o_i = self._run_with_retrieval(
                prompt, stage=stage, index=index, usage=usage, telemetry=telemetry
            )
        else:
            out = self.work_model.run(prompt, stage=stage, round_index=index + 1)
            o_i = out.text
            # 这一轮的 thinking 本体（README §1.3：只有它进推理编排器）。
            thinking_text = out.thinking or ""
            telemetry["o_i_source"] = "work_model"
            telemetry["work_note"] = out.note
            telemetry["work_calls"] = 1
            telemetry["reasoning_chars"] = out.reasoning_chars
            telemetry["thinking_fallback"] = out.thinking_fallback
            # 上下文纯净度的凭证：这一轮发出去的 system 消息长度（应当恒为 0）。
            telemetry["work_system_chars"] = out.system_chars
            if out.retries:
                telemetry["work_retries"] = out.retries
            _accumulate_usage(usage, out.usage)

        if not o_i.strip():
            self.store.append_event("empty_output", round_index=index, stage=stage)
            raise SessionConflict("工作模型返回空输出（可能是 think 吃光预算），本轮未提交")

        # 2) 写入前快照（revert 用）
        self.store.snapshot(index)

        # 3) 解析并落盘
        # 对话驱动下"这一轮没落盘"不是错（提问/闲聊本来就不该动文件）。
        parsed = parse_o_i(o_i, require_files=not self.cfg.chat_driven)
        written: list[str] = []
        for fw in parsed.files:
            try:
                self.store.write_file(fw.path, fw.content)
                written.append(fw.path)
            except ValueError as exc:
                parsed.errors.append(str(exc))
        telemetry["files_written"] = written

        # 3b) 检查点：derived 来自可验证的状态变化；declared 来自模型的 @@UNIT 标记。
        #     declared 只用于**测量**（自我报告注水率），不参与压缩时机控制。
        written_hashes = {fw.path: fw.sha256 for fw in parsed.files}
        declared = parse_declared_checkpoints(o_i)
        for cp in declared:
            if cp.kind == "file":
                sha = written_hashes.get(cp.target)
                cp.backed = sha is not None
                if sha:
                    cp.evidence = {"sha256": sha}
            elif cp.kind == "validate":
                cp.backed = True  # server 每轮都跑验证器，validate 声明恒有据
            else:
                cp.backed = None  # 一次调研没有状态变化，不可验证
        telemetry["declared_units"] = len(declared)
        telemetry["unbacked_claims"] = [
            cp.target for cp in declared if cp.backed is False
        ]

        # 4) 冻结测试：先测篡改，再恢复
        last = self.store.last_round()
        ctx = CheckContext(
            workspace=self.store.workspace,
            prefix=self.prefix,
            cfg=self.cfg,
            round_index=index,
            frozen_manifest=self._frozen_manifest,
            parse_errors=list(parsed.errors),
            prev_signatures=(last or {}).get("snapshot", {}).get("signatures", {}),
            prev_docstrings=(last or {}).get("snapshot", {}).get("docstrings", {}),
            prev_hashes=(last or {}).get("snapshot", {}).get("hashes", {}),
            prev_test_counts=(last or {}).get("snapshot", {}).get("test_counts", {}),
        )
        frozen_result = check_frozen_tests(ctx)
        if self.cfg.validator.restore_frozen_tests:
            restored = self.prefix.restore_frozen_tests(self.store.workspace)
            telemetry["frozen_restored"] = len(restored)

        # 5) 客观验证
        validation = run_validator(ctx)
        validation = _override_check(validation, frozen_result)

        # 5b) 检查点：derived 部分 —— 本轮真实落盘的文件 + 一次真实验证
        derived = [
            Checkpoint(
                kind="file",
                target=path,
                source="derived",
                backed=True,
                evidence={"sha256": sha},
            )
            for path, sha in written_hashes.items()
        ]
        if parsed.files:
            derived.append(
                Checkpoint(
                    kind="validate",
                    source="derived",
                    backed=True,
                    evidence={"violations": list(validation.violations)},
                )
            )
        checkpoints = derived + declared
        telemetry["derived_units"] = len(derived)

        # 6) 压缩 -> ΔC
        # THINK 档（compression.scope=thinking）下送进编排器的是**这一轮的 thinking 块**，
        # 不是 O_i：正文是给人看的不可变纸，动作由 server 自己记，只有 thinking 里住着"为什么"。
        written_triples = [
            (
                p,
                next((fw.sha256 for fw in parsed.files if fw.path == p), ""),
                next((len(fw.content.encode("utf-8")) for fw in parsed.files if fw.path == p), 0),
            )
            for p in written
        ]
        # ACT 节点的锚点要细到符号，符号只能从文件正文里抽（AST）—— 所以把正文一并交给它。
        written_sources = {fw.path: fw.content for fw in parsed.files}
        delta = self.compressor.compress(
            o_i,
            prior_state=prior_state,
            round_index=index,
            c_max_tokens=self.cfg.c_max_tokens,
            checkpoints=checkpoints,
            # 把"这一轮真的写出去了哪些文件"交给压缩器。
            # 它是**唯一可信的"已完成"依据**：模型的自述会说"已落盘"，而盘上没有；
            # 之前只能靠提示词求它别把计划记成事实，现在给它一份 server 侧的物证清单。
            landed=written,
            thinking=thinking_text,
        )
        # 思考原文无条件落盘（0 起序号，与快照一致）：它既是「提纯够不够」的原料，
        # 也是 `/v1` 那条路要回给客户端的 `reasoning_content`（DSH 的「已思考」块）。
        if thinking_text.strip():
            self.store.write_thinking(index, thinking_text)
        telemetry["thinking_chars"] = len(thinking_text)
        if self.cfg.compression.scope == "thinking":
            if not thinking_text.strip():
                # 这一轮没有 thinking 可编排 —— 不是失败（DeltaC.error 保持 None）。
                telemetry["thinking_empty"] = True
            # 服务端硬规则 1+2：THINK 节点必须挂真实动作（无锚不留），
            # 且不许出现完成态断言（完成状态只能来自 ACT）—— 违反的直接丢并计数。
            allowed = think_anchor_paths(prior_state, written_triples)
            delta.ops, think_stats = filter_think_ops(
                delta.ops,
                allowed_paths=allowed,
                source=thinking_text,
                # 准入判据：负节点只留"从 P／盘上文件／别的节点推不出来"的那些。
                # P 每轮逐字在 prompt 里，复述它写的"不修改冻结测试"只是白占 C 的预算。
                constraint_texts=[c.text for c in self.prefix.constraints],
            )
            telemetry.update(think_stats)
            # "验证到哪一步"和完成状态一样，只能由 server 说（模型自述只会说"应该没问题"）。
            # `pytest_suite` 的 passed=None 就是"这轮没跑过测试" —— 直接渲染进 ACT 节点。
            pt = validation.check("pytest_suite")
            verify_clause, verify_state = describe_verification(
                None if pt is None else pt.passed, "" if pt is None else pt.detail
            )
            # ACT 节点由 server 从写盘结果渲染，**零模型调用**；同路径再次写入走 SUPERSEDE（老化规则）。
            delta, act_stats = with_act_nodes(
                delta,
                prior_state=prior_state,
                written=written_triples,
                sources=written_sources,
                verify=verify_clause,
                verify_state=verify_state,
                # 原文只用来找"被否掉的备选"（`A 或 B` 里那个没被写进文件的 B）。
                thinking=thinking_text,
            )
            telemetry.update(act_stats)
        # 压缩开销是本方案的主要成本项，必须真的记进 usage。
        # 这里以前漏了累加，于是"压缩器开销"在报告里恒为 0 ——
        # 那等于把方案的最大代价从成本表里抹掉，而且不会报任何错。
        _accumulate_usage(
            usage,
            {
                "compressor_prompt_tokens": delta.prompt_tokens,
                "compressor_completion_tokens": delta.completion_tokens,
                # 缓存命中量也要进成本表：只记总量会让"压缩器有多贵"这件事失一半真。
                "compressor_cached_tokens": delta.cached_tokens,
            },
        )
        if delta.error:
            telemetry["compressor_error"] = delta.error
        if delta.retried:
            # 截断重试要能在事后数据里数出来：压缩开销是本方案的主要成本项，
            # "差点被截断"是它的风险面，静默就等于没发生。
            telemetry["compressor_retried"] = True

        # 6b) 被拒的落盘尝试要进**失败链**（用户口径）。
        #
        # 路径越界 / 空文件块是**客观事实**（server 判定的），不能只躺在
        # `validation.errors` 里 —— 那份东西下一轮看不到，于是模型会把同一条死路
        # 反复走。失败链存在的全部意义就是"试过 X，因 Y 否决"，而
        # "试过写 ../escape.py，因越界被拒"正是最该被记住的一类失败尝试。
        #
        # 标记 `source="server"`：按事实链的口径，来源必须可区分 ——
        # 这是 server 可验证的，不是模型声称的。
        server_failures = [
            DeltaOp(
                op="ADD_FAILURE",
                payload={
                    "attempt": _rejected_attempt(err),
                    "reason": err,
                    "source": "server",
                },
            )
            for err in parsed.errors
            if err.startswith(("非法路径", "空文件块"))
        ]
        if server_failures:
            delta = delta.model_copy(
                update={"ops": [*delta.ops, *server_failures], "raw": delta.raw}
            )
            telemetry["rejected_paths"] = len(server_failures)

        # 7) 确定性合并 + 更正级联
        state_new, report = apply_delta(
            prior_state,
            delta,
            round_index=index + 1,
            c_max_tokens=self.cfg.c_max_tokens,
            constraint_texts=self.prefix.constraint_texts(),
            est_tokens_fn=_est_c_tokens,
        )

        # 7b) 合并结果要能数出来。真机实测过一轮 4 条推理节点（含 2 条负节点）全被判成"复述"吞掉、
        #     C 一个字节没变 —— 而当时的 telemetry 只说"保留了 4 条"，从数据上根本看不出它没进链。
        telemetry["nodes_added"] = sum(
            1
            for x in report.applied
            if x.startswith(("ADD_FACT:", "SUPERSEDE_FACT"))
        )
        telemetry["nodes_restated"] = sum(1 for x in report.applied if "(restated)" in x)
        telemetry["nodes_rejected"] = len(report.rejected)

        # 8) 锚点解析
        anchor_info = resolve_anchors(self.store.workspace, state_new)
        report.correction_cascades.extend(anchor_info["newly_broken"])
        telemetry["anchors"] = anchor_info

        # 9) 约束状态：验证器客观覆写
        apply_validation_to_constraints(self.prefix, state_new, validation)
        state_new.est_tokens = _est_c_tokens(state_new)

        # 10) 判定
        prev_validation = (
            ValidationResult(**last["validation"]) if last and last.get("validation") else None
        )
        judgement = judge_round(
            prev=prev_validation,
            cur=validation,
            merge_report=report,
            cfg=self.cfg,
            round_index=index,
            hard_checks=self.hard_check_ids,
        )

        # 提取器失效必须出声。
        #
        # 真机踩过：提示词没写 id 格式 -> 模型用 F1/A1/P1/R1 -> FactNode 只收 N<数字> ->
        # 每轮 9 条事实被拒 6~8 条 -> **事实链一直是空的**，而违反数曲线照常演进
        # （约束状态由验证器独立写入），没有任何东西报警。
        # 按事实链的口径，"提取出的事实全被拒收"是提取器的硬失败，不是静默细节。
        rejected_facts = [x for x in report.rejected if x.startswith("ADD_FACT")]
        applied_facts = [x for x in report.applied if x.startswith("ADD_FACT")]
        telemetry["fact_ops_rejected"] = len(rejected_facts)
        telemetry["fact_ops_applied"] = len(applied_facts)
        if rejected_facts and not applied_facts:
            telemetry["extractor_failed"] = True
            judgement.reasons.append(
                f"提取器失效：本轮 {len(rejected_facts)} 条 ADD_FACT 全被拒收，事实链没有增长"
                f"（首条原因：{rejected_facts[0][:120]}）"
            )

        # 11) accept -> 提交；revert -> 回退工作区
        git_sha: str | None = None
        reverted = judgement.decision == "revert" and self.cfg.revert_on_hard_regression
        if reverted:
            judgement.reverted_files = self.store.restore(index)
            telemetry["reverted"] = True
            snapshot = (last or {}).get("snapshot", {})
            # C 不前进 —— 但**回退撤销的是模型的产出，不是 server 自己的判定记录**。
            #
            # 被拒的落盘尝试（越界路径 / 空文件块）是 server 客观测到的事实：
            # 模型试过写 `../escape.py` 并被拒。如果连同它一起丢掉，
            # 下一轮的上下文里就没有"这条路走过且不通"，模型会反复走同一条死路 ——
            # 而"重复尝试率（失败链是否真的防住了）"这条指标也同时失去意义。
            #
            # 所以口径是：**模型提取的事实不前进，server 记录的失败照记**。
            # 这也符合事实链那条口径 —— 来源必须可区分，可验证的不能因回退被抹掉。
            # 只在**真有** server 失败要记时才动 C：否则保持逐字节不变，
            # 让"回退 = 这一轮等于没发生"这条性质在最常见的情况下字面为真
            # （第一版无条件重算，没失败也把 est_tokens 从 0 变成 143）。
            if server_failures:
                state_new, _ = apply_delta(
                    prior_state,
                    DeltaC(ops=server_failures, model="server"),
                    round_index=index,
                    c_max_tokens=self.cfg.c_max_tokens,
                    constraint_texts=self.prefix.constraint_texts(),
                    est_tokens_fn=_est_c_tokens,
                )
            else:
                state_new = prior_state
        else:
            telemetry["reverted"] = False
            snapshot = take_snapshot(self.store.workspace)
            if self.cfg.workspace_path:
                spec = self.prefix.stage(stage)
                try:
                    git_sha = self.store.git_commit(
                        f"[round {index + 1}] 阶段 {stage}: {spec.title if spec else '推进'}",
                        trailers={
                            "PCISO-Round": index + 1,
                            "PCISO-Arm": self.cfg.arm or "unset",
                            "PCISO-Prefix-Hash": self.prefix.hash[:16],
                            "PCISO-Judgement": judgement.decision,
                        },
                    )
                except Exception as exc:
                    # 先记账再抛：这一轮不会被写进 rounds.jsonl（所以不会被误当成成功），
                    # 但失败原因必须留在 events.jsonl 里，否则只剩一个裸 traceback。
                    self.store.append_event(
                        "git_commit_failed", round_index=index, error=str(exc)
                    )
                    raise
        # 12) 落轨迹日志
        record: dict[str, Any] = {
            "round_index": index,
            "stage": stage,
            "ts": time.time(),
            "committed": True,
            "judgement": judgement.decision,
            # 判定理由也要落盘：否则"这一轮为什么被回退"只存在于 events.jsonl，
            # 读 rounds.jsonl 的人（和聊天界面）根本看不到解释。
            "judgement_reasons": judgement.reasons,
            "judgement_regressions": judgement.regressions,
            "prompt": prompt,
            "prompt_tokens_est": prompt_est,
            "o_i": o_i,
            "o_i_chars": len(o_i),
            "delta_c": delta.model_dump(mode="json"),
            "c_i": state_new.model_dump(mode="json"),
            "c_tokens_est": state_new.est_tokens,
            "merge_report": report.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "violation_count": validation.violation_count(),
            "violations": validation.violations,
            "usage": usage.model_dump(mode="json"),
            "checkpoints": [cp.model_dump(mode="json") for cp in checkpoints],
            "snapshot": snapshot,
            "git_sha": git_sha,
            "telemetry": telemetry,
        }
        self.store.append_round(record)
        if judgement.decision == "revert":
            self.store.append_event(
                "round_reverted",
                round_index=index,
                reasons=judgement.reasons,
                regressions=judgement.regressions,
            )

        # 13) 下一轮 prompt（走 assemble_prompt：这是唯一组装点，别再手拼一遍）
        next_index = self.store.round_index()
        next_prompt = self.assemble_prompt(
            index=next_index,
            extra_user_words=[req.extra_instruction or ""],
        )

        return RoundResponse(
            session_id=self.store.session_id,
            round_index=index,
            stage=stage,
            committed=True,
            prompt=prompt,
            prompt_tokens_est=prompt_est,
            o_i=o_i,
            delta_c=delta,
            c_i=state_new,
            merge_report=report,
            validation=validation,
            judge=judgement,
            next_prompt=next_prompt,
            usage=usage,
            artifacts=self.store.index_artifacts(),
            telemetry=telemetry,
        )

    # -- 查询 ------------------------------------------------------------- #

    def dep_graph_consistency(self) -> dict[str, Any]:
        """C 里的依赖图 vs **每轮用 AST 从磁盘重抽的**真实依赖图。

        这是实验设计原文写下的风险对策：「依赖图与真实代码不符 →
        每轮用 AST 重新抽取真实依赖图，与 C 中比对」。
        没有它，"事实链可信"就只是一句声称 —— C 可能带着一条早已不成立的依赖边
        继续往下走（或漏掉一条真实存在的边），而下游会据此做错决策。

        precision = C 声称的边里有多少被代码证实；recall = 代码里的边有多少进了 C。
        两端都要看：只报 precision 会掩盖"漏记"，只报 recall 会掩盖"编造"。
        """
        from .checks import SKIP_DIRS
        from .pyast import import_targets, iter_py_files, parse_file, path_to_module

        workspace = self.store.workspace
        claimed = {(d.src, d.dst) for d in self.state().deps}

        files: list[tuple[str, Path]] = []
        for path in iter_py_files(workspace, skip=SKIP_DIRS):
            rel = path.relative_to(workspace).as_posix()
            parts = rel.split("/")
            if "tests" in parts or parts[0] == "post":
                continue
            files.append((path_to_module(rel), path))

        # **必须同包过滤**：事实链里的依赖边只记同一顶层包内部的边
        # （外部包如 json/zlib 不是"模块依赖"，也不是可维护的架构关系）。
        # 不过滤的话 actual 会混进一堆外部 import，recall 永远小于 1 —— 假警报。
        top_packages = {module.split(".")[0] for module, _ in files}
        actual: set[tuple[str, str]] = set()
        for module, path in files:
            tree = parse_file(path)
            if tree is None:
                continue
            for target in import_targets(tree, module):
                if target == module:
                    continue
                if target.split(".")[0] in top_packages:
                    actual.add((module, target))

        confirmed = claimed & actual
        return {
            "claimed": len(claimed),
            "actual": len(actual),
            "confirmed": len(confirmed),
            "precision": (len(confirmed) / len(claimed)) if claimed else None,
            "recall": (len(confirmed) / len(actual)) if actual else None,
            "missing_in_c": sorted(f"{s}->{d}" for s, d in (actual - claimed))[:10],
            "extra_in_c": sorted(f"{s}->{d}" for s, d in (claimed - actual))[:10],
        }

    def metrics(self) -> dict[str, Any]:
        rounds = self.store.read_rounds()
        per_round: list[dict[str, Any]] = []
        for r in rounds:
            per_round.append(
                {
                    "round": r["round_index"],
                    "stage": r["stage"],
                    "judgement": r["judgement"],
                    "violations": len(r.get("violations") or []),
                    "violation_count": r.get("violation_count", 0),
                    "violation_ids": r.get("violations") or [],
                    "prompt_tokens_est": r.get("prompt_tokens_est", 0),
                    "work_prompt_tokens": r.get("usage", {}).get("work_prompt_tokens", 0),
                    "work_cached_tokens": r.get("usage", {}).get("work_cached_tokens", 0),
                    "work_completion_tokens": r.get("usage", {}).get(
                        "work_completion_tokens", 0
                    ),
                    "compressor_tokens": r.get("usage", {}).get("compressor_prompt_tokens", 0)
                    + r.get("usage", {}).get("compressor_completion_tokens", 0),
                    "c_tokens": r.get("c_tokens_est", 0),
                    "facts": len([f for f in r["c_i"]["fact_chain"] if f["status"] == "active"]),
                    "failures": len(r["c_i"]["failure_chain"]),
                    "deps": len(r["c_i"]["deps"]),
                    "anchors": r.get("telemetry", {}).get("anchors", {}),
                    "compressor_error": r.get("telemetry", {}).get("compressor_error"),
                    "delta_ops": len(r.get("delta_c", {}).get("ops") or []),
                    "files_written": len(r.get("telemetry", {}).get("files_written") or []),
                    "declared_units": r.get("telemetry", {}).get("declared_units", 0),
                    "derived_units": r.get("telemetry", {}).get("derived_units", 0),
                    "unbacked_claims": r.get("telemetry", {}).get("unbacked_claims") or [],
                    "work_calls": r.get("telemetry", {}).get("work_calls", 1),
                    "reads_served": len(
                        (r.get("telemetry", {}).get("reads", {}) or {}).get("served", [])
                    ),
                    "reads_denied": len(
                        (r.get("telemetry", {}).get("reads", {}) or {}).get("denied", [])
                    ),
                    "reads_redundant": len(
                        (r.get("telemetry", {}).get("reads", {}) or {}).get("redundant", [])
                    ),
                    "read_bytes": (r.get("telemetry", {}).get("reads", {}) or {}).get("bytes", 0),
                    "reasoning_chars": r.get("telemetry", {}).get("reasoning_chars", 0),
                    "thinking_fallback": bool(
                        r.get("telemetry", {}).get("thinking_fallback")
                    ),
                    "o_i_chars": r.get("o_i_chars", 0),
                }
            )

        reverts = sum(1 for r in rounds if r["judgement"] == "revert")
        total_work = sum(r.get("usage", {}).get("work_prompt_tokens", 0) for r in rounds)
        total_cached = sum(r.get("usage", {}).get("work_cached_tokens", 0) for r in rounds)
        total_completion = sum(
            r.get("usage", {}).get("work_completion_tokens", 0) for r in rounds
        )
        total_comp = sum(
            r.get("usage", {}).get("compressor_prompt_tokens", 0)
            + r.get("usage", {}).get("compressor_completion_tokens", 0)
            for r in rounds
        )
        anchor_pairs = [
            (p["anchors"].get("anchors_checked", 0), len(p["anchors"].get("unresolved", [])))
            for p in per_round
        ]
        checked = sum(a for a, _ in anchor_pairs)
        unresolved = sum(b for _, b in anchor_pairs)

        declared_total = sum(p["declared_units"] for p in per_round)
        derived_total = sum(p["derived_units"] for p in per_round)
        unbacked = [c for p in per_round for c in p["unbacked_claims"]]
        no_delta_rounds = [p["round"] + 1 for p in per_round if p["files_written"] == 0]

        reads_served = sum(p["reads_served"] for p in per_round)
        reads_denied = sum(p["reads_denied"] for p in per_round)
        reads_redundant = sum(p["reads_redundant"] for p in per_round)
        read_bytes_total = sum(p["read_bytes"] for p in per_round)
        read_attempts = reads_served + reads_denied + reads_redundant

        # 提取出的事实有多少真的进了事实链。
        #
        # 这条指标是被一次真机跑逼出来的：提示词没写 id 格式，真实模型用了 F1/A1/P1/R1，
        # 而 FactNode 只接受 N<数字> —— 每轮 9 条事实被拒 6~8 条，**事实链整整空了一轮又一轮**，
        # 而违反数曲线照样在动（约束状态由验证器独立写入），离线的 heuristic 提取器又只生成 N1/N2，
        # 所以没有任何东西报警。按新口径，提取出来的事实被全部拒收是提取器的**硬失败**，
        # 不是可以沉默的细节 —— 它必须是个能一眼看到的数。
        fact_ops_applied = sum(
            1
            for r in rounds
            for x in (r["merge_report"].get("applied") or [])
            if x.startswith("ADD_FACT")
        )
        fact_ops_rejected = sum(
            1
            for r in rounds
            for x in (r["merge_report"].get("rejected") or [])
            if x.startswith("ADD_FACT")
        )
        fact_attempts = fact_ops_applied + fact_ops_rejected

        # 「被压缩掉的体量」——界面上要显示"隐藏了多少"，但不暴露内容。
        reasoning_total = sum(p["reasoning_chars"] for p in per_round)
        fallback_rounds = [p["round"] + 1 for p in per_round if p["thinking_fallback"]]
        o_i_chars_total = sum(int(p["o_i_chars"]) for p in per_round)

        last = self.state()
        return {
            "session_id": self.store.session_id,
            "arm": self.cfg.arm,
            "experiment_row": self.cfg.experiment_row(),
            "rounds": len(rounds),
            "reverts": reverts,
            "revert_rate": (reverts / len(rounds)) if rounds else 0.0,
            "violation_count_total": sum(p["violation_count"] for p in per_round),
            "violation_ids_total": sorted(
                {v for p in per_round for v in p["violation_ids"]}
            ),
            "per_round": per_round,
            "tokens": {
                "work_prompt_total": total_work,
                "work_cached_total": total_cached,
                "work_completion_total": total_completion,
                "compressor_total": total_comp,
                "total": total_work + total_completion + total_comp,
            },
            "compression_quality": {
                "anchors_checked": checked,
                "anchors_unresolved": unresolved,
                "anchor_resolvable_rate": (1 - unresolved / checked) if checked else None,
                "duplicate_attempts": sum(
                    len(r["merge_report"].get("duplicate_attempts") or []) for r in rounds
                ),
                "constraint_text_mutations": sum(
                    len(r["merge_report"].get("constraint_text_mutations") or [])
                    for r in rounds
                ),
                "correction_cascades": sum(
                    len(r["merge_report"].get("correction_cascades") or []) for r in rounds
                ),
                "c_tokens_max": max((p["c_tokens"] for p in per_round), default=0),
                # 事实链的**产出率**：提取出的 ADD_FACT 有多少真被接受。
                # 0 表示事实链是空的 —— 那这套方案的核心就不在工作。
                "fact_ops_applied": fact_ops_applied,
                "fact_ops_rejected": fact_ops_rejected,
                "fact_acceptance_rate": (
                    fact_ops_applied / fact_attempts if fact_attempts else None
                ),
                # 事实链里的依赖图与磁盘上真实依赖的一致性。
                # 设计原文把"依赖图与真实代码不符"列为风险，对策就是这条比对。
                "dep_graph_consistency": self.dep_graph_consistency(),
            },
            # 自我报告质量：模型声称的工作量 vs 可验证的状态变化。
            # 这是把"@@UNIT 声明"当**因变量**而不是控制信号的收益所在 ——
            # 宣称改完却没有对应文件变化 = 注水，可客观计数。
            "self_report": {
                "declared_units_total": declared_total,
                "derived_units_total": derived_total,
                "declaration_ratio": (declared_total / derived_total) if derived_total else None,
                "unbacked_claims_total": len(unbacked),
                "unbacked_claims": unbacked[:20],
                "unbacked_claim_rate": (
                    len(unbacked) / declared_total if declared_total else None
                ),
                "rounds_without_file_delta": no_delta_rounds,
            },
            # G 组的检索质量：磁盘不是免费的信息源，它的失败模式要能被计数。
            "retrieval": {
                "served_total": reads_served,
                "denied_total": reads_denied,
                "redundant_total": reads_redundant,
                "bytes_total": read_bytes_total,
                "invalid_read_rate": (reads_denied / read_attempts) if read_attempts else None,
                "redundant_read_rate": (
                    reads_redundant / read_attempts if read_attempts else None
                ),
                "extra_calls_total": sum(max(0, p["work_calls"] - 1) for p in per_round),
            },
            # 隐藏面的体量：思考与原始轨迹各被丢掉多少。这是"压缩"的可见刻度，
            # 也是极简模式界面唯一该展示的"过程"信息。
            "hidden": {
                "reasoning_chars_total": reasoning_total,
                "reasoning_chars_per_round": [p["reasoning_chars"] for p in per_round],
                "thinking_fallback_rounds": fallback_rounds,
                "o_i_chars_total": o_i_chars_total,
                "c_tokens_max": max((p["c_tokens"] for p in per_round), default=0),
                "compression_ratio_chars_per_token": (
                    round(o_i_chars_total / max(1, max((p["c_tokens"] for p in per_round), default=0)), 1)
                ),
            },
            "constraint_status": [c.model_dump(mode="json") for c in last.constraints],
        }

    def artifacts(self) -> ArtifactIndex:
        return self.store.index_artifacts()

    def revert(self, to_index: int) -> dict[str, Any]:
        restored = self.store.restore(to_index)
        self.store.append_event("manual_revert", to_index=to_index, files=len(restored))
        self.store.patch_meta(status="reverted")
        return {"to_index": to_index, "restored_files": len(restored)}

    def state_response(self) -> SessionStateResponse:
        meta = self.store.read_meta()
        state = self.state()
        return SessionStateResponse(
            session_id=self.store.session_id,
            project=meta.get("project", ""),
            arm=meta.get("arm"),
            prefix_hash=meta.get("prefix_hash", ""),
            round_index=self.store.round_index(),
            status=meta.get("status", "open"),
            c_current=state,
            fact_chain=state.fact_chain,
            failure_chain=state.failure_chain,
            dependency_graph=state.deps,
            constraint_status=state.constraints,
            metrics=self.metrics(),
        )

    def delete(self) -> None:
        self.store.delete()

    # -- IoT：一个 thinking block 一次编排 ---------------------------------- #
    #
    # 规格（用户口径）：
    #   · 压的**只有 thinking 块** —— 一个块一次，触发点是这个块结束（下一个块开始输出）；
    #   · 工具调用 / 工具结果 / 正文都不进压缩器：前两者是事实块，由 `absorb_actions`
    #     从落盘结果确定性地渲染成 ACT（零模型调用）；正文要原样留给人看；
    #   · 用户原话逐字回流，不进 C；
    #   · **不做预算裁剪**：C 的规模由"块数 × 每块几条"天然决定。按显著度裁是我们替
    #     模型猜它以后需不需要（实测把 salience=85 的事实都裁没了）。
    #
    # 这两条路是 `/v1` 代理（DSH 那条路）在用的；实验臂走的仍是 `round()` 的全量流水线。

    def absorb_thinking_block(
        self,
        thinking: str,
        *,
        landed: Sequence[tuple[str, str, int]] = (),
        sources: dict[str, str] | None = None,
        workspace: Path | None = None,
        extra_allowed_paths: Sequence[str] = (),
        source: str = "proxy",
    ) -> dict[str, Any]:
        """把一个 thinking block 提纯成思考节点（IoT items），追加进 C，返回统计。"""
        if not thinking.strip():
            return {"compressed": False, "reason": "空的 thinking 块"}
        prior = self.state()
        index = self.store.round_index()
        started = time.time()
        # ThinkingNodeCompressor 的输入就是思考本身（`_text(o_i, thinking)` 认这个参数），
        # 所以 o_i 传空串 —— 正文与工具往返不归它管。
        delta = self.compressor.compress(
            "",
            prior_state=prior,
            round_index=index,
            c_max_tokens=0,
            checkpoints=(),
            landed=landed,
            thinking=thinking,
        )
        # 原文落盘：它既是"提纯够不够"的原料，也是判"这条节点有没有据"的底稿。
        self.store.write_thinking(index, thinking)
        telemetry: dict[str, Any] = {
            "source": source,
            "kind": "thinking_block",
            "thinking_chars": len(thinking),
        }
        # 无锚不留：允许挂的路径 = 历史上真写过的 ∪ 客户端这一轮的工具点过名的。
        # 只认真写过的会让**第一个**块全军覆没（那时还没有任何 ACT），而工具的
        # 参数是客户端发上来、看得见的事实 —— 允许它，但不因此就说文件已落地。
        allowed = think_anchor_paths(prior, landed) | {
            p for p in extra_allowed_paths if p
        }
        delta.ops, think_stats = filter_think_ops(
            delta.ops,
            allowed_paths=allowed,
            source=thinking,
            constraint_texts=[c.text for c in self.prefix.constraints],
        )
        telemetry.update(think_stats)
        delta, act_stats = with_act_nodes(
            delta,
            prior_state=prior,
            written=landed,
            sources=sources or {},
            thinking=thinking,
        )
        telemetry.update(act_stats)
        state_new, report = apply_delta(
            prior,
            delta,
            round_index=index + 1,
            c_max_tokens=0,  # 0 = 不裁（见 merge.apply_delta 的注释）
            constraint_texts=self.prefix.constraint_texts(),
            est_tokens_fn=_est_c_tokens,
        )
        telemetry["anchors"] = resolve_anchors(workspace or self.store.workspace, state_new)
        state_new.est_tokens = _est_c_tokens(state_new)
        self._append_orchestrated_round(
            kind="thinking_block",
            index=index,
            state_new=state_new,
            delta=delta,
            report=report,
            telemetry=telemetry,
            note=thinking,
        )
        # 这个块的**紧凑版**：块级替换要用它（把上下文里那一整块原文换成这几行）。
        # 行文格式见 `actnodes.item_line`：正向不挂前缀，只有 [否掉] / [待定] / [落地]。
        items = [
            item_line(n)
            for n in state_new.fact_chain
            if n.source_round == index + 1 and n.node_kind in ("think", "act")
        ]
        # 块级替换的底线：每一块都要有一行紧凑版。编排器说"没什么值得留的"时，
        # 由服务端从原文抽一句概述补上（零模型调用）——否则客户端那一块就是空白。
        overview = False
        if not items:
            line = one_line_overview(thinking)
            if line:
                items = [line]
                overview = True
        return {
            "compressed": True,
            "round_index": index + 1,
            "ops": len(delta.ops),
            "items": items,
            "overview": overview,
            "error": delta.error,
            "retried": delta.retried,
            "c_tokens": state_new.est_tokens,
            "elapsed_s": round(time.time() - started, 2),
            **think_stats,
            **act_stats,
        }

    def absorb_actions(
        self,
        *,
        written: Sequence[tuple[str, str, int]] = (),
        sources: dict[str, str] | None = None,
        verify: str = "",
        verify_state: str = "",
        workspace: Path | None = None,
        source: str = "proxy",
    ) -> dict[str, Any]:
        """工具往返 → ACT 节点（**零模型调用**）。

        "这一轮真的写了哪个文件"只能由 server 说：它的依据是工具结果 + 盘上文件，
        不是模型的自述。所以这条路一行压缩器都不调。
        """
        if not written:
            return {"act_nodes": 0}
        prior = self.state()
        index = self.store.round_index()
        delta, act_stats = with_act_nodes(
            DeltaC(ops=[], model="server"),
            prior_state=prior,
            written=written,
            sources=sources or {},
            verify=verify,
            verify_state=verify_state,
        )
        if not delta.ops:
            return {"act_nodes": 0, **act_stats}
        state_new, report = apply_delta(
            prior,
            delta,
            round_index=index + 1,
            c_max_tokens=0,
            constraint_texts=self.prefix.constraint_texts(),
            est_tokens_fn=_est_c_tokens,
        )
        state_new.est_tokens = _est_c_tokens(state_new)
        telemetry: dict[str, Any] = {
            "source": source,
            "kind": "tool_actions",
            "anchors": resolve_anchors(workspace or self.store.workspace, state_new),
        }
        telemetry.update(act_stats)
        self._append_orchestrated_round(
            kind="tool_actions",
            index=index,
            state_new=state_new,
            delta=delta,
            report=report,
            telemetry=telemetry,
            note="",
        )
        return {"round_index": index + 1, "c_tokens": state_new.est_tokens, **act_stats}

    def _append_orchestrated_round(
        self,
        *,
        kind: str,
        index: int,
        state_new: CompressedState,
        delta: DeltaC,
        report: MergeReport,
        telemetry: dict[str, Any],
        note: str,
    ) -> None:
        """写一条"编排出来的"轮次记录。

        形状必须与真轮次兼容 —— `/metrics` 与 `/session/{id}/state` 是按真轮次的字段
        硬读的（`judgement`/`violations`/`usage`/`c_tokens_est`），少一个就 KeyError。
        这些轮次**不判定**：工具与轮次由客户端驱动，服务端只负责把这一块记成链。
        """
        self.store.append_round(
            {
                "round_index": index + 1,
                "kind": kind,
                "stage": 0,
                "o_i": note,
                "o_i_chars": len(note),
                "judgement": "accept",
                "judgement_reasons": ["编排轮次不判定：轮次与工具由客户端驱动"],
                "violations": [],
                "c_i": state_new.model_dump(mode="json"),
                "delta_c": {
                    "ops": [op.model_dump(mode="json") for op in delta.ops],
                    "raw": delta.raw,
                    "error": delta.error,
                    "truncated": delta.truncated,
                    "retried": delta.retried,
                    "model": delta.model,
                    "prompt_tokens": delta.prompt_tokens,
                    "completion_tokens": delta.completion_tokens,
                    "cached_tokens": delta.cached_tokens,
                },
                "merge_report": report.model_dump(mode="json"),
                "prompt_tokens_est": 0,
                "c_tokens_est": state_new.est_tokens,
                "usage": {
                    "compressor_prompt_tokens": delta.prompt_tokens,
                    "compressor_completion_tokens": delta.completion_tokens,
                    "compressor_cached_tokens": delta.cached_tokens,
                },
                "telemetry": telemetry,
            }
        )

    # -- 聊天视图 --------------------------------------------------------- #

    def say(
        self, message: str = "", *, stage: int | None = None, include_hidden: bool = False
    ) -> SayResponse:
        """跑一轮，并把它投影成聊天视图。

        用户的这句话走 `extra_instruction`，落在骨架里固定的「本轮任务」段落上 ——
        所以"每轮说的话不同"不会破坏极简模式的形态（见 render.build_prompt）。

        跟 `ask` 的分工：`say` 是**干活的那条路**（写文件、跑验证、ΔC 前进、判定、提交），
        `ask` 是只读的检视通道。对话窗口要能派活，所以它走这条。
        """
        self.round(
            RoundRequest(extra_instruction=(message.strip() or None), stage=stage)
        )
        record = self.store.last_round()
        if record is None:  # pragma: no cover - round() 刚提交过
            raise SessionConflict("轮次未提交")
        view = turn_view(record, self.cfg)
        view["session_id"] = self.store.session_id
        view["usage"] = record.get("usage") or {}
        # 思考原文回传（`/v1` 用它填 `reasoning_content`）：跟"回显历史"是两回事，
        # 这是**本轮**的过程，客户端看不看由它决定。
        thinking_text = self.store.read_thinking(view["round_index"])
        if thinking_text:
            view["thinking"] = thinking_text
        # 说过的每一句 + 它带回的正文都进 chat.jsonl（append-only，不入库）：
        # 刷新页面要原样摆回来 —— "已经渲染出来的东西不可变"就落在这张纸上。
        self.store.append_chat(
            {
                "round_index": view["round_index"],
                "message": message.strip(),
                "reply": view["reply"],
                "reply_kind": view["reply_kind"],
                "verdict": view["verdict"],
                "files_changed": view["files_changed"],
            }
        )
        detail = None
        if include_hidden:
            # 审计视图：给人看的，**不会**回流进极简模式。
            detail = {
                "prompt": record.get("prompt", ""),
                "o_i": record.get("o_i", ""),
                "delta_raw": (record.get("delta_c") or {}).get("raw", ""),
                "merge_report": record.get("merge_report", {}),
                "failed_checks": [
                    {"id": c["id"], "detail": c["detail"], "findings": c["findings"][:5]}
                    for c in (record.get("validation", {}).get("checks") or [])
                    if c.get("passed") is False
                ],
            }
        return SayResponse(**view, hidden_detail=detail)

    def turns(self) -> list[dict[str, Any]]:
        """已有轮次的聊天视图（供页面刷新后恢复）。"""
        views = []
        for record in self.store.read_rounds():
            view = turn_view(record, self.cfg)
            view["session_id"] = self.store.session_id
            views.append(view)
        return views

    def chat_log(self) -> list[dict[str, Any]]:
        """对话窗口的记录（append-only）：刷新页面时把说过的话原样摆回来。

        它**不是上下文**：模型看不到它，C 里也没有它。窗口里那串历史只是给人看的纸 ——
        这正是"已经渲染出来的东西不可变"那条要求的落点。
        """
        return self.store.read_chat()

    def ask(
        self, message: str, *, max_tokens: int = 0, thinking: bool | None = None
    ) -> AskResponse:
        """只对话：把**当前 C** 给模型看一眼，拿一段回答。

        **这不是循环的一部分。** 它不产生 ΔC、不落 rounds.jsonl、C 不前进、
        工作区一个字节都不动。它存在的唯一理由是"人要看一眼当前压缩状态是否说得通"——
        跟审计视图同一类东西（检视通道，不是第二个机制：它不压缩、不摘要、
        也不改变任何状态）。

        为什么必须单独有它：把"发一句话"实现成"跑一整轮"是个设计错误 ——
        说一句"你好"会被那轮提示词要求去推进一整个阶段（实测：思考 57580 字、
        写了 5 个文件、112 秒）。检视不该按实验的代价收费。

        两条跟"聊天窗口"直接有关的性质：

        * **思维链不可见是硬保证**：`content` 为空而 `reasoning_content` 非空时，
          客户端会把思考当正文回退（`workmodels` 里那条唯一的泄漏路径）——
          这里把它当作"没写正文"，绝不把思考摊进窗口。
        * **说过的每一句进 `chat.jsonl`（append-only）**：那只是**显示记录**，
          不喂回模型、不参与判定、不碰 C。写它是因为"已经渲染出来的东西是不可变的"：
          刷新之后原样摆回来，永远不会被后来的压缩改写。
        """
        started = time.time()
        state = self.state()
        prompt = render_ask_prompt(state, message)
        base_url = self.cfg.work_model.base_url or os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
        client = ModelClient(
            base_url=base_url,
            model=self.cfg.work_model.model,
            api_key_env=self.cfg.work_model.api_key_env,
        )
        res = client.chat(
            prompt,
            max_tokens=max_tokens or self.cfg.work_model.max_tokens,
            temperature=0.3,
            thinking=self.cfg.work_model.thinking if thinking is None else thinking,
        )
        body = "" if res.thinking_fallback else (res.text or "").strip()
        reply = (
            f"（这一轮没走通：{res.error}）"
            if res.error
            else body or "（模型这轮没写正文）"
        )
        elapsed = round(time.time() - started, 1)
        self.store.append_chat(
            {
                "message": message,
                "reply": reply,
                "elapsed_s": elapsed,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "error": res.error,
            }
        )
        return AskResponse(
            reply=reply,
            thinking_fallback=res.thinking_fallback,
            thinking_chars=res.reasoning_chars,
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            c_tokens=_est_c_tokens(state),
            rounds_done=self.store.round_index(),
            elapsed_s=elapsed,
            facts=len(state.active_facts()),
            failures=len(state.failure_chain),
            error=res.error,
        )

    def audit(self, round_index: int) -> dict[str, Any]:
        """单轮的审计视图：**给人看**的全文，绝不回流进极简模式。"""
        rounds = self.store.read_rounds()
        if not (0 <= round_index < len(rounds)):
            raise KeyError(f"没有第 {round_index} 轮")
        record = rounds[round_index]
        return {
            "round_index": round_index,
            "prompt": record.get("prompt", ""),
            "o_i": record.get("o_i", ""),
            "delta_raw": (record.get("delta_c") or {}).get("raw", ""),
            "merge_report": record.get("merge_report", {}),
            "failed_checks": [
                {"id": c["id"], "detail": c["detail"], "findings": c["findings"][:5]}
                for c in (record.get("validation", {}).get("checks") or [])
                if c.get("passed") is False
            ],
        }

    # -- 眼睛通道 --------------------------------------------------------- #

    def render_preview(self, sentence: str = "", *, timeout_s: int = 300) -> dict[str, Any]:
        """跑一遍工作区里的成片流水线，把产物收出来给**人和视觉模型**看。

        为什么服务端要做这件事：工作模型是单发无工具的，它**看不到自己画的帧**。
        视频开发最需要的恰恰是看画面（构图、节奏、字幕可读性、转场是否生硬），
        而极简模式对这部分的反馈是零 —— 这条通道补的就是它。

        两条约束：
        - **不碰工作区**：示例素材与输出都落在 state/previews/<sid>/ 下，
          不然预览会变成"往被测项目里塞文件"，直接污染实验。
        - 用被测项目**自己的**接口跑（`pipeline.make_video`），所以它同时是一次端到端检查：
          接口没实现或跑不通，预览就会失败并给出 traceback。
        """
        out_dir = self.store.root / "previews" / "latest"
        assets_dir = self.store.root / "previews" / "assets"
        out_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)

        driver = out_dir / "_preview_driver.py"
        driver.write_text(_PREVIEW_DRIVER, encoding="utf-8")
        # 参数走 UTF-8 的 JSON 文件，**不走 argv**：
        # Windows 上 argv 按 ANSI 代码页解码，中文会被换成 `?` ——
        # 实测就踩了：生成的 subs.vtt 里中文全变成问号，看起来像被测项目的编码 bug，
        # 其实是调用方把参数搞坏了。
        params = out_dir / "_preview_params.json"
        params.write_text(
            json.dumps(
                {
                    "sentence": sentence or "「预览」用示例素材出一支 3 秒的片子",
                    "assets_dir": str(assets_dir),
                    "out_dir": str(out_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        argv = [sys.executable, str(driver), str(params)]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.store.workspace)
        env["PYTHONIOENCODING"] = "utf-8"
        # 禁写字节码：否则预览会在被测项目里留下 __pycache__ ——
        # 预览必须是零侵入的，不然它就成了"往被生成的项目里塞文件"。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.store.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                env=env,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "stage": "run", "error": f"{type(exc).__name__}: {exc}"}

        files: dict[str, str] = {}
        for name in ("movie.apng", "subs.vtt", "bgm.wav", "plan.json", "index.html"):
            if (out_dir / name).is_file():
                files[name] = f"previews/latest/{name}"
        frames_dir = out_dir / "frames"
        frames = sorted(frames_dir.glob("frame_*.png")) if frames_dir.is_dir() else []
        if frames:
            files["frames/frame_0000.png"] = "previews/latest/frames/frame_0000.png"

        return {
            "ok": proc.returncode == 0,
            "stage": "make_video",
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-4000:],
            "files": files,
            "frame_count": len(frames),
            "player": "previews/latest/index.html" if "index.html" in files else None,
        }


# 预览驱动脚本：在工作区里跑，用被测项目自己的接口。
# 示例素材用项目自己的 new_image/write_png 生成 —— 那本身就是契约的一部分，
# 生成不出来说明素材层还没实现，预览失败的信息量比"跳过"大。
_PREVIEW_DRIVER = '''\
"""由 pciso 生成的预览驱动脚本（不属于被测项目，落在 state/ 下）。"""
from __future__ import annotations

import json
import os
import sys

# 自己把工作区放上 sys.path。
# `python script.py` 的 sys.path[0] 是**脚本目录**（在 state/ 下），不是 cwd，
# 所以只靠 PYTHONPATH 环境变量是脆的 —— 实测就踩了：子进程报 No module named 'drawvid'。
sys.path.insert(0, os.getcwd())


def main() -> int:
    """生成示例素材并调用工作区的 make_video，最后打印一行 JSON 结果。"""
    # 参数从 UTF-8 的 JSON 文件读 —— 见调用处的说明（argv 会毁掉中文）。
    with open(sys.argv[1], encoding="utf-8") as fh:
        params = json.load(fh)
    sentence = params["sentence"]
    assets_dir = params["assets_dir"]
    out_dir = params["out_dir"]

    made = 0
    try:
        from drawvid.assets import new_image, write_png

        # 素材要有**结构**，不能是纯色：纯色素材上 Ken Burns 的位移缩放完全看不出来，
        # 那样预览就无法用来判断渲染器好不好（实测踩过）。
        sizes = [(96, 72), (72, 96), (96, 96)]
        palette = [(40, 90, 160, 255), (160, 70, 60, 255), (60, 150, 110, 255)]
        for i, (w, h) in enumerate(sizes):
            img = new_image(w, h, palette[i])
            accent = (255, 255, 255, 200)
            dark = (16, 16, 16, 255)
            for y in range(h):
                for x in range(w):
                    if (x + y) % 16 < 3:
                        img.put_pixel(x, y, accent)
                    elif x < w // 5 and y < h // 5:
                        img.put_pixel(x, y, dark)
                    elif abs(x - y) < 3:
                        img.put_pixel(x, y, dark)
            write_png(img, f"{assets_dir}/sample_{i}.png")
            made += 1
    except Exception as exc:  # noqa: BLE001 - 素材层没实现就退回纯图形
        print(f"[preview] 示例素材生成失败，退回纯图形：{type(exc).__name__}: {exc}",
              file=sys.stderr)

    try:
        from drawvid.pipeline import make_video
    except Exception:
        # 失败时把定位信息一起吐出来 —— 这个端点最可能的失败原因是"工作区根本不对"，
        # 而那种情况下只报 ModuleNotFoundError 会让人往错的方向查。
        print(f"[preview] cwd={os.getcwd()}", file=sys.stderr)
        print(f"[preview] sys.path 前 4 项={sys.path[:4]}", file=sys.stderr)
        try:
            print(f"[preview] cwd 内容={sorted(os.listdir('.'))[:20]}", file=sys.stderr)
        except OSError as exc:
            print(f"[preview] 列目录也失败：{exc}", file=sys.stderr)
        raise

    result = make_video(sentence, assets_dir, out_dir)
    print(json.dumps({"apng": result, "assets": made}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_round_report(session: Session, out_path: Path) -> Path:
    """把 metrics 落成 JSON，便于跨组画曲线。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(session.metrics(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path
