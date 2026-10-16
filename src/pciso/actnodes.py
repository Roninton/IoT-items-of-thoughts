"""ACT 节点（server 渲染的动作事实）与 THINK 节点的服务端过滤 —— README §1.3。

**为什么 ACT 不能由模型产生。** 模型写的"事实"可能是计划：真机实测过一轮（用户说"先别动文件"），
压缩器把设计讲稿记成了 12 条事实，锚点内部全标着 `resolved=False / 文件不存在`，而渲染出来的
正文读起来就是"已经写完了" —— 下一轮的工作模型没有读盘通道，只能照 C 办事，于是它开始"盘点、重写"。
把事实的**生产者**限定成 telemetry，这件事就不是纪律问题，而是**写不出来**。

ACT 节点的老化规则也在这一层：同一个路径再次被写 → 用 `SUPERSEDE_FACT` 取代旧 ACT 节点。
没有这条，C 会随**写入次数**线性增长（README §1.3 说的"随动作数增长"里最凶的一项）。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import CompressedState, DeltaC, DeltaOp, FactNode
from .pyast import SymbolInfo, module_level_public_defs, parse_source

# 完成态断言：模型在推理里替 server 宣布"做完了"。
# 推理里的"做完了"只是猜测，而 C 是下一轮唯一的依据 —— 这类节点一律丢弃（并计数，
# 不静默丢：计数进 telemetry，否则"推理在冒充事实"这件事在数据里看不见）。
COMPLETION_CLAIM_RE = re.compile(
    r"已实现|已落盘|已完成|已写完|已建好|已修好|已修复|测试通过|验证通过|验收通过|跑通了|通过了"
    r"|implemented|completed|tests? pass|passes",
    re.IGNORECASE,
)

ACT_SALIENCE = 70

# **元评论**：THINK 节点在议论别的节点（"否掉 N1 的表述方式"、"N2 与 N5 矛盾"）。
# 提示词里已经写了"不要记你对 C 的评论"，但真机实测（会话 4746898cbe504248）里，
# 让它"评审一下 C 里的推理节点"时它照样记了 5 条 —— 提示词是纪律，不是机制。
# 这类节点的代价很具体：下一轮的工作模型看到的是"上一条节点写得不清楚"，
# 于是接着讨论节点本身，真正的工作被挤掉；而节点间的依赖关系本来该走 `deps` 字段。
_META_REF_RE = re.compile(r"(?<![A-Za-z0-9])N\d+")

# "决定不做 X"的条款式负节点（提示词规则 10）。**这是度量不是判定** ——
# 它回答的是"这一轮有多少条'克制'被记下来了"。真机自述里，模型把"不加 __init__.py"
# 这类低字数高风险的克制列为自己丢掉的第二类信息；没有这条计数就无从知道
# 提示词规则 10 到底有没有生效（0 与"没记"必须能区分）。
NEGATIVE_CONSTRAINT_RE = re.compile(
    r"不(?:做|加|引|改|修|新|动|碰|建|删|重写|覆盖|使用|采用)|禁止|不许|不要|避免|仅用|只用"
)

# **不确定信号的窄集**：只收"这处还没定"的措辞。刻意**不收** `是否` / `可能` / `大概` ——
# 那些通常在同一段里就被自己回答掉了（原文"是否仍然输出更多位数？…合理"），
# 拿它们当未定信号会把已定的事标成待定，污染下一轮的判断。精度优先。
# 来源：会话 ba902da26e6c4b20 —— 原文 L5 明写"不知道应否"，节点里却是 `assertion: settled`
# （提示词规则 11 管不住），所以"未定"必须由服务端从原文自己判。
UNCERTAINTY_RE = re.compile(
    r"不知道应否|不知道要不要|不知道是否|不知道|不确定|待定|存疑|拿不准|没定|尚未决定"
    r"|需要确认|tbd|not sure|unclear",
    re.IGNORECASE,
)
# 那句 + 前面两句（不超过这么多字）算"这个话题的簇"：句级太窄（真机那句"不知道应否"独立成句，
# 话题 `bool` 在上一句），整段太宽（第一版 ±160 字把 `unittest`/`pytest` 全罩进来，
# 一轮 8 条节点降级 6 条 —— 满屏待定等于没有待定）。
_CLUSTER_CHARS = 200

# "决定不做 X"（克制）的**改标签**口径：比 `NEGATIVE_CONSTRAINT_RE`（度量口径）更宽。
_NEGATIVE_CLAUSE_RE = re.compile(
    r"不加|不新建|不新|不引(?:入|用|进)|不依赖|不使用|不采用|不做|不写|不动|不改|不删|不覆盖|不重写|不碰"
    r"|不允许|禁止|避免|仅(?:用|做|限)|只用|只做|只写|只加|不需要|不用"
)
# 一句话里既有"克制"又有正面主张时（"测试用 unittest 覆盖 …；不引入 pytest"），
# 不该整条改成负节点 —— 只给它负节点的保护（salience 地板），标签留给模型自己写的那半句。
_POSITIVE_CLAIM_RE = re.compile(
    r"覆盖|写入|采用|使用|实现|输出|返回|生成|跑|执行|添加|新建|定义|def |assert|import |调用|接收"
)


def describe_verification(pytest_passed: bool | None, detail: str = "") -> tuple[str, str]:
    """`(要写进 ACT 节点的小句, 计数用的状态)` —— 由 server 从验证结果算，零模型调用。

    来源是真机自述（会话 f8f438e95d924713，轮 2）：「本次未实际运行测试」只被记在 C 的
    【未解决】里，节点本身没写 —— 而"测试没跑"和"测试绿了"对下一轮是完全不同的两件事。
    完成状态只能由 server 说，**验证状态同理**：与其求模型自己记，不如从 `pytest_suite`
    那条 check 直接渲染出来。`passed is None` 就是"这轮没有跑过测试"，这不是失败。
    """
    if pytest_passed is None:
        return "；本轮未跑测试", "none"
    tag = "测试通过" if pytest_passed else "测试未通过"
    state = "passed" if pytest_passed else "failed"
    return (f"；{tag}（{detail}）" if detail else f"；{tag}"), state


def _id_picker(prior_state: CompressedState, reserved: Sequence[str] = ()):
    """给 ACT 节点分配 id：接在已有节点与**同一批模型 ops** 后面。

    必须先算上同一批里的模型 ops：server ops 与模型 ops 会合成一个 ΔC 一起进 apply_delta，
    两边都从 `next_fact_id()` 起编号的话，后到的那批会整批撞上 "ADD_FACT:N3 已存在" 被拒收。
    """
    used = {f.id for f in prior_state.fact_chain}
    used.update(str(x) for x in reserved if x)
    counter = 0

    def nxt() -> str:
        nonlocal counter
        while True:
            counter += 1
            cand = f"N{_max_num(used) + 1}"
            if cand not in used:
                used.add(cand)
                return cand

    return nxt


def _max_num(ids) -> int:
    nums = [int(i[1:]) for i in ids if re.fullmatch(r"N\d+", str(i))]
    return max(nums, default=0)


def _dedup_written(written: Sequence[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    """同一个路径在一轮里被写了两次（模型贴了两个 `file:` 块）时只留最后一次。

    真机实例（会话 `da5f070d64014d7b`）：`tools/test_duration.py` 被写了两遍，
    C 里因此出现两条字节数和 sha 完全相同的 ACT 节点（N8、N9）——同一件事占两份预算。
    """
    last: dict[str, tuple[str, str, int]] = {}
    for item in written:
        last[item[0]] = item
    return list(last.values())


def build_act_ops(
    *,
    prior_state: CompressedState,
    written: Sequence[tuple[str, str, int]],
    reserved_ids: Sequence[str] = (),
    sources: Mapping[str, str] | None = None,
    verify: str = "",
) -> list[DeltaOp]:
    """把"这一轮真的发生了什么"渲染成 ACT 节点。

    `written` 是 `(path, sha256, nbytes)` 三元组，逐字来自写盘结果 —— 这里**没有任何模型调用**。

    **锚点粒度到符号。** 真机自述（会话 aae79de300504908，轮 1）里模型自己指出：
    一条节点的锚点写成 `tools/duration.py::`（双冒号后空着）时，"验证器无法据此确认符号是否存在"——
    它只能确认文件在。所以 `.py` 文件按 **AST 顶层公开符号**逐个出节点（带签名），
    解析不出符号（markdown / 数据文件 / 空模块）才退回路径级一条。

    同一路径已有活跃 ACT 节点 → 该路径的**全部**旧节点被 SUPERSEDE 取代（老化规则）：
    文件被整份重写时旧符号可能已经不存在了，只取代"同名符号"会留下幽灵节点。
    """
    nxt = _id_picker(prior_state, reserved_ids)
    stale: dict[str, list[str]] = {}
    for f in prior_state.fact_chain:
        if f.status == "active" and f.node_kind == "act" and f.anchor is not None:
            stale.setdefault(f.anchor.path, []).append(f.id)

    ops: list[DeltaOp] = []
    for path, sha, nbytes in _dedup_written(written):
        # `verify` 是 server 从验证结果算出来的小句（"；本轮未跑测试" / "；测试通过（…）"）：
        # 让 ACT 节点自己带上"验证到哪一步"，否则下一轮只能靠 C 的【未解决】去猜。
        tail = f"（{nbytes}B，sha256 {sha[:12]}）{verify}"
        targets = list(stale.get(path, []))
        symbols = _symbols_for(path, (sources or {}).get(path))
        entries: list[tuple[str, str]] = (
            [(s.name, s.signature) for s in symbols] if symbols else [("", "")]
        )
        for name, signature in entries:
            # 签名本身含名字（`format_duration(seconds: int) -> str`），所以**优先用签名**：
            # 早先写成 `f"{name}{signature}"` 会在节点里印出 `format_durationformat_duration(…)`，
            # 真机那轮的工作模型自己把这处粘连当笔误点了出来（会话 d607bd4339b04c91，轮 2）。
            label = f"{path}::{signature or name}" if name else path
            anchor: dict[str, str] = {"path": path}
            if name:
                anchor["symbol"] = name
            payload = {
                "id": nxt(),
                "statement": f"写入 {label}{tail}",
                "anchor": anchor,
                "node_kind": "act",
                "salience": ACT_SALIENCE,
            }
            if targets:
                # 第一个新节点接住全部旧节点的 SUPERSEDE，其余旧节点在循环里各自被取代。
                ops.append(DeltaOp(op="SUPERSEDE_FACT", target=targets.pop(0), payload=payload))
            else:
                ops.append(DeltaOp(op="ADD_FACT", payload=payload))
        # 旧节点多于新符号（文件里的符号被删掉了）：剩下的旧节点同样要作废。
        ops.extend(DeltaOp(op="INVALIDATE_FACT", target=t) for t in targets)
    return ops


def _symbols_for(path: str, source: str | None) -> list[SymbolInfo]:
    """`.py` 的锚点符号 = AST 顶层公开符号（带签名）；非 .py 或解析失败返回空。"""
    if not path.endswith(".py") or not source:
        return []
    tree = parse_source(source)
    return module_level_public_defs(tree) if tree is not None else []


def think_anchor_paths(prior_state: CompressedState, written: Sequence[tuple[str, str, int]]) -> set[str]:
    """THINK 节点允许挂的路径集合 = 真实存在过 ACT 的路径 ∪ 本轮真的写出去的路径。

    "无锚不留"的可执行版本：一条推理如果连一个真实动作都挂不上，它就不是工作记忆，
    是设想 —— 丢进 SET_UNRESOLVED，不进链。
    """
    paths = {p for p, _, _ in written}
    for f in prior_state.fact_chain:
        if f.node_kind == "act" and f.anchor is not None:
            paths.add(f.anchor.path)
    return paths


def _norm_rel(path: str) -> str:
    """路径归一化：反斜杠→正斜杠、剥掉前导 `./`、去掉尾部斜杠（大小写留给比较时处理）。"""
    s = str(path or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def path_matches(anchor: str, allowed: Sequence[str]) -> bool:
    """锚点算不算「真实动作里出现过」。

    真机事故：DSH 传的路径是绝对的（`D:\\_playground\\workspace\\oneline\\out\\x.png`），
    模型写的 anchor 是相对的（`oneline/out/x.png`）—— 字符串不相等就被判「编路径」，
    一整块思考 27 条节点全丢、`kept=0`。所以比较必须归一化，并允许**后缀**匹配：
    同一根下的同一段后缀，就是同一个文件。
    """
    a = _norm_rel(anchor).lower()
    if not a:
        return False
    for cand in allowed:
        c = _norm_rel(cand).lower()
        if not c:
            continue
        if a == c or c.endswith("/" + a) or a.endswith("/" + c):
            return True
    return False


def build_tool_error_ops(
    *,
    prior_state: CompressedState,
    errors: Sequence[tuple[str, int, str]],
    reserved_ids: Sequence[str] = (),
    max_nodes: int = 2,
) -> list[DeltaOp]:
    """工具**报错**进链：一条失败链条目 + 一条 `[否掉]` 负节点（零模型调用）。

    真机事故：`read_image` 每一步都返回同一句
    `Error: ... model "pciso" does not declare image input`，但这句只活在当轮的消息里 ——
    一被吸收就没了。模型没有现实回执可读，只能一遍遍写「我这就去读图」，转了一百多圈。
    报错必须是链上的**长期事实**：`errors` 是 `(调用标签, 连续次数, 最近一次报错原文)`。
    """
    picker = _id_picker(prior_state, reserved_ids)
    ops: list[DeltaOp] = []
    for label, count, last in list(errors)[:max_nodes]:
        reason = f"{label} 连续 {count} 次报错：{(last or '').strip()[:160] or '（无正文）'}"
        ops.append(
            DeltaOp(
                op="ADD_FAILURE",
                payload={"attempt": f"调用 {label}", "reason": reason, "source": "tool"},
            )
        )
        ops.append(
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "id": picker(),
                    "statement": f"不再调用 {label}（连续 {count} 次报错，这条路当前不可用）",
                    "node_kind": "think",
                    "polarity": "-",
                    "assertion": "settled",
                    "why": reason,
                    "salience": 85,
                },
            )
        )
    return ops


def filter_think_ops(
    ops: Sequence[DeltaOp],
    *,
    allowed_paths: Sequence[str],
    source: str = "",
    constraint_texts: Sequence[str] = (),
) -> tuple[list[DeltaOp], dict[str, int]]:
    """服务端硬规则：**不编锚点** + **禁完成态断言** + **未定/风险/克制必须可溯源**。

    返回 (保留的 ops, 计数)。计数进 telemetry —— 丢了几条、为什么丢，必须能在事后数出来。

    `source` 是这段节点对应的原文（thinking 块）。传空串 = 不做溯源检查（老调用方照旧）。
    `constraint_texts` 是 P 的约束原文：负节点若只是把它们复述一遍就丢掉（P 每轮都在）。
    """
    allowed = set(allowed_paths)
    kept: list[DeltaOp] = []
    stats = {
        "think_kept": 0,
        "think_dropped_unanchored": 0,
        "think_anchorless": 0,
        "think_dropped_claim": 0,
        "think_dropped_meta": 0,
        # 下面两条是**度量**（0 也要出现在 telemetry 里）：缺席和"这一轮是 0"是两件事，
        # 报告要能区分"没记"和"没发生"。
        "think_anchor_file_level": 0,
        "think_vague": 0,
        "think_open": 0,
        "think_negative_constraint": 0,
        "think_risk": 0,
        "think_ungrounded": 0,
        "think_dropped_ungrounded": 0,
        # 服务端**代模型改标签**的次数（第八轮）：这两个数回答"提示词里的规则 10/11
        # 到底有没有生效" —— 实测 0，于是规则从提示词下沉到服务端。
        "think_negative_clause": 0,
        "think_forced_negative": 0,
        "think_forced_open": 0,
        # 准入判据（negatives 只留"推不出来"的）：复述 P 的负节点丢掉要能数出来。
        "think_dropped_p_restated": 0,
    }
    for op in ops:
        payload = dict(op.payload or {})
        if not (op.op in ("ADD_FACT", "SUPERSEDE_FACT") and payload.get("node_kind") == "think"):
            kept.append(op)
            continue
        statement = str(payload.get("statement") or "")
        if COMPLETION_CLAIM_RE.search(statement) or COMPLETION_CLAIM_RE.search(
            str(payload.get("why") or "")
        ):
            stats["think_dropped_claim"] += 1
            continue
        if _META_REF_RE.search(statement):
            # 议论别的节点 = 元评论。deps 字段才是节点间依赖的合法通道。
            stats["think_dropped_meta"] += 1
            continue
        anchor = payload.get("anchor") or {}
        path = str(anchor.get("path") or "") if isinstance(anchor, dict) else ""
        if path and not path_matches(path, allowed):
            # **编路径**才是要拦的：那等于声称动过一个不存在的产物。
            stats["think_dropped_unanchored"] += 1
            continue
        if not path:
            # 没锚点**不**丢节点（旧规则"无锚不留"在这里是错的，真机实测）：纯推理
            # ——不碰文件的决策、权衡、风险——本来就没有文件可挂。旧规则把一整块思考的
            # 12 条节点全丢了，紧凑版为空 → 回退原文 → IoT 机制等于没开。
            # 留下它，但标记 `unanchored`（由 merge 按"有没有 anchor"自己算）：
            # C 装不下时 `_trim_to_budget` 会**先丢**这些没有物证的节点。
            stats["think_anchorless"] = stats.get("think_anchorless", 0) + 1
            payload.pop("anchor", None)
        pol = str(payload.get("polarity") or "")
        if pol not in ("+", "-"):
            # 极性缺失不丢节点（宁可当正节点），但要在计数里留痕。
            payload["polarity"] = "+"
            stats["polarity_missing"] = stats.get("polarity_missing", 0) + 1
        # 克制（"决定不做 X"）的极性也由**服务端**判。提示词规则 10 要求这类节点标
        # `polarity="-"`；真机里模型给的是缺省正极性，而"负节点保底"这条裁剪规则
        # 只认 `polarity` —— 标错不丢节点，但会在 C 装不下时**第一个被裁掉**，
        # 而负节点恰恰是长程任务里最贵的事实（它拦住"重走已否掉的路"）。
        negative_clause = bool(
            _NEGATIVE_CLAUSE_RE.search(statement)
            or _NEGATIVE_CLAUSE_RE.search(str(payload.get("why") or ""))
        )
        if negative_clause:
            stats["think_negative_clause"] += 1
            # 判"是不是纯粹一条否决"要把否定词先摘掉：`不新建 X` 里的"新建"不是正面主张。
            rest = _NEGATIVE_CLAUSE_RE.sub("", statement + " " + str(payload.get("why") or ""))
            if payload["polarity"] != "-" and not _POSITIVE_CLAIM_RE.search(rest):
                payload["polarity"] = "-"
                stats["think_forced_negative"] += 1
        payload.setdefault("salience", 75 if payload["polarity"] == "-" else 70)
        # 已定 / 未定：服务端只认字面 `open`（其余当 settled，不丢节点）。
        is_open = str(payload.get("assertion") or "").strip().lower() == "open"
        payload["assertion"] = "open" if is_open else "settled"
        if not is_open and payload["polarity"] != "-" and is_uncertain_about(statement, source):
            # 原文在这条节点所谈的具体物附近明说了"还没定"，节点却写成已定 ——
            # 服务端自己降级（不丢节点：内容还在，只是不再冒充结论）。
            #
            # 负节点不降级：一条否决**本身就是一个决定**。真机实例（会话 7b3e5f7c935f49b4）：
            # "不加 CLI""不新建 __init__.py""不采用双路导入"三条已定的克制全被标成
            # `⟨THINK -⟩ ⟨待定⟩`（原文只是讨论过"要不要加"，最后明确不加）——
            # 下一轮读到"待定"，会以为这件事还没定、可以随时翻案。
            payload["assertion"] = "open"
            is_open = True
            stats["think_forced_open"] += 1
        if is_open:
            stats["think_open"] += 1
        if str(payload.get("risk") or "").strip():
            stats["think_risk"] += 1
        if negative_clause or (
            payload["polarity"] == "-" and NEGATIVE_CONSTRAINT_RE.search(statement)
        ):
            stats["think_negative_constraint"] += 1
            # 提示词规则 10 要求这类节点给 80 以上 —— 服务端把这条要求变成**地板**，
            # 而不是指望模型自己记住（纪律 vs 机制：真机里纪律那一路从来没全对过）。
            # 含克制条款的混合节点也上地板：它同样不该在 C 装不下时先被裁掉。
            payload["salience"] = max(int(payload.get("salience") or 0), 80)
        # 只压不补：`未定 / 风险 / 克制`在原文里找不到出处就不留 —— 编出来的"上一轮的犹豫"
        # 比缺一条节点更糟（它会被下一轮当成真的背景）。普通正节点只计数。
        if source:
            grounded = is_grounded(statement, source) and is_grounded(
                str(payload.get("why") or ""), source
            )
            if not grounded:
                stats["think_ungrounded"] += 1
                declared = (
                    payload["polarity"] == "-"
                    or payload.get("assertion") == "open"
                    or bool(str(payload.get("risk") or "").strip())
                )
                if declared:
                    stats["think_dropped_ungrounded"] += 1
                    continue
        if payload["polarity"] == "-" and is_p_restatement(statement, constraint_texts):
            # 只说了一遍 P 里已经在的话（"不修改冻结测试"）—— 推得出来的信息不进 C。
            # 只对负节点做：正节点复述 P 罕见，且丢正节点的信息损失不对称。
            stats["think_dropped_p_restated"] += 1
            continue
        if not str(anchor.get("symbol") or ""):
            # 文件级锚点（`path::` 后面空着）：验证器只能确认文件在、不能确认符号在。
            # 不丢节点（很多推理确实只到文件粒度），但要能数出来 —— 真机自述里
            # 模型自己把这条列为"验证器无法据此确认"的缺口（会话 aae79de300504908）。
            stats["think_anchor_file_level"] = stats.get("think_anchor_file_level", 0) + 1
        if not is_concrete(statement):
            stats["think_vague"] = stats.get("think_vague", 0) + 1
        kept.append(DeltaOp(op=op.op, target=op.target, payload=payload))
        stats["think_kept"] += 1
    return kept, stats


# "只给类别不给具体值"的探针（真机自述第 2 条：节点写"覆盖边界情况"，
# 事后没法判断到底覆盖了什么）。判据是"句子里有没有具体物"：数字、标识符、`::`、括号形态。
# 这是**度量不是判定**：纯中文命题（"选胶囊距离场画笔画"）也会被计进来 ——
# 它只想回答"这一轮有多少条读起来无法核对"，不做阻断，不进 hard check。
_CONCRETE_RE = re.compile(r"\d|[A-Za-z_][A-Za-z0-9_]{3,}|`|\(")


def is_concrete(statement: str) -> bool:
    return bool(_CONCRETE_RE.search(statement))


# "只压不补"的可执行近似：节点里出现的**具体物**（标识符 / 数字）至少在原文里出现过一次。
# 来源是真机自述（会话 50693faf271b4fdd，轮 2）：那一轮的 thinking 只有 388 字、
# 完全没提 `__init__.py` 也没提 `bool`/`1.5`，编排器却写出一条"不新建 tools/__init__.py"
# 和一条"布尔值/1.5 的处置待定"——**把不属于上一轮的态度写成了它的态度**。
# 模型的原话："原文的摇摆被固化成了决定，原文没有的摇摆被标成了待定。这个错位比单纯丢失更值得修。"
# 于是：`未定 / 风险 / 克制`这三类节点必须先过"原文里真有这件事"这一关，普通正节点只计数不丢。
_CONCRETE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d+(?:\.\d+)?")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _grounding_tokens(text: str) -> list[str]:
    """可比对的具体物：ASCII 标识符/数字 + 中文二元组（中文没有空格，二元组是最省的近似）。"""
    out = _CONCRETE_TOKEN_RE.findall(text)
    for run in _CJK_RUN_RE.findall(text):
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def is_grounded(statement: str, source: str) -> bool:
    """节点里的具体物有没有在原文里露过面。没有具体物的句子无从判"编"，一律算有据。"""
    tokens = _grounding_tokens(statement)
    if not tokens:
        return True
    return any(t in source for t in tokens)


OVERVIEW_MAX_CHARS = 120


def one_line_overview(thinking: str, *, limit: int = OVERVIEW_MAX_CHARS) -> str:
    """从原始 thinking 里抽一句当**概述**（服务端兜底，零模型调用）。

    为什么要它：编排器对短块常常返回 `{"ops": []}`（"没什么值得留的"），于是这一块就没有
    紧凑版 —— 客户端要么看到原文、要么什么都看不到。块级替换的语义是"客户端看到的那块就是
    紧凑版"，所以每块至少要有一行；这一行由服务端从原文里抽，不额外花钱、也绝不把原文整段发出去。
    """

    text = " ".join((thinking or "").split())
    if not text:
        return ""
    piece = text
    for match in _SENT_END_RE.finditer(text):
        candidate = text[: match.end()].strip()
        if len(candidate) >= 12:
            piece = candidate
            break
    if len(piece) <= limit:
        return piece
    return piece[:limit].rstrip() + "…"


def item_line(node: FactNode) -> str:
    """一条节点在**客户端面板**里的读法（块级替换后的那一行）。

    这不是 C 的记账格式（C 里照旧用 `⟨THINK +|-⟩` / salience / why 那套）。极性只在**这一行
    自己说不清**的时候才标出来：

    * 正向节点**不挂前缀** —— 它就是"这么定了"，挂 `[THINK +]` 只是把内部字段摊给人看；
    * 被否掉的路线挂 `[否掉]` —— 这是最需要被下一轮看见的一类（别再走一遍）；
    * 还没定的事挂 `[待定]`（`assertion == "open"`）；
    * 动作（物证）挂 `[落地]`。

    刻意**不用** `√` / `×` 这类符号：`√` 会被读成"已验证 / 已完成"，正好撞上我们明令禁止的
    完成态断言；`×` 会读成"这条是错的"，而它真正的意思是"这条路我没走"。这行文本既要给人看，
    也会随历史回灌给模型，所以宁可多两个字也要自述清楚。
    """

    if node.node_kind == "act":
        return f"[落地] {node.statement}"
    if node.node_kind == "think":
        if node.polarity == "-":
            return f"[否掉] {node.statement}"
        if node.assertion == "open":
            return f"[待定] {node.statement}"
    return node.statement


def is_p_restatement(statement: str, constraint_texts: Sequence[str]) -> bool:
    """这条负节点是不是把 P 里现成的约束又说了一遍。

    用户给的准入判据（原话）："重要 = 下一轮会因为它走错 / 重走贵路，而且从 P、盘上的文件、
    C 里其它节点都推不出来。" P 每轮**逐字**在 prompt 里，所以「不修改 tests/frozen」
    「不加 TODO」这类写进 C 只是白占预算；而「不新建 tools/__init__.py」具体到本轮产物，
    照旧要留。判据＝节点里的具体物有 ≥60% 落在同一条约束文本里（纯中文件靠二元组近似）。
    """
    toks = {t for t in _grounding_tokens(statement) if len(t) >= 2}
    if not toks:
        return False
    for c in constraint_texts:
        ct = set(_grounding_tokens(str(c)))
        if not ct:
            continue
        if len(toks & ct) / len(toks) >= 0.6:
            return True
    return False


_SENT_END_RE = re.compile(r"[。！？!?\n]")


def _sentence_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END_RE.finditer(source):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(source):
        spans.append((start, len(source)))
    return spans


def _uncertain_clusters(source: str) -> list[tuple[int, int]]:
    """每个"没定"信号的**话题簇**：它所在那句 + 前面两句。"""
    sents = _sentence_spans(source)
    out: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(sents):
        if not UNCERTAINTY_RE.search(source[a:b]):
            continue
        start = a
        for j in (i - 1, i - 2):
            if j >= 0:
                start = sents[j][0]
        out.append((max(start, b - _CLUSTER_CHARS), b))
    return out


def _present(token: str, text: str) -> bool:
    """整词匹配：短 token（`int`/`type`）不许在 `print`/`TypeError` 里"顺带命中"。"""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text) is not None


def is_uncertain_about(statement: str, source: str) -> bool:
    """这条节点谈的具体物，是不是落在原文某个"还没定"的话题簇里。

    判据要求 **两个共享具体物**，或者**一个够长的标识符**（≥5 字符，`format_seconds`/`TypeError`
    这种）：只共享一个 `int`/`type` 这种通用词就降级，会把半段原文的节点全标成待定 ——
    第一版就是这样一轮降级 6 条的。宁可漏标成已定，也不要满屏 ⟨待定⟩。
    """
    if not source:
        return False
    clusters = _uncertain_clusters(source)
    if not clusters:
        return False
    text = " ".join(source[a:b] for a, b in clusters)
    hits = [t for t in _CONCRETE_TOKEN_RE.findall(statement) if len(t) >= 2 and _present(t, text)]
    if len(hits) >= 2:
        return True
    return any(len(t) >= 5 for t in hits)


# 原文里的"二选一"（`A 或 B` / `A 还是 B`）。来源：真机自述连续两轮把**被否掉的备选**
# 列为自己丢掉的第二类信息（`format_seconds` 这个名字、"否掉的路线"）。
# 这不是模型能自觉记的（它只记结论），但判据在原文里是可验证的：
# A 出现在真的写出去的文件正文里、B 在任何文件正文与已有节点里都没出现 —— 那就是"选了 A、没选 B"。
_ALT_PAIR_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]{3,})\s*(?:还是|或者|或|vs\.?)\s*([A-Za-z_][A-Za-z0-9_]{3,})"
)
# 第三种形态（第十一轮）：散文里写出来的方案对比 ——「用 unittest，而不是 pytest」。
# 判据仍是可验证的：被选中的那个出现在落盘正文里、被否的那个不在。
_ALT_PROSE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]{3,})\s*(?:，)?\s*(?:而不是|而非|代替)\s*([A-Za-z_][A-Za-z0-9_]{3,})"
)


def build_alternative_ops(
    *,
    prior_state: CompressedState,
    sources: Mapping[str, str] | None = None,
    thinking: str = "",
    reserved_ids: Sequence[str] = (),
    known_extra: str = "",
    max_nodes: int = 2,
) -> list[DeltaOp]:
    """被否掉的备选 → 负节点（server 从原文逐字取，不生成新内容）。

    只在**备选对**上做，而且被否的那个名字一旦在别处被提过就跳过（说明已经有节点在管它）。
    `known_extra` 装**同一批模型 ops 的正文** —— 它们还没进 `prior_state`（同一个 ΔC 里排在后面才 apply），
    不带上就会为"N7 已经写过的 pytest"再补一条重复的负节点。
    """
    src_text = dict(sources or {})
    if not thinking.strip() or not src_text:
        return []
    known = " ".join(
        [str(f.statement) for f in prior_state.fact_chain]
        + [str(f.reason) for f in prior_state.failure_chain]
        + [known_extra]
    )
    nxt = _id_picker(prior_state, reserved_ids)
    ops: list[DeltaOp] = []
    seen: set[str] = set()
    pairs = [(m.group(1), m.group(2)) for m in _ALT_PAIR_RE.finditer(thinking)]
    pairs += [(m.group(1), m.group(2)) for m in _ALT_PROSE_RE.finditer(thinking)]
    for left, right in pairs:
        if len(ops) >= max_nodes:
            break
        if left == right:
            continue
        for chosen, rejected in ((left, right), (right, left)):
            if rejected in seen:
                continue
            # 选中的那个必须真的出现在某个写出去的文件正文里 —— 否则这条配对与"这一轮干了什么"无关。
            owner = next((p for p in sorted(src_text) if chosen in src_text[p]), None)
            if owner is None:
                continue
            if any(rejected in text for text in src_text.values()):
                continue
            if rejected in known:
                continue
            seen.add(rejected)
            ops.append(
                DeltaOp(
                    op="ADD_FACT",
                    payload={
                        "id": nxt(),
                        "statement": f"不再采用备选名 {rejected}（本轮选了 {chosen}，已经落进 {owner}）",
                        "anchor": {"path": owner},
                        "node_kind": "think",
                        "polarity": "-",
                        "assertion": "settled",
                        "salience": 80,
                        # 服务端生成的负节点必须自带依据：只写"未采用 X"读起来是历史陈述，
                        # 不是禁止项，下一轮就没有"别再选它"的约束力（真机自述第 2、3 点）。
                        # 依据引原文那句，别只写机制结论（"比对落盘结果"补不上"为什么否"）。
                        "why": f"原文里 {chosen} 与 {rejected} 并列，最终落盘的 {owner} 里只有 {chosen}",
                    },
                )
            )
            break
    return ops


# 草稿里出现过的 import 行（只认行首就是 import/from 的那种，散文里的"依赖 pytest"不算）。
_IMPORT_LINE_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]+|import[ \t]+)([A-Za-z_][\w., \t]*)$",
    re.MULTILINE,
)

# 草稿里的 try/except 降级导入（第三种形态用）。上限 6 行是为了不跨过一整段代码去乱配。
_TRY_FALLBACK_RE = re.compile(
    r"try\s*:[^\n]*\n(?:[^\n]*\n){0,6}?[ \t]*except[^\n]{0,60}?(?:ModuleNotFoundError|ImportError)"
)
# 落盘文件里的同一结构（有就不算"被否"）。
_LANDED_FALLBACK_RE = re.compile(r"except[^\n]{0,60}?(?:ModuleNotFoundError|ImportError)")

# 第二种"可选结构"：`if __name__ == "__main__":` 入口块。真机原文写的是
# "最好不要加 __main__ 功能，只做工具"（工具文件不加、测试文件加），落盘的 duration.py 里确实没有。
_MAIN_GUARD_RE = re.compile(r"__name__\s*==\s*[\"']__main__[\"']")
# 散文里提到（不一定贴代码块）的 try/except 兜底导入 —— 真机实例（会话 da5f070d64014d7b）：
# 原文只写了一句"还把 `try/except ImportError` 这个方案明确否了（'那更啰嗦'）"，
# 没贴代码块，于是老的正则认不出来，模型自述把它列为"方案层面的排除丢了两条"之一。
_TRY_MENTION_RE = re.compile(
    r"try\s*(?:/|…|\.\.\.|\s+)?\s*except[^\n]{0,40}?(?:ImportError|ModuleNotFoundError|导入)"
)
_TRY_ANY_RE = re.compile(f"(?:{_TRY_FALLBACK_RE.pattern})|(?:{_TRY_MENTION_RE.pattern})")
# "试过又放弃的结构"登记表：名字 → (草稿里的形状, 落盘里的同一形状, 已记账时用来判重的标记词)。
# 标记词只认这个结构的**特征串**，不认整条 label：模型自己写的负节点正文是
# "不加 CLI / __main__ 入口（…）"，整条 label 不在里面，只有 `__main__` 在 ——
# 用整条 label 判重会漏，于是同一件事被记成两条（真机自述："N4 和 N12 是同一件事拆成两条"）。
_REJECTED_ROUTES: tuple[tuple[str, re.Pattern[str], re.Pattern[str], str], ...] = (
    ("try/except 兜底导入", _TRY_ANY_RE, _LANDED_FALLBACK_RE, "except"),
    ('if __name__ == "__main__" 入口块', _MAIN_GUARD_RE, _MAIN_GUARD_RE, "__main__"),
)
_ROUTE_MENTION_CHARS = 250
# 给"草稿 import"找归属文件时往前看的窗口：草稿通常按文件顺序贴，import 前面最近的
# 那个文件名才是它所属的文件（真机实例：`import os` 写在 test_duration.py 的中间稿里，
# 却因为"排序第一个没落的文件"被锚到了 duration.py）。
_IMPORT_OWNER_CHARS = 2000
# "识破但先搁置"的风险（真机自述第 2 条：「原文有一句"如果别处也有 tools 包会不会冲突……
# 不管了"。这是**已知风险 + 主动搁置**，两个信息量都在。节点里完全没有」）。
# 只在原文**明确写了搁置**时才造节点，别把每一个问句都变成风险。
_SHELVED_RE = re.compile(
    r"不管了|先不管|暂且不管|搁置|先放一放|以后再说|后面再说|留待以后|回头再说|先这样吧"
)


def _modules_of(m: re.Match[str]) -> list[str]:
    """一行草稿 import 里的模块名（`from a import b` 取 a；`import a, b as c` 取 a、b）。"""
    if m.group(1):
        return [m.group(1)]
    return [p.strip().split(" as ")[0].strip() for p in (m.group(2) or "").split(",") if p.strip()]


def _drafted_imports(text: str) -> set[str]:
    mods: set[str] = set()
    for m in _IMPORT_LINE_RE.finditer(text):
        mods.update(_modules_of(m))
    return {m for m in mods if m}


def _landed_imports(source: str) -> set[str]:
    tree = parse_source(source)
    if tree is None:
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def build_dropped_import_ops(
    *,
    prior_state: CompressedState,
    sources: Mapping[str, str] | None = None,
    thinking: str = "",
    reserved_ids: Sequence[str] = (),
    known_extra: str = "",
    max_nodes: int = 2,
) -> list[DeltaOp]:
    """草稿里 import 过、最终写出去的文件里没有的依赖 → 负节点。

    这是"被否掉的具体物"里**唯一可完全验证**的一类：原文写了 `from __future__ import annotations`，
    最终 `tools/duration.py` 里没有 —— 那就是"决定不加"。真机自述把"不引某个依赖"列为
    低字数高风险的克制，而提示词规则 10 那一轮漏掉了它（会话 ba902da26e6c4b20 的原文 L118）。
    锚点用**本轮写出去的 .py**：被砍的 import 一定属于这些文件之一。归属优先按
    "这条 import 在原文里往前最近提到的文件"判（草稿通常按文件顺序贴），推不出来
    才退回"排序第一个没落的文件"（近似）。
    """
    py = {p: t for p, t in dict(sources or {}).items() if p.endswith(".py")}
    if not py or not thinking.strip():
        return []
    drafted = _drafted_imports(thinking)
    if not drafted:
        return []
    landed: set[str] = set()
    for text in py.values():
        landed |= _landed_imports(text)
    # 同一个模块被"换了个写法导入"不算被砍：真机反例（会话 5f24337359ac4c39 的 N9）
    # 草稿里写过 `from tools.duration import format_duration`，最终文件里是
    # `from duration import format_duration` —— 同一个模块，尾段相同，服务端却报
    # "不再引入 tools.duration 这个依赖"。尾段相同就放过，宁可少一条也别造假的。
    landed_tails = {m.rsplit(".", 1)[-1] for m in landed}
    dropped = sorted(
        m
        for m in drafted - landed
        if m not in known_extra and m.rsplit(".", 1)[-1] not in landed_tails
    )
    if not dropped:
        return []
    seen_at = _drafted_import_positions(thinking)
    nxt = _id_picker(prior_state, reserved_ids)
    ops: list[DeltaOp] = []
    for mod in dropped[:max_nodes]:
        # 归属文件优先按"这条 import 在原文里往前最近提到的文件"判（准），
        # 推不出来时退回"排序第一个没落的文件"（近似）。
        pos = seen_at.get(mod, 0)
        owner = (
            _owner_near(thinking, pos, sorted(py))
            or next((p for p in sorted(py) if mod not in _landed_imports(py[p])), sorted(py)[0])
        )
        quote = _source_quote(thinking, pos, len(mod))
        ops.append(
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "id": nxt(),
                    "statement": f"不再引入 {mod} 这个依赖（本轮决定不用它，别再加回来）",
                    "anchor": {"path": owner},
                    "node_kind": "think",
                    "polarity": "-",
                    "assertion": "settled",
                    "salience": 80,
                    # 依据必须是**原文那句**：只写"服务端比对落盘结果"等于机制替推理做总结，
                    # 补上了"没出现"，补不上"当时为什么否"（真机自述第 2 条）。
                    "why": f"原文：{quote}｜服务端比对：落盘的 {owner} 里没有 {mod}"
                    if quote
                    else f"草稿里出现过 {mod}，最终落盘的 {owner} 的 import 里没有（服务端比对落盘结果）",
                },
            )
        )
    return ops


def _source_quote(thinking: str, pos: int, span: int, *, limit: int = 150) -> str:
    """命中处前后的原文片段（逐字、压掉换行），给服务端补的节点当依据。

    这里**不做句子切分**：草稿 import 的理由通常紧跟在它后面那句
    （"`from __future__ import annotations` … 它是 future，不必要"），
    只取"前一句"会把理由切掉。
    """
    if not thinking:
        return ""
    end = min(len(thinking), max(0, pos) + max(1, span))
    lo = max(0, max(0, pos) - 90)
    hi = min(len(thinking), end + 120)
    return " ".join(thinking[lo:hi].split())[:limit]


def _drafted_import_positions(thinking: str) -> dict[str, int]:
    """草稿 import → 它在原文里**第一次**出现的位置（用来找归属文件）。"""
    out: dict[str, int] = {}
    for m in _IMPORT_LINE_RE.finditer(thinking):
        for mod in _modules_of(m):
            out.setdefault(mod, m.start())
    return out


def _owner_near(thinking: str, pos: int, paths: Sequence[str], *, window: int = _IMPORT_OWNER_CHARS) -> str:
    """`pos` 往前最近提到的落盘文件；都没有就返回空串。

    只认**带扩展名的文件名**（`duration.py`），不认裸主干名 —— 裸名会被别处的
    `from duration import …` 这类本模块导入命中，把归属带偏（真机实测：测试文件末尾的
    入口块因此被算成 `tools/duration.py` 的）。
    """
    lo = max(0, pos - window)
    head = thinking[lo:pos]
    best: tuple[int, str] | None = None
    for path in paths:
        stem = Path(path).stem
        pat = rf"(?<![A-Za-z0-9_.]){re.escape(stem)}\.[A-Za-z0-9]+"
        for m in re.finditer(pat, head):
            if best is None or m.start() > best[0]:
                best = (m.start(), path)
    return best[1] if best else ""


def build_rejected_route_ops(
    *,
    prior_state: CompressedState,
    sources: Mapping[str, str] | None = None,
    thinking: str = "",
    reserved_ids: Sequence[str] = (),
    known_extra: str = "",
    max_nodes: int = 2,
) -> list[DeltaOp]:
    """草稿里试过的**代码结构**、落盘里没有 → 负节点（"被否掉的路线"的第三种形态）。

    真机实例（会话 `d8bf4b45a8db4827`，think-eval10）：原文写过 `try: ... except
    ModuleNotFoundError: ...` 的降级导入，最后选了"先插 `sys.path` 再直接 import"；
    同轮还写过 `if __name__ == "__main__":` 又自己否掉（"最好不要加 __main__ 功能"）。
    模型自述把这两条列为可惜的丢失，而前两种形态（`A 或 B` 标识符对、草稿 import）
    都认不出它们 —— 它们不是名字对，是**结构**。

    可完全验证：落盘的 .py 里有没有同样的结构，正则就能查。**归属先定、再查落盘**：
    每条命中往前找最近被提到的**带扩展名的文件名**，那个文件就是这条结构所属的文件；
    只有这个文件里真的没有该结构时，才算"被试过又放弃"。归属判不出来（一个文件名都没提，
    且本轮写了不止一个文件）就不记 —— 这条节点的全部价值都在"别再加回来"那个指针上，
    锚错文件比不记更糟（真机实例：会话 `5f24337359ac4c39` 的 N11，入口块明明写进了
    测试文件，却因为"差集里只剩 duration.py"被记成"不再给 duration.py 加入口块"）。
    """
    py = {p: t for p, t in dict(sources or {}).items() if p.endswith(".py")}
    if not py or not thinking.strip():
        return []
    nxt = _id_picker(prior_state, reserved_ids)
    ops: list[DeltaOp] = []
    for label, draft_re, landed_re, marker in _REJECTED_ROUTES:
        if len(ops) >= max_nodes:
            break
        hits = list(draft_re.finditer(thinking))
        if not hits:
            continue
        if marker in known_extra:
            continue
        owner = ""
        for h in hits:
            who = _owner_near(thinking, h.start(), sorted(py))
            if not who and len(py) == 1:
                # 本轮只写了一个文件：不用点名也知道这条结构在说谁。
                who = next(iter(py))
            if who and not landed_re.search(py[who]):
                owner = who
                break
        if not owner:
            continue
        ops.append(
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "id": nxt(),
                    "statement": f"不再给 {owner} 加 {label}（草稿里提过又否掉，本轮落盘文件里没有）",
                    "anchor": {"path": owner},
                    "node_kind": "think",
                    "polarity": "-",
                    "assertion": "settled",
                    "salience": 80,
                    "why": f"原文：{quote}｜服务端比对：落盘的 {owner} 里没有这个结构"
                    if (quote := _source_quote(thinking, hits[0].start(), 0))
                    else f"原文草稿里出现过这个结构，落盘的 {owner} 里没有（服务端比对落盘结果）",
                },
            )
        )
    return ops


def build_shelved_risk_ops(
    *,
    prior_state: CompressedState,
    sources: Mapping[str, str] | None = None,
    thinking: str = "",
    reserved_ids: Sequence[str] = (),
    known_extra: str = "",
    max_nodes: int = 1,
) -> list[DeltaOp]:
    """原文里"识破了一个风险、然后决定先不管"的地方 → 一条带 `risk` 的 ⟨待定⟩ 节点。

    真机自述（会话 `da5f070d64014d7b`，第 2 条）：「原文有一句"如果别处也有 tools 包会不会
    冲突……不管了"。这是**已知风险 + 主动搁置**，两个信息量都在。节点里完全没有。」
    —— 它把这条列为"值钱"，因为下一轮导入真出问题时，这条是"第一嫌疑"，C 里却没线索。

    与 `assertion="open"` 的区别照提示词规则 12：**决定已经定了**（先不管），
    悬着的是附加条件 —— 所以 `risk` 装那句话，`assertion` 保持 settled 语义之外的
    `open` 会更含糊，这里用 `risk` + `salience 75`，statement 写明"先搁置"。
    只在原文**逐字出现搁置措辞**时才造节点；没有具体物（文件名/标识符/数字）的不记。
    """
    py = {p: t for p, t in dict(sources or {}).items() if p.endswith(".py")}
    hits = list(_SHELVED_RE.finditer(thinking or ""))
    if not hits:
        return []
    nxt = _id_picker(prior_state, reserved_ids)
    ops: list[DeltaOp] = []
    for m in hits:
        if len(ops) >= max_nodes:
            break
        clause = _clause_around(thinking, m.start(), m.end())
        if not clause:
            continue
        owner = _owner_near(thinking, m.start(), sorted(py))
        tokens = _grounding_tokens(clause)
        # 要有**具体物**：标识符/数字（`tools`、`from tools.duration`）或点得出文件名。
        # "嗯，还有些细节，后面再说吧"这种没有对象的搁置不记 —— 记了只是噪声。
        if not any(t.isascii() for t in tokens) and not owner:
            continue
        if sum(1 for t in tokens if t in known_extra) >= 2:
            continue
        if not owner:
            if len(py) != 1:
                continue
            owner = next(iter(py))
        ops.append(
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "id": nxt(),
                    "statement": f"已知风险、本轮先搁置：{clause}（没解决，别当成处理过了）",
                    "anchor": {"path": owner},
                    "node_kind": "think",
                    "polarity": "+",
                    "salience": 75,
                    "risk": clause,
                    "why": "原文里识破之后明确写了先不管（服务端从原文摘出，逐字保留）",
                },
            )
        )
    return ops


def _clause_around(text: str, start: int, end: int, *, width: int = 90) -> str:
    """取搁置标记所在的**那一句 + 前一句**（前一句通常才是被搁置的问题本身）。

    "如果别处也有 `tools` 包会不会冲突。不管了" —— 只取标记那句就只剩"不管了"，
    风险的对象丢了；所以往前多要一句，再去掉代码围栏的碎片。
    """
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    head = text[lo:start]
    tail = text[end:hi]
    cuts = [i for i, ch in enumerate(head) if ch in "。！？!?\n"]
    # 只有一个句末标点（而且就在标记前面）时，前面整段都算"上一句"，别把它切掉。
    left = cuts[-2] if len(cuts) >= 2 else -1
    right = min((i for i in (tail.find(ch) for ch in "。！？!?\n") if i >= 0), default=len(tail))
    clause = (head[left + 1 :] + text[start:end] + tail[: right + 1]).strip()
    return " ".join(clause.split())[:200]


# 【未解决】里**已经自带结论**的条目：原文自己问过、自己答完了（"要不要 X？→ 不加/未新建"）。
# 这类条目留在【未解决】里，下一轮会把它们当悬案去"确认" —— 真机自述第 1 条就是这个：
# C 里挂着"确认 tools/ 是否需要 __init__.py：当前按命名空间包处理，未新建"，
# 而模型上一轮已经决定"只要两个文件"，它读到那条悬案后怕自己"顺手把 __init__.py 加上，正好违反 G1"。
_ANSWERED_UNRESOLVED_RE = re.compile(
    r"未新建|未创建|未添加|未引入|未采用|未使用|不加|不引入|不采用|不使用|不依赖"
    r"|保持现状|维持现状|按.{0,16}处理|作为.{0,16}处理"
)


def reclassify_answered_questions(
    *,
    prior_state: CompressedState,
    ops: Sequence[DeltaOp],
    written: Sequence[tuple[str, str, int]] = (),
    reserved_ids: Sequence[str] = (),
    max_nodes: int = 2,
) -> tuple[list[DeltaOp], int]:
    """把「已经答完的悬案」从【未解决】搬到事实链，写成 `<THINK ->` 负节点。

    只动 `SET_UNRESOLVED` 这一条 op 的 payload（文字**逐字保留**，不生成新内容），
    命中判据是"条目自己就带结论词"（未新建/不加/按…处理）—— 没有结论词的悬案照旧留在【未解决】。
    返回 `(追加的负节点, 从【未解决】里摘掉的条数)`。
    """
    tail = [op for op in ops if op.op == "SET_UNRESOLVED"]
    if not tail:
        return [], 0
    payload = dict(tail[-1].payload or {})
    items = payload.get("items")
    if not isinstance(items, list):
        return [], 0
    kept: list[str] = []
    answered: list[str] = []
    for raw in items:
        text = str(raw).strip()
        if text and _ANSWERED_UNRESOLVED_RE.search(text):
            answered.append(text)
        elif text:
            kept.append(text)
    if not answered:
        return [], 0
    tail[-1].payload["items"] = kept
    # 锚点：写出去的第一个 .py（"无锚不留"这条对 THINK 一律适用）
    owner = next((p for p, _sha, _nb in written if p.endswith(".py")), "")
    if not owner:
        owner = next(
            (f.anchor.path for f in prior_state.fact_chain if f.anchor and f.anchor.path),
            "",
        )
    if not owner:
        return [], 0
    nxt = _id_picker(prior_state, list(reserved_ids) + [str((op.payload or {}).get("id") or "") for op in ops])
    made: list[DeltaOp] = []
    for text in answered[:max_nodes]:
        made.append(
            DeltaOp(
                op="ADD_FACT",
                payload={
                    "id": nxt(),
                    "statement": f"已定（不再悬置）：{text}",
                    "anchor": {"path": owner},
                    "node_kind": "think",
                    "polarity": "-",
                    "assertion": "settled",
                    "salience": 80,
                    "why": "原文里这条悬念自己给了结论（服务端据此从【未解决】移到这里），别再去'确认'它",
                },
            )
        )
    return made, len(answered)


def with_act_nodes(
    delta: DeltaC,
    *,
    prior_state: CompressedState,
    written: Sequence[tuple[str, str, int]],
    sources: Mapping[str, str] | None = None,
    verify: str = "",
    verify_state: str = "",
    thinking: str = "",
) -> tuple[DeltaC, dict[str, int]]:
    """在模型 ΔC 后面追加 ACT 节点（server 渲染）+ 被否备选/被砍依赖的负节点。

    ACT 紧跟在模型 ops 后面是有意的：`apply_delta` 顺序执行，id 由 `_id_picker` 让开
    模型 ops 与前面 ACT 用掉的号；反过来排就会撞号。

    `sources` 是 `{path: 文本}`，只用来从 `.py` 里抽符号（符号级锚点），不参与任何判定。
    `thinking` 只用来找"被否掉的备选 / 草稿里 import 过但最终没留下的依赖"。
    """
    reserved = [str((op.payload or {}).get("id") or "") for op in delta.ops]
    act_ops = build_act_ops(
        prior_state=prior_state,
        written=written,
        reserved_ids=reserved,
        sources=sources,
        verify=verify,
    )
    reserved.extend(str((op.payload or {}).get("id") or "") for op in act_ops)
    known_extra = " ".join(
        str(v)
        for op in delta.ops
        for v in [
            (op.payload or {}).get("statement"),
            (op.payload or {}).get("why"),
            (op.payload or {}).get("risk"),
            (op.payload or {}).get("reason"),
        ]
        if v
    )
    alt_ops = build_alternative_ops(
        prior_state=prior_state,
        sources=sources,
        thinking=thinking,
        reserved_ids=reserved,
        known_extra=known_extra,
    )
    reserved.extend(str((op.payload or {}).get("id") or "") for op in alt_ops)
    dep_ops = build_dropped_import_ops(
        prior_state=prior_state,
        sources=sources,
        thinking=thinking,
        reserved_ids=reserved,
        known_extra=known_extra + " " + " ".join(str((op.payload or {}).get("statement") or "") for op in alt_ops),
    )
    reserved.extend(str((op.payload or {}).get("id") or "") for op in dep_ops)
    route_ops = build_rejected_route_ops(
        prior_state=prior_state,
        sources=sources,
        thinking=thinking,
        reserved_ids=reserved,
        known_extra=known_extra
        + " "
        + " ".join(str((op.payload or {}).get("statement") or "") for op in alt_ops + dep_ops),
    )
    reserved.extend(str((op.payload or {}).get("id") or "") for op in route_ops)
    risk_ops = build_shelved_risk_ops(
        prior_state=prior_state,
        sources=sources,
        thinking=thinking,
        reserved_ids=reserved,
        known_extra=known_extra
        + " "
        + " ".join(
            str((op.payload or {}).get("statement") or "")
            for op in alt_ops + dep_ops + route_ops
        ),
    )
    reserved.extend(str((op.payload or {}).get("id") or "") for op in risk_ops)
    answered_ops, answered_n = reclassify_answered_questions(
        prior_state=prior_state,
        ops=delta.ops,
        written=written,
        reserved_ids=reserved,
    )
    merged = DeltaC(
        ops=list(delta.ops) + act_ops + alt_ops + dep_ops + route_ops + risk_ops + answered_ops,
        model=delta.model,
        est_tokens=delta.est_tokens,
        truncated=delta.truncated,
        retried=delta.retried,
        raw=delta.raw,
        error=delta.error,
        prompt_tokens=delta.prompt_tokens,
        completion_tokens=delta.completion_tokens,
        cached_tokens=delta.cached_tokens,
    )
    return merged, {
        "act_nodes": len(act_ops),
        "act_verified": verify_state or "none",
        "think_alternatives": len(alt_ops),
        "think_dropped_deps": len(dep_ops),
        "think_rejected_routes": len(route_ops),
        "think_shelved_risks": len(risk_ops),
        "unresolved_answered": answered_n,
    }
