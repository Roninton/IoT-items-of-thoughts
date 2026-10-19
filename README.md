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
#    ⚠️ 填「声明了 dsh.bundle 的那一层」：子目录或仓库根都行（根 package.json 已声明），
#       但不要填 GitHub 的 /tree/main/... 子目录页地址，那是 invalid-spec
#    装完重启一次 dsh web（host 模块是新的；client 半个从 exports["./client"] 出）

# 2) 插件自测（纯逻辑，不启动 dsh、不连模型）
node dsh-plugin-iot-thinking/selftest.mjs

# 3) pciso 平台测试（可选，跑的是实验台那套）
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q -m "not slow"
```

开关与参数在 **Settings → Plugins → IoT 思考压缩**：启用/关闭、最小压缩字数（默认 700；
400–500 区间实测净亏，见插件 README）、
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

## 设计沿革与「哪些算损失」（摘要；全文见 [`docs/thinking-compression.md`](docs/thinking-compression.md)）

这份文档是**这套东西的脑**：它记的是每一处裁定的来路与代价，而不只是结论。上面那些规则之所以
长成现在这样，全在这里能找到"为什么"和"当时否掉了什么"。下面只摘最要紧的部分。

**沿革：从循环架构收窄到块级替换。** 灵感来自循环架构那一批（[LoopMoE](https://arxiv.org/abs/2606.04438)
等）：同一组权重多转几圈能把推理做深，但**能力上限由循环状态尺寸决定** —— 所以"多转"买不到
"更大的草稿纸"。抽象出来的那句是：**别把历史喂给它，让它自己把上一轮输出按结构化 schema 重新设计。**
随后一步步收窄：压缩粒度改成一次 `complete` → 位置名改叫"事实链提取" → 范围收窄到**只有 thinking**
→ 事实改由 server 渲染成 ACT → thinking 压成**正负推理节点** → 落地成**块级替换（压缩后返回）**。
最小路径最后只剩三件事：**转发 + 换上下文 + 编排思考**，工具循环 / 沙箱 / UI 全用 DSH 现成的。

**三条立场。** ①**压缩有损但并非不可逆** —— 前提是有一条只读回读通道（模型自己 read/grep 把细节取回来）；
②**拿压缩后的 C 对标原文不公平** —— 长上下文本身的注意力也是有损的，我们做的不是"无损压缩"，
而是**更好的上下文管理**；③**闸门在增长的前置位置** —— 每一段 thinking 一产生就编排，不让它先堆积到
撞阈值再收拾（"零成本闸门"：正则先判这段有没有决定/否定/未定/代价标记，没有就不调压缩器，还没做）。

**分类不按语义，按"能不能核对"分两层。** 现状分析 / 多方案并举 / 权衡 / 预演这些类别是**交错**的
（决定→预演→发现漏洞→退回权衡，一轮里能来回好几次），任何"先分类再按类压缩"都会在交错处出错。
可用的切法是：**可核对的**（写了哪个文件、什么符号、跑没跑测试）由 **server** 从落盘结果与 telemetry
确定性渲染，模型没有裁量权；**不可核对的**（权衡、被否的路线、未定项、风险）模型提，**server 按规则
验/补/丢**。这就是"幽灵事实在结构上写不出来"的实现方式。

**准入判据：什么值得占 C 的预算。** 原话：**"重要 = 下一轮会因为它走错 / 重走贵路，而且从 P、
盘上的文件、C 里其它节点都推不出来。"** 两条推论都要记住 —— ①"有否定就记"**该拦**：把 P 里现成的
约束再复述一遍（"不修改冻结测试"）推得出来，不占预算；②代价**不对称**：漏记一条会重走贵路的否决，
比多一条噪声贵得多，所以服务端兜底宁可多补一条带 `why` 的，也不要漏。

**标尺：自评只能当探针。** 实测它**三次**把原文里根本没有的东西算成"丢失"（`True` 的用例覆盖、
候选 `3599/86400`、`America/New_York` 的两个 instant —— 原文要么讨论过、要么从没算过）。
所以真正的标尺是**任务级三项**：文件重写率
（笔刷载体修前 42% → 修后 0%）、revert 次数 / 硬约束新违反（同一批 6 轮：2 → 0）、无据声明率。
逐字覆盖率降级为"上限参考"，而且**审计脚本自己也会错**（四条假阳性已修）。

**"有引导"到底有没有用（三臂对照，真机 6 样本）。**

| 臂 | 逐字覆盖 | 幻影完成态断言 | 负极性 | 待定 |
|---|---|---|---|---|
| guided（现行提示词） | **31/43 = 72%** | **0** | **9** | **2** |
| facts_only（只给落盘清单） | 28/43 = 65% | 3 | 3 | 0 |
| plain（只给原文 + schema） | 18/43 = 42% | 3 | 3 | 1 |

结论：**有效，而且三件事各买各的** —— 给信息买覆盖率（42% → 65%），给规则买口径
（负极性 3 → 9、幻影完成态 3 → 0），两者一起才是 72%。成本几乎白送（三臂**未命中** token
基本相同，规则吃前缀缓存 81%）。但也别当万能药：6 个样本里 4 胜 1 平 1 负，它买的是**口径**，
不是"每条都记得更多"。

**还没做的（诚实清单）。** step loop（一轮多段正文 → 每段一次编排）、零成本闸门、
**四臂真机对照**（`iot` / `raw` / `drop` / `full` —— "agent 能力不降甚至更强"就靠它量，**还没量**）、
"边压边回"、给"查过的 vs 推的"加置信度标记。完整清单在文档 §8。

## 文档

| 文档 | 管什么 |
|---|---|
| [`docs/principle.md`](docs/principle.md) | **原理**：核心裁定、压缩粒度为什么是"一块一次"、为什么叫"事实链提取"而不是压缩、范围边界、触发点与代价 |
| [`docs/thinking-compression.md`](docs/thinking-compression.md) | **设计沿革与裁定来路（上面有摘要，这份是全文）**：LoopMoE 那批论文的逐条映射、thinking 块的结构分类与"按能否核对分两层"、准入判据、标尺与审计脚本的假阳性、三臂引导对照、未决项、用户原话存档 |
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

## 关于测试的边界，以及可以怎么参与

这个项目是**业余时间做的**，测试做到哪一步，说清楚比说漂亮更有用。

**已经做过的**：平台测试 546 项（`-m "not slow"`）、插件纯逻辑自测全通过；
更关键的是**真机会话逐条核对** —— 每个"它说丢了 X"都回去翻原文、翻盘上的文件，
核得动的修机制，核不动的记成框架偏差。因此现在这条链上，**已知的失效模式大多是被真机抓出来、
再补上闸门的**（块内退化重复、标签记账错位、`[待定]` 里装已定内容，都是这么来的）。

**没做到的**：受时间和成本限制，**完整的对照试验与消融试验没有跑**。这意味着：

- 现有数字多来自**单会话量级估计**（真实任务里 thinking 约占上下文 14.9%；提纯比落在 0.11~0.32），
  可以说"这个量级上有收益"，不能说"这是分布定律"；
- **"agent 能力不降甚至更强"目前只是设计与观察，不是已量化的结论** ——
  四臂真机对照（`iot` / `raw` / `drop` / `full`）仍是未决项；
- **藏得深的 bug 难免还有**。有些问题只在长会话、特定草稿形态下才现形，
  自测全绿也挡不住，需要真实用法才能撞出来。

**怎么参与**：欢迎**"以用待测"** —— 正常用起来本身就是在替这个项目做测试。
如果怀疑哪一段被压坏了，请把 `$DSH_HOME/plugin-data/iot-thinking/events.jsonl` 里的对应事件
和 `originals/` 下的原文草稿一起带上；有原文才判得出"是压缩丢了"还是"原文本来就没写"。

**如果暂时不想承担这份不确定性**，也完全合理：可以先关注、等社区反馈，等这些项收敛了再跟进。
这个项目不着急。
