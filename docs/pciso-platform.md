# pciso 实验平台：P/C 隔离长程编码的机制与验收记录

这是 IoT 的**早期形态**：把"冻结需求前缀 P + 结构化循环状态 C"做成一个可离线复现的实验平台，
用七组对照（A–G）量"约束违反数随轮数"与"prompt 成本"。它现在仍在仓库里、仍有测试，但**不是主形态**
—— 主形态是 DSH 插件（见 [`../dsh-plugin-iot-thinking/README.md`](../dsh-plugin-iot-thinking/README.md)）。
两者的关系：pciso 是**实验台**（可离线、可回放、可故障注入），插件是**产品形态**（运行时块级替换）。

原理与裁定在 [`principle.md`](principle.md)；实验叙事与数字在
[`experiment-log-1.md`](experiment-log-1.md) / [`experiment-log-2.md`](experiment-log-2.md)。

> 章节编号沿用拆分前的 README（§2–§9），因为其他文档与脚本按 §3.1.1 / §7.1.1 这类编号引用，
> 重排会让引用全部失准。

---

## 2. 快速开始

> 七个实验臂的配置在 `configs/`；`_smoke_*` 冒烟配置已归档到 `experiment/configs-archive/`
> （该目录不进版本库），下面两条冒烟命令要先补全路径才能跑。

```powershell
cd IoT-items-of-thoughts
$env:PYTHONPATH = "$PWD\src"

# 离线冒烟：假工作模型 + 确定性压缩器，不花钱，8 轮跑完整流水线
python -m pciso.cli run --config experiment/configs-archive/_smoke_fake.yaml

# 验证回退路径（第 3 轮起篡改冻结测试 -> 应触发 revert 且 C 不前进）
python -m pciso.cli run --config experiment/configs-archive/_smoke_revert.yaml

# 七组对照一次跑完并产出曲线（默认离线：假 oracle + 确定性压缩器，且拒绝写交付仓库）
python -m pciso.cli matrix --out out/matrix

# 故障注入：证明「硬约束回归 -> 回退」这条路真的通
python -m pciso.cli run --config experiment/configs-archive/_smoke_fake.yaml --fake-fault cycle

# 实验设置表（configs/*.yaml 就是论文里那张表）
python -m pciso.cli arms

# 验收某载体的 oracle：把 8 个阶段叠起来，跑该载体的冻结契约 + ruff + demo
python scripts/verify_oracle.py

# 起 HTTP server
python -m pciso.cli serve --port 8099
```

起 server 之后 `http://127.0.0.1:8099/` 是**对话窗口**（纯净版：一条对话流 + 一个输入框）。
**一句话 = 一整轮**：窗口把这句话发到 `POST /session/{id}/say`，server 照常跑完整轮
（工作模型 → 落盘 → 校验 → ΔC → 判定 → 需要就回退/提交），窗口只显示这一轮的正文叙述，
判定不是 `accept` 时在气泡里补一行原因。
每轮进模型的上下文是 **P + C + 本轮这句话**（上一轮模型自己的输出不回流，由 C 代表），
`state/sessions/<sid>/chat.jsonl` 只是给人看的 append-only 记录，不参与判定。
会话固定用 `configs/chat.yaml`（`arm: chat`、`chat_driven: true`、`max_rounds: 200`）——
窗口**不接实验臂**：往 arm_a..g 上追加轮次会把对照轨迹搅成半实验半对话，事后看不出来是哪几轮被污染。
思维链不进窗口，但**逐轮原文落盘**在 `state/sessions/<sid>/thinking/{i:03d}.txt`（回看/评估用，
那目录被 gitignore）。

真实对照组的凭据只从环境变量读，永不入库：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"   # 可选
python -m pciso.cli run --config configs/arm_c_fact_chain.yaml --report out/arm_c.json
```

跑真实实验时，`session.workspace_path` 指向 `../drawing-video-toolchain`，
server 会**逐轮以带 trailer 的 git 提交**写入目标仓库，让"实验生成"与"实验后人工加固"边界机器可查：

```bash
git log --grep='^PCISO-Round:'                                   # 只看实验生成的提交
git log --grep='^PCISO-Round:' --invert-grep --no-merges -- .     # 只看人工改动
```

---

## 3. API

| 端点 | 语义 |
|---|---|
| `POST /session/create` | `{ prefix_path, project, config }` → `{ session_id, prefix_hash, stages }` |
| `POST /session/{id}/round` | `{ o_i?, round_index?, stage?, dry_run? }` → `{ C_i, ΔC_i, merge_report, validation, judge, next_prompt, usage }` |
| `GET /session/{id}/state` | `C_current` + 事实链 / 失败链 / 依赖图 / 约束状态 / metrics |
| `GET /session/{id}/artifacts` | 工作区文件清单 + 逐个 sha256 |
| `GET /session/{id}/metrics` | 曲线数据（P0 违反数 / P1 成本分项 / P2 完成度 / 压缩质量 / 自我报告） |
| `GET /session/{id}/log` | 轨迹日志（`rounds.jsonl` + `events.jsonl`），`?include_prompts=true` 带全文 |
| `POST /session/{id}/revert` | `{ to_index }` |
| `DELETE /session/{id}` | 删会话 |
| `GET /arms` | 实验设置表 |

`round` 的 `o_i` **可选**，这是消融路径与产品路径的统一 seam：
缺省 → server 自己调工作模型；传入 → 直接用（回放、注入、离线复现）。

### 3.1 OpenAI 兼容入口：`/v1`

`src/pciso/openaiapi.py` 让 pciso 能被任何 OpenAI 客户端直接当模型用（DSH 里就是一个 provider）。
两种模式，由 `PCISO_OPENAI_MODE` 选，**默认 `proxy`**：

| 模式 | 配置 | 语义 |
|---|---|---|
| `proxy`（默认） | `configs/openai_proxy.yaml` | **上下文替换代理**：工具循环在客户端（DSH 已经有 agent loop），pciso 只转发 + 换上下文（见 §3.1.1） |
| `round` | `configs/openai_chat.yaml` | 一轮 = 一次 `session.say`：服务端自己干活、落盘、判定。离线评估用 |

| 端点 | 语义 |
|---|---|
| `GET /v1/models` | `pciso`（默认配置 + `prefixes/general`），每个 `prefixes/<name>/` 派生一个 `pciso@<name>` |
| `POST /v1/chat/completions` | 转发（proxy）或跑轮（round）；`stream: true` 都走 SSE |

**会话身份**（协议本身没有会话概念，所以按这个顺序认）：
头 `X-PCISO-Session` / `X-Session-Id` / `X-Conversation-Id` → body 的 `user` → **首条真 user 消息**的 sha256 前 16 位（`fp:...`）。
响应头回传 `X-PCISO-Session`（客户端可以自己存下来下轮带上），`X-PCISO-New: 1` 强制开新会话；
注册表在 `state/openai_sessions.json`。头里给的若是本 server 的会话 id 就直接接上，但**只认 `arm == chat`**。

**包装层**：DSH 塞进 user 消息的 `<system-reminder>`（技能目录之类）进门先剥，**不拿它当本轮任务、
也不算会话指纹**（不剥会变成"模型开口就谈技能目录"＋同一场对话开出两个会话）。

**回传**：正文在 `choices[0].message.content`；思维链原文在 `reasoning_content`（**只回传，不回灌上下文**）；
`usage` 是真 token（`prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`）；
顶层 `x_pciso` 带 `session_id / mode / c_rounds / c_ops`（round 模式另有 `round_index / verdict / files_changed`）。

#### 3.1.1 代理模式：转发 + 换上下文 + 编排思考

DSH 侧已经有 agent loop（它 filter assistant 的 `tool-call` 块去执行、把结果回传），所以服务端
**不执行工具**，只做三件事：

1. **转发**：`tools` / `tool_choice` / 采样参数原样转给上游，`tool_calls`、`finish_reason` 原样回去
   （流式是**字节级**透传）。吞掉 `tools`，客户端就没有工具可调 —— 那正是 DSH 里"工具调用没了"的真因。
2. **换上下文**：上游 prompt = 客户端 system（原样）+【上下文替换代理】块（P + C + 用户原话）
   + `messages[last_user_index:]`（本轮原样）。历史不回流，由 C 代表；用户原话逐字带回，不进压缩器。
3. **编排思考（IoT = Items of Thoughts 的核心）**：**一个 thinking 块一次编排，压缩后返回** ——
   客户端看到的那个 thinking 块**就是提纯后的紧凑版**；正文 / `tool_calls` / `usage` / `finish_reason`
   与原生行为一模一样（`raw` / `drop` 是同一开关下的对照臂）。

**块边界用"位置法"，不猜文本**：`proxyapi.StreamScan` 在第一个 reasoning 字段出现时记
`start = len(_raw)`，`delta.content` 或 `delta.tool_calls` 出现时记 `end = len(_raw)`，
`_raw[start:end]` 就是这一块原文；`(start, end)` 落进 `events.jsonl` 的 `proxy_call.think_spans`。
一次响应可以有**多个**块（工具往返之间各一段），每块各编排各回显 —— 旧写法只认第一块，
第二段思考会被静默吞掉。判断"这是不是思考"只按**字段名**（`reasoning_content` / `reasoning` /
`reasoning_text`）：正文里逐字写 `{"reasoning_content": …}` 或 `<thinking>` 都不算，转发时一个字节不动。

编排失败、或压缩器没给出紧凑版，就**回原文**（宁可不压，不可丢）。原文另外落盘
`state/sessions/<sid>/thinking/{index:03d}.txt`，拿客户端的块与它对比就能验"真的换了"。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `PCISO_PROXY_CONTEXT` | `iot` | `iot`（块换条目）/ `raw`（块保持原文）/ `drop`（块丢掉）/ `full`（整份 messages 原样透传 = 标准 agent 基线） |
| `PCISO_PROXY_WORKSPACE` | 会话配置 | 解析锚点、判断"哪些文件真被写了"用的工作区根 |
| `PCISO_PROXY_TAIL` / `PCISO_PROXY_MIN_ABSORB` | 6 / 4 | 尾部保留多少条、至少攒够多少条才做历史替换 |

工具往返不喂压缩器：它们由服务端**确定性抽成 ACT 节点**（零模型调用，比对盘上文件的 sha256/字节数）。
轮次记录落 `rounds.jsonl`（`kind` ∈ `thinking_block` / `tool_actions`，`c_i` 就是那个事实链，
用 `scripts/_inspect_proxy_c.py` 看）；上游拒绝的原文记进 `events.jsonl` 的 `proxy_upstream_error`。

一轮里服务端还会顺手做五件事（都零模型调用，读的全是客户端发来的原始事实）：

- **路径归一化**：工具给的是绝对路径（`D:\_playground\workspace\oneline\out\x.png`），模型写的
  anchor 是相对路径（`oneline/out/x.png`）—— 两侧都按工作区根归一成 posix 相对路径再比，并按 `/`
  边界做双向后缀匹配。不归一化的话 ACT 一条都落不下来、带路径的思考节点还会被判成"编路径"丢掉
  （真机：298 轮 `kept=0`、`think_dropped_unanchored=27`）。
- **锚点闸门只拦编造**：旧规则是"路径不在本轮点过名的集合里就丢"，于是锚向**盘上真有、但这轮没动过**
  的文件（讨论 `spec.py` 时它就在那儿）也被判成编造 —— 真机会话 3881a53e1bd84ed1 里一整块 27 条
  全丢、`kept=0`。现在的判据是 `_anchor_on_disk()`：文件存在、或它所在目录存在就放行
  （新建文件本来就在规划里），只有连目录都是编的才丢。离线复盘同一会话：旧规则留 63 条 THINK
  → 新规则留 121 条（救回 58 条，`dropped_unanchored` 18 轮里 17 轮归零）。
- **工具报错进链**：同一个工具连续 ≥2 次返回同一类报错 → 失败链一条 + 一条 `[否掉]` 负节点
  （`不再调用 X（连续 N 次报错，这条路当前不可用）`），计数记在 meta `proxy_loop_reported`，
  每个增量只记一次。报错本来只活在当轮消息里、一被吸收就没了，于是模型没有回执可读，只能反复写
  "我这就去读"。
- **循环 / 停滞告警**：`loop_report()` 只看客户端发来的工具往返（同一调用指纹 ≥3 次、同指纹连续报错、
  连续 N 步只有话没有工具调用），命中就往请求末尾那块注入一行【循环告警】/【停滞告警】。不占 C、
  不花模型调用；停滞那条同时是断路开关 ——「下一步只允许一件事：发一个工具调用，不要写计划」。
- **跑过的命令也是 ACT**：`build_command_ops()` 把 `pwsh` 这类工具的命令 + `[exit code: N]` 记成
  `运行 \`python -m pytest -q\`（exit=0）`；同一条命令再跑一次就 SUPERSEDE（旧的退出码不许和新的一起
  挂着）。不记的话「测试过了」这类结论既没有 ACT 可挂、又会被"禁完成态断言"挡掉，两头落空 ——
  真机现场就是 `think_dropped_claim` 把「motion 子命令已冒烟通过（exit=0）」丢掉了。
  【验证状态】那一行也会读 `exit=` 判"已跑测试且通过 / 未通过"。
- **进度不再等于自述**：链上一条 ACT 都没有时，【当前进度】渲染成【自述进度·无工具回执核对】，
  并补一行【验证状态】0 条物证。实测（会话 `d87b61bb051b4849`）【当前进度】逐字就是模型自己写的
  "本轮打破只规划不执行的僵局：直接并行发 pwsh 列目录 + read_image"，它读到就以为已经执行了。

一条都没留下时（编排器判"没什么值得记的"，或全被闸门挡掉）那一块**退回不压缩**：原文逐字返回，
不加工也不截断。别拿"原文首句截 120 字"充数 —— 英文块没有 `。！？` 可切，会变成
`… I alre…`，读起来像提纯结果，实际是把一句话切一半。

上游脾气：thinking 模式下历史里的 assistant 消息必须把 `reasoning_content` 带回来，否则 400
`The reasoning_content in the thinking mode must be passed back to the API.`（探针实测）。`drop`
臂把思考删干净就必然撞上这条 —— 它是"故意破坏"的对照臂，别在它上面跑长任务。

真机冒烟（`scripts/_smoke_iot_stream.py`，纯推理题、3976 字思考）：客户端看到 **1259 字 13 条 THINK**
（压缩比 0.32），原文没泄漏、落盘可审计、C 记 1 条 `kind=thinking_block`。
修前同题是 `think_dropped_unanchored=12` —— 不碰文件的思考没有锚点可挂，12 条全被丢，紧凑版为空、
回退原文，机制等于没开。`scripts/_smoke_proxy_tools.py` 验的是另一条：thinking 模式下上游真的回了
`read_file({"path":"README.md"})`；同一轮的工具往返 `c_rounds` 保持 0；新的 user 消息进来才 `c_rounds=1 / c_ops=2`。

已知边界：①上游 thinking 模式**不接受** `tool_choice` 指定具体函数（400
`Thinking mode does not support this tool_choice`），代理不替客户端改语义，只把原文转回去；
②"边压边回"（块还在流的时候就替换）是次要功能，现在没做 —— thinking 阶段的 chunk 先扣住，
块结束后一次性回紧凑版，所以客户端看到的 thinking 是"顿一下再出现"而不是逐字流。

---

## 4. P 的结构

P 不是一段散文，是「散文 + 机器可读契约」的冻结包。`prefix_hash` 是整包哈希，进每一轮日志。

平台支持**多个实验载体**，P 按载体分目录；`fakeimpl/<载体名>/stages/` 放该载体的脚本 oracle。
配置里 `session.fake_oracle` 显式指定用哪个 oracle ——
prefix 被复制/改名之后（例如测试），按目录名猜 oracle 会落空，而那种失败看起来像"模型没干活"。

```
prefixes/one_sentence_edit/        ← 目标载体：一句话成片（见 §4.1）
  PROJECT.md          需求 / 硬边界 / 数据模型与规则规格（§6-§8 逐字写死）
  constraints.yaml    15 条约束（id / 原文 / 绑定检查 / 硬软标记）
  contracts.yaml      21 个接口契约（签名漂移的基准）
  stages.yaml         8 阶段分解 + 每阶段的 temptation（人为制造的诱惑点）
  layers.yaml         7 层允许的依赖方向
  tests_frozen/       冻结一致性测试（只读，每轮由 server 从 P 恢复）

prefixes/drawing_video/            ← 排练载体：绘图工具箱 + 视频工具链（oracle 完整、跑得快）
```

### 4.1 目标载体：一句话成片

参考形态是 Claude Motion / Opus 5.5 代码动效视频那条路线，它有三个结构性的点：

1. **画面是时间的确定性函数。** 页面/接口暴露 `render(t)`，给任意 t 就画出那一帧且结果永远相同。
   由此才能"任意一帧单独重算、预览与导出一致"。在我们的纯 Python 版本里就是
   `render_frame(timeline, t_ms, images)` 必须是纯函数。
2. **一条时间轴，多路输出。** 时间轴是唯一事实来源，画面 / 字幕 / 配乐 / 清单 / 播放页
   全部由它派生，因此**必须互相对齐** —— 这是可客观判定的性质，也是本载体的核心验收项。
3. **改一个细节，其余不变。** 这是他们的卖点，也正是本实验在量的东西：
   **约束不漂移**。所以这个载体和实验命题是同构的，不是随便挑的项目。

硬边界：禁止生成式模型（含用 LLM 解析那句话，否则不可复现）、禁止 shell out、
禁止第三方库。产物是 `movie.apng` + `subs.vtt` + `bgm.wav` + `plan.json` + `index.html` + 帧序列。
代价是明确的：素材限 PNG、字幕走 WebVTT 旁挂而不烧进画面（中文光栅化纯标准库做不到）、
默认 640×360@30fps。这些都是写进 P 的工程取舍，不是缺陷。

**冻结测试为什么是地基**：如果测试由工作模型自己写，那"测试通过率"完全可博弈 ——
写空断言即可。而且长程约束压力下**弱化测试恰恰是模型最自然的偷懒路径**，
那正是最该被测到的失效模式。所以一致性测试由 P 给定、只读、每轮恢复；
模型自写测试单独作为覆盖率约束；"修改/弱化既有测试"本身是一条硬约束（C14）。

---

## 5. 15 条约束：哪些客观、哪些不客观

原设计的 9 条里，真正客观可判定的只有 5~6 条，而且客观的那几条（ruff / TODO）在 8 轮内
几乎不会退化 —— 属于**空信号**。所以补了 C10 之后的结构性指标，它们才是长程退化真正显形的地方。

| id | 约束 | 检查 | 硬 |
|---|---|---|---|
| C1 | 模块边界清晰、单一职责 | `manual_cohesion` — **明确标 `passed=None`**，只给 fan-in/out 供人工判读 | |
| C2 | 已定义接口签名不破坏 | `interface_contract`（**漂移**才违规，缺失只算完成度） | ✔ |
| C3 | 不允许循环依赖 | `import_cycles` | ✔ |
| C4 | 每个公开接口有 docstring | `docstring_coverage` | |
| C5 | README + 架构文档存在 | `docs_presence` | |
| C6 | 覆盖率 > 80% | `coverage_proxy`（pytest-cov，不可用则退化为符号触达率并注明） | |
| C7 | 新增功能有对应测试 | `feature_tests`（孤儿公开符号数） | |
| C8 | PEP8 | `ruff` | |
| C9 | 无 TODO/FIXME/XXX 残留 | `todo_residue` | |
| C10 | 冻结测试不得被改/删 | `frozen_tests`（**首次缺失 ≠ 篡改**） | ✔ |
| C11 | 签名变更须同步全部调用点 | `stale_callers` | ✔ |
| C12 | 依赖方向不得逆向 | `layer_violation` | ✔ |
| C13 | 签名未变则 docstring 不得退化 | `docstring_drift` | |
| C14 | 不得删除/弱化既有测试 | `test_weakening`（用例数下降或文件被删） | ✔ |
| C15 | 交付时契约全部实现 | `contract_completeness` | |

三条"看起来像 bug 其实是裁定"的地方：

- **C2 只查漂移，不查缺失** —— 否则每个组第 1 轮都会被回退，曲线从起点就废了。
- **C10 区分首次缺失与篡改式缺失** —— 同理，第 1 轮冻结测试还没被恢复过。
- **C1 标 `passed=None`** —— 不可客观判定的绝不伪装成客观指标，否则曲线是假的。

---

## 6. 七组对照

| # | 配置 | history_mode | 目的 |
|---|---|---|---|
| A | `arm_a_single_shot` | full | 下界：一次能写多少 |
| B | `arm_b_full_history` | full | Self-Refine 全历史（P + 全部历史，无压缩） |
| C | `arm_c_fact_chain` | state | **目标方案** |
| D | `arm_d_frozen_full_history` | full | **P 漂出上下文**（P 仅第 1 轮出现） |
| E | `arm_e_rolling_summary` | summary | 滚动摘要的问题 |
| F | `arm_f_stateless_stage` | none | 无状态：连工作区清单都没有 |
| G | `arm_g_disk_retrieval` | none | **磁盘检索**：P + 清单 + `@@READ`，读盘由 server 执行 |

**F 组为什么必须有**：P 里已经写死 8 阶段分解。如果 F（每轮只给 P + "做第 k 阶段"，
不给历史也不给 C）的违反曲线和 C 差不多，那么 **C 的全部贡献是零** ——
增益来自 P 的阶段分解，不是工作记忆。这会是对本方案最重要的反驳，而 A/B/C/D/E 五组都测不出来。

**D 组被重新定义过。** 原设计写的是「P 冻结 + 完整历史，用来隔离 P 冻结变量」。
离线矩阵一跑就暴露：在"每轮重发整个 prompt"的形态下它与 B 的**内容完全相同**，
只差 API 侧前缀缓存命中率 —— 那是成本项，不是可观察内容项，原样保留只会得到两条重合曲线。
现在 D 测的是另一个真实失效模式：**P 不再被重述时，模型还能不能守住约束**。

**G 组把"磁盘不如在上下文中"变成可测量的。** 读盘由 server 执行，于是三类失败被计数：
无效检索率（申请不存在的路径）、冗余检索率（重读已给过的文件）、检索上下文成本（字节数）。

---

## 7. 已验收的事实

### 7.1 七组离线矩阵（各 8 轮，管线全通）

**目标载体 `one_sentence_edit`**（`|P| ≈ 5.6k token`）：

| 组 | 轮 | 回退 | 违反总数 | prompt 首 → 末 | 增长 | C 峰值 | 检索遥测 |
|---|---|---|---|---|---|---|---|
| A | 1 | 0 | 19 | 5594 | 1.00× | 350 | — |
| B | 8 | 0 | 74 | 5594 → **27038** | **4.83×** | 357 | — |
| C | 8 | 0 | 74 | 5729 → 7573 | **1.32×** | **1997** | — |
| D | 8 | 0 | 74 | 5594 → 21983 | 3.93× | 357 | — |
| E | 8 | 0 | 74 | 5584 → 12882 | 2.31× | 532 | — |
| F | 8 | 0 | 74 | 5566 → 5556 | 1.00× | 357 | — |
| G | 8 | 0 | 74 | 5791 → 5854 | 1.01× | 357 | 无效 55.6% / 冗余 33.3% / 8953B |

排练载体 `drawing_video`（`|P| ≈ 2.3k`）的同一张表在 `out/matrix/`，量级关系一致
（B 4.31×、C 1.34×）。两条曲线的**形状相同、常数不同** —— 这正是"O(1) 但常数是 |P|"的直接证据。

**这个矩阵证明了什么、没证明什么，必须分清：**

- **成本曲线有效** —— C/F/G 恒定、B/D/E 增长。这是 P1 那张图的真实数据。
- **违反数曲线在离线态是退化的** —— 假 oracle 的输出与上下文无关，
  所以凡能产出文件的组必然同分（这里七组都是 `19/14/12/11/9/5/4/0`，
  那其实是**天花板自己的收敛曲线**）。真正的 P0 曲线必须接真实模型（当前缺凭据）。

### 7.1.1 C 组的饱和与裁剪：**丢事实是错误，不是损耗**

按 [`principle.md`](principle.md) §1.2 的口径，这里记的不是"压缩损耗有多大"，而是**事实链被丢了什么、为什么还能接受**。

`c_max_tokens=2000` 时，C 组在第 7 轮涨到 42 条事实 / 1997 token，**开始裁剪**：

```
轮7: 裁掉 3 条 -> N20(budget,salience=30) / N21 / N22
轮8: 裁掉 7 条 -> N23..N26 都是 salience=30
```

两点值得记：

1. **裁剪是按显著度丢的，不是按新旧** —— 被丢的 10 条全部是 `salience=30`
   的"测试存在性"事实（下一轮用 AST 就能重新看出来），接口与架构决定一条没丢。
   这条策略在真实数据上证明了自己：如果按新旧丢，被丢的会是第 1–2 轮建立的类型定义。
2. **"O(1) 上下文"不是免费的** —— 它在 2000 token 处是靠**丢事实**换来的，
   而按 [`principle.md`](principle.md) §1.2 的口径，丢事实是**错误**而不是有损压缩的成本。
   这一轮之所以还能接受，是因为被丢的那一类恰好是"下一轮用 AST 就能重新看出来"的，
   即**可从磁盘重建**。所以真正该写进报告的不是"压缩率"，
   而是**"被丢的事实里有多少是不可从磁盘重建的"** —— 那才是这条策略的正确性代价。
   如果某一轮丢掉的是接口/架构决定，那就是一次货真价实的失败，不能记成成本。
   所以 P1 的成本对比里必须把 `report.trimmed` 作为**损耗项**列出来，
   否则就是拿"丢了东西的恒定"去比"没丢的增长"。
   想消掉这项就把预算抬到 3000，但那样常数也更大 —— 这是个真实的取舍，不是 bug。

### 7.2 判定器语义由故障注入验证

| 故障注入 | 触发 | 回退 | 说明 |
|---|---|---|---|
| `cycle` | 第 4 轮 C12 层违规、第 6 轮 C3 循环依赖 | **2（25%）** | 硬约束**新**违反才回退 |
| `break_signature` | 第 2 轮 C2 契约漂移 | **1** | 同上 |
| `drop_docstring` | C4 违反数一路涨到 222 | **0** | C4 是软约束：**只度量、不回退** |
| `touch_frozen` | 第 3 轮 C10 冻结测试被篡改 | **1** | 回退后 C 不前进、工作区被还原 |

`drop_docstring` 那一行是关键：如果软约束也一起回退，那条"违反数随轮数"的曲线
就永远是被摁平的零 —— 测量目标被判定器自己消掉了。

### 7.3 排练载体 8 轮冒烟

21 个测试全绿、`python demo.py` 真跑通、锚点可溯源率 **1.0**（85 个锚点）、
`declared_units=19 / derived_units=19 / unbacked_claims=0`（指标无假阳性）。

两条**跑出来的**观察（不是设计意图）：

1. **验证器在脚本 oracle 上都抓到了真缺陷** —— `ellipse` 与 `Clip` 无任何测试触达。
   说明这把尺子在"满分参照物"上也有信号，而不是只会全绿。
2. **回退不是免费的** —— 回退第 3 轮导致 `Canvas` 永久缺失，此后 C15 一直违反、再也补不回来。
   这正是"回退率"比"累积违反数"更有信息量的原因。

### 7.4 分数上限参照（ceiling）：尺子本身已被验证

「违反数」这个指标只有在**完美实现能拿满分**时才可信。否则它量的就不是模型，
而是尺子自己的毛病。所以两个载体都做了上限验证：

| 载体 | oracle 阶段/文件 | 冻结契约 | ruff | 8 轮顶点 |
|---|---|---|---|---|
| `drawing_video`（排练） | 8 / 9 | 21 passed | 干净 | — |
| `one_sentence_edit`（目标） | 8 / 12 | 40 passed | 干净 | **第 8 轮违反数 0（15 条约束全绿）** |

目标载体的 8 轮冒烟：违反数 `19 → 14 → 12 → 11 → 9 → 5 → 4 → 0`，0 次回退，
C 稳定在 1187/1200 token，prompt 5729 → 6763。

**这个上限验证过程本身抓到了三个"尺子的 bug"**，每一个都会静默污染曲线：

1. **排练 oracle 有 4 条 lint 债** → C8 对**所有**组恒红，那条线完全没有分辨力。
2. **两个载体的冻结测试都藏了 `importorskip`** → "模块没实现"会变成"跳过"，
   而跳过在 pytest 里算通过。等于给工作模型留了自我豁免的口子。
   修法有两道：P 自检禁止冻结测试出现 skip 形态；`pytest_suite` 把跳过直接计为失败。
3. **新 P 的冻结测试有一条断言与规格互相矛盾**（进度条）。
   我原先写的是"t=75% 时最右侧像素已变白"，它等价于要求进度条在 75% 就接近满格 ——
   实现为了让这条错断言变绿，被迫做了一条加速缓动曲线。
   **教训：当冻结测试就是验收标准时，写错的断言会把实现逼成错误的样子。
   发现矛盾要改断言，不要改规格。** 现在断言直接量填充边界，规格写明严格线性。

### 7.5 眼睛通道：模型看不到画面，人得能看到

工作模型是单发无工具的，**它永远看不到自己画的帧** —— 而视频开发最需要看画面。
`POST /session/{id}/preview` 用被测项目**自己的** `pipeline.make_video` 出一支片子，
产物落在 `state/previews/latest/`，再由 `GET /session/{id}/blob/...` 交给浏览器看。
它同时是一次端到端检查：接口没实现或跑不通，预览就失败并给出 traceback。

实测（目标载体的完整 oracle）：**360 帧、584 KB APNG、6 类产物齐全**，
同场景两帧有 20.1% 的字节差异 —— Ken Burns 位移缩放真的在动。
两条硬约束：预览**零侵入**（示例素材与输出都在 `state/` 下，还禁写字节码），
blob **挡掉 `.git`**（见下）。

**这条通道一上线就抓到三件事**，其中两件是别的测试都测不出来的：

1. **用纯色素材根本没法判断渲染器** —— 纯色上 Ken Burns 完全不改变采样结果，
   第一版示例素材就是纯色，成片看起来"什么都没发生"。现在示例素材带斜条纹与色块。
2. **`.git/config` 会被 blob 端点发出去**（HTTP 200）。`..` 穿越挡住了，但它就在工作区内，
   resolve 之后完全"合法"，而那个文件里可能有带 token 的 remote URL。
3. **中文经 `argv` 传进子进程会变成 `?`** —— 但这个锅不在被测项目：隔离验证后确认
   `plan_from_sentence` 与 `write_vtt` 的中文完好，是调用方把参数搞坏的。
   现在参数走 UTF-8 的 JSON 文件。（同一个坑还有一次：PowerShell 的
   `Invoke-RestMethod -Body <string>` 按控制台代码页编码，中文一样会变问号 ——
   手工验证时要显式按 UTF-8 编码 body。）

第 3 条是个值得记住的教训：**告警指向被测项目时，先隔离验证到底是谁的问题**。
如果当时直接去"修" `write_vtt`，就会把一个正确的实现改坏。

### 7.6 真机跑的两道保险

七组 × 8 轮要花二十分钟和真钱，所以最贵的两种失败模式都补上了：

| 保险 | 机制 | 验证 |
|---|---|---|
| **预算闸** | `max_total_tokens`（配置项，进实验设置表）。累计超限就让这一轮拒绝执行 | 限 1 token 时跑完第 1 轮即停，越界那轮不提交 |
| **断点续跑** | `pciso resume --session-id <sid>`；轮次幂等，**不重复计费** | 从 1/8 续跑到 4/8，历史无重复；`matrix` 出错时直接打印续跑命令 |

预算闸有个细节：**提供方不报 usage 时要退回 prompt 估算**。
脚本 oracle 与一部分 OpenAI 兼容端点都不返回 usage —— 累计恒为 0 的话，闸门就是摆设。

续跑还挡住一种污染：**换工作模型**。前几轮可能是真模型跑的，续跑时切到别的模型会让
同一组的轨迹混进两种模型，而"每组的模型"正是实验的自变量。这种污染不报错，但结论直接作废。

### 7.7 一个会毁数据的 bug（构建眼睛通道时暴露的）

`git_prepare_arm_branch` 原先在**没有基线提交的空仓库**里也执行 `clean -fd`，
而空仓库里所有文件都是未跟踪的 —— 于是它把整个工作区删空了。
真机上交付仓库的脚手架是已提交的，所以没炸；**但那是运气**。
现在基线不存在时它什么都不做。

### 7.8 相信曲线之前先跑体检

`scripts/verify_rehearsal.py <repo>` 逐组核对独立性不变量：

| 不变量 | 为什么它必须成立 |
|---|---|
| 每组第 1 轮提交的父提交就是基线 | 否则这一组从别组成品上出发 |
| 分支上没有属于别组的提交 | 继承会让整组数据失去对照意义 |
| 本组 git 提交数 == 轨迹轮数 | 提交失败却继续跑 = 溯源断了 |
| 每轮记录的 `git_sha` 非空 | 同上 |
| **所有组第 1 轮违反数相等** | 同一套冻结 P + 同一确定性起点，第 1 轮只可能有一个值；不一致就说明起点不同 |
| `post/` 在所有分支上都没被实验碰过 | 加固区只能属于人工 |

聚合报告**看不出**这些问题 —— 实测踩过一次（三处静默失败叠加，七组零提交、
后五组跑在前一组成品之上，而报告完美无缺）。所以这张表要逐组看，不能只看汇总。

体检脚本自己也被测（`tests/test_verify_rehearsal.py`）：用人为构造的
"继承前一组成品"、"提交失败还继续跑"、"post/ 被改" 三种坏现场证明它抓得住。
写这些测试时还逼出了体检脚本本身的一个弱点：它原先靠 `branch~n_commits` 推算
父提交，而继承会让提交数虚高、算出来**仍然落在基线上**。现在按 trailer 精确定位。

### 7.9 trailer 必须真的是 trailer（正文与 trailer 块之间要空一行）

工具会读 `%(trailers:key=PCISO-Round,valueonly)` 来区分"实验生成"与"人工加固"。
**git 只把"最后一段、且与正文用空行隔开"的 `Key: value` 当 trailer。**

实测：紧挨着正文写时

```
--grep='^PCISO-Round:'          -> 能搜到（纯文本搜索）
%(trailers:key=PCISO-Round)     -> 返回空字符串
```

也就是说，"提交信息里有这行字"和"它是可解析的 trailer"是两件事。
之前那条测试只断言了文本存在，所以一直蒙对。现在既有空行分隔的写入实现，
也有断言**解析结果**的测试 —— 只测 `--grep` 会让"逐轮可溯源"变成文本上的巧合。

### 7.10 目标载体的成本特征（真实的 P1 数据）

目标载体的 P 有 ~5.7k token（含数据模型、9 条规划规则、渲染规格、多路输出规格），
比排练载体的 P（~2.3k）大一倍多。这直接印证了 P1 那条曲线真正的形态：
**C 组是 O(1)，但常数是 |P|** —— 8 轮里 prompt 从 5729 涨到 6763，
而 B 组同期的涨幅是 4 倍量级。所以"恒定"不等于"便宜"，|P| 本身就是主要成本项。

另一个实测成本：目标载体的冻结套件单次约 20 秒，每轮要跑两遍（`pytest_suite` +
`coverage_proxy`），7 组 × 8 轮 ≈ 40 分钟纯验证开销。这是"客观验证"的实价，值得先知道。

### 7.11 真机 C 组 8 轮（第一份真实数据）

`deepseek-flash`，按设计原文 `thinking: true`（`max_tokens: 32000`），
写进交付仓库 `arm-C` 分支，8 个逐轮提交。

| 轮 | 判定 | 违反数 | 违反约束 | C token | 事实链 | 失败链 | prompt≈ | 硬违反 |
|---|---|---|---|---|---|---|---|---|
| 1 | accept | 16 | C15 | 351 | 0 | 0 | 5793 | — |
| 2 | accept | 11 | C15 | 863 | 5 | 0 | 5984 | — |
| 3 | accept | 10 | C15,C8 | 1117 | 9 | 0 | 6505 | — |
| 4 | accept | 9 | C15,C8 | 1764 | 18 | 0 | 6773 | — |
| 5 | accept | **23** | C15,C8 | 1922 | 21 | 0 | 7411 | — |
| 6 | accept | 19 | C15,C8 | 1997 | 21 | 0 | 7568 | — |
| 7 | accept | 20 | C8 | 1997 | 19 | 1 | 7623 | — |
| 8 | accept | 24 | C8 | 1935 | 18 | 2 | 7637 | — |

8 轮全部 accept、**0 回退**、token 合计 303122。

三条结论（都是离线矩阵给不出来的）：

1. **硬约束一次都没破。** 每轮"硬违反"都是空 —— 设计里 P0 的底线（不丢约束）站住了。
   所有违反都落在软约束上，且都对应真实未完成的工作（C15 = 契约尚未实现完，C8 = ruff 诊断）。
2. **总违反数不是单调下降**：9（轮 4）→ 23（轮 5）跳升后维持 19~24。
   原因是 C8/C15 这类软约束**随项目规模增长**（每多一个公开符号就多一次 docstring/
   覆盖率/测试触达的判定面）。所以"约束违反总数"被软约束主导，
   **做头图之前必须区分硬/软并把口径写清楚**，否则那张图会被软约束淹没，
   看起来像"越做越差"，而实际是"越做越大"。
3. **prompt 5793→7637（1.32×），C 饱和在 1997**（`c_max_tokens=2000`，第 6 轮起开始裁剪，
   事实 21→18，被裁的按设计是低 salience 的）。1.32× 与离线 C 组一致 ——
   **真实模型下 O(1) 上下文这条成立**。

产物（模型 8 轮写出来的，冻结契约测试全过）：`drawvid/` 8 个模块、
`docs/ARCHITECTURE.md`、`README.md`、`pyproject.toml`、`scripts/demo.py` +
`scripts/drawvid_cli.py`、9 个自测文件。眼睛通道跑它出片：360 帧、
六路产物齐全、中文字幕完好，帧上可见 Ken Burns 缩放与严格线性进度条。

---

## 8. 已知缺口（诚实清单）

- **目标载体 `one_sentence_edit` 的 oracle 已落地并验收通过**（阶段 8 / 12 文件 /
  冻结契约 40 passed / ruff 干净 / 8 轮顶点违反数 0）。它现在是"分数上限参照"。
- **真实模型矩阵没跑过**（缺凭据）。离线矩阵只验证管线与成本曲线，不验证假设 ——
  因为脚本 oracle 的输出与上下文无关，能产出文件的组必然同分。
  （**C 组单组 8 轮已跑通**，见 §7.11；A/B/D/E/F/G 六组仍待跑。）
- **G 组不是真 agent。** 它是"server 执行读盘"的确定性替身，故意把 harness 排除在外。
  真 agent 形态必须在轮内关掉 harness 自带压缩，否则就是双机制打架 —— 那是另一个实验。
- **C1（模块内聚）不可客观判定**，目前只给 fan-in/out 数据。要么接受人工判读，
  要么换成"单模块出边数/职责数"这类代理指标，但那等于换了约束。
- **`ruff` / `pytest-cov` 是可选依赖。** 缺了能优雅降级（`passed=None`），但 C8 / C6 就变暗号。
- **`c_max_tokens` 的取值远比原设计大。** 原设计写 400 token；实测目标载体的 C
  在 1600 时仍涨到 1581（32 条事实）——也就是说 400 会裁掉约 3/4 的事实链，
  等于把"工作记忆"变成"只有最近几条"。现在的七组统一用 **2000**。
  这不是调参口味问题：`c_max_tokens` 直接决定压缩损耗有多大，它是实验的一个自变量。
- **`@@UNIT` 的收益尚未在真实模型上验证。** 它当前的价值是**测量**（自我报告注水率）
  与压缩器的切分提示，不是压缩时机控制。
- **配置里 `mode: off` 必须加引号。** YAML 1.1 会把未加引号的 `off` 解析成布尔 `False`
  （`on`/`yes`/`no` 同理）。代码里已经认下这种写法，但源头仍应写 `mode: "off"`。
- **`state/workspaces/<sid>/` 与 `state/sessions/<sid>/snapshots/` 会随轮数增长。**
  真实 7 组 × 8 轮建议定期清理未选中会话的工作区。

---

## 9. 测试

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pytest -q -m "not slow"          # 546 passed, 37 deselected（本机实测 2026-10-18）
python -m pytest -q                        # 含端到端 8 轮集成（较慢）
python scripts/verify_oracle.py            # 验收所有载体的 oracle
```

下表只列主干（口径级的那几组）。`tests/` 下其余文件覆盖代理协议、流式块边界、usage 记账、git trailer、依赖一致性、机制不变量等 —— 它们是逐次真机 bug 的回归。

| 文件 | 覆盖 |
|---|---|
| `test_prefix.py` | **P 完整性**：约束绑定的检查名真实存在、冻结测试不得自我豁免、契约阶段号/键唯一性/层覆盖、层图无环、每阶段有 temptation |
| `test_pyast.py` | 规范化签名、相对 import 解析、docstring 探测 |
| `test_merge.py` | 复述去重、SUPERSEDE 级联、约束原文不可改写、**按 salience 裁剪**、判定器语义 |
| `test_checks.py` | 首次缺失≠篡改、漂移≠缺失、分层违规、陈旧调用点、测试弱化、单检查异常隔离 |
| `test_compressors.py` | AST 抽取零幻觉、salience 按类别、失败链抽取、模型压缩器的平铺/嵌套 JSON 容错 |
| `test_oparse.py` | file 块解析、路径穿越拒绝、`@@UNIT` 与 `@@READ` 解析、JSON 抽取 |
| `test_render.py` | 四种 history_mode 的 prompt 隔离（P/C 不出现在该消失的地方） |
| `test_report.py` | SVG 转义与退化（空/单点/常数序列）、表格列数一致、CSV 逐轮一行 |
| `test_config.py` | **YAML `off` 陷阱**、十份配置逐份可加载、history_mode 推导、七组齐备 |
| `test_retrieval.py` | G 组检索往返、无效/冗余检索记账、清单不含内容、预算严格、F 组无清单 |
| `test_lifecycle.py` | 端到端 8 轮、冻结测试每轮恢复、**回退时 C 不前进**、P 改动使会话失效、无据声明计数 |
| `test_thinking_nodes.py` | ACT 由 server 渲染 / THINK 由模型提取 / 两条硬规则在服务端执行的**口径测试**（含"把计划记成事实"那次真机事故的回归） |
| `test_iot_orchestration.py` | 编排流水线：一个 thinking 块 → 思考节点；工具往返 → ACT 节点（压缩器换 stub） |
| `test_stream_blocks.py` | 思考块边界按**位置**取，绝不按文本判（正文里写 `<thinking>` 也不算） |
| `test_proxy_api.py` | `/v1` 代理：原样转发 + 历史替换 + **块级替换（压缩后返回）** |
| `test_mechanism_invariants.py` | 对**任意**模型输出都必须成立的机制不变量（此前 bug 的共同形状：核心产物已坏还继续跑且不报警） |
