// dsh-plugin-iot-thinking — external DSH bundle.
//
// IoT's rule: a thinking block is compressed when it ends and the client/model
// sees the compact block in place. dsh's compaction backend cannot express that:
// it replaces a HISTORY RANGE and appends a checkpoint message, which the model
// reads as a new "context compacted" turn.
//
// This plugin instead hooks the `llm/stream` waterfall:
//   adapter chunks -> hold reasoning deltas -> compress the completed block
//   -> yield ONE compact reasoning delta + a rewritten block-end
// The assistant message that dsh commits already contains the compact thinking,
// so there is no extra message and no checkpoint.

import { importSeam } from "./lib/seams.mjs";
import { ConfigStore, defaultConfigPath } from "./lib/config-store.mjs";
import { compressThinking } from "./lib/compress.mjs";
import { registerConfigApi } from "./lib/config-api.mjs";
import { archiveOriginal, mark } from "./lib/ledger.mjs";
import { transformStream } from "./lib/stream.mjs";

const Llm = await importSeam("@deepseek-ai/dsh-llm");
const { isAgentLoopRequest } = Llm;

export const name = "iot-thinking";
export const inject = ["llm"];

export function apply(ctx, config = {}) {
  const store = new ConfigStore(defaultConfigPath(), config);

  // The Settings self-test has no request to follow, so remember the route of the
  // last real agent-loop request. Writing it costs nothing and only exposes the
  // provider/model names the Models page already shows.
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
    // Auxiliary calls (compaction summary, session title) are hand-built and are
    // not agent-loop requests anyway; this is a second, explicit guard.
    if (options.purpose === "compaction" || options.purpose === "session-title") return false;
    return isAgentLoopRequest(options);
  };

  const compress = (text, options, cfg, index) =>
    compressThinking({
      Llm,
      ctx,
      text,
      options,
      cfg,
      index,
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
    return transformStream(source, options, {
      shouldTransform,
      getConfig: () => store.load(),
      compress,
      onEvent: (event, payload) => mark(event, payload),
    });
  });

  // The Settings card needs an HTTP bridge. Web profiles mount webServer after
  // this row, so the bridge waits for it. Non-web profiles (headless) can set
  // `webApi: false` in the row config to skip the waiting fiber entirely.
  if (config.webApi !== false) registerConfigApi(ctx, store, { compress, mark, getRoute: () => lastRoute });
  mark("constructed", { configPath: store.path });
  ctx.effect(() => () => mark("disposed"));
}
