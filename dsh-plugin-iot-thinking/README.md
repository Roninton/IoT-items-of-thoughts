# dsh-plugin-iot-thinking

IoT (Items of Thoughts) for **DeepSeek Harness 0.2.x**，以 **external bundle** 形式安装
（设置 → 添加插件填绝对路径，或 `dsh plugin add`）。

**IoT 的本质不是"压缩"，是"在 thinking 块结束那一刻改写它"。** 压缩只是决策型任务
需要的那种改写。所以改写方式是可选的**压缩强度**，并且可以**按会话 / 按 agent preset** 指定：

| 压缩强度 | 目标 | 判定"能不能替换" |
|---|---|---|
| `iot-compact` / `iot-balanced` / `iot-full` | 压成带标签的思维节点（3 / 5 / 8 标签） | **必须比原文短** |
| `custom` | 用户自己写的文本（预设目录里的 `.md`，或设置卡里贴进来的一段） | 默认"不是原样即可"；压缩型预设可配"必须更短" |
| `off` | 保留原生 thinking，一个字节都不改 | 永不替换 |

建议（内置 / 外部 / 关闭）是**另一个维度**，与压缩强度自由组合，见「三个正交维度」一节。

**除这三档 + `custom` 外不内置别的档位。** 没有真机数据支撑的改写方式（长程研究、
角色内心这类）不配和基础档平起平坐 —— 它们该以**用户配置**的形式存在。
`inner-voice` 就是这样一份 preset（见 [`../../inner-voice/presets/character-inner-voice.md`](../../inner-voice/presets/character-inner-voice.md)）。

三层解析顺序（写死，没有"看情况"）：

```text
sessions[<sessionId>]  >  agentPresets[<agent preset id>]  >  strategy（全局默认）
```

## 会话归档与 Session 记忆（开发版）

新增 `sessionLog` 与 `sessionMemory` 开关（默认 true，独立于改写档位）。Log 自动按会话/轮次保存原始思考、IoT 处理与增强、正文、工具事件；不复制每次请求的全量历史。当前仅POSIX实现，Windows归档拒绝且保留原始思考。

`iot_session_memory` 提供状态总览/关键记忆的读写、检索、带revision的修改与逻辑删除；`iot_session_log` 提供原始轮次list/search/read。随包的 [iot-session-memory Skill](skills/iot-session-memory/SKILL.md) 通过宿主 skills 注册，教模型先读记忆、不足再读Log。记忆删除不删除日志或历史修订。

完整设计、目录、批量持久化语义与局限见仓库 [Session 归档与分层记忆](../docs/session-memory.md)。测试：`npm run test:archive`；当前Desktop隔离集成测试：`ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" session-host-test.mjs`。真实DGX原思/处理/增强归档已验证；自然自主召回准确率、Windows和长期磁盘压力仍未完成。

## 它修的是什么

上一代 `iot-compaction` 走 `ctx.compaction`：dsh 的 compaction 会换掉一段**历史区间**并
追加一条 checkpoint 消息，模型把它当成新一轮用户输入：

> 上下文已压缩 · 已压缩 10 条历史记录（约 5002 tokens）

然后开始想"上下文被压缩了，我要重新定位"。那不是 IoT 的规则。IoT 是：
**thinking 块结束时把这块改写，原地替换。**

## 它怎么做到的

dsh 把 `llm/stream` 暴露成 **waterfall**，包住每一次模型调用；适配器吐原始 chunk，
提交进会话的 assistant message 由 waterfall 吐出的东西拼成。这个插件：

1. 放行 `block-start`（thinking 块正常开始）；
2. 按块下标扣住 `reasoning-delta`；
3. 块 `block-end` 时，按当前档位改写整段草稿；
4. 吐**一个**改写后的 `reasoning-delta` + 改写的 `block-end`；
5. 正文增量、工具调用、usage、finish 一律原样透传。

因为替换发生在 assistant message **提交之前**，所以没有第二条消息、没有 checkpoint、
没有 `compaction/start`，也没有那行"上下文已压缩"。它同样在 preset 会话里生效，
因为钩子挂在 LLM 层而不是 preset 局部的 compaction realm。

改写失败 / 被关掉 / 输出为空 / 不满足该档位替换规则（比原文长、或原样没改）时，
原来的 delta 与 block 原样放行。

### 为什么路由必须写在钩子内部

`llm/stream` 是 `ctx.waterfall`，**不过 scope filter**（对比 `system-prompt/assemble`
是明确 scope-filtered 的）。插件哪怕挂在某个 preset 的隔离组里，钩子照样对整进程
每个请求触发；两个插件同时挂上去会形成 waterfall 嵌套，外层拿到的 chunk 已经含
内层吐出的东西 —— 语义没有定义过，而且每块会调两次模型。

所以"谁用哪一档"只能在**同一个钩子内部**读会话身份再分派。身份是拿得到的：

- `sessionId` 在 `GenerateOptions` 上；
- `agentPreset` 不在 `GenerateOptions` 上，但在**会话投影**里（`key: "agentPreset"`，
  `init: header.agentPreset`，`apply: agent-preset/selected`），用
  `ctx.get("sessions").get(sessionId)` + `ctx.get("sessionProjections").stateOf(session, "agentPreset")`
  读，会话一创建就有值。

任何一步取不到都只意味着"少一层映射"，会话覆盖与全局默认仍然有效。

## 三个入口（写的是同一张表）

| 入口 | 作用 |
|---|---|
| 对话顶部下拉 | 这个会话用哪一档（写 `sessions[sessionId]`）。**默认不显示**，要在卡片里打开「会话级档位」；打开后新会话也有：先记住选择，等会话出现（发第一条消息）时自动落盘 |
| 会话内 `/iot <档位>` | 同上；`/iot custom:<文件名>` 指定自定义预设；`/iot auto` 清除覆盖。**始终可用**，不受上面那个开关影响 |
| 设置卡（设置 → 插件 → IoT 思考改写） | 压缩强度、用户注入、逐档限制、按 agent preset 映射、用户配置的目录与内容 |

优先级：**会话自己的选择 > agent preset 行 > 全局默认**。

> 为什么默认关：per-session 覆盖是少数场景的需求，而那行控件贴在每个会话标题旁，
> 很容易让人以为"档位归它管"。默认只留全局默认 + preset 映射这两层；需要时会话里
> 用 `/iot` 也能切，想要常驻下拉再去卡片里打开。

## 三个正交维度（别把它们当成档位组合）

插件里只有三个互相独立的维度，界面上也是分开的：

| 维度 | 取值 | 作用 |
| --- | --- | --- |
| **压缩强度** | 极简 / 均衡 / 完整 / 关闭 | 决定压缩后保留多少标签与节点 |
| **建议** | 关闭 / 内置 / 外部 | 内置 = 压缩器自己多输出 1~2 条 `[旁观者建议]`；外部 = 另发一路强模型 |
| **用户注入** | 不注入 / 内置预设 / 目录文件 / 贴入文本 | 追加在插件固定提示词**后面**的一层，只能补充，不能替掉标签规则 |

所以「压缩 · 完整 + 内置建议」= 8 标签压缩 + 压缩器顺带给出建议；「压缩 · 完整 + 外部建议」=
8 标签压缩 + 另发一路强模型给建议。**这是组合，不是两个档位。**

旧档位 `iot-guided` 已折叠成别名：它等价于「压缩 · 完整 + 内置建议」。读到旧值仍能工作
（全局、会话覆盖、preset 映射里写了都一样），但界面上不再出现这个档位——同一件事只留一种表达。

旧档位 `custom` 也不再是第四档：它迁移成「压缩 · 完整 + 用户注入」。老配置、老会话覆盖、
老 preset 映射里写了 `custom` 仍能工作，内容（内置预设 / 目录文件 / 贴入文本）继续生效，
但语义从"替代内置提示词"变成"追加在它后面"。**注入是叠加，不是替代。**

`off` 档不调用改写模型，所以注入在那时**不生效**——这是 off 的定义，不是 bug。

```jsonc
"inject": ""                       // 不注入
"inject": "builtin:standard"       // 内置预设
"inject": "custom:my-style.md"     // 目录里的文件
"inject": "inline:<你的文本>"      // 直接贴一段
```

## 用户配置（自定义预设）

预设就是**一份文本**：告诉改写器"把 thinking 改成什么样"。插件**开局是空的**。

- **来源**：`presetDir` 里的 `.md` / `.txt`，或设置卡里贴进来的一段。
  **目录必须自己填，没有默认路径**；留空时文件类预设不可用，贴入的文本仍可用；
- **值域**：`custom`（贴入的那段）/ `custom:<文件名>`（目录里的文件，**名字带后缀**，
  例如 `custom:my-style.md`、`custom:notes.txt`）；
- **界面上是两级**：先选压缩强度（三档 + 用户配置 + 关闭），选到「用户配置」后才出现
  第二级下拉挑具体文件 —— 不把每个预设文件铺进档位下拉；
- **另存为**：文件名带后缀就按那个后缀存（`.md` / `.txt` / `.markdown`），
  不写后缀自动补 `.md`；
- **提示词约定**：默认"你的文本是指令体"，引擎把草稿包进 `<thinking>`；
  文本里写了 `{{draft}}` 就按**整段模板**处理（引擎不再追加任何包装）；
- **替换规则**：`presetAccept`，默认 `different`（只要不是原样就采用），
  压缩型预设可设 `shorter`（必须更短才替换）；
- **质量门**：只有四道最小闸 —— `empty` / `unchanged` / `too-thin` / `degenerate`。
  没有禁词、口头禅、塌缩比例这类判定：那些需要一份规格才说得清，而预设是一段自由文本，
  引擎没有资格替用户猜它的约束。要更严，就在预设文本里自己写规矩。

## 设置项与限制

设置卡里每一项都是"改完再保存"：控件只改内存，点**保存**才写进 `config.json`，
未保存时按钮变成「保存 *」。落盘后**下一个模型请求**生效，不用重启。

- **启用 / 关闭** — 全局总闸（单会话静音请用 `/iot off`）；
- **默认档位** — 没被 preset / 会话覆盖时用哪一档；
- **逐档限制** — `minChars` / `maxChars` / `maxTokens` / `maxNodes` / `maxNeutral`；
- **按 agent preset 映射** — "当 preset 是 X → 就用 Y"。渲染成一张**条件 → 结论**表，
  因为这里只有一层选择：preset 是条件，档位是结论；
- **temperature 可配且会自动降级**：`temperature` 留空 = 不发该字段；有些模型只接受默认值
  （例如 GPT-6 Astra 只认 1，发 0.2 直接 400）。被拒时会去掉该字段重试一次并记住这条路由，
  不会每块都白打一次调用。指导模型同样有 `advisor.temperature` 与同一套降级。
- **推理档位（`rewrite.effort`）** — `auto` 默认：先发 `off`（改写器不花预算在自己的
  thinking 上），宿主拒绝该档位就改用该路由公开的 `minimal`/`low`，没有就不发这个字段。
  决策按 `(provider, model, 档位)` 记住，同一路由不会再白试一次；
- **压缩器路由（算力卸载）** — 见下一节：可以指定压缩调用走哪条路由，也可以按档位覆盖；
- **内置预设** — 两个只读模板（决策骨架 / 记忆锚点），点选即设为全局默认；
- **会话级 prompt** — custom 档位下可按会话指定规格，默认跟随全局。

### 内置预设（开箱即用）

不想自己写规格时，设置卡里「内置预设」有两个可直接点选的只读模板：

| id | 名字 | 适用 |
|---|---|---|
| `standard` | 标准结构化压缩（决策骨架） | 大多数长思考：保留结论/权衡/风险/待定/计划，压掉推导与试错 |
| `deep` | 超长上下文深度压缩（记忆锚点） | 极长思考链、长对话续推：只留结论/约束/权衡/计划四类锚点 |

它们随插件发布，**不需要 presetDir**。正文可以直接在卡片里编辑：改过的内容存在
`builtinOverrides` 里，随时能「恢复默认」；没改时用插件自带文本。
内置预设的替换规则固定为 `shorter` —— 一条不比原文短的输出不该替换掉整块 thinking。

点「用这个」会把它写成「用户注入」并写入 `defaultPreset`；
全局默认预设只在 custom 档位、且没有会话/preset 映射覆盖时生效 —— 档位不是
用户注入时，`defaultPreset` 会被静默忽略（这是配置层的既有语义，不是 bug）。

⚠️ 已知坑（已修）：旧版设置卡从别的档位切到「自定义预设」时会沿用旧档位的值
（`targetOf("iot-balanced")` 返回 `"iot-balanced"`），结果 `strategy` 没切过去，
看起来像"选了预设但不生效"。现在只在 target 确实是 custom 形态时才沿用。

### 会话级 prompt

会话覆盖不只是档位：custom 档位下可以单独指定这个会话用哪份规格（内置预设、
目录里的文件、贴入的文本），默认跟随全局配置。

```text
sessions: {
  "<sessionId>": { "strategy": "custom", "preset": "builtin:deep" }
}
```

- 头部下拉只放压缩强度与建议；用户注入在设置卡里选（它是全局叠加层，不按会话分）。
- `/iot preset:<名字>`：内置预设优先；没命中就当目录里的文件名。
- `/iot custom:<文件名>` 仍然可用，等价于 `preset: "custom:<文件名>"`。
- 旧形态 `"custom:名字"` 字符串仍读得懂；新写入统一成对象形态。
- 未知内置名会被拒绝（400），不会静默落盘。

### 指导（两种模式 + 会话开关）

指导是**建议型**的：给当前工作模型额外的思路与视角，不是命令。

**模式一：内置指导**（`guidance.mode = "builtin"`）
压缩器在同一次调用里以**旁观者**身份，额外输出 1~2 条 `[旁观者建议]`。本质仍是压缩，
成本与普通档相同；`[旁观者建议]` 不参与节点预算。

> 标签为什么叫"旁观者建议"而不是"指导"：外部模型只看到这一轮草稿，它是旁观者，不是老师。
> 标签本身就在告诉主模型：这是建议，不是命令。旧的 `[指导]` 输出仍会被识别并改名。

**模式二：外部指导**（`guidance.mode = "external"`）
与压缩并发两路：原始 thinking 草稿同时发给压缩模型和一个更强的模型，两路结果合并。
强模型只看 thinking，不看完整对话。

```jsonc
"guidance": { "enabled": true, "mode": "external" },
"advisor": {
  "route": { "provider": "strong-provider", "model": "strong-model" },
  "effort": "auto",
  "wait": true,
  "maxTokens": 1200,
  "maxAdvice": 3,
  "maxChars": 600,
  "timeoutMs": 30000
}
```

**优先级**：`sessions[会话] > guidance`。设置里开启后，新会话默认继承；会话内还能单独改：

```jsonc
"sessions": {
  "<sessionId>": { "strategy": "iot-full", "guidance": { "enabled": true, "mode": "builtin" } }
}
```

- `guidance: null` = 跟随全局；
- 只写 `enabled` 时模式继承全局；
- 清除档位不会清掉指导覆盖，除非显式传 `guidance: null`。

**输出预算**：`advisor.maxTokens` 不配时按 `maxChars` 推算（中文约 2 token/字），
避免固定预算把建议截成空正文。

**可选建议（`advisor.wait: false`）**：压缩一完成就继续推理，建议没赶上就跳过
（记 `advisor-skipped: not-ready`）。慢的指导模型不该拖住整个任务流程；默认 `true`（等待）。

**指导模型与档位**：设置卡按 provider 拉该路由的模型清单与真实档位，从列表里选。
`advisor.effort` 与压缩器同语义：`auto` 先试 `off`，被拒就探这个路由公开的最低档，
再被拒就不发该字段，并把结论按 route+mode 记住；显式档位被拒时不擅自换档。

**自测**：设置卡可以点名测「内置建议」或「外部建议」，返回的报文日志（trace）包含每次调用
实际发出的提示词、原始输出、finish 与 usage，便于比对两路行为。

事件：`advisor-called` / `advisor-ready` / `advisor-injected` / `advisor-skipped` / `advisor-failed` /
`advisor-usage`。

### 教师增强（模型主动咨询）

按块自动触发建议有个根本问题：**重要性是任务状态，不是文本特征**。长会话里 `[决定]`/`[风险]`
早就出现过，规则会误触发；用户回一句「继续」也不是新需求。所以时机判断交给**正在做这件事的模型**。

插件注册一个声明稳定的工具；会话开关在执行入口生效：

```
iot_consult_teacher(depth, ask, goal, progress)
```

- `depth`（1-5）：要几轮历史上下文，**由模型决定**；插件按它抽取并固定格式化；
- `ask`：`advice` / `plan` / `framework` / `approach` / `review`，决定教师输出的结构；
- `goal` / `progress`：需求与进展综述。

**插件负责的三件事**（这是它比"用户直接调强模型"可靠的原因）：

1. **系统提示词骨架固定**——角色、边界、输出纪律由插件写死，模型只能填槽位；
2. **上下文由插件组织**——只取用户消息与助手结论正文，按 `inputBudget` 裁剪，不发送推理块或工具调用块；
3. **冷却闸门（CD）**——成功返回后进入冷却；状态通过 `iot-thinking.teacher` 运行时上下文快照提供，不再写入 thinking。

**新 Session 如何使用**：实际工具声明提供用途、参数类型、`ask` 枚举与返回说明，
不依赖旧会话历史，也不需要先加载单独 Skill。开启会话另获不超过 180 字的运行时上下文，
说明外援用途、参数入口和当前可用/CD 状态。它是宿主的 user-role 动态上下文，不是 system-role 段。
未启用、缺会话身份、或宿主抑制可选运行时上下文时不注入；工具仍由宿主工具过滤策略决定是否可见。

最小调用示例（depth 以该会话公布范围为准）：

```json
{"ask":"review","depth":3,"goal":"检查迁移方案","progress":"已完成设计，尚未执行"}
```

注入文本使用 `interpolate:false`；上下文回调不调用模型、不遍历历史。2026-11-13 曾怀疑上下文注册导致卡死，但真实 Cordis/SystemPrompt 隔离测试未复现，不能把相关性写成根因；后续确认的本地长卡顿来自 Session 历史归档逐事件锁与 fsync，已改为批量归档。运行时上下文现已恢复，并由入口、纯逻辑及真实宿主契约测试覆盖。读取配置异常时上下文 fail-open；配置存储本身会保留损坏原文件，热更新使用最近一次有效配置，冷启动则关闭插件，绝不以默认启用配置覆盖用户字节。

CD 按**块**计量，只暴露冷却、不暴露总额（避免"不用白不用"的心理）。同一会话同时只能有一次教师咨询；并发调用在本地拒绝，不会重复请求模型。失败、取消与超时会释放预约且**不进**冷却——一次没生效的咨询不该让模型付出代价。教师跟随会话路由时按 session 隔离，绝不借用另一会话最近的 provider/model；会话释放时同步清理路由、冷却和最近结果状态。

```jsonc
"advisor": {
  "teacher": {
    "enabled": false,        // 全局默认；会话可以单独覆盖
    "cooldownBlocks": 3,
    "depthRange": [1, 5],
    "inputBudget": 12000,
    "maxTokens": 1500,
    "timeoutMs": 30000
  }
}
```

**全局默认 + 会话开关**：设置卡里有全局开关；每个会话还能单独覆盖（会话覆盖表 / 对话头部下拉）。
全局没开时会话也能勾上——只影响那个会话。会话只能改开关，冷却/深度/预算仍走全局。
教师选择只修改教师字段，不修改压缩档位、自定义规格或指导开关；新会话中连续选择也会合并保留。
工具定义始终稳定保留在列表中，不随会话开关增删，避免影响提示词前缀缓存。
未启用的会话如果调用工具，会在执行入口按有效配置立即返回拒绝，不查教师路由、不提取历史、不调用外部模型。
插件总闸 `enabled:false` 会禁用教师；压缩档位 `off` 与教师开关独立，教师开启时仍可主动咨询。
“已添加工具”仅表示工具列表变化，不代表已经调用教师。

```jsonc
"sessions": {
  "<sessionId>": { "teacher": { "enabled": true } }   // null = 跟随全局
}
```

**与外部建议互斥**：咨询过教师的那一轮（以及紧接着的下一块）不再追加外部建议——
一次咨询就是一次对话，没必要在同一轮里再打一次强模型。账本记 `advisor-skipped: teacher-consulted`。

**上下文只用正文**：教师拿到的是最近几轮的用户消息与助手**正文**。thinking 是"为了产出正文"
服务的中间产物，不发给教师——它最贵，而且对"任务现在到哪了"没有帮助。

咨询是**同步**的（模型主动调用，代价由它自己承担），但必须有终点：超时返回明确结果，
不会把主任务挂死。

事件：`teacher-tool-registered` / `teacher-consulted` / `teacher-failed` /
`teacher-context-error`。

⚠️ 指导模型与工作模型相同（或同源）时收益有限；要发挥"老师"作用，应指向一个**不同的、更强的**模型。

### 压缩器路由（算力卸载）

压缩调用的路由是三层解析：`strategies[<档位>]` > `rewrite` > 跟随当前请求。
"跟随当前请求"意味着压缩与主对话同模型；显式指定后，压缩算力就落在那个模型上
（例如本地小模型、另一家供应商）。解析在钩子内部做 —— 档位是按会话/preset 选出来的，
路由必须跟着档位走。

- **必须 provider + model 同时给。** 只填一半不算"配了"，会继续往下掉一层：
  跨 provider 猜 model 名比回退更危险。
- **失败策略 `rewrite.fallback`**：`keep`（默认，保留原文）或 `session`
  （卸载路由失败时回退到会话路由再试一次）。回退只发生一次，且两条路由必须不同；
  用户按 Esc 中断不触发回退 —— 中断是用户的决定，不是路由的错。
- **禁止输出思考（`noThink`，默认开）**：压缩器的任务只是给结论。输出里一旦出现
  `<thinking>` / `<think>` / `[思考]` / `思考：`，判失败并按原因重试一次；两次都犯就保留原文，
  不再用确定性兜底顶替（兜底内容不是模型按预设改写的）。
- **effort 档位以宿主声明为准**：`off` / `medium` / `high` / `max` 这些值来自宿主的
  `reasoningEfforts` 配置；给路由发一个它没声明的档位（比如 `low`）不会报错，但也不会按预期生效。
- **usage 记账**：改写调用会把服务端的 `completionTokens` / `reasoningTokens` 记进账本。
  这些数**包含被丢弃的思考**，所以和最终可见的几百字对不上是正常的 ——
  `maxTokens` 是生成总预算，不是"可见正文的长度上限"。
- **输出预算（`maxOut`）**：超过字数上限判 `over-budget`，带针对性指令重试一次，不做硬截断。
  截断（`max-tokens`）本身直接停止，不再降级重试 —— 换档位不解决输出预算。
- **提示词三层结构**：标签层（按档位固定，必须保留）+ 用户补充层（贴入文本或预设正文，
  只能加不能替）+ 收尾约束（长度预算与 `maxTokens` 匹配）。标签层是"模型在压缩而不是
  在继续思考"的唯一可机检信号，用户文本不能替掉它。
- **无标签即拒绝**：输出里一个合法标签都没有，判 `no-label` 并重试一次；两次都无标签就
  保留原文。检测在 `formatNodes` 之前做 —— 否则补出来的 `[中性]` 会掩盖无标签输出。
- **收尾约束**：字符预算按 `maxTokens` 保守换算（约 1.2 字/token），`maxOut` 配了就以它为准；
  超了就删次要条目，不靠换行凑长度。
- **调用上限（`rewriteTimeoutMs` / `rewriteBudget`）**：单块改写调用有硬超时（默认 120s）
  与总调用预算（默认 4 次）。模型"一直吐字但永不结束"时到点放弃并保留原文；
  effort 降级 + 重试 + 回退叠起来也不会把一块打成十几次调用。
- **保存时的路由体检**：卡片保存后会拿 `listProviders` / `resolveModelInfo` 核一遍，
  不在注册表里的 provider、解析不了的 model 会以 `warnings` 返回。**只报警、不拒绝**：
  目录可能离线、provider 可能稍后才注册，把一次目录抖动变成"保存失败"更糟。
- **账本**：每次改写尝试先记 `rewrite-route`（provider / model / source / offloaded）；
  回退记 `rewrite-route-fallback`（from / to / 原因）；最终 `block-replaced` 里的
  `provider`/`model` 是**实际用过**的那条路由，回退之后 `offloaded` 为 false。

自测（`POST /iot-thinking/api/test`）走同一套解析，并在响应里回 `routeSource`/`offloaded`，
避免"测的是会话路由、跑的是卸载路由"的假绿。

### 配置

```text
$DSH_HOME/plugin-data/iot-thinking/config.json
```

```jsonc
{
  "enabled": true,
  "strategy": "iot-full",                    // 全局默认
  "strategies": {                            // 逐档位覆盖（键是档位 id）
    "iot-full":    { "minChars": 700 },
    "iot-compact": { "minChars": 700, "provider": "local-llama", "model": "qwen3-4b" },
    "custom":      { "minChars": 120 }
  },
  "agentPresets": { "standard": "iot-balanced", "jingjing": "custom:character-inner-voice" },
  "sessions": { "<sessionId>": "iot-compact" },
  "defaultPreset": null,                     // "builtin:standard" / "custom:文件名" / "custom"
  "rewrite": {                               // 压缩器路由（算力卸载）
    "provider": null,                        // null = 跟随当前请求
    "model": null,
    "effort": "auto",
    "fallback": "keep"                       // 或 "session"
  },
  "presetDir": "",                           // 必填：留空则文件类预设不可用
  "customText": "",                          // 选 `custom` 时用这段
  "presetAccept": "different",               // 或 "shorter"
  "sessionSwitch": false,                    // 对话顶部是否显示「按会话覆盖」下拉，默认 false
  "archive": true
}
```

**迁移是一次性的、行为不变的**：旧 `profile`（`compact`/`balanced`/`full`）与平铺的
`minChars`/`maxChars`/`maxTokens`/`maxNodes`/`maxNeutral` 会被搬进 `strategies[<档位>]`，
然后**删掉旧键**。留着不行：`limitsOf` 在没有逐档覆盖时会去读它们，于是"切到一个
没配过的档位"会把旧档位的数字一起带过去（实测：从 compact 切到 full，`maxNodes`
变成旧配置里的 10）。旧的顶层 `provider`/`model`/`reasoningEffort` 同样会被搬进
`rewrite` 并删键 —— 留着的话，以后把 `rewrite` 清空，旧值会通过兜底重新活过来。

## 怎么看"到底会发什么"

| 想知道 | 去哪看 |
|---|---|
| 这一档真正发给模型的提示词 | 卡片里那一行的**看原文**（`GET /iot-thinking/api/strategy?id=…`），或 `node show.mjs iot-full` |
| 这个 agent preset 由哪些插件行组成 | 卡片里那一行的**看组成**（`GET /iot-thinking/api/presets?detail=<id>`） |
| 宿主上有哪些 agent preset | 卡片里的 preset 下拉（`agentPresets.list()` + 默认标记 + `broken` 诊断） |
| 哪些映射指向了已不存在的 preset | 同一张表下方的 `orphaned` 提示 |

不启动 dsh 也能看文本：

```powershell
node show.mjs                            # 列出全部档位：family / accept / 关键默认值
node show.mjs iot-full                   # 打印该档位真正发给模型的提示词
node show.mjs --json iot-compact         # 同一份内容，给脚本用
node show.mjs --presets                  # agent preset 的声明与组成（读 patch，不连宿主）
```

`--presets` 读的是**声明**，不是运行态注册表：

> **agent preset 没有"根目录"。** `dsh-agent-preset-registry` 明确"不扫描目录，也不接受
> preset 路径"。登记处只有两处：① bundle 的 `dsh.bundle.patch`（`dsh-web-app` 是数组：
> `cordis.patch.yml` + `presets/{standard,ptc,minimal,cordis}.patch.yml`）；
> ② profile 的 `cordis.patch.yml` 里 `- id: preset-*` 的行。
> `$DSH_HOME/.agent-presets/` 是旧的目录式来源，运行时不读它。

⚠️ 别把两个"preset"搞混：**agent preset** 是会话的插件组合（standard / jingjing…），
本插件只能引用它；**改写预设**是本插件自己的那份文本（用户注入 / 用户配置）。

## 账本与事件

唯一可靠的证据是插件账本：

```text
$DSH_HOME/plugin-data/iot-thinking/events.jsonl
$DSH_HOME/plugin-data/iot-thinking/originals/<sessionId>/<time>-block<index>.txt
```

事件：`constructed`、`block-replaced`、`block-kept`、`block-skipped`、`block-failed`、
`thinking-rewritten`、`thinking-failed`、`thinking-skipped`（`reason: "aborted"` 表示是你
自己按了 Esc，不是插件出错）、`thinking-degenerate`（基础三档的输出被拒 / 兜底 / 输入已循环）、
`thinking-rejected`（用户配置被拒）、`thinking-effort-fallback`、
`thinking-effort-unsupported`、`thinking-effort-probe-failed`、`thinking-self-test`、
`strategy-switched`（会话内切换）、`rewrite-route`（本次改写发往哪条路由、是否卸载）、
`rewrite-route-fallback`（卸载路由失败后回退到会话路由）。

每条改写事件都带 `strategy`；`block-replaced` 还带形态学指标（基础三档 `nodes`，
用户配置 `lines` / `fragmentRate`）。统计收益请用 `block-replaced`，不要用
`thinking-rewritten` 求和（后者含被拒的块）。要确认"压缩真的卸载到了别的模型"，
看 `rewrite-route` 的 `source`/`offloaded`，以及 `block-replaced` 里的 `provider`/`model`。

**失败本来静默**：按设计失败就保留原文，界面上只表现为"没效果"。所以 `thinking-failed`
带宿主的 `code`（如 `UNSUPPORTED_REASONING_EFFORT`），设置卡在最近一次判定是失败时
直接显示原因。

**一键测试**（`POST /iot-thinking/api/test`）用选中档位真跑一次（不归档、不改配置），
并按与 `stream.mjs` 相同的替换规则判定 —— 只检查"有没有返回内容"会假绿。它同样服从
`enabled` 与该档位的输入闸门：关着、输入太短、或草稿已判为循环重复时，直接说
"真实链路会跳过"，不报绿。

## 文件

| 文件 | 作用 |
|---|---|
| `index.mjs` | host 半个：`llm/stream` 钩子（按会话解析档位）、配置桥、`/iot` 命令、最近路由 |
| `lib/stream.mjs` | 纯 chunk 转换器（扣住 → 改写 → 一个 delta + 改写的 block-end）；替换判定交给档位 |
| `lib/strategies.mjs` | 档位注册表：三档基础 + `custom` + `off`，五个部件（prepare / skipInput / buildPrompt / finalize / accepts） |
| `lib/route.mjs` | 档位三层解析、改写器路由解析（算力卸载）、内置预设解析、旧配置迁移、档位目录 |
| `lib/custom.mjs` | 用户配置/注入：目录 / 列表 / 读（mtime 缓存）/ 另存为 / 注入解析 / 提示词拼装 |
| `lib/builtin-presets.mjs` | 内置预设（standard / deep）：正文、元数据、清单 |
| `lib/advisor.mjs` | 指导层：路由解析、提示词、格式闸门、追加注入 |
| `lib/gates.mjs` | 用户配置的四道最小闸 |
| `lib/switch.mjs` | `/iot` 的纯逻辑（与 HTTP 桥共用同一张 `sessions` 表） |
| `lib/compress.mjs` | 通用改写引擎：模型调用、effort 恢复、按档位判定与重试、确定性兜底 |
| `lib/effort.mjs` | 改写器推理档位策略：id、按路由缓存、拒绝判定 |
| `lib/think.mjs` | 基础三档的指令、prompt 装配、草稿净化、退化判定、节点渲染 |
| `lib/config-store.mjs` | 活配置（行默认值 + 用户文件）+ 一次性迁移（含 `rewrite` 块） |
| `lib/config-api.mjs` | HTTP 路由：`config` / `status` / `models` / `test` / `session` / `presets` / `strategy` / `preset-dir` |
| `lib/ledger.mjs` | 事件账本 + 原文归档 |
| `lib/client.js` | 设置卡 + 对话顶部档位下拉 |
| `show.mjs` | 不启动 dsh 的文本视图 |
| `selftest.mjs` | 纯逻辑自测（不启动 dsh、不连模型） |
| `smoke.mjs` | 接线自测：真 `index.mjs` + 真 dsh-llm seam，假 ctx 与模型调用 |

## 自测

```powershell
node selftest.mjs   # 纯逻辑 + 真实头部回调/API 回归
node --experimental-vm-modules teacher-entry-test.mjs # 原始教师入口、开关、预算、取消、输出契约（离线）
node smoke.mjs      # 接线（需要盘上有 dsh 安装来解析 seam；找不到时设 IOT_DSH_SEAM_ANCHORS）
```

覆盖：块级替换与透传、`minChars` / `maxChars`、改写失败与空输出回退、多块、中断、
无 delta 的 block-end、输出清理、退化判定（输入侧与输出侧）、严格重试、确定性兜底、
effort 归一化与 auto/降级/缓存、替换规则、账本脱敏、HTTP 路由门禁与自测路由、
三层路由与野值降级、旧配置迁移与清理、`custom[:名字]` 的解析/落盘/生效、
自定义预设的目录操作与两种提示词形态、`/iot` 的查询/切换/清除。

## 安装

```powershell
# 本机目录（推荐，开发期）：生成 link: 依赖，改代码下次请求生效，零网络
dsh plugin --profile web add "<clone 下来的仓库路径>\dsh-plugin-iot-thinking"

# 或走 UI：设置 → 插件 → 添加插件，填「声明了 dsh.bundle 的那一层」的绝对路径
#   不要填 GitHub 的 /tree/main/... 子目录页地址，那是 invalid-spec
```

装完**重启一次** `dsh web`（host 模块是新的；client 半个从 `exports["./client"]` 出）。

⚠️ **中国网络**：走 GitHub 地址安装前先给 `git config --global http.proxy` 与 profile 的
`.npmrc` 都配代理 —— 实测直连 `github.com:443` 会等 109 s 后超时。

若报 `找不到宿主 seam 包 @deepseek-ai/dsh-llm`，显式指定锚点：

```powershell
$env:IOT_DSH_SEAM_ANCHORS = "<DSH_HOME>/dsh-<ver>/node_modules"
```

## 已知限制

1. **块结束才有输出**：dsh 的 thinking 块在结束时才被替换，所以客户端看到"顿一下再出现"，
   不是逐字流。"边压边回"是明确排除的次要功能。
2. **每改写一块多一次模型调用**（被拒时多一次带针对性指令的重试）。
3. **不能发 `off` 的路由上，改写器会自己思考**：降级为不发 `reasoningEffort`，
   代价是每块多 10–20 s。想避免就显式选该路由公开的低声档。
4. **不是安全边界**：插件以 host 权限运行、能读模型输出，只装可信来源。
5. **基础三档的质量有已知失效面**：标签记账错位、块内退化重复。后者已由四道闸
   （输入净化 / 提示词 / 低温度 + 严格重试 / 质量门 + 确定性兜底）拦住，属于急性失败。
6. **自定义预设的好坏取决于你写的文本**：只有四道最小闸，没有禁词/口头禅/塌缩判定；
   写得不严，它就会按你说的写，而不会替你发现问题。
7. **HTTP 路由自带门禁**：要求自定义头 `x-iot-thinking: 1`、只收同源文档、默认只接受
   loopback 对端。把 web 绑到 `0.0.0.0` 时设置卡默认会被 403 拒绝；要恢复就设
   `IOT_THINKING_ALLOW_REMOTE=1`，并把"谁能访问这个端口"当成安全边界来管理。
8. **宿主 seam 必须可解析**：源码安装、版本化安装与 Electron/ASAR 宿主的解析锚点不同；运行宿主可正常加载链接插件，但独立 Node smoke 若找不到盘上的 `@deepseek-ai/dsh-llm`，需设置 `IOT_DSH_SEAM_ANCHORS` 或改用 `DSH_NODE_EXECUTABLE` 做宿主契约测试。独立 smoke 失败不能直接推断运行中的插件未激活。
9. **装完先看账本，别信"装上了"**：只有 `events.jsonl` 里出现 `constructed`，
   才说明这条 entry 真的激活了。

### 教师回归测试

默认 `npm test` 包含 selftest、教师入口、上下文纯逻辑和 smoke，任一失败立即停止。
真实宿主契约测试必须另用当前安装的 Electron runtime（可读 ASAR）执行：

```sh
ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" host-context-test.mjs --expect-context
```

它使用真实 Cordis、SystemPrompt、Tools 和 NativeToolExecutor，验证新会话可见的参数 schema、
上下文注册/组装/渲染/刷新、会话覆盖、总闸、宿主 suppression、CD 和卸载。
所有配置和账本在临时目录，LLM 为离线替身，不访问模型。它不等价于完整线上 AgentLoop 验收。
