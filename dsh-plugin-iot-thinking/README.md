# dsh-plugin-iot-thinking

IoT (Items of Thoughts) for **DeepSeek Harness 0.2.x**，以 **external bundle** 形式安装
（设置 → 添加插件填绝对路径，或 `dsh plugin add`）。

**IoT 的本质不是"压缩"，是"在 thinking 块结束那一刻改写它"。** 压缩只是决策型任务
需要的那种改写。所以改写方式是可选的**档位**，并且可以**按会话 / 按 agent preset** 指定：

| 档位 | 目标 | 判定"能不能替换" |
|---|---|---|
| `iot-compact` / `iot-balanced` / `iot-full` | 压成带标签的思维节点（3 / 5 / 8 标签） | **必须比原文短** |
| `custom` | 用户自己写的文本（预设目录里的 `.md`，或设置卡里贴进来的一段） | 默认"不是原样即可"；压缩型预设可配"必须更短" |
| `off` | 保留原生 thinking，一个字节都不改 | 永不替换 |

**除这三档 + `custom` 外不内置别的档位。** 没有真机数据支撑的改写方式（长程研究、
角色内心这类）不配和基础档平起平坐 —— 它们该以**自定义预设**的形式存在。
`inner-voice` 就是这样一份 preset（见 [`../../inner-voice/presets/character-inner-voice.md`](../../inner-voice/presets/character-inner-voice.md)）。

三层解析顺序（写死，没有"看情况"）：

```text
sessions[<sessionId>]  >  agentPresets[<agent preset id>]  >  strategy（全局默认）
```

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
| 设置卡（设置 → 插件 → IoT 思考改写） | 全局默认、逐档限制、按 agent preset 映射、"第 4 项"预设的目录与内容 |

优先级：**会话自己的选择 > agent preset 行 > 全局默认**。

> 为什么默认关：per-session 覆盖是少数场景的需求，而那行控件贴在每个会话标题旁，
> 很容易让人以为"档位归它管"。默认只留全局默认 + preset 映射这两层；需要时会话里
> 用 `/iot` 也能切，想要常驻下拉再去卡片里打开。

## 第 4 项：自定义预设

预设就是**一份文本**：告诉改写器"把 thinking 改成什么样"。插件**开局是空的**。

- **来源**：`presetDir` 里的 `.md` / `.txt`，或设置卡里贴进来的一段。
  **目录必须自己填，没有默认路径**；留空时文件类预设不可用，贴入的文本仍可用；
- **值域**：`custom`（贴入的那段）/ `custom:<文件名>`（目录里的文件，**名字带后缀**，
  例如 `custom:my-style.md`、`custom:notes.txt`）；
- **界面上是两级**：先选档位（三档 + 自定义预设 + 关闭），选到「自定义预设」后才出现
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
- **推理档位（`reasoningEffort`）** — `auto` 默认：先发 `off`（改写器不花预算在自己的
  thinking 上），宿主拒绝该档位就改用该路由公开的 `minimal`/`low`，没有就不发这个字段。
  决策按 `(provider, model, 档位)` 记住，同一路由不会再白试一次；
- 可选 **provider / model** — 留空跟随当前请求的路由。

### 配置

```text
$DSH_HOME/plugin-data/iot-thinking/config.json
```

```jsonc
{
  "enabled": true,
  "strategy": "iot-full",                    // 全局默认
  "strategies": {                            // 逐档位覆盖（键是档位 id）
    "iot-full": { "minChars": 700 },
    "custom":   { "minChars": 120 }
  },
  "agentPresets": { "standard": "iot-balanced", "jingjing": "custom:character-inner-voice" },
  "sessions": { "<sessionId>": "iot-compact" },
  "presetDir": "",                           // 必填：留空则文件类预设不可用
  "customText": "",                          // 选 `custom` 时用这段
  "presetAccept": "different",               // 或 "shorter"
  "sessionSwitch": false,                    // 对话顶部是否显示「按会话覆盖」下拉，默认 false
  "provider": null,
  "model": null,
  "reasoningEffort": "auto",
  "archive": true
}
```

**迁移是一次性的、行为不变的**：旧 `profile`（`compact`/`balanced`/`full`）与平铺的
`minChars`/`maxChars`/`maxTokens`/`maxNodes`/`maxNeutral` 会被搬进 `strategies[<档位>]`，
然后**删掉旧键**。留着不行：`limitsOf` 在没有逐档覆盖时会去读它们，于是"切到一个
没配过的档位"会把旧档位的数字一起带过去（实测：从 compact 切到 full，`maxNodes`
变成旧配置里的 10）。

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
本插件只能引用它；**改写预设**是本插件自己的那份文本（第 4 项）。

## 账本与事件

唯一可靠的证据是插件账本：

```text
$DSH_HOME/plugin-data/iot-thinking/events.jsonl
$DSH_HOME/plugin-data/iot-thinking/originals/<sessionId>/<time>-block<index>.txt
```

事件：`constructed`、`block-replaced`、`block-kept`、`block-skipped`、`block-failed`、
`thinking-rewritten`、`thinking-failed`、`thinking-skipped`（`reason: "aborted"` 表示是你
自己按了 Esc，不是插件出错）、`thinking-degenerate`（基础三档的输出被拒 / 兜底 / 输入已循环）、
`thinking-rejected`（自定义预设被拒）、`thinking-effort-fallback`、
`thinking-effort-unsupported`、`thinking-effort-probe-failed`、`thinking-self-test`、
`strategy-switched`（会话内切换）。

每条改写事件都带 `strategy`；`block-replaced` 还带形态学指标（基础三档 `nodes`，
自定义预设 `lines` / `fragmentRate`）。统计收益请用 `block-replaced`，不要用
`thinking-rewritten` 求和（后者含被拒的块）。

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
| `lib/route.mjs` | 三层解析、旧配置迁移、档位目录 |
| `lib/custom.mjs` | 自定义预设：目录 / 列表 / 读（mtime 缓存）/ 另存为 / 提示词拼装 |
| `lib/gates.mjs` | 自定义预设的四道最小闸 |
| `lib/switch.mjs` | `/iot` 的纯逻辑（与 HTTP 桥共用同一张 `sessions` 表） |
| `lib/compress.mjs` | 通用改写引擎：模型调用、effort 恢复、按档位判定与重试、确定性兜底 |
| `lib/effort.mjs` | `reasoningEffort` 策略：id、按路由缓存、拒绝判定 |
| `lib/think.mjs` | 基础三档的指令、prompt 装配、草稿净化、退化判定、节点渲染 |
| `lib/config-store.mjs` | 活配置（行默认值 + 用户文件）+ 一次性迁移 |
| `lib/config-api.mjs` | HTTP 路由：`config` / `status` / `models` / `test` / `session` / `presets` / `strategy` / `preset-dir` |
| `lib/ledger.mjs` | 事件账本 + 原文归档 |
| `lib/client.js` | 设置卡 + 对话顶部档位下拉 |
| `show.mjs` | 不启动 dsh 的文本视图 |
| `selftest.mjs` | 纯逻辑自测（不启动 dsh、不连模型） |
| `smoke.mjs` | 接线自测：真 `index.mjs` + 真 dsh-llm seam，假 ctx 与模型调用 |

## 自测

```powershell
node selftest.mjs   # 纯逻辑
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
8. **Electron 打包版（`app.asar`）不支持**：host 半个要从盘上找宿主的 `@deepseek-ai/dsh-llm`
   （见 `lib/seams.mjs`），打包版把它放在归档里，外部进程看不见。现象是能聊天、能出答案，
   只有 stderr 一行 `warning: 1 entry did not activate`，账本里没有 `constructed`。
9. **装完先看账本，别信"装上了"**：只有 `events.jsonl` 里出现 `constructed`，
   才说明这条 entry 真的激活了。
