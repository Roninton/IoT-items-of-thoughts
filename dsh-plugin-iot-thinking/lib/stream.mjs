// lib/stream.mjs — the llm/stream transformer.
//
// This is the whole trick: dsh assembles the assistant message from the chunks
// this waterfall yields. If we hold the reasoning deltas until the block ends,
// compress the draft, and then yield ONE compressed reasoning delta plus a
// rewritten block-end, the committed assistant message already contains the
// compact thinking block. There is no second message, no checkpoint, and no
// "context compacted" event for the model to react to.
//
// The transformer is dependency-injected and has no dsh imports, so selftest can
// drive it with a fake chunk source and a fake compressor.

/**
 * The one rule that decides whether a compressed block may replace the original:
 * the candidate must be a non-empty string and strictly shorter once trimmed.
 * Exported because the Settings self-test must judge its result by exactly this
 * rule — a test that only looks for a non-empty reply reports green on a
 * compression the real link would never substitute.
 *
 * @param {unknown} original - the buffered original draft.
 * @param {unknown} candidate - what the compressor returned.
 * @returns {boolean} whether the candidate may replace the block.
 */
export function isShorterReplacement(original, candidate) {
  if (typeof candidate !== "string") return false;
  const next = candidate.trim();
  return next.length > 0 && next.length < String(original ?? "").trim().length;
}

/**
 * @param {AsyncIterable<any>} source - chunks from the underlying adapter.
 * @param {object} options - the original GenerateOptions (read-only).
 * @param {object} deps
 * @param {(options:any)=>boolean} deps.shouldTransform
 * @param {()=>object} deps.getConfig
 * @param {(text:string, options:any, cfg:object, index:number)=>Promise<{text:string,event:object}|string|null>} deps.compress
 * @param {(event:string, payload?:object)=>void} [deps.onEvent]
 */
export async function* transformStream(source, options, deps) {
  const emit = deps.onEvent ?? (() => {});
  let cfg;
  try {
    cfg = deps.getConfig();
  } catch (error) {
    emit("config-error", { message: String(error?.message ?? error) });
    yield* source;
    return;
  }

  if (!cfg?.enabled || !deps.shouldTransform(options)) {
    yield* source;
    return;
  }

  const minChars = Number.isFinite(cfg.minChars) ? Math.max(0, cfg.minChars) : 400;
  /** @type {Map<number, { text: string, chunks: any[] }>} */
  const buffers = new Map();

  const flush = function* (index) {
    const buffer = buffers.get(index);
    if (!buffer) return;
    for (const chunk of buffer.chunks) yield chunk;
    buffers.delete(index);
  };

  const replace = async function* (index, block, buffered) {
    const bufferedText = buffered?.text ?? "";
    const original = bufferedText.trim()
      ? bufferedText
      : String(block?.block?.text ?? block?.text ?? "");
    if (!original.trim()) {
      yield* flush(index);
      yield block;
      return false;
    }
    if (original.trim().length < minChars) {
      emit("block-skipped", { index, chars: original.length, reason: "below-min-chars" });
      yield* flush(index);
      yield block;
      return false;
    }

    let compressed = null;
    try {
      compressed = await deps.compress(original, options, cfg, index);
    } catch (error) {
      emit("block-failed", { index, chars: original.length, message: String(error?.message ?? error) });
    }

    // `compress` returns `{ text, event }`; a plain string is also accepted so the
    // transformer stays testable with a trivial compressor.
    const candidate = typeof compressed === "string" ? compressed : compressed?.text;
    const candidateEvent = typeof compressed === "string" ? null : compressed?.event ?? null;
    if (!isShorterReplacement(original, candidate)) {
      const shortened = typeof candidate === "string" && candidate.trim();
      if (candidateEvent !== null) emit("thinking-compressed", { ...candidateEvent, replaced: false, reason: shortened ? "not-shorter" : "empty" });
      if (shortened) {
        emit("block-kept", { index, charsIn: original.length, charsOut: candidate.trim().length, reason: "not-shorter" });
      }
      yield* flush(index);
      yield block;
      return false;
    }

    const text = candidate.trim();
    yield { type: "reasoning-delta", index, text };
    yield { ...block, type: "block-end", index, block: { ...block.block, text } };
    buffers.delete(index);
    if (candidateEvent !== null) emit("thinking-compressed", { ...candidateEvent, replaced: true });
    emit("block-replaced", { index, charsIn: original.length, charsOut: text.length });
    return true;
  };

  for await (const chunk of source) {
    if (chunk?.type === "block-start" && chunk.blockType === "reasoning") {
      buffers.set(chunk.index, { text: "", chunks: [] });
      yield chunk;
      continue;
    }

    if (chunk?.type === "reasoning-delta") {
      const buffer = buffers.get(chunk.index) ?? { text: "", chunks: [] };
      buffer.text += String(chunk.text ?? "");
      buffer.chunks.push(chunk);
      buffers.set(chunk.index, buffer);
      continue;
    }

    if (chunk?.type === "block-end" && chunk.block?.type === "reasoning") {
      const buffered = buffers.get(chunk.index);
      // A block-end without deltas still carries its assembled text.
      if (buffered || String(chunk.block.text ?? "").trim()) {
        yield* replace(chunk.index, chunk, buffered);
        continue;
      }
    }

    // The adapter should close a reasoning block before anything else. If it
    // does not (abort/error path), flush the originals so no thinking is lost.
    if (chunk?.type === "finish" || chunk?.type === "usage") {
      for (const index of [...buffers.keys()]) yield* flush(index);
    }

    yield chunk;
  }

  // Stream ended without block-end: preserve every buffered original.
  for (const index of [...buffers.keys()]) yield* flush(index);
}
