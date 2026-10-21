// 基础三档的指令、prompt 装配、草稿净化、退化判定、节点渲染。

const REASONING_TYPES = new Set(["reasoning"]);

function blocksOf(message) {
  if (!message) return [];
  if (Array.isArray(message.content)) return message.content;
  if (typeof message.content === "string") {
    return [{ type: "text", text: message.content }];
  }
  return [];
}

function textOf(blocks) {
  let out = "";
  for (const block of blocks) {
    if (block && block.type === "text" && typeof block.text === "string") out += block.text;
  }
  return out;
}

/**
 * 从待压缩的 surface 里抽出全部 thinking 原文。
 *
 * 返回 `{ reasoning, visible, toolCalls }`：
 *   reasoning = 所有 reasoning 块的文本（**这是提纯的唯一原料**）
 *   visible   = 正文文本（只用来给条目定位"这轮在干什么"，不要求逐字保留）
 *   toolCalls = 工具名列表（条目里"做了什么"的物证索引）
 */
export function extractThinking(messages) {
  const reasoning = [];
  const visible = [];
  const toolCalls = new Set();
  for (const message of messages || []) {
    for (const block of blocksOf(message)) {
      if (!block || typeof block !== "object") continue;
      if (REASONING_TYPES.has(block.type) && typeof block.text === "string") {
        if (block.text.trim()) reasoning.push(block.text);
      } else if (block.type === "text" && typeof block.text === "string") {
        if (block.text.trim()) visible.push(block.text);
      } else if (block.type === "tool-call" && typeof block.name === "string") {
        toolCalls.add(block.name);
      }
    }
  }
  return {
    reasoning: reasoning.join("\n\n"),
    visible: visible.join("\n").slice(0, 4000),
    toolCalls: [...toolCalls],
  };
}

/** 整段历史是否真的含 thinking —— 不含就没有提纯的必要，直接跳过压缩。 */
export function hasThinking(messages) {
  for (const message of messages || []) {
    for (const block of blocksOf(message)) {
      if (block && REASONING_TYPES.has(block.type) && typeof block.text === "string" && block.text.trim()) {
        return true;
      }
    }
  }
  return false;
}

export const IOT_INSTRUCTION = `你是工作记忆的**推理编排器**，不是摘要器，也不是工作日志记录器。

输入是另一个模型的 thinking 草稿。目标：把它压成**可以直接接着干**的推理骨架。
不是把原文逐条搬进节点；每条节点必须是一次**归纳/综合**。

输出格式（纯文本；每个节点一行；节点之间空一行；不要 JSON、不要代码块、不要前言、不要编号、不要 Markdown 列表符号）：

[支持] 支撑某个判断/方案的理由、证据、正向约束
[反对] 被否掉的路线、反例、代价
[中性] 只写影响决策的硬事实/约束（版本、路径、接口限制、用户硬要求）
[计划] 接下来要做什么、顺序、验收动作
[权衡] 方案之间的比较、取舍、优先级
[待定] 还没定/需要确认的问题
[风险] 已定决策在什么条件下会出问题、如何兜底
[决定] 已经拍板的结论

节点预算（硬约束）：
- 目标 4~6 条，最多 8 条；原文再长也不要超过 10 条。
- [中性] 最多 2 条；超过就合并或删除。
- 每条尽量不超过 80 字，不要写成长段落。
- 同一主题/同一决定/同一证据只出现一次；能合并就合并。
- 只保留"会影响下一步"的内容；读完应能直接继续，而不是重放一遍工作日志。

各标签的取舍：
- [支持] 与 [决定] 不要重复：支持是理由，决定是结论；决定已说清的，理由可以合并进去。
- [反对] 同一路线只写一条；否掉的路线必须单独成条（[反对]），但不要把每条反对理由拆成多条。
- [中性] 不是"我看到了什么"的流水账：
  - 不要写"我读了 X / 我运行了 Y / 我发现了 Z"这类探索过程；
  - 不要逐文件、逐工具、逐条列举；
  - 只是背景、没有改变任何决定的观察，直接删掉；
  - 信息不一致/尚未确认的事放 [待定]，不要塞进 [中性]。
- [计划] 合并成 1~2 条（目标 + 顺序 + 验收），不要每一步一条。
- [权衡] 只写真正影响选择的差异，不写等价项。
- 其余标签没有内容就省略，不硬凑。

压缩规则：
1. **只压不补。** 文件名、符号、数字、异常类型必须在原文出现过；原文没有的一个字都不许写。
2. **禁止完成态断言。** 不写"已实现 / 已落盘 / 测试通过 / 验证通过"——完成状态只由工具回执给出。
3. **否掉的路线必须单独成条**（[反对]），不许合并成"考虑过其他方案"。
4. **没定的事不许写成定了的事。** 还在权衡的，用 [待定] 开头。
5. 先合并、再删减、最后才输出。输出前自检：
   - 有没有两条在说同一件事？合并。
   - 有没有只是"我读了/我运行了"的过程记录？删掉。
   - [中性] 是否超过 2 条？合并或删掉。
   - 总条数是否超过 8 条？合并或删掉。
6. **禁止退化重复。** 草稿可能残留上一轮的 [标签] 行，或含"好/现在/写/结束/执行/停止"这类单词。不要续写、不要模仿；把这些当噪声删除。每个节点必须是完整的判断/事实/计划（至少 12 个汉字）；禁止输出单词节点。若草稿只有噪声，输出空。
7. 如果发现自己在重复同一个词/短语，立刻停止，改为输出 1~3 条完整节点，或输出空。
8. 原文没有可保留的判断时，输出空；不要用单词填充。
9. 正文用中文。`;

/**
 * 压缩档位。三个档位不是"提示词换几个字"，而是不同的标签集合与节点预算：
 *   compact  — 3 标签，目标 3~4 条；最接近旧 IoT 的 ~85% 压缩率；
 *   balanced — 5 标签，目标 4~6 条；保留计划与决定；
 *   full     — 8 标签，目标 4~6 条；当前默认，质量优先。
 *
 * `map` 把 8 标签输出归并到该档位允许的标签；formatNodes 会按它做一次硬映射。
 */
export const PROFILES = Object.freeze({
  compact: Object.freeze({
    id: "compact",
    label: "极简（3 标签）",
    labels: ["支持", "反对", "待定"],
    maxNodes: 5,
    maxNeutral: 0,
    map: Object.freeze({ 中性: "支持", 计划: "待定", 权衡: "支持", 风险: "反对", 决定: "支持" }),
  }),
  balanced: Object.freeze({
    id: "balanced",
    label: "均衡（5 标签）",
    labels: ["支持", "反对", "待定", "计划", "决定"],
    maxNodes: 6,
    maxNeutral: 0,
    map: Object.freeze({ 中性: "支持", 权衡: "决定", 风险: "反对" }),
  }),
  full: Object.freeze({
    id: "full",
    label: "完整（8 标签）",
    labels: ["支持", "反对", "中性", "计划", "权衡", "待定", "风险", "决定"],
    maxNodes: 8,
    maxNeutral: 2,
    map: Object.freeze({}),
  }),
  // 长程研究档：目标函数不是"压缩到最短"，而是**不舍掉仍然活着的方案**。
  // 所以标签集与 full 一致（不引入新标签，避免 formatNodes 与旧输出失配），
  // 放宽的是预算与准入：节点更多、[中性]/[权衡]/[风险]/[待定] 都允许占用预算。
  research: Object.freeze({
    id: "research",
    label: "长程研究（多方案 / 待定 / 风险）",
    labels: ["支持", "反对", "中性", "计划", "权衡", "待定", "风险", "决定"],
    maxNodes: 12,
    maxNeutral: 3,
    map: Object.freeze({}),
  }),
});

export const COMPACT_INSTRUCTION = `你是工作记忆的**推理编排器**，不是摘要器，也不是工作日志记录器。

输入是另一个模型的 thinking 草稿。目标：用最少的节点保住“下一步不会走错”的信息。

只输出三类节点（纯文本；一行一条；节点之间空一行；不要 JSON、代码块、前言、编号或 Markdown 列表符号）：

[支持] 已定结论、关键事实、理由、证据
[反对] 被否掉的路线、反例、代价、风险
[待定] 还没定/需要确认的问题、下一步、验收动作

节点预算（硬约束）：
- 目标 3~4 条，最多 5 条；不要输出 [中性]。
- 每条尽量 ≤80 字；同一主题/证据只出现一次；能合并就合并。
- 剥掉探索过程（我读了 X / 我运行了 Y）；只留会影响下一步的内容。
- 只压不补：文件名、符号、数字必须在原文出现过；禁止完成态断言（已实现/已落盘/测试通过）。
- 草稿里如果出现 [中性]/[计划]/[权衡]/[风险]/[决定]，不要照抄；归并到 [支持]/[反对]/[待定]。
- 禁止退化重复：不要续写草稿里的标签或“好/现在/写/结束”这类词；每个节点必须是完整句子（至少 12 个汉字）；没有内容就输出空。
- 正文用中文。`;

export const BALANCED_INSTRUCTION = `你是工作记忆的**推理编排器**，不是摘要器，也不是工作日志记录器。

输入是另一个模型的 thinking 草稿。目标：把它压成能直接接着干的推理骨架，同时保留“决定了什么”和“下一步做什么”。

只输出五类节点（纯文本；一行一条；节点之间空一行；不要 JSON、代码块、前言、编号或 Markdown 列表符号）：

[支持] 支撑某个判断/方案的理由、证据、正向约束
[反对] 被否掉的路线、反例、代价、风险
[待定] 还没定/需要确认的问题
[计划] 接下来要做什么、顺序、验收动作
[决定] 已经拍板的结论

节点预算（硬约束）：
- 目标 4~6 条，最多 6 条；不要输出 [中性]。
- 每条尽量 ≤80 字；同一主题/决定/证据只出现一次；能合并就合并。
- [权衡] 归并到 [决定]（已选）或 [支持]（理由）；[风险] 归并到 [反对]；[中性] 归并到 [支持]。
- 只压不补：文件名、符号、数字必须在原文出现过；禁止完成态断言（已实现/已落盘/测试通过）。
- 禁止退化重复：不要续写草稿里的标签或“好/现在/写/结束”这类词；每个节点必须是完整句子（至少 12 个汉字）；没有内容就输出空。
- 正文用中文。`;

export const RESEARCH_INSTRUCTION = `你是工作记忆的**推理编排器**。这一档服务的是长程任务：研究、方案设计、跨文件重构这一类
"要好几轮才收敛"的工作。它不是摘要器，也不是工作日志记录器。

输入是另一个模型的 thinking 草稿。目标：保住**下一轮还要接着算的那部分**，而不是只保结论。

与决策档的差别（很重要，别按决策档的直觉压）：
- 决策档的目标是"下一步不会走错"；这一档的目标是"**下一轮还能接着想**"。
- 还在并行的多个方案、彼此之间的差异、各自的代价，**都要留下**；不要提前合并成一个"最优方案"。
- 未决问题、还没验证的假设、依赖谁先做，属于最贵的信息：宁可少写两条支持，也要留住它们。
- 被否掉的路线仍然单独成条（[反对]），但**否掉的理由**要一起留下 —— 下一轮可能会回来。

输出格式（纯文本；每个节点一行；节点之间空一行；不要 JSON、不要代码块、不要前言、不要编号、不要 Markdown 列表符号）：

[支持] 支撑某个判断/方案的理由、证据、正向约束
[反对] 被否掉的路线、反例、代价
[中性] 只写影响后续判断的硬事实/约束（版本、路径、接口限制、用户硬要求）
[计划] 接下来要做什么、顺序、验收动作
[权衡] 方案之间的比较、取舍、优先级（**允许保留未收敛的比较**）
[待定] 还没定/需要确认的问题、尚未验证的假设
[风险] 已定决策在什么条件下会出问题、如何兜底
[决定] 已经拍板的结论

节点预算（硬约束）：
- 目标 6~9 条，最多 12 条。
- [中性] 最多 3 条。[权衡] 最多 3 条（并行方案越多，这一档越该用它）。
- 每条尽量不超过 120 字；允许一条里写出"方案 A / 方案 B"的差异，但不要写成长段落。
- 同一证据只出现一次；能合并就合并 —— 但**不要为了短而牺牲"还有哪些路没走"**。

压缩规则：
1. **只压不补。** 文件名、符号、数字、异常类型必须在原文出现过；原文没有的一个字都不许写。
2. **禁止完成态断言。** 不写"已实现 / 已落盘 / 测试通过 / 验证通过"——完成状态只由工具回执给出。
3. **没定的事不许写成定了的事。** 还在权衡的，用 [待定] 或 [权衡] 开头。
4. 剥掉探索过程（"我读了 X / 我运行了 Y"），但**保留结论与仍然开放的分叉**。
5. 输出前自检：还有哪条并行的路线被我压没了？还有哪个未决问题被我写成已决了？补回去。
6. **禁止退化重复。** 草稿可能残留上一轮的 [标签] 行。不要续写、不要模仿；每个节点必须是完整句子
   （至少 12 个汉字）；禁止输出单词节点。若草稿只有噪声，输出空。
7. 原文没有可保留的推理时，输出空；不要用单词填充。
8. 正文用中文。`;

// 退化词：压缩器偶尔会把标签当成续写模板，吐出 [中性] 好 / [中性] 现在
// 这类单词节点。这些词单独成节点时没有信息量，必须在输入和输出两侧都拦。
const DEGENERATE_WORDS = new Set([
  "好", "现在", "写", "结束", "执行", "停止", "继续", "开始", "可以", "行", "对", "是", "不",
  "完", "走", "干", "做", "看", "说", "想", "然后", "接着", "下一步", "嗯", "哦",
]);

// 压缩器自己意识到“我在重复”时写下的元评论，也不是有效节点。
const META_PATTERN = /(?:这|本|当前)(?:轮|次)?(?:思考|推理).{0,8}(?:陷入|出现|产生).{0,4}(?:重复|循环)/;

const LABEL_PREFIX = /^\s*\[(支持|反对|中性|计划|权衡|待定|风险|决定)\]\s*/;

function fillerKey(text) {
  return String(text).replace(/[。！？!?，,、\s]/g, "");
}

function isFiller(text) {
  const key = fillerKey(text);
  return key.length > 0 && key.length <= 3 && DEGENERATE_WORDS.has(key);
}

/**
 * 压缩前净化草稿：
 *   - 剥掉上一轮残留的 [标签]，避免压缩器把它当输出模板续写；
 *   - 删掉只有“好/现在/写/结束”的单词行；
 *   - 折叠连续重复行；
 *   - 折叠多余空行。
 */
export function sanitizeDraft(text) {
  const lines = String(text ?? "").split(/\r?\n/);
  const out = [];
  let last = "";
  for (const raw of lines) {
    let line = raw.trim();
    if (!line) {
      if (out.length > 0 && out[out.length - 1] !== "") out.push("");
      continue;
    }
    line = line.replace(LABEL_PREFIX, "").trim();
    if (!line || isFiller(line)) continue;
    if (line === last) continue;
    out.push(line);
    last = line;
  }
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// 退化判定只统计"有实质内容"的行。语法骨架天然重复：一段真在写代码的草稿里
// ```python 围栏能出现 13~15 次，`return out` / `)` / `}` 更是一屏几十行。
// 旧判据 duplicate >= 3 把这些也算成循环，于是 23484 字的正常草稿被判退化、整块
// 换成 53 字兜底——比不压缩更糟（issue #3）。
const SUBSTANTIVE_CHARS = 20;
const DUPLICATE_MIN = 5;
const DUPLICATE_RATIO = 0.25;
// 同一句长行重复到**过半**才判主导循环：真循环几乎总是整段同一句（占比 ≈1），
// 而"一段草稿里 8 条 expect(...) 断言相同"是正常写法，不该把整块换成兜底。
const DOMINANT_REPEAT_MIN = 8;
const DOMINANT_RATIO = 0.5;

/** 草稿本身是否已经退化成单词/重复行。 */
export function isDegenerateDraft(text) {
  const lines = String(text ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0) return true;
  const contents = lines.map((line) => line.replace(LABEL_PREFIX, "").trim()).filter(Boolean);
  if (contents.length === 0) return true;
  const filler = contents.filter(isFiller).length;
  if (filler >= 3 && filler / contents.length >= 0.4) return true;

  const substantive = contents.filter((line) => line.length >= SUBSTANTIVE_CHARS);
  if (substantive.length === 0) return false;
  const counts = new Map();
  let repeated = 0;
  for (const line of substantive) {
    const seen = (counts.get(line) ?? 0) + 1;
    counts.set(line, seen);
    if (seen > repeated) repeated = seen;
  }
  const duplicate = substantive.length - counts.size;
  // 两种真循环：重复行占比够高（正常草稿里的重复是零散的），或同一句长行反复出现
  // 到成为草稿主体（单词循环由上面的 filler 判据抓）。
  // 判据刻意保守：误判的代价是**把正常内容换成兜底**，比漏判糟得多；漏判只多一次
  // 模型调用，输出侧还有质量门兜着。
  const looped =
    (duplicate >= DUPLICATE_MIN && duplicate / substantive.length >= DUPLICATE_RATIO) ||
    (repeated >= DOMINANT_REPEAT_MIN && repeated / substantive.length >= DOMINANT_RATIO);
  return looped;
}

/** 压缩结果是否退化（单词节点、重复内容、或压缩器的自我循环评论）。 */
export function isDegenerateItems(items) {
  // 压缩器可能不按空行分隔，直接一行一个节点；按“换行 + 行首标签”也切一刀。
  const nodes = String(items ?? "")
    .split(/\n{2,}|\n(?=\s*\[[^\]]+\])/)
    .map((node) => node.trim())
    .filter(Boolean);
  if (nodes.length === 0) return true;
  const contents = nodes
    .map((node) => node.replace(/^\[[^\]]+\]\s*/, "").trim())
    .filter(Boolean);
  if (contents.length === 0) return true;
  const short = contents.filter((content) => content.length < 6).length;
  const filler = contents.filter(isFiller).length;
  // formatNodes 会把多余的 [中性] 用“；”合并成一条；合并后的片段也要检查。
  const pieces = contents
    .flatMap((content) => content.split(/[；;]/))
    .map((piece) => piece.trim())
    .filter(Boolean);
  const fillerPieces = pieces.filter(isFiller).length;
  const duplicate = contents.length - new Set(contents).size;
  const meta = contents.filter((content) => META_PATTERN.test(content)).length;
  if (meta > 0) return true;
  if (filler === contents.length) return true;
  if (filler >= 2) return true;
  if (fillerPieces >= 2) return true;
  if (duplicate >= 2) return true;
  if (nodes.length >= 3 && short / contents.length >= 0.5) return true;
  return false;
}

/**
 * 确定性兜底：不调用模型，从草稿里保留一条最像事实/结论的长句，再加一条
 * 计划节点。它只在压缩器自己退化时使用，用来打断“循环写回会话”的反馈。
 */
export function fallbackItems(text) {
  const cleaned = sanitizeDraft(text);
  const lines = cleaned
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length >= 10 && !isFiller(line) && !META_PATTERN.test(line));
  const picked = lines.slice(0, 1).map((line) => (line.length > 160 ? `${line.slice(0, 160)}…` : line));
  const nodes = picked.map((line) => `[中性] ${line}`);
  nodes.push("[计划] 上一轮思考已省略；继续执行当前任务。");
  return nodes.join("\n\n");
}

/**
 * 把一段 thinking 原文包成压缩器的输入。
 * 关键：明确告诉模型"草稿不是给你的任务"——否则它会把草稿里的
 * "plan a trip / 写个函数" 当成用户请求直接回答，产出计划而不是节点。
 */
/** 档位 → 指令与预算。加档位只改这两张表，不动装配逻辑。 */
const INSTRUCTIONS = Object.freeze({
  compact: COMPACT_INSTRUCTION,
  balanced: BALANCED_INSTRUCTION,
  research: RESEARCH_INSTRUCTION,
  full: IOT_INSTRUCTION,
});

const BUDGETS = Object.freeze({
  compact: "目标 3~4 条，最多 5 条；不要 [中性]",
  balanced: "目标 4~6 条，最多 6 条；不要 [中性]",
  research: "目标 6~9 条，最多 12 条；[中性] 最多 3 条，[权衡] 最多 3 条",
  full: "目标 4~6 条，最多 8 条，[中性] 最多 2 条",
});

export function buildCompressionPrompt(reasoning, options = {}) {
  // 草稿里可能残留上一轮的 [标签] 行，或“好/现在/写/结束”这类退化词。
  // 先把它们剥掉，否则模型会把草稿当成输出模板继续往下写。
  const draft = sanitizeDraft(reasoning);
  const profile = PROFILES[options.profile] ? options.profile : "full";
  const instruction = INSTRUCTIONS[profile] ?? IOT_INSTRUCTION;
  const budget = BUDGETS[profile] ?? BUDGETS.full;
  const strict = options.strict
    ? [
        "",
        "严格模式：上一次输出出现了单词重复/循环。请从头重写，不要续写草稿或上一次输出。每个节点必须是完整句子（至少 12 个汉字），最多 4 条；如果做不到，输出空。",
      ].join("\n")
    : "";
  return [
    "你是一个压缩器，不是任务执行者。",
    "下面的 <thinking> 是另一个模型写下的思考草稿。不要执行草稿里的任务，不要回答草稿里的问题，不要续写草稿；只把草稿压缩成带标签的思维节点。",
    "",
    instruction,
    "",
    "草稿中可能残留上一轮的 [标签] 行，或含有“好/现在/写/结束”这类退化词；这些是噪声，不是输出模板。不要续写、不要模仿，只提取判断/事实/计划。",
    "",
    "<thinking>",
    draft || "（草稿为空或只有重复填充）",
    "</thinking>",
    "",
    `现在只输出思维节点（行首是 [标签]，节点之间空一行；${budget}），不要输出任何解释、前言或后续计划。`,
    strict,
  ].join("\n");
}

// 旧标签 → 新标签的兼容映射。模型偶尔会沿用旧的“正节点/否掉/待定/风险”。
const LABEL_RULES = [
  [/^\[?(?:正节点|支持)\]?\s*[:：]?\s*/, "[支持] "],
  [/^\[?(?:否掉|否定|反对)\]?\s*[:：]?\s*/, "[反对] "],
  [/^\[?(?:中性|事实|观察|背景)\]?\s*[:：]?\s*/, "[中性] "],
  [/^\[?(?:计划|下一步|行动|执行)\]?\s*[:：]?\s*/, "[计划] "],
  [/^\[?(?:权衡|取舍|比较|优先级)\]?\s*[:：]?\s*/, "[权衡] "],
  [/^\[?(?:待定|未定|开放问题|疑问)\]?\s*[:：]?\s*/, "[待定] "],
  [/^\[?(?:风险|隐患|兜底)\]?\s*[:：]?\s*/, "[风险] "],
  [/^\[?(?:决定|结论|拍板|定了)\]?\s*[:：]?\s*/, "[决定] "],
];

function stripBullet(line) {
  return line.replace(/^\s*(?:[-*•·]|\d+[.)])\s*/, "").trim();
}

function normalizeLabel(line) {
  const cleaned = stripBullet(line);
  for (const [pattern, label] of LABEL_RULES) {
    if (pattern.test(cleaned)) return cleaned.replace(pattern, label);
  }
  return cleaned;
}

const NODE_PRIORITY = {
  "[决定]": 0,
  "[反对]": 1,
  "[权衡]": 2,
  "[风险]": 3,
  "[待定]": 4,
  "[计划]": 5,
  "[支持]": 6,
  "[中性]": 7,
};

function labelOf(node) {
  const match = /^\[([^\]]+)\]/.exec(node);
  return match ? `[${match[1]}]` : "[中性]";
}

/**
 * 规范化压缩器输出：
 *   - 去代码围栏、去"条目/输出/结果"前缀；
 *   - 去行首 bullet / 编号；
 *   - 旧标签映射为新标签；
 *   - 每个节点一行，节点之间空一行；
 *   - 没有标签的续行并入上一个节点；整段都没有标签时补 [中性]；
 *   - [中性] 超过 maxNeutral 时合并多余项；
 *   - 总节点超过 maxNodes 时按优先级保留（决定 > 反对 > 权衡 > 风险 > 待定 > 计划 > 支持 > 中性），再恢复原顺序。
 * 只做格式整理，不替模型补内容。
 *
 * @param {string} text
 * @param {{ maxNodes?: number, maxNeutral?: number }} [options]
 */
export function formatNodes(text, options = {}) {
  let out = String(text ?? "").trim();
  out = out.replace(/^```[a-zA-Z0-9_-]*\s*\r?\n?/, "").replace(/\r?\n?```\s*$/, "").trim();
  out = out.replace(/^(?:条目|输出|结果)\s*[:：]\s*/, "").trim();
  if (!out) return "";
  const profile = PROFILES[options.profile] ?? PROFILES.full;

  const lines = out.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  let nodes = [];
  let current = null;
  for (const raw of lines) {
    const line = normalizeLabel(raw);
    if (/^\[[^\]]+\]/.test(line)) {
      if (current) nodes.push(current);
      current = line;
      continue;
    }
    if (current) current = `${current} ${line}`;
    else current = `[中性] ${line}`;
  }
  if (current) nodes.push(current);

  // 档位硬映射：8 标签 → 该档允许的标签。full 保留未知标签（证据/约束等），
  // compact/balanced 把未知标签归到 [支持]，避免整条被静默丢掉。
  const labelMap = profile.map ?? {};
  nodes = nodes
    .map((node) => {
      const name = labelOf(node).slice(1, -1);
      let target = labelMap[name] ?? name;
      if (!profile.labels.includes(target)) target = profile.id === "full" ? target : "支持";
      return target === name ? node : node.replace(/^\[[^\]]+\]/, `[${target}]`);
    })
    .filter(Boolean);

  const maxNodes = Math.min(
    Number.isFinite(options.maxNodes) ? Math.max(1, Math.floor(options.maxNodes)) : profile.maxNodes,
    profile.maxNodes,
  );
  const maxNeutral =
    profile.maxNeutral === 0
      ? 0
      : Number.isFinite(options.maxNeutral)
        ? Math.max(0, Math.floor(options.maxNeutral))
        : profile.maxNeutral;

  const neutralCount = nodes.filter((node) => labelOf(node) === "[中性]").length;
  if (neutralCount > maxNeutral) {
    if (maxNeutral === 0) {
      nodes = nodes.filter((node) => labelOf(node) !== "[中性]");
    } else {
      const keep = maxNeutral - 1;
      const mergedText = nodes
        .filter((node) => labelOf(node) === "[中性]")
        .slice(keep)
        .map((node) => node.replace(/^\[中性\]\s*/, ""))
        .join("；")
        .slice(0, 240);
      const next = [];
      let seen = 0;
      let merged = false;
      for (const node of nodes) {
        if (labelOf(node) !== "[中性]") {
          next.push(node);
          continue;
        }
        seen += 1;
        if (seen <= keep) next.push(node);
        else if (!merged) {
          next.push(`[中性] ${mergedText}`);
          merged = true;
        }
      }
      nodes = next;
    }
  }

  if (nodes.length > maxNodes) {
    const keep = nodes
      .map((node, index) => ({ index, priority: NODE_PRIORITY[labelOf(node)] ?? 99 }))
      .sort((a, b) => a.priority - b.priority || a.index - b.index)
      .slice(0, maxNodes);
    const keepIndexes = new Set(keep.map((entry) => entry.index));
    nodes = nodes.filter((_, index) => keepIndexes.has(index));
  }

  return nodes.join("\n\n");
}

/** Backwards-compatible alias for older imports/tests. */
export const cleanItems = formatNodes;
