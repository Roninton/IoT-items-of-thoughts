"""OpenAI 兼容的「IoT 代理」：**每个 thinking block 换成一条思考条目链**。

仓库名就是规格：IoT = **Items of Thoughts**。做的不是"把对话压小"，而是把模型每写完
的一段思考**提纯成条目**（正节点 / 负节点 / 待定 / 风险），其余原样保留。

代理在 DSH 这条路上只做三件事：

  1. **转发**：客户端拿什么来就往上发什么（`tools`、采样参数一个字段都不改），
     上游的 `tool_calls`、思考、`finish_reason` 原样流回去。吞掉 `tools`，模型就没有
     工具可调 —— 那就是"工具调用没了"的真因。
  2. **替换历史**：发上去的消息只有 `客户端 system + P + C + 用户原话 + 本轮展开的原文`。
     更早的对话（含工具往返）不再回灌 —— 它们已经被 C 代表。
  3. **编排思考**：**一个 thinking block 一次**（触发点是这个块结束），把它提纯成条目写进 C。

三条边界（用户裁定）：

  · **只压 thinking 块。** 工具调用与工具结果是**事实块**，由 `absorb_actions` 从落盘结果
    确定性地渲染成 ACT（零模型调用）；正文不进压缩器（它要原样留给人看）。
  · **用户原话逐字回流**，不进 C —— 压缩用户指令等于改写需求。
  · **不做预算裁剪**：C 的规模由"块数 × 每块几条"天然决定。按显著度裁是我们替模型猜它
    以后需不需要，实测把 salience=85 的事实都裁掉了（`trimmed: N3(budget, salience=85)`）。
  · 上游报错**把原文透给客户端**，吞成 500 只会让人以为代理坏了。

压缩产物照旧写进 `rounds.jsonl` 的 `c_i`，所以 `/session/{id}/state`、`/sessions`、
`/metrics` 在这条路上照样能看。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .actnodes import describe_verification
from .openaiapi import (
    ROOT,
    ChatCompletionRequest,
    _error,
    _resolve_session,
    _session_key,
    _split_model,
    _strip_reminders,
    _text_of,
)
from .render import render_state_block, render_user_words

MODE_ENV = "PCISO_OPENAI_MODE"
SSE_DUMP_ENV = "PCISO_PROXY_SSE_DUMP"  # =1 时把上游/回给客户端的 SSE 字节原样落盘（排查闪现）
PROXY_CONFIG_ENV = "PCISO_OPENAI_PROXY_CONFIG"
DEFAULT_PROXY_CONFIG = "configs/openai_proxy.yaml"
# 锚点要落到盘上才叫"落地"。代理不知道客户端的工作目录，所以只能在服务端配置一处；
# 不配也能跑，只是锚点解析会退化成"未落地"（不会因此丢节点，见 merge.apply_delta 的注释）。
WORKSPACE_ENV = "PCISO_PROXY_WORKSPACE"
# 上下文策略：核心机制只有一个开关 —— 块结束后**立刻**压，并把这一块换成紧凑版。
#   iot  = 每个 thinking 块结束后立刻编排，上下文里那一块换成条目（**默认，被测的那条**）
#   raw  = 一样替换历史，但块保持原文（分离出"提纯"这一步的影响）
#   drop = 一样替换历史，但块直接丢弃（朴素压缩基线）
#   full = 什么都不做：全量历史原样回灌（标准 agent 基线）
CONTEXT_ENV = "PCISO_PROXY_CONTEXT"
CONTEXT_MODES = ("iot", "raw", "drop", "full")
DEFAULT_CONTEXT_MODE = "iot"
DUMP_ENV = "PCISO_PROXY_DUMP"  # =1 时把客户端真实请求体原样落盘（排查"它到底发了什么"）
# 块级替换的账本长度（key=块 sha256 前 16 位 → 紧凑版正文）
ITEMS_MAX = 64

# 上下文块里回流多少条用户原话（与 render.render_user_words 同一口径）
USER_WORDS_MAX = 24
# 幂等账本的长度上限：块/工具调用都是只增的，长会话不能让它无限长。
THINKING_DONE_MAX = 64
TOOLS_DONE_MAX = 200
# ACT 节点要读落盘文件的正文（抽符号、判"草稿里 import 过但最终没有"），给个上限。
SOURCE_MAX_CHARS = 20000
SOURCE_MAX_FILES = 8


def mode() -> str:
    """`proxy`（默认，转发 + 替换上下文 + 编排思考）或 `round`（旧的"每个请求跑一整轮"）。"""
    return (os.environ.get(MODE_ENV) or "proxy").strip().lower()


def config_path() -> str:
    env = (os.environ.get(PROXY_CONFIG_ENV) or "").strip()
    if env:
        return str(Path(env).resolve())
    return str(ROOT / DEFAULT_PROXY_CONFIG)


def context_mode() -> str:
    """上下文策略（四臂对照用），见 `CONTEXT_MODES` 的注释。"""
    value = (os.environ.get(CONTEXT_ENV) or DEFAULT_CONTEXT_MODE).strip().lower()
    return value if value in CONTEXT_MODES else DEFAULT_CONTEXT_MODE


def block_key(block: str) -> str:
    """块的身份：原文的 sha256 前 16 位。客户端回传的那份也是按它认。"""
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]


def _items_map(session: Any) -> dict[str, str]:
    meta = session.store.read_meta()
    raw = meta.get("proxy_items")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def swap_block(reasoning: str, items: dict[str, str], mode: str) -> str:
    """一个块该以什么形态进上下文：紧凑版（条目）/ 丢掉 / 原样。

    拿不到紧凑版（编排失败、或这个块不是我们压的）时**保留原文** —— 宁可多花 token，
    也不要静默把一段思考弄没。
    """
    if mode == "drop":
        return ""
    if mode == "iot":
        return items.get(block_key(reasoning)) or reasoning
    return reasoning


def workspace_root(session: Any) -> Path | None:
    """锚点解析/落盘核对用的根目录：环境变量优先，其次会话配置里的 `workspace_path`。"""
    env = (os.environ.get(WORKSPACE_ENV) or "").strip()
    if env:
        candidate = Path(env)
        return candidate if candidate.is_dir() else None
    configured = getattr(session.cfg, "workspace_path", None)
    if configured:
        path = Path(configured)
        return path if path.is_dir() else None
    return None


# --------------------------------------------------------------------------- #
# 消息切分
# --------------------------------------------------------------------------- #


# DSH 会把「运行时上下文 / 技能目录」当成 **user 消息追加在本轮真实提问之后**（实测顺序：
# [system][人的提问][workspace 提醒][runtime context][技能目录]）。这些不是人说的话：
# 既不能算本轮起点（否则真提问被当成"历史"丢掉 —— 症状是模型说"我只看到运行时快照和技能目录，
# 没有任务"），也不该进「用户原话」清单污染 C。
_INJECTED_USER_HEADS = ("Current runtime context", "Current DSH file policy")


def _is_injected_user(content: Any) -> bool:
    """这条 user 消息是不是"客户端注入的上下文"，而不是人打的字。"""
    text = _strip_reminders(_text_of(content) or "").strip()
    if not text:
        return True
    return text.startswith(_INJECTED_USER_HEADS)


def last_user_index(messages: Sequence[dict[str, Any]]) -> int | None:
    """本轮起点 = 最后一条**真** user 消息的下标（剥包装层、并跳过注入的上下文）。

    从这里往后是正在展开的这一轮（原样转发，格式必须合法，注入的上下文也一起原样带出去），
    往前是已经被 C 代表的过去（不再回灌）。
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "user":
            continue
        if not _is_injected_user(msg.get("content")):
            return i
    return None


def _tool_calls_of(msg: dict[str, Any]) -> list[dict[str, Any]]:
    calls = msg.get("tool_calls")
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _thinking_of(msg: dict[str, Any]) -> str:
    """从**响应**里取思考：三个字段名都认（pi-ai 的 REASONING_FIELDS 同款）。"""
    for key in ("reasoning_content", "reasoning", "reasoning_text"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _user_words_in(messages: Sequence[dict[str, Any]]) -> list[str]:
    """人打过的字（逐字、不压缩）。注入的运行时上下文/技能目录不算用户原话。"""
    out: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        if _is_injected_user(msg.get("content")):
            continue
        text = _strip_reminders(_text_of(msg.get("content"))).strip()
        if text:
            out.append(text)
    return out


_PATH_KEYS = ("path", "file_path", "filepath", "filename", "file", "target", "target_file")
_WRITE_TOOL_RE = re.compile(r"write|edit|create|patch|replace|insert|append|apply", re.IGNORECASE)


def _norm_path(root: Path | None, raw: str) -> str:
    """把路径归一成「相对工作区根」的 posix 形式（求不出来时就只做分隔符/前缀归一）。

    为什么必须归一：工具给的是**绝对**路径（DSH 实测 `D:\\_playground\\workspace\\oneline\\out\\x.png`），
    模型写的 anchor 是**相对**路径（`oneline/out/x.png`）。不归一化两边永远对不上 ——
    真机后果是 ACT 一条都落不下来，带路径的思考节点还被当「编路径」丢掉（`kept=0`）。
    """
    s = str(raw or "").replace("\\", "/").strip()
    if not s:
        return ""
    if root is not None:
        try:
            p = Path(s)
            target = p if p.is_absolute() else (root / p)
            return target.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return s[2:] if s.startswith("./") else s


def _path_from_args(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    try:
        args = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key, value in args.items():
        if str(key).strip().lower() in _PATH_KEYS and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def write_calls(
    messages: Sequence[dict[str, Any]], root: Path | None = None
) -> list[tuple[str, str]]:
    """客户端这一轮点过名的写操作：`(tool_call_id, 归一化后的路径)`。

    只看工具的**参数**（客户端发上来的事实）。文件是否真的写了，要等它的工具结果
    回来，并且盘上真有这个文件 —— 那一步在 `absorb_actions` 里做。
    """
    out: list[tuple[str, str]] = []
    for msg in messages:
        for call in _tool_calls_of(msg):
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str((fn or {}).get("name") or call.get("name") or "")
            if not _WRITE_TOOL_RE.search(name):
                continue
            path = _path_from_args((fn or {}).get("arguments") or call.get("arguments"))
            if path:
                out.append((str(call.get("id") or ""), _norm_path(root, path)))
    return out


_EXIT_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)


def _verify_from(texts: Sequence[str]) -> tuple[str, str]:
    """"测试跑到哪一步"只看工具结果里的退出码/通过字样 —— 模型的自述不算数。"""
    state = ""
    for text in texts:
        body = text or ""
        for match in _EXIT_RE.finditer(body):
            state = "passed" if match.group(1) == "0" else "failed"
        low = body.lower()
        if state != "failed" and ("failed" in low or "traceback" in low):
            state = "failed"
        if not state and " passed" in low:
            state = "passed"
    return describe_verification(None if not state else (state == "passed"), "")


def _read_sources(root: Path | None, paths: Sequence[str]) -> dict[str, str]:
    """读落盘文件的正文（给 ACT 抽符号用）—— 只在真有这个文件时才读。"""
    if root is None:
        return {}
    out: dict[str, str] = {}
    for rel in list(dict.fromkeys(paths))[:SOURCE_MAX_FILES]:
        try:
            out[rel] = (root / rel).read_text(encoding="utf-8")[:SOURCE_MAX_CHARS]
        except (OSError, ValueError):
            continue
    return out


def _written_triples(root: Path | None, paths: Sequence[str]) -> list[tuple[str, str, int]]:
    """`(路径, sha256, 字节数)` —— 只有盘上真有这个文件才算"写了"。

    路径一律归一成"相对工作区根"的 posix 形式（`_norm_path`）。根**没配**时，绝对路径
    照样能核对 —— 别因为少配一个环境变量就把整条 ACT 通道静默关掉（真机就是这么丢的）。
    """
    triples: list[tuple[str, str, int]] = []
    for raw in dict.fromkeys(str(p) for p in paths if p):
        norm = _norm_path(root, raw)
        if not norm:
            continue
        target = Path(norm)
        if not target.is_absolute():
            if root is None:
                continue
            target = root / norm
        try:
            data = target.read_bytes()
        except OSError:
            continue
        triples.append((norm, hashlib.sha256(data).hexdigest(), len(data)))
    return triples


# --------------------------------------------------------------------------- #
# 循环检测：工具往返的指纹（零模型调用，纯字符串比对）
# --------------------------------------------------------------------------- #

LOOP_WINDOW = 12
LOOP_REPEAT = 3
STALL_STEPS = 3
LOOP_REPORTED_MAX = 40

_ERROR_HINTS = (
    "does not declare image input",
    "permission denied",
    "no such file or directory",
    "cannot read ",
    "[exit code: 1",
    "[exit code: 2",
    "command not found",
    "access is denied",
    "sandbox: file access denied",
)
_ERROR_HEADS = ("error", "exception", "traceback", "fatal", "panic", "failed")


def result_is_error(text: str) -> bool:
    """工具结果是不是报错 —— 只看退出码与固定报错串，不看模型的自述。"""
    body = (text or "").strip()
    if not body:
        return False
    low = body.lower()
    if any(h in low for h in _ERROR_HINTS):
        return True
    first = low.splitlines()[0].strip()
    return any(first.startswith(h) for h in _ERROR_HEADS)


def tool_trail(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """这一轮工具往返的轨迹（客户端发上来的原始事实）。

    每个元素 `{id, name, args, fp, result, error}`；`fp` 是 `(工具名 + 排序后的参数)`
    的短哈希 —— 循环按指纹聚合，同样的调用换个参数顺序也算同一个。
    """
    results = {
        str(m.get("tool_call_id") or ""): _text_of(m.get("content"))
        for m in messages
        if m.get("role") == "tool"
    }
    trail: list[dict[str, Any]] = []
    for msg in messages:
        for call in _tool_calls_of(msg):
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str((fn or {}).get("name") or call.get("name") or "")
            args = str((fn or {}).get("arguments") or call.get("arguments") or "")
            try:
                norm_args = json.dumps(json.loads(args), sort_keys=True, ensure_ascii=False)
            except ValueError:
                norm_args = args
            call_id = str(call.get("id") or "")
            result = results.get(call_id, "")
            trail.append(
                {
                    "id": call_id,
                    "name": name,
                    "args": norm_args,
                    "fp": hashlib.sha1(f"{name}|{norm_args}".encode()).hexdigest()[:12],
                    "result": result,
                    "error": result_is_error(result),
                }
            )
    return trail


def loop_report(messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """循环/停滞的信号：重复指纹、连续报错、只说不做的步数、成功回执数。"""
    trail = tool_trail(messages)
    window = trail[-LOOP_WINDOW:]
    counts: dict[str, int] = {}
    for step in window:
        counts[step["fp"]] = counts.get(step["fp"], 0) + 1
    failures: dict[str, list[dict[str, Any]]] = {}
    for step in window:
        if step["error"]:
            failures.setdefault(step["fp"], []).append(step)
    failing = [
        (steps[-1]["name"], len(steps), steps[-1]["result"])
        for steps in failures.values()
        if len(steps) >= 2
    ]
    plan_only = 0
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        if _tool_calls_of(msg):
            break
        plan_only += 1
    return {
        "calls": len(trail),
        "window": len(window),
        "repeated": [fp for fp, n in counts.items() if n >= LOOP_REPEAT],
        "failing": sorted(failing, key=lambda item: -item[1]),
        "plan_only_steps": plan_only,
        "ok": sum(1 for step in trail if step["result"] and not step["error"]),
    }


def loop_warning(report: dict[str, Any]) -> str:
    """把循环/停滞写成给模型看的硬事实（挂进请求末尾那块，不占 C）。"""
    lines: list[str] = []
    if report.get("failing"):
        name, count, last = report["failing"][0]
        tail = " ".join((last or "").split())[:200]
        lines.append(
            f"【循环告警】{name} 已经连续 {count} 次返回同一类报错"
            f"（最近一次：{tail or '（无正文）'}）。不要再调用它 —— 换参数、换工具，"
            "或者把这条记成「当前不可用」往下走。"
        )
    if int(report.get("plan_only_steps") or 0) >= STALL_STEPS:
        lines.append(
            f"【停滞告警】最近 {report['plan_only_steps']} 步你只写了话，没有发出任何工具调用。"
            "下一步只允许一件事：发一个工具调用，不要写计划。"
        )
    elif report.get("repeated"):
        lines.append(
            f"【循环告警】同一组调用（指纹 {report['repeated'][0]}）在最近 {report['window']} 步里"
            f"出现了 {LOOP_REPEAT} 次以上 —— 它没有让事情往前走。换个做法。"
        )
    return "\n".join(lines)


def _report_tool_errors(
    session: Any, messages: Sequence[dict[str, Any]], meta: dict[str, Any]
) -> list[tuple[str, int, str]]:
    """同一个工具连续报错 → 失败链 + `[否掉]` 负节点（每个增量只记一次）。"""
    failing = loop_report(messages)["failing"]
    if not failing:
        return []
    reported = {str(k): int(v) for k, v in (meta.get("proxy_loop_reported") or {}).items()}
    fresh = [item for item in failing if item[1] > reported.get(item[0], 0)]
    if not fresh:
        return []
    try:
        session.absorb_tool_failures(errors=[(name, count, last) for name, count, last in fresh])
    except Exception as exc:
        print(f"[v1:proxy] tool errors not recorded: {type(exc).__name__}: {exc}")
        return []
    reported.update({name: count for name, count, _ in fresh})
    session.store.patch_meta(proxy_loop_reported=dict(list(reported.items())[-LOOP_REPORTED_MAX:]))
    return fresh


# --------------------------------------------------------------------------- #
# 事实块：工具往返 → ACT（零模型调用）
# --------------------------------------------------------------------------- #


_CMD_TOOL_RE = re.compile(r"pwsh|bash|shell|terminal|exec|command|^run|_run", re.IGNORECASE)
_CMD_ARG_KEYS = ("command", "cmd", "script", "input", "args")


def _command_with_exit(step: dict[str, Any]) -> tuple[str, int | None] | None:
    """工具调用如果是"跑命令"，返回 `(命令文本, 退出码)`（退出码取 `[exit code: N]`）。

    为什么单拎出来：ACT 原来只认"写文件"，于是「pytest 跑过了」既没有 ACT 可挂、
    又会被"禁完成态断言"挡掉 —— 一条真事实两头落空（真机：`think_dropped_claim`）。
    """
    if not _CMD_TOOL_RE.search(str(step.get("name") or "")):
        return None
    try:
        args = json.loads(str(step.get("args") or "{}"))
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    cmd = next(
        (
            str(args[key])
            for key in _CMD_ARG_KEYS
            if isinstance(args.get(key), str) and str(args[key]).strip()
        ),
        "",
    )
    if not cmd.strip():
        return None
    match = _EXIT_RE.search(str(step.get("result") or ""))
    return cmd.strip(), (int(match.group(1)) if match else None)


def absorb_actions(session: Any, messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把这一轮**新出现**的工具调用（有结果、盘上真有文件 / 有退出码）记成 ACT 节点。

    返回里带 `paths`：这一轮工具点过名的所有路径 —— 思考编排用它当"允许挂的锚点"
    （第一个 thinking 块落地时还没有任何 ACT，只认真写过的会让它全军覆没）。
    跑过的命令也记 ACT（带退出码），否则"测试过了"这类结论没有任何落点。
    """
    meta = session.store.read_meta()
    done = {str(x) for x in (meta.get("proxy_tools_done") or [])}
    results = {
        str(m.get("tool_call_id") or ""): _text_of(m.get("content"))
        for m in messages
        if m.get("role") == "tool"
    }
    root = workspace_root(session)
    turn_paths: list[str] = []
    fresh: list[tuple[str, str]] = []
    for call_id, path in write_calls(messages, root):
        if path not in turn_paths:
            turn_paths.append(path)
        if not call_id or call_id in done or call_id not in results:
            continue
        fresh.append((call_id, path))

    trail = tool_trail(messages)
    fresh_calls = [s for s in trail if s["id"] and s["id"] not in done and s.get("result")]
    commands = [pair for step in fresh_calls for pair in [_command_with_exit(step)] if pair]

    out: dict[str, Any] = {"paths": turn_paths, "act_nodes": 0, "fresh": len(fresh_calls)}
    if fresh or commands:
        triples = _written_triples(root, [p for _, p in fresh])
        verify, verify_state = _verify_from(list(results.values()))
        out["written"] = [t[0] for t in triples]
        if commands:
            out["commands"] = len(commands)
        if triples or commands:
            out.update(
                session.absorb_actions(
                    written=triples,
                    commands=commands,
                    sources=_read_sources(root, [p for _, p in fresh]),
                    verify=verify,
                    verify_state=verify_state,
                    workspace=root,
                )
            )
        session.store.patch_meta(
            proxy_tools_done=[*list(done), *(str(s["id"]) for s in fresh_calls)][
                -TOOLS_DONE_MAX:
            ]
        )
    errors = _report_tool_errors(session, messages, meta)
    if errors:
        out["tool_errors"] = errors
    return out


# --------------------------------------------------------------------------- #
# 思考块：一个块一次编排
# --------------------------------------------------------------------------- #


def flush_thinking(
    session: Any,
    block: str,
    *,
    turn_paths: Sequence[str] = (),
    root: Path | None = None,
) -> dict[str, Any]:
    """**一个 thinking block 一次**：提纯成条目，写进 C。

    同步函数（里面是 session 的落盘/合并流水线），调用方用 `asyncio.to_thread` 丢线程池，
    这样它能和"同一个响应里后面还在生成的正文/工具调用"并行 —— 不额外增加等待。
    """
    if not block.strip():
        return {"compressed": False, "reason": "空 thinking 块"}
    key = block_key(block)
    meta = session.store.read_meta()
    done = [str(x) for x in (meta.get("proxy_think_done") or [])]
    if key in done:
        return {"compressed": False, "reason": "这个块已经编排过"}

    from . import api as _api

    lock = _api._lock_for(session.store.session_id)
    acquired = lock.acquire(timeout=300)
    try:
        result = session.absorb_thinking_block(
            block,
            sources=_read_sources(root, turn_paths),
            workspace=root,
            extra_allowed_paths=turn_paths,
            source="proxy",
        )
    finally:
        if acquired:
            lock.release()
    session.store.patch_meta(proxy_think_done=[*done, key][-THINKING_DONE_MAX:])
    # 紧凑版记账：下一个请求里那一整块原文要换成这几行（块级替换，见 forward_payload）。
    items = [str(x) for x in (result.get("items") or []) if str(x).strip()]
    if items:
        ledger = _items_map(session)
        ledger[key] = "\n".join(items)
        session.store.patch_meta(proxy_items=dict(list(ledger.items())[-ITEMS_MAX:]))
        result["items_text"] = ledger[key]
    session.store.append_event(
        "proxy_think",
        ops=result.get("ops"),
        error=result.get("error"),
        retried=result.get("retried"),
        think_kept=result.get("think_kept"),
        act_nodes=result.get("act_nodes"),
        # 一条都没留 ⇒ 客户端那块退回原文（不压缩）。这个标记是给审计用的。
        raw_fallback=not items,
        c_tokens=result.get("c_tokens"),
        chars=len(block),
        item_chars=sum(len(x) for x in items),
    )
    return result


# --------------------------------------------------------------------------- #
# 组装转发请求
# --------------------------------------------------------------------------- #


def context_block(session: Any) -> str:
    """**固定**的那一半：给模型的说明 + P。

    它每一步都一模一样，所以留在请求头部（客户端 system 之后）—— 前缀缓存能一直吃到它。
    会变的部分（C、用户原话）一律走 `volatile_block()`，挂在**请求末尾**。
    """
    parts = [
        "【上下文替换代理】你的更早对话历史已被下面这段**事实链**替换（原文在客户端那边，"
        "你不需要它）。把它当已知事实用；不要因为看不到原文就重新去读、去重写已经做过的文件。"
        "你的思考正被逐段提纯成这条链上的条目 —— 所以你只需要把话想清楚，不必替它做摘要。",
        session.prefix.render().strip(),
    ]
    return "\n\n".join(p for p in parts if p)


def volatile_block(session: Any, messages: Sequence[dict[str, Any]] = ()) -> str:
    """**会变**的那一半：C（每段思考过后就长长）+ 用户原话（每轮追加）。

    位置很要紧：必须挂在请求**末尾**。前缀缓存算的是"从头开始的最长公共前缀"，头部一变，
    它后面整条尾巴（工具往返、正文、工具结果）全部作废 —— 线上命中率跌到 36% 就是这个。

    实测（`tmp/_cache_layout_ab.py`，8.8K 的 prompt，尾巴是带 tool 结果的真实形状，只让
    这一块变）：插在头部 27.5% → 挂到末尾 91.3% → 完全不带它（上界）98.3%。
    线上低命中那几次的 cached 恰好只剩 ~1920 token（客户端 system + 固定说明），同一个指纹。
    """
    parts = []
    warning = loop_warning(loop_report(messages)) if messages else ""
    if warning:
        # 循环/停滞是**硬事实**（工具的返回就在上面），放在这块的最前面：它比 C 更急。
        parts.append(warning)
    parts.append(render_state_block(session.state()).strip())
    words = session.user_words()[-USER_WORDS_MAX:]
    if words:
        parts.append(render_user_words(words).strip())
    return "\n\n".join(p for p in parts if p)


def _with_context(system_messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    """把上下文块接在第一段 system 的**末尾**（客户端自己的 system 提示保持原样在前）。"""
    if not system_messages:
        return [{"role": "system", "content": block}]
    first = dict(system_messages[0])
    first["content"] = f"{_text_of(first.get('content')).rstrip()}\n\n{block}".strip()
    return [first, *[dict(m) for m in system_messages[1:]]]


def forward_payload(body: dict[str, Any], session: Any, *, mode: str | None = None) -> dict[str, Any]:
    """客户端请求 → 上游请求：换掉 messages，其余字段原样带过去（含 `tools`）。

    发上去的 messages = 客户端 system + **固定**上下文（说明 + P）+ **本轮**（最后一条真 user
    消息之后）的原文 + 末尾一块**会变的**上下文（C + 用户原话）；更早的历史不回灌（这就是
    "用事实链替换全量历史"）。本轮里那些**已经编排过**的 thinking 块，就地换成它们的紧凑版
    （条目）—— 块还是块，只是不再是一整段长文本。

    顺序是按**前缀缓存**定的，不是随手排的：固定 → 追加（本轮） → 会变（C）。任何会变的东西
    放到前面，它后面的一切都要重算。

    `mode` 见 `CONTEXT_MODES`：`full` 走原样透传（标准 agent 基线）。
    """
    mode = mode or context_mode()
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    if mode == "full" or last_user_index(messages) is None:
        # `full`：什么都不做，全量历史原样回灌（这是被测机制的对照组）。
        # 没有真 user 消息：也原样透传，别把事情弄更糟。
        payload = {k: v for k, v in body.items() if k != "stream_options"}
        payload["model"] = session.cfg.work_model.model
        return payload
    start = last_user_index(messages) or 0
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1  # 尾部不能以孤立的 tool 消息开头：上游要它前面那条 tool_calls
    head = [m for m in messages if m.get("role") == "system"]
    items = _items_map(session) if mode == "iot" else {}
    tail = [
        _rewrite_reasoning(m, items, mode)
        for m in messages[start:]
        if m.get("role") != "system"
    ]
    payload = {k: v for k, v in body.items() if k not in ("messages", "model", "stream_options")}
    payload["model"] = session.cfg.work_model.model
    payload["messages"] = _with_context(head, context_block(session)) + tail
    volatile = volatile_block(session, messages)
    if volatile:
        # 会变的那块挂在**最后**（不能跟固定说明一起塞进头部）：见 volatile_block 的实测数字。
        sent = payload["messages"]
        sent.append({"role": "system", "content": volatile})
    if session.cfg.work_model.thinking and "thinking" not in payload:
        payload["thinking"] = {"type": "enabled"}
        payload.setdefault("reasoning_effort", "high")
    return payload


def _rewrite_reasoning(msg: dict[str, Any], items: dict[str, str], mode: str) -> dict[str, Any]:
    """把一个 assistant 消息里的 thinking 块换成它该有的形态（见 `swap_block`）。"""
    if msg.get("role") != "assistant":
        return msg
    block = _thinking_of(msg)
    if not block:
        return msg
    text = swap_block(block, items, mode)
    out = {
        k: v
        for k, v in msg.items()
        if k not in ("reasoning_content", "reasoning", "reasoning_text")
    }
    if text:
        out["reasoning_content"] = text
    return out



# --------------------------------------------------------------------------- #
# 上游
# --------------------------------------------------------------------------- #


def upstream_url() -> str:
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    return f"{base}/chat/completions"


def upstream_headers(session: Any) -> dict[str, str]:
    env = getattr(session.cfg.work_model, "api_key_env", None) or "DEEPSEEK_API_KEY"
    key = os.environ.get(env, "")
    if not key:
        raise RuntimeError(f"环境变量 {env} 未设置 —— 凭据不得写进仓库，请先导出。")
    # `Accept-Encoding: identity`：httpx 默认会跟上游协商 gzip，而 `iter_raw()` 拿到的
    # **仍然是压缩后的字节**（解压只发生在 `iter_bytes()`）。不声明 identity 又原样转发，
    # 客户端收到的是 gzip 流被当成 SSE —— 真机上表现为"一个工具调用都看不见"。
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }


def _timeout() -> httpx.Timeout:
    # 一轮可能几分钟（带思考 + 工具循环），读超时必须关掉；连接超时留着。
    return httpx.Timeout(None, connect=30.0)


def upstream_json(payload: dict[str, Any], session: Any) -> tuple[int, dict[str, Any]]:
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post(upstream_url(), headers=upstream_headers(session), json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": resp.text[:2000] or "上游没有返回 JSON"}}
        return resp.status_code, data if isinstance(data, dict) else {"error": {"message": str(data)}}


def upstream_sse(payload: dict[str, Any], session: Any) -> Iterator[bytes]:
    """SSE **原样**转发：`tool_calls` 的增量、思考、finish_reason 一个字段都不改。"""
    with (
        httpx.Client(timeout=_timeout()) as client,
        client.stream(
            "POST", upstream_url(), headers=upstream_headers(session), json=payload
        ) as resp,
    ):
        if resp.status_code >= 400:
            body = resp.read().decode("utf-8", "replace")[:2000]
            yield _sse_error(f"HTTP {resp.status_code}: {body}")
            return
        for chunk in resp.iter_bytes():  # 不是 iter_raw：上游要是压了，字面转发就是把 gzip 当 SSE 发
            if chunk:
                yield chunk


_SSE_SEQ: dict[tuple[str, str], int] = {}


def _write_sse_dump(session_id: str, side: str, chunks: Sequence[bytes]) -> None:
    """把"上游发来的"或"回给客户端的" SSE 字节原样落盘（env 开关，默认关）。

    排查"客户端到底收到了什么"用：两份 dump 对着看，就知道原文有没有漏出去。
    """

    if not os.environ.get(SSE_DUMP_ENV):
        return
    key = (session_id, side)
    seq = _SSE_SEQ.get(key, 0)
    _SSE_SEQ[key] = seq + 1
    out_dir = Path(config_path()).resolve().parent.parent / "out" / "proxy-sse"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{session_id}-{seq:02d}.{side}.sse").write_bytes(b"".join(chunks))
    except OSError:
        pass


def _sse_error(message: str) -> bytes:
    body = json.dumps({"error": {"message": message, "type": "upstream_error"}}, ensure_ascii=False)
    return f"data: {body}\n\ndata: [DONE]\n\n".encode()


_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")


def _reasoning_event(text: str) -> bytes:
    """把一个 thinking 块（紧凑版）当作上游的思考增量回给客户端。"""
    payload = {"object": "chat.completion.chunk"}
    payload["choices"] = [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _strip_events(events: Sequence[str]) -> list[bytes]:
    """完整 SSE 事件 → 去掉思考字段后的事件（思考要换成本地编排的紧凑版再回）。

    只含思考的事件整条丢掉；`[DONE]` 也在这里丢掉 —— 它由收口处统一发，
    否则"块还没换完就已经收到 [DONE]"，客户端会以为这一轮结束了。
    """
    out: list[bytes] = []
    for event in events:
        payload = ""
        for line in event.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                break
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            out.append(f"data: {payload}\n\n".encode())
            continue
        if not isinstance(data, dict):
            continue
        emptied = 0
        choices = [c for c in (data.get("choices") or []) if isinstance(c, dict)]
        for choice in choices:
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            had = any(key in delta for key in _REASONING_KEYS)
            for key in _REASONING_KEYS:
                delta.pop(key, None)
            if had and not delta:
                emptied += 1
        if choices and emptied == len(choices) and not data.get("usage"):
            continue
        out.append(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
    return out



# --------------------------------------------------------------------------- #
# 流式：一边转发，一边在 thinking 块结束处开始编排
# --------------------------------------------------------------------------- #


class StreamScan:
    """从原样转发的 SSE 里捞出：**按块**的思考原文、正文、工具名、用量。

    块边界只按**字段**判：`delta.reasoning_content` 出现＝块开始，`delta.content` /
    `tool_calls` 出现＝块结束。**绝不按文本判** —— 正文里写一段"思考块样式"的输出
    （哪怕它逐字引用 `"reasoning_content"`、`<thinking>`、【思考块】）都不该被当成思考：
    那是最容易误匹配的地方，而它是客户端看得见的正文字节。

    一次响应里可以有**多个**块（工具往返之间各一段思考）；每块结束各自编排一次，
    谁也不吞谁的原文。上游是逐字段发的，所以"块结束"这个锚点是稳定的：
    它不依赖模型愿不愿意写 `</thinking>` 这种标记。
    """

    def __init__(self) -> None:
        self._pending = ""
        # 思考原文用**位置**取：块开始时记下 len(self._raw) 作起点，结束时 end=len(self._raw)，
        # 于是 self._raw[start:end] 就是这一块 —— 不用维护"当前块"的副本，也不会漂。
        self._raw = ""
        self._open_start: int | None = None  # None = 当前没有开着的块
        self._closed: list[str] = []
        self.spans: list[tuple[int, int]] = []  # 已关掉的块 [(start, end), ...]
        self.content = ""
        self.tools: list[str] = []
        self.usage: dict[str, Any] = {}
        self.bytes = 0

    def feed(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", "replace")
        self.bytes += len(chunk)
        self._pending += text
        while "\n\n" in self._pending:
            event, self._pending = self._pending.split("\n\n", 1)
            self._event(event)

    def _event(self, event: str) -> None:
        for line in event.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(data.get("usage"), dict):
                self.usage = data["usage"]
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                thought = ""
                for key in _REASONING_KEYS:
                    piece = delta.get(key)
                    if isinstance(piece, str) and piece:
                        thought = piece
                        break
                body = delta.get("content")
                body = body if isinstance(body, str) else ""
                calls = delta.get("tool_calls") or []
                if thought:
                    if self._open_start is None:
                        self._open_start = len(self._raw)  # 块的**起点**
                    self._raw += thought
                if body:
                    self.content += body
                for call in calls:
                    name = ((call.get("function") or {}) or {}).get("name")
                    if name and name not in self.tools:
                        self.tools.append(name)
                # 同一个 delta 里既有思考又有正文时，这段思考属于**正在关掉**的那一块。
                if self._open_start is not None and (body or calls):
                    self._close_block()

    def _close_block(self) -> None:
        """块的**终点**就是此刻的 `len(self._raw)`；切片取原文、不复制。"""
        if self._open_start is None:
            return
        start, end = self._open_start, len(self._raw)
        self._open_start = None
        text = self._raw[start:end].strip()
        if text:
            self._closed.append(text)
            self.spans.append((start, end))

    def take_closed_block(self) -> str:
        """取出一个刚刚结束的块（没有就返回空串）。"""
        return self._closed.pop(0) if self._closed else ""

    def take_open_block(self) -> str:
        """流结束时还开着的块（最后一句话只有思考、没有正文）。"""
        if self._open_start is None:
            return ""
        self._close_block()
        return self.take_closed_block()

    @property
    def thinking(self) -> str:
        """当前**还开着**的块（已关掉的块不在里面 —— 它们已经交付过了）。"""
        if self._open_start is None:
            return ""
        return self._raw[self._open_start :].strip()

    @property
    def thinking_closed(self) -> bool:
        return bool(self._closed)


def _pump(gen: Iterator[bytes], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """在**一个**工作线程里把同步的上游流推给事件循环（httpx 客户端不跨线程用）。"""
    try:
        for chunk in gen:
            asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
    except Exception as exc:  # 连接断了也得让客户端看到原因
        asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
    finally:
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #


async def chat_completions(request: Request) -> Any:
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _error(400, f"请求体不是合法 JSON：{exc}")
    if not isinstance(body, dict):
        return _error(400, "请求体必须是 JSON 对象")
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return _error(400, "messages 为空：至少给一条消息")

    model_name, prefix_name = _split_model(str(body.get("model") or "pciso"))
    if not prefix_name:
        prefix_name = "general"
    parsed = ChatCompletionRequest.model_validate(body)
    key = _session_key(parsed, request)
    force_new = (request.headers.get("x-pciso-new") or "").strip() in ("1", "true", "yes")
    try:
        session, created = _resolve_session(
            key=key,
            prefix_name=prefix_name,
            cfg_path=config_path(),
            force_new=force_new,
            project=prefix_name,
        )
    except Exception as exc:  # HTTPException 与配置错误都从这里出去，别吞成 500
        status = getattr(exc, "status_code", 400)
        return _error(status, str(getattr(exc, "detail", exc)))
    _ = created

    sid = session.store.session_id
    from . import api as _api

    if os.environ.get(DUMP_ENV):
        try:
            dump_dir = Path(config_path()).resolve().parent.parent / "out" / "proxy-dump"
            dump_dir.mkdir(parents=True, exist_ok=True)
            seq = len(list(dump_dir.glob(f"{sid}-*.json"))) + 1
            (dump_dir / f"{sid}-{seq:02d}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    ctx_mode = context_mode()
    # 事实块先记：工具往返 → ACT 节点（零模型调用）。忙就跳过 —— 不能因为记事实挡住说话。
    info: dict[str, Any] = {"paths": [], "act_nodes": 0}
    lock = _api._lock_for(sid)
    if ctx_mode == "full":
        session.store.append_event("proxy_actions_skipped", reason="full 基线：不做上下文替换")
    elif lock.acquire(blocking=False):
        try:
            info = absorb_actions(session, messages)
        except Exception as exc:
            session.store.append_event("proxy_actions_failed", error=f"{type(exc).__name__}: {exc}")
            info = {"paths": [], "act_nodes": 0, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.release()
    else:
        session.store.append_event("proxy_actions_skipped", reason="会话忙")

    try:
        payload = forward_payload(body, session)
        # 提前失败：缺 API key 这类问题不该等到 SSE 已经开始流了才炸（那时只能吐一个 error 事件）
        _ = upstream_headers(session)
    except Exception as exc:
        return _error(503, f"{type(exc).__name__}: {exc}", "upstream_error")

    root = workspace_root(session)
    turn_paths = list(info.get("paths") or [])
    out_headers = {
        "X-PCISO-Session": sid,
        "X-PCISO-Mode": "proxy",
        "X-PCISO-Act-Nodes": str(info.get("act_nodes", 0)),
    }
    print(
        f"[v1:proxy] sid={sid} ctx={ctx_mode} msgs={len(messages)} "
        f"turn_start={last_user_index(messages)} "
        f"actions=+{info.get('act_nodes', 0)} paths={len(turn_paths)} "
        f"stream={bool(body.get('stream'))}",
        flush=True,
    )

    if not body.get("stream"):
        try:
            status, out = upstream_json(payload, session)
        except Exception as exc:
            return _error(503, f"{type(exc).__name__}: {exc}", "upstream_error")
        if status >= 400:
            # 上游拒绝的原文要留一份：代理最容易出的事就是"客户端要什么我就转发什么"，
            # 而上游对某些组合是不接受的（实测：thinking 模式下不许 tool_choice 指定函数）。
            session.store.append_event(
                "proxy_upstream_error",
                status=status,
                stream=False,
                body=json.dumps(out, ensure_ascii=False)[:500],
            )
            return JSONResponse(status_code=status, content=out, headers=out_headers)
        block = _thinking_of((out.get("choices") or [{}])[0].get("message") or {})
        think: dict[str, Any] = {"ops": 0, "reason": f"{ctx_mode} 基线不编排"}
        if ctx_mode == "iot":
            try:
                think = await asyncio.to_thread(
                    flush_thinking, session, block, turn_paths=turn_paths, root=root
                )
            except Exception as exc:
                think = {"error": f"{type(exc).__name__}: {exc}"}
                session.store.append_event("proxy_think_failed", error=think["error"])
        if block and ctx_mode in ("iot", "raw", "drop"):
            # 非流式同样"压缩后返回"：客户端拿到的那个 thinking 块就是紧凑版。
            message = ((out.get("choices") or [{}])[0] or {}).get("message") or {}
            compact = ""
            if ctx_mode == "iot":
                compact = (think.get("items_text") or "").strip() or block
            elif ctx_mode == "raw":
                compact = block
            for key in _REASONING_KEYS:
                message.pop(key, None)
            if compact:
                message["reasoning_content"] = compact
        out["model"] = model_name
        out["x_pciso"] = {
            "session_id": sid,
            "mode": "proxy",
            "c_rounds": session.store.round_index(),
            "think_ops": think.get("ops", 0),
            "think_error": think.get("error"),
            "act_nodes": info.get("act_nodes", 0),
        }
        session.store.append_event(
            "proxy_call",
            usage=(out.get("usage") or {}),
            tools=len(payload.get("tools") or []),
            stream=False,
        )
        return JSONResponse(content=out, headers=out_headers)

    scan = StreamScan()
    flush_state: dict[str, Any] = {}

    async def body_stream() -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        worker = threading.Thread(
            target=_pump, args=(upstream_sse(payload, session), loop, queue), daemon=True
        )
        worker.start()
        strip_buf = ""
        flushed_blocks = 0
        # 每次发出紧凑版时，上游已经喂进来多少字节。≈0 说明紧凑版走在正文/工具之前（就地替换）；
        # ≈scan.bytes 说明这块一直没关，直到流末尾才补发（客户端会看到 thinking 落在最后）。
        emit_marks: list[int] = []
        up_buf: list[bytes] = []

        async def emit_compact(block: str) -> str:
            """编排这一块，返回该回给客户端的文本（拿不到紧凑版就回原文）。

            触发点就是**块结束**这一个事件 —— 它由上游的字段边界给出（见 `StreamScan`），
            不是猜的、也不是靠模型写标记。
            """
            if not block or ctx_mode != "iot":
                return "" if ctx_mode == "drop" else block
            state: dict[str, Any] = {}
            try:
                state.update(
                    await asyncio.to_thread(
                        flush_thinking, session, block, turn_paths=turn_paths, root=root
                    )
                )
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                session.store.append_event("proxy_think_failed", error=state["error"])
            flush_state.update(state)
            if state.get("error") or not state:
                # 编排失败（上游报错/超时/没拿到结果）：宁可回原文，也不静默吞掉一段思考。
                return block
            # 编排成功但一条都没留：**退回不压缩** —— 原文照发，不加工也不截断。
            # 截断过的原文比原文更像失真（真机见过：英文块没有 。！？ 可切，被硬截成
            # `… I alre…`，读起来像提纯结果，其实只是把一句话切了一半）。
            return (state.get("items_text") or "").strip() or block

        def forward(raw: bytes) -> list[bytes]:
            """转发的字节：剥掉思考字段、攒够一个完整事件再发、[DONE] 留着由收口发。"""
            nonlocal strip_buf
            strip_buf += raw.decode("utf-8", "replace")
            if "\n\n" not in strip_buf:
                return []
            events = strip_buf.split("\n\n")
            strip_buf = events.pop()
            return _strip_events(events)

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                up_buf.append(item)
                scan.feed(item)
                block = scan.take_closed_block()
                if block:
                    flushed_blocks += 1
                    # 块结束 → 先编排、先把紧凑版回给客户端，再转发"关掉这个块"的那个 chunk。
                    # 紧凑版就是客户端看到的那个 thinking 块；正文与工具调用一个字段都不动。
                    # （"边压边回"能再顺滑一点，属于次要功能，先不做。）
                    text = await emit_compact(block)
                    if text:
                        emit_marks.append(scan.bytes - len(item))
                        yield _reasoning_event(text)
                for chunk in forward(item):
                    yield chunk
        except Exception as exc:
            yield _sse_error(f"{type(exc).__name__}: {exc}")
        finally:
            # 只有思考、没有正文的那一块（或者流被截断）：到这里补上，别让它凭空消失。
            tail = scan.take_open_block()
            if tail:
                flushed_blocks += 1
                text = await emit_compact(tail)
                if text:
                    emit_marks.append(scan.bytes)
                    yield _reasoning_event(text)
            yield b"data: [DONE]\n\n"
            session.store.append_event(
                "proxy_call",
                usage=scan.usage,
                tools=len(payload.get("tools") or []),
                tool_calls=scan.tools,
                stream=True,
                bytes=scan.bytes,
                ctx=ctx_mode,
                think_blocks=flushed_blocks,
                think_spans=scan.spans,
                think_emit_bytes=emit_marks,
                think_ops=flush_state.get("ops", 0),
                think_error=flush_state.get("error"),
                act_nodes=info.get("act_nodes", 0),
            )
            _write_sse_dump(sid, "upstream", up_buf)

    async def tee_client(gen: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """客户端可见的字节单独收一份（early-close 也要落，所以用 finally）。"""

        chunks: list[bytes] = []
        try:
            async for chunk in gen:
                chunks.append(chunk)
                yield chunk
        finally:
            _write_sse_dump(sid, "client", chunks)

    return StreamingResponse(
        tee_client(body_stream()),
        media_type="text/event-stream",
        headers={**out_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
