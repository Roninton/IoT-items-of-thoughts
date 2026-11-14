// 改写引擎：只负责调用、失败分类、重试、记账；怎么改写全在 lib/strategies.mjs。
// 这次调用是手工构造的，不会被标成 agent-loop 请求，所以不会递归改写自己。

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
import { DEFAULT_STRATEGY, limitsOf, strategyById } from "./strategies.mjs";
import { hasLabel, hasThinkTag } from "./gates.mjs";
import { ADVISOR_LIMITS, advise, appendAdvice, buildAdvisorPrompt, formatAdvice, resolveAdvisorRoute } from "./advisor.mjs";
import { injectTextOf } from "./custom.mjs";
import { adviceMuted } from "./teacher.mjs";
import { isOffloaded, normalizeRouteTarget, resolveRewriteRoute } from "./route.mjs";

/** 改写结果 + 账本载荷。是否真的替换由 stream.mjs 判定后写进 replaced。 */
function rewritten(text, payload, advice = null) {
  // `advice` 单独带出来：主体先过替换判定，指导在替换之后再追加。
  return { text, event: payload, ...(advice ? { advice } : {}) };
}

/** 目录查询只是建议，不能卡住响应；测试会调小它。 */
export const EFFORT_PROBE = { timeoutMs: 3000 };

/**
 * 改写调用的硬上限。
 *
 * 没有这个上限时，模型"一直吐字但永不 finish"会让块结束路径一直等下去：
 * 客户端看到的是思考卡住，随后又被重试/回退放大成连续失败。
 * 默认 120s 是给本地大模型留的余量（27B 单块实测 30~45s）；超时后按失败处理，
 * 原文由 stream.mjs 原样放行 —— 宁可这一块不改写，也不能把会话挂住。
 */
export const REWRITE_TIMEOUT = { defaultMs: 120000, minMs: 1000, maxMs: 600000 };

/**
 * 单块调用的总预算。
 *
 * 重试本身不是问题，叠起来才是：effort 降级（最多 3 次）+ 首轮 + 重试 +
 * 回退（2 次）能让一块打到 7 次模型调用，用户看到的就是"一直吐字、一直失败"。
 * 这里给整块一个硬上限，超出即保留原文并记账。
 */
export const REWRITE_BUDGET = { defaultCalls: 4, minCalls: 1, maxCalls: 8 };

/**
 * 服务端计费口径归一：不同适配器给 `completion_tokens` / `output_tokens`，
 * 有的还单列 `reasoning_tokens`。这些数包含**被丢弃的思考**，所以会和
 * 最终可见的几百字对不上 —— 记账里把两者都写出来，省得靠猜。
 */
function normalizeUsage(chunk) {
  const raw = chunk?.usage ?? chunk;
  const pick = (...keys) => {
    for (const key of keys) {
      const value = raw?.[key];
      if (Number.isFinite(value)) return Math.floor(value);
    }
    return null;
  };
  return {
    promptTokens: pick("prompt_tokens", "input_tokens"),
    completionTokens: pick("completion_tokens", "output_tokens"),
    reasoningTokens: pick("reasoning_tokens", "reasoningTokens", "completion_tokens_details.reasoning_tokens"),
    totalTokens: pick("total_tokens"),
  };
}

/** 超时即拒，且不留下挂住的 timer。 */
function withTimeout(promise, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs} ms`)), timeoutMs);
    timer.unref?.();
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/** cfg 是 resolveConfig 的结果（strategy / limits / route 已就位）。返回 null 表示保留原文。 */
export async function compressThinking({ Llm, ctx, text, options, cfg, index, extra, archive, mark }) {
  const started = Date.now();
  const strategy = strategyById(cfg.strategy) ?? strategyById(DEFAULT_STRATEGY);
  const limits = cfg.limits ?? limitsOf(strategy.id, cfg.strategies?.[strategy.id]);
  const recent = Array.isArray(extra?.recent) ? extra.recent : [];
  const trace = Array.isArray(extra?.trace) ? extra.trace : null;

  // 改写器路由：档位覆盖 > rewrite 块 > 跟随当前请求。必须在钩子内部解析，
  // 因为档位是按会话/preset 选出来的，路由得跟着档位走。
  const sessionRoute = normalizeRouteTarget({ provider: options.provider, model: options.model });
  const route = resolveRewriteRoute(cfg, strategy.id, sessionRoute);
  if (!route.provider || !route.model) {
    mark("thinking-skipped", { index, strategy: strategy.id, reason: "no-route", routeSource: route.source });
    return null;
  }

  let offloaded = isOffloaded(route);
  mark("rewrite-route", {
    index,
    strategy: strategy.id,
    provider: route.provider,
    model: route.model,
    source: route.source,
    offloaded,
  });

  // 卸载路由失败时是否回退到会话路由。只回退一次，且只在两条路由确实不同时回退 ——
  // 否则同一路由会被连打两次，把一次网络抖动变成两次计费。
  const canFallback =
    cfg.rewrite?.fallback === "session" &&
    sessionRoute !== null &&
    offloaded &&
    (sessionRoute.provider !== route.provider || sessionRoute.model !== route.model);

  /** 当前实际使用的路由；回退后会被替换。 */
  let active = route;

  const maxChars = Number.isFinite(limits.maxChars) ? Math.max(1, limits.maxChars) : 24000;
  if (text.length > maxChars) {
    mark("thinking-skipped", { index, strategy: strategy.id, reason: "above-max-chars", chars: text.length, maxChars });
    return null;
  }

  // 策略准备失败就保留原文并记账，不降级去跑别的档位。
  const prepared = strategy.prepare(limits);
  if (prepared?.error) {
    mark("thinking-skipped", { index, strategy: strategy.id, reason: "strategy-unusable", detail: prepared.error });
    return null;
  }
  const state = prepared?.state ?? null;

  const archived =
    cfg.archive === false
      ? null
      : archive({
          sessionId: options.sessionId,
          index,
          text,
          provider: route.provider,
          model: route.model,
          strategy: strategy.id,
          // 预设文件路径也进归档头，便于事后区分"改写坏了"还是"预设不对"。
          ...(state?.path ? { style: state.path } : {}),
        });

  const maxTokens = Number.isFinite(limits.maxTokens) ? Math.max(1, limits.maxTokens) : 2000;
  const temperature = Number.isFinite(limits.temperature) ? limits.temperature : 0.2;

  // auto：先试 off，被拒则按路由公开的档位降级。改写器自己的档位可以单独配
  // （rewrite.effort），不必跟着会话走。
  const mode = normalizeEffort(cfg.rewrite?.effort ?? cfg.reasoningEffort) ?? AUTO_EFFORT;
  /** 最后一次实际发出的档位；null = 没发这个字段。 */
  let effortSent = null;
  let effortFallback = false;
  let called = false;

  const configuredCalls = Number(cfg.rewriteBudget);
  const maxCalls = Number.isFinite(configuredCalls)
    ? Math.min(REWRITE_BUDGET.maxCalls, Math.max(REWRITE_BUDGET.minCalls, Math.floor(configuredCalls)))
    : REWRITE_BUDGET.defaultCalls;
  /** 这一块已经发出去的调用数；超过预算就不再发。 */
  let callCount = 0;
  const overBudget = () => callCount >= maxCalls;

  const configuredTimeout = Number(cfg.rewriteTimeoutMs);
  const rewriteTimeoutMs = Number.isFinite(configuredTimeout)
    ? Math.min(REWRITE_TIMEOUT.maxMs, Math.max(REWRITE_TIMEOUT.minMs, Math.floor(configuredTimeout)))
    : REWRITE_TIMEOUT.defaultMs;

  const run = async (instruction, temperatureForCall, effort, target) => {
    if (overBudget()) {
      return { error: `本块调用预算已用尽（${maxCalls} 次）`, code: "REWRITE_BUDGET", finish: undefined };
    }
    callCount += 1;
    const assembler = new Llm.BlockAssembler();
    effortSent = effort;
    called = true;
    // 超时要真的把请求停掉，否则后台还在吐字、白烧 token。
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    // 宿主取消（Esc）与自己的超时都必须能中断这次调用。
    const signal = combineSignals(controller?.signal, options?.signal);
    let timedOut = false;
    let usage = null;
    // 默认收集：只记类型/长度，不记正文，成本很小；对端到底有没有 reasoning
    // 通道，靠这个而不是靠猜。要关掉就显式配 `debugChunks: false`。
    const debugChunks = cfg.debugChunks === false ? null : [];
    const timer = setTimeout(() => {
      timedOut = true;
      controller?.abort?.();
    }, rewriteTimeoutMs);
    timer.unref?.();
    /** 真正消费流的那一半；超时后它仍在后台跑，结果由竞速丢弃。 */
    const consume = async () => {
    try {
      const stream = ctx.llm.stream({
        provider: target.provider,
        model: target.model,
        // 不发字段 = 交给适配器默认值；off 仍是快路径（不花预算在自己的思考上）。
        ...(effort === null ? {} : { reasoningEffort: effort }),
        // null = 不发字段，交给模型默认值（有的模型只接受默认 temperature）。
        ...(temperatureForCall === null ? {} : { temperature: temperatureForCall }),
        messages: [
          Llm.createUserMessage({
            content: [{ type: "text", text: instruction }],
            source: { kind: "plugin", plugin: "iot-thinking" },
          }),
        ],
        maxTokens,
        sessionId: options.sessionId,
        // 辅助调用：合法的 purpose，且不是 agent-loop 请求，所以自己的钩子会忽略它。
        purpose: "compaction",
        ...(signal === undefined ? {} : { signal }),
      });
      for await (const chunk of stream) {
        // 只取正文块（assembler 的事）；usage 单独记：服务端统计的
        // completion_tokens 可能包含 reasoning 通道，而正文只有几百字。
        if (chunk?.type === "usage") usage = normalizeUsage(chunk);
        // 调试：记录对端实际送来的 chunk 形状（类型 + 文本长度），
        // 用来核对"到底有没有 reasoning 通道"，而不是靠猜。
        if (debugChunks && chunk?.type) {
          debugChunks.push({
            type: String(chunk.type),
            index: chunk.index ?? null,
            chars: typeof chunk.text === "string" ? chunk.text.length : null,
          });
        }
        assembler.push(chunk);
      }
    } catch (error) {
      return {
        error: timedOut
          ? `改写调用超时（${rewriteTimeoutMs} ms），已放弃这一块并保留原文`
          : describeError(error),
        code: timedOut ? "REWRITE_TIMEOUT" : errorCodeOf(error),
        finish: assembler.finish,
      };
    }
    if (timedOut) {
      return {
        error: `改写调用超时（${rewriteTimeoutMs} ms），已放弃这一块并保留原文`,
        code: "REWRITE_TIMEOUT",
        finish: assembler.finish,
      };
    }
    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      return { error: describeError(finish.failure ?? "unknown"), code: finish.failure?.code, finish };
    }
    // 中断不是成功：当成"空输出"会把好块换成兜底内容。
    if (finish && finish.kind === "aborted") {
      return {
        error: describeError(finish.failure ?? "rewriter call aborted"),
        code: finish.failure?.code ?? "ABORTED",
        finish,
      };
    }
    const output = assembler
      .blocks()
      .filter((block) => block && block.type === "text" && typeof block.text === "string" && block.text.trim())
      .map((block) => block.text)
      .join("\n");
    lastUsage = usage;
    lastChunks = debugChunks;
    return { output, finish, usage, chunks: debugChunks };
    };

    // 与建议链同样的道理：abort 不保证适配器真的停下，`for await` 可能永远挂着。
    // 用竞速兜底 —— 超时必须**返回**，不能让整块一直等。
    let raceTimer = null;
    const timeout = new Promise((resolve) => {
      raceTimer = setTimeout(() => resolve({
        error: `改写调用超时（${rewriteTimeoutMs} ms），已放弃这一块并保留原文`,
        code: "REWRITE_TIMEOUT",
        finish: assembler.finish,
      }), rewriteTimeoutMs);
    });
    try {
      const consumed = consume().catch((error) => ({
        error: describeError(error),
        code: errorCodeOf(error),
        finish: assembler.finish,
      }));
      return await Promise.race([consumed, timeout]);
    } finally {
      clearTimeout(raceTimer);
      clearTimeout(timer);
    }
  };

  /**
   * Ask the exact route which effort levels it advertises, so the rewriter can
   * keep thinking off (or nearly off) on routes that simply do not accept `off`.
   * Purely advisory: an unavailable or slow catalog never fails the block, and a
   * failed probe is reported as *unknown* rather than as "this route has no low
   * level" — caching the latter would cost a native DeepSeek route its `off` fast
   * path for the rest of the process after one catalog hiccup.
   *
   * @returns {Promise<{ answered: boolean, effort: string|null }>}
   */
  const probeLowEffort = async (target) => {
    const llm = (typeof ctx.get === "function" ? ctx.get("llm") : undefined) ?? ctx?.llm;
    if (typeof llm?.resolveModelInfo !== "function") return { answered: false, effort: null };
    try {
      const info = await withTimeout(llm.resolveModelInfo(target.provider, target.model, options.signal), EFFORT_PROBE.timeoutMs, "resolveModelInfo");
      return { answered: true, effort: pickLowEffort((info?.reasoning?.efforts ?? []).map((effort) => effort?.id)) };
    } catch (error) {
      mark("thinking-effort-probe-failed", {
        index,
        provider: target.provider,
        model: target.model,
        message: describeError(error),
      });
      return { answered: false, effort: null };
    }
  };

  /**
   * One rewriter attempt, including the effort-level recovery. The recovery is
   * not a retry of a failed rewrite: the request never reached the provider,
   * so it costs one local round-trip, and an answered route decision is cached.
   *
   * `target` is the route this attempt goes to; the effort decision is cached
   * per route+mode, so an offloaded route never inherits the session route's
   * verdict.
   */
  const attempt = async (instruction, temperatureForCall, stage, target) => {
    // role 进 key：建议链与改写链各自记账，不能互相污染（建议链探测失败曾把
    // 改写链的 off 快路径永久关掉）。
    const key = planKey(target.provider, target.model, mode, "rewrite");
    const known = knownEffortPlan(key);
    const send = known !== undefined ? known : mode === AUTO_EFFORT ? "off" : mode;
    // temperature 决策也按路由记住：有的模型只接受默认值（例如 GPT-6 Astra 只认 1），
    // 每次发 0.2 都会 400，白打一次调用。
    const tempKey = temperaturePlanKey(target.provider, target.model, "rewrite");
    const knownTemp = knownTemperaturePlan(tempKey);
    let temperatureToSend = knownTemp !== undefined ? knownTemp : temperatureForCall;
    let result = await run(instruction, temperatureToSend, send, target);
    // 参数约束先处理：temperature 被拒与 effort 无关，去掉字段重试一次即可。
    if (result.error && knownTemp === undefined && isUnsupportedTemperatureFailure(result)) {
      mark("thinking-temperature-fallback", {
        index,
        provider: target.provider,
        model: target.model,
        stage,
        requested: temperatureToSend,
        reason: "unsupported",
        message: result.error,
      });
      temperatureToSend = null;
      result = await run(instruction, temperatureToSend, send, target);
      if (!result.error) rememberTemperaturePlan(tempKey, null);
    }
    // 截断先于 effort 降级判断：换档位不解决输出预算，继续降级只会再截一次。
    if (result.finish && result.finish.kind === "max-tokens") return result;
    if (!result.error || !isUnsupportedEffortFailure(result)) return result;

    // `null` means "send no effort at all". If even that is refused, the adapter's
    // own default level is missing from its advertised list: nothing this plugin
    // can send will work, so name that cause instead of retrying.
    if (send === null) {
      mark("thinking-effort-unsupported", {
        index,
        provider: target.provider,
        model: target.model,
        stage,
        requested: null,
        reason: "adapter-default-unsupported",
        message: result.error,
      });
      return result;
    }

    mark("thinking-effort-fallback", {
      index,
      provider: target.provider,
      model: target.model,
      stage,
      requested: send,
      reason: "unsupported",
      message: result.error,
    });
    // Only auto mode goes looking for a cheap supported level. An explicit
    // choice that the route refuses is a configuration signal, not a hint to
    // pick some other level: send nothing and let the provider decide.
    const probe = mode === AUTO_EFFORT && send === "off" ? await probeLowEffort(target) : { answered: true, effort: null };
    let chosen = probe.effort;
    // 用**已经验证过**的 temperature：这一轮可能刚因为不支持 0.2 把字段去掉了，
    // 再发原始值会立刻又 400，降级链白跑。
    result = await run(instruction, temperatureToSend, chosen, target);
    effortFallback = true;
    // An adapter can advertise a level the host then refuses anyway (stale or
    // optimistic catalog). Omitting the field is the remaining option, so try it
    // once instead of leaving the route pinned on a level that fails every block.
    if (probe.answered && chosen !== null && isUnsupportedEffortFailure(result)) {
      chosen = null;
      result = await run(instruction, temperatureToSend, chosen, target);
    }
    // "No field at all" refused is the adapter's own default being unadvertised —
    // nothing this plugin can send will work. Name it, and do not cache it: the
    // adapter can be fixed, and a cache entry would pin the route for the process.
    if (chosen === null && isUnsupportedEffortFailure(result)) {
      mark("thinking-effort-unsupported", {
        index,
        provider: target.provider,
        model: target.model,
        stage,
        requested: null,
        reason: "adapter-default-unsupported",
        message: result.error,
      });
      return result;
    }
    if (probe.answered) rememberEffortPlan(key, chosen);
    return result;
  };

  /** 最近一次调用拿到的 usage；服务端计费口径，含被丢弃的思考 token。 */
  let lastUsage = null;
  /** 最近一次调用的原始 chunk 形状（只在 debugChunks 打开时收集）。 */
  let lastChunks = null;

  const payload = (fields) => ({
    index,
    provider: active.provider,
    model: active.model,
    routeSource: active.source,
    offloaded,
    strategy: strategy.id,
    // `effort: null` is meaningful only after a call went out (the field was
    // omitted); a draft skipped before the model has no effort.
    ...(called ? { effort: effortSent } : {}),
    ...(effortFallback ? { effortFallback: true } : {}),
    charsIn: text.length,
    ms: Date.now() - started,
    archived,
    ...(lastUsage ? { usage: lastUsage } : {}),
    // 思考占比：服务端 token 的大头若在 reasoning，说明这个路由关不掉思考，
    // 光调 maxTokens 只会更容易把正文截断。
    ...(lastUsage && Number.isFinite(lastUsage.reasoningTokens) && Number.isFinite(lastUsage.completionTokens) && lastUsage.completionTokens > 0
      ? { reasoningShare: Number((lastUsage.reasoningTokens / lastUsage.completionTokens).toFixed(2)) }
      : {}),
    ...(lastChunks ? { chunks: lastChunks } : {}),
    ...fields,
  });

  /**
   * 模式二：外部建议，与压缩**并发**两路。
   *
   * 指导模型拿的是**原始 thinking 草稿**（不是压缩骨架），压缩模型拿同一份草稿；
   * 两路结果合并成最终的 think 内容。并发是为了不让指导的延迟叠加到压缩上；
   * 指导失败只丢指导，压缩结果照常返回。
   */
  // 指导开关与模式来自配置（全局默认 + 会话覆盖）。直接传 cfg 的调用方
  // （测试、自测路由）可能没有 guidance 字段：按档位推断 —— 指导档等于内置指导，
  // 其余档默认关闭，与旧行为一致。
  const guidance = cfg.guidance
    ?? (Array.isArray(strategy.labels) && strategy.labels.some((label) => label === "旁观者建议" || label === "指导")
      ? { enabled: true, mode: "builtin" }
      : { enabled: false, mode: "external" });
  const guidanceOn = guidance.enabled === true;

  /** 指导请求的落地状态：`wait` 为 false 时，压缩一完成就只看它有没有恰好就绪。 */
  const adviceState = { settled: false, result: null, discarded: false };

  const startAdvice = () => {
    if (!guidanceOn) return null;
    // 外部指导模式：并发调用强模型。内置指导模式由提示词负责，不发第二路。
    if (guidance.mode !== "external") {
      mark("advisor-skipped", { index, strategy: strategy.id, reason: "builtin-mode" });
      return null;
    }
    // 互斥：这一轮已经咨询过教师，就不再追加外部建议（一次咨询就是一次对话）。
    if (adviceMuted(options?.sessionId)) {
      mark("advisor-skipped", { index, strategy: strategy.id, reason: "teacher-consulted" });
      return null;
    }
    const advisorRoute = resolveAdvisorRoute(cfg, strategy.id, sessionRoute);
    if (!advisorRoute) return null;
    const advisorCfg = cfg.advisor ?? {};
    const timeoutMs = Math.min(120000, Math.max(3000, Number(advisorCfg.timeoutMs) || 30000));
    const prompt = buildAdvisorPrompt({
      draft: text,
      goal: advisorCfg.goal ?? recent[0] ?? "",
      recent,
      limits: advisorCfg,
    });
    mark("advisor-called", {
      index,
      strategy: strategy.id,
      provider: advisorRoute.provider,
      model: advisorRoute.model,
      source: advisorRoute.source,
      offloaded: advisorRoute.offloaded,
      mode: "parallel",
    });
    return advise({
      ctx,
      Llm,
      route: advisorRoute,
      prompt,
      options,
      timeoutMs,
      trace,
      effort: advisorCfg.effort,
      temperature: advisorCfg.temperature,
      maxTokens: advisorCfg.maxTokens ?? Math.max(400, Math.ceil((Number(advisorCfg.maxChars) || 600) * 2)),
      onEvent: (event, fields) => mark(event, { index, strategy: strategy.id, ...fields }),
    }).then((raw) => {
      // 空输出/截断的原因已经由 advise 自己记账（advisor-skipped），这里不再重复。
      if (!raw) return null;
      const advice = formatAdvice(raw, advisorCfg);
      if (!advice) {
        mark("advisor-skipped", { index, strategy: strategy.id, reason: "no-usable-line" });
        return null;
      }
      // 已经被 wait=false 跳过的那一块：不要再记 ready（否则同一块既有 skipped 又有 ready，
      // 账本自相矛盾），改记 discarded 说明"建议到了但没用上"。
      if (adviceState.discarded) {
        mark("advisor-discarded", {
          index,
          strategy: strategy.id,
          provider: advisorRoute.provider,
          model: advisorRoute.model,
          adviceLines: advice.split("\n\n").length,
          charsOut: advice.length,
          reason: "not-ready",
        });
      } else {
        mark("advisor-ready", {
          index,
          strategy: strategy.id,
          provider: advisorRoute.provider,
          model: advisorRoute.model,
          adviceLines: advice.split("\n\n").length,
          charsOut: advice.length,
        });
      }
      return {
        advice,
        event: { provider: advisorRoute.provider, model: advisorRoute.model, offloaded: advisorRoute.offloaded, lines: advice.split("\n\n").length },
      };
    }).then((result) => {
      adviceState.settled = true;
      adviceState.result = result;
      return result;
    }, (error) => {
      // 指导链任何意外都不能变成未处理的 rejection：它只是增强。
      adviceState.settled = true;
      adviceState.result = null;
      mark("advisor-failed", { index, strategy: strategy.id, message: String(error?.message ?? error) });
      return null;
    });
  };

  /**
   * 压缩成功后才收指导结果：压缩失败时指导结果直接丢弃。
   *
   * `advisor.wait === false` 时**不等待**：压缩一完成就检查指导是否已经就绪；
   * 没就绪就跳过，继续往下走。慢的指导模型不该拖住整个任务流程 ——
   * 这一块少一条建议，比多等十几秒划算。
   */
  const collectAdvice = async (nodes, pending) => {
    if (!pending) return { text: nodes, advice: null, event: null };
    const wait = cfg.advisor?.wait !== false;
    if (!wait && !adviceState.settled) {
      adviceState.discarded = true;
      mark("advisor-skipped", { index, strategy: strategy.id, reason: "not-ready" });
      return { text: nodes, advice: null, event: null };
    }
    const result = await pending;
    if (!result) return { text: nodes, advice: null, event: null };
    return { text: nodes, advice: result.advice, event: result.event };
  };

  const finishFields = (finish, fields) => ({
    finish: finish?.kind,
    truncated: finish?.kind === "max-tokens",
    ...fields,
  });

  /**
   * 一次「调用 + 判定」。返回 `{ done: true, value }` 表示已有结论（成功或不重试的失败），
   * `{ done: false, reason, detail }` 表示"不合格，可以按原因重试一次"。
   */
  const pass = async ({ strict, reason, detail, stage, temperatureForCall }) => {
    let injectedText = String(cfg.customText ?? "");
    const injectRaw = String(cfg.inject ?? "").trim();
    if (injectRaw) {
      const resolved = injectTextOf(injectRaw, { dir: cfg.presetDir ?? null, builtinOverrides: cfg.builtinOverrides ?? {} });
      if (resolved.error) {
        mark("inject-unusable", { index, strategy: strategy.id, value: injectRaw, reason: resolved.error });
      } else if (resolved.text.trim()) {
        // 已有用户补充时按"补充在前、注入在后"拼：注入是用户显式选择的规则，放后面更贴近输出要求。
        injectedText = [injectedText.trim(), resolved.text.trim()].filter(Boolean).join("\n\n");
      }
    }
    const instruction = strategy.buildPrompt(text, {
      strict,
      reason,
      detail,
      limits,
      state,
      // 用户注入是叠加层：追加在内置三档的固定提示词后面，只能补充标签规则。
      // 解析失败不让整块压缩失败 —— 降级为无注入并记账，原文仍按档位改写。
      extraPrompt: injectedText,
      // 内置建议模式：让压缩器自己在输出里加 [旁观者建议]。
      allowAdvice: guidanceOn && guidance.mode === "builtin",
    });
    if (!instruction) {
      mark("thinking-skipped", { index, strategy: strategy.id, reason: "no-prompt", stage });
      return { done: true, value: null };
    }
    let result = await attempt(instruction, temperatureForCall, stage, active);
    // 自测的报文留痕：记录这次实际发出的提示词、原始输出、finish 与 usage，
    // 供设置卡的 Log 窗口比对。正常链路上 trace 为 undefined，零开销。
    if (Array.isArray(trace) && trace.length < 8) {
      trace.push({
        stage,
        provider: active.provider,
        model: active.model,
        prompt: instruction,
        raw: String(result.output ?? ""),
        finish: result.finish?.kind ?? null,
        usage: result.usage ?? null,
        error: result.error ?? null,
        code: result.code ?? null,
      });
    }

    // 卸载路由失败（网络、鉴权、模型不存在、供应商 5xx）时，按配置回退到会话路由
    // 再试一次。只回退一次，且用户按 Esc 中断不重试 —— 中断是用户的决定，不是路由的错。
    if (result.error && canFallback && result.code !== "ABORTED") {
      const from = { provider: active.provider, model: active.model };
      active = { ...sessionRoute, source: "session" };
      offloaded = false;
      mark("rewrite-route-fallback", {
        index,
        strategy: strategy.id,
        stage,
        from,
        to: { provider: active.provider, model: active.model },
        reason: result.error,
        code: result.code,
      });
      result = await attempt(instruction, temperatureForCall, `${stage}:fallback`, active);
    }

    if (result.error) {
      // Esc/超时记 skipped，不算失败。
      if (result.code === "ABORTED") {
        mark("thinking-skipped", { index, strategy: strategy.id, reason: "aborted", stage, chars: text.length });
        return { done: true, value: null };
      }
      mark("thinking-failed", {
        index,
        provider: active.provider,
        model: active.model,
        strategy: strategy.id,
        ms: Date.now() - started,
        message: result.error,
        code: result.code,
        stage,
      });
      return { done: true, value: null };
    }

    // 截断不是"内容不合格"，是输出预算不够。重试/降级都解决不了它 ——
    // 继续打只会让同一块反复吐字、反复被截断，正是用户看到的恶性循环。
    if (result.finish && result.finish.kind === "max-tokens") {
      mark("thinking-truncated", {
        index,
        strategy: strategy.id,
        stage,
        charsIn: text.length,
        charsOut: String(result.output ?? "").trim().length,
        maxTokens,
        retried: strict,
        action: "stop",
      });
      return { done: true, value: null };
    }

    // "输出里不许思考"是跨档位的硬约束：放在 finalize 之前统一拦，
    // 不依赖某个档位的实现。命中就按原因重试一次，第二次仍命中则放弃。
    if (cfg.noThink !== false && hasThinkTag(result.output)) {
      mark("thinking-think-tag", {
        index,
        strategy: strategy.id,
        stage,
        retried: strict,
        charsOut: String(result.output ?? "").trim().length,
      });
      return { done: false, reason: "has-think-tag", detail: "output", finish: result.finish };
    }

    // 标签检测必须在 finalize 之前：formatNodes 会给无标签行补 [中性]，
    // 补完就永远"有标签"，等于没检测。没有标签说明模型大概率只是复述了草稿。
    if (!hasLabel(result.output, strategy.labels ?? null)) {
      mark("thinking-no-label", {
        index,
        strategy: strategy.id,
        stage,
        retried: strict,
        charsOut: String(result.output ?? "").trim().length,
      });
      return { done: false, reason: "no-label", detail: "output", finish: result.finish };
    }

    const verdict = strategy.finalize(result.output, {
      original: text,
      limits,
      state,
      recent,
      // 内置建议模式：让 finalize 知道 [旁观者建议] 不计入节点预算。
      allowAdvice: guidanceOn && guidance.mode === "builtin",
    });
    if (verdict.ok) {
      // 防御：档位没回 text 时（自定义预设早期版本就这样）退回原始输出，不在这里抛。
      const out = typeof verdict.text === "string" ? verdict.text : String(result.output ?? "").trim();
      const advised = await collectAdvice(out, pendingAdvice);
      // 指导与主体分开返回：stream 要先按"主体"做替换判定（必须更短），
      // 通过之后再追加指导 —— 否则指导会把候选撑长，shorter 档位直接拒掉。
      return {
        done: true,
        value: rewritten(advised.text, payload(finishFields(result.finish, {
          charsOut: advised.text.length,
          retried: strict,
          ...strategy.metrics?.(out),
          ...(advised.event ? { advisor: advised.event } : {}),
        })), advised.advice),
      };
    }
    return { done: false, reason: verdict.reason ?? "rejected", detail: verdict.detail ?? null, finish: result.finish };
  };

  // 这段草稿值不值得跑 —— 由策略说。items 说"已经在重复的草稿不值得一次模型调用"，
  // voice 说"空草稿不值得"，off 永远说"不值得"。
  const skip = strategy.skipInput(text);
  if (skip) {
    // `degenerate-input` 是既有账本词汇（设置卡与审计脚本都在认它），
    // 其余跳过原因走 thinking-skipped，别把两种性质混进同一个事件。
    const skipEvent = skip === "degenerate-input" ? "thinking-degenerate" : "thinking-skipped";
    const fallbackText = strategy.fallback?.(text) ?? null;
    if (fallbackText) {
      mark(skipEvent, {
        index,
        strategy: strategy.id,
        stage: "input",
        chars: text.length,
        charsOut: fallbackText.length,
        action: "fallback",
      });
      return rewritten(
        fallbackText,
        payload({
          charsOut: fallbackText.length,
          ...finishFields(null, { finish: skip, truncated: false, retried: false, fallback: true }),
        }),
      );
    }
    mark(skipEvent, { index, strategy: strategy.id, reason: skip, stage: "input", chars: text.length, action: "skip" });
    return null;
  }

  // 指导与压缩并发：先启动指导请求，压缩照常跑；压缩失败时这个 Promise 会被丢弃。
  const pendingAdvice = startAdvice();
  // 压缩首轮就失败时，这一块不会再收建议：标记丢弃，晚到的结果不再记 ready。
  const discardAdvice = () => {
    if (!pendingAdvice) return;
    adviceState.discarded = true;
  };
  const first = await pass({ strict: false, reason: null, detail: null, stage: "first", temperatureForCall: temperature });
  if (first.done) {
    // 首轮就出结论（成功已在 pass 里收过建议；失败则这一块不会再收）。
    if (!first.value) discardAdvice();
    return first.value;
  }

  // 不合格：按策略给的原因重试一次（更低温、带针对性指令）。
  // 事件名由策略决定：items 沿用 thinking-degenerate（历史账本与审计脚本认它），
  // prose 用 thinking-rejected（被拒的原因不止退化）。
  const retryEvent = strategy.retryEvent ?? "thinking-rejected";
  mark(retryEvent, {
    index,
    strategy: strategy.id,
    reason: first.reason,
    detail: first.detail,
    stage: "output",
    action: "retry",
    charsIn: text.length,
  });
  const second = await pass({
    strict: true,
    reason: first.reason,
    detail: first.detail,
    stage: "retry",
    temperatureForCall: Math.max(0.05, temperature / 2),
  });
  if (second.done) {
    // 重试本身没拿到结果（报错/中止）：回原文。改写器没给出结果时拿兜底内容
    // 替换正文，等于把"没改好"降级成"内容被砍成两句"——那比不改糟。
    if (!second.value) discardAdvice();
    return second.value;
  }

  // "输出里在思考"两次都犯：直接保留原文，不要用确定性兜底顶替。
  // 兜底内容虽然不带标签，但它不是模型按预设改写的，拿它替换等于掩盖了失败。
  if (second.reason === "has-think-tag") {
    mark("thinking-think-tag-skip", {
      index,
      strategy: strategy.id,
      stage: "output",
      charsIn: text.length,
      charsOut: 0,
      retried: true,
      action: "skip",
    });
    discardAdvice();
    return null;
  }

  // 两次都不过：items 家族有确定性兜底（宁可给两条真节点，也不把循环写回会话）；
  // prose 家族没有兜底 —— 一段编造的内心比不转写糟得多。
  const fallbackText = strategy.fallback?.(text) ?? null;
  if (fallbackText) {
    mark("thinking-degenerate", {
      index,
      strategy: strategy.id,
      stage: "output",
      charsIn: text.length,
      charsOut: fallbackText.length,
      action: "fallback",
      reason: second.reason,
      detail: second.detail,
      retried: true,
    });
    // 确定性兜底不带建议：它不是模型按预设改写的结果，建议与它没有对应关系。
    discardAdvice();
    return rewritten(
      fallbackText,
      payload({
        charsOut: fallbackText.length,
        ...finishFields(second.finish, { retried: true, fallback: true }),
      }),
    );
  }
  mark(retryEvent, {
    index,
    strategy: strategy.id,
    reason: second.reason,
    detail: second.detail,
    stage: "output",
    action: "skip",
    retried: true,
    charsIn: text.length,
  });
  mark("block-kept", { index, strategy: strategy.id, charsIn: text.length, reason: second.reason ?? "rejected" });
  discardAdvice();
  return null;
}
