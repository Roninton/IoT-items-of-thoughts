"""工作模型后端。

两类：
- `FakeWorkModel`   离线 scripted oracle，逐轮吐出 `fakeimpl/stages/<NN>/` 里的文件。
                    它的价值不只是"不用花钱就能测生命周期"：它是一个**分数上限参照**——
                    真实对照组的成绩如果低于它，说明验证器在工作，而不是指标全绿没信号。
                    它还支持定点故障注入（todo / cycle / break_signature / weaken_tests /
                    touch_frozen），用来证明"硬约束回归 -> 回退"这条路真的通。
- `OpenAICompatWorkModel`  OpenAI 兼容 /chat/completions（DeepSeek 等）。

单发、无工具。O_i 就是这一整段文本，server 负责解析与落盘。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import SessionConfig, WorkModelConfig
from .render import READ_RESULT_HEADER

STAGES_ROOT = Path(__file__).resolve().parent / "fakeimpl"


def resolve_stages_dir(prefix: Any, override: str = "") -> Path:
    """按 `fakeimpl/<名字>/stages/` 找项目的 oracle；名字默认取 prefix 目录名。

    多个实验载体共用同一套平台，所以 oracle 必须按项目分目录。
    `override` 用于 prefix 被复制/改名（例如测试）之后仍能指回正确的 oracle。
    找不到就**立刻报错**并列出有哪些 —— 静默返回一个空目录会让整轮产出为空，
    而那种失败看起来像"模型没干活"，极难排查。
    """
    root = getattr(prefix, "root", None)
    default_name = Path(root).name if root is not None else ""
    name = override or default_name
    if name:
        cand = STAGES_ROOT / name / "stages"
        if cand.is_dir():
            return cand
    available = (
        sorted(p.name for p in STAGES_ROOT.iterdir() if p.is_dir())
        if STAGES_ROOT.is_dir()
        else []
    )
    raise FileNotFoundError(
        f"找不到 oracle {name!r}（期望 {STAGES_ROOT / name / 'stages'} 存在）。"
        f"prefix 目录名是 {default_name!r}；现有 oracle: {available or '（无）'}。"
        f"若 prefix 被复制/改名过，请在配置里显式设置 session.fake_oracle。"
    )


# --------------------------------------------------------------------------- #
# 模型客户端
# --------------------------------------------------------------------------- #


@dataclass
class ChatResult:
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # 思考过程被丢弃了多少字。这是**被压缩掉的东西的体量** ——
    # 把它记下来，界面上才能显示"隐藏了 N 字思考"而不暴露内容。
    reasoning_chars: int = 0
    # 思考被当成正文回退的情况（content 为空）。必须显式标出来：
    # 那是唯一会让"隐藏的思考"漏进可见回复的路径。
    thinking_fallback: bool = False
    # 思考**原文**。以前只记 `reasoning_chars`（体量），原文直接扔掉 —— 于是
    # "为什么这么改"这唯一的住处没了，压缩器只能从正文里猜因果（plan-as-fact 的来源）。
    # README §1.3 把压缩范围改成"只压 thinking"，它必须真的被传下去。
    thinking: str = ""


class ModelClient:
    """OpenAI 兼容 chat completions 客户端。凭据只从环境变量读，永不入库。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str = "DEEPSEEK_API_KEY",
        timeout_s: float = 900.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"环境变量 {self.api_key_env} 未设置 —— 凭据不得写进仓库，请先导出。"
            )
        return key

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.2,
        thinking: bool = True,
        response_format: str | None = None,
    ) -> ChatResult:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        # 思考开关按各家约定传递；不认识该字段的服务端会忽略它。
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        payload["reasoning_effort"] = "high" if thinking else "none"

        # 结构化输出。实测本 endpoint **不支持** `json_schema`（所有变体一律 400），
        # 只支持 `json_object`（保证"是合法 JSON"，不保证 schema）。
        # 所以它只能消掉"输出根本不是 JSON"这一类失败；
        # 字段格式（例如事实节点 id 必须是 N<数字>）仍然只能靠
        # 提示词 + 服务端校验 + 拒收率度量三层来管。
        if response_format:
            payload["response_format"] = {"type": response_format}

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return ChatResult(error=f"TRANSPORT: {exc}")

        if resp.status_code >= 400:
            return ChatResult(error=f"HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
        except ValueError as exc:
            return ChatResult(error=f"非 JSON 响应: {exc}")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        fallback = False
        if not text and reasoning:
            text = reasoning  # 思考吃光预算的兜底，仍然留下痕迹
            fallback = True

        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return ChatResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(
                usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
            ),
            reasoning_chars=len(reasoning),
            thinking_fallback=fallback,
            thinking=reasoning,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# 工作模型
# --------------------------------------------------------------------------- #


@dataclass
class WorkOutput:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    note: str = ""
    # 被丢弃的思考体量 + 是否发生了"思考当正文"的回退（见 ChatResult 的说明）
    reasoning_chars: int = 0
    thinking_fallback: bool = False
    # 空回复重试了几次（用户裁定：正文为空就重试，不算这一轮失败）
    retries: int = 0
    # 这一轮实际发出去的 system 消息长度。
    #
    # 实验前提是"工作模型只看到 P + C + 指令 + 任务 + 格式"，**多一条 system 就是多一个
    # 隐藏输入**。实测抓过 payload：确实是 1 条 user、无 system、无历史。
    # 但"我记得没传"不是证据 —— 所以把这个数字记进每轮轨迹：
    # 它恒为 0 就是"上下文是纯净的"的可核对凭证。源码里保留 `system=` 这个 seam
    # 是为了将来做对照实验，一旦真用了，轨迹里立刻看得出来。
    system_chars: int = 0
    # thinking **原文**（README §1.3）：压缩范围改成"只压 thinking"之后，它是压缩器唯一的输入。
    # 默认空 —— 离线假模型没有思考，也不该假装有。
    thinking: str = ""


class WorkModel(Protocol):
    name: str

    def run(self, prompt: str, *, stage: int, round_index: int) -> WorkOutput: ...


_FAULTS = (
    "none",
    "todo",
    "cycle",
    "drop_docstring",
    "break_signature",
    "weaken_tests",
    "touch_frozen",
    "overclaim",
)

_DOCSTRING_RE = re.compile(r'^([ \t]*)((?:""".*?""")|(?:\'\'\'.*?\'\'\'))\n', re.DOTALL | re.MULTILINE)


class FakeWorkModel:
    """逐轮吐出预置文件；可注入定点故障以验证回退路径。"""

    name = "fake"

    def __init__(
        self,
        *,
        fault: str = "none",
        fault_after: int = 1,
        prefix_titles: dict[int, str] | None = None,
        prefix_contracts: list[Any] | None = None,
        retrieval: bool = False,
        retrieval_mode: str = "good",
        stages_dir: Path | None = None,
    ):
        if fault not in _FAULTS:
            raise ValueError(f"未知故障注入 {fault!r}，可选 {_FAULTS}")
        self.stages_dir = Path(stages_dir) if stages_dir else STAGES_ROOT / "drawing_video" / "stages"
        self.fault = fault
        self.fault_after = fault_after
        self.prefix_titles = prefix_titles or {}
        self.prefix_contracts = prefix_contracts or []
        self.retrieval = retrieval
        self.retrieval_mode = retrieval_mode

    # -- 对外 ------------------------------------------------------------- #

    def run(self, prompt: str, *, stage: int, round_index: int) -> WorkOutput:
        stage_dir = self.stages_dir / f"{stage:02d}"
        if not stage_dir.is_dir():
            return WorkOutput(
                text=f"阶段 {stage} 无预置产物（fakeimpl/stages/{stage:02d} 不存在）。",
                note="fake: 该阶段为空",
            )

        emit: dict[str, str] = {}
        for p in sorted(stage_dir.rglob("*")):
            if p.is_file():
                emit[p.relative_to(stage_dir).as_posix()] = p.read_text(encoding="utf-8")

        # G 组形态：第一趟只提检索申请；拿到内容后第二趟才产出。
        if self.retrieval and READ_RESULT_HEADER not in prompt:
            return WorkOutput(
                text=self._read_requests(emit),
                note=f"fake: 检索申请({self.retrieval_mode})",
            )

        active = self.fault != "none" and round_index >= self.fault_after
        note = "fake"
        if active:
            emit, note = self._inject(emit, stage)

        blocks: list[str] = []
        for rel, body in emit.items():
            # 声明式检查点：假模型也照契约写，这样 derived/declared 两路都被走到
            blocks.append(f"@@UNIT file {rel}")
            blocks.append(f"```file:{rel}\n{body}```")
        blocks.append("@@UNIT validate")

        narrative = self._narrative(stage, active, note)
        if active and self.fault == "overclaim":
            # 声称改完一个根本没写的文件 —— 用来验证"无据声明"计数
            narrative += "\n\n@@UNIT file drawvid/never_written.py"
        return WorkOutput(text="\n\n".join([narrative, *blocks]), note=note)

    def _read_requests(self, emit: dict[str, str]) -> str:
        """模拟 agent 先看盘再动手。

        good   —— 申请本阶段要改/要参考的文件，外加 core 模块（下层依赖）
        sloppy —— 额外申请一个不存在的路径（无效检索）+ 重复申请同一文件（冗余检索），
                  用来证明这两类失败真的被 server 记账，而不是"看起来在工作"。
        """
        wants = [rel for rel in sorted(emit) if rel.endswith(".py")][:2]
        core = _first_py(self.stages_dir / "01")
        if core is not None:
            rel = core.relative_to(self.stages_dir / "01").as_posix()
            if rel not in wants:  # 别在"正常"模式里制造重复 —— 那会污染冗余率
                wants.append(rel)
        lines = ["我先看一下工作区里相关的文件再动手。", ""]
        lines += [f"@@READ {rel}" for rel in wants]
        if self.retrieval_mode == "sloppy":
            lines.append("@@READ drawvid/does_not_exist.py")
            if wants:
                lines.append(f"@@READ {wants[0]}")  # 冗余：同一轮里要第二遍
        return "\n".join(lines)

    # -- 叙述（失败链的来源） --------------------------------------------- #

    def _narrative(self, stage: int, active: bool, note: str) -> str:
        title = self.prefix_titles.get(stage, f"阶段 {stage}")
        head = f"## 第 {stage} 轮：{title}\n\n本轮按 P 的阶段分解推进，产出文件见下方 file 块。"
        lines = [head]
        if stage == 4:
            lines.append(
                "设计取舍：曾考虑用 ffmpeg 子进程封装编码器，"
                "因引入外部进程依赖、编码器无法单测而否决。"
            )
        if stage == 6:
            lines.append(
                "设计取舍：曾考虑把绘图与视频合并在一个模块，"
                "因职责耦合、破坏分层而否决。"
            )
        lines.append("未解决：\n- 保持与冻结契约一致\n- 补齐下一阶段模块")
        if active:
            lines.append(f"（注意：本轮注入了故障 {note}）")
        return "\n\n".join(lines)

    # -- 故障注入 --------------------------------------------------------- #

    def _inject(self, emit: dict[str, str], stage: int) -> tuple[dict[str, str], str]:
        out = dict(emit)
        if self.fault == "todo":
            for rel in list(out):
                if rel.endswith(".py"):
                    out[rel] = out[rel] + "\n# TODO: 补充异常处理\n"
            return out, "todo"

        if self.fault == "drop_docstring":
            for rel in list(out):
                if rel.endswith(".py"):
                    out[rel] = _DOCSTRING_RE.sub("", out[rel])
            return out, "drop_docstring"

        if self.fault == "break_signature":
            # 项目无关：把**第一个模块级函数**的第一个位置参数改名 -> 与契约产生签名漂移
            for rel in list(out):
                if rel.endswith(".py"):
                    out[rel] = _FIRST_DEF_ARG_RE.sub(
                        lambda m: f"{m.group(1)}zz_{m.group(2)}", out[rel], count=1
                    )
            return out, "break_signature"

        if self.fault == "cycle":
            # 让本轮产物里的第一个模块反向依赖一个**更晚阶段**的模块 —— 同时制造
            # 循环依赖与分层违规。项目无关：按各阶段目录里的第一个 .py 取文件名与符号名。
            #
            # 只注入到"本来就属于本轮产物"的文件里。早先的写法用 stages_dir 直接相对化，
            # 结果把文件写到了 `03/drawvid/drawing.py` 这种不存在的路径上，
            # 故障看起来"注入成功"实则毫无效果 —— 这种静默失效最难查。
            src_rel = next((rel for rel in sorted(out) if rel.endswith(".py")), "")
            later: Path | None = None
            if src_rel:
                src_stem = Path(src_rel).stem
                for probe in range(stage + 1, 13):
                    cand = _first_py(self.stages_dir / f"{probe:02d}")
                    if cand is not None and cand.stem != src_stem:
                        later = cand
                        break
            symbol = _first_public_symbol(later) if later is not None else ""
            if src_rel and later is not None and symbol:
                injected = f"from .{later.stem} import {symbol}  # 反向依赖"
                out[src_rel] = out[src_rel].rstrip("\n") + "\n\n\n" + injected + "\n"
            return out, "cycle" if symbol else "cycle(no-op)"

        if self.fault == "weaken_tests":
            out["tests/test_drawvid.py"] = (
                '"""被弱化的自测：只剩一个用例。"""\n\n\n'
                "def test_nothing():\n"
                '    """占位。"""\n'
                "    assert True\n"
            )
            return out, "weaken_tests"

        if self.fault == "touch_frozen":
            out["tests/frozen/test_contract.py"] = (
                '"""被模型篡改的冻结测试。"""\n\n\n'
                "def test_tampered():\n"
                '    """什么都不测。"""\n'
                "    assert True\n"
            )
            return out, "touch_frozen"

        if self.fault == "overclaim":
            return out, "overclaim"

        return out, "none"


class OpenAICompatWorkModel:
    """真实工作模型：单发，thinking 由配置决定。"""

    name = "openai"

    def __init__(self, cfg: WorkModelConfig, system: str | None = None):
        self.cfg = cfg
        base_url = cfg.base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self.client = ModelClient(
            base_url=base_url, model=cfg.model, api_key_env=cfg.api_key_env
        )
        self.system = system

    def run(self, prompt: str, *, stage: int, round_index: int) -> WorkOutput:
        """单发一轮；**正文为空时重试**（用户裁定）。

        正文为空几乎总是"思考吃光了输出预算"（`finish_reason=length`）。
        实测：`max_tokens=8000` 时 31996 字符思考、正文 0 字、0 个文件；
        给到 32000 就正常了 —— 而平台自己的 DSH 配置对同一个适配器用的也是
        `maxTokens: 32768`，注释写着"够用（还含 thinking）"。所以
        **正常情况不会发生，根源是窗口给太小**。

        真出现了就重试，而不是判这一轮失败：重试时**把输出预算翻倍**
        （正是缺的那个东西），并把重试次数与额外开销全部记账 ——
        重试是真实花费，不能吞掉。
        """
        attempt = 0
        budget = self.cfg.max_tokens
        total = {
            "work_prompt_tokens": 0,
            "work_completion_tokens": 0,
            "work_cached_tokens": 0,
        }
        thinking_chars = 0
        thinking_text = ""
        while True:
            res = self.client.chat(
                prompt,
                system=self.system,
                max_tokens=budget,
                temperature=self.cfg.temperature,
                thinking=self.cfg.thinking,
            )
            # **重试的每一次都要累加**：只记最后一次等于把重试的钱吞掉，
            # 而"重试是真实花费"正是这条机制必须自报的东西。
            total["work_prompt_tokens"] += res.prompt_tokens
            total["work_completion_tokens"] += res.completion_tokens
            total["work_cached_tokens"] += res.cached_tokens
            thinking_chars += res.reasoning_chars
            # 只留**最后一次尝试**的思考原文：空回复重试时，第一次那段的结论已经被推翻
            # （它连正文都没写出来），把两段拼一起会让推理节点里同时躺着两个互相矛盾的方案。
            thinking_text = res.thinking or thinking_text
            usage = dict(total)
            if res.error:
                return WorkOutput(
                    text="",
                    usage=usage,
                    note=f"ERROR {res.error}",
                    reasoning_chars=thinking_chars,
                    retries=attempt,
                    system_chars=len(self.system or ""),
                )
            out = WorkOutput(
                text=res.text,
                usage=usage,
                note="ok",
                reasoning_chars=thinking_chars,
                thinking_fallback=res.thinking_fallback,
                retries=attempt,
                system_chars=len(self.system or ""),
                thinking=thinking_text,
            )
            if attempt >= max(0, self.cfg.retry_on_empty) or not self._is_empty(res):
                if attempt:
                    out.note = f"ok (空回复后重试 {attempt} 次，最终预算 {budget})"
                return out
            attempt += 1
            budget = min(budget * 2, max(budget, self.cfg.retry_max_tokens_cap))

    @staticmethod
    def _is_empty(res: ChatResult) -> bool:
        """正文为空 = 没写正文。带 thinking 兜底回退**不算**有正文。"""
        return not (res.text or "").strip() or res.thinking_fallback


def build_work_model(cfg: SessionConfig, prefix: Any = None) -> WorkModel:
    if cfg.work_model.endpoint in ("fake", "oracle"):
        titles = {s.index: s.title for s in prefix.stages} if prefix else {}
        return FakeWorkModel(
            fault=cfg.work_model.fake_fault,
            fault_after=cfg.work_model.fake_fault_after,
            prefix_titles=titles,
            retrieval=cfg.work_model.retrieval,
            retrieval_mode=cfg.work_model.fake_retrieval_mode,
            stages_dir=resolve_stages_dir(prefix, cfg.fake_oracle) if prefix is not None else None,
        )
    return OpenAICompatWorkModel(cfg.work_model)


def _first_py(stage_dir: Path) -> Path | None:
    """某阶段目录里的第一个 .py —— 故障注入用它保持项目无关。"""
    if not stage_dir.is_dir():
        return None
    for path in sorted(stage_dir.rglob("*.py")):
        return path
    return None


_PUBLIC_DEF_RE = re.compile(
    r"^(?:class|def|async def)\s+([A-Za-z][A-Za-z0-9_]*)", re.MULTILINE
)

# 第一个模块级 def 的第一个位置参数（用于制造签名漂移）
_FIRST_DEF_ARG_RE = re.compile(
    r"^(def [A-Za-z_]\w*\()([a-z_]\w*)", re.MULTILINE
)


def _first_public_symbol(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for match in _PUBLIC_DEF_RE.finditer(text):
        name = match.group(1)
        if not name.startswith("_"):
            return name
    return ""
