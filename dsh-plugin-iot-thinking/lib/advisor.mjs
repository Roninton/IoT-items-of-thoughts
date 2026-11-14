// 指导层：压缩之后，让另一个（通常更强的）模型看一遍推理骨架，给出下一步建议。
//
// 为什么是"压缩之后再指导"：压缩把上下文从几万字降到几百字，指导模型只需读
// 这几百字 + 最近的动作结果。这比"把整段对话喂给强模型"便宜一个数量级，
// 同时保住了强模型对方向的判断力。
//
// 指导与压缩是两件事，不要合并成一个档位：
//   - 压缩是"减 token"，档位越高留得越多；
//   - 指导是"加信息"，它不改压缩结果，只在后面追加一段外部评审。
// 把两者塞进一个档位会让"想少留一点但又要指导"变得没法表达。

import { normalizeRouteTarget } from "./route.mjs";
import {
  AUTO_EFFORT,
  combineSignals,
  describeError,
  errorCodeOf,
  isUnsupportedEffortFailure,
  isUnsupportedTemperatureFailure,
  knownTemperaturePlan,
  rememberTemperaturePlan,
  temperaturePlanKey,
  knownEffortPlan,
  normalizeEffort,
  pickLowEffort,
  planKey,
  rememberEffortPlan,
} from "./effort.mjs";

/**
 * 建议输出的标签：独立于压缩节点，不参与节点预算与优先级排序。
 *
 * 为什么叫"旁观者建议"而不是"指导"：「指导」听起来像权威指令，主模型容易照办；
 * 而外部模型其实只看到这一轮草稿，它是旁观者，不是老师。标签本身就说明了
 * 这条建议的**证据边界** —— 建议型、可忽略、不构成命令。
 */
export const ADVISOR_LABEL = "[旁观者建议]";

/** 旧标签：升级前写进配置或模型输出里的 `[指导]`，读的时候仍认。 */
export const ADVISOR_LABEL_LEGACY = "[指导]";

/** 指导条数与长度的默认上限：宁少勿多，泛泛而谈的建议不如不给。 */
export const ADVISOR_LIMITS = Object.freeze({
  maxAdvice: 3,
  maxChars: 600,
  // 中文短建议很常见（"先补测试。"只有 5 个字），门槛太高会把它们全滤掉。
  minChars: 4,
});

/**
 * 解析指导路由：`advisor.route` 优先；没配就复用压缩器路由（同一台强模型）。
 * 与压缩路由一样，必须在钩子内部解析 —— 档位是按会话/preset 选出来的。
 */
export function resolveAdvisorRoute(cfg, strategyId, sessionRoute) {
  const advisor = cfg?.advisor;
  // 开关由调用方按 guidance 判断（旧 advisor.enabled 已折进 guidance.enabled）。
  // 这里不再要求 advisor.enabled === true —— 否则 UI 只写 guidance 时永远拿不到路由。
  if (!advisor || typeof advisor !== "object") return null;
  const explicit = normalizeRouteTarget(advisor.route);
  const fallback = cfg?.rewrite ? normalizeRouteTarget(cfg.rewrite) : null;
  const target = explicit ?? fallback ?? sessionRoute ?? null;
  if (!target?.provider || !target?.model) return null;
  return {
    ...target,
    source: explicit ? "advisor" : cfg?.rewrite ? "rewrite" : "session",
    // 卸载 = 指导模型与当前会话模型不是同一条路由。
    offloaded: sessionRoute
      ? target.provider !== sessionRoute.provider || target.model !== sessionRoute.model
      : true,
    strategyId,
  };
}

/**
 * 指导提示词。
 *
 * 刻意把"只给可执行的下一步"写死：强模型最容易犯的错是复述已知信息、
 * 或者给一堆"可以考虑…"的空话。这些注入回去只会浪费上下文。
 */
export function buildAdvisorPrompt({ draft, nodes, goal, recent, limits }) {
  const maxAdvice = Math.min(ADVISOR_LIMITS.maxAdvice, Math.max(1, Number(limits?.maxAdvice) || ADVISOR_LIMITS.maxAdvice));
  const maxChars = Math.min(ADVISOR_LIMITS.maxChars, Math.max(80, Number(limits?.maxChars) || ADVISOR_LIMITS.maxChars));
  const recentLines = Array.isArray(recent) && recent.length
    ? recent.slice(-6).map((entry) => `- ${String(entry).slice(0, 200)}`).join("\n")
    : "（还没有工具结果）";
  return [
    "你是一位资深工程师，正在审阅另一个模型**这一轮**的思考过程。",
    "",
    "边界（先读这一条）：",
    "- 你只看到这一轮思考的草稿，以及这一轮里刚发生的动作结果。",
    "- 你看不到更早的对话、项目历史、用户偏好。**不要假设**你知道这些。",
    "- 因此你的建议必须能在这份草稿内部闭环：要么指出草稿自身的缺口，要么给出",
    "  草稿里已经出现的东西可以直接执行的下一步。需要外部信息才能判断的事，不要硬说。",
    "",
    "这一轮的任务目标（如果为空，就以草稿本身为准）：",
    String(goal ?? "（未提供）").slice(0, 400),
    "",
    "这一轮的原始思考草稿：",
    String(draft ?? nodes ?? "").slice(0, 12000),
    "",
    "这一轮刚发生的动作结果：",
    recentLines,
    "",
    `输出最多 ${maxAdvice} 条、合计不超过 ${maxChars} 字的建议，每条一行，以 ${ADVISOR_LABEL} 开头。`,
    "写作要求：",
    "1. 每条要么是「草稿里没考虑到、但草稿本身已经提供了判断依据」的缺口，",
    "   要么是「用草稿里已有的信息就能执行的下一步/检查」。",
    "2. 不要复述草稿已经写下的内容；不要给需要更多背景才能成立的泛泛建议。",
    "3. 优先说「哪里可能错」「漏了什么检查」「先做哪一步最省事」。",
    "4. 没有值得说的就少说，甚至只说一条；不要凑数。",
    "5. 不要输出解释、开场白、总结，只输出建议行。",
  ].join("\n");
}

/** 把模型输出整理成指导节点：只保留带标签或能归入标签的行，去重，限条数。 */
export function formatAdvice(raw, limits = {}) {
  const maxAdvice = Math.min(ADVISOR_LIMITS.maxAdvice, Math.max(1, Number(limits?.maxAdvice) || ADVISOR_LIMITS.maxAdvice));
  const maxChars = Math.min(ADVISOR_LIMITS.maxChars, Math.max(80, Number(limits?.maxChars) || ADVISOR_LIMITS.maxChars));
  const lines = String(raw ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    // 模型有时会把标签写成 `指导：` / `【指导】` / `旁观建议`，统一成方括号。
    .map((line) => line.replace(/^[\[【]?(?:旁观者建议|旁观建议|指导|建议|observer|advisor)[\]】]?\s*[:：]?\s*/iu, `${ADVISOR_LABEL} `))
    .filter((line) => line.startsWith(ADVISOR_LABEL) && line.slice(ADVISOR_LABEL.length).trim().length >= ADVISOR_LIMITS.minChars);

  const seen = new Set();
  const out = [];
  let used = 0;
  for (const line of lines) {
    const body = line.slice(ADVISOR_LABEL.length).trim();
    if (seen.has(body)) continue;
    seen.add(body);
    if (out.length >= maxAdvice) break;
    if (used + line.length > maxChars && out.length > 0) break;
    out.push(line);
    used += line.length + 2;
  }
  return out.join("\n\n");
}

/**
 * 一次指导调用。失败一律返回 null —— 指导是**增强**，不是必需；
 * 它挂了不该影响这一块的压缩结果，也不该阻塞主流程。
 */
/**
 * 指导模型的思考档位：与压缩器同样的 auto 语义 —— 先试 off，被拒就探这个路由
 * 公开的最低档，再被拒就不发字段，并把结论按 route+mode 记住，避免每块白试一次。
 * 指导是并发的一路，多一次降级调用会拖慢它，所以缓存尤其重要。
 */
async function effortForAdvisor({ ctx, route, requested, options, onEvent }) {
  const mode = normalizeEffort(requested) ?? AUTO_EFFORT;
  const key = planKey(route.provider, route.model, mode, "advisor");
  const known = knownEffortPlan(key);
  const send = known !== undefined ? known : mode === AUTO_EFFORT ? "off" : mode;
  return { mode, key, send };
}

async function probeLowEffortForAdvisor({ ctx, route, options, onEvent }) {
  const llm = (typeof ctx.get === "function" ? ctx.get("llm") : undefined) ?? ctx?.llm;
  if (typeof llm?.resolveModelInfo !== "function") return { answered: false, effort: null };
  try {
    const info = await llm.resolveModelInfo(route.provider, route.model, options?.signal);
    return { answered: true, effort: pickLowEffort((info?.reasoning?.efforts ?? []).map((effort) => effort?.id)) };
  } catch (error) {
    onEvent?.("advisor-effort-probe-failed", { provider: route.provider, model: route.model, message: String(error?.message ?? error) });
    return { answered: false, effort: null };
  }
}

export async function advise({ ctx, Llm, route, prompt, options, timeoutMs, onEvent, trace, effort, temperature, maxTokens }) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const signal = combineSignals(controller?.signal, options?.signal);
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller?.abort?.();
  }, timeoutMs);
  timer.unref?.();

  // 超时必须是**竞速**而不是"只发 abort"：适配器的异步迭代器不一定响应 abort，
  // 一旦它不响应，`for await` 会永远挂着，collectAdvice 的 await 就永不返回 ——
  // 整块不输出，用户看到的就是"任务卡住"。这里用 deadline 兜底。
  const deadline = Date.now() + Math.max(1, timeoutMs);
  const remaining = () => Math.max(0, deadline - Date.now());

  const { mode, key, send } = await effortForAdvisor({ ctx, route, requested: effort, options, onEvent });
  // 指导模型的 temperature 同样按路由记住：只接受默认值的模型不能每块白试一次。
  const tempKey = temperaturePlanKey(route.provider, route.model, "advisor");
  const knownTemp = knownTemperaturePlan(tempKey);
  let temperatureToSend = knownTemp !== undefined ? knownTemp : (temperature === undefined ? 0.3 : temperature);

  /** 一次调用；超时与传输错误都以 `{ error }` 返回，交给外层决定是否降级。 */
  const callOnce = async (effortSent, tempSent) => {
    const assembler = new Llm.BlockAssembler();
    let usage = null;
    /** 真正消费流的那一半；超时后它仍在后台跑，结果由竞速丢弃。 */
    const consume = async () => {
    try {
      const stream = ctx.llm.stream({
        provider: route.provider,
        model: route.model,
        // 与压缩调用同一形态：`createUserMessage` 在宿主里要的是对象（content 数组），
        // 直接传字符串会抛 `content is not iterable` —— 这就是"开了外部建议却什么都没有"。
        messages: [
          Llm.createUserMessage({
            content: [{ type: "text", text: prompt }],
            source: { kind: "plugin", plugin: "iot-thinking" },
          }),
        ],
        // 输出预算按建议字数推算：中文 1 字 ≈ 1.5~2 token，固定 800 会在 maxChars=600 时截断。
        maxTokens: Number.isFinite(maxTokens) ? Math.max(200, Math.floor(maxTokens)) : 1200,
        ...(tempSent === null ? {} : { temperature: tempSent }),
        purpose: "compaction",
        ...(effortSent === null ? {} : { reasoningEffort: effortSent }),
        ...(signal === undefined ? {} : { signal }),
      });
      for await (const chunk of stream) {
        if (chunk?.type === "usage") usage = chunk?.usage ?? chunk;
        assembler.push(chunk);
      }
    } catch (error) {
      return {
        error: timedOut ? `指导调用超时（${timeoutMs} ms）` : describeError(error),
        code: timedOut ? "ADVISOR_TIMEOUT" : errorCodeOf(error),
        usage,
        finish: assembler.finish,
        effortSent,
      };
    }
    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      return { error: describeError(finish.failure ?? "unknown"), code: finish.failure?.code, usage, finish, effortSent };
    }
    if (finish && finish.kind === "aborted") {
      return { error: describeError(finish.failure ?? "指导调用被中断"), code: finish.failure?.code ?? "ABORTED", usage, finish, effortSent };
    }
    const raw = assembler
      .blocks()
      .filter((block) => block && block.type === "text" && typeof block.text === "string")
      .map((block) => block.text)
      .join("\n");
    return { raw, usage, finish, effortSent };
    };

    const ms = remaining();
    if (ms <= 0) {
      return { error: `指导调用超时（${timeoutMs} ms）`, code: "ADVISOR_TIMEOUT", usage, finish: assembler.finish, effortSent };
    }
    let timer = null;
    const timeout = new Promise((resolve) => {
      // 这个 timer **不能 unref**：流挂住时事件循环可能只剩它一个句柄，
      // unref 掉就永远不会触发，竞速也就永远不会 resolve。
      timer = setTimeout(() => resolve({ error: `指导调用超时（${timeoutMs} ms）`, code: "ADVISOR_TIMEOUT", usage, finish: assembler.finish, effortSent }), ms);
    });
    try {
      // 输掉竞速的那一半仍在后台消费流：加 catch 防未处理 rejection。
      const consumed = consume().catch((error) => ({
        error: describeError(error),
        code: errorCodeOf(error),
        usage,
        finish: assembler.finish,
        effortSent,
      }));
      return await Promise.race([consumed, timeout]);
    } finally {
      clearTimeout(timer);
    }
  };

  let result = await callOnce(send, temperatureToSend);
  if (result.error && knownTemp === undefined && isUnsupportedTemperatureFailure(result)) {
    onEvent?.("advisor-temperature-fallback", {
      provider: route.provider,
      model: route.model,
      requested: temperatureToSend,
      reason: "unsupported",
      message: result.error,
    });
    temperatureToSend = null;
    result = await callOnce(send, temperatureToSend);
    if (!result.error) rememberTemperaturePlan(tempKey, null);
  }
  // 档位不被支持：只对 auto 做降级（显式档位是配置信号，不擅自换档）。
  if (result.error && mode === AUTO_EFFORT && isUnsupportedEffortFailure(result)) {
    onEvent?.("advisor-effort-fallback", {
      provider: route.provider,
      model: route.model,
      requested: send,
      reason: "unsupported",
      message: result.error,
    });
    const probe = await probeLowEffortForAdvisor({ ctx, route, options, onEvent });
    const chosen = probe.effort;
    result = await callOnce(chosen);
    if (!result.error) rememberEffortPlan(key, chosen);
  }
  clearTimeout(timer);

  if (timedOut || result.code === "ADVISOR_TIMEOUT") {
    onEvent?.("advisor-failed", { provider: route.provider, model: route.model, message: `指导调用超时（${timeoutMs} ms）`, code: "ADVISOR_TIMEOUT" });
    return null;
  }
  if (result.error) {
    onEvent?.("advisor-failed", { provider: route.provider, model: route.model, message: result.error, code: result.code });
    return null;
  }

  const raw = String(result.raw ?? "").trim();
  const finish = result.finish;
  const usage = result.usage;
  // 自测留痕：指导这一路也记下实际 prompt 与正文输出，Log 窗口才能比对两路。
  if (Array.isArray(trace) && trace.length < 8) {
    trace.push({
      stage: "advisor",
      provider: route.provider,
      model: route.model,
      prompt,
      raw,
      finish: finish?.kind ?? null,
      usage,
      error: null,
      code: null,
      effort: result.effortSent ?? null,
    });
  }
  const reasoningTokens = Number(usage?.completion_tokens_details?.reasoning_tokens ?? usage?.reasoning_tokens);
  const completionTokens = Number(usage?.completion_tokens ?? usage?.output_tokens);
  onEvent?.("advisor-usage", {
    provider: route.provider,
    model: route.model,
    finish: finish?.kind ?? null,
    truncated: finish?.kind === "max-tokens",
    charsOut: raw.length,
    effort: result.effortSent ?? null,
    ...(Number.isFinite(completionTokens) ? { completionTokens } : {}),
    ...(Number.isFinite(reasoningTokens) ? { reasoningTokens } : {}),
    ...(Number.isFinite(completionTokens) && completionTokens > 0 && Number.isFinite(reasoningTokens)
      ? { reasoningShare: Number((reasoningTokens / completionTokens).toFixed(2)) }
      : {}),
  });
  if (!raw) {
    onEvent?.("advisor-skipped", {
      provider: route.provider,
      model: route.model,
      reason: finish?.kind === "max-tokens" ? "truncated-no-text" : "empty-text",
    });
    return null;
  }
  return raw;
}

/**
 * 把指导追加到压缩结果后面。
 *
 * 追加而不是替换：压缩结果是模型"自己的想法"，指导是外部评审。
 * 用独立标签隔开，主模型能分清哪部分是自己推出来的、哪部分是别人给的。
 * 指导**不参与**节点预算与截断 —— 否则它会挤掉 [决定] 这类关键节点。
 */
export function appendAdvice(nodes, advice) {
  const text = String(nodes ?? "").trim();
  const extra = String(advice ?? "").trim();
  if (!extra) return text;
  return text ? `${text}\n\n${extra}` : extra;
}
