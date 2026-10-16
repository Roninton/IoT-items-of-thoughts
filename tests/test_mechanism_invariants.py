"""机制不变量：对**任意**模型输出都必须成立（属性测试 / TDD）。

## 为什么是这一组测试

此前每一个真机 bug 都是同一类：**机制在核心产物已经坏掉时继续往下跑，而且不报警**。

| 踩过的 | 表面现象 | 实际 |
|---|---|---|
| 事实节点 id 格式没写进提示词 | 违反数曲线照常演进 | 事实链**一直是空的** |
| `Session.load` 不解析相对 workspace | 会话加载成功 | 悄悄写到**另一个空目录** |
| `git_commit` 失败只 return None | 七组 8 轮跑完 | **零提交**，分支隔离失效 |
| trailer 前没空行 | `--grep` 搜得到 | trailer **解析为空** |
| 压缩开销没人写入 | 报告齐全 | 主要成本项**恒为 0** |

共同点：**它们都不是崩溃，是静默降级**。而离线测试没抓到，是因为离线测试只喂
heuristic 提取器与脚本 oracle 的**规范输出** —— 真实模型不吐规范输出。

所以这组测试的做法是：随机生成敌意但合法的 O_i（乱 id、坏路径、空块、重复路径、
只声明不落盘、干脆没有 file 块），跑完整轮次，然后断言不变量。

**它不测"模型对不对"，它测"机制有没有兜住"。**
"""

from __future__ import annotations

import os
import random
import re
from pathlib import Path

import pytest

from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"
C_MAX = 800  # 故意小：让裁剪路径也被覆盖

# 种子数**可调**：每轮都要起真 pytest+ruff 子进程，24 个种子 x 3 轮 ≈ 72s，
# 占整套测试的 29%。默认 8 个种子 = 24 轮敌意输出，足以覆盖全部失败类别；
# 想跑满用 `PCISO_INVARIANT_SEEDS=24`。（"全部通过"仍由生成器自检守着，
# 它保证样本里确实存在无 file 块 / 越界路径 / @@READ 这些形态。）
_SEEDS = int(os.environ.get("PCISO_INVARIANT_SEEDS", "8"))

FACT_ID = re.compile(r"^N\d+$")


# --------------------------------------------------------------------------- #
# 会话脚手架
# --------------------------------------------------------------------------- #


def make_session(tmp_path: Path, *, c_max_tokens: int = C_MAX) -> Session:
    workspace = tmp_path / "target"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "INV",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": 40,
        "c_max_tokens": c_max_tokens,
        "workspace_path": str(workspace),
        "branch_per_arm": False,
        "fake_oracle": "drawing_video",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="INV",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


# --------------------------------------------------------------------------- #
# 敌意 O_i 生成器
# --------------------------------------------------------------------------- #

GOOD_PATHS = [
    "drawvid/core.py",
    "drawvid/assets.py",
    "drawvid/render.py",
    "drawvid/planner.py",
    "drawvid/audio.py",
    "drawvid/container.py",
    "drawvid/pipeline.py",
    "drawvid/extra.py",
]
# 这些应当被**拒绝**（越界 / 不是 py / 冻结区 / 目录）
HOSTILE_PATHS = [
    "../escape.py",
    "/abs/evil.py",
    "tests/frozen/test_contract.py",
    "post/patch.py",
    "notes.txt",
    "drawvid/",
]

BODIES = [
    '"""有 docstring。"""\n\nVALUE = 1\n',
    "VALUE = 2\n",  # 缺 docstring
    '"""带 TODO 的。"""\n\n# TODO: 以后再说\nVALUE = 3\n',
    '"""语法坏的。"""\n\ndef broken(:\n',  # 语法错误
    "",  # 空文件块
    '"""越层依赖。"""\n\nfrom drawvid.render import render_frame\n\nVALUE = 4\n',
]


def hostile_o_i(rng: random.Random) -> str:
    """生成一段"真实模型可能吐出来"的 O_i。"""
    lines: list[str] = []
    if rng.random() < 0.85:
        lines.append("本轮的设计取舍与遗留问题。")
        lines.append("")
    if rng.random() < 0.3:
        lines.append("被否决的方案：试 X，因 Y 否决。")
        lines.append("")

    # 0~4 个文件块：路径从"正常"与"敌意"里混着取
    paths = rng.sample(GOOD_PATHS, k=rng.randint(0, 4))
    if rng.random() < 0.45:
        paths.append(rng.choice(HOSTILE_PATHS))
    if paths and rng.random() < 0.3:  # 重复写同一个文件
        paths.append(rng.choice(paths))
    rng.shuffle(paths)

    for path in paths:
        if rng.random() < 0.5:
            # 先声明一个工作单位 —— 有时声明的路径并不落盘
            declared = path if rng.random() < 0.8 else rng.choice(GOOD_PATHS)
            lines.append(f"@@UNIT file {declared}")
        body = rng.choice(BODIES)
        lines.append(f"```file:{path}")
        lines.append(body.rstrip("\n"))
        lines.append("```")
        lines.append("")
        if rng.random() < 0.4:
            lines.append("@@UNIT validate")
            lines.append("")
    if rng.random() < 0.25:
        lines.append("@@UNIT note 随手记一个结论")
    if rng.random() < 0.15:
        lines.append("@@READ drawvid/core.py")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 不变量
# --------------------------------------------------------------------------- #


TOOL_JUNK = ("__pycache__", ".ruff_cache", ".pytest_cache")


def project_files(session: Session) -> set[str]:
    """工作区里的**项目文件**。

    工具缓存不算：它们不是"被生成的项目"的一部分，而是验证器跑 ruff/pytest 的残留。
    回退不变量关心的是"模型改的东西有没有被还原"，所以两边都排除它们。
    （`store.restore()` 仍然会清掉这些缓存 —— 那是卫生，不是语义。）
    """
    workspace = session.store.workspace
    return {
        p.relative_to(workspace).as_posix()
        for p in workspace.rglob("*")
        if p.is_file() and not any(part in TOOL_JUNK for part in p.parts)
    }


def check_invariants(
    session: Session, record: dict, before_files: set[str], o_i: str
) -> list[str]:
    """一轮之后必须成立的事。返回违规描述列表。"""
    problems: list[str] = []
    reverted = bool(record.get("telemetry", {}).get("reverted"))
    committed = bool(record.get("committed"))
    workspace = session.store.workspace
    after = project_files(session)

    # --- I1 回退必须把工作区还原到本轮之前，一个文件都不许差 ---
    if reverted and after != before_files:
        problems.append(
            f"回退后工作区没还原：多了 {sorted(after - before_files)}、"
            f"少了 {sorted(before_files - after)}"
        )

    # --- I2 冻结区的内容必须等于 P，而不是等于模型写的东西 ---
    #
    # 注意不能断言"冻结区不许出现文件"：验证器每轮都会把 P 的冻结测试**恢复到**盘上，
    # 所以那个文件本来就该在。要断言的是"盘上的内容不是模型写的那份"。
    # （第一版断言写成了"不许出现"，于是 24/24 全报假错 —— 测试自己也会静默犯错。）
    frozen_src = session.prefix.root / "tests_frozen"
    for rel in sorted(after):
        if not rel.startswith("tests/frozen/"):
            continue
        name = Path(rel).name
        source = frozen_src / name
        if not source.is_file():
            problems.append(f"工作区里出现了 P 里没有的冻结测试：{rel}")
            continue
        on_disk = (workspace / rel).read_bytes()
        if on_disk != source.read_bytes():
            problems.append(f"冻结测试被模型改写且未被还原：{rel}")

    # --- I2b 落盘的路径必须全在工作区内 ---
    for rel in sorted(after - before_files):
        if rel.startswith(("..", "/")):
            problems.append(f"写出了工作区：{rel}")

    # --- I3 提交与 git_sha 必须一致（有工作区时） ---
    if session.cfg.workspace_path:
        sha = record.get("git_sha")
        if committed and not reverted and sha is None:
            problems.append("轮次被接受却没产生 git_sha —— 提交静默失败了")
        if reverted and sha is not None:
            problems.append("被回退的轮次却提交了")

    # --- I4 事实 id 必须合法且唯一 ---
    facts = record["c_i"]["fact_chain"]
    ids = [f["id"] for f in facts]
    for fid in ids:
        if not FACT_ID.match(fid):
            problems.append(f"事实 id 不合格式：{fid!r}")
    if len(ids) != len(set(ids)):
        problems.append(f"事实 id 重复：{sorted(ids)}")

    # --- I5 无锚点的事实必须显式标记为 unanchored（不许洗成"有据"） ---
    for fact in facts:
        if not fact.get("anchor") and not fact.get("unanchored"):
            problems.append(f"事实 {fact['id']} 没有锚点却没标 unanchored")

    # --- I6 C 不许超预算 ---
    if record["c_tokens_est"] > session.cfg.c_max_tokens:
        problems.append(
            f"C 超预算：{record['c_tokens_est']} > {session.cfg.c_max_tokens}"
        )

    # --- I7 约束原文必须与 P 逐字一致（模型不许改写） ---
    prefix_text = {c.id: c.text for c in session.prefix.constraints}
    for item in record["c_i"]["constraints"]:
        if item["text"] != prefix_text.get(item["id"]):
            problems.append(f"约束 {item['id']} 原文被改写")

    # --- I8 ΔC 的 op 不许凭空消失 ---
    ops = len(record["delta_c"]["ops"])
    report = record["merge_report"]
    accounted = (
        len(report.get("applied") or [])
        + len(report.get("rejected") or [])
        + len(report.get("conflicts") or [])
    )
    if accounted < ops:
        problems.append(f"ΔC 有 {ops} 条 op，但只有 {accounted} 条被记账 —— 有 op 静默消失了")

    # --- I9 没有 file 块必须被**检查出来**，而且必须在指标里看得见 ---
    #
    # 口径说明：判定器只对硬约束的**新鲜回归**回退，所以"连续空产出"从第 2 轮起会被
    # accept —— 那是设计语义，不是 bug。但"空产出"绝不能因此变得**不可见**：
    # 它必须是失败的检查 + 一个可数的指标（`rounds_without_file_delta`），
    # 否则模型停手不写文件时曲线会毫无反应。
    has_block = "```file:" in o_i
    if not has_block:
        checks = {c["id"]: c for c in (record["validation"].get("checks") or [])}
        parse = checks.get("parse_output")
        if parse is None or parse.get("passed") is not False:
            problems.append("本轮没有任何 file 块，parse_output 检查却没失败")
        if not record["telemetry"].get("files_written"):
            # 这一轮确实零产出 —— 必须有指标能数到它
            metrics = session.metrics()
            if not metrics["self_report"].get("rounds_without_file_delta"):
                problems.append("零产出轮次没有被 rounds_without_file_delta 数到")

    # --- I10 提取出事实却全被拒收时，必须留下可见信号 ---
    telemetry = record.get("telemetry") or {}
    if (
        telemetry.get("fact_ops_rejected", 0)
        and not telemetry.get("fact_ops_applied", 0)
        and not telemetry.get("extractor_failed")
    ):
        problems.append("事实全被拒收却没标记 extractor_failed")

    return problems


def snapshot_files(session: Session) -> set[str]:
    return project_files(session)


# --------------------------------------------------------------------------- #
# 属性测试
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", list(range(_SEEDS)))
def test_invariants_hold_for_hostile_model_output(tmp_path: Path, seed: int) -> None:
    """对任意敌意输出，机制不变量都必须成立。"""
    rng = random.Random(seed)
    session = make_session(tmp_path)
    failures: list[str] = []
    for _ in range(3):
        before = snapshot_files(session)
        o_i = hostile_o_i(rng)
        # 注入路径 = 真实模型输出走的是同一条下游（写盘/校验/提取/合并/判定）
        session.round(RoundRequest(o_i=o_i or "（空输出）"))
        for problem in check_invariants(session, session.store.last_round(), before, o_i):
            failures.append(f"seed={seed}: {problem}")
    assert not failures, "\n".join(failures)


def test_generator_actually_produces_both_clean_and_hostile(tmp_path: Path) -> None:
    """生成器自检：否则"全部通过"可能只是因为它只生成规范输入。"""
    rng = random.Random(0)
    samples = [hostile_o_i(rng) for _ in range(60)]
    assert any("```file:" not in s for s in samples), "从来没生成过'无 file 块'的样本"
    assert any("@@UNIT file" in s for s in samples)
    assert any("@@READ" in s for s in samples)
    assert any("../escape.py" in s for s in samples)
    assert any("```file:tests/frozen" in s for s in samples)
