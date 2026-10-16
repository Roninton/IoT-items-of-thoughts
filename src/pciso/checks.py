"""客观验证器 —— 全案最关键的一块。

它把 P0 指标从"模型自评/人工判读"变成**客观计数**。设计取舍：

1. **只有客观可判定的才计入违反数。** 不可客观判定的（模块内聚性）明确标成
   `passed=None` + 提供 fan-in/fan-out 数据供人工判读，绝不伪装成客观指标 ——
   否则曲线是假的。
2. **"已定义接口不破坏" ≠ "接口必须已全实现"。** 缺失是**完成度**（软），
   漂移才是**违反**（硬）。搞混会让每个组在第 1 轮全部被回退，曲线直接压平。
3. **结构性漂移优先于 lint。** 一个称职的模型在 8 轮里不会让 ruff/TODO 变红，
   所以那几条大概率是平坦的空信号；真正会退化的是
   「签名改了但调用点没跟」「docstring 随轮次消失」「被否决设计重现」「测试被弱化」。
4. 每个 check 都必须返回**计数**而不只是布尔 —— 曲线要有分辨率。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import SessionConfig
from .merge import find_cycles
from .models import CheckResult, ValidationResult
from .prefix import PrefixBundle
from .pyast import (
    find_test_functions,
    import_targets,
    iter_py_files,
    module_level_public_defs,
    parse_file,
    path_to_module,
    referenced_names,
    symbols_of,
)

SKIP_DIRS = (".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "post")
RUFF_HINT = ("ruff",)
TODO_PATTERN = ("TODO", "FIXME", "XXX")


# --------------------------------------------------------------------------- #
# 上下文与快照
# --------------------------------------------------------------------------- #


@dataclass
class CheckContext:
    workspace: Path
    prefix: PrefixBundle
    cfg: SessionConfig
    round_index: int
    frozen_manifest: dict[str, str] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)
    prev_signatures: dict[str, str] = field(default_factory=dict)
    prev_docstrings: dict[str, bool] = field(default_factory=dict)
    prev_hashes: dict[str, str] = field(default_factory=dict)
    prev_test_counts: dict[str, int] = field(default_factory=dict)
    _py_files: list[Path] | None = None
    _pytest: tuple[int | None, str] | None = None

    def pytest_run(self) -> tuple[int | None, str]:
        """跑一次 pytest（尽量带 --cov），**同一轮内复用**。

        `pytest_suite` 与 `coverage_proxy` 检查的是同一套测试。各跑一遍 = 每轮验证成本翻倍：
        目标载体一次约 20 秒，7 组 x 8 轮就是多花二十分钟；在对话界面里更是直接翻倍。
        一次带 --cov 的运行同时给出通过情况与覆盖率，两边都读它。
        """
        if self._pytest is None:
            base = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ]
            code, out = _run(
                [*base, "--cov", "--cov-report=term"],
                self.workspace,
                self.cfg.validator.pytest_timeout_s,
            )
            if out and "unrecognized arguments" in out:
                # 没装 pytest-cov：退回不带 --cov 的那次
                code, out = _run(base, self.workspace, self.cfg.validator.pytest_timeout_s)
            self._pytest = (code, out)
        return self._pytest
    _rel: dict[Path, str] | None = None

    # -- 惰性缓存 --------------------------------------------------------- #

    def py_files(self) -> list[Path]:
        """**包内**源码文件。

        根目录的脚本（`demo.py` / `conftest.py`）不属于包的公开接口面，
        否则它们会变成"缺 docstring""无测试触达""越层依赖"的一堆假阳性来源。
        """
        if self._py_files is None:
            root = self.prefix.layer_root
            out: list[Path] = []
            for p in iter_py_files(self.workspace, skip=SKIP_DIRS):
                rel = p.relative_to(self.workspace).as_posix()
                if "tests" in rel.split("/"):
                    continue
                if root and rel.split("/")[0] != root and rel != f"{root}.py":
                    continue
                out.append(p)
            self._py_files = out
        return self._py_files

    def rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def test_files(self) -> list[Path]:
        return [
            p
            for p in iter_py_files(self.workspace, skip=SKIP_DIRS)
            if "tests" in p.relative_to(self.workspace).as_posix().split("/")
            or p.name.startswith("test_")
        ]

    def all_py_files(self) -> list[Path]:
        return iter_py_files(self.workspace, skip=SKIP_DIRS)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def take_snapshot(workspace: Path) -> dict[str, dict]:
    """每轮结束记一份指纹，供下一轮做漂移/陈旧调用点/测试弱化判定。"""
    signatures: dict[str, str] = {}
    docstrings: dict[str, bool] = {}
    hashes: dict[str, str] = {}
    test_counts: dict[str, int] = {}

    for p in iter_py_files(workspace, skip=SKIP_DIRS):
        rel = p.relative_to(workspace).as_posix()
        try:
            hashes[rel] = sha256_file(p)
        except OSError:
            continue
        tree = parse_file(p)
        if tree is None:
            continue
        is_test = "tests" in rel.split("/") or p.name.startswith("test_")
        if is_test:
            test_counts[rel] = len(find_test_functions(tree))
            continue
        module = path_to_module(rel)
        for sym in module_level_public_defs(tree):
            key = f"{module}.{sym.name}"
            signatures[key] = sym.signature
            docstrings[key] = sym.has_docstring
    return {
        "signatures": signatures,
        "docstrings": docstrings,
        "hashes": hashes,
        "test_counts": test_counts,
    }


def frozen_manifest(prefix: PrefixBundle) -> dict[str, str]:
    out: dict[str, str] = {}
    if not prefix.frozen_tests_dir:
        return out
    for p in sorted(prefix.frozen_tests_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(prefix.frozen_tests_dir).as_posix()
            out[f"tests/frozen/{rel}"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
# 单条检查
# --------------------------------------------------------------------------- #


def check_parse_output(ctx: CheckContext) -> CheckResult:
    n = len(ctx.parse_errors)
    return CheckResult(
        id="parse_output",
        passed=n == 0,
        count=n,
        detail=f"O_i 解析错误 {n} 条",
        findings=list(ctx.parse_errors),
    )


def check_frozen_tests(ctx: CheckContext) -> CheckResult:
    """冻结一致性测试是否被改动。这是"测试不得弱化"的第一道锁。

    "缺失"必须区分两种：
      - **首次缺失**：第 1 轮它们还没被恢复过，缺失是正常初始化，不是篡改；
      - **篡改式缺失**：上一轮快照里存在（且哈希等于 P 的清单），这一轮没了。
    不区分就会让**每个组的第 1 轮都被回退**，曲线从起点就废了。
    """
    changed: list[str] = []
    tampered_missing: list[str] = []
    pending: list[str] = []

    for rel, want in ctx.frozen_manifest.items():
        target = ctx.workspace / rel
        if not target.exists():
            if ctx.prev_hashes.get(rel) == want:
                tampered_missing.append(rel)
            else:
                pending.append(rel)
            continue
        if sha256_file(target) != want:
            changed.append(rel)

    findings: list[str] = []
    if tampered_missing:
        findings.append(f"冻结测试被删除 {len(tampered_missing)}: {', '.join(tampered_missing)}")
    if changed:
        findings.append(f"冻结测试被篡改 {len(changed)}: {', '.join(changed)}")

    count = len(tampered_missing) + len(changed)
    if not ctx.frozen_manifest:
        return CheckResult(id="frozen_tests", passed=None, count=0, detail="P 未提供冻结测试")
    return CheckResult(
        id="frozen_tests",
        passed=count == 0,
        count=count,
        detail=(
            f"冻结测试 {len(ctx.frozen_manifest)} 个，被改 {len(changed)}、被删 {len(tampered_missing)}"
            + (f"，尚未初始化 {len(pending)}" if pending else "")
        ),
        findings=findings,
    )


def _internal_edges(ctx: CheckContext) -> list[tuple[str, str]]:
    modules: dict[str, Path] = {}
    for p in ctx.py_files():
        modules[path_to_module(ctx.rel(p))] = p
    edges: set[tuple[str, str]] = set()
    for module, path in modules.items():
        tree = parse_file(path)
        if tree is None:
            continue
        for target in import_targets(tree, module):
            if target in modules and target != module:
                edges.add((module, target))
    return sorted(edges)


def check_import_cycles(ctx: CheckContext) -> CheckResult:
    # **空集不产生阳性证据。** 工作区里一个 .py 都没有时，"检出环 0 个"是空真 ——
    # 它度量的是空集，报成 satisfied 会被下一轮读成"依赖是干净的"。
    # 真机踩过（会话 46aa73d7e5b94ffa）：模型据此写下"内部依赖边 0 条（C3 0 条、C12 0 条）"，
    # 而那时仓库里一行代码都没有。空读数只能是"尚无观测"，不能是"通过"。
    if not ctx.py_files():
        return CheckResult(
            id="import_cycles", passed=None, count=0, detail="工作区暂无源文件（空真，不计为通过）"
        )
    edges = _internal_edges(ctx)
    cycles = find_cycles(edges)
    return CheckResult(
        id="import_cycles",
        passed=not cycles,
        count=len(cycles),
        detail=f"内部依赖边 {len(edges)} 条，检出环 {len(cycles)} 个",
        findings=[" -> ".join(c) for c in cycles],
    )


def check_layer_violation(ctx: CheckContext) -> CheckResult:
    allowed = {rule.src_layer: set(rule.may_depend_on) for rule in ctx.prefix.layers}
    if not allowed:
        return CheckResult(id="layer_violation", passed=None, count=0, detail="P 未定义分层")
    if not ctx.py_files():
        # 同 import_cycles：没有模块时"越层依赖 0 条"是空真（见那里的说明）。
        return CheckResult(
            id="layer_violation", passed=None, count=0, detail="工作区暂无源文件（空真，不计为通过）"
        )
    bad: list[str] = []
    for src, dst in _internal_edges(ctx):
        sl, dl = ctx.prefix.layer_of(src), ctx.prefix.layer_of(dst)
        if sl == dl:
            continue
        if dl not in allowed.get(sl, set()):
            bad.append(f"{src}({sl}) -> {dst}({dl})")
    return CheckResult(
        id="layer_violation",
        passed=not bad,
        count=len(bad),
        detail=f"反向/越层依赖 {len(bad)} 条",
        findings=bad,
    )


def check_interface_contract(ctx: CheckContext) -> CheckResult:
    """只检查**已存在**符号的签名是否漂移；缺失计入 contract_completeness（软）。"""
    contracts = [c for c in ctx.prefix.contracts if c.kind != "class" and c.signature]
    if not contracts:
        return CheckResult(id="interface_contract", passed=None, count=0, detail="P 未定义接口契约")

    index: dict[str, str] = {}
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in symbols_of(tree, include_methods=True):
            if sym.signature:
                index[f"{module}.{sym.name}"] = sym.signature

    drifted: list[str] = []
    for c in contracts:
        key = f"{c.module}.{c.symbol}"
        got = index.get(key)
        if got is None:
            continue
        want = " ".join(c.signature.split())
        if got != want:
            drifted.append(f"{key}: 契约 `{want}` -> 实际 `{got}`")
    implemented = sum(1 for c in contracts if f"{c.module}.{c.symbol}" in index)
    if implemented == 0:
        # 没有已实现符号就**没有可比的签名**：这时"漂移 0 个"是空真。
        # 而原来的 detail 写的是「已实现的契约符号 {len(contracts)} 个」—— 那个数字其实是
        # "带签名的契约条数"，被读成了"已实现条数"，于是同一份 C 里同时出现
        # 「已实现 16 个」（本检查）与「已实现 0 个」（completeness）—— 一条凭空造出来的矛盾。
        # 真机踩过（会话 46aa73d7e5b94ffa 的 N8）：压缩器把这条矛盾记成高危噪声事实，
        # 之后三轮都在讨论它，而不是讨论代码。
        return CheckResult(
            id="interface_contract",
            passed=None,
            count=0,
            detail=f"契约符号 {len(contracts)} 个（带签名），工作区里一个都没实现 —— 无可比对的签名",
        )
    return CheckResult(
        id="interface_contract",
        passed=not drifted,
        count=len(drifted),
        detail=f"契约符号 {len(contracts)} 个，已实现 {implemented} 个，签名漂移 {len(drifted)} 个",
        findings=drifted,
    )


def check_contract_completeness(ctx: CheckContext) -> CheckResult:
    contracts = ctx.prefix.contracts
    if not contracts:
        return CheckResult(id="contract_completeness", passed=None, count=0, detail="P 未定义契约")
    present: set[str] = set()
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in symbols_of(tree, include_methods=True):
            present.add(f"{module}.{sym.name}")
    missing = [
        f"{c.module}.{c.symbol}"
        for c in contracts
        if f"{c.module}.{c.symbol}" not in present
    ]
    return CheckResult(
        id="contract_completeness",
        passed=not missing,
        count=len(missing),
        detail=f"契约 {len(contracts)} 个，已实现 {len(contracts) - len(missing)} 个",
        findings=[f"未实现: {m}" for m in missing],
    )


def check_docstring_coverage(ctx: CheckContext) -> CheckResult:
    total = 0
    missing: list[str] = []
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in module_level_public_defs(tree):
            total += 1
            if not sym.has_docstring:
                missing.append(f"{module}.{sym.name}")
    if total == 0:
        return CheckResult(id="docstring_coverage", passed=None, count=0, detail="暂无公开符号")
    rate = (total - len(missing)) / total
    thr = ctx.cfg.validator.docstring_threshold
    return CheckResult(
        id="docstring_coverage",
        passed=rate >= thr,
        count=len(missing),
        detail=f"公开符号 {total} 个，缺 docstring {len(missing)} 个（覆盖率 {rate:.0%}，阈值 {thr:.0%}）",
        findings=missing[:40],
    )


def check_docstring_drift(ctx: CheckContext) -> CheckResult:
    """签名没变但 docstring 消失 —— 文档退化，比"覆盖率不够"更尖锐。"""
    if not ctx.prev_docstrings:
        return CheckResult(id="docstring_drift", passed=None, count=0, detail="首轮，无基线")
    drifted: list[str] = []
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in module_level_public_defs(tree):
            key = f"{module}.{sym.name}"
            was = ctx.prev_docstrings.get(key)
            if was is True and not sym.has_docstring:
                drifted.append(key)
    return CheckResult(
        id="docstring_drift",
        passed=not drifted,
        count=len(drifted),
        detail=f"签名未变但 docstring 消失 {len(drifted)} 个",
        findings=drifted,
    )


def check_docs_presence(ctx: CheckContext) -> CheckResult:
    missing: list[str] = []
    readme = ctx.workspace / "README.md"
    if not readme.exists() or len(readme.read_text(encoding="utf-8", errors="replace").strip()) < 80:
        missing.append("README.md（缺失或过短）")
    docs_dir = ctx.workspace / "docs"
    arch = list(docs_dir.glob("*.md")) if docs_dir.is_dir() else []
    if not arch:
        missing.append("docs/*.md（架构文档缺失）")
    return CheckResult(
        id="docs_presence",
        passed=not missing,
        count=len(missing),
        detail=f"工程文档缺失 {len(missing)} 项",
        findings=missing,
    )


def check_todo_residue(ctx: CheckContext) -> CheckResult:
    if not ctx.all_py_files():
        # "TODO 残留 0 处"在没有源文件时是空真（同 import_cycles 的说明）。
        return CheckResult(
            id="todo_residue", passed=None, count=0, detail="工作区暂无源文件（空真，不计为通过）"
        )
    hits: list[str] = []
    for p in ctx.all_py_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for word in TODO_PATTERN:
                if word in line:
                    hits.append(f"{ctx.rel(p)}:{i}: {line.strip()[:80]}")
                    break
    return CheckResult(
        id="todo_residue",
        passed=not hits,
        count=len(hits),
        detail=f"TODO/FIXME/XXX 残留 {len(hits)} 处",
        findings=hits[:40],
    )


def check_feature_tests(ctx: CheckContext) -> CheckResult:
    """"每轮新增功能有对应测试"的客观化：公开符号是否被测试触达（孤儿符号数）。"""
    touched: set[str] = set()
    for p in ctx.test_files():
        tree = parse_file(p)
        if tree is not None:
            touched |= referenced_names(tree)
    orphans: list[str] = []
    total = 0
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in module_level_public_defs(tree):
            total += 1
            if sym.name not in touched:
                orphans.append(f"{module}.{sym.name}")
    if total == 0:
        return CheckResult(id="feature_tests", passed=None, count=0, detail="暂无公开符号")
    return CheckResult(
        id="feature_tests",
        passed=not orphans,
        count=len(orphans),
        detail=f"公开符号 {total} 个，无任何测试触达 {len(orphans)} 个",
        findings=orphans[:40],
    )


def check_test_weakening(ctx: CheckContext) -> CheckResult:
    """既有测试被删除，或测试函数数变少 —— 长程约束压力下最自然的偷懒路径。"""
    if not ctx.prev_test_counts:
        return CheckResult(id="test_weakening", passed=None, count=0, detail="首轮，无基线")
    weak: list[str] = []
    for rel, before in ctx.prev_test_counts.items():
        target = ctx.workspace / rel
        if not target.exists():
            weak.append(f"{rel}: 整个测试文件被删除（原有 {before} 个用例）")
            continue
        tree = parse_file(target)
        after = len(find_test_functions(tree)) if tree is not None else 0
        if after < before:
            weak.append(f"{rel}: 用例数 {before} -> {after}")
    return CheckResult(
        id="test_weakening",
        passed=not weak,
        count=len(weak),
        detail=f"测试弱化 {len(weak)} 处",
        findings=weak,
    )


def check_stale_callers(ctx: CheckContext) -> CheckResult:
    """签名变了但调用点没跟着改 —— 比裸签名 diff 尖锐得多。"""
    if not ctx.prev_signatures:
        return CheckResult(id="stale_callers", passed=None, count=0, detail="首轮，无基线")

    now: dict[str, str] = {}
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        module = path_to_module(ctx.rel(p))
        for sym in symbols_of(tree, include_methods=True):
            if sym.signature:
                now[f"{module}.{sym.name}"] = sym.signature

    changed = {
        key: (ctx.prev_signatures[key], sig)
        for key, sig in now.items()
        if key in ctx.prev_signatures and ctx.prev_signatures[key] != sig
    }
    if not changed:
        return CheckResult(
            id="stale_callers", passed=True, count=0, detail="无签名变更，无需同步调用点"
        )

    # 谁在调用这些符号？
    callers: dict[str, set[str]] = {key: set() for key in changed}
    for p in ctx.all_py_files():
        rel = ctx.rel(p)
        tree = parse_file(p)
        if tree is None:
            continue
        names = referenced_names(tree)
        for key in changed:
            short = key.rsplit(".", 1)[1]
            if short in names:
                callers[key].add(rel)

    stale: list[str] = []
    for key, (old, new) in changed.items():
        for caller in sorted(callers[key]):
            if caller == key.rsplit(".", 1)[0].replace(".", "/") + ".py":
                continue  # 定义处自己
            same_as_before = ctx.prev_hashes.get(caller) == _hash(ctx.workspace / caller)
            if same_as_before:
                stale.append(f"{key} 签名 `{old}` -> `{new}`，但调用方 {caller} 未更新")
    return CheckResult(
        id="stale_callers",
        passed=not stale,
        count=len(stale),
        detail=f"签名变更 {len(changed)} 处，未同步调用方 {len(stale)} 处",
        findings=stale[:40],
    )


def _hash(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 需要跑子进程的检查
# --------------------------------------------------------------------------- #


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int | None, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,  # 调用方解析 returncode，异常路径另行处理
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_pytest_suite(ctx: CheckContext) -> CheckResult:
    if ctx.cfg.validator.tests == "off":
        return CheckResult(id="pytest_suite", passed=None, count=0, detail="配置关闭")
    tests_dir = ctx.workspace / "tests"
    if not tests_dir.is_dir() or not any(tests_dir.rglob("test_*.py")):
        return CheckResult(id="pytest_suite", passed=None, count=0, detail="暂无测试")
    code, out = ctx.pytest_run()
    if code is None:
        return CheckResult(id="pytest_suite", passed=None, count=0, detail=f"无法执行: {out[:200]}")
    failures = 0
    skipped = 0
    for line in out.splitlines():
        if " failed" in line or " error" in line:
            tail = line.strip()
            if tail and tail[0].isdigit():
                failures = max(failures, int(tail.split()[0]))
        if " skipped" in line:
            tail = line.strip()
            if tail and tail[0].isdigit():
                skipped = max(skipped, int(tail.split()[0]))
    if code != 0 and failures == 0:
        failures = 1  # 收集期错误等
    findings = out.strip().splitlines()[-6:]
    if skipped:
        # 跳过在 pytest 里算通过 —— 而"跳过"恰好是绕开冻结契约最省事的办法。
        # 所以这里把跳过直接当失败：它是自我豁免，不是正常结果。
        findings.append(f"出现 {skipped} 个跳过：跳过 = 自我豁免，计为失败")
        failures = max(failures, skipped)
    return CheckResult(
        id="pytest_suite",
        passed=code == 0 and skipped == 0,
        count=failures,
        detail=(
            f"pytest 退出码 {code}，跳过 {skipped}；"
            f"{out.strip().splitlines()[-1][:160] if out.strip() else ''}"
        ),
        findings=findings,
    )


def check_coverage_proxy(ctx: CheckContext) -> CheckResult:
    """优先 pytest-cov；不可用则退化为"公开符号被测试触达率"，并在 detail 里说明用的是哪个。

    **复用 `pytest_suite` 那一次运行**：两者跑的是同一套测试，
    各跑一遍等于把每轮验证成本翻倍（目标载体一次约 20 秒，聊天界面里体感很明显）。
    """
    if ctx.cfg.validator.docs == "off":
        return CheckResult(id="coverage_proxy", passed=None, count=0, detail="配置关闭")
    thr = ctx.cfg.validator.coverage_threshold
    tests_dir = ctx.workspace / "tests"
    if tests_dir.is_dir() and any(tests_dir.rglob("test_*.py")):
        code, out = ctx.pytest_run()
        if code is not None and "unrecognized arguments" not in out:
            for line in out.splitlines():
                if line.strip().startswith("TOTAL"):
                    parts = line.split()
                    pct = next((p for p in parts if p.endswith("%")), None)
                    if pct:
                        rate = float(pct.rstrip("%")) / 100.0
                        return CheckResult(
                            id="coverage_proxy",
                            passed=rate >= thr,
                            count=0 if rate >= thr else 1,
                            detail=f"pytest-cov TOTAL {pct}（阈值 {thr:.0%}）",
                        )

    touched: set[str] = set()
    for p in ctx.test_files():
        tree = parse_file(p)
        if tree is not None:
            touched |= referenced_names(tree)
    total = 0
    hit = 0
    for p in ctx.py_files():
        tree = parse_file(p)
        if tree is None:
            continue
        for sym in module_level_public_defs(tree):
            total += 1
            if sym.name in touched:
                hit += 1
    if total == 0:
        return CheckResult(id="coverage_proxy", passed=None, count=0, detail="暂无公开符号")
    rate = hit / total
    return CheckResult(
        id="coverage_proxy",
        passed=rate >= thr,
        count=0 if rate >= thr else 1,
        detail=f"pytest-cov 不可用，退化为符号触达率 {rate:.0%}（{hit}/{total}，阈值 {thr:.0%}）",
    )


def check_ruff(ctx: CheckContext) -> CheckResult:
    """PEP8 检查。

    必须用 `--isolated`，理由有两条，都是实质性的：
    1. 工作区常常嵌在平台仓库里（`state/workspaces/<sid>/`），ruff 会**向上**找到平台的
       `pyproject.toml` —— 于是 C8 的结果耦合到平台配置，同一个工作区在不同位置得分不同。
    2. 不加 `--isolated` 时，模型只要自己写一个宽松的 ruff 配置就能自我豁免（reward hacking）。
    冻结规则集才能让跨组可比。
    `tests/frozen/` 与 `post/` 排除：前者是 P 提供的只读输入（模型改不了，也不该替 P 的风格背锅），
    后者是实验后人工加固区。
    """
    if not ctx.all_py_files():
        # ruff 在空工作区里返回"All checks passed"—— 那是空真，不是"风格干净"。
        return CheckResult(
            id="ruff", passed=None, count=0, detail="工作区暂无源文件（空真，不计为通过）"
        )
    code, out = _run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--line-length",
            "100",
            "--exclude",
            "tests/frozen",
            "--exclude",
            "post",
            "--output-format",
            "concise",
            ".",
        ],
        ctx.workspace,
        90,
    )
    if code is None or "No module named" in out:
        return CheckResult(
            id="ruff", passed=None, count=0, detail="ruff 未安装，跳过（PEP8 检查降级为不可用）"
        )
    findings = [
        line
        for line in out.splitlines()
        if re.match(r"^\S+:\d+:\d+:", line.strip())
    ]
    return CheckResult(
        id="ruff",
        passed=code == 0,
        count=len(findings),
        detail=f"ruff 退出码 {code}，诊断 {len(findings)} 条",
        findings=findings[:40],
    )


def check_manual_cohesion(ctx: CheckContext) -> CheckResult:
    """模块内聚性无法客观判定 —— 明确标注 None，只给判读数据。"""
    fan_out: dict[str, int] = {}
    for src, _ in _internal_edges(ctx):
        fan_out[src] = fan_out.get(src, 0) + 1
    noisy = [f"{m} ({n} 出边)" for m, n in sorted(fan_out.items()) if n >= 3]
    return CheckResult(
        id="manual_cohesion",
        passed=None,
        count=0,
        detail=f"不可客观判定；模块 {len(fan_out)} 个有出边，其中出边>=3 的 {len(noisy)} 个（供人工判读）",
        findings=noisy,
    )


CHECKS: dict[str, Callable[[CheckContext], CheckResult]] = {
    "parse_output": check_parse_output,
    "frozen_tests": check_frozen_tests,
    "import_cycles": check_import_cycles,
    "layer_violation": check_layer_violation,
    "interface_contract": check_interface_contract,
    "contract_completeness": check_contract_completeness,
    "docstring_coverage": check_docstring_coverage,
    "docstring_drift": check_docstring_drift,
    "docs_presence": check_docs_presence,
    "todo_residue": check_todo_residue,
    "feature_tests": check_feature_tests,
    "test_weakening": check_test_weakening,
    "stale_callers": check_stale_callers,
    "pytest_suite": check_pytest_suite,
    "coverage_proxy": check_coverage_proxy,
    "ruff": check_ruff,
    "manual_cohesion": check_manual_cohesion,
}


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #


def run_validator(ctx: CheckContext) -> ValidationResult:
    if not ctx.cfg.validator.enabled:
        return ValidationResult(round_index=ctx.round_index, checks=[])

    results: list[CheckResult] = []
    errors: list[str] = []
    for name, fn in CHECKS.items():
        try:
            results.append(fn(ctx))
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            results.append(
                CheckResult(id=name, passed=None, count=0, detail=f"检查异常: {exc}")
            )

    # 把 check 绑定到 P 的约束 id
    binding = {c.check: c.id for c in ctx.prefix.constraints}
    for r in results:
        r.constraint_id = binding.get(r.id)

    violations = sorted({r.constraint_id for r in results if r.passed is False and r.constraint_id})
    return ValidationResult(
        round_index=ctx.round_index, checks=results, violations=violations, errors=errors
    )
