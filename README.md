# IoT-items-of-thoughts

**IoT = Items of Thoughts。** 把模型的 thinking block 在它**结束的那一刻**按某个**档位**改写，
**原地替换**后返回 —— 正文、`tool_calls`、`usage`、`finish_reason` 与原生行为一模一样。

**IoT 的本质不是"压缩"。** 压缩只是**决策型任务**需要的那种改写。所以改写方式是可选的
**档位**，并且可以**按会话 / 按 agent preset** 各选一档：

| 档位 | 目标函数 | 替换规则 |
|---|---|---|
| `iot-compact` / `iot-balanced` / `iot-full` | 压成带标签的决策节点（`[支持] [反对] [权衡] [待定] [计划] [决定] [风险] [中性]`） | **必须更短** |
| `custom` | **你自己写的一份文本**（预设目录里的 `.md`，或设置卡里贴进来的一段） | 默认"不是原样即可"，压缩型可配"必须更短" |
| `off` | 原文不动 | 永不替换 |

除这三档 + `custom` 之外**不内置**别的档位。没有真机数据支撑的改写方式（长程研究、
角色内心这类）不配和基础档平起平坐 —— 它们应该以**自定义预设**的形式存在。
[`inner-voice`](../inner-voice/) 就是这样一份 preset（现在只剩
[`presets/character-inner-voice.md`](../inner-voice/presets/character-inner-voice.md) 这份文本）。

解析顺序写死：`sessions[<sessionId>]` > `agentPresets[<preset id>]` > `strategy`（全局默认）。
三个入口写的是同一张表：**对话顶部下拉**、**`/iot <档位>`**（`/iot custom:<名字>`、
`/iot auto` 清除覆盖）、**设置卡**。

压掉的只是填充（`let me…` / `hmm…` / 把用户要求再复述一遍），保留的是**关键推理节点**：
正节点（这条路线为什么成立）与**负节点**（哪条路被否了、为什么否）。动机一句话：
**不让思维链里有价值的信息被淹没在 thinking 长文本下，提纯，且降本。**
负节点是长程任务里最贵的一类事实 —— 它拦住的是"重新走一遍已经被否掉的路"。

## 为什么 per-preset 只能写在插件内部

`llm/stream` 是 `ctx.waterfall`，**不过 scope filter**（对比 `system-prompt/assemble`
是明确 scope-filtered 的）。所以：

- 把插件挂在某个 preset 的隔离组里**不会**限定它的作用面 —— 钩子照样对整进程每个请求触发；
- 两个插件同时挂 `llm/stream` 会形成 waterfall 嵌套，外层拿到的 chunk 已经含内层吐出的东西，
  语义没有定义过。

正确做法是**一个钩子内部按会话身份分派**。身份拿得到：`sessionId` 在 `GenerateOptions` 上；
`agentPreset` 在**会话投影**里（`key: "agentPreset"`，`init: header.agentPreset`，
`apply: agent-preset/selected`），用
`ctx.get("sessions").get(sessionId)` + `ctx.get("sessionProjections").stateOf(session, "agentPreset")` 读，
会话一创建就有值。本机实测会话头即 `{"agentPreset":"standard"}`。

这也正是"把角色档做成一种预设"的来由：**预设是数据，不是挂载位置**。

## 现状：DSH 插件模式

| 形态 | 目录 | 状态 |
|---|---|---|
| **运行时 thinking 改写插件**（主形态） | [`dsh-plugin-iot-thinking/`](dsh-plugin-iot-thinking/) | **在用**（v0.2）。挂在 DSH 的 `llm/stream` waterfall 上：块开始时放行、reasoning 增量先扣住、块结束时按档位改写、回一个 `reasoning-delta` + 改写的 `block-end`。**不 provide `ctx.compaction`**，所以没有 checkpoint 消息、没有"上下文已压缩"块，preset 会话里同样生效 |
| 角色档的一种写法（示例预设） | [`../inner-voice/`](../inner-voice/) | 不是插件、不是内置档位。仓库里留着它的**实现基线**与一份可直接用的预设文本 [`presets/character-inner-voice.md`](../inner-voice/presets/character-inner-voice.md)；删掉内置 `voice` 档时一并写明了代价（没有 tic / collapse / bans 闸门） |
| 内置 compaction 形态 | [`dsh-plugin-compaction/`](dsh-plugin-compaction/) | **已退役**（2026-10-18）。走 `ctx.compaction`，会把一段历史换成 checkpoint 消息，模型读到后当成新指令重新定位。留作历史与回滚参考 |
| pciso 实验平台（前身） | `src/pciso/`、`configs/`、`prefixes/`、`scripts/`、`tests/` | 保留、测试仍在跑（见 [`docs/pciso-platform.md`](docs/pciso-platform.md)）。它是**实验台**：离线可复现、可回放、可故障注入，用来量"约束违反数随轮数"与"prompt 成本" |

插件是外部 bundle：`package.json` 声明 `dsh.bundle.patch`，装进 profile 后
`cordis.patch.yml` 那一行自动成为配置层的一部分。

## 快速开始

```powershell
# 1) 装插件（DSH 0.2.x / node >= 20）
dsh plugin --profile web add "<repo>\dsh-plugin-iot-thinking"
#    也可以走 UI：设置 → 插件 → 添加插件 → 填上面那个绝对路径
#    ⚠️ 填「声明了 dsh.bundle 的那一层」：子目录或仓库根都行（根 package.json 已声明），
#       但不要填 GitHub 的 /tree/main/... 子目录页地址，那是 invalid-spec
#    ⚠️ 走 GitHub 地址安装前先看插件 README 的「中国网络」一节：直连 github.com:443
#       实测会等 109 s 后超时，需要给 git 和 profile 的 .npmrc 都配代理
#    装完重启一次 dsh web（host 模块是新的；client 半个从 exports["./client"] 出）

# 2) 插件自测（纯逻辑，不启动 dsh、不连模型）
node dsh-plugin-iot-thinking/selftest.mjs

# 2b) 接线自测（真插件入口 + 真 dsh-llm seam，假的 ctx 与模型调用；需要盘上有 dsh 安装）
node dsh-plugin-iot-thinking/smoke.mjs

# 3) 看某一档真正发给模型的提示词（不需要 dsh 在跑）
node dsh-plugin-iot-thinking/show.mjs iot-full

# 4) pciso 平台测试（可选，跑的是实验台那套）
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q -m "not slow"
```

开关与参数在 **设置 → 插件 → IoT 思考改写**：启用/关闭、**默认档位**、**逐档限制**
（`minChars` / `maxChars` / `maxTokens` / `maxNodes` / `maxNeutral`）、**按 agent preset 映射**
（"当 preset 是 X → 就用 Y"）、第 4 项自定义预设的目录与内容、可选 provider/model、
**改写器推理档位**（`reasoningEffort`，默认 `auto`）。
配置写到 `$DSH_HOME/plugin-data/iot-thinking/config.json`，每次请求按 mtime 读，
**改完下一个模型请求就生效，不用重启**。卡片是"改完再保存"的，未保存时有明确标记。

`auto` 档位的意思是：先发 `off`（改写器不花预算在自己的 thinking 上），宿主拒绝该档位时
改用该路由公开的 `minimal`/`low`，没有就不发这个字段、由适配器取默认值。旧实现把 `off`
写死，于是不接受该档位的路由（pi-ai 一类）**改写一次都没成功过**，而失败按设计静默保留
原文 —— 界面上只表现为"没效果"。运行中可点卡片上的**一键测试**：它用选中的档位真跑
一次，并按与真实链路相同的替换规则判定；关着插件、输入太短、草稿已判为循环重复时
它会直接说"真实链路会跳过"，不报绿。

## 自定义预设（第 4 项）

预设就是**一份文本**：告诉改写器"把 thinking 改成什么样"。插件**开局是空的**。

```text
<你填的 presetDir>/*.md | *.txt    # 文件名即预设名（带后缀）；目录必须自己填
```

- 值域：`custom`（设置卡里贴入的那段）/ `custom:<文件名>`（目录里的文件，带后缀，如 `custom:my-style.md`）；
- 提示词：默认"你的文本是指令体"，引擎把草稿包进 `<thinking>`；写了 `{{draft}}`
  就按整段模板处理；
- 替换规则 `presetAccept`：默认 `different`，压缩型预设改 `shorter`；
- 质量门只有四道最小闸：`empty` / `unchanged` / `too-thin` / `degenerate`。

要一份现成的角色内心档，抄 [`inner-voice/presets/character-inner-voice.md`](../inner-voice/presets/character-inner-voice.md)，
放进上面的目录，然后 `/iot custom:character-inner-voice`。

## 怎么验证它真的生效了

**只有插件账本是可靠证据**（会话日志里看见的紧凑块，分不清是插件压的还是模型自己写短的）：

```text
$DSH_HOME/plugin-data/iot-thinking/events.jsonl          # 事件账本
$DSH_HOME/plugin-data/iot-thinking/originals/<sid>/<time>-block<index>.txt   # 替换前的原文归档
```

事件：`constructed` / `strategy-switched` / `block-replaced` / `block-kept` / `block-skipped` /
`block-failed` / `thinking-rewritten` / `thinking-failed` / `thinking-skipped` /
`thinking-degenerate` / `thinking-rejected` / `thinking-effort-fallback` /
`thinking-effort-unsupported` / `thinking-effort-probe-failed` / `thinking-self-test`。
每条改写事件带 `strategy`，可以按档位统计效果；`block-replaced` 还带形态学指标
（基础三档 `nodes`，自定义预设 `lines` / `fragmentRate`）；`thinking-failed` 带宿主的
`code`（如 `UNSUPPORTED_REASONING_EFFORT`）；`thinking-skipped` 的 `reason: "aborted"`
表示是你自己按了 Esc，不是插件出错。替换前的原文只存在 `originals/` 里。

## 关键裁定（完整版见 [`docs/principle.md`](docs/principle.md)）

| 裁定 | 为什么 |
|---|---|
| **只改写 thinking**，正文与动作块一个字节不动 | 代码块、read/edit 的执行结果是**事实块**，动它只会失真、误判、丢细节 |
| 压成**正负推理节点**（基础三档），不是散文摘要 | 推理不是没了，是去掉填充；负节点决定了下一轮不会重走已否掉的路 |
| 事实**由 server 渲染**，不由模型编 | 谁写了哪个文件、跑没跑测试，本来就有 sha / 退出码；让模型经手，"把计划记成事实"才可能出现 |
| 触发点 = **thinking block 结束** | 这是唯一的稳定锚点；块边界用**位置法**，不按文本猜 |
| 失败就**回原文** | 宁可不改，不可丢。改写器失败 / 输出为空 / 不满足替换规则，都原样放行 |
| **只有三档是验证过的** | 其余改写方式没有真机数据，按"用户自定义文本"提供，不冒充内置能力 |
| **模型自述只能当探针，不能当判据** | 实测它三次把原文里根本没有的东西算成"丢失"。有丢失就一定有原因，找得出来才算数 |

## 文档

| 文档 | 管什么 |
|---|---|
| [`docs/principle.md`](docs/principle.md) | **原理**：核心裁定、压缩粒度为什么是"一块一次"、为什么叫"事实链提取"而不是压缩、范围边界、触发点与代价 |
| [`docs/thinking-compression.md`](docs/thinking-compression.md) | **设计沿革与裁定来路**：LoopMoE 那批论文的逐条映射、thinking 块的结构分类与"按能否核对分两层"、准入判据、标尺与审计脚本的假阳性、三臂引导对照、未决项 |
| [`docs/experiment-log-1.md`](docs/experiment-log-1.md) | **实验日志 1（pciso 期）**：七臂 A–G、压缩器质量事故、thinking 提纯十五轮评估、缓存命中、接进 DSH |
| [`docs/experiment-log-2.md`](docs/experiment-log-2.md) | **实验日志 2（插件期）**：运行时压缩的收益形状、三个口径错、退化重复反例、逐条核过的改进项 |
| [`docs/pciso-platform.md`](docs/pciso-platform.md) | **pciso 实验台**：P/C 结构、`/v1` 代理、15 条约束、七组对照、验收数据、已知缺口、测试清单 |
| [`dsh-plugin-iot-thinking/README.md`](dsh-plugin-iot-thinking/README.md) | **插件手册**：档位表与三层路由、自定义预设、设置项、看原文、安装、账本、文件表、已知限制 |
| [`dsh-plugin-compaction/README.md`](dsh-plugin-compaction/README.md) | **退役插件**：为什么 `ctx.compaction` 这条路线不对（含 preset 挂载的坑） |

## 仓库结构

```text
dsh-plugin-iot-thinking/   主形态：llm/stream 块级改写（host + client 半个）
  lib/strategies.mjs         档位注册表（三档基础 + custom + off）
  lib/route.mjs              三层解析：会话覆盖 > agent preset 映射 > 全局默认
  lib/custom.mjs             自定义预设：目录 / 读取 / 另存为 / 提示词拼装
  lib/gates.mjs              自定义预设的四道最小闸
  show.mjs                   不启动 dsh 的文本视图（看某一档到底发什么）
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
- **每改写一块多一次模型调用**（被拒时多一次带针对性指令的重试）。默认走当前请求的路由，
  可在设置里换更便宜的。
- **不支持 `off` 档位的路线上，改写器会自己思考**：插件降级为不发 `reasoningEffort`，
  代价是每块多 10–20 s。想避免就显式选该路由公开的低声档。
- **不是安全边界**：插件以 host 权限运行、能读模型输出，只装可信来源。
- **基础三档的质量有已知失效面**：标签记账错位（该 `[决定]` 的落进 `[待定]`）、块内退化重复。
  后者已由四道闸拦住，属于"急性失败"，体积与接地率这类指标抓不到 —— 详见
  [`docs/experiment-log-2.md`](docs/experiment-log-2.md) §4.2。
- **自定义预设没有专门闸门**：只有 empty / unchanged / too-thin / degenerate 四道最小闸。
  角色档那套 tic（口头禅）/ collapse（塌缩比例）/ bans（禁词）**已经不内置**了；
  禁词可以写在预设文本里，但机器不替你拦。这是把"未验证的改写方式"退回用户自定义
  所付的代价，写在这里而不是藏在代码里。

## 关于测试的边界，以及可以怎么参与

这个项目是**业余时间做的**，测试做到哪一步，说清楚比说漂亮更有用。

**已经做过的**：平台测试 546 项（`-m "not slow"`）、插件纯逻辑自测与接线自测全通过；
更关键的是**真机会话逐条核对** —— 每个"它说丢了 X"都回去翻原文、翻盘上的文件，
核得动的修机制，核不动的记成框架偏差。因此现在这条链上，**已知的失效模式大多是被真机
抓出来、再补上闸门的**（块内退化重复、标签记账错位、`[待定]` 里装已定内容，都是这么来的）。

**没做到的**：受时间和成本限制，**完整的对照试验与消融试验没有跑**。这意味着：

- 现有数字多来自**单会话量级估计**（真实任务里 thinking 约占上下文 14.9%；提纯比落在 0.11~0.32），
  可以说"这个量级上有收益"，不能说"这是分布定律"；
- **"agent 能力不降甚至更强"目前只是设计与观察，不是已量化的结论** ——
  四臂真机对照（`iot` / `raw` / `drop` / `full`）仍是未决项；
- **自定义预设没有任何量化数据**：它是一份用户文本，好坏取决于写它的人。别指望它
  "天然更好"，先用账本看它到底改成了什么。

**怎么参与**：欢迎**"以用待测"** —— 正常用起来本身就是在替这个项目做测试。
如果怀疑哪一段被改坏了，请把 `$DSH_HOME/plugin-data/iot-thinking/events.jsonl` 里的对应事件
和 `originals/` 下的原文草稿一起带上；有原文才判得出"是改写丢了"还是"原文本来就没写"。

**如果暂时不想承担这份不确定性**，也完全合理：可以先关注、等社区反馈，等这些项收敛了再跟进。
这个项目不着急。
