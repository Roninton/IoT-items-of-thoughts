"""P（冻结前缀）的装载与哈希锁定。

P 不是一段散文，而是"散文 + 机器可读契约"的冻结包：

    prefix/
      PROJECT.md        需求 / 架构约束 / 编码规范（进 prompt 的人读部分）
      constraints.yaml  约束清单（id / text / check 绑定 / 硬软标记）
      contracts.yaml    接口契约（模块 -> 公开符号 -> 签名）  <- 签名漂移的基准
      stages.yaml       阶段分解（写死在 P，保证跨组可比）
      layers.yaml       允许的依赖方向（分层违规检测的基准）
      tests_frozen/     冻结一致性测试（只读，每轮由 server 恢复）

prefix_hash = 整包内容哈希。它进入每个 round 的日志，所以"P 有没有被偷偷改"是个客观事实。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .models import ConstraintStatus


class ConstraintSpec(BaseModel):
    id: str
    text: str
    check: str
    hard: bool = False
    category: str = ""


class ContractSymbol(BaseModel):
    module: str
    symbol: str
    kind: str = "func"
    signature: str = ""
    stage: int = 1  # 属于哪个阶段；缺省 1（假工作模型据此决定第几轮实现它）


class LayerRule(BaseModel):
    src_layer: str
    may_depend_on: list[str] = Field(default_factory=list)


class StageSpec(BaseModel):
    index: int
    title: str
    goal: str = ""
    temptation: str = ""  # 人为制造的"想走被否方案"的压力点


class PrefixBundle(BaseModel):
    root: Path
    project_md: str
    constraints: list[ConstraintSpec]
    contracts: list[ContractSymbol]
    stages: list[StageSpec]
    layers: list[LayerRule]
    hash: str
    layer_root: str = ""  # 计算分层时的根包名，如 "drawvid"
    frozen_tests_dir: Path | None = None

    def layer_of(self, module: str) -> str:
        """模块 -> 层。`drawvid.drawing.canvas` 且 root=drawvid -> `drawing`。"""
        parts = module.split(".")
        if self.layer_root and parts and parts[0] == self.layer_root:
            parts = parts[1:]
        return parts[0] if parts else "_root"

    def contracts_by_stage(self, stage: int) -> list[ContractSymbol]:
        return [c for c in self.contracts if c.stage == stage]

    # -- 组装 ------------------------------------------------------------- #

    @classmethod
    def load(cls, root: str | Path) -> PrefixBundle:
        root = Path(root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"prefix 目录不存在: {root}")

        project_md = ""
        pj = root / "PROJECT.md"
        if pj.exists():
            project_md = pj.read_text(encoding="utf-8")

        constraints = [
            ConstraintSpec(**c)
            for c in cls._yaml(root / "constraints.yaml", key="constraints")
        ]
        contracts = [
            ContractSymbol(**c)
            for c in cls._yaml(root / "contracts.yaml", key="contracts")
        ]
        stages = [StageSpec(**s) for s in cls._yaml(root / "stages.yaml", key="stages")]
        layers_doc: dict[str, Any] = {}
        lp = root / "layers.yaml"
        if lp.exists():
            layers_doc = yaml.safe_load(lp.read_text(encoding="utf-8")) or {}
        layers = [LayerRule(**rule) for rule in (layers_doc.get("layers") or [])]
        layer_root = str(layers_doc.get("root") or "")

        ft = root / "tests_frozen"
        return cls(
            root=root,
            project_md=project_md,
            constraints=constraints,
            contracts=contracts,
            stages=stages,
            layers=layers,
            layer_root=layer_root,
            hash=cls._hash_dir(root),
            frozen_tests_dir=ft if ft.is_dir() else None,
        )

    @staticmethod
    def _yaml(path: Path, key: str) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get(key) or [])

    @staticmethod
    def _hash_dir(root: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode("utf-8"))
                h.update(b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
        return h.hexdigest()

    # -- 渲染 ------------------------------------------------------------- #

    def constraint_texts(self) -> dict[str, str]:
        return {c.id: c.text for c in self.constraints}

    def initial_constraint_status(self) -> list[ConstraintStatus]:
        return [
            ConstraintStatus(id=c.id, text=c.text, status="unverified")
            for c in self.constraints
        ]

    def stage(self, index: int) -> StageSpec | None:
        return next((s for s in self.stages if s.index == index), None)

    def render(self) -> str:
        """进 prompt 的 P 正文。顺序固定 = 前缀缓存命中率的前提。"""
        # 用 `##` 而不是 `#`：P 是 prompt 骨架里**第 1 段的正文**，
        # 顶层 `#` 留给骨架本身，这样"骨架"才能被机器单独抽出来校验（见 render.extract_skeleton）。
        parts: list[str] = ["## 项目冻结前缀 P（只读，不得修改）", "", self.project_md.strip()]

        if self.constraints:
            parts += ["", "## 约束清单（原文，逐轮校验）", ""]
            for c in self.constraints:
                tag = "硬" if c.hard else "软"
                parts.append(f"- [{c.id}][{tag}][{c.category}] {c.text}")

        if self.contracts:
            parts += ["", "## 接口契约（已定义接口不得破坏）", ""]
            for c in self.contracts:
                parts.append(f"- `{c.module}.{c.symbol}` ({c.kind}) {c.signature}")

        if self.stages:
            parts += ["", "## 阶段分解（写死在 P，跨组可比）", ""]
            for s in self.stages:
                parts.append(f"{s.index}. {s.title} —— {s.goal}")

        if self.layers:
            parts += ["", "## 允许的依赖方向（反向依赖 = 分层违规）", ""]
            for rule in self.layers:
                allowed = ", ".join(rule.may_depend_on) or "（无）"
                parts.append(f"- {rule.src_layer} 只可依赖: {allowed}")

        return "\n".join(parts).strip() + "\n"

    # -- 冻结测试 --------------------------------------------------------- #

    def restore_frozen_tests(self, workspace: Path) -> list[str]:
        """每轮验证前把冻结测试从 P 恢复 —— 模型改了也无效。"""
        if not self.frozen_tests_dir:
            return []
        dest = workspace / "tests" / "frozen"
        dest.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        for p in sorted(self.frozen_tests_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.frozen_tests_dir)
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, out)
                restored.append(f"tests/frozen/{rel.as_posix()}")
        return restored


class WorkspaceLayerMap(BaseModel):
    """模块路径 -> 层。默认按第一段目录推断。"""

    explicit: dict[str, str] = Field(default_factory=dict)

    def layer_of(self, module: str) -> str:
        if module in self.explicit:
            return self.explicit[module]
        return module.split(".", 1)[0]

    @classmethod
    def from_prefix(cls, prefix: PrefixBundle, mapping: dict[str, str] | None = None) -> WorkspaceLayerMap:
        return cls(explicit=dict(mapping or {}))
