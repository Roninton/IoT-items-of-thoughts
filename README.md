# IoT-items-of-thoughts

**IoT = Items of Thoughts。** 把模型的 thinking block 在它**结束的那一刻**提纯成一组带标签的
思维节点链（`[支持] [反对] [权衡] [待定] [计划] [决定] [风险] [中性]`），**原地替换**后返回 ——
正文、`tool_calls`、`usage`、`finish_reason` 与原生行为一模一样。

压掉的只是填充（`let me…` / `hmm…` / 把用户要求再复述一遍），保留的是**关键推理节点**：
正节点（这条路线为什么成立）与**负节点**（哪条路被否了、为什么否）。动机一句话：
**不让思维链里有价值的信息被淹没在 thinking 长文本下，提纯，且降本。**
负节点是长程任务里最贵的一类事实 —— 它拦住的是"重新走一遍已经被否掉的路"。

## 现状：DSH 插件模式

| 形态 | 目录 | 状态 |
|---|---|---|
| **运行时 thinking 提纯插件**（主形态） | [`dsh-plugin-iot-thinking/`](dsh-plugin-iot-thinking/) | **在用**。挂在 DSH 的 `llm/stream` waterfall 上：块开始时放行、reasoning 增量先扣住、块结束时调压缩器、回一个紧凑 `reasoning-delta` + 改写的 `block-end`。**不 provide `ctx.compaction`**，所以没有 checkpoint 消息、没有"上下文已压缩"块，preset 会话里同样生效 |
| 内置 compaction 形态 | [`dsh-plugin-compaction/`](dsh-plugin-compaction/) | **已退役**（2026-10-18）。走 `ctx.compaction`，会把一段历史换成 checkpoint 消息，模型读到后当成新指令重新定位。留作历史与回滚参考 |
| pciso 实验平台（前身） | `src/pciso/`、`configs/`、`prefixes/`、`scripts/`、`tests/` | 保留、测试仍在跑（见 [`docs/pciso-platform.md`](docs/pciso-platform.md)）。它是**实验台**：离线可复现、可回放、可故障注入，用来量"约束违反数随轮数"与"prompt 成本" |

插件是外部 bundle：`package.json` 声明 `dsh.bundle.patch`，装进 profile 后
`cordis.patch.yml` 那一行自动成为配置层的一部分。

## 快速开始

```powershell
# 1) 装插件（DSH 0.2.x / node >= 20）
dsh plugin --profile web add "<repo>\dsh-plugin-iot-thinking"
#    也可以走 UI：Settings → Plugins → 添加插件 → 填上面那个绝对路径
#    装完重启一次 dsh web（host 模块是新的；client 半个从 exports["./client"] 出）

# 2) 插件自测（纯逻辑，不启动 dsh、不连模型）
node dsh-plugin-iot-thinking/selftest.mjs

# 3) pciso 平台测试（可选，跑的是实验台那套）
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q -m "not slow"
```

开关与参数在 **Settings → Plugins → IoT 思考压缩**：启用/关闭、最小压缩字数（默认 400）、
最大输入字数（默认 24000）、节点输出上限（`maxTokens`，默认 2000）、最多节点数（默认 8）、
最多 `[中性]` 数（默认 2）、可选 provider/model，以及**压缩档位**（`compact` 3 标签 /
`balanced` 5 标签 / `full` 8 标签）。配置写到 `$DSH_HOME/plugin-data/iot-thinking/config.json`，
每次请求按 mtime 读，**改完下一个模型请求就生效，不用重启**。

## 怎么验证它真的生效了

**只有插件账本是可靠证据**（会话日志里看见的紧凑块，分不清是插件压的还是模型自己写短的）：

```text
$DSH_HOME/plugin-data/iot-thinking/events.jsonl          # 事件账本
$DSH_HOME/plugin-data/iot-thinking/originals/<sid>/<time>-block<index>.txt   # 替换前的原文归档
```

事件：`constructed` / `block-replaced` / `block-kept` / `block-skipped` / `block-failed` /
`thinking-compressed` / `thinking-failed` / `thinking-degenerate`。
`thinking-compressed` 带 `charsIn / charsOut / profile`，可以按档位统计压缩比。
替换前的原文只存在 `originals/` 里 —— dsh 存的是 waterfall 吐出来的那些 chunk，块一被换，
原文在会话里就没了。

## 关键裁定（完整版见 [`docs/principle.md`](docs/principle.md)）

| 裁定 | 为什么 |
|---|---|
| **只压 thinking**，正文与动作块一个字节不动 | 代码块、read/edit 的执行结果是**事实块**，压它只会失真、误判、丢细节 |
| 压成**正负推理节点**，不是摘要 | 推理不是没了，是去掉填充；负节点决定了下一轮不会重走已否掉的路 |
| 事实**由 server 渲染**，不由模型压 | 谁写了哪个文件、跑没跑测试，本来就有 sha / 退出码；让模型经手，"把计划记成事实"才可能出现 |
| 触发点 = **thinking block 结束** | 这是唯一的稳定锚点；块边界用**位置法**（块开始记 `start`、正文或工具调用出现记 `end`），不按文本猜 |
| 失败就**回原文** | 宁可不压，不可丢。压缩器失败 / 输出为空 / 不比原文短，都原样放行 |
| **模型自述只能当探针，不能当判据** | 实测它三次把原文里根本没有的东西算成"丢失"。有丢失就一定有原因，找得出来才算数 |

## 文档

| 文档 | 管什么 |
|---|---|
| [`docs/principle.md`](docs/principle.md) | **原理**：核心裁定、压缩粒度为什么是"一块一次"、为什么叫"事实链提取"而不是压缩、范围边界、触发点与代价 |
| [`docs/thinking-compression.md`](docs/thinking-compression.md) | **讨论与设计沿革**：LoopMoE 那一批论文的映射、结构分类、准入判据、标尺、未决项、用户原话存档 |
| [`docs/experiment-log-1.md`](docs/experiment-log-1.md) | **实验日志 1（pciso 期）**：七臂 A–G、压缩器质量事故、thinking 提纯十五轮评估、缓存命中、接进 DSH |
| [`docs/experiment-log-2.md`](docs/experiment-log-2.md) | **实验日志 2（插件期）**：运行时压缩的收益形状、三个口径错、退化重复反例、逐条核过的改进项 |
| [`docs/pciso-platform.md`](docs/pciso-platform.md) | **pciso 实验台**：P/C 结构、`/v1` 代理、15 条约束、七组对照、验收数据、已知缺口、测试清单 |
| [`dsh-plugin-iot-thinking/README.md`](dsh-plugin-iot-thinking/README.md) | **插件手册**：节点格式与退化保护、设置项与档位、安装、账本、文件表、已知限制 |
| [`dsh-plugin-compaction/README.md`](dsh-plugin-compaction/README.md) | **退役插件**：为什么 `ctx.compaction` 这条路线不对（含 preset 挂载的坑） |

## 仓库结构

```text
dsh-plugin-iot-thinking/   主形态：llm/stream 块级替换（host + client 半个）
dsh-plugin-compaction/     已退役的 compaction 后端（历史/回滚参考）
src/pciso/                 前身实验平台（P/C 隔离、验证器、/v1 代理）
configs/                   实验臂 A–G 与 chat 档配置
prefixes/                  P：冻结需求包（目标载体 one_sentence_edit / 排练载体 drawing_video）
scripts/                   验收、探针与复盘脚本（verify_*、_think_*、_session_* …）
tests/                     平台测试（546 passed / 37 deselected，-m "not slow"）
docs/                      原理、设计沿革、实验日志、平台文档
experiment/  out/  state/  留档与运行期产物，不进版本库（见 .gitignore）
```

## 已知边界

- **thinking 是流式中断的**：块结束才替换，所以客户端看到的是"顿一下再出现"，不是逐字流。
  "边压边回"是明确排除的次要功能。
- **每压一块多一次模型调用**（退化时多一次严格重试）。默认走当前请求的路由，可在设置里换更便宜的。
- **不是安全边界**：插件以 host 权限运行、能读模型输出，只装可信来源。
- **压缩质量有已知失效面**：标签记账错位（该 `[决定]` 的落进 `[待定]`）、块内退化重复。
  后者已由四道闸（输入净化 / 提示词 / 低温度 + 严格重试 / 质量门 + 确定性兜底）拦住，
  但它属于"急性失败"，体积与接地率这类指标抓不到 —— 详见
  [`docs/experiment-log-2.md`](docs/experiment-log-2.md) §4.2。
