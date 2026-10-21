// lib/compress.mjs — auxiliary model call(s) that turn a reasoning draft into
// the IoT item chain, with a degeneration gate, one strict retry, and a local
// deterministic fallback.
//
// This call is hand-built, so it is NOT marked by dsh-agent-loop. The llm/stream
// waterfall checks isAgentLoopRequest(options) and therefore lets this call pass
// through untouched: no recursion, no compressing the compressor.

import {
  AUTO_EFFORT,
  isUnsupportedEffortFailure,
  knownEffortPlan,
  normalizeEffort,
  pickLowEffort,
  planKey,
  rememberEffortPlan,
} from "./effort.mjs";
import {
  buildCompressionPrompt,
  fallbackItems,
  formatNodes,
  isDegenerateDraft,
  isDegenerateItems,
  sanitizeDraft,
} from "./think.mjs";

/**
 * A compressed item chain plus the ledger payload for it.
 *
 * The payload is not written here: only `lib/stream.mjs` knows whether the block
 * is actually replaced or kept, and `thinking-compressed` must carry that answer
 * (`replaced: true|false`) so a compression ratio read from the ledger is not
 * inflated by blocks that were ultimately thrown away (issue #1, problem 3).
 *
 * @param {string} text - the item chain that stream.mjs may substitute.
 * @param {object} payload - the ledger fields for this attempt.
 * @returns {{ text: string, event: object }}
 */
function compressed(text, payload) {
  return { text, event: payload };
}

/**
 * A route's catalog lookup is advisory, so it must never be able to stall a
 * response: the reasoning block already ended and the reader is waiting. Tests
 * lower this to keep the timeout path fast.
 */
export const EFFORT_PROBE = { timeoutMs: 3000 };

/** Reject after `timeoutMs` without leaving the timer holding the process open. */
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

/**
 * @param {object} args
 * @param {object} args.Llm - namespace from @deepseek-ai/dsh-llm
 * @param {object} args.ctx - plugin context (ctx.llm)
 * @param {string} args.text - original reasoning draft
 * @param {object} args.options - the outer GenerateOptions
 * @param {object} args.cfg - live config
 * @param {number} args.index - reasoning block index
 * @param {(info:object)=>string|null} args.archive - original-draft archiver
 * @param {(event:string,payload?:object)=>void} args.mark - ledger writer
 * @returns {Promise<{ text: string, event: object }|null>} item chain plus its ledger payload, or null to keep the original
 */
export async function compressThinking({ Llm, ctx, text, options, cfg, index, archive, mark }) {
  const started = Date.now();
  const provider = cfg.provider || options.provider;
  const model = cfg.model || options.model;
  const profile = cfg.profile ?? "full";

  if (!provider || !model) {
    mark("thinking-skipped", { index, reason: "no-route" });
    return null;
  }

  const maxChars = Number.isFinite(cfg.maxChars) ? Math.max(1, cfg.maxChars) : 24000;
  if (text.length > maxChars) {
    mark("thinking-skipped", { index, reason: "above-max-chars", chars: text.length, maxChars });
    return null;
  }

  const archived = cfg.archive === false ? null : archive({ sessionId: options.sessionId, index, text, provider, model });
  const maxTokens = Number.isFinite(cfg.maxTokens) ? Math.max(1, cfg.maxTokens) : 2000;

  // Which reasoning level to ask for. "auto" is the only value that works on
  // every route without configuration: it starts from `off` and learns from the
  // route's own refusal (issue #3).
  const mode = normalizeEffort(cfg.reasoningEffort) ?? AUTO_EFFORT;
  const key = planKey(provider, model, mode);
  /** Effort id carried by the last attempt; null = the field was omitted. */
  let effortSent = null;
  let effortFallback = false;
  let called = false;

  const run = async (instruction, temperature, effort) => {
    const assembler = new Llm.BlockAssembler();
    effortSent = effort;
    called = true;
    try {
      const stream = ctx.llm.stream({
        provider,
        model,
        // Omitting the field hands the level choice to the adapter's own default,
        // which is what a route that refuses `off` needs. `off` stays the fast
        // path: it stops the compressor from spending the output budget on its
        // own thinking (DeepSeek maps it to thinking:{type:"disabled"}).
        ...(effort === null ? {} : { reasoningEffort: effort }),
        // Low temperature makes the flash compressor much less likely to fall
        // into a one-word repetition loop.
        temperature,
        messages: [
          Llm.createUserMessage({
            content: [{ type: "text", text: instruction }],
            source: { kind: "plugin", plugin: "iot-thinking" },
          }),
        ],
        maxTokens,
        sessionId: options.sessionId,
        // Auxiliary call: a valid GenerateOptions purpose. This request is not an
        // agent-loop request, so our own waterfall ignores it.
        purpose: "compaction",
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      });
      for await (const chunk of stream) assembler.push(chunk);
    } catch (error) {
      return {
        error: String(error?.message ?? error),
        code: typeof error?.code === "string" ? error.code : undefined,
        finish: assembler.finish,
      };
    }
    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      return { error: finish.failure?.message ?? "unknown", code: finish.failure?.code, finish };
    }
    // An aborted call is NOT success. dsh normalizes it to `finish.kind:"aborted"`
    // with no output; treating that as an empty (hence degenerate) answer would
    // replace a perfectly good thinking block with the deterministic stub just
    // because the user pressed Esc or the turn timed out.
    if (finish && finish.kind === "aborted") {
      return {
        error: finish.failure?.message ?? "compressor call aborted",
        code: finish.failure?.code ?? "ABORTED",
        finish,
      };
    }
    const output = assembler
      .blocks()
      .filter((block) => block && block.type === "text" && typeof block.text === "string" && block.text.trim())
      .map((block) => block.text)
      .join("\n");
    return { output, finish };
  };

  /**
   * Ask the exact route which effort levels it advertises, so the compressor can
   * keep thinking off (or nearly off) on routes that simply do not accept `off`.
   * Purely advisory: an unavailable or slow catalog never fails the block, and a
   * failed probe is reported as *unknown* rather than as "this route has no low
   * level" — caching the latter would cost a native DeepSeek route its `off` fast
   * path for the rest of the process after one catalog hiccup.
   *
   * @returns {Promise<{ answered: boolean, effort: string|null }>}
   */
  const probeLowEffort = async () => {
    const llm = (typeof ctx.get === "function" ? ctx.get("llm") : undefined) ?? ctx?.llm;
    if (typeof llm?.resolveModelInfo !== "function") return { answered: false, effort: null };
    try {
      const info = await withTimeout(llm.resolveModelInfo(provider, model, options.signal), EFFORT_PROBE.timeoutMs, "resolveModelInfo");
      return { answered: true, effort: pickLowEffort((info?.reasoning?.efforts ?? []).map((effort) => effort?.id)) };
    } catch (error) {
      mark("thinking-effort-probe-failed", {
        index,
        provider,
        model,
        message: String(error?.message ?? error),
      });
      return { answered: false, effort: null };
    }
  };

  /**
   * One compressor attempt, including the effort-level recovery. The recovery is
   * not a retry of a failed compression: the request never reached the provider,
   * so it costs one local round-trip, and an answered route decision is cached.
   */
  const attempt = async (instruction, temperature, stage) => {
    const known = knownEffortPlan(key);
    const send = known !== undefined ? known : mode === AUTO_EFFORT ? "off" : mode;
    let result = await run(instruction, temperature, send);
    if (!result.error || !isUnsupportedEffortFailure(result)) return result;

    // `null` means "send no effort at all". If even that is refused, the adapter's
    // own default level is missing from its advertised list: nothing this plugin
    // can send will work, so name that cause instead of retrying.
    if (send === null) {
      mark("thinking-effort-unsupported", {
        index,
        provider,
        model,
        stage,
        requested: null,
        reason: "adapter-default-unsupported",
        message: result.error,
      });
      return result;
    }

    mark("thinking-effort-fallback", {
      index,
      provider,
      model,
      stage,
      requested: send,
      reason: "unsupported",
      message: result.error,
    });
    // Only auto mode goes looking for a cheap supported level. An explicit
    // choice that the route refuses is a configuration signal, not a hint to
    // pick some other level: send nothing and let the provider decide.
    const probe = mode === AUTO_EFFORT && send === "off" ? await probeLowEffort() : { answered: true, effort: null };
    let chosen = probe.effort;
    result = await run(instruction, temperature, chosen);
    effortFallback = true;
    // An adapter can advertise a level the host then refuses anyway (stale or
    // optimistic catalog). Omitting the field is the remaining option, so try it
    // once instead of leaving the route pinned on a level that fails every block.
    if (probe.answered && chosen !== null && isUnsupportedEffortFailure(result)) {
      chosen = null;
      result = await run(instruction, temperature, chosen);
    }
    // "No field at all" refused is the adapter's own default being unadvertised —
    // nothing this plugin can send will work. Name it, and do not cache it: the
    // adapter can be fixed, and a cache entry would pin the route for the process.
    if (chosen === null && isUnsupportedEffortFailure(result)) {
      mark("thinking-effort-unsupported", {
        index,
        provider,
        model,
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

  const payload = (extra) => ({
    index,
    provider,
    model,
    profile,
    // `effort: null` is meaningful only after a call went out (the field was
    // omitted); a degenerate input that never called the model has no effort.
    ...(called ? { effort: effortSent } : {}),
    ...(effortFallback ? { effortFallback: true } : {}),
    charsIn: text.length,
    ms: Date.now() - started,
    archived,
    ...extra,
  });

  const fallback = () => {
    const items = fallbackItems(text);
    return items && items.length < text.length ? items : null;
  };

  // A draft that is already a word loop is not worth a model call: sanitize
  // cannot recover it, and sending it back to the compressor reproduces the loop.
  if (isDegenerateDraft(text) || !sanitizeDraft(text).trim()) {
    const items = fallback();
    if (items) {
      mark("thinking-degenerate", { index, stage: "input", chars: text.length, charsOut: items.length, action: "fallback" });
      return compressed(
        items,
        payload({ charsOut: items.length, finish: "degenerate-input", truncated: false, retried: false, fallback: true }),
      );
    }
    mark("thinking-degenerate", { index, stage: "input", chars: text.length, action: "skip" });
    return null;
  }

  const first = await attempt(buildCompressionPrompt(text, { profile }), 0.2, "first");
  if (first.error) {
    // 用户按 Esc / 请求超时不是插件的失败：记 skipped 而不是 failed，免得设置卡
    // 因为一次主动取消就弹"压缩一直失败"。
    if (first.code === "ABORTED") {
      mark("thinking-skipped", { index, reason: "aborted", stage: "first", chars: text.length });
      return null;
    }
    mark("thinking-failed", {
      index,
      provider,
      model,
      ms: Date.now() - started,
      message: first.error,
      code: first.code,
      stage: "first",
    });
    return null;
  }

  const firstRawDegenerate = isDegenerateItems(first.output);
  let items = formatNodes(first.output, { profile, maxNodes: cfg.maxNodes, maxNeutral: cfg.maxNeutral });
  let lastFinish = first.finish;
  let retried = false;

  if (firstRawDegenerate || !items || isDegenerateItems(items)) {
    mark("thinking-degenerate", {
      index,
      stage: "output",
      charsIn: text.length,
      charsOut: items.length,
      action: "retry",
      finish: first.finish?.kind,
    });
    retried = true;
    const second = await attempt(buildCompressionPrompt(text, { profile, strict: true }), 0.1, "retry");
    if (second.error) {
      if (second.code === "ABORTED") {
        mark("thinking-skipped", { index, reason: "aborted", stage: "retry", chars: text.length });
        return null;
      }
      mark("thinking-failed", {
        index,
        provider,
        model,
        ms: Date.now() - started,
        message: second.error,
        code: second.code,
        stage: "retry",
      });
      // 失败就回原文：压缩器没给出结果（报错/中止）时拿兜底节点替换正文，
      // 等于把"没压成"降级成"内容被砍成两句"——那比不压糟。
      return null;
    }
    const secondRawDegenerate = isDegenerateItems(second.output);
    items = formatNodes(second.output, { profile, maxNodes: cfg.maxNodes, maxNeutral: cfg.maxNeutral });
    lastFinish = second.finish;

    if (secondRawDegenerate || !items || isDegenerateItems(items)) {
      const itemsFallback = fallback();
      if (itemsFallback) {
        mark("thinking-degenerate", {
          index,
          stage: "output",
          charsIn: text.length,
          charsOut: itemsFallback.length,
          action: "fallback",
          retried,
        });
        return compressed(
          itemsFallback,
          payload({
            charsOut: itemsFallback.length,
            finish: lastFinish?.kind,
            truncated: lastFinish?.kind === "max-tokens",
            retried,
            fallback: true,
          }),
        );
      }
      mark("thinking-degenerate", { index, stage: "output", charsIn: text.length, action: "skip", retried });
      return null;
    }
  }

  if (!items) {
    mark("thinking-failed", { index, provider, model, ms: Date.now() - started, message: "empty-output" });
    return null;
  }

  return compressed(
    items,
    payload({
      charsOut: items.length,
      finish: lastFinish?.kind,
      truncated: lastFinish?.kind === "max-tokens",
      retried,
    }),
  );
}
