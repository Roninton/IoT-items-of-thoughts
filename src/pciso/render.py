"""prompt 组装 + C 状态渲染。

两种 prompt 产物都要产出，因为实验要同时记录**成本**和**内容**：

- 产品路径：`build_prompt(...)` -> 真正发给工作模型的东西
- 分析路径：`render_state_block(state)` -> C 的文本形态，用于计 token 与事后判读

P 的渲染顺序固定不变 —— 这是前缀缓存命中率的前提，也是"P 冻结"在计费上唯一有意义的形式。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from pathlib import Path

from .config import SessionConfig
from .models import CompressedState
from .prefix import PrefixBundle

# --------------------------------------------------------------------------- #
# token 估算（真实值一律以 API usage 为准；这里只用于预算控制与日志）
# --------------------------------------------------------------------------- #


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x3000 <= o <= 0x303F
        or 0xFF00 <= o <= 0xFFEF
    )


def est_tokens(text: str) -> int:
    """CJK 约 1 字/token，其余约 4 字符/token。粗估，只用于预算。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if _is_cjk(c))
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))


def est_tokens_state(state: CompressedState) -> int:
    return est_tokens(render_state_block(state))


# --------------------------------------------------------------------------- #
# C 的文本形态
# --------------------------------------------------------------------------- #


def render_state_block(state: CompressedState) -> str:
    lines: list[str] = []

    lines.append("【事实链】")
    facts = state.active_facts()
    if not facts:
        lines.append("  （空）")
    for f in facts:
        # 锚点没落地（文件不存在）必须**在正文里说出来**。
        #
        # 真机踩过：第 1 轮（"先别动文件"）里模型讲的是**计划**，压缩器把计划记成了事实，
        # 于是 C 里躺着 12 条"brushkit/export.py 每次覆盖写 current-view.jpg"这类句子，
        # 而那个文件从来没被创建过。锚点对象内部其实标着 resolved=False/文件不存在，
        # 但渲染出来的正文读起来就是"已经写完了" —— 下一轮的工作模型没有读盘通道，
        # 只能照 C 办事，于是它开始"盘点/重写"，而它根本没法知道真相。
        # 这一行标记就是把"客观已知的不一致"还给读者。
        unresolved_anchor = f.anchor is not None and f.anchor.resolved is False
        anchor = f" (锚: {f.anchor.key()})" if f.anchor else " (无锚)"
        if unresolved_anchor:
            # 这里必须报**锚点对象里记着的原因**，不能一律写"文件不存在"。
            # 真机踩过（会话 46aa73d7e5b94ffa 的 N3/N4）：README.md 明明在盘上，
            # 只是压缩器给一份 markdown 编了个符号锚点（`README.md::drawvid`），
            # 解析不到符号 ≠ 文件不存在 —— 而渲染写的是"文件不存在"。
            # 于是模型基于这句假话推出四条"物证没对上"的噪声事实（N6–N10），
            # 整条链被自己制造的矛盾占满，真正的工作反而没人做了。
            anchor += f" —— {f.anchor.detail or '锚点未解析'}"
        deps = f" (依据: {','.join(f.deps)})" if f.deps else ""
        # 依据与风险必须渲染出来 —— 只存不渲染等于没存（`why` 就是这么被静默丢掉好几轮的）。
        why = f" ｜依据：{f.why}" if f.why else ""
        risk = f" ｜风险：{f.risk}" if f.risk else ""
        # "文件不在"和"文件在、符号名对不上"是两回事，标错会直接误导下一轮：
        # 真机自述（会话 5f24337359ac4c39）：`[未落地]` 被读成"计划过但没落地"，
        # 于是它以为测试用例只是计划、没有真的写进文件。
        mark = ""
        if unresolved_anchor:
            mark = "[符号未见] " if f.anchor.symbol_missing else "[未落地] "
        # 节点类别要在**正文里**看得见（README §1.3）：读者必须能一眼分清
        # "这是 server 记下的动作事实" / "这是模型压出来的推理" / "这是被否掉的路线"。
        # 分不清的后果是实打实的：推理节点会被当成已完成的事实（plan-as-fact 那类噪声）。
        tag = ""
        if f.node_kind == "act":
            tag = "⟨ACT⟩ "
        elif f.node_kind == "think":
            tag = f"⟨THINK {f.polarity or '?'}⟩ "
        # 未定的推理必须在正文里看得见（真机自述第 2 条）：把"还没定"读成"已经定了"，
        # 下一轮就不会回来重新检查它 —— 过度确定比缺失更危险。
        if f.node_kind == "think" and f.assertion == "open":
            tag += "⟨待定⟩ "
        lines.append(f"  [{f.id}] {tag}{mark}{f.statement}{anchor}{why}{risk}{deps}")

    lines.append("【失败链】")
    if not state.failure_chain:
        lines.append("  （空）")
    for x in state.failure_chain:
        lines.append(f"  - {x.id} 试{x.attempt}：因{x.reason}否决 (轮{x.round})")

    # 【当前进度】只认**有工具回执**的进度。一条 ACT 都没有时，progress 里每一句都只是
    # 模型自述 —— 真机事故：进度行被"本轮判定必须打破僵局、直接并行发 pwsh + read_image"
    # 占了 70 轮，模型每轮读到的是"我已决定执行"，于是执行本身变得不必要（计划被执行化）。
    acts = sum(1 for f in state.fact_chain if f.node_kind == "act")
    label = "【当前进度】" if acts else "【自述进度·无工具回执核对】"
    lines.append(f"{label}{state.progress or '（空）'}")

    # 验证状态**集中成一行**（真机自述：「决定层够用，状态层略薄 —— 验证状态散在几条节点里」）。
    # 这一行是**推导出来的**，不占 C 的存储、不花模型调用、也不会过期：
    # 内容直接来自最近一轮 ACT 节点自己带的验证小句（那个小句由 server 从 pytest_suite 渲染）。
    status = _verification_line(state)
    if status:
        lines.append(status)
    elif not acts:
        lines.append("【验证状态】0 条物证 —— 链上没有任何工具回执，上面的进度与结论都没有被核对过")

    lines.append("【未解决】")
    if not state.unresolved:
        lines.append("  （空）")
    for u in state.unresolved:
        lines.append(f"  - {u}")

    lines.append("【约束状态】（验证器客观写入）")
    if not state.constraints:
        lines.append("  （空）")
    for c in state.constraints:
        tag = "硬" if c.id in _hard_ids() else "软"
        ev = f" — {c.evidence}" if c.evidence else ""
        lines.append(f"  - {c.id}[{tag}] {c.status}{ev}")

    if state.deps:
        lines.append("【依赖图】")
        lines.append("  " + ", ".join(f"{e.src}->{e.dst}" for e in state.deps))

    if state.summary:
        lines.append("【滚动摘要】")
        lines.append("  " + state.summary.strip().replace("\n", " "))

    return "\n".join(lines)


_HARD_CACHE: set[str] = set()

# ACT 节点里的退出码（`运行 \`pytest -q\`（exit=0）`，由 `actnodes.build_command_ops` 写）。
_EXIT_CODE_RE = re.compile(r"exit=(-?\d+)")


def _verification_line(state: CompressedState) -> str:
    """把"最近一轮的物证有没有被运行验证过"汇成一行。

    只看 `node_kind == "act"` 且轮次 == 最新 act 轮次的节点：ACT 节点的验证小句是 server
    从 `pytest_suite` 渲染的（`actnodes.describe_verification`），所以这一行是客观的，
    不是让模型自己复述。没有 ACT 节点（这轮没写文件）就不输出。
    """
    acts = [f for f in state.active_facts() if f.node_kind == "act"]
    if not acts:
        return ""
    latest = max(f.source_round for f in acts)
    rows = [f for f in acts if f.source_round == latest]
    ids = "、".join(f.id for f in rows)
    codes = [m.group(1) for f in rows for m in _EXIT_CODE_RE.finditer(f.statement)]
    if any("测试通过" in f.statement for f in rows) or (codes and all(c == "0" for c in codes)):
        verdict = "已跑测试且通过"
    elif any("测试未通过" in f.statement for f in rows) or any(c != "0" for c in codes):
        verdict = "已跑测试但未通过"
    else:
        verdict = "未跑测试（只有静态检查，没有任何运行结论）"
    return f"【验证状态】第 {latest} 轮的物证 {len(rows)} 条：{verdict} —— {ids}"


def _hard_ids() -> set[str]:
    return _HARD_CACHE


def register_hard_ids(ids: Sequence[str]) -> None:
    _HARD_CACHE.clear()
    _HARD_CACHE.update(ids)


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #

O_I_FORMAT_BODY = """\
先写自由叙述（设计取舍、为何这样拆模块、遗留问题），然后每个要落盘的文件写成：

```file:相对路径
文件完整内容
```

规则：
- 路径必须是相对仓库根的 POSIX 风格路径，禁止 `..`、禁止绝对路径。
- 每个文件块给出**完整文件内容**，不要给 diff、不要写省略号。
- 不要修改 `tests/frozen/` 下的任何文件（那是冻结的前缀测试，改了也会被还原并计为违约）。
- 不要留下 TODO / FIXME / XXX。

## 工作单位边界（@@UNIT 标记）

每完成一个**连贯的工作单位**，单独占一行写一个标记：

    @@UNIT file 相对路径      # 刚改完这个文件
    @@UNIT validate           # 刚跑过一次验证（冻结测试 / 自测）
    @@UNIT note 一句话        # 一次调研或一个结论（无状态变化）

示例：

    @@UNIT file drawvid/video.py
    ...（该文件的 file 块）...
    @@UNIT validate
    @@UNIT note 对比过 zip 与 tar 的成员开销，zip 更省

规则：
- 只在**真正完成**一个单位后写。不要预告、不要凑数。
- `@@UNIT file` 后面必须是本轮**确实落盘**的路径；写了却没写文件块，会被记为"无据声明"。
- 这些标记不改变你的产出内容，也不改变轮次边界；它们只用来让压缩器正确切分工作单位。
"""


def render_workspace_manifest(root: Path, *, limit: int = 200) -> str:
    """工作区清单：**只有路径与大小，没有内容**。

    G 组的起点就是这个清单 —— 这是"磁盘检索"的真实形态：
    模型知道有什么文件，但必须主动申请才能看到内容。
    """
    if not root.is_dir():
        return "（工作区为空）"
    skip = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "post"}
    lines: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in skip for part in rel.split("/")):
            continue
        try:
            size = p.stat().st_size
        except OSError:  # pragma: no cover
            continue
        lines.append(f"- {rel} ({size}B)")
    if not lines:
        return "（工作区为空）"
    if len(lines) > limit:
        lines = [*lines[:limit], f"…（共 {len(lines)} 个文件，已截断）"]
    return "\n".join(lines)


def read_protocol_spec(cfg: SessionConfig, manifest: str) -> str:
    """磁盘检索段落的**正文**（标题由 build_prompt 的固定骨架给出）。"""
    return f"""\
你**看不到**之前轮次做过什么。唯一的信息来源是磁盘上的代码。

工作区清单（只有路径与大小，没有内容）：

{manifest}

要读文件，单独占一行写：

    @@READ 相对路径

server 会把内容贴回来，然后你**再输出一次**本轮结果。
最多往返 {cfg.work_model.retrieval_max_cycles} 次；单轮最多服务 {cfg.work_model.retrieval_max_files} 个文件、
{cfg.work_model.retrieval_max_bytes} 字节。

规则：
- 路径必须来自上面的清单。要一个不存在的路径会被记为**无效检索**。
- 重复要已经给过你的文件会被记为**冗余检索**。
- 只要还有信息没看，就先 @@READ，不要凭猜测写代码。
"""


READ_RESULT_HEADER = "## 已读取的文件内容（磁盘检索）"


def build_stage_directive(
    prefix: PrefixBundle, stage_index: int, extra: str | None = None
) -> str:
    spec = prefix.stage(stage_index)
    lines = [f"## 本轮（第 {stage_index} 轮）阶段指令"]
    if spec is not None:
        lines.append(f"阶段 {spec.index}：{spec.title}")
        if spec.goal:
            lines.append(f"目标：{spec.goal}")
    else:
        lines.append("阶段超出 P 定义的分解范围，请收尾并做整体验收。")
    if extra:
        lines.append(f"额外要求：{extra}")
    return "\n".join(lines)


def render_ask_prompt(state: CompressedState, question: str) -> str:
    """检视通道的 prompt：**只给当前 C 与这句话**。

    刻意不给 P、不给阶段指令、不要文件块 —— 这条通道是为了让人"看一眼压缩结果是否
    说得通"，不是让模型接着干活。给了 P 它就会去推进项目（实测过）。
    也不给历史：C 就是全部记忆，这样"回答得对不对"直接反映 C 的质量。
    """
    return f"""下面是某个长程编码项目的**工作记忆**（压缩后的循环状态 C）。
请只根据它回答最后那个问题。不要写代码、不要给文件块、不要推进项目。

{render_state_block(state)}

=== 问题 ===
{question or "（没有输入）"}
"""


def stage_directive_body(prefix: PrefixBundle, stage_index: int) -> str:
    """阶段指令的**正文**。

    轮次值只出现在正文里 —— 标题只命名段落，不带值。
    第一版把轮次写进了标题（`本轮阶段指令（第 1 轮）`），于是骨架逐轮都不同，
    是骨架不变量测试把它抓出来的。
    """
    spec = prefix.stage(stage_index)
    lines: list[str] = [f"第 {stage_index} 轮。"]
    if spec is not None:
        lines.append(f"阶段 {spec.index}：{spec.title}")
        if spec.goal:
            lines.append(f"目标：{spec.goal}")
    else:
        lines.append("阶段超出 P 定义的分解范围，请收尾并做整体验收。")
    return "\n".join(lines)


EMPTY_TASK_PLACEHOLDER = "（未指定额外任务；按上一段给出的阶段目标推进。）"


# 对话驱动（cfg.chat_driven）那一段的正文。**是常量**：段落的位置、标题、
# 段数都不随轮数变 —— 骨架不变量这条比内容重要（见下面 build_prompt 的说明）。
CHAT_DIRECTIVE_BODY = """\
本会话不按阶段计划推进。怎么干由两件事决定：循环状态 C 里记着的现状 + 下面的「本轮任务」。

- 这句话是要干活（改代码、补文档、修问题）：照常输出 file: 块，改哪些文件就声明哪些。
- 这句话只是提问、解释、闲聊：**只回话，一个文件都不要动** —— 不要为了"看起来在推进"改工作区。
- **你没有工具**：不能读文件、不能列目录、不能执行命令。正文里不要写 ```bash / ls / cat / find
  这类块，也不要写"我先摸清仓库的实际状态" —— 那些命令不会被执行，那一轮就白烧了。
  你对"磁盘上现在有什么"的全部依据就是上面的 C，加上你在本轮 file: 块里声明的内容。
- C 里标了 `[未落地]` 的事实，表示它挂着的锚点**没解析出来**：文件不存在（还只是计划，
  或者已被回退）。标了 `[符号未见]` 的是**另一种情况：文件在盘上，但里面没有那个符号名**
  （名字是提纯时写下的，可能就是猜的）—— 这一类**不要**读成"计划过但没落地"，
  文件在，只是那个名字对不上，要用就按路径去找真名。锚点后面写了具体是哪一种 ——
  照着它读，别自己脑补。有疑问就写进【未解决】，不要为了"对得上 C"去重写。
- 没做过、不确定的事，写进【未解决】或直接说不知道；不要为了显得在推进而宣称做过。
- C 里 ACT 节点记的 `sha256`/字节数**只用于变更检测**：文件正文不在 C 里。
  别拿它宣称"内容就是这个"，也别声称能复现出字节相同的文件（要看正文就按锚点路径去找）。
- 正文用中文写。
"""


# 「用户原话」段落：用户自己说过的话**逐字带回来**，不改写、不摘要。
# 依据（用户裁定）：事实链 C 替换的是**模型输出的新内容**，不是用户指令 ——
# 让 C 去引用用户要求会失真，而"标准对话框里已经渲染出来的东西是不可变的"。
# 真机证据（会话 caf989977f5f4269）：第 1 轮说了「只用标准库」，第 2 轮让它自评提纯质量，
# 它答"唯一真缺口就是这条约束没进任何节点" —— 上一轮的用户消息既没进 C、也没回流，等于消失。
# 预算：只留最近这些，超了从**最旧**的丢（新要求优先），并在正文里写明丢了几条。
USER_WORDS_MAX_MESSAGES = 24
USER_WORDS_MAX_CHARS = 4000
USER_WORDS_PREAMBLE = "用户之前说过的话（逐字，与 C 无关；C 里只有模型输出的压缩，不含用户指令）："
USER_WORDS_EMPTY = "（这是本会话的第一条用户消息，之前没有别的原话。）"


def render_user_words(messages: Sequence[str]) -> str:
    """把用户原话渲染成一个固定形态的段落（空也有占位，骨架不随轮数变）。"""
    texts = [m.strip() for m in messages if m and m.strip()]
    kept: list[str] = []
    used = 0
    for text in reversed(texts[-USER_WORDS_MAX_MESSAGES:]):
        if kept and used + len(text) > USER_WORDS_MAX_CHARS:
            break
        kept.append(text)
        used += len(text)
    kept.reverse()
    if not kept:
        return USER_WORDS_EMPTY
    omitted = len(texts) - len(kept)
    head = f"（更早的 {omitted} 条用户消息因预算省略。）\n" if omitted > 0 else ""
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(kept, start=1))
    return f"{USER_WORDS_PREAMBLE}\n{head}{numbered}"


def build_prompt(
    *,
    prefix: PrefixBundle,
    state: CompressedState,
    cfg: SessionConfig,
    round_index: int,
    stage_index: int,
    history: Sequence[str] = (),
    extra_instruction: str | None = None,
    workspace_manifest: str = "",
    retrieval: bool = False,
    user_words: Sequence[str] = (),
) -> str:
    """按 history_mode 组装本轮 prompt。

    **段落骨架是不变量**（这条比内容重要得多）：
    段落顺序固定、标题固定、编号固定，而且**每一段总是出现** ——
    没有内容时用显式占位，而不是把段落删掉。

    为什么：如果喂进极简模式的内容**形态**每轮不同（有时多一段临时说明、
    有时少一段），那模型行为的差异就不可归因 —— 你分不清它来自 P/C 还是来自
    某轮多塞的那段话。骨架稳定，才谈得上"违反数随轮数的变化是配置造成的"。

    注意 `full` / `summary` 两种模式的**内容**本来就随轮数增长（那是 B/D/E 组的
    自变量本身），骨架稳定不等于内容稳定。
    """
    mode = cfg.resolved_history_mode()
    sections: list[tuple[str, str]] = []

    # ---- 1) 冻结前缀 P -------------------------------------------------- #
    # prefix_frozen 的语义必须精确，否则会造出"看起来是 A 其实是 B"的假对照组：
    #   True  -> P 每轮原样置于最前（可缓存、字节稳定）
    #   False -> P 只在第 1 轮有内容，之后用一个固定占位代替
    #            （模拟"P 早已滑出上下文窗口"，而不是"从头就没有需求"）
    # 段落本身**始终存在**，否则 D 组的骨架会随轮数变，归因就断了。
    if cfg.prefix_frozen or round_index == 0:
        sections.append(("项目冻结前缀 P（只读，不得修改）", prefix.render()))
    else:
        sections.append(
            (
                "项目冻结前缀 P（只读，不得修改）",
                "（本组刻意不重述 P：模拟需求已经滑出上下文窗口，请依据后续段落推进。）",
            )
        )

    # ---- 2) 记忆段（形态由配置决定，不随轮数变） ------------------------ #
    if mode == "state":
        sections.append(
            ("循环状态 C（工作记忆，唯一可迭代部分）", render_state_block(state))
        )
    elif mode == "full":
        body = ["（本轮是第 1 轮，无历史）"] if not history else []
        body += [f"### 第 {i} 轮输出\n\n{o_i}" for i, o_i in enumerate(history, start=1)]
        sections.append(("历史轮次输出（完整，未压缩）", "\n\n".join(body)))
    elif mode == "summary":
        body = [state.summary or "（本轮是第 1 轮，无摘要）"]
        keep = max(0, cfg.compression.summary_keep_recent)
        recent = list(history)[-keep:] if keep else []
        for offset, o_i in enumerate(recent):
            idx = len(history) - len(recent) + offset + 1
            body.append(f"### 第 {idx} 轮输出（原始，保留最近 {keep} 轮）\n\n{o_i}")
        sections.append(("前情摘要与最近轮次", "\n\n".join(body)))
    elif mode == "none":
        sections.append(
            ("循环状态 C（工作记忆）", "（本组不使用工作记忆，本段恒为空。）")
        )
    else:  # pragma: no cover
        raise ValueError(f"未知 history_mode {mode!r}")

    # ---- 3) 阶段指令（或对话驱动下的"该不该动手"） ---------------------- #
    if cfg.chat_driven:
        sections.append(("本轮该怎么干", CHAT_DIRECTIVE_BODY))
        # 用户自己的话逐字带回来。C 只压缩**模型的输出**，不记用户指令（用户裁定），
        # 所以不回流就等于"上一轮说的范围约束凭空消失"。
        sections.append(("用户原话（逐字，不压缩）", render_user_words(user_words)))
    else:
        sections.append(
            ("本轮阶段指令", stage_directive_body(prefix, stage_index))
        )

    # ---- 4) 本轮任务：恒定存在的独立段落 -------------------------------- #
    # 任务以前是拼在阶段指令里的"额外要求"，有则出现、无则消失 —— 骨架随轮数变。
    # 现在它有自己的标题、自己的位置，空的时候给显式占位。
    task = (extra_instruction or "").strip()
    sections.append(("本轮任务", task or EMPTY_TASK_PLACEHOLDER))

    # ---- 5) 磁盘检索（是否出现由配置决定，跨轮不变） -------------------- #
    if retrieval:
        sections.append(
            (
                "磁盘检索（你没有历史、也没有工作记忆）",
                read_protocol_spec(cfg, workspace_manifest or "（工作区为空）"),
            )
        )

    # ---- 6) 输出格式 ---------------------------------------------------- #
    sections.append(("本轮输出格式（机器解析，必须严格遵守）", O_I_FORMAT_BODY))

    # 顶层段落用一条**定界符**标出，而不是靠 `#` 层级：
    # P 的正文里本来就有各级标题（PROJECT.md 自己就有一级标题），
    # 用 `#` 当骨架标记必然和内容撞车 —— 第一次实现就被这撞了一次。
    rendered = [
        f"# ===== {index}. {title} =====\n\n{body.strip()}"
        for index, (title, body) in enumerate(sections, start=1)
    ]
    return "\n\n".join(rendered) + "\n"


# 骨架标记：`# ===== 1. 标题 =====`。用正则抽取而不是"看前缀"，
# 这样 P 或输出格式段落里的任意层级标题都不会被误当成骨架。
SECTION_RE = re.compile(r"^# ===== (\d+)\. (.+?) =====$", re.MULTILINE)


def extract_skeleton(prompt: str) -> list[str]:
    """抽出 prompt 的顶层骨架。用于断言"形态不随轮数变"。"""
    return [m.group(0) for m in SECTION_RE.finditer(prompt)]


def _out_cap(c_max_tokens: int, out_max_tokens: int) -> int:
    """提示词里那个"硬上限"到底该写多少 —— 必须是**这次调用真实的上限**。

    两个坑都踩过：
    1. 写 `c_max_tokens`（C 的容量，2000）而真实输出上限是压缩器的 `max_tokens`（1200）：
       模型照着 2000 写，写到 1200 被硬截断，JSON 断在半句上，整批 ΔC 静默丢失。
    2. `c_max_tokens=0`（编排路径用它表示"不裁 C"）**不是**"输出零 token"：
       提示词里成了"必须控制在 0 tokens 以内"，模型老实回了 `{"ops": []}` ——
       一整块思考一条节点都没提。所以 0 要当成"没给这个约束"。
    """
    if out_max_tokens and c_max_tokens > 0:
        return min(c_max_tokens, out_max_tokens)
    return out_max_tokens or c_max_tokens or 1200


def build_compressor_prompt(
    *,
    o_i: str,
    round_index: int,
    prior_nodes: Sequence[str] = (),
    prior_failures: Sequence[str] = (),
    c_max_tokens: int,
    units: Sequence[str] = (),
    out_max_tokens: int = 0,
    landed: Sequence[str] = (),
) -> str:
    """压缩器 prompt：前缀隔离（不给 P），只给 O_i + C_{i-1} 的**结构头部**。

    给结构头部的理由：无状态压缩器无法判断自己提取的节点是否推翻了旧节点，
    "更正沿依赖传播"就落不了地。给 id + 一行标签既能让它表达 SUPERSEDE/INVALIDATE，
    又不把 P 或旧散文灌进来，成本仍被 c_max_tokens 夹住。
    """
    prior = "\n".join(f"- {x}" for x in prior_nodes) or "（无）"
    fails = "\n".join(f"- {x}" for x in prior_failures) or "（无）"
    units_block = "\n".join(f"- {u}" for u in units) or "（未提供；按 file 块与叙述自行切分）"
    landed_block = "\n".join(f"- {x}" for x in landed) or "（本轮没有任何文件被写出去）"
    # 提示词里的"预算"必须是**这次调用真实的上限**。
    # 以前这里写的是 c_max_tokens（C 的容量，2000），而真正的输出上限是压缩器的
    # max_tokens（1200）—— 模型照着 2000 写，写到 1200 就被硬截断，JSON 断在半句上。
    out_cap = _out_cap(c_max_tokens, out_max_tokens)
    # 轮号**不许出现在前言里**：它每轮都变，会把前缀缓存从那一行切断。
    # 更根本的一条**排版不变量**：这份 prompt 里"逐字不变的文字"必须全部排在前面，
    # "每轮都变的文字"（先验节点 / 失败链 / 单位边界 / O_i / 轮号）必须全部排在后面。
    # 实测（探针 scripts/_probe_compressor_prefix.py）：重排前第一处不同出现在第 173 个字符 —— 可缓存前缀
    # 只有 150 tokens，压缩器命中率恒为 0%；重排后固定段（约 1.4K tokens）全部落在前面。
    round_note = f"（本轮是第 {round_index} 轮。）"

    return f"""你是代码项目的工作记忆管理器，不是程序员。
你的职责是**提取事实链**，不是压缩文本。产出的东西要被用来判断"下一轮能不能安全地做改动"，
所以**一条错的事实比少一条事实更糟**。

输入：本轮完整的开发草稿 O_i（原文在文末）。
任务：只提取**结构化操作**，输出 JSON。不要写散文。

可用操作：
- ADD_FACT        新增事实节点。必须来自 O_i，不能编造。
                  payload: {{id, statement, deps:[节点id], anchor:{{path, symbol}}, salience}}
                  **id 必须是 `N` 加数字**（N1、N2、N3…），且与已有节点不重复。
                  server 只接受这个格式；写成 F1 / A1 / 事实1 会被**直接拒收**，
                  那样这一轮提取出的事实就全丢了（实测踩过：9 条里被拒 7 条，事实链空了一整轮）。
                  salience 是 0-100 的显著度：预算不够时按它从低到高丢弃。
                  给 80-100 的是"丢了就会做错下一步"的接口/架构决定；
                  给 30-50 的是"从代码里还能重新看出来"的普通事实。
- SUPERSEDE_FACT  新事实推翻旧节点。target=旧id，payload: {{id, statement, anchor}}
- INVALIDATE_FACT 旧节点被证伪。target=旧id
- ADD_FAILURE     被否决的设计方案。payload: {{attempt, reason}}
- SET_PROGRESS    1-2 句本轮完成了什么。payload: {{text}}
- SET_UNRESOLVED  下一轮要处理的 1-3 条。payload: {{items:[...]}}
- ADD_DEP / DROP_DEP  模块依赖。payload: {{src, dst}}

anchor 必须是 O_i 里真实出现的文件路径和符号名。给不出锚点就不要给 anchor 字段。
事实只许记**已经发生**的事：本轮 file: 块里真的写出来的文件、里面真的定义了的接口。
"打算做 / 下一轮要做 / 设计上应该如此"不是事实 —— 那属于 SET_PROGRESS 或 SET_UNRESOLVED。
如果一句话里提到的文件本轮并没有写出来（只是计划），**不要**给它 ADD_FACT 和那个路径的锚点：
锚点指不到真实存在的文件，下一轮读 C 的人会把它当成已经写完，然后去重写或去"盘点"。
文末有 server 给出的**真实落盘清单** —— 那是唯一可信的"已完成"依据，以它为准。
`symbol` 只对 `.py` 路径给。给 markdown / 文档 / 配置文件编一个符号
（例如 `README.md::drawvid`、`docs/ARCHITECTURE.md::架构`）会让这条事实**永远**解析不到，
于是一份真实存在的文档被永久标成"未落地" —— 非代码文件只给 `path`。
**不要记你自己对 C 的评论、疑问或"口径冲突"分析**（例如"当前面板同时给出 C2 与 C15，
二者矛盾"）。那是元评论，不是项目事实；把它记进事实链，下一轮就会接着讨论这条评论，
真正的工作被挤掉。有疑问写进 SET_UNRESOLVED。
**不要记"本轮未创建、修改或删除任何文件"这类空转陈述** —— 它不改变任何后续决策。

每条事实必须**自足**：读它的人没有历史、不能读盘、也不能来问你。
写"文件 + 符号 + 它的确切形态"，不要写"已按 P 实现""同上""见上文"这种要靠上下文才懂的话。
- 文件路径、类名/函数名/参数名与默认值、常量值、输出格式（例如 JPEG 的 quality/subsampling/optimize）
  都要落在 statement 里 —— 这些正是下一轮照着动手所需要的东西；省掉它们，下一轮就只能把文件重写一遍。
- 同一个文件的多个符号可以合成一条事实，但要把该文件**已经实现的内容列全**，宁长勿糊。
- 每条事实都要能回答"下一轮能不能不动这个文件"。已经做完、这一轮又没改动的文件，
  用一条事实把它的状态讲清楚；**不要**每轮都把同一个文件重新 ADD 一遍 ——
  重复的事实会让下一轮以为"又做了一次"，真实进度反而被掩盖。
- 用**已完成**的语气陈述现状（"X 定义在 a.py，签名是 …"），不要用"打算/接下来/将要"。
- 每轮至少给 1 条**文件清单**事实：把上面"工作单位边界"里出现过的文件合成一条，
  说清每个文件现在的状态（新建 / 改动 / 删除 / 未变）。锚点挂其中一个真实存在的文件。

禁止：引入 O_i 中不存在的新接口、新模块、新结论。
禁止：修改任何既有接口签名、模块名、约束条件。
整份输出（含 JSON 外的字符）必须控制在 {out_cap} tokens 以内 —— 这不是建议，是**硬上限**：
超过它输出会被原地截断，JSON 断在半句上，**这一轮的事实会全部丢失**（实测踩过两次）。

**预算耗尽时丢弃是有代价的，不是免费的取舍。** 丢掉一条后续轮次需要的事实，
下一轮就会在缺前提的情况下自信地做错事 —— 那比多花 token 贵得多。
所以：只在确实超预算时才丢，按 `salience` 从低到高丢，
并且**优先丢"从代码里还能重新看出来"的那一类**（例如"某测试存在"）。
"丢了就会做错下一步"的接口/架构决定，宁可让它顶着预算也要留下。
如果为了不超预算而不得不丢掉这类事实，请在 `SET_UNRESOLVED` 里明说丢了什么。

只输出一个 JSON 对象：
{{"ops": [ ... ]}}

一个合法的 op 长这样（注意 id 用 N 前缀、anchor 指向 O_i 里真实出现的路径与符号）：

{{"op": "ADD_FACT", "payload": {{"id": "N1",
  "statement": "接口 drawvid.core.Timeline.frame_count 已定义",
  "deps": [], "anchor": {{"path": "drawvid/core.py", "symbol": "frame_count"}},
  "salience": 80}}}}

已有的状态节点（只读，用于判断是否推翻）：
{prior}

已有的失败链条目（只读，用于判断是否重复尝试）：
{fails}

本轮的工作单位边界（由 server 从**可验证的状态变化**推导，可信）：
{units_block}
请让事实节点与这些单位对齐：一个单位里的接口定义合成一条事实，
不要把同一个单位拆成多条，也不要把不同单位的事实混成一条。

本轮 server 记下的**真实落盘清单**（逐字来自写盘结果，唯一可信的"已完成"依据）：
{landed_block}
清单里没有的路径，一律不许用"已落盘 / 已实现 / 已写完"这类完成语气 ——
只能写"计划"，或者干脆不记。锚点也只在清单里（或 O_i 的 file: 块里）有的路径上给。

=== O_i 开始 ===
{round_note}
{o_i}
=== O_i 结束 ===
"""


def build_thinking_prompt(
    *,
    thinking: str,
    round_index: int,
    prior_nodes: Sequence[str] = (),
    c_max_tokens: int,
    out_max_tokens: int = 0,
    landed: Sequence[str] = (),
    act_units: Sequence[str] = (),
) -> str:
    """**推理编排器**的 prompt（README §1.3）：把一段 thinking 压成关键推理节点链。

    与 `build_compressor_prompt` 的两处根本差别：

    1. **输入只有 thinking 块**，正文与动作块都不进 —— 正文是给人看的不可变纸，
       动作是 server 自己记的物证；只有 thinking 是"为什么"唯一的住处。
       而且单段输入比整轮 O_i 小得多，输出预算的压力跟着小（实测 1200 上限曾被复述挤爆、
       6 轮里 2 轮静默丢了整个 ΔC）。
    2. **产出不是结论，是链**：正节点（被采纳的路线 + 为什么成立）与负节点
       （被否掉的路线 + 为什么被否）并列。压掉的是填充与自我对话，不是推理骨架。

    排版不变量与 `build_compressor_prompt` 一致：逐字不变的文字排在前面、
    每轮都变的文字（先验节点 / 落盘清单 / 轮号 / thinking 原文）排在后面，
    否则压缩器的前缀缓存会被第一处变动切断（实测：可缓存前缀从 1529 tokens 掉到 150）。
    """
    prior = "\n".join(f"- {x}" for x in prior_nodes) or "（无）"
    landed_block = "\n".join(f"- {x}" for x in landed) or "（本轮没有任何文件被写出去）"
    units_block = "\n".join(f"- {u}" for u in act_units) or "（无）"
    out_cap = _out_cap(c_max_tokens, out_max_tokens)
    round_note = f"（本轮是第 {round_index} 轮。）"

    return f"""你是工作记忆的**推理编排器**，不是程序员，也不是摘要器。
输入是工作模型这一轮的 thinking（思考草稿）。你要把它压成**关键推理节点链**：
留下推理骨架，丢掉填充。**一条错节点比少一条节点更糟**。

要丢掉的（它们占掉的正是预算）：
- 填充与自我对话："let me…"、"嗯…"、"让我重新考虑"、"用户想要我…"、把任务复述一遍。
- 同一个方案被反复推演、来回犹豫、把已经确定的结论再确认一遍。
- 逐字的试探过程（"先看看 A，不对，还是 B…"）——只留最后被采纳的那条。

要留下的，每条一个节点：
- **正节点** `polarity="+"`：一条被采纳的路线，以及它**为什么成立**（依据是什么）。
- **负节点** `polarity="-"`：一条被否掉的路线，以及它**为什么被否**。
  负节点比正节点更贵 —— 它拦住的是"下一轮重新走一遍已经否掉的路"，不许省、不许合并成一句
  "考虑过别的方法"。

输出：只输出一个 JSON 对象 `{{"ops": [ ... ]}}`，不要散文。

节点用这个形状（id 必须是 `N` 加数字，与已有节点不重复）：
{{"op": "ADD_FACT", "payload": {{"id": "N9",
  "node_kind": "think", "polarity": "+",
  "statement": "选 Timeline 当唯一事实源：帧数由 duration×fps 推导，渲染层不许自己攒状态",
  "why": "渲染层攒状态会让同一份输入产出不同帧，破坏确定性验收",
  "anchor": {{"path": "drawvid/core.py", "symbol": "Timeline"}}, "salience": 75}}}}

带已知隐患的节点多一个字段 `"risk"`（决定已经定了、但埋着一个坑）：
{{"op": "ADD_FACT", "payload": {{"id": "N11",
  "node_kind": "think", "polarity": "+",
  "statement": "测试文件不建包，靠 sys.path 把自身目录插进来再 from duration import",
  "why": "从 tools/ 下直接跑和从仓库根跑都要能导入",
  "risk": "tools/ 没有 __init__.py 时 `unittest discover` 可能递归不进这个目录，下一轮跑测试要显式给路径",
  "anchor": {{"path": "tools/test_duration.py"}}, "salience": 80}}}}

还没定下来的节点多一个字段 `"assertion": "open"`（不写这个字段 = 已定）；写成这样：
{{"op": "ADD_FACT", "payload": {{"id": "N10",
  "node_kind": "think", "polarity": "+", "assertion": "open",
  "statement": "是否把布尔值当合法输入：待定 —— 需要先确认调用方会不会传 True",
  "why": "bool 是 int 子类，不拦的话 format_duration(True) 会静默算出 1 秒",
  "anchor": {{"path": "tools/duration.py", "symbol": "format_duration"}}, "salience": 70}}}}

规则（前三条是硬的，违反的节点会被 server 直接丢掉）：
1. **禁止完成态断言。** 不许写"已实现 / 已落盘 / 已完成 / 测试通过 / 验证通过"。
   推理里的"做完了"只是猜测 —— 完成状态只能由 server 的 ACT 节点给出。
   要表达"我以为做完了"，写成推理："根据 X 推断 Y 已经就位（待物证核对）"。
2. **锚点：有就核对，没有也行，但不许编。** `anchor.path` 若写了，必须是**下面动作清单里
   出现过的真实路径**（或本轮真的被写出的文件）—— 编一个不存在的产物，服务端会丢掉这条节点。
   而**不碰文件的推理**（纯推理题的判断顺序、与文件无关的权衡、风险）**可以不给 anchor 字段**：
   服务端会保留它，只是标成"没有物证"，C 装不下时先丢它。别为了凑锚点硬挂一个无关文件。
   **同一个文件内部的方案分歧**（导入方式、测试框架、数据结构、命名）锚点就挂那个文件：
   "否掉 `from tools.duration`：从 tools/ 下直接跑会导入失败，改成 sys.path + `from duration`"
   是这条链最值钱的一类负节点（少了它，下一轮会把已经否掉的写法再走一遍）。
3. **statement 自足**：读它的人没看过这段思考、不能读盘、也不能来问你。
   写清"选了什么/否了什么 + 依据"，不要写"同上""见上文""按计划"。
   **不许只给类别**：写"测试覆盖边界情况"没用，写"测试覆盖 59/60、3599/3600、86399/86400"才有用；
   凡是能给出具体值、具体名字、具体阈值的地方都要给出来（事后没人能回到这段思考里补）。
   **`why` 是必填的**：一句话说清这条为什么成立/为什么被否 —— 服务端会把它渲染在节点上。
   只留结论不留依据，下一轮就会把同一个权衡重新走一遍（真机自述把它列为扣分主因）。
4. `salience` 0-100：80-100 = 丢了下一步就会做错（架构/接口级决定）；
   30-50 = 从代码里还能重新看出来。负节点的 salience 不要低于 70。
5. 同一件事只写一条。没有值得留下的推理（例如这轮只是闲聊）就输出 {{"ops": []}}。
6. **不要记你对 C 本身的评论**（"这两条矛盾""这条节点是废话"）。那是元评论，不是项目推理；
   把它记进链，下一轮就会接着讨论这条评论，真正的工作被挤掉。有疑问写进 SET_UNRESOLVED。
7. **锚点能细到符号就细到符号**：`.py` 里的顶层函数/类，名字逐字照抄进 `"symbol"`。
   只给 `path`（`symbol` 空着）时，验证器只能确认"文件在"，没法确认"那个符号还在" ——
   下一轮就无法据此核对，只能重读文件。
8. **一条节点只讲一件事**：不要用 `/`、`或者` 把两条路线并列在一句里（"试 A / 试 B 都行"），
   读起来分不清是选择还是清单。两条路线就写两条节点；负节点各自写自己的否掉理由。
9. **只记对外可观察的决定，不记实现手段**：换一种写法对外行为不变的东西（"用 divmod 拆秒"、
   "用 f-string 补零"）不配占据节点预算 —— 要么升格成对外契约，要么别记。
   要留就留在 `why` 的依据里，不要单独开节点。
   **但别把"对外契约"当实现手段压掉**（真机自述的扣分主因）：以下都必须写全、具体到值 ——
   ① **输入校验与异常类型**：哪种输入抛哪个异常（`-1` → `ValueError`、`1.5` 与 `True` → `TypeError`），
      "拒绝非法输入"这种写法等于没写；② **测试覆盖了哪些具体输入**（含边界值与非法值）；
   ③ 输出格式的边界（超过 99 小时是继续补位还是回绕）；
   ④ 测试文件的**用例划分与方法名**（下一轮要"补一条用例"时，只有值清单不知道分组边界，
     容易重复或错位 —— 原文有明确划分就记一条）。
10. **"不做 X"必须单独成条**：凡是你决定**不**做的事 —— 不加某个文件、不引某个依赖、
   不改某个既有文件、不做某类优化、不新增 `__init__.py` —— 都要写成一条 `polarity="-"` 的节点，
   statement 用条款语气："不新建 tools/__init__.py（单文件工具不需要包结构）"。
   这类节点字数极少、风险极高：下一轮只看得见结果，看不见你当时克制的理由，
   于是会顺手把 `__init__.py` 加上、把依赖引进来。它们的 `salience` 给 80 以上。
   注意：**不要把用户的要求原样抄成节点**（那是 P 的职责，P 每轮都在上下文里）；
   要记的是"你据此做出的工程决定"，并写清依据。
   如果这条"不做"是**权衡之后否掉的备选**（原文先考虑过另一种更省事的做法，再决定不用），
   把那个备选写进 `why`，别只留单方面理由 —— 例：`why: "曾考虑把 True 当 1 处理（更省事），
   最终以'明确拒绝'落地"`。下一轮要重新讨论这个决定时，这条备选就是现成的依据；
   只留结论的话，下一轮会以为它从来不是选项。
11. **没定的事不许写成定了的事**：还在权衡、还没验证、只是"先这么办试试"的，
    要么加 `"assertion": "open"`（它会渲染成 `⟨待定⟩`，下一轮一眼看出不能当依据），
    要么写进 SET_UNRESOLVED。**不许**用确定语气写一条你自己都还没定的结论 ——
    下一轮会拿它当既成事实往下推，而且不会再回来检查。
    典型场合：**动作做了但没验证**（文件写了但没跑测试）—— 这属于"待定"，
    写成节点就加 `"assertion": "open"`（ACT 节点上 server 也会标出本轮的验证状态）。
12. **已知风险单独一个槽位**：决定可以是**已定的、同时又带一个坑** ——
    "这么定，但某个入口可能不兼容""这个假设还没验证""这个边界没测"。
    这类内容加 `"risk": "什么条件下会出问题"`（不是 `assertion="open"`：决定已经定了，
    风险是它的附加条件）。**不许**把风险揉进 statement 当结论写，也不许因为
    "它只是个担心"就丢掉：下一轮踩坑的代价，比多写这十几个字大得多。
    例：**这条方案的正确性完全靠"跑"才能确认**（导入路径、相对路径、打包/发现方式），
    而本轮根本没跑过测试 —— 加 `"risk": "未经运行验证（本轮没跑测试）"`。
    但别给每条节点都贴这一句：ACT 节点上服务端已经标了本轮的验证状态，
    只给**靠跑才能确认**的那几条。
13. **只压不补 —— 这是最硬的一条。** 你的职责是**压缩**，不是补全、不是替上一轮继续想：
    - 节点里的每个具体物（文件名、符号名、数字、输入值、异常类型）都必须在**原文里出现过**。
      原文没提到的，一个字都不许写进来 —— 哪怕你觉得它显然该写。
    - 规则 10（不做 X）/ 11（未定）/ 12（风险）**只在原文真有这类内容时才用**。
      原文没提到"要不要建 `__init__.py`"、没提到"布尔值怎么处理"，就不要为了凑格式造出这两条。
    - **不要替上一轮补它没想到的检查项**，也不要把它的"猜"（"presumably"、"大概"、"应该"）
      改写成有依据的决定：你补的每一分道理，下一轮都会当成它自己想过的东西接受。
    - 原文很干、没什么可记的，就**少输出几条甚至输出空 ops** —— 空是合格答案，不是失败。
      宁缺毋滥：一条编出来的节点比少一条节点糟得多。
14. **不确定的符号名一个都别写**：`anchor.symbol` 只填**原文里逐字出现过的**名字。
    原文只在说"一个测试类/几个用例方法"、没说具体方法名时，就只给 `anchor.path`（文件路径），
    别顺手拼一个 `test_xxx` 上去 —— 服务端会去文件里核对，对不上的名字会让这条节点被标成
    "符号未见"，下一轮读起来像"计划过但没落地"，比不写名字糟得多
    （真机自述第 1、3 条就是这个：它按一个不存在的方法名去找，然后以为用例没写进文件）。
15. **类型/边界处理的结论必须进节点**：原文里凡讨论过"某个输入该怎么算"（`bool`/`None`/
    浮点/超长值/空值），无论最后是"拒绝"还是"不检查"，都要落成一条节点 ——
    前者是 `polarity="-"`（"不接受 `bool`"），后者写成 `statement` 里明确的行为
    （"`bool` 不单独拦，按 `int` 处理"）。这类信息的价值不在长度，而在下一轮做类型校验、
    写边界测试时会直接用到；漏了它，下一轮要么误判行为、要么重跑一遍同样的权衡
    （真机自述连续两轮把它列进"真丢"）。

**六条兜底由服务端做**（写错不致命，但会被改掉）：
- 原文里出现「不知道应否 / 待定 / 拿不准 / 要不要」这类措辞的地方，附近那条节点的
  `assertion` 会被自动改成 `open`（`⟨待定⟩`）；
- 原文里"纯粹一条否决"（不新建/不加/不引入 …）的节点会被自动改成 `polarity="-"` 并抬到 salience 80；
- 原文里的「A 或 B」二选一、「A 而不是 B」的散文对比、**草稿里 import 过但最终文件里没有**的依赖、
  以及**草稿里写过或提到过 `try/except ImportError` 兜底**（贴了代码块、或只在散文里提了一句
  都算）而落盘文件里没有这种结构的，若你自己没记，服务端会各补一条带 `why` 的负节点
  （「不再采用 B」「不再引入 X」）；
- 你自己给过结论的悬念（"要不要 X？→ 不加/未新建/按…处理"）不该留在 SET_UNRESOLVED 里：
  服务端会把这类条目搬进事实链，写成一条负节点；
- **识破但先搁置的风险**（原文写了"……不管了 / 先不管 / 搁置 / 以后再说"这类话）会补成一条
  带 `risk` 的节点 —— 下一轮踩坑时，这条是"第一嫌疑"；你若自己记了就不会重复；
- 只是把 P 里现成的约束复述一遍的负节点（"不修改冻结测试"这类，P 每轮逐字都在上下文里）
  会被丢掉 —— 推得出来的信息不占 C 的预算。
所以你真正不能出错的是**具体物必须来自原文**（规则 13）：标签写错会被纠正，内容编的找不回来。

可用操作：
- ADD_FACT        新增推理节点（payload 见上，必须带 node_kind="think" 与 polarity；
                 还没定的加 assertion="open"）
- ADD_FAILURE     被否决的方案（payload: {{attempt, reason}}）
- SET_PROGRESS    1-2 句本轮推进了什么。payload: {{text}}
- SET_UNRESOLVED  下一轮要处理的 1-3 条。payload: {{items:[...]}}

整份输出（含 JSON 外的字符）必须控制在 {out_cap} tokens 以内 —— 这是**硬上限**，
超过会被原地截断、JSON 断在半句上，这一段推理节点就全丢了。

已有的状态节点（只读，用于判断是否推翻）：
{prior}

本轮 server 侧真实发生的动作（逐字来自写盘与验证结果，**唯一可信的"已完成"依据**）：
{landed_block}
动作单位边界（server 推导）：
{units_block}

=== thinking 开始 ===
{round_note}
{thinking}
=== thinking 结束 ===
"""
