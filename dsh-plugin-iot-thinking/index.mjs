// external DSH bundle：在 llm/stream 的块结束时改写 thinking，原地替换（无 checkpoint）。
// 改写方式由档位决定（lib/strategies.mjs），哪段会话用哪档由 lib/route.mjs 三层解析。
// 解析必须在钩子内部：llm/stream 是 waterfall，不过 scope filter。

import { importSeam } from "./lib/seams.mjs";
import { ConfigStore, defaultConfigPath } from "./lib/config-store.mjs";
import { compressThinking } from "./lib/compress.mjs";
import { registerConfigApi } from "./lib/config-api.mjs";
import { archiveOriginal, mark } from "./lib/ledger.mjs";
import { resolveConfig } from "./lib/route.mjs";
import { switchSessionStrategy } from "./lib/switch.mjs";
import { transformStream } from "./lib/stream.mjs";

const Llm = await importSeam("@deepseek-ai/dsh-llm");
const { isAgentLoopRequest } = Llm;

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

  // 自测没有请求可跟随，记住最近一次 agent-loop 请求的路由。
  const lastRoute = { provider: null, model: null };
  const rememberRoute = (options) => {
    if (!options || typeof options !== "object") return;
    try {
      if (!isAgentLoopRequest(options)) return;
    } catch {
      return;
    }
    lastRoute.provider = typeof options.provider === "string" ? options.provider : null;
    lastRoute.model = typeof options.model === "string" ? options.model : null;
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
      return resolveConfig(cfg, { sessionId, agentPreset: agentPresetFor(ctx, sessionId) });
    };

    return transformStream(source, options, {
      shouldTransform,
      getConfig,
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
