"""配置结构 —— 直接就是论文的实验设置表。跑实验 = 切配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import CompressionMode, HistoryMode


class CompressionConfig(BaseModel):
    """事实链提取器的配置。

    **键名叫 compression，指的是"坐在 O_i 与 C 之间"这个位置，不是职责。**
    职责是从 O_i 提取事实链：衡量标准是无据声明率/锚点可解析率/作废正确性，不是体积比；
    丢事实是**错**而不是有损；来源（server 可验证 vs 模型声称）必须可区分。
    完整口径见 README §1.2。
    """

    mode: CompressionMode = "fact_chain"
    # 提取器后端名，在 models.yaml 里解析；"heuristic"/"fake" = 确定性离线实现
    model: str = "heuristic"
    # **压缩范围**（README §1.3）：
    #   full      整轮 O_i 进提取器（七个实验臂的口径，默认值 —— 不许动）
    #   thinking  只把 thinking 块压成**关键推理节点**（正/负）；动作事实由 server 从
    #             telemetry 确定性渲染（actnodes.build_act_ops），正文不进 C。
    scope: str = "full"
    thinking: bool = False  # 提取不 thinking（用户口径）
    # 结构化输出：实测本 endpoint 不支持 `json_schema`（一律 400），只支持 `json_object`。
    # 它保证"是合法 JSON"，**不保证字段格式** —— 所以事实节点 id 那类约束
    # 仍然依赖提示词 + 服务端校验 + 拒收率度量。留 `none` 可关掉做对照。
    response_format: str = "json_object"
    max_tokens: int = 1000
    # 结构化先验：把 C_{i-1} 的节点头部（id + 一行标签）喂给压缩器，
    # 使"更正/推翻"可被表达为 SUPERSEDE / INVALIDATE 操作。
    give_prior_structure: bool = True
    prior_node_head: int = 60
    summary_keep_recent: int = 1

    @field_validator("scope")
    @classmethod
    def _scope_known(cls, v: str) -> str:
        """scope 只认 full / thinking。

        不校验的代价是**静默降级**：写 `scope: thingking` 不会报错，只会安静地按 full 跑 ——
        而 full 与 thinking 产出的 C 长得完全不一样，这种错会被当成"机制没效果"。
        """
        if v not in ("full", "thinking"):
            raise ValueError(f"compression.scope 只认 'full' / 'thinking'，收到 {v!r}")
        return v

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_bool_off(cls, v: object) -> object:
        """YAML 1.1 会把未加引号的 `off` 解析成布尔 False（`on`/`yes`/`no` 同理）。

        这是经典坑：`mode: off` 到不了这里就变成 False，报错信息还完全指不到病根。
        配置里仍应写 `mode: "off"`，但这里也认下来，免得这个坑反复咬人。
        """
        if v is False:
            return "off"
        return v


class WorkModelConfig(BaseModel):
    endpoint: str = "fake"
    # ⚠️ wire id，必须实测存在。DSH 自己给这个模型用的 id 就是 `deepseek-flash`，
    # 而"DS V4.1 Flash"是它的版本名 —— 两者指同一个模型，但**发请求只能用前者**。
    # 实测 `GET /v1/models` 只返回 deepseek-flash / deepseek-v4-pro；
    # 写成版本名会拿到 400，而症状看起来像"模型抽风"而不是"模型名不存在"。
    model: str = "deepseek-flash"
    thinking: bool = True  # 工作 thinking
    # 输出预算。**思考也算在这里面**，所以给小了会得到"思考很长、正文为空"。
    # 实测 8000 不够（31996 字符思考、正文 0 字）；平台自己的 DSH 配置对同一适配器
    # 用的是 32768，注释写"够用（还含 thinking）"。
    max_tokens: int = 32000
    # 正文为空时重试几次（用户裁定）。重试会把输出预算翻倍，正是缺的那样东西。
    retry_on_empty: int = 1
    retry_max_tokens_cap: int = 64000
    temperature: float = 0.2
    base_url: str | None = None
    api_key_env: str = "DEEPSEEK_API_KEY"
    # 仅 endpoint=fake 时生效：定点故障注入，用来证明"硬约束回归 -> 回退"这条路真的通。
    # 可选 none / todo / cycle / drop_docstring / break_signature / weaken_tests / touch_frozen
    fake_fault: str = "none"
    fake_fault_after: int = 1
    # G 组：磁盘检索形态。工作模型看不到历史、也看不到 C，
    # 唯一的信息来源是工作区清单 + @@READ 现读的文件内容。
    # 这是"普通 agent 模式"的诚实替身：没有 harness 自动压缩，检索由 server 执行并可审计。
    retrieval: bool = False
    retrieval_max_cycles: int = 2  # 读一次 -> 再答一次的往返上限
    retrieval_max_files: int = 8  # 单轮最多服务几个文件
    retrieval_max_bytes: int = 60000  # 单轮读取字节上限（防止一轮吃爆上下文）
    # 仅测试用：good = 正常申请；sloppy = 故意申请不存在的路径 + 重复读
    fake_retrieval_mode: str = "good"


class ValidatorConfig(BaseModel):
    enabled: bool = True
    tests: Literal["run_on_each_round", "off"] = "run_on_each_round"
    docs: Literal["check_coverage", "off"] = "check_coverage"
    constraints: Literal["check_list", "off"] = "check_list"
    restore_frozen_tests: bool = True
    coverage_threshold: float = 0.80
    docstring_threshold: float = 1.0
    pytest_timeout_s: int = 120


class SessionConfig(BaseModel):
    project: str = "drawing_video_toolchain"
    prefix_path: str = "prefixes/drawing_video"
    label: str | None = None
    arm: str | None = None

    max_rounds: int = 8
    c_max_tokens: int = 1000
    prefix_frozen: bool = True
    # stateless=True -> F 组：每轮只给 P + 阶段指令，既不给历史也不给 C
    stateless: bool = False
    # history_mode 缺省由 compression.mode 推导；显式设置可覆盖（用于造额外对照臂）
    history_mode: HistoryMode | None = None

    # 对话驱动（configs/chat.yaml）：prompt 里那一段不再由"阶段计划"决定，
    # 换成"该不该动手"的判断规则 —— 对话里没人承诺每轮推进一个阶段，
    # 带着阶段指令聊天，说一句"你好"也会被催着写五个文件（实测）。
    # 默认 False：七个实验臂的 prompt 一个字节都不变。
    chat_driven: bool = False

    # 判定器：硬约束回归 -> 回退该轮工作区（核心指标从"违反数"升级为"回退率"）
    revert_on_hard_regression: bool = True
    revert_on_merge_conflict: bool = False

    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    work_model: WorkModelConfig = Field(default_factory=WorkModelConfig)
    validator: ValidatorConfig = Field(default_factory=ValidatorConfig)

    # 真实实验里指向 ../drawing-video-toolchain（让被生成项目自带 git 历史）。
    # 为空则落在 state/workspaces/<sid>/ 下。
    workspace_path: str | None = None
    state_dir: str = "state"
    layer_map: dict[str, str] = Field(default_factory=dict)
    # 用哪个脚本 oracle（fakeimpl/<名字>/stages/）。缺省取 prefix 目录名。
    # 显式字段是必要的：把 prefix 复制到临时目录做实验时，目录名会变，
    # 按名字找 oracle 就会落空 —— 那种失败看起来像"模型没干活"。
    fake_oracle: str = ""
    # 每组一条 git 分支，并从 `main` 复位后再开跑。
    # 七组共用同一个目标仓库时这是**必须**的：否则 B 组从 A 组成品接着做，
    # 七组退化成同一条流水线的七个阶段，而不是可比对照。
    branch_per_arm: bool = True
    arm_base_branch: str = "main"

    # 预算闸：累计 token 超过它就让这一轮直接拒绝执行。0 = 不限。
    # 真机七组 x 8 轮是要花钱的，出错时最贵的失败模式是"没人发现它一直在烧"。
    # 放进配置（而不是只做命令行参数）是为了让"这次跑预算是多少"进 meta 与实验设置表。
    max_total_tokens: int = 0

    def resolved_history_mode(self) -> HistoryMode:
        if self.stateless:
            return "none"
        if self.history_mode:
            return self.history_mode
        return {"off": "full", "summary": "summary", "fact_chain": "state"}[
            self.compression.mode
        ]

    @classmethod
    def load(cls, path: str | Path, *, base_dir: Path | None = None) -> SessionConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw, base_dir=base_dir)

    @classmethod
    def from_mapping(
        cls, raw: dict[str, Any], *, base_dir: Path | None = None
    ) -> SessionConfig:
        data = dict(raw)

        def _section(name: str) -> dict[str, Any]:
            sec = data.pop(name, None) or {}
            return dict(sec)

        comp = _section("compression")
        work = _section("work_model")
        val = _section("validator")

        # 允许 session.<x> 与顶层 <x> 混写
        session_sec = data.pop("session", None)
        if isinstance(session_sec, dict):
            merged = dict(session_sec)
            merged.update(data)
            data = merged

        cfg = cls(**data)
        if comp:
            cfg.compression = CompressionConfig(**comp)
        if work:
            cfg.work_model = WorkModelConfig(**work)
        if val:
            cfg.validator = ValidatorConfig(**val)

        if base_dir is not None and not Path(cfg.prefix_path).is_absolute():
            cfg.prefix_path = str((base_dir / cfg.prefix_path).resolve())
        if base_dir is not None and not Path(cfg.state_dir).is_absolute():
            cfg.state_dir = str((base_dir / cfg.state_dir).resolve())
        # workspace_path 也必须解析 —— 否则配置里存着相对路径，
        # 而重载会话时不知道相对谁（实测：续跑会静默写到错的目录）。
        if (
            base_dir is not None
            and cfg.workspace_path
            and not Path(cfg.workspace_path).is_absolute()
        ):
            cfg.workspace_path = str((base_dir / cfg.workspace_path).resolve())
        return cfg

    def experiment_row(self) -> dict[str, Any]:
        """直接就是论文实验设置表的一行。"""
        return {
            "arm": self.arm,
            "label": self.label,
            "max_rounds": self.max_rounds,
            "compression": self.compression.mode,
            # 压缩**范围**要进实验设置表：full 与 thinking 是两种不同的上下文机制，
            # 只报 mode 会让两组数据混成一组（README §1.3）。
            "compression_scope": self.compression.scope,
            "history_mode": self.resolved_history_mode(),
            "prefix_frozen": self.prefix_frozen,
            "stateless": self.stateless,
            "c_max_tokens": self.c_max_tokens,
            "max_total_tokens": self.max_total_tokens,
            # endpoint 必须单独报：离线排练时 model 字段仍然写着真实模型名，
            # 只报 model 会让实验设置表把"脚本 oracle 跑出来的数"标成"某模型跑出来的" ——
            # 那是会让方法节直接说谎的一类错误。
            "work_model_endpoint": self.work_model.endpoint,
            "work_model": self.work_model.model,
            "work_is_scripted": self.work_model.endpoint in ("fake", "oracle"),
            "work_thinking": self.work_model.thinking,
            "compressor": self.compression.model,
            "compressor_response_format": self.compression.response_format,
            "compressor_thinking": self.compression.thinking,
            "revert_on_hard_regression": self.revert_on_hard_regression,
        }
