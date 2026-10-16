"""HTTP 接口 —— 设计的照妖镜。

端点定义一旦钉死，P / C / O_i / ΔC 的语义就没有含糊余地。

    POST   /session/create          { prefix, project, config } -> { session_id }
    POST   /session/{id}/round      { o_i? , round_index?, stage? } -> { C_i, next_prompt, judge, validation }
    GET    /session/{id}/state      -> { C_current, fact_chain, failure_chain, dependency_graph, constraint_status }
    GET    /session/{id}/artifacts  -> { files, tests, docs }
    GET    /session/{id}/metrics    -> 曲线数据（P0/P1/P2 三个场景各自的原始量）
    GET    /session/{id}/log        -> 轨迹日志（rounds.jsonl + events.jsonl）
    POST   /session/{id}/revert     { to_index }
    DELETE /session/{id}
    GET    /arms                    实验设置表（配置即实验）

OpenAI 兼容那条路（任何 OpenAI 客户端 / DSH 都能把 pciso 当模型用）在 `openaiapi.py`：

    GET    /v1/models
    POST   /v1/chat/completions     一个请求 = 一轮；历史不回流，由 C 代表

`POST /round` 的 `o_i` 是**可选**的 —— 这是消融路径与产品路径的统一 seam：
  缺省 -> server 自己调工作模型（产品形态）
  传入 -> 直接用（回放、注入、离线复现）
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .chatui import PAGE as CHAT_PAGE
from .config import SessionConfig
from .models import (
    ArtifactIndex,
    AskRequest,
    AskResponse,
    RoundRequest,
    RoundResponse,
    SayRequest,
    SayResponse,
    SessionCreateRequest,
    SessionCreateResponse,
)
from .openaiapi import router as openai_router
from .session import Session, SessionConflict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"
DEFAULT_STATE_DIR = os.environ.get("PCISO_STATE_DIR", str(ROOT / "state"))
# 交付仓库：脚本 oracle 绝不许写它。CLI 与接口两条路径都必须挡住。
DELIVERABLE = (ROOT / ".." / "drawing-video-toolchain").resolve()

app = FastAPI(
    title="pciso — P/C 隔离长程编码实验 server",
    version="0.1.0",
    description="冻结前缀 P + 结构化循环状态 C + 客观验证器；跑实验 = 切配置。",
)
# OpenAI 兼容入口（/v1/models、/v1/chat/completions）—— 见 openaiapi.py。
app.include_router(openai_router)

_SESSIONS: dict[str, Session] = {}
# 按会话串行化。没有它，两次 `/say` 并发进同一会话会各自从同一个 round_index 起算，
# 于是同一轮被跑两遍、rounds.jsonl 出现重复或互相覆盖 —— 而这类损坏是静默的。
# uvicorn 的同步 handler 跑在线程池里，所以"浏览器双击发送"真的会并发。
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.Lock())


def _state_dir() -> str:
    return DEFAULT_STATE_DIR


def _resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else (ROOT / p).resolve())


def _load(session_id: str) -> Session:
    if session_id in _SESSIONS:
        return _SESSIONS[session_id]
    try:
        session = Session.load(session_id, _state_dir(), base_dir=ROOT)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _SESSIONS[session_id] = session
    return session


# --------------------------------------------------------------------------- #
# 前端（极简模式对话）
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def chat_page() -> HTMLResponse:
    """极简模式的对话页。单文件、无构建步骤、由 server 直接吐出来。"""
    return HTMLResponse(CHAT_PAGE)


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #


@app.get("/health")
def health() -> dict[str, Any]:
    """健康检查 + **能不能真跑**的自述。

    前端靠 `work_model_ready` 决定默认勾不勾"离线排练"：
    没有凭据时默认离线（罐头回复），有凭据时默认真实模型。
    """
    api_key_env = os.environ.get("PCISO_API_KEY_ENV", "DEEPSEEK_API_KEY")
    return {
        "ok": True,
        "root": str(ROOT),
        "state_dir": _state_dir(),
        "api_key_env": api_key_env,
        "work_model_ready": bool(os.environ.get(api_key_env)),
        "default_work_model": "deepseek-flash",
        "deliverable": str(DELIVERABLE),
    }


@app.get("/arms")
def list_arms() -> dict[str, Any]:
    """实验设置表：目录里每个 yaml 就是一行。"""
    rows: list[dict[str, Any]] = []
    if CONFIG_DIR.is_dir():
        for p in sorted(CONFIG_DIR.glob("*.yaml")):
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            try:
                cfg = SessionConfig.from_mapping(dict(raw), base_dir=ROOT)
                row = cfg.experiment_row()
            except Exception as exc:
                row = {"error": f"{type(exc).__name__}: {exc}"}
            row["config_path"] = p.relative_to(ROOT).as_posix()
            rows.append(row)
    return {"arms": rows}


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


@app.post("/session/create", response_model=SessionCreateResponse)
def create_session(req: SessionCreateRequest) -> SessionCreateResponse:
    # 两个相对路径都必须以**仓库根**为基准。config_path 原先没走 _resolve，
    # 于是它跟着 server 的 cwd 走：从 src/ 起 server（README 里的起法之一）时，
    # 页面发的 `configs/chat.yaml` 直接 400「配置无效: No such file or directory」，
    # 而界面上只表现为"开新对话失败" —— 看起来像前端坏了，其实是路径基准错。
    req = req.model_copy(
        update={
            "prefix_path": _resolve(req.prefix_path),
            "config_path": _resolve(req.config_path) if req.config_path else None,
        }
    )

    # 脚本 oracle **绝不许写交付仓库** —— 这条闸在 CLI 里有，接口这条路径原先没有。
    # 前端用配置里的 workspace_path（指向交付仓库）+ 离线假 oracle 时，
    # 会把罐头输出写进交付物、还会在那儿建出 arm-* 分支。必须先挡掉再建会话。
    try:
        probe = Session.preview_config(req, base_dir=ROOT)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"配置无效: {exc}") from exc

    if probe.work_model.endpoint in ("fake", "oracle"):
        explicit = "workspace_path" in (req.overrides or {})
        ws = Path(probe.workspace_path).resolve() if probe.workspace_path else None
        if ws is not None and (ws == DELIVERABLE or not explicit):
            why = "指向交付仓库" if ws == DELIVERABLE else "未显式指定"
            print(f"[guard] 脚本 oracle + {why} -> 清空 workspace_path（{ws}）")
            probe.workspace_path = None
            req = req.model_copy(
                update={
                    "config": probe.model_dump(mode="json"),
                    "config_path": None,
                    "overrides": {},
                }
            )

    try:
        session = Session.create(req, base_dir=ROOT, state_dir=_state_dir())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _SESSIONS[session.store.session_id] = session
    meta = session.store.read_meta()
    return SessionCreateResponse(
        session_id=session.store.session_id,
        project=req.project,
        arm=meta.get("arm"),
        prefix_hash=meta["prefix_hash"],
        max_rounds=session.cfg.max_rounds,
        stages=meta.get("stages", []),
        history_mode=session.cfg.resolved_history_mode(),
        workspace=str(session.store.workspace),
        workspace_is_deliverable=(
            session.store.workspace.resolve() == DELIVERABLE
            if session.cfg.workspace_path
            else False
        ),
    )


@app.post("/session/{session_id}/round", response_model=RoundResponse)
def run_round(session_id: str, req: RoundRequest) -> RoundResponse:
    session = _load(session_id)
    try:
        return session.round(req)
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 缺凭据之类的运行期问题：给 503 + 原文，而不是 500 堆栈
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/session/{session_id}/say", response_model=SayResponse)
def say(session_id: str, req: SayRequest) -> SayResponse:
    """极简模式的一轮对话：跑一轮，但只回"可见的那部分"。

    思考、检索往返、文件内容、ΔC 原文都只给**体量**不给内容 —— 它们正是被压缩掉的对象。
    """
    session = _load(session_id)
    lock = _lock_for(session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="这个会话已有一轮在跑（同一会话必须串行，否则会重复算同一轮）"
        )
    started = time.time()
    # **请求开始就打日志**。uvicorn 只在响应完成后才打访问日志，而真机一轮要 1~2 分钟 ——
    # 于是"请求到底有没有到达"在日志里完全看不出来（实测就为这个查了一轮）。
    print(
        f"[round] start sid={session_id} arm={session.cfg.arm} "
        f"round={session.store.round_index() + 1} task={req.message[:40]!r}",
        flush=True,
    )
    try:
        out = session.say(req.message, stage=req.stage, include_hidden=req.include_hidden)
        print(
            f"[round] done  sid={session_id} round={out.round_index + 1} "
            f"verdict={out.verdict} files={len(out.files_changed)} "
            f"C={out.c_tokens}tok {time.time() - started:.1f}s",
            flush=True,
        )
        return out
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        print(f"[round] fail  sid={session_id} {time.time() - started:.1f}s {exc}", flush=True)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        lock.release()


@app.post("/session/{session_id}/ask", response_model=AskResponse)
def ask(session_id: str, req: AskRequest) -> AskResponse:
    """**只对话**：给当前 C，拿一段回答。不推进、不写文件、不提交。

    与 `/say` 的区别是本质的：`/say` 跑一整轮实验（写文件、跑验证、逐轮提交、
    C 前进，真机 1~2 分钟）；`/ask` 只把当前 C 给模型看一眼，秒级返回，
    **不动任何状态**。两者分开是因为把"发一句话"实现成"跑一整轮"是个设计错误 ——
    说一句"你好"会被那轮提示词要求推进一整个阶段。
    """
    session = _load(session_id)
    return session.ask(req.message, max_tokens=req.max_tokens, thinking=req.thinking)


@app.get("/session/{session_id}/turns")
def list_turns(session_id: str) -> dict[str, Any]:
    """已有轮次的聊天视图（页面刷新后恢复用）。"""
    return {"session_id": session_id, "turns": _load(session_id).turns()}


@app.get("/session/{session_id}/chat")
def get_chat(session_id: str) -> dict[str, Any]:
    """对话窗口的记录（append-only）：刷新页面靠它把说过的话原样摆回来。"""
    return {"session_id": session_id, "turns": _load(session_id).chat_log()}


@app.get("/sessions")
def list_sessions(limit: int = 20) -> dict[str, Any]:
    """最近开过的会话 —— 给对话窗口挑"接着哪次实验聊"用，不是实验面板。

    按"有没有轮次"再按创建时间倒排：对话窗口默认接最近那个**有 C** 的会话，
    这样它一开口就带着事实链，而不是对着一份空状态聊天。
    """
    base = Path(_state_dir()) / "sessions"
    rows: list[dict[str, Any]] = []
    if base.is_dir():
        for p in base.iterdir():
            meta_file = p / "session.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rounds = 0
            rounds_file = p / "rounds.jsonl"
            if rounds_file.is_file():
                rounds = sum(
                    1 for line in rounds_file.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            rows.append(
                {
                    "session_id": meta.get("session_id") or p.name,
                    "project": meta.get("project"),
                    "arm": meta.get("arm"),
                    "rounds": rounds,
                    "created_at": meta.get("created_at") or 0,
                }
            )
    rows.sort(key=lambda r: (r["rounds"] > 0, r["created_at"]), reverse=True)
    return {"sessions": rows[:limit]}


@app.get("/session/{session_id}/audit/{round_index}")
def get_audit(session_id: str, round_index: int) -> dict[str, Any]:
    """单轮审计视图：**给人看**的全文，不回流进极简模式。"""
    try:
        return _load(session_id).audit(round_index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# 眼睛通道：让"画面"能被看见
# --------------------------------------------------------------------------- #


@app.post("/session/{session_id}/preview")
def post_preview(
    session_id: str, sentence: str = Body("", embed=True)
) -> dict[str, Any]:
    """跑一遍工作区里的成片流水线，把产物收出来。

    工作模型是单发无工具的，看不到自己画的帧 —— 而视频开发最需要看画面。
    这条通道给**人和视觉模型**看，不回流进极简模式。
    """
    return _load(session_id).render_preview(sentence)


@app.get("/session/{session_id}/blob/{rel_path:path}")
def get_blob(session_id: str, rel_path: str) -> FileResponse:
    """把工作区或预览目录里的文件发出去（播放页要从这里取帧与音频）。

    越界一律拒绝。除了 `..` 穿越，还**必须显式挡掉 `.git`**：
    实测里 `.git/config` 是能取到的（它就在工作区内，resolve 之后完全"合法"），
    而那个文件里可能有带 token 的 remote URL —— 这是会真丢凭据的口子。
    """
    session = _load(session_id)
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if any(part == ".git" or part.startswith(".git") for part in parts):
        raise HTTPException(status_code=404, detail=f"拒绝访问版本库内部: {rel_path}")
    for root in (session.store.root.resolve(), session.store.workspace.resolve()):
        target = (root / rel_path).resolve()
        if root in target.parents and target.is_file():
            return FileResponse(target)
    raise HTTPException(status_code=404, detail=f"不存在或越界: {rel_path}")


@app.get("/session/{session_id}/state")
def get_state(session_id: str) -> dict[str, Any]:
    return _load(session_id).state_response().model_dump(mode="json")


@app.get("/session/{session_id}/artifacts", response_model=ArtifactIndex)
def get_artifacts(session_id: str) -> ArtifactIndex:
    return _load(session_id).artifacts()


@app.get("/session/{session_id}/metrics")
def get_metrics(session_id: str) -> dict[str, Any]:
    return _load(session_id).metrics()


@app.get("/session/{session_id}/log")
def get_log(session_id: str, include_prompts: bool = False) -> JSONResponse:
    session = _load(session_id)
    rounds = session.store.read_rounds()
    if not include_prompts:
        rounds = [{k: v for k, v in r.items() if k not in ("prompt", "o_i")} for r in rounds]
    return JSONResponse(
        {
            "session_id": session_id,
            "rounds": rounds,
            "events": list(session.store.iter_events()),
        }
    )


@app.post("/session/{session_id}/revert")
def post_revert(session_id: str, to_index: int = Body(..., embed=True)) -> dict[str, Any]:
    return _load(session_id).revert(to_index)


@app.delete("/session/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    session = _load(session_id)
    sid = session.store.session_id
    session.delete()
    _SESSIONS.pop(sid, None)
    return {"deleted": sid}
