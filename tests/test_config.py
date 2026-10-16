"""配置：它就是论文的实验设置表，所以加载路径必须绝对可靠。

这里守两类曾经真的咬过人的坑：
  1. YAML 1.1 把未加引号的 `off` / `on` / `yes` / `no` 解析成布尔 —— `mode: off` 会变成 False。
  2. 相对路径要相对**仓库根**解析，而不是相对当前工作目录（否则换个 cwd 就找不到 P）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pciso.config import SessionConfig
from pciso.workmodels import STAGES_ROOT

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"


def test_unquoted_yaml_off_is_accepted() -> None:
    """YAML 把 off 解析成 False 之后，到这里必须还能认出是 "off"。"""
    cfg = SessionConfig.from_mapping({"compression": {"mode": False}})
    assert cfg.compression.mode == "off"


def test_quoted_off_works_too() -> None:
    cfg = SessionConfig.from_mapping({"compression": {"mode": "off"}})
    assert cfg.compression.mode == "off"


def test_invalid_mode_still_rejected() -> None:
    with pytest.raises(ValueError):
        SessionConfig.from_mapping({"compression": {"mode": "nonsense"}})


def test_real_config_file_with_off_loads() -> None:
    """直接从磁盘读 —— 这是真正会踩到 YAML 类型推断的那条路径。"""
    path = CONFIG_DIR / "arm_b_full_history.yaml"
    cfg = SessionConfig.load(path, base_dir=ROOT)
    assert cfg.compression.mode == "off"
    assert cfg.resolved_history_mode() == "full"


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_every_shipped_config_loads(path: Path) -> None:
    cfg = SessionConfig.load(path, base_dir=ROOT)
    assert cfg.project
    assert Path(cfg.prefix_path).is_absolute()
    assert Path(cfg.state_dir).is_absolute()
    assert (Path(cfg.prefix_path) / "constraints.yaml").is_file()
    row = cfg.experiment_row()
    assert {"arm", "compression", "history_mode", "prefix_frozen"} <= set(row)


def test_history_mode_derivation() -> None:
    assert SessionConfig(compression={"mode": "off"}).resolved_history_mode() == "full"  # type: ignore[arg-type]
    assert (
        SessionConfig(compression={"mode": "summary"}).resolved_history_mode() == "summary"  # type: ignore[arg-type]
    )
    assert (
        SessionConfig(compression={"mode": "fact_chain"}).resolved_history_mode() == "state"  # type: ignore[arg-type]
    )
    assert SessionConfig(stateless=True).resolved_history_mode() == "none"


def test_explicit_history_mode_overrides_derivation() -> None:
    cfg = SessionConfig(compression={"mode": "fact_chain"}, history_mode="full")  # type: ignore[arg-type]
    assert cfg.resolved_history_mode() == "full"


def test_nested_session_section_is_supported() -> None:
    cfg = SessionConfig.from_mapping(
        {
            "session": {"arm": "X", "max_rounds": 3, "stateless": True},
            "compression": {"mode": "off"},
            "work_model": {"endpoint": "fake"},
        }
    )
    assert cfg.arm == "X"
    assert cfg.max_rounds == 3
    assert cfg.stateless is True
    assert cfg.work_model.endpoint == "fake"


def test_relative_prefix_path_resolves_against_base_dir() -> None:
    cfg = SessionConfig.from_mapping(
        {"prefix_path": "prefixes/drawing_video"}, base_dir=ROOT
    )
    assert Path(cfg.prefix_path) == (ROOT / "prefixes" / "drawing_video").resolve()


def test_retrieval_defaults_are_off() -> None:
    cfg = SessionConfig()
    assert cfg.work_model.retrieval is False
    assert cfg.work_model.retrieval_max_cycles >= 1


def test_arm_configs_cover_the_seven_groups() -> None:
    """七组对照必须都在盘上，且 arm 字段不重不漏。"""
    arms = set()
    for path in CONFIG_DIR.glob("arm_*.yaml"):
        arms.add(SessionConfig.load(path, base_dir=ROOT).arm)
    assert {"A", "B", "C", "D", "E", "F", "G"} <= arms


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("arm_*.yaml")), ids=lambda p: p.name)
def test_arm_configs_declare_their_oracle_explicitly(path: Path) -> None:
    """正式对照必须显式写出用哪个 oracle。

    缺省是按 prefix 目录名猜 —— 那在 prefix 被复制/改名后就会指错，
    而指错的后果是"整轮产出为空"，看起来像模型没干活，极难排查。
    把耦合写成配置里的一行，是让它在评审时可见的最便宜办法。
    """
    cfg = SessionConfig.load(path, base_dir=ROOT)
    assert cfg.fake_oracle, f"{path.name} 没有显式声明 fake_oracle"
    assert (STAGES_ROOT / cfg.fake_oracle / "stages").is_dir(), (
        f"{path.name} 指向的 oracle 不存在: {cfg.fake_oracle}"
    )
    assert Path(cfg.prefix_path).name == cfg.fake_oracle, (
        f"{path.name}: prefix 目录名与 oracle 名不一致，容易在改名后静默指错"
    )
