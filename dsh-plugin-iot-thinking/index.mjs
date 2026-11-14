// external DSH bundle：在 llm/stream 的块结束时改写 thinking，原地替换（无 checkpoint）。
// 改写方式由档位决定（lib/strategies.mjs），哪段会话用哪档由 lib/route.mjs 三层解析。
// 解析必须在钩子内部：llm/stream 是 waterfall，不过 scope filter。

import { registerSessionMemory } from "./lib/session-memory-tools.mjs";
import { createArchiveRuntime } from "./lib/archive-runtime.mjs";
import { importSeam } from "./lib/seams.mjs";
import { ConfigStore, defaultConfigPath } from "./lib/config-store.mjs";
import { compressThinking } from "./lib/compress.mjs";
import { resolveAdvisorRoute } from "./lib/advisor.mjs";
import { registerConfigApi } from "./lib/config-api.mjs";
import { archiveOriginal, mark } from "./lib/ledger.mjs";
import { resolveConfig } from "./lib/route.mjs";
import { switchSessionStrategy } from "./lib/switch.mjs";
import { transformStream } from "./lib/stream.mjs";
import { registerTeacherContext } from "./lib/teacher-prompt.mjs";
import {
  ASK_KINDS,
  TEACHER_LIMITS,
  buildTeacherPrompt,
  normalizeTeacherLimits,
  canConsult,
  clearTeacherState,
  consultTeacher,
  extractSessionContext,
  markConsulted,
  releaseConsult,
  reserveConsult,
  sessionIdOf,
  sessionOf,
  teacherStatus,
} from "./lib/teacher.mjs";

const Llm = await importSeam("@deepseek-ai/dsh-llm");
const { isAgentLoopRequest } = Llm;
const { defineTool } = await importSeam("@deepseek-ai/dsh-tools");

export const name = "iot-thinking";
export const inject = ["llm"];

// 每个 session 最近几段改写结果（内存，重启即失）。
const recentBySession = new Map();
const RECENT_LIMIT = 3;

function recentFor(sessionId) {
  return recentBySession.get(String(sessionId ?? "unknown")) ?? [];
}

function rememberFor(sessionId, text) {
  const key = String(sessionId ?? "unknown");
  const list = recentBySession.get(key) ?? [];
  list.push(text);
  while (list.length > RECENT_LIMIT) list.shift();
  recentBySession.set(key, list);
}

/** 会话 id → agent preset。取不到就是少一层映射，不是失败。 */
export function agentPresetFor(ctx, sessionId) {
  if (sessionId === undefined || sessionId === null) return undefined;
  try {
    const sessions = ctx.get?.("sessions");
    const session = sessions?.get?.(sessionId);
    if (!session) return undefined;
    const projections = ctx.get?.("sessionProjections");
    const preset = projections?.stateOf?.(session, "agentPreset");
    return typeof preset === "string" && preset ? preset : undefined;
  } catch {
    return undefined;
  }
}

export function apply(ctx, config = {}) {
  const store = new ConfigStore(defaultConfigPath(), config);
  const sessionArchive = createArchiveRuntime(ctx, { getConfig: () => store.load(), mark });
  if (config.sessionMemory !== false) registerSessionMemory(ctx, { defineTool, runtime: sessionArchive, getConfig: () => store.load() });

  // Routes are session-scoped: a request in session B must never choose the
  // teacher/rewrite fallback route for session A.
  const lastRoute = { provider: null, model: null };
  const routesBySession = new Map();
  const rememberRoute = (options) => {
    if (!options || typeof options !== "object") return;
    try {
      if (!isAgentLoopRequest(options)) return;
    } catch {
      return;
    }
    const route = {
      provider: typeof options.provider === "string" ? options.provider : null,
      model: typeof options.model === "string" ? options.model : null,
    };
    lastRoute.provider = route.provider;
    lastRoute.model = route.model;
    if (options.sessionId !== undefined && options.sessionId !== null) routesBySession.set(String(options.sessionId), route);
  };

  const shouldTransform = (options) => {
    if (!options || typeof options !== "object") return false;
    // 辅助调用（压缩摘要/会话标题）本来就不是 agent-loop 请求，这是第二道保险。
    if (options.purpose === "compaction" || options.purpose === "session-title") return false;
    return isAgentLoopRequest(options);
  };

  const rewrite = (text, options, cfg, index, extra) =>
    compressThinking({
      Llm,
      ctx,
      text,
      options,
      cfg,
      index,
      extra,
      archive: (info) => archiveOriginal(info),
      mark,
    });

  ctx.on("llm/stream", (options, next) => {
    rememberRoute(options);
    let source;
    try {
      source = next();
    } catch (error) {
      mark("stream-next-failed", { message: String(error?.message ?? error) });
      throw error;
    }

    const sessionId = options?.sessionId;

    // 每次请求都重新解析：改策略、改会话覆盖都是"下一个请求就生效"，
    // 不重启、不重载 profile。解析结果里已含 `limits` 与顶层 minChars/maxChars。
    const getConfig = () => {
      const cfg = store.load();
      if (cfg.enabled === false) return cfg;
      const resolved = resolveConfig(cfg, { sessionId, agentPreset: agentPresetFor(ctx, sessionId) });
      return { ...resolved, teacherActive: cfg.enabled !== false && resolved.teacher?.enabled === true };
    };

    const capture = shouldTransform(options) ? sessionArchive.request(options) : null;
    return transformStream(capture ? capture.observe(source) : source, options, {
      shouldTransform,
      getConfig,
      beforeReplace: capture?.replacement,
      rewrite,
      recent: () => recentFor(sessionId),
      remember: (text) => rememberFor(sessionId, text),
      onEvent: (event, payload) => mark(event, { sessionId, ...payload }),
    });
  });

  // The Settings card needs an HTTP bridge. Web profiles mount webServer after
  // this row, so the bridge waits for it. Non-web profiles (headless) can set
  // `webApi: false` in the row config to skip the waiting fiber entirely.
  if (config.webApi !== false) registerConfigApi(ctx, store, { rewrite, mark, getRoute: () => lastRoute });

  // 会话内切换。命令处理器直接拿到接收它的 agent —— 所以能精确到那一个会话，
  // 写的是 `sessions[<sessionId>]`，与设置卡、头部下拉读的是同一张表。
  // 非交互 profile（headless / qqbot 一类）可以设 `command: false` 省掉这条等待。
  if (config.command !== false) registerCommand(ctx, store);

  // 教师咨询工具：由**正在做这件事的模型**主动调用，插件只提供骨架、上下文与冷却闸门。
  // tools 服务在 web 组合里有；缺失时不报错，插件其余部分照常工作。
  // 共用解析：总闸关闭时教师也关闭；压缩档位 off 不影响独立教师工具。
  const teacherConfigFor = (execution) => {
    const cfg = store.load();
    const sessionId = sessionIdOf(execution);
    const resolved = resolveConfig(cfg, { sessionId, agentPreset: agentPresetFor(ctx, sessionId) });
    return { cfg, sessionId, resolved, teacher: resolved.teacher ?? {}, enabled: cfg.enabled !== false && resolved.teacher?.enabled === true };
  };
  registerTeacherContext(ctx, teacherConfigFor, error =>
    mark("teacher-context-error", { message: String(error?.message ?? error) }));
  // Tool execution and runtime context share the same effective configuration.
  const teacherLimits = (teacherCfg) => normalizeTeacherLimits(teacherCfg);
  const teacherTool = () => {
    return defineTool({
      name: "iot_consult_teacher",
      description: [
        "请一位更强的模型（教师）就当前任务给一次限时咨询。",
        "何时调用由你判断：方案定型前、准备执行不可逆动作前、宣布完成前，或你确实卡住时。",
        "插件会按 depth 从本会话里抽取最近几轮正文作为背景；你只需说明需求与进展。",
        "返回咨询正文，供你判断采纳，不替你执行任务。成功后按思考块冷却；未启用或冷却中会本地拒绝，失败时继续自行判断。",
      ].join(" "),
      parameters: {
        depth: {
          type: "number",
          required: true,
          description: "要几轮本会话历史正文，填写 1-5 的整数；以外援运行时上下文公布的深度范围为准。插件取正文，不需要你复制历史。",
        },
        ask: {
          type: "string",
          required: true,
          enum: [...ASK_KINDS],
          description: "要教师产出什么：advice（可执行建议）/ plan（简短方案）/ framework（分析框架）/ approach（换视角思路）/ review（审阅当前进展）。",
        },
        goal: {
          type: "string",
          required: true,
          description: "当前需求，一句话说清要达成什么。",
        },
        progress: {
          type: "string",
          description: "进展综述：已完成什么、卡在哪、已排除什么。",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            ok: { type: "boolean", required: true },
            text: { type: "string" },
            reason: { type: "string" },
            provider: { type: "string" },
            model: { type: "string" },
            chars: { type: "number" },
            available: { type: "boolean" },
            remainingBlocks: { type: "number" },
          },
        },
        render: (_args, value) => [{ type: "text", text: value?.text ? String(value.text) : String(value?.reason ?? "教师没有返回内容") }],
      },
      async execute(args, exec) {
        exec?.signal?.throwIfAborted();
        const { cfg, sessionId, resolved, teacher, enabled } = teacherConfigFor(exec);
        const limits = teacherLimits(teacher);
        if (!enabled) {
          return { ok: false, reason: "教师咨询未启用（在设置卡里打开「教师增强」）", available: false };
        }
        // 冷却闸门是**硬约束**：不只是提示，冷却中直接拒绝。
        if (!canConsult(sessionId)) {
          const status = teacherStatus(sessionId, limits);
          return { ok: false, reason: status.available ? "教师咨询正在进行" : `教师冷却中（还需 ${status.remainingBlocks} 块）`, available: false, remainingBlocks: status.remainingBlocks };
        }
        const sessionRoute = routesBySession.get(String(sessionId));
        const route = resolveAdvisorRoute(cfg, resolved.strategy, sessionRoute ?? { provider: null, model: null });
        if (!route?.provider || !route?.model) {
          return { ok: false, reason: "没有可用的教师路由：在设置卡里指定建议模型，或先在本会话发一条消息" };
        }
        const reservation = reserveConsult(sessionId);
        if (reservation === null) {
          const status = teacherStatus(sessionId, limits);
          return { ok: false, reason: status.available ? "教师咨询正在进行" : `教师冷却中（还需 ${status.remainingBlocks} 块）`, available: false, remainingBlocks: status.remainingBlocks };
        }
        const depth = Math.max(limits.minDepth, Math.min(limits.maxDepth, Math.floor(Number(args.depth) || limits.minDepth)));
        const session = sessionOf(ctx, sessionId);
        const rounds = extractSessionContext(session, depth);
        // 上下文用**正文**：之前几轮 assistant 已经产出的正文才是任务的真实状态。
        // thinking 是给"产出正文"服务的中间产物，教师不需要看它（也最贵）。
        const lastBody = rounds.length ? rounds[rounds.length - 1].assistant : "";
        const prompt = buildTeacherPrompt({
          goal: args.goal,
          progress: args.progress,
          rounds,
          draft: lastBody,
          ask: args.ask,
          limits,
        });
        let result;
        try {
          result = await consultTeacher({
            ctx,
            Llm,
            route,
            prompt,
            options: { sessionId, signal: exec?.signal },
            timeoutMs: Math.max(3000, Math.min(120000, Number(teacher.timeoutMs) || 30000)),
            maxTokens: limits.maxTokens,
            effort: cfg.advisor?.effort,
            temperature: cfg.advisor?.temperature,
          });
          exec?.signal?.throwIfAborted();
        } catch (error) {
          releaseConsult(sessionId, reservation);
          throw error;
        }
        if (!result.ok) {
          releaseConsult(sessionId, reservation);
          mark("teacher-failed", { sessionId, provider: route.provider, model: route.model, code: result.code, message: result.error, depth, rounds: rounds.length });
          return { ok: false, reason: result.error, provider: route.provider, model: route.model };
        }
        const status = markConsulted(sessionId, limits, reservation);
        mark("teacher-consulted", {
          sessionId,
          provider: route.provider,
          model: route.model,
          ask: args.ask,
          depth,
          rounds: rounds.length,
          chars: result.meta?.chars ?? 0,
          completionTokens: result.meta?.completionTokens,
          reasoningTokens: result.meta?.reasoningTokens,
          remainingBlocks: status.remainingBlocks,
        });
        return {
          ok: true,
          text: result.text,
          provider: route.provider,
          model: route.model,
          chars: result.meta?.chars ?? result.text.length,
          available: status.available,
          remainingBlocks: status.remainingBlocks,
        };
      },
    });
  };

  ctx.on("session/disposed", (session) => {
    const id = String(session?.id ?? "unknown");
    recentBySession.delete(id);
    routesBySession.delete(id);
    clearTeacherState(id);
  });

  ctx.inject(["tools"], (toolCtx) => {
    // 保持工具声明稳定，避免会话开关改变 prompt 前缀；未启用由 execute 本地立即拒绝。
    const unregister = toolCtx.tools.register(teacherTool());
    mark("teacher-tool-registered", { name: "iot_consult_teacher" });
    return () => unregister();
  });

  mark("constructed", { configPath: store.path, strategy: store.load().strategy });
  ctx.effect(() => () => mark("disposed"));
}

/**
 * `/iot` —— 会话内查看与切换策略。
 *
 * 命令不进模型消息（dsh 的命令平面语义），所以它不会污染对话；
 * 结算文本直接显示给用户。真正的逻辑在 lib/switch.mjs，与 HTTP 桥共用同一张表。
 */
function registerCommand(ctx, store) {
  ctx.inject({ commands: null }, (ready) => {
    const commands = ready.get("commands");
    if (!commands || typeof commands.register !== "function") return;
    commands.register({
      name: "iot",
      description: "查看或切换本会话的思维链改写策略（IoT）",
      input: { hint: "<策略 id|auto>" },
      handler: ({ agent, rawInput }) => {
        const sessionId = agent?.session?.id ?? agent?.id;
        if (!sessionId) return { kind: "error", text: "拿不到会话 id，无法切换" };
        return switchSessionStrategy({
          store,
          sessionId,
          preset: agentPresetFor(ctx, sessionId),
          rawInput,
          mark,
        });
      },
    });
  });
}
