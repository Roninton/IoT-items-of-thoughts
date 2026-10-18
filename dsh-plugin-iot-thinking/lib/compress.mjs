// lib/compress.mjs — auxiliary model call(s) that turn a reasoning draft into
// the IoT item chain, with a degeneration gate, one strict retry, and a local
// deterministic fallback.
//
// This call is hand-built, so it is NOT marked by dsh-agent-loop. The llm/stream
// waterfall checks isAgentLoopRequest(options) and therefore lets this call pass
// through untouched: no recursion, no compressing the compressor.

import {
  buildCompressionPrompt,
  fallbackItems,
  formatNodes,
  isDegenerateDraft,
  isDegenerateItems,
  sanitizeDraft,
} from "./think.mjs";

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
 * @returns {Promise<string|null>} item chain, or null to keep the original
 */
export async function compressThinking({ Llm, ctx, text, options, cfg, index, archive, mark }) {
  const started = Date.now();
  const provider = cfg.provider || options.provider;
  const model = cfg.model || options.model;

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

  const run = async (instruction, temperature) => {
    const assembler = new Llm.BlockAssembler();
    try {
      const stream = ctx.llm.stream({
        provider,
        model,
        // The compressor must not spend the output budget on its own thinking.
        // DeepSeek maps "off" to thinking:{type:"disabled"}.
        reasoningEffort: "off",
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
      return { error: String(error?.message ?? error), finish: assembler.finish };
    }
    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      return { error: finish.failure?.message ?? "unknown", finish };
    }
    const output = assembler
      .blocks()
      .filter((block) => block && block.type === "text" && typeof block.text === "string" && block.text.trim())
      .map((block) => block.text)
      .join("\n");
    return { output, finish };
  };

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
      mark("thinking-compressed", {
        index,
        provider,
        model,
        charsIn: text.length,
        charsOut: items.length,
        ms: Date.now() - started,
        archived,
        finish: "degenerate-input",
        truncated: false,
        retried: false,
        fallback: true,
      });
      return items;
    }
    mark("thinking-degenerate", { index, stage: "input", chars: text.length, action: "skip" });
    return null;
  }

  const first = await run(buildCompressionPrompt(text), 0.2);
  if (first.error) {
    mark("thinking-failed", {
      index,
      provider,
      model,
      ms: Date.now() - started,
      message: first.error,
      stage: "first",
    });
    return null;
  }

  const firstRawDegenerate = isDegenerateItems(first.output);
  let items = formatNodes(first.output, { maxNodes: cfg.maxNodes, maxNeutral: cfg.maxNeutral });
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
    const second = await run(buildCompressionPrompt(text, { strict: true }), 0.1);
    let secondRawDegenerate = false;
    if (second.error) {
      mark("thinking-failed", {
        index,
        provider,
        model,
        ms: Date.now() - started,
        message: second.error,
        stage: "retry",
      });
      items = "";
    } else {
      secondRawDegenerate = isDegenerateItems(second.output);
      items = formatNodes(second.output, { maxNodes: cfg.maxNodes, maxNeutral: cfg.maxNeutral });
      lastFinish = second.finish;
    }

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
        mark("thinking-compressed", {
          index,
          provider,
          model,
          charsIn: text.length,
          charsOut: itemsFallback.length,
          ms: Date.now() - started,
          archived,
          finish: lastFinish?.kind,
          truncated: false,
          retried,
          fallback: true,
        });
        return itemsFallback;
      }
      mark("thinking-degenerate", { index, stage: "output", charsIn: text.length, action: "skip", retried });
      return null;
    }
  }

  if (!items) {
    mark("thinking-failed", { index, provider, model, ms: Date.now() - started, message: "empty-output" });
    return null;
  }

  mark("thinking-compressed", {
    index,
    provider,
    model,
    charsIn: text.length,
    charsOut: items.length,
    ms: Date.now() - started,
    archived,
    finish: lastFinish?.kind,
    truncated: lastFinish?.kind === "max-tokens",
    retried,
  });
  return items;
}
