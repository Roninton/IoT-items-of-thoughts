"""P（冻结前缀）的完整性自检。

P 是整场实验的输入。它出错的方式全都**不报警**：
约束绑到一个不存在的检查名 → 那条约束永远 `unverified`，曲线上少一条线；
契约的 stage 超出阶段数 → 那个接口永远不会被要求实现；
冻结测试里用了 `importorskip` → "没实现"变成"跳过"，而跳过算通过。

所以这里把 P 当代码来测。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from pciso.checks import CHECKS
from pciso.prefix import PrefixBundle

PREFIX_ROOT = Path(__file__).resolve().parents[1] / "prefixes"


def _kind(root: Path) -> str:
    """前缀类别：`experiment`（实验载体，默认）或 `chat`（通用助手，见 prefixes/general/prefix.yaml）。

    为什么需要这个区分：实验载体的 P 必须完整（约束 + 契约 + 阶段 + 分层 + 冻结测试），
    那是客观验证器的输入，下面逐项守着；通用助手的 P 故意没有这些 —— 它不规定产物结构，
    任务由当轮那句话给。硬套一份 contracts.yaml / layers.yaml 只会凭空造出误报违反。
    所以这里按显式声明分类，而不是让自检去猜。
    """
    meta = root / "prefix.yaml"
    if not meta.is_file():
        return "experiment"
    try:
        data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return "experiment"
    return str(data.get("kind") or "experiment")


PROJECT_DIRS = sorted(p for p in PREFIX_ROOT.iterdir() if (p / "PROJECT.md").is_file())
PREFIX_DIRS = sorted(p for p in PROJECT_DIRS if _kind(p) == "experiment")
CHAT_DIRS = sorted(p for p in PROJECT_DIRS if _kind(p) == "chat")
PREFIX_IDS = [p.name for p in PREFIX_DIRS]


@pytest.fixture(params=PREFIX_DIRS, ids=PREFIX_IDS)
def bundle(request: pytest.FixtureRequest) -> PrefixBundle:
    return PrefixBundle.load(request.param)


# --------------------------------------------------------------------------- #
# 基本可加载性
# --------------------------------------------------------------------------- #


def test_prefix_loads_with_content(bundle: PrefixBundle) -> None:
    assert bundle.project_md.strip()
    assert bundle.constraints and bundle.contracts and bundle.stages and bundle.layers
    assert bundle.hash


@pytest.fixture(params=CHAT_DIRS, ids=[p.name for p in CHAT_DIRS])
def chat_bundle(request: pytest.FixtureRequest) -> PrefixBundle:
    return PrefixBundle.load(request.param)


def test_chat_prefix_is_loadable_and_soft_only(chat_bundle: PrefixBundle) -> None:
    """非实验载体：没有契约/分层是**故意的**，但该有的仍要有 —— 能加载、能渲染、有约束。

    这里不测"完整性"（上面那组测的是实验载体），只测它确实是一份可用的 P：
    否则通用助手会变成一片无人守着的荒地，哪天 PROJECT.md 空了也没人知道。
    """
    assert chat_bundle.project_md.strip()
    assert chat_bundle.constraints, "至少要有一条约束（软约束也算）"
    assert not [c for c in chat_bundle.constraints if c.hard], "通用助手不该有硬约束"
    assert not chat_bundle.contracts and not chat_bundle.layers, "非实验载体故意不声明契约/分层"
    assert "## 约束清单" in chat_bundle.render()


def test_prefix_hash_is_stable_and_path_independent(bundle: PrefixBundle) -> None:
    assert PrefixBundle.load(bundle.root).hash == bundle.hash


def test_there_is_at_least_one_prefix() -> None:
    assert PREFIX_DIRS, "prefixes/ 下没有任何载体，实验无法开跑"


# --------------------------------------------------------------------------- #
# 约束
# --------------------------------------------------------------------------- #


def test_every_constraint_binds_to_a_real_check(bundle: PrefixBundle) -> None:
    """绑错检查名 = 那条约束永远 unverified，而且不会有任何报错。"""
    unknown = sorted({c.check for c in bundle.constraints if c.check not in CHECKS})
    assert not unknown, f"这些约束绑到了不存在的检查: {unknown}"


def test_constraint_ids_and_texts_are_unique(bundle: PrefixBundle) -> None:
    ids = [c.id for c in bundle.constraints]
    assert len(ids) == len(set(ids))
    texts = [c.text for c in bundle.constraints]
    assert len(texts) == len(set(texts))


def test_at_least_one_hard_constraint(bundle: PrefixBundle) -> None:
    """没有硬约束，判定器永远不会回退 —— 回退率恒为 0，那半张图就没了。"""
    assert any(c.hard for c in bundle.constraints)


def test_soft_constraints_exist_too(bundle: PrefixBundle) -> None:
    """全是硬约束则会退化成"每轮都回退"，曲线同样被摁平。"""
    assert any(not c.hard for c in bundle.constraints)


def test_constraint_texts_are_preserved_verbatim_in_render(bundle: PrefixBundle) -> None:
    rendered = bundle.render()
    for c in bundle.constraints:
        assert c.text in rendered, f"{c.id} 的原文没进 P 的渲染结果"


# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #


def test_stages_are_contiguous_from_one(bundle: PrefixBundle) -> None:
    assert [s.index for s in bundle.stages] == list(range(1, len(bundle.stages) + 1))


def test_stages_have_titles_and_goals(bundle: PrefixBundle) -> None:
    for s in bundle.stages:
        assert s.title.strip(), f"阶段 {s.index} 缺 title"
        assert s.goal.strip(), f"阶段 {s.index} 缺 goal"


def test_every_stage_has_a_temptation(bundle: PrefixBundle) -> None:
    """没有诱惑点，失败链就是装饰品：模型在线性推进里不会回头试被否方案，
    `重复尝试率` 会恒等于 0，失败链的增量就测不出来。"""
    missing = [s.index for s in bundle.stages if not s.temptation.strip()]
    assert not missing, f"这些阶段没有 temptation: {missing}"


# --------------------------------------------------------------------------- #
# 接口契约
# --------------------------------------------------------------------------- #


def test_contracts_have_signature_unless_they_are_classes(bundle: PrefixBundle) -> None:
    for c in bundle.contracts:
        if c.kind == "class":
            continue
        assert c.signature.strip(), f"{c.module}.{c.symbol} 是 {c.kind} 却没有签名"


def test_contract_modules_live_under_the_layer_root(bundle: PrefixBundle) -> None:
    root = bundle.layer_root
    assert root, "layers.yaml 必须写 root"
    bad = [c.module for c in bundle.contracts if c.module.split(".")[0] != root]
    assert not bad, f"这些契约模块不在 {root} 下: {sorted(set(bad))}"


def test_contract_stages_are_within_range(bundle: PrefixBundle) -> None:
    n = len(bundle.stages)
    bad = [(c.module, c.symbol, c.stage) for c in bundle.contracts if not 1 <= c.stage <= n]
    assert not bad, f"这些契约的阶段号越界（阶段数 {n}）: {bad}"


def test_contract_symbols_are_unique(bundle: PrefixBundle) -> None:
    keys = [f"{c.module}.{c.symbol}" for c in bundle.contracts]
    assert len(keys) == len(set(keys)), "契约查表键必须唯一，否则会互相覆盖"


def test_contracts_by_stage_covers_every_contract(bundle: PrefixBundle) -> None:
    covered = [c for s in bundle.stages for c in bundle.contracts_by_stage(s.index)]
    assert len(covered) == len(bundle.contracts)


# --------------------------------------------------------------------------- #
# 分层
# --------------------------------------------------------------------------- #


def test_layers_are_self_consistent(bundle: PrefixBundle) -> None:
    names = {rule.src_layer for rule in bundle.layers}
    assert len(names) == len(bundle.layers), "层名重复"
    dangling = sorted(
        {dep for rule in bundle.layers for dep in rule.may_depend_on if dep not in names}
    )
    assert not dangling, f"这些依赖目标不是已定义的层: {dangling}"


def test_no_layer_depends_on_itself(bundle: PrefixBundle) -> None:
    bad = [r.src_layer for r in bundle.layers if r.src_layer in r.may_depend_on]
    assert not bad, f"层依赖了自己: {bad}"


def test_every_contract_module_maps_to_a_defined_layer(bundle: PrefixBundle) -> None:
    names = {rule.src_layer for rule in bundle.layers}
    bad = sorted(
        {bundle.layer_of(c.module) for c in bundle.contracts} - names
    )
    assert not bad, f"这些契约所属的层没在 layers.yaml 里定义: {bad}"


def test_layer_graph_has_no_cycle(bundle: PrefixBundle) -> None:
    """层图本身有环 = 约束 C3 从一开始就无法满足。"""
    from pciso.merge import find_cycles

    edges = [
        (rule.src_layer, dep) for rule in bundle.layers for dep in rule.may_depend_on
    ]
    assert find_cycles(edges) == [], "分层规则自身成环"


# --------------------------------------------------------------------------- #
# 冻结一致性测试
# --------------------------------------------------------------------------- #


def test_frozen_tests_exist(bundle: PrefixBundle) -> None:
    assert bundle.frozen_tests_dir is not None, "P 必须自带冻结一致性测试"
    files = sorted(bundle.frozen_tests_dir.rglob("test_*.py"))
    assert files, "冻结测试目录里没有 test_*.py"


# 只匹配**代码形态**，不匹配散文 —— 冻结测试的 docstring 里很可能正写着
# "不要用 importorskip" 这句话，用裸子串会把正当说明判成违规。
_SKIP_PATTERNS = (
    "pytest.importorskip(",
    "pytest.skip(",
    "@pytest.mark.skip",
    "@pytest.mark.xfail",
    "pytest.mark.skipif(",
)


def test_frozen_tests_cannot_self_skip(bundle: PrefixBundle) -> None:
    """这是最容易漏掉、后果最严重的一个口子。

    `importorskip` 会把"模块没实现"变成"跳过"，而跳过在 pytest 里算通过 ——
    工作模型只要不实现那个模块，冻结测试就自动绿了。
    """
    assert bundle.frozen_tests_dir is not None
    offenders: list[str] = []
    for path in sorted(bundle.frozen_tests_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in _SKIP_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders, f"冻结测试里出现了自我豁免手段: {offenders}"


def test_frozen_tests_import_the_contract_modules_at_top_level(bundle: PrefixBundle) -> None:
    """顶层 import 才会让"模块缺失"表现为收集期错误（红），而不是静默跳过。"""
    assert bundle.frozen_tests_dir is not None
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(bundle.frozen_tests_dir.rglob("*.py"))
    )
    modules = sorted({c.module for c in bundle.contracts})
    imported = set(re.findall(r"^from\s+([\w.]+)\s+import", text, re.MULTILINE))
    missing = [m for m in modules if m not in imported]
    assert not missing, f"冻结测试没有在顶层 import 这些契约模块: {missing}"


def test_frozen_manifest_covers_every_frozen_file(bundle: PrefixBundle) -> None:
    from pciso.checks import frozen_manifest

    assert bundle.frozen_tests_dir is not None
    manifest = frozen_manifest(bundle)
    on_disk = {
        f"tests/frozen/{p.relative_to(bundle.frozen_tests_dir).as_posix()}"
        for p in bundle.frozen_tests_dir.rglob("*")
        if p.is_file()
    }
    assert on_disk == set(manifest), "冻结清单与磁盘内容不一致"


def test_restore_frozen_tests_is_idempotent(bundle: PrefixBundle, tmp_path: Path) -> None:
    assert bundle.frozen_tests_dir is not None
    first = bundle.restore_frozen_tests(tmp_path)
    assert first
    second = bundle.restore_frozen_tests(tmp_path)
    assert sorted(first) == sorted(second)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def test_render_contains_every_section(bundle: PrefixBundle) -> None:
    rendered = bundle.render()
    for header in ("项目冻结前缀 P", "## 约束清单", "## 接口契约", "## 阶段分解", "## 允许的依赖方向"):
        assert header in rendered, f"P 的渲染结果缺段落: {header}"


def test_render_is_deterministic(bundle: PrefixBundle) -> None:
    """P 的字节稳定性是前缀缓存命中率的前提。"""
    assert bundle.render() == bundle.render()
