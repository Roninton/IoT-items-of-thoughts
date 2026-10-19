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

## Install

### From the UI

Settings → Plugins → **添加插件** → enter the absolute path:

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
  400 是"够覆盖多数会话草稿"的下限：实测 400–500 区间是**净亏**（省几个字符，
  付一次完整模型调用 + 1.2–1.9 s 首字延迟），按自己的会话规模调到 **700–800** 更划算。
  想知道自己的分布，看 `below-min-chars` 的 `chars` 与 `block-replaced` 的 `charsIn`;
- **最大输入字符数** — drafts longer than this are left alone (default 24000);
- **节点输出上限** — compressor `maxTokens` (default 2000);
- **最多节点数** — hard cap on the node chain (default 8);
- **最多 [中性] 节点** — extra `[中性]` nodes are merged (default 2; `0` drops them);
- optional **provider / model** — leave blank to follow the current request's
  route, or set another provider/model registered in DSH.

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
`block-failed`, `thinking-compressed`, `thinking-failed`, `thinking-degenerate`.

Original drafts are archived before replacement:

```text
$DSH_HOME/plugin-data/iot-thinking/originals/<sessionId>/<time>-block<index>.txt
```

dsh stores whatever chunks the waterfall yields, so once a block is replaced the
original draft exists only in this archive.

## Files

| File | Role |
|---|---|
| `index.mjs` | host plugin: `llm/stream` hook + config API |
| `lib/stream.mjs` | pure chunk transformer (hold → compress → single delta + block-end) |
| `lib/compress.mjs` | auxiliary `ctx.llm.stream()` call(s), low-temperature retry, quality gate, deterministic fallback |
| `lib/think.mjs` | IoT instruction, prompt wrapper, draft sanitization, degeneration detection, output cleanup |
| `lib/config-store.mjs` | live JSON config (row defaults + user file) |
| `lib/config-api.mjs` | exact HTTP routes for the Settings card |
| `lib/ledger.mjs` | events ledger + original-draft archive |
| `lib/client.js` | Settings → Plugins → IoT 思考压缩 card |
| `selftest.mjs` | pure transformer tests (no dsh boot, no model) |

## Self-test

```powershell
cd D:\_playground\workspace\IoT-items-of-thoughts\dsh-plugin-iot-thinking
node selftest.mjs
```

Covers: replacement of a completed block, disabled pass-through, non-agent-loop
pass-through, `minChars`, compressor failure, empty/longer output fallback,
multiple blocks, aborted streams, block-end without deltas, output cleanup,
degenerate input/output detection, strict retry, and deterministic fallback.

## Known limits

1. **Latency at block end.** The reasoning block is shown as "thinking" while
   the draft is buffered, then the compact text appears. Streaming the compact
   text while compressing is deliberately out of scope ("压缩后返回").
2. **Extra model call per compressed block.** The compressor uses the current
   route by default; set a cheaper provider/model in the card if needed.
3. **Not a security boundary.** The plugin runs with the host's permissions and
   can read model output; install only from a source you trust.
4. **The original draft is not in the session log.** It is archived under
   `plugin-data/iot-thinking/originals/`; keep that directory if you need audit.
5. **Degenerate retry costs one more compressor call.** Only when the quality
   gate rejects the first output; the deterministic fallback itself is local.
6. **The fallback is lossy.** If both attempts are degenerate, it keeps at most
   one fact line plus a plan line; the full draft remains in `originals/`.
7. **统计压缩率请用 `block-replaced`，不是 `thinking-compressed`。** 后者在"决定要不要替换"
   之前就发出去了：它只说明这次压出过结果。变长的块会被 `block-kept (reason=not-shorter)`
   拦下、原文照旧保留，所以拿 `thinking-compressed` 求和会低估真实收益。
