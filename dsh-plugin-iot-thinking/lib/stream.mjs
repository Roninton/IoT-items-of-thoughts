// 块级转换器：扣住 reasoning 增量，块结束时改写，回一个 delta + 改写的 block-end。
// 因为替换发生在 assistant message 提交之前，所以没有 checkpoint、没有第二条消息。
// 怎么改写由档位决定；这里只管缓冲、长度闸门、以及"能不能替换"。
// 不 import dsh，自测可以用假 source 直接驱动。

import { DEFAULT_STRATEGY, isShorterReplacement, strategyById } from "./strategies.mjs";

export { isShorterReplacement };

/** 兜底：配置里没有 strategy 就用默认档，绝不因为缺字段丢块。 */
function strategyOf(cfg) {
  return strategyById(cfg?.strategy) ?? strategyById(DEFAULT_STRATEGY);
}

/**
 * @param {AsyncIterable<any>} source - chunks from the underlying adapter.
 * @param {object} options - the original GenerateOptions (read-only).
 * @param {object} deps
 * @param {(options:any)=>boolean} deps.shouldTransform
 * @param {()=>object} deps.getConfig
 * @param {(text:string, options:any, cfg:object, index:number, extra:object)=>Promise<{text:string,event:object}|string|null>} deps.rewrite
 * @param {()=>string[]} [deps.recent] - 跨轮记录：最近几段改写结果（口头禅闸用）
 * @param {(text:string)=>void} [deps.remember]
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

  const strategy = strategyOf(cfg);
  const minChars = Number.isFinite(cfg.minChars) ? Math.max(0, cfg.minChars) : 400;
  const maxChars = Number.isFinite(cfg.maxChars) ? Math.max(1, cfg.maxChars) : 24000;
  const recent = () => {
    try {
      return deps.recent?.() ?? [];
    } catch {
      return [];
    }
  };
  const remember = (text) => {
    try {
      deps.remember?.(text);
    } catch {
      /* 记账失败不该影响主流程 */
    }
  };

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
    const chars = original.trim().length;
    if (chars < minChars) {
      emit("block-skipped", { index, chars, reason: "below-min-chars", strategy: strategy.id });
      yield* flush(index);
      yield block;
      return false;
    }
    if (original.length > maxChars) {
      emit("block-skipped", { index, chars, reason: "above-max-chars", maxChars, strategy: strategy.id });
      yield* flush(index);
      yield block;
      return false;
    }

    let rewritten = null;
    try {
      rewritten = await deps.rewrite(original, options, cfg, index, { recent: recent() });
    } catch (error) {
      emit("block-failed", { index, chars, message: String(error?.message ?? error), strategy: strategy.id });
    }

    // `rewrite` returns `{ text, event }`; a plain string is also accepted so the
    // transformer stays testable with a trivial rewriter.
    const candidate = typeof rewritten === "string" ? rewritten : rewritten?.text;
    const candidateEvent = typeof rewritten === "string" ? null : rewritten?.event ?? null;
    // 判定要带上 limits：自定义预设的替换规则（shorter / different）是配置出来的，
    // 不在策略常量里。
    const accepted = strategy.accepts(original, candidate, cfg.limits);

    if (!accepted) {
      const trimmed = typeof candidate === "string" ? candidate.trim() : "";
      if (candidateEvent !== null) {
        emit("thinking-rewritten", {
          ...candidateEvent,
          replaced: false,
          reason: trimmed ? `not-${strategy.accept}` : "empty",
        });
      }
      if (trimmed) {
        emit("block-kept", {
          index,
          charsIn: original.length,
          charsOut: trimmed.length,
          reason: `not-${strategy.accept}`,
          strategy: strategy.id,
        });
      }
      yield* flush(index);
      yield block;
      return false;
    }

    const text = candidate.trim();
    yield { type: "reasoning-delta", index, text };
    yield { ...block, type: "block-end", index, block: { ...block.block, text } };
    buffers.delete(index);
    remember(text);
    if (candidateEvent !== null) emit("thinking-rewritten", { ...candidateEvent, replaced: true });
    emit("block-replaced", {
      index,
      charsIn: original.length,
      charsOut: text.length,
      strategy: strategy.id,
      ...(strategy.metrics ? strategy.metrics(text) : {}),
    });
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
