# iot-compaction —— IoT 的内置 DSH 插件形态

> ⚠️ **已退役（2026-10-18）**：本插件走 `ctx.compaction`，会把一段历史替换成 checkpoint
> 消息；模型会看到新的「上下文已压缩」块并把它当成新指令。它已被外部 bundle
> **`dsh-plugin-iot-thinking`** 取代：后者在 `llm/stream` 里把当前 thinking 块**原地**
> 换成条目链，不产生 checkpoint、也不插入新消息。
>
> 本目录仅保留作历史/回滚参考；web profile 里的 `iot-compaction` 挂载行已注释。
> 新插件位置：`workspace/IoT-items-of-thoughts/dsh-plugin-iot-thinking`。

把 IoT 的「**thinking block 结束后自动提纯**」做成 DSH 的 **compaction 后端**，
而不是在外面再套一个 OpenAI 兼容服务。

## 为什么是 compaction 后端，不是 provider

DSH 里能做"把历史换成更小的东西"的**唯一合法位置**就是 compaction：

| 位置 | 能不能改历史 | 结论 |
|---|---|---|
| `agent/request` | 不能（源码注释：cannot mutate messages） | 只能换路由 |
| `llm/stream` | 不能（loop 请求 deep-frozen，改写抛错） | 只能观察 |
| adapter（provider） | 能，但那是「整个请求由我代发」 | 等于把 server 换个壳 |
| **`ctx.compaction`** | **能**（durable 替换 + checkpoint 框架） | ← IoT 应该在这 |

所以上一版"注册一个 provider 转发到 pciso"是偷懒：它没有实现任何 IoT 语义。
本插件删掉那条路，改为实现压缩引擎。

## 触发点正好是 IoT 要的那个点

`BasicCompactionEngine` 的自动触发是 **between-step**（一个 step 结束、下一个开始之前）。
在 DSH 的 step 模型里，「一个 step」正好等于「一个 thinking block + 它的正文/工具调用」。
所以 between-step 就是 IoT 规格里的「thinking block 输出后、下一个 block 之前」。

默认后端按**阈值压力**才触发。本插件提供两种模式：

- `everyStep: false` → 完全交给宿主：按 provider/model 的压力阈值触发，`summarize()` 仍是本插件的条目提纯。
- `everyStep: true`（默认）→ 不等阈值：在每个 `agent/pre-step` 自己选一个**工具配对平衡**、覆盖到当前尾部的段，直接调用宿主的 `compactRegion()`。选段失败、或选段里没有 thinking 时返回 `null`，不冒险。
  - `context-overflow` 是紧急恢复，始终交给宿主的强制路径（仍然走本插件的 `summarize()`）。

为什么不再调 `super.compactIfNeeded`：它的第一道判断就是
`totalTokens < thresholdTokens → return null`，而阈值被宿主的两条不变量锁住
（`thresholdRatio ∈ (0,1]`、`retainRatio < thresholdRatio`），所以"把阈值调到 0"这条路由宿主模型里不存在。

## 行为覆盖

- `summarize()`：把「散文摘要」换成「正节点 / 否掉 / 待定 / 风险」的条目链。
- `compactIfNeeded()`：只在 `everyStep && trigger === 'pressure'` 时接管选段并调用 `compactRegion()`；
  其余情况（`everyStep: false`、`context-overflow`）原样委托宿主。
- 选段使用宿主导出的 `toolPairingBalancedBefore/After`，从 surface 尾部向前找最近的平衡点，
  再校验起点；`tokenMeter` 的 seq 列表与 surface 不一致时直接放弃（不猜）。

提纯调用走 `ctx.llm.stream()`，输入复用宿主重放的会话前缀（system + tools + 历史），
所以这次辅助调用仍然吃得到提供方的前缀缓存，只有末尾那条提纯指令是新增输入。

## 文件

| 文件 | 作用 |
|---|---|
| `index.mjs` | 插件入口：default export = 引擎类（挂载即 provide `ctx.compaction`） |
| `lib/engine.mjs` | `IotCompactionEngine extends BasicCompactionEngine`：覆盖 `summarize()` 与 `everyStep` 的 `compactIfNeeded()` |
| `lib/surface.mjs` | 0.2.x seq surface → 消息投影；工具配对平衡选段（纯函数，可单测） |
| `lib/think.mjs` | thinking 抽取 + 提纯指令（只压不补 / 禁完成态断言 / 否定单列） |
| `lib/seams.mjs` | 按绝对路径导入宿主 seam 包（本地插件裸 `@deepseek-ai/*` import 会 ERR_MODULE_NOT_FOUND） |
| `selftest.mjs` | 28 项纯逻辑自测，不启动 dsh、不连模型 |
| `deploy/preset-compaction-row.yml` | 挂载行片段与两条部署路径 |

## 自测

```sh
node dsh-plugin-compaction/selftest.mjs
```

覆盖：thinking 识别/抽取（漏抽会把提纯退化成摘要，多抽会把正文当思考压掉）、
提纯指令的四条硬规则、0.2.x seq surface 投影、工具配对平衡选段（含未配对尾部/起点回退），
以及 `thresholdRatio` 的宿主不变量。

## 挂载

非 preset 会话：挂在 profile（host）层即可，本插件已在本机 web profile 生效。

preset 会话：**必须挂进该 preset 自己的 `compaction` 组**（`isolate: { compaction: true }` 的
entry-local realm），host 层挂的后端对它永不触发。详见 `deploy/preset-compaction-row.yml`。
同一个 realm 只能有一个 compaction 后端；`standard-acp` 已挂 billion-context，不要直接替换。

## 已知限制

1. **压缩粒度是"段"，不是"块"**：宿主一次替换一段 surface 范围并合成一条 checkpoint。
   `everyStep` 选的是"从 system 头到当前平衡尾部"的整段，因此该段里的用户原话、正文与
   工具回执会一起被 checkpoint 取代，只有条目链留下。这是宿主 compaction 的替换语义，
   不是提纯指令能改变的。
2. **`everyStep` 每个 step 都会多一次模型调用**：闸门只保证"选段里没有 thinking 时不调用"。
   长工具会话里如果每个 step 都带 reasoning，调用次数会明显上升。
3. **没有端到端跑过真实长会话**：当前只有纯逻辑自测。验证需要一次真实会话把 surface
   推到多步，然后确认 checkpoint 是条目链、工具配对未坏、`events.jsonl` 出现 `every-step`。
4. **preset 挂载尚未启用**：本机 web 的 `iot-compaction` 仍在 host 层；preset 会话要生效，
   需按 `deploy/preset-compaction-row.yml` 把行放进目标 preset 的 compaction 组（并移除
   realm 里原有的 `compaction-basic`，二者互斥）。
5. `surface.nodes` 在 0.2.0 已确认为 seq 数组；消息投影走 `session.deriveEventMessage()`。
   取不到消息时闸门放行（宁可压一次，也不静默失效）。
