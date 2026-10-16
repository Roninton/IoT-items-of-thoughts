"""客观验证器：把"违反"从主观判定变成客观计数。

这里逐条验证的关键性质：
  - 首次缺失 ≠ 篡改（否则每个组第 1 轮都被回退，曲线从起点就废）
  - 接口**漂移**才算违反，**缺失**只算完成度
  - 结构性漂移（陈旧调用点 / 分层违规 / 测试弱化）真的能被抓到
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso.checks import (
    CheckContext,
    check_contract_completeness,
    check_docs_presence,
    check_docstring_coverage,
    check_feature_tests,
    check_frozen_tests,
    check_import_cycles,
    check_interface_contract,
    check_layer_violation,
    check_manual_cohesion,
    check_parse_output,
    check_stale_callers,
    check_test_weakening,
    check_todo_residue,
    frozen_manifest,
    run_validator,
    take_snapshot,
)
from pciso.config import SessionConfig
from pciso.prefix import PrefixBundle

CONSTRAINTS = """
constraints:
  - id: C4
    category: 文档
    text: 每个公开接口必须有 docstring
    check: docstring_coverage
    hard: false
  - id: C10
    category: 测试
    text: 冻结一致性测试不得被修改
    check: frozen_tests
    hard: true
"""

CONTRACTS = """
contracts:
  - module: pkg.api
    symbol: f
    kind: func
    signature: "f(a: int) -> int"
"""

LAYERS = """
root: pkg
layers:
  - src_layer: core
    may_depend_on: []
  - src_layer: api
    may_depend_on: [core]
"""

FROZEN = '''"""冻结契约测试。"""

from pkg.api import f


def test_f():
    """契约。"""
    assert f(1) == 1
'''


def make_prefix(tmp_path: Path) -> PrefixBundle:
    root = tmp_path / "prefix"
    (root / "tests_frozen").mkdir(parents=True)
    (root / "PROJECT.md").write_text("# 需求\n", encoding="utf-8")
    (root / "constraints.yaml").write_text(CONSTRAINTS, encoding="utf-8")
    (root / "contracts.yaml").write_text(CONTRACTS, encoding="utf-8")
    (root / "layers.yaml").write_text(LAYERS, encoding="utf-8")
    (root / "tests_frozen" / "test_contract.py").write_text(FROZEN, encoding="utf-8")
    return PrefixBundle.load(root)


def make_workspace(tmp_path: Path, prefix: PrefixBundle) -> Path:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)
    (ws / "pkg" / "__init__.py").write_text('"""包。"""\n', encoding="utf-8")
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int) -> int:\n    """好函数。"""\n    return a\n',
        encoding="utf-8",
    )
    (ws / "tests" / "test_api.py").write_text(
        '"""自测。"""\n\nfrom pkg.api import f\n\n\ndef test_f():\n    """用例。"""\n    assert f(1) == 1\n',
        encoding="utf-8",
    )
    return ws


def ctx_for(
    ws: Path,
    prefix: PrefixBundle,
    *,
    prev: dict | None = None,
    parse_errors: list[str] | None = None,
) -> CheckContext:
    prev = prev or {}
    return CheckContext(
        workspace=ws,
        prefix=prefix,
        cfg=SessionConfig(prefix_path=str(prefix.root)),
        round_index=1,
        frozen_manifest=frozen_manifest(prefix),
        parse_errors=list(parse_errors or []),
        prev_signatures=prev.get("signatures", {}),
        prev_docstrings=prev.get("docstrings", {}),
        prev_hashes=prev.get("hashes", {}),
        prev_test_counts=prev.get("test_counts", {}),
    )


# --------------------------------------------------------------------------- #
# 冻结测试：首次缺失 ≠ 篡改
# --------------------------------------------------------------------------- #


def test_frozen_missing_first_time_is_not_a_violation(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    res = check_frozen_tests(ctx_for(ws, prefix))
    assert res.passed is True
    assert res.count == 0
    assert "尚未初始化" in res.detail


def test_frozen_tampering_is_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    prefix.restore_frozen_tests(ws)
    (ws / "tests" / "frozen" / "test_contract.py").write_text(
        '"""改过了。"""\n\n\ndef test_tampered():\n    """空。"""\n    assert True\n',
        encoding="utf-8",
    )
    res = check_frozen_tests(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_frozen_deletion_after_restore_is_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    prefix.restore_frozen_tests(ws)
    prev = take_snapshot(ws)
    (ws / "tests" / "frozen" / "test_contract.py").unlink()
    res = check_frozen_tests(ctx_for(ws, prefix, prev=prev))
    assert res.passed is False
    assert "被删除" in res.findings[0]


# --------------------------------------------------------------------------- #
# 接口：漂移 = 违反，缺失 = 完成度
# --------------------------------------------------------------------------- #


def test_contract_compliant_workspace_passes(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    assert check_interface_contract(ctx_for(ws, prefix)).passed is True
    assert check_contract_completeness(ctx_for(ws, prefix)).passed is True


def test_signature_drift_is_a_violation(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int, b: int = 0) -> int:\n    """漂了。"""\n    return a\n',
        encoding="utf-8",
    )
    res = check_interface_contract(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_missing_symbol_is_completeness_not_drift(tmp_path: Path) -> None:
    """关键：缺失不能让每个组第 1 轮就被回退。

    语义要点（真机踩过）：契约符号**一个都没实现**时，interface_contract 没有可比的签名 ——
    它返回 `passed=None`（尚无观测），不是 True。原先它返回 True 且 detail 写
    「已实现的契约符号 16 个」（那个数字其实是"带签名的契约条数"），于是同一份 C 里
    「已实现 16 个」和 completeness 的「已实现 0 个」并排出现，看起来像硬矛盾，
    压缩器把它记成高危噪声事实，后面几轮都在讨论这条假矛盾。
    """
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "api.py").write_text('"""空的。"""\n', encoding="utf-8")
    nothing = check_interface_contract(ctx_for(ws, prefix))
    assert nothing.passed is None, "空工作区里'漂移 0 个'是空真，不能报成通过"
    assert "一个都没实现" in nothing.detail
    missing = check_contract_completeness(ctx_for(ws, prefix))
    assert missing.passed is False
    assert missing.count == 1
    # 真的实现了、但签名不对 —— 那才是漂移
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int, b: int = 0) -> int:\n    """漂了。"""\n    return a\n',
        encoding="utf-8",
    )
    drift = check_interface_contract(ctx_for(ws, prefix))
    assert drift.passed is False and drift.count == 1
    assert "已实现 1 个" in drift.detail, "detail 里的数字必须是**已实现**的条数，不是契约总数"


# --------------------------------------------------------------------------- #
# 依赖图
# --------------------------------------------------------------------------- #


def test_import_cycle_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\nfrom .core import g\n\n\ndef f(a: int) -> int:\n    """好。"""\n    return g(a)\n',
        encoding="utf-8",
    )
    (ws / "pkg" / "core.py").write_text(
        '"""核心。"""\n\nfrom .api import f\n\n\ndef g(a: int) -> int:\n    """核心函数。"""\n    return a\n',
        encoding="utf-8",
    )
    res = check_import_cycles(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_layer_violation_detected(tmp_path: Path) -> None:
    """core 是叶子，反向 import api 就是越层。"""
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "core.py").write_text(
        '"""核心。"""\n\nfrom .api import f\n\n\ndef g(a: int) -> int:\n    """核心。"""\n    return a\n',
        encoding="utf-8",
    )
    res = check_layer_violation(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_scripts_outside_package_are_ignored(tmp_path: Path) -> None:
    """根目录脚本（demo.py）不是包的公开接口面，否则全是假阳性。"""
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "demo.py").write_text(
        "from pkg.api import f\n\n\ndef main():\n    return f(1)\n", encoding="utf-8"
    )
    assert check_layer_violation(ctx_for(ws, prefix)).passed is True
    assert check_docstring_coverage(ctx_for(ws, prefix)).passed is True
    assert check_feature_tests(ctx_for(ws, prefix)).passed is True


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #


def test_docstring_coverage_flags_missing(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int) -> int:\n    return a\n', encoding="utf-8"
    )
    res = check_docstring_coverage(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_docs_presence(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    assert check_docs_presence(ctx_for(ws, prefix)).passed is False
    (ws / "README.md").write_text("# 项目\n" + "说明 " * 40, encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "ARCHITECTURE.md").write_text("# 架构\n", encoding="utf-8")
    assert check_docs_presence(ctx_for(ws, prefix)).passed is True


def test_todo_residue(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int) -> int:\n    """好。"""\n    # TODO: 补边界\n    return a\n',
        encoding="utf-8",
    )
    res = check_todo_residue(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


def test_orphan_public_symbol_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "extra.py").write_text(
        '"""额外。"""\n\n\ndef never_tested(x: int) -> int:\n    """没人测。"""\n    return x\n',
        encoding="utf-8",
    )
    res = check_feature_tests(ctx_for(ws, prefix))
    assert res.passed is False
    assert res.count == 1


# --------------------------------------------------------------------------- #
# 结构性漂移：陈旧调用点 / 测试弱化
# --------------------------------------------------------------------------- #


def test_stale_caller_detected_when_signature_changes(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "user.py").write_text(
        '"""调用方。"""\n\nfrom .api import f\n\n\ndef use() -> int:\n    """调用。"""\n    return f(1)\n',
        encoding="utf-8",
    )
    prev = take_snapshot(ws)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int, b: int = 0) -> int:\n    """改签名了。"""\n    return a\n',
        encoding="utf-8",
    )
    res = check_stale_callers(ctx_for(ws, prefix, prev=prev))
    assert res.passed is False
    assert res.count >= 1


def test_no_stale_caller_when_caller_updated(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "pkg" / "user.py").write_text(
        '"""调用方。"""\n\nfrom .api import f\n\n\ndef use() -> int:\n    """调用。"""\n    return f(1)\n',
        encoding="utf-8",
    )
    prev = take_snapshot(ws)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int, b: int = 0) -> int:\n    """改签名。"""\n    return a\n',
        encoding="utf-8",
    )
    (ws / "pkg" / "user.py").write_text(
        '"""调用方。"""\n\nfrom .api import f\n\n\ndef use() -> int:\n    """已同步。"""\n    return f(1, 2)\n',
        encoding="utf-8",
    )
    # 自测也是调用点，必须一起同步 —— 否则它就是真正的陈旧调用点
    (ws / "tests" / "test_api.py").write_text(
        '"""自测。"""\n\nfrom pkg.api import f\n\n\ndef test_f():\n    """用例。"""\n    assert f(1, 2) == 1\n',
        encoding="utf-8",
    )
    assert check_stale_callers(ctx_for(ws, prefix, prev=prev)).passed is True


def test_unchanged_test_caller_is_also_a_stale_caller(tmp_path: Path) -> None:
    """测试文件也是调用点：签名变了却没跟着改，就是陈旧调用点（而且测试会红）。"""
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    prev = take_snapshot(ws)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int, b: int = 0) -> int:\n    """改签名。"""\n    return a\n',
        encoding="utf-8",
    )
    res = check_stale_callers(ctx_for(ws, prefix, prev=prev))
    assert res.passed is False
    assert any("test_api.py" in f for f in res.findings)


def test_test_weakening_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    (ws / "tests" / "test_extra.py").write_text(
        '"""两个用例。"""\n\n\ndef test_a():\n    """a。"""\n    assert True\n\n\ndef test_b():\n    """b。"""\n    assert True\n',
        encoding="utf-8",
    )
    prev = take_snapshot(ws)
    (ws / "tests" / "test_extra.py").write_text(
        '"""只剩一个。"""\n\n\ndef test_a():\n    """a。"""\n    assert True\n',
        encoding="utf-8",
    )
    res = check_test_weakening(ctx_for(ws, prefix, prev=prev))
    assert res.passed is False
    assert res.count == 1


def test_test_file_deletion_detected(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    prev = take_snapshot(ws)
    (ws / "tests" / "test_api.py").unlink()
    res = check_test_weakening(ctx_for(ws, prefix, prev=prev))
    assert res.passed is False
    assert "被删除" in res.findings[0]


# --------------------------------------------------------------------------- #
# 其余
# --------------------------------------------------------------------------- #


def test_parse_errors_counted(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    res = check_parse_output(ctx_for(ws, prefix, parse_errors=["非法路径 '../x.py'"]))
    assert res.passed is False
    assert res.count == 1


def test_manual_cohesion_is_explicitly_not_objective(tmp_path: Path) -> None:
    """不可客观判定的约束必须标 None —— 绝不伪装成客观指标。"""
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    assert check_manual_cohesion(ctx_for(ws, prefix)).passed is None


def test_runner_binds_checks_to_constraint_ids(tmp_path: Path) -> None:
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    prefix.restore_frozen_tests(ws)
    (ws / "pkg" / "api.py").write_text(
        '"""接口。"""\n\n\ndef f(a: int) -> int:\n    return a\n', encoding="utf-8"
    )
    res = run_validator(ctx_for(ws, prefix))
    assert "C4" in res.violations
    assert res.violation_count() >= 1


def test_validator_isolates_check_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单条检查炸了不能毁掉整轮。"""
    from pciso import checks as checks_mod

    def boom(ctx: CheckContext):
        raise RuntimeError("故意炸")

    monkeypatch.setitem(checks_mod.CHECKS, "docstring_coverage", boom)
    prefix = make_prefix(tmp_path)
    ws = make_workspace(tmp_path, prefix)
    res = run_validator(ctx_for(ws, prefix))
    assert any("docstring_coverage" in e for e in res.errors)
    assert res.check("docstring_coverage").passed is None
