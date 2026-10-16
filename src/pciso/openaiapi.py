"""OpenAI 兼容入口 —— 让任何 OpenAI 客户端（DSH、curl、官方 SDK）把 pciso 当模型用。

    GET  /v1/models              列出可用模型（`pciso` + 每个 prefix 一个 `pciso@<name>`）
    POST /v1/chat/completions    一个请求 = 一轮

**协议无状态，而 pciso 的核心是有状态**（P 冻结 + C 事实链 + 轮次）。两者的接缝就是
"会话身份"，按可靠性分三层：

  1. 请求头 `X-PCISO-Session` / `X-Session-Id` —— 显式指定，最可靠。这个值可以是
     (a) 一个自选的会话名（server 记在 `state/openai_sessions.json` 里），或者
     (b) **本 server 的会话 id**（也就是上一次回传头里的那个值，照抄回来即可）。
  2. 请求体 `user` 字段 —— OpenAI 协议里这个字段本来就是给"终端用户/会话标识"用的。
  3. 兜底：**首条 user 消息**的 sha256 前 16 位。同一个对话的第一句话通常不变，
     于是"同一个对话 = 同一个会话"。代价是：两个对话若第一句话一模一样，会并到
     一个会话里 —— 想避免就显式给 1 或 2。

`X-PCISO-New: 1` 强制开新会话（回传头里永远带 `X-PCISO-Session`，客户端可以照抄下来钉死）。

**历史不回流**：客户端每次都会把整段对话发过来，但我们只取**最后一条 user 消息** ——
之前发生过什么由 C 代表。这正是"用事实链替换全量历史"那条要求落到协议上的样子，
而不是把客户端的 history 截断一下再用。

模型名可以选 prefix：`pciso`（默认） / `pciso@brush_paint` / `pciso/brush_paint`。
换 prefix 就是换一份冻结的 P —— 会话会跟着分开（身份里含 prefix 名）。

真机一轮 1~2 分钟。`stream: true` 时中途发 SSE 注释行做 keepalive，
免得客户端把"模型在干活"当成"连接死了"。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = "configs/openai_chat.yaml"
DEFAULT_PREFIX = "prefixes/general"
KEEPALIVE_S = 15.0

router = APIRouter()

_REG_LOCK = threading.Lock()


def _api() -> Any:
    """延迟取 api 模块。

    api.py 在模块级 `include_router(openaiapi.router)`，所以这里**不能**在顶层
    反向 import —— 会成环（api 还没定义完就被重新进入）。运行时再取就没这问题，
    而且测试 monkeypatch `api.DEFAULT_STATE_DIR` 之后我们读到的也是新值。
    """
    from . import api as _a

    return _a


# --------------------------------------------------------------------------- #
# 会话身份
# --------------------------------------------------------------------------- #


def _config_path() -> str:
    """`/v1` 用哪份配置。环境变量可换（测试与多套前缀都用它，不用改代码）。"""
    env = (os.environ.get("PCISO_OPENAI_CONFIG") or "").strip()
    return str(Path(env).resolve()) if env else str(ROOT / DEFAULT_CONFIG)


def _registry_file() -> Path:
    return Path(_api()._state_dir()) / "openai_sessions.json"


def _read_registry() -> dict[str, Any]:
    p = _registry_file()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_registry(data: dict[str, Any]) -> None:
    p = _registry_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _text_of(content: Any) -> str:
    """OpenAI 的 content 允许是字符串，也允许是分块数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (None, "text", "input_text"):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _split_model(model: str) -> tuple[str, str]:
    """模型名 -> (回给客户端的名字, prefix 名)。识别不了就返回默认 prefix。"""
    m = (model or "").strip()
    if not m:
        return "pciso", ""
    for sep in ("@", "/", ":", "-"):
        if sep in m:
            head, tail = m.split(sep, 1)
            if head.lower().startswith("pciso") and tail.strip():
                return m, tail.strip()
    return m, ""


def _strip_reminders(text: str) -> str:
    """剥掉客户端塞进 user 消息里的包装层（典型是 DSH 的技能目录 `<system-reminder>` 块）。

    它**不是用户说的话**。实测代价有两条：
      1. 当成本轮任务发给工作模型 → 整轮跑偏（它开口就谈技能目录，没干正事）；
      2. 拿它当会话指纹 → 同一个 DSH 对话每轮开一个新 pciso 会话。
    所以：进门先剥，剥完是空的就当这条消息不存在。
    """
    out = text
    while "<system-reminder>" in out:
        head, _, rest = out.partition("<system-reminder>")
        if "</system-reminder>" in rest:
            _, _, tail = rest.partition("</system-reminder>")
            out = f"{head} {tail}"
        else:  # 没闭合：后面全算包装层（宁可少一句，也不要把目录当任务）
            out = head
    return out.strip()


def _first_user_message(messages: list[ChatMessage]) -> str:
    for msg in messages:
        if msg.role == "user":
            text = _strip_reminders(_text_of(msg.content))
            if text:
                return text
    return ""


def _session_key(req: ChatCompletionRequest, request: Request) -> str:
    for header in ("x-pciso-session", "x-session-id", "x-conversation-id"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return f"hdr:{value}"
    if (req.user or "").strip():
        return f"user:{req.user.strip()}"
    first = _first_user_message(req.messages)
    if not first:
        first = _text_of(req.messages[-1].content) if req.messages else ""
    return "fp:" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:16]


def _existing_chat_session(session_id: str) -> Any | None:
    """头里给的值已经是本 server 的会话 id 时直接认下来。

    只认 `arm == chat` 的会话：实验臂（arm_a..g）是**对照数据**，谁都不能从
    OpenAI 入口往它们身上追加轮次 —— 那会把一条实验轨迹搅成半对话半实验的东西，
    而且事后看不出来是哪几轮被污染了。
    """
    api = _api()
    # 先按文件判存在：`api._load` 会构造 SessionStore，而它的 __init__ 会 mkdir ——
    # 那样每来一个没见过的头值都会在 state/sessions/ 下留一个空目录（实测踩过）。
    meta = Path(api._state_dir()) / "sessions" / session_id / "session.json"
    if not meta.exists():
        return None
    try:
        session = api._load(session_id)
    except HTTPException:
        return None
    return session if session.cfg.arm == "chat" else None


def _resolve_session(
    *, key: str, prefix_name: str, cfg_path: str, force_new: bool, project: str
) -> tuple[Any, bool]:
    """按 key 拿会话；没有（或对不上 prefix/配置）就开一个。返回 (Session, 是否新建)。"""
    api = _api()
    from .models import SessionCreateRequest

    with _REG_LOCK:
        # 头里给的如果**已经是本 server 的会话 id**（客户端把上一次回传的
        # X-PCISO-Session 原样发回来，这是最自然的用法），就直接接上。
        # 没有这一条，"照抄回传头"会被当成一个没见过的新 key -> 每轮悄悄开一个新会话，
        # 而客户端那边完全看不出来（它以为自己一直在同一个会话里）。
        if key.startswith("hdr:") and not force_new:
            direct = _existing_chat_session(key[4:])
            if direct is not None:
                return direct, False

        registry = _read_registry()
        entry = registry.get(key)
        if entry and not force_new and entry.get("prefix") == prefix_name and entry.get(
            "config"
        ) == cfg_path:
            try:
                return api._load(entry["session_id"]), False
            except HTTPException:
                pass  # 会话目录被人删了：重建一个，别让客户端从此报 404

        req = SessionCreateRequest(
            project=project,
            prefix_path=str(ROOT / "prefixes" / prefix_name) if prefix_name else "",
            config_path=cfg_path,
            overrides={"workspace_path": None},
        )
        resp = api.create_session(req)
        registry[key] = {
            "session_id": resp.session_id,
            "prefix": prefix_name,
            "config": cfg_path,
            "created_at": time.time(),
        }
        _write_registry(registry)
        return api._load(resp.session_id), True


# --------------------------------------------------------------------------- #
# 协议模型（宽容解析：客户端多发的字段一律忽略）
# --------------------------------------------------------------------------- #


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "pciso"
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None
    metadata: dict[str, Any] | None = None


def _error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    """OpenAI 形状的错误体：客户端（含 DSH）认这个，不认 FastAPI 的 `detail`。"""
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": kind, "code": None}}
    )


def _usage_of(out: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 形状的用量，取**真实 token**（`record.usage`），不再拿字符数冒充。

    之前用 `hidden.delta_raw_chars` 填 completion_tokens，客户端于是显示 0 tok ——
    那个数是压缩器原始 JSON 的字符数，跟这一轮花了多少 token 没关系。
    """
    usage = out.get("usage") or {}
    hidden = out.get("hidden") or {}
    upstream = out.get("upstream") or {}
    prompt = int(usage.get("work_prompt_tokens") or out.get("prompt_tokens") or 0)
    completion = int(usage.get("work_completion_tokens") or 0)
    cached = int(usage.get("work_cached_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "x_pciso_c_tokens": int(out.get("c_tokens") or 0),
        "x_pciso_history_mode": 1 if upstream.get("history_mode") == "state" else 0,
        "x_pciso_compressor_tokens": int(usage.get("compressor_prompt_tokens") or 0)
        + int(usage.get("compressor_completion_tokens") or 0),
        "x_pciso_thinking_chars": int(hidden.get("thinking_chars") or 0),
    }


def _message_of(out: dict[str, Any]) -> dict[str, Any]:
    """assistant 消息：正文 + 思考。

    思考以 `reasoning_content` 回给客户端 —— DSH 拿它渲染「已思考」块。它**只回传**，
    不回灌上下文（下一轮的上下文仍然只有 P + C + 那句话）。
    """
    content = out.get("reply") or ""
    if out.get("reply_kind") == "empty":
        content = content or "（这一轮模型没写正文）"
    if out.get("verdict") and out["verdict"] != "accept":
        content = f"{content}\n\n（判定：{out['verdict']}｜" + "；".join(
            (out.get("verdict_reasons") or [])[:3]
        ) + "）"
    message: dict[str, Any] = {"role": "assistant", "content": content}
    thinking = (out.get("thinking") or "").strip()
    if thinking:
        message["reasoning_content"] = thinking
    return message


def _completion(sid: str, model: str, out: dict[str, Any], *, created: float) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-pciso-{sid}-{out.get('round_index', 0)}",
        "object": "chat.completion",
        "created": int(created),
        "model": model,
        "choices": [
            {"index": 0, "message": _message_of(out), "finish_reason": "stop"}
        ],
        "usage": _usage_of(out),
        "x_pciso": {
            "session_id": sid,
            "round_index": out.get("round_index"),
            "verdict": out.get("verdict"),
            "files_changed": out.get("files_changed") or [],
            "c_tokens": out.get("c_tokens"),
            "thinking_chars": (out.get("hidden") or {}).get("thinking_chars"),
        },
    }


def _noop_response(req: ChatCompletionRequest, model: str, note: str) -> Any:
    """没什么可跑的（最后一条 user 消息剥完是空的）→ 200 空回复，**不建会话、不跑轮**。

    为什么不回 400：客户端（DSH）会单独发一条只装技能目录的 user 消息，那不是错误，
    只是没有可执行的任务；回错误会在界面上弹一条红字，回空回复什么都不显示。
    """
    headers = {"X-PCISO-Skipped": "1"}
    if req.stream:

        def sse() -> Any:
            def envelope(delta: dict[str, Any], finish: str | None = None) -> str:
                return "data: " + json.dumps(
                    {
                        "id": "chatcmpl-pciso-skip",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    },
                    ensure_ascii=False,
                ) + "\n\n"

            yield envelope({"role": "assistant", "content": ""})
            yield envelope({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    body = {
        "id": "chatcmpl-pciso-skip",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "x_pciso": {"skipped": note},
    }
    return JSONResponse(content=body, headers=headers)


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #


@router.get("/v1/models")
def list_models() -> dict[str, Any]:
    """`pciso` 是默认（一般助手前缀）；每个 `prefixes/<name>/` 派生一个 `pciso@<name>`。"""
    ids = ["pciso"]
    pdir = ROOT / "prefixes"
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and (p / "PROJECT.md").is_file():
                ids.append(f"pciso@{p.name}")
    now = int(time.time())
    return {
        "object": "list",
        "data": [{"id": mid, "object": "model", "created": now, "owned_by": "pciso"} for mid in ids],
    }


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request) -> Any:
    # 两种模式：
    #   proxy（默认）= 转发 + 上下文替换，工具循环由客户端自己做（DSH 已经实现了）；
    #   round（PCISO_OPENAI_MODE=round）= 老路子，每个请求在服务端跑完整一轮。
    from . import proxyapi

    if proxyapi.mode() != "round":
        return await proxyapi.chat_completions(request)
    cfg_path = _config_path()
    model_name, prefix_name = _split_model(req.model)
    # 没点名 prefix 就用默认那份。**不能传空字符串** —— 空路径会被解析成仓库根，
    # 于是 P 变成"整个仓库的哈希 + 空正文"，既贵又毫无意义。
    if not prefix_name:
        prefix_name = Path(DEFAULT_PREFIX).name
    if not req.messages:
        return _error(400, "messages 为空：至少给一条消息")
    # 本轮任务 = **最后一条** user 消息（剥掉包装层之后）。
    # 刻意不往回找更早的那条：DSH 会单独发一条只装技能目录的 user 消息，
    # 若此时回退到上一句真话，就会把上一轮的任务**再干一遍** —— 白烧一轮钱、重复落盘。
    last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    message = _strip_reminders(_text_of(last_user.content)) if last_user is not None else ""
    if not message:
        return _noop_response(
            req,
            model_name,
            "最后一条 user 消息剥掉 <system-reminder> 之类包装层之后是空的：这一轮不跑",
        )

    key = _session_key(req, request)
    force_new = (request.headers.get("x-pciso-new") or "").strip() in ("1", "true", "yes")
    project = prefix_name or "general"
    try:
        session, created = _resolve_session(
            key=key, prefix_name=prefix_name, cfg_path=cfg_path, force_new=force_new,
            project=project,
        )
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail))

    sid = session.store.session_id
    api = _api()
    lock = api._lock_for(sid)
    if not lock.acquire(blocking=False):
        return _error(
            409, f"会话 {sid} 已有一轮在跑（同一会话必须串行，否则会重复算同一轮）", "conflict"
        )
    headers = {
        "X-PCISO-Session": sid,
        "X-PCISO-Session-Created": "1" if created else "0",
        "X-PCISO-Prefix": prefix_name or "general",
    }
    started = time.time()
    print(
        f"[v1] start sid={sid} model={model_name} new={created} "
        f"msg={message[:40]!r} key={key}",
        flush=True,
    )

    def run() -> dict[str, Any]:
        try:
            out = session.say(message)
            return out.model_dump(mode="json")
        finally:
            lock.release()

    if not req.stream:
        try:
            out = run()
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail))
        except Exception as exc:  # 缺凭据 / 上游报错：原文给出去，别吞成 500
            print(f"[v1] fail  sid={sid} {time.time() - started:.1f}s {exc}", flush=True)
            return _error(503, f"{type(exc).__name__}: {exc}", "upstream_error")
        print(
            f"[v1] done  sid={sid} round={out.get('round_index')} verdict={out.get('verdict')} "
            f"files={len(out.get('files_changed') or [])} {time.time() - started:.1f}s",
            flush=True,
        )
        body = _completion(sid, model_name, out, created=started)
        return JSONResponse(content=body, headers=headers)

    # -- 流式 ------------------------------------------------------------- #
    # 一轮 1~2 分钟，中途一个字都没有的话客户端会以为连接死了 ——
    # 所以边等边发 SSE 注释行（keepalive），结束时才把正文一次性给出去。
    box: queue.Queue = queue.Queue()
    done = threading.Event()

    def worker() -> None:
        try:
            box.put(("ok", run()))
        except Exception as exc:
            box.put(("err", f"{type(exc).__name__}: {exc}"))
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    chunk_id = f"chatcmpl-pciso-{sid}-{session.store.round_index()}"

    def sse() -> Any:
        def envelope(delta: dict[str, Any], finish: str | None = None) -> str:
            return "data: " + json.dumps(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(started),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                },
                ensure_ascii=False,
            ) + "\n\n"

        yield envelope({"role": "assistant", "content": ""})
        item: tuple[str, Any] | None = None
        while item is None:
            try:
                item = box.get(timeout=KEEPALIVE_S)
            except queue.Empty:
                if done.is_set():
                    break
                yield ": keepalive\n\n"
        if item is None:
            yield envelope({}, "stop")
            yield "data: [DONE]\n\n"
            return
        kind, value = item
        if kind == "err":
            yield "data: " + json.dumps({"error": {"message": str(value)}}) + "\n\n"
            yield "data: [DONE]\n\n"
            return
        body = _completion(sid, model_name, value, created=started)
        msg = body["choices"][0]["message"]
        if msg.get("reasoning_content"):
            # 思考先发（DSH 的「已思考」块），正文后发；两者都在这一轮的 delta 里。
            yield envelope({"role": "assistant", "reasoning_content": msg["reasoning_content"]})
        yield envelope({"content": msg["content"]})
        yield envelope({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
