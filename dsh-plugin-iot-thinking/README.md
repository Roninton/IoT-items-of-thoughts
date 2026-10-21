# dsh-plugin-iot-thinking

IoT thinking-block compression for **DeepSeek Harness 0.2.x**, packaged as an
**external bundle** (installable from Settings → 添加插件 with an absolute local
path, or with `dsh plugin add`).

## The problem it fixes

The previous `iot-compaction` backend used `ctx.compaction`. dsh's compaction
replaces a **history range** and appends a checkpoint message. The model sees
that checkpoint as a new user turn:

> 上下文已压缩 · 已压缩 10 条历史记录（约 5002 tokens）

and then thinks:

> The conversation has been compacted. I need to figure out where we are…

That is not IoT's rule. IoT says: **when a thinking block ends, compress that
block and return the compact block in place.** The body, tool calls, usage and
finish reason stay native.

## How this plugin does it

dsh exposes `llm/stream` as a **waterfall** around every model call. Adapters
emit raw chunks; dsh assembles the committed assistant message from whatever the
waterfall yields. This plugin:

1. passes `block-start` through (the thinking block starts normally);
2. holds `reasoning-delta` chunks per block index;
3. at the reasoning block's `block-end`, calls the IoT compressor with the full
   draft;
4. yields **one compact `reasoning-delta`** and a **rewritten `block-end`**;
5. passes text deltas, tool-call deltas, usage and finish through untouched.

Because the replacement happens **before** the assistant message is committed,
there is no second message, no checkpoint, no `compaction/start`,
`compaction/end`, or `compaction` service. It also works inside preset sessions,
because the hook is at the LLM layer rather than a preset-local compaction realm.

If compression fails, is disabled, or the result is empty / not shorter than the
draft, the original deltas and block are yielded unchanged.

## 节点格式

压缩结果不是散文，也不是只有“正/负”两类，而是一组带标签的思维节点：

```text
[支持] ...
[反对] ...
[中性] ...
[计划] ...
[权衡] ...
[待定] ...
[风险] ...
[决定] ...
```

- 每个节点一行；**节点之间空一行**；
- 行首直接写 `[标签]`，不要 `-` / `·` / 编号；
- 目标 4~6 条，最多 8 条（`maxNodes`，可在设置里改）；
- `[中性]` 最多 2 条（`maxNeutral`），多余的会合并；
- 每条尽量 ≤80 字；不要把“我读了 X / 我运行了 Y”写进节点；
- 超量节点按优先级保留：决定 > 反对 > 权衡 > 风险 > 待定 > 计划 > 支持 > 中性；
- 没有内容的类别直接省略，不硬凑；
- 旧标签（正节点 / 否掉 / 待定 / 风险）由 `formatNodes()` 自动映射到新标签；
- 没有标签的续行会并入上一个节点；整段都没有标签时补 `[中性]`。

## 退化保护（压缩器自己陷入重复）

长会话里模型可能把上一轮的 `[中性]` 当成输出模板，压缩器也可能在 `[中性]` 后吐出
`好` / `现在` / `写` / `结束` 这种单词节点。插件在四个点拦截：

1. **输入净化** — 压缩前剥掉草稿里的 `[标签]`，删掉单词填充行，折叠连续重复；
2. **提示词** — 明确“不要续写/模仿草稿里的标签和重复词；每个节点是完整判断，至少
   12 个汉字；没内容就输出空”；
3. **低温度** — 压缩器 `temperature: 0.2`；检测到退化后用 `0.1` + 严格模式重试一次；
4. **质量门** — 单词 / 重复内容 / 自我循环评论 / “好；现在；写”合并链都判为退化；
   重试仍退化时用确定性兜底（一条有效事实 + 一条计划），绝不把循环写回会话。

对应事件：

```text
thinking-degenerate { stage: "input" | "output", action: "retry" | "fallback" | "skip" }
```

如果看到 `fallback: true`，说明这次压缩器输出被判退化，会话里写回的是兜底节点；
原草稿仍在 `originals/`。实测：一段曾经产出 9 条 `[中性]` 单词节点的 622 字草稿，
现在稳定输出 5 条有效节点（322 字），无需重试；普通 web 会话 1161 字 → 153 字 / 3 条节点。

### 输入门只统计实质行（issue #3）

旧的输入判据是 `duplicate >= 3`，统计**所有**重复行。真在写代码的草稿里
```python 围栏能出现 13~15 次，`return out` / `)` / `}` 更是一屏几十行，于是一段
23484 字的正常草稿被判成循环，整块被换成 53 字的兜底内容 —— **比不压缩更糟**。

现在 `isDegenerateDraft()`：

- 只统计长度 ≥ 20 字的**实质行**（围栏、括号、`return out` 这类骨架行不参与）；
- 重复行 ≥ 5 **且** 占实质行 ≥ 25% 才算循环；
- 或者同一句长行重复 ≥ 8 次 **且** 占实质行 ≥ 20%（"整句循环"）；
- 短句/单词循环仍由 filler 判据抓（`filler >= 3` 且占比 ≥ 40%）。

三个真机草稿的验收：修复前 1 个被误判（23484 字 → 53 字兜底），修复后全部不误判；
真循环与单词循环仍被识别。

**已知取舍**：短行（< 20 字）交错重复的草稿不再在输入侧判退化（例如 A/B 两行反复
交换），因为"短行重复"与"正常代码骨架"在长度上分不开。代价是这种草稿会多走一次
模型调用，由输出侧的质量门与确定性兜底接住 —— 误判会把正常内容换成兜底，那比多调
一次贵得多。同理，"同一句长行重复"要占到实质行的一半才判主导循环：8 条一模一样的
`expect(...)` 断言是正常写法。

## Install

装这个插件有 **一个必踩的坑**：仓库根和插件子目录是两个不同的包目录。
插件管理器只认**声明了 `dsh.bundle` 的那一层**，所以别把"仓库地址"当成"插件地址"。

| 你手里的东西 | 该填什么 |
|---|---|
| GitHub 仓库根地址（`https://github.com/Roninton/IoT-items-of-thoughts`） | **可以**（根 `package.json` 已声明 `dsh.bundle.patch`，指向子目录） |
| GitHub 子目录页地址（`…/tree/main/dsh-plugin-iot-thinking`） | **不要填**：安装器只收"仓库"或"tarball"，`tree/...` 会被判 `invalid-spec` |
| 本机目录：仓库根 `…\IoT-items-of-thoughts` | **可以**（同上） |
| npm 包名 `dsh-plugin-iot-thinking` | 尚未发布到 npm，填了会 `not-found` |

失败时的报错分别是：`这个包没有声明组合包，无法作为插件安装`（安装前预检，`not-a-bundle`）、
`这个包没有声明组合包，不能作为插件管理`（装完发现包里没有 `dsh.bundle`，`not-bundle`）、
`无法识别这个包名或地址`（`tree/...` 这类 URL）。三条都是"填错了那一层"的症状。

### ⚠️ 中国网络：GitHub 地址安装会超时，先配代理

**症状**：UI 里填 GitHub 地址后**等 1~2 分钟**，然后
`ERR_PNPM_GIT_RESOLVE_FAILED … Failed to connect to github.com:443 after 21078 ms`。
实测这台机器直连 `github.com:443` **三次全 21 s 超时**，偶尔又能 1.2 s 连通 —— 是链路抖动，不是插件问题。

**为什么给 `HTTPS_PROXY` 没用**：`pnpm add <github-url>` 走的是 **git 协议**，git 不读
`HTTPS_PROXY`，只认自己的 `http.proxy` 配置。而安装界面里的「安装源」是 **npm registry 地址**，
GitHub 地址根本不经过它 —— 在那里填代理只会变成另一种失败。

**两步修好**（本机 FlClash 跑在 7890，按自己的端口改）：

```powershell
# 1) git 走代理（影响所有 git 操作；不想要时 git config --global --unset http.proxy）
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
```

```ini
# 2) profile 里的 pnpm 走代理：$DSH_HOME/profiles/web/.npmrc
proxy=http://127.0.0.1:7890
https-proxy=http://127.0.0.1:7890
noproxy=localhost,127.0.0.1
```

`.npmrc` 这条对**界面里的安装同样生效**，且**不用重启 dsh**（pnpm 在 profile 目录里运行，会读它）。
实测对比：

| 路径 | 耗时 | 结果 |
|---|---|---|
| GitHub 地址，无代理 | 109 s | **失败**（21 s 超时） |
| GitHub 地址，配好上面两步 | 3.2 s | 成功 |
| 本机目录（零网络） | 0.8 s | 成功 |
| `ghproxy.net` 镜像 tarball | 2.8 s | 实验时 **502**，不可用 |

**替代方案**：拿不到代理就直接用本机目录（先 clone 一次，或用网盘/U 盘拷仓库），完全不碰网络。

### From the UI

Settings → Plugins → **添加插件** → 填**声明了 `dsh.bundle` 那一层**的绝对路径
（仓库根现在也是合法包）：

```text
D:\_playground\workspace\IoT-items-of-thoughts\dsh-plugin-iot-thinking
```

The package declares `dsh.bundle.patch`, so the plugin manager adds it to the
profile's `dsh.profile.bundles` and its `cordis.patch.yml` row loads
automatically.

### From the CLI

```powershell
dsh plugin --profile web add "D:\_playground\workspace\IoT-items-of-thoughts\dsh-plugin-iot-thinking"
```

Restart `dsh web` once after installing (the host module is new). The client
half is served from `exports["./client"]`.

### 版本化安装（`$DSH_HOME/dsh-<ver>/`）

版本化安装把包装在 `<DSH_HOME>/dsh-<ver>/node_modules/@deepseek-ai/`，而不是
`profiles/node_modules`。插件按 `process.argv[1]`（宿主入口）上溯出这个目录，
并兜底扫描 `$DSH_HOME/dsh-*/node_modules`（多版本共存取最近安装的），所以不用额外配置。

若仍报 `找不到宿主 seam 包 @deepseek-ai/dsh-llm`，显式指定锚点：

```powershell
$env:IOT_DSH_SEAM_ANCHORS = "<DSH_HOME>/dsh-<ver>/node_modules"
```

装完先确认插件真的激活了：账本里该有 `constructed`，之后才轮到 `thinking-compressed` /
`block-replaced`。只有一行 `warning: 1 entry did not activate` 而账本空着，就是没生效。

## Switch and limits

Settings → Plugins → **IoT 思考压缩**:

以下每一项都在卡片里直接改；也可以写 `$DSH_HOME/plugin-data/iot-thinking/config.json`，
或在 `cordis.patch.yml` 的 row 里给部署级默认值。改完**下一轮模型请求**生效，不用重启。

- **已启用 / 已关闭** — the live switch;
- **最小压缩字符数（`minChars`）** — drafts shorter than this are left alone (default 400).
  这是个参数选择，不是缺陷：实测 400–500 区间收益很薄（省几个字符，付一次完整模型调用
  + 1.2–1.9 s 首字延迟）；想要更明显的回本，按自己的会话规模在 **700–900** 之间调。
  想知道自己的分布，看 `below-min-chars` 的 `chars` 与 `block-replaced` 的 `charsIn`;
- **最大输入字符数** — drafts longer than this are left alone (default 24000);
- **节点输出上限** — compressor `maxTokens` (default 2000);
- **最多节点数** — hard cap on the node chain (default 8);
- **最多 [中性] 节点** — extra `[中性]` nodes are merged (default 2; `0` drops them);
- optional **provider / model** — leave blank to follow the current request's
  route, or set another provider/model registered in DSH; the two dropdowns read
  the host's model catalog (`llm.listProviders()` / `llm.listModels()`) so you
  cannot pick a model that does not exist. The free-text fields stay as an
  escape hatch when the catalog is unavailable;
- **推理档位（`reasoningEffort`）** — `auto` by default: try `off`, and if the
  route refuses that level, use a cheap level it does advertise or send no field
  at all. See the section below for why this is the default.

### 压缩器推理档位（`reasoningEffort`）

压缩调用第一次发出时**不再写死 `off`**（issue #3）。旧实现在 `lib/compress.mjs`
里硬编码 `reasoningEffort: "off"`，而 `off` 恰恰是最不通用的那个值：声明了
reasoning 的模型会把它从可接受档位里过滤掉，宿主在发请求前就抛
`UNSUPPORTED_REASONING_EFFORT`（实测 8 ms 返回，供应商根本收不到请求）。于是
pi-ai 路由上压缩**一次都没成功过**，失败又按设计保留原文，界面上只表现为"效果不明显"。

现在的语义：

| 配置值 | 行为 |
|---|---|
| `auto`（默认） | 先发 `off`；被拒则查该路由公开的档位，有 `minimal` / `low` 就用它，没有就不发该字段 |
| 具体档位 | 原样发送；被拒同样降级为不发该字段（不擅自换档） |

- **不发该字段是宿主唯一不用查目录就接受的取值** —— 它把档位交给适配器自己的默认值。
  注意这不是"所有路由都能跑"：宿主会解析 `requested ?? defaultEffort`，如果适配器自报的
  默认档位根本不在它公开的列表里，**连不发字段也会被拒**。那种情况插件会记一条
  `thinking-effort-unsupported`（`requested: null`）并保留原文——能让它工作的只有修适配器；
- 降级决策按 `(provider, model, 档位)` **记住**，同一路由后面的块不会再白试一次
  （进程内缓存；重启 `dsh web` 后重新探测）。**只缓存目录真正答过的结果**：探测超时/
  报错什么都不决定，下次仍从 `off` 试起，一次目录抖动不会永久拿走 DeepSeek 的快路径；
- 目录查询有 3 s 上限（`EFFORT_PROBE.timeoutMs`），挂起也不会卡住这一块的响应；
  查到的档位如果**实际也被拒**（目录乐观/过期），再退一步到不发该字段并记住；
- 降级会记 `thinking-effort-fallback`（带 `requested` 与被拒原因），
  `thinking-compressed` 的 payload 多两个字段：`effort`（本次实际发出的档位，
  `null` = 没发）与 `effortFallback: true`；
- **代价**：不能发 `off` 的路由上，压缩器自己会思考，每块多 10–20 s（实测
  1425 字 → 240 字用 11.7 s、4508 字 → 584 字用 22.7 s）。想避免就显式选一个该
  路由支持的低声档（`minimal` / `low`）；
- 设置卡里选这一个路由时，会显示宿主 `resolveModelInfo()` 报的**真实可选档位**；
  拿不到目录元数据时退回通用列表，并提示目录不可用；
- 档位 id 的词表由适配器定义（宿主侧 `ReasoningEffortId` 对插件是不透明的），
  所以配置里存的是**卡片给你的那个 id**，不限于常见的 7 个；未知 id 原样发送，
  被拒就走降级；
- 直接改 `cordis.patch.yml` 时注意 YAML：`off` / `on` / `no` 这类裸值会被解析成布尔，
  必须写成 `"off"`（写错只会退回默认，不会崩）。

### 压缩档位（3 / 5 / 8 标签）

设置页的 **压缩档位** 决定标签集合与硬节点上限；档位是预设，`maxNodes` /
`maxNeutral` 仍可手动覆盖，但不会超过档位硬上限。

| 档位 | 标签 | 目标 / 上限 | 说明 |
|---|---|---|---|
| `compact` | `[支持] [反对] [待定]` | 3~4 / 5 | 最接近旧 IoT 的 ~85% 压缩；8 标签会硬归并到这三类 |
| `balanced` | `[支持] [反对] [待定] [计划] [决定]` | 4~6 / 6 | 保留决定与下一步；`[中性]`→`[支持]`、`[权衡]`→`[决定]`、`[风险]`→`[反对]` |
| `full` | 8 标签（当前默认） | 4~6 / 8 | 质量优先；`[中性]` 最多 2 条 |

归并是**有损**的：`compact` 把 `[计划]` 并进 `[待定]`、`[决定]`/`[权衡]`/`[中性]`
并进 `[支持]`、`[风险]` 并进 `[反对]`。要保留“决定了什么 / 下一步”就用 `balanced`
或 `full`；要极限省 token 才用 `compact`。`thinking-compressed` 事件会记录 `profile`，
便于按档位统计 `charsIn/charsOut`。

The switch writes:

```text
$DSH_HOME/plugin-data/iot-thinking/config.json
```

and the stream hook reads it on every request (mtime cache), so toggling takes
effect on the **next model request** — no restart.

## Evidence

The only reliable proof that a draft was intercepted is the plugin ledger:

```text
$DSH_HOME/plugin-data/iot-thinking/events.jsonl
```

Events: `constructed`, `block-replaced`, `block-kept`, `block-skipped`,
`block-failed`, `thinking-compressed`, `thinking-failed`, `thinking-skipped`
(`reason: "aborted"` when *you* cancelled — not a plugin failure),
`thinking-degenerate`, `thinking-effort-fallback`, `thinking-effort-unsupported`,
`thinking-effort-probe-failed`, `thinking-self-test`.

`thinking-compressed` 带 `replaced: true|false`：压缩器给出了结果，但只有
`replaced: true` 的块真的替换了会话里的原文。想按块统计收益，用
`block-replaced`（每条都真实替换了）或按 `replaced: true` 过滤；直接对
`thinking-compressed` 求和会把 `not-shorter` 的块算进去，低估真实压缩率。

**失败本来是静默的**：按设计失败就保留原文，界面上只表现为"没效果"。所以
`thinking-failed` 现在带宿主的 `code`（例如 `UNSUPPORTED_REASONING_EFFORT`、`QUOTA`），
设置卡也会在**最近一次判定是失败**时直接显示失败原因（一次成功不会把后面连续失败
的横幅盖掉；确定性兜底替换也算降级）。

设置卡的 **一键测试**（`POST /iot-thinking/api/test`）用同一套 `compressThinking`
真跑一次（不归档、不改配置），并按与 `stream.mjs` 相同的"必须严格变短才替换"
规则判定 —— 只检查"有没有返回内容"会假绿：`charsIn == charsOut` 那种结果在真实
链路上会被 `block-kept (reason=not-shorter)` 拦下。它同样服从 `enabled` 与
`minChars`：关着、或输入比阈值短时直接告诉你"真实链路会跳过"，不报绿。压缩器
失败（含你按 Esc 取消）会报 `compressor-failed` 并带最后一行的失败原因，不会伪装
成"没有变短"。测试会记一条 `thinking-self-test`，自带样稿超过默认 `minChars`。

Original drafts are archived before replacement:

```text
$DSH_HOME/plugin-data/iot-thinking/originals/<sessionId>/<time>-block<index>.txt
```

dsh stores whatever chunks the waterfall yields, so once a block is replaced the
original draft exists only in this archive.

## Files

| File | Role |
|---|---|
| `index.mjs` | host plugin: `llm/stream` hook + config API + last agent-loop route |
| `lib/stream.mjs` | pure chunk transformer (hold → compress → single delta + block-end) + the one replacement rule |
| `lib/compress.mjs` | auxiliary `ctx.llm.stream()` call(s), effort recovery, low-temperature retry, quality gate, deterministic fallback |
| `lib/effort.mjs` | `reasoningEffort` policy: ids, per-route decision cache, refusal detection |
| `lib/think.mjs` | IoT instruction, prompt wrapper, draft sanitization, degeneration detection, output cleanup |
| `lib/config-store.mjs` | live JSON config (row defaults + user file) |
| `lib/config-api.mjs` | exact HTTP routes for the Settings card (`config` / `status` / `models` / `test`) |
| `lib/ledger.mjs` | events ledger + original-draft archive |
| `lib/client.js` | Settings → Plugins → IoT 思考压缩 card |
| `selftest.mjs` | pure transformer tests (no dsh boot, no model) |
| `smoke.mjs` | host-side wiring test: real `index.mjs` + real dsh-llm seam, fake ctx/model call |

## Self-test

```powershell
cd D:\_playground\workspace\IoT-items-of-thoughts\dsh-plugin-iot-thinking
node selftest.mjs   # 纯逻辑，不启动 dsh、不连模型
node smoke.mjs      # 接线：真插件入口 + 真 dsh-llm seam，假的 ctx 与模型调用
```

Covers: replacement of a completed block, disabled pass-through, non-agent-loop
pass-through, `minChars`, compressor failure, empty/longer output fallback,
multiple blocks, aborted streams, block-end without deltas, output cleanup,
degenerate input/output detection, strict retry, deterministic fallback,
effort normalization and the auto/fallback/cached route decisions, the
degenerate-input fixture from issue #3 (code fences and short lines are not
loops), the strict-shorter replacement rule, ledger redaction including the
failure message, and the three HTTP routes the card talks to.

`smoke.mjs` 多盖一层：插件入口能加载（seam 解析成功）、`llm/stream` 钩子真的被注册、
一个 reasoning 块被原地替换而正文不动、压缩调用第一次带 `off`、被拒后第二次带该路由
的 `low`、账本里 `thinking-effort-fallback` 与 `replaced: true` 都在、设置卡的一键测试
复用同一批路由决策且 `charsIn == charsOut` 不报绿、四个 HTTP 路由都挂着门禁、客户端
半个能在最小 `__ModuleLoader__` + React 替身下加载并渲染。它需要盘上有一个 dsh 安装来
解析 seam；找不到时设 `IOT_DSH_SEAM_ANCHORS`。

## Known limits

1. **Latency at block end.** The reasoning block is shown as "thinking" while
   the draft is buffered, then the compact text appears. Streaming the compact
   text while compressing is deliberately out of scope ("压缩后返回").
2. **Extra model call per compressed block.** The compressor uses the current
   route by default; set a cheaper provider/model in the card if needed. On a
   route that refuses `off` the compressor also thinks while compressing, which
   costs 10–20 s more per block (see 推理档位 above).
3. **Not a security boundary.** The plugin runs with the host's permissions and
   can read model output; install only from a source you trust.
4. **The original draft is not in the session log.** It is archived under
   `plugin-data/iot-thinking/originals/`; keep that directory if you need audit.
5. **Degenerate retry costs one more compressor call.** Only when the quality
   gate rejects the first output; the deterministic fallback itself is local.
6. **The fallback is lossy.** If both attempts are degenerate, it keeps at most
   one fact line plus a plan line; the full draft remains in `originals/`.
7. **统计压缩率请用 `block-replaced`，不是 `thinking-compressed`。** 后者现在带
   `replaced: true|false`：只有 `true` 的块真的替换了原文。变长的块会被
   `block-kept (reason=not-shorter)` 拦下、原文照旧保留，所以拿 `thinking-compressed`
   求和仍会低估真实收益（`replaced: true` 过滤则正确）。
8. **设置接口的门禁与 LAN 绑定。** `/iot-thinking/api/config`、`/status`、
   `/models` 与 `/test` 挂在宿主 `webServer` 上，而宿主只维护路由表、不做鉴权 ——
   所以这些 handler 自己守：要求自定义头 `x-iot-thinking: 1`（跨站页面设不了它而不
   触发预检，本服务从不回 CORS 批准）、`Sec-Fetch-Site` 只收 `same-origin`/`none`、
   `Origin` 的 host 必须等于 `Host`、拒绝 opaque origin（DNS rebinding）、默认**只接受
   loopback 对端**。GET 不再下发配置文件的绝对路径，账本事件只保留卡片要用的字段
   （不再有 `sessionId` / `archived` / `provider` / `model`；`message` 只留单行截断后的
   240 字失败原因）。`/models` 只回宿主已有的 provider / model / 档位元数据（Models
   页本来就显示这些），`/test` 只接受一段草稿文本，路由取自配置或最近一次 agent-loop
   请求，不能由请求体指定。
   **代价**：若你把 web 绑定到 `0.0.0.0`（如 `dsh-lan-access`），设置卡片默认会被
   403 拒绝；要恢复就在宿主环境里设 `IOT_THINKING_ALLOW_REMOTE=1`，并把「谁能访问
   这个端口」当成安全边界来管理。
9. **Electron 打包版（`app.asar`）不支持 —— 已确认，不是配置问题。** 插件的 host 半个
   要从盘上找到宿主的 `@deepseek-ai/dsh-llm`（见 `lib/seams.mjs`）。打包版把它放在
   `resources/app.asar` 归档里，`existsSync` / `readFileSync` 对外部进程看不见归档内部，
   `IOT_DSH_SEAM_ANCHORS` 指向 asar 路径也救不回来（路径换成了 asar 那条，判据仍是"目录里
   有 package.json"，永远读不到）。**现象**：装完能聊天、能出答案，只有 stderr 一行
   `warning: 1 entry did not activate`，账本里没有 `constructed`。适用面是 npm 安装、
   版本化安装（`$DSH_HOME/dsh-<ver>/`）和源码 checkout。
10. **装完先看账本，别信"装上了"。** 只有 `$DSH_HOME/plugin-data/iot-thinking/events.jsonl`
    里出现 `constructed`，才说明这条 entry 真的激活了；否则就是第 9 条那种静默失效。
11. **一键测试是真调用。** 它跑一次完整压缩（按一次请求计费、约 10–30 s），不做归档、
    不改配置；失败时同样按"保留原文"处理。它证明的是"当前设置下这条路能压出更短的节点"，
    不是"N 轮会话里质量一定不降"。路由留空时它用**最近一次 agent-loop 请求**的
    provider/model；一个请求都还没发过时会直接报错（没有可用的路由）。
