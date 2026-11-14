// 教师咨询（Teacher consult）：由**正在做这件事的模型**主动调用，而不是插件猜时机。
//
// 为什么不靠规则判断时机：重要性是任务状态，不是文本特征。长会话里 [决定]/[风险]
// 早就出现过，"首次承诺"这类信号会失效；用户回一句"继续"也不是新需求。
// 只有执行中的模型知道"我现在卡在哪、下一步要做什么"。
//
// 但调用权交给模型不等于放任：插件负责三件事 ——
//   ① 系统提示词骨架固定（模型只能填槽位，不能改骨架），保证输出结构与质量稳定；
//   ② 上下文由插件按 depth 抽取并固定格式化（模型只决定"要几层"）；
//   ③ 冷却闸门（CD）：调用后进入冷却，冷却状态注入 think，模型自己看得到。

import { combineSignals, describeError, errorCodeOf } from "./effort.mjs";

/** 咨询的产出形态。枚举而不是自由文本：强制模型先想清楚"要什么"，输出也可预期。 */
export const ASK_KINDS = Object.freeze(["advice", "plan", "framework", "approach", "review"]);

/** 每种形态的固定输出结构。骨架由插件写死，模型参数只填槽位。 */
const ASK_TEMPLATES = Object.freeze({
  advice: [
    "输出 1~3 条**可执行**建议，每条一行，以 [建议] 开头。",
    "每条必须引用你看到的证据（用户需求/上一轮结论/当前进展），说明为什么现在要做这件事。",
    "不要复述已知内容；不要给需要更多背景才能成立的空话。",
  ].join("\n"),
  plan: [
    "输出一个**简短方案**：目标 → 关键步骤（3~6 步）→ 每步的验证方式 → 主要风险。",
    "步骤必须能直接执行，不要写「做好架构设计」这类无法验收的话。",
    "总长度控制在 400 字以内。",
  ].join("\n"),
  framework: [
    "输出一个**分析框架**：把问题拆成 3~5 个维度，每个维度给出判断标准与常见陷阱。",
    "框架要能直接套在当前需求上，不要写成通用方法论。",
  ].join("\n"),
  approach: [
    "输出**一条思路**：换一个角度看这个问题，指出当前做法可能忽略的方向。",
    "必须说明这个视角为什么在当前情境下成立，以及它会带来什么不同做法。",
  ].join("\n"),
  review: [
    "做一次**审阅**：指出当前进展里的错误、遗漏与不一致，按严重程度排序。",
    "每条给出证据（引用你看到的片段）与修正建议；没有问题的部分不要提。",
  ].join("\n"),
});

/**
 * 系统提示词骨架。**固定**，不随模型参数变化 —— 这是 skill/tool 与"用户直接调强模型"
 * 的本质区别：质量稳定靠的是骨架，不是靠模型自觉。
 */
export const TEACHER_SYSTEM = [
  "你是一位资深工程师，被另一个模型请来做一次限时咨询。",
  "",
  "你的边界：",
  "- 你看到的是插件按固定格式抽取的上下文（用户需求、进展、最近几轮结论），不是完整对话。",
  "- 上下文里没有的，不要假设；需要外部信息才能判断的事，明确说「需要确认」。",
  "- 你只给判断与方向，不重写对方的思考过程，也不替他做完整实现。",
  "",
  "输出纪律：",
  "- 只输出正文，不要开场白、不要总结、不要解释你在做什么。",
  "- 结论先行；能一句话说清就不要写三句。",
  "- 不确定的地方标出来，不要用模糊措辞掩盖。",
].join("\n");

/** 默认上限：与 advisor 保持一致，避免咨询把主上下文撑爆。 */
export const TEACHER_LIMITS = Object.freeze({
  maxTokens: 1500,
  maxChars: 1200,
  minDepth: 1,
  maxDepth: 5,
  inputBudget: 12000,
  cooldownBlocks: 3,
});

/**
 * 把配置形态归一成内部 limits：配置里写的是 `depthRange: [min, max]`，
 * 内部用的是 `minDepth/maxDepth`。两处调用方（stream 注入状态行、tool 执行）
 * 必须走同一个归一化，否则状态行会显示 `深度 undefined-undefined`。
 */
export function normalizeTeacherLimits(cfg = {}) {
  const raw = cfg && typeof cfg === "object" && !Array.isArray(cfg) ? cfg : {};
  const range = Array.isArray(raw.depthRange) && raw.depthRange.length === 2
    ? [Math.floor(Number(raw.depthRange[0])), Math.floor(Number(raw.depthRange[1]))]
    : null;
  const minDepth = Number.isFinite(range?.[0]) ? Math.max(1, Math.min(5, range[0])) : TEACHER_LIMITS.minDepth;
  const maxDepth = Number.isFinite(range?.[1]) ? Math.max(1, Math.min(5, range[1])) : TEACHER_LIMITS.maxDepth;
  const [low, high] = minDepth <= maxDepth ? [minDepth, maxDepth] : [maxDepth, minDepth];
  return {
    ...TEACHER_LIMITS,
    minDepth: low,
    maxDepth: high,
    cooldownBlocks: Number.isFinite(raw.cooldownBlocks) ? Math.max(0, Math.floor(raw.cooldownBlocks)) : TEACHER_LIMITS.cooldownBlocks,
    inputBudget: Number.isFinite(raw.inputBudget) ? Math.max(1000, Math.floor(raw.inputBudget)) : TEACHER_LIMITS.inputBudget,
    maxTokens: Number.isFinite(raw.maxTokens) ? Math.max(200, Math.floor(raw.maxTokens)) : TEACHER_LIMITS.maxTokens,
  };
}

/** 冷却状态：按会话记"下一次可用时的块序号"。只暴露冷却，不暴露总额。 */
const TEACHER_STATE = new Map();

function stateOf(sessionId) {
  const key = String(sessionId ?? "unknown");
  const current = TEACHER_STATE.get(key);
  if (current) return current;
  const fresh = { blocks: 0, availableAt: 0, calls: 0, lastOutcome: null, reservation: null, disposed: false };
  TEACHER_STATE.set(key, fresh);
  return fresh;
}

/** 每结束一个 thinking 块推进一次块计数：CD 按"块"计量，不按时间（长任务里时间会失真）。 */
export function noteBlock(sessionId) {
  const state = stateOf(sessionId);
  state.blocks += 1;
  return state;
}

/**
 * 只读的状态描述：给 think 注入用。**不修改状态** —— 渲染/追加可能发生多次，
 * 每次递减会让冷却凭空走快。
 */
export function teacherStatus(sessionId, limits = TEACHER_LIMITS) {
  const state = stateOf(sessionId);
  const remaining = Math.max(0, state.availableAt - state.blocks);
  if (remaining <= 0) return { available: true, remainingBlocks: 0, calls: state.calls };
  return { available: false, remainingBlocks: remaining, calls: state.calls, cooldown: limits.cooldownBlocks };
}

/** 状态行：固定格式、固定位置，进白名单（不计节点预算、不被重映射）。 */
export function formatTeacherStatus(status, limits = TEACHER_LIMITS) {
  if (status.available) {
    return `[教师] 可用（深度 ${limits.minDepth}-${limits.maxDepth}；调用后冷却 ${limits.cooldownBlocks} 块）`;
  }
  return `[教师] 冷却中 · 还需 ${status.remainingBlocks} 块`;
}

/**
 * 调用成功后进入冷却。失败/超时**不**进冷却：一次没生效的咨询不该让模型付出冷却代价。
 */
export function markConsulted(sessionId, limits = TEACHER_LIMITS, reservation = null) {
  const state = stateOf(sessionId);
  if (reservation !== null && state.reservation !== reservation) return teacherStatus(sessionId, limits);
  state.reservation = null;
  if (state.disposed) {
    TEACHER_STATE.delete(String(sessionId ?? "unknown"));
    return { available: false, remainingBlocks: 0, calls: state.calls };
  }
  state.calls += 1;
  state.availableAt = state.blocks + Math.max(0, Math.floor(limits.cooldownBlocks));
  state.lastOutcome = "ok";
  // 互斥：这一轮已经问过教师了，同一轮（以及紧接着的下一块）不再触发外部建议 ——
  // 一次咨询就是一次对话，没必要在同一轮里再打一次强模型。
  state.mutexUntil = state.blocks + 1;
  return teacherStatus(sessionId, limits);
}

/**
 * 互斥判定：刚咨询过教师的那一轮不再追加外部建议。
 * 返回 true 表示本次建议应当跳过（记 advisor-skipped: teacher-consulted）。
 */
export function adviceMuted(sessionId) {
  const state = stateOf(sessionId);
  return typeof state.mutexUntil === "number" && state.blocks <= state.mutexUntil;
}

/** 调用前校验：冷却中直接拒绝（tool 的硬约束，不只是提示）。 */
export function canConsult(sessionId) {
  const state = stateOf(sessionId);
  const status = teacherStatus(sessionId);
  return status.available && state.reservation === null;
}

/** Atomically reserve one consultation slot for this session. */
export function reserveConsult(sessionId) {
  const state = stateOf(sessionId);
  if (!teacherStatus(sessionId).available || state.reservation !== null) return null;
  const token = Symbol(String(sessionId ?? "unknown"));
  state.reservation = token;
  return token;
}

/** Release a failed/cancelled reservation without entering cooldown. */
export function releaseConsult(sessionId, token) {
  const key = String(sessionId ?? "unknown");
  const state = TEACHER_STATE.get(key);
  if (!state || state.reservation !== token) return;
  state.reservation = null;
  if (state.disposed) TEACHER_STATE.delete(key);
}

export function clearTeacherState(sessionId) {
  const key = String(sessionId ?? "unknown");
  const state = TEACHER_STATE.get(key);
  if (!state) return;
  if (state.reservation !== null) state.disposed = true;
  else TEACHER_STATE.delete(key);
}

export function resetTeacherState() {
  TEACHER_STATE.clear();
}

/** 会话历史里一条可用的消息。 */
function textOf(message) {
  if (!message) return "";
  const content = message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && (block.type === "text" || block.type === "input_text") && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n")
    .trim();
}

/**
 * 从会话里抽取最近 `depth` 轮（用户消息 + 助手**正文**）。
 *
 * 为什么只要正文：thinking 是"为了产出正文"服务的中间产物，而教师要看的是任务
 * 的真实状态 —— 上一轮到底交付了什么、用户提了什么要求。assistant message 里
 * 只有正文（reasoning 是流里的块，不进 message），所以这里天然不会带上思考过程。
 * 工具调用细节同理：`textOf` 只收 text/input_text 块。
 *
 * 拿不到会话（非 web 组合/接口变化）时返回空数组，调用方降级为"只有当前进展"。
 */
export function extractSessionContext(session, depth) {
  const rounds = [];
  if (!session) return rounds;
  let log = [];
  try {
    log = Array.isArray(session.log) ? session.log : [];
  } catch {
    return rounds;
  }
  // 从尾部往前扫，凑够 depth 轮用户消息就停：早期历史对"当前任务"的价值很低。
  // 反向扫描时先遇到的是**助手**消息（它跟在用户消息后面），所以先把结论存起来，
  // 等扫到对应的用户消息时再合并 —— 顺序反过来会把每一轮拆成两条。
  let pendingAssistant = "";
  for (let index = log.length - 1; index >= 0 && rounds.length < depth; index -= 1) {
    const event = log[index];
    let message = null;
    try {
      message = session.surface?.deriveEventMessage?.(event) ?? null;
    } catch {
      message = null;
    }
    if (!message || (message.role !== "user" && message.role !== "assistant")) continue;
    const text = textOf(message);
    if (!text) continue;
    if (message.role === "assistant") {
      // Reverse scan sees the final assistant message first. Keep that newest body;
      // intermediate/tool-stage assistant messages must not overwrite it.
      if (!pendingAssistant) pendingAssistant = text;
      continue;
    }
    rounds.push({ user: text, assistant: pendingAssistant });
    pendingAssistant = "";
  }
  return rounds.reverse();
}

/**
 * 组装咨询提示词。上下文格式固定：需求 → 进展 → 最近轮次 → 当前草稿 → 输出要求。
 * 顺序固定是为了让模型每次看到同样的结构，减少输出漂移。
 */
export function buildTeacherPrompt({ goal, progress, rounds, draft, ask, limits = TEACHER_LIMITS }) {
  const kind = ASK_KINDS.includes(ask) ? ask : "advice";
  const budget = Math.max(1000, Math.min(40000, Number(limits.inputBudget) || TEACHER_LIMITS.inputBudget));
  const roundText = (Array.isArray(rounds) ? rounds : [])
    .map((round, index) => {
      const user = String(round.user ?? "").slice(0, 800);
      const assistant = String(round.assistant ?? "").slice(0, 600);
      return `【第 ${index + 1} 轮】\n用户：${user || "（无）"}\n助手结论：${assistant || "（无）"}`;
    })
    .join("\n\n");

  const sections = [
    TEACHER_SYSTEM,
    "",
    "## 当前需求",
    String(goal ?? "（未提供）").slice(0, 600),
    "",
    "## 进展综述",
    String(progress ?? "（未提供）").slice(0, 1200),
    "",
    "## 最近轮次（由插件抽取，只保留正文）",
    roundText || "（没有可用的历史轮次）",
    "",
    "## 当前思考草稿",
    String(draft ?? "").slice(0, 4000) || "（未提供）",
    "",
    "## 本次要你做什么",
    ASK_TEMPLATES[kind],
    "",
    "直接输出正文，不要重复上面的结构标题。",
  ];
  const text = sections.join("\n");
  return text.length > budget ? `${text.slice(0, budget)}\n（上下文已按预算截断）` : text;
}

/**
 * 一次咨询。同步等待（模型主动调用的代价由它自己承担），但**必须有终点**：
 * 超时返回明确结果，不能把主任务挂死。
 */
export async function consultTeacher({
  ctx,
  Llm,
  route,
  prompt,
  options,
  timeoutMs = 30000,
  maxTokens = TEACHER_LIMITS.maxTokens,
  effort,
  temperature,
  trace,
}) {
  // Caller cancellation is not a failed consultation or a successful partial answer.
  options?.signal?.throwIfAborted();
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const signal = combineSignals(controller?.signal, options?.signal);
  const deadline = Date.now() + Math.max(1, timeoutMs);
  const remaining = () => Math.max(0, deadline - Date.now());

  const consume = async () => {
    const assembler = new Llm.BlockAssembler();
    let usage = null;
    try {
      const stream = ctx.llm.stream({
        provider: route.provider,
        model: route.model,
        messages: [
          Llm.createUserMessage({
            content: [{ type: "text", text: prompt }],
            source: { kind: "plugin", plugin: "iot-thinking" },
          }),
        ],
        maxTokens: Number.isFinite(maxTokens) ? Math.max(200, Math.floor(maxTokens)) : TEACHER_LIMITS.maxTokens,
        ...(temperature === null || temperature === undefined ? {} : { temperature }),
        ...(effort === null || effort === undefined ? {} : { reasoningEffort: effort }),
        purpose: "compaction",
        ...(signal === undefined ? {} : { signal }),
      });
      for await (const chunk of stream) {
        signal?.throwIfAborted();
        if (chunk?.type === "usage") usage = chunk?.usage ?? chunk;
        assembler.push(chunk);
      }
    } catch (error) {
      return { error: describeError(error), code: errorCodeOf(error), usage, finish: assembler.finish };
    }
    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      return { error: describeError(finish.failure ?? "unknown"), code: finish.failure?.code, usage, finish };
    }
    const text = assembler
      .blocks()
      .filter((block) => block && block.type === "text" && typeof block.text === "string")
      .map((block) => block.text)
      .join("\n")
      .trim();
    return { text, usage, finish };
  };

  const ms = remaining();
  if (ms <= 0) return { ok: false, error: `教师咨询超时（${timeoutMs} ms）`, code: "TEACHER_TIMEOUT" };
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ error: `教师咨询超时（${timeoutMs} ms）`, code: "TEACHER_TIMEOUT" }), ms);
  });
  let onAbort;
  const cancelled = new Promise((_, reject) => {
    onAbort = () => {
      controller?.abort(options.signal.reason);
      reject(options.signal.reason ?? new DOMException("教师咨询已取消", "AbortError"));
    };
    if (options?.signal?.aborted) onAbort();
    else options?.signal?.addEventListener("abort", onAbort, { once: true });
  });
  let result;
  try {
    const consumed = consume().catch((error) => ({ error: describeError(error), code: errorCodeOf(error) }));
    result = await Promise.race([consumed, timeout, cancelled]);
    options?.signal?.throwIfAborted();
  } finally {
    clearTimeout(timer);
    options?.signal?.removeEventListener("abort", onAbort);
    controller?.abort?.();
  }

  if (result.error) return { ok: false, error: result.error, code: result.code, usage: result.usage ?? null };
  if (!result.text) return { ok: false, error: "教师返回了空正文（可能被思考预算吃光）", code: "TEACHER_EMPTY", usage: result.usage ?? null };

  const completionTokens = Number(result.usage?.completion_tokens ?? result.usage?.output_tokens);
  const reasoningTokens = Number(result.usage?.completion_tokens_details?.reasoning_tokens ?? result.usage?.reasoning_tokens);
  const meta = {
    provider: route.provider,
    model: route.model,
    chars: result.text.length,
    ...(Number.isFinite(completionTokens) ? { completionTokens } : {}),
    ...(Number.isFinite(reasoningTokens) ? { reasoningTokens } : {}),
    finish: result.finish?.kind ?? null,
  };
  if (Array.isArray(trace) && trace.length < 8) {
    trace.push({ stage: "teacher", provider: route.provider, model: route.model, prompt, raw: result.text, finish: meta.finish, usage: result.usage ?? null, error: null, code: null });
  }
  return { ok: true, text: result.text, meta };
}

/** 从工具调用上下文里尽力取出会话 id：不同宿主版本字段位置不同，取不到就退化为全局。 */
export function sessionIdOf(exec) {
  const candidates = [
    exec?.sessionId,
    exec?.agent?.sessionId,
    exec?.agent?.session?.id,
    exec?.agent?.session?.header?.id,
  ];
  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "unknown";
}

/** 取会话对象：`ctx.get("sessions")` 在 web 组合里有；headless 可能没有。 */
export function sessionOf(ctx, sessionId) {
  try {
    const sessions = typeof ctx.get === "function" ? ctx.get("sessions") : undefined;
    const session = sessions?.get?.(sessionId);
    if (session) return session;
  } catch {
    // 取不到就当作没有历史：咨询仍可用，只是上下文更薄。
  }
  return null;
}
