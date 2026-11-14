// Host integration: mirror committed events, capture pre-IoT reasoning, never mutate history.
import { randomUUID, createHash } from 'node:crypto';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { SessionArchive } from './session-archive.mjs';

export function defaultSessionArchiveRoot() {
  const data = process.env.IOT_THINKING_DATA_DIR || join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'plugin-data', 'iot-thinking');
  return join(data, 'sessions');
}

export function createArchiveRuntime(ctx, { getConfig, mark = () => {}, archive }) {
  // Bad storage must disable archival/replacement, not prevent the plugin loading.
  const storage = () => archive ??= new SessionArchive({ root: defaultSessionArchiveRoot() });
  const hydrated = new Map();
  const positions = new Map();
  const pending = new Set();
  const timers = new Map();
  let disposed = false;
  const active = () => getConfig().sessionLog !== false;
  const report = (sessionId, error) => mark('session-log-error', { sessionId, message: String(error?.message ?? error) });
  const attempt = (id, fn) => { try { return fn(); } catch (error) { report(id, error); return null; } };
  const flush = id => attempt(id, () => storage().flush(id));
  const schedule = id => {
    if (disposed || timers.has(id)) return;
    const timer = setTimeout(() => { timers.delete(id); flush(id); }, 250);
    timer.unref?.();
    timers.set(id, timer);
  };
  const rememberPosition = (id, event) => {
    const data = event?.data;
    if (Number.isSafeInteger(data?.turn)) {
      const old = positions.get(id);
      if (!old || event.seq >= old.seq) positions.set(id, {
        turn: data.turn,
        step: Number.isSafeInteger(data.step) ? data.step : event.type === 'turn/start' ? null : old?.step ?? null,
        seq: event.seq,
      });
    }
  };
  const record = (id, event) => {
    rememberPosition(id, event);
    const result = attempt(id, () => storage().recordEvent(id, event));
    schedule(id);
    return result;
  };
  // One query at activation/resume, not a full-history copy on every tool result.
  // The public query service is optional. Without it live capture still works;
  // the archive's seq coverage exposes missing historical prefixes.
  const ensure = id => {
    if (!active() || disposed) return Promise.resolve();
    if (hydrated.has(id)) return hydrated.get(id);
    const job = (async () => {
      try {
      const query = ctx.get?.('sessionQuery');
      if (!query?.readSession) {
        mark('session-log-history-unavailable', { sessionId: id, reason: 'sessionQuery unavailable; capturing live events only' });
        return;
      }
        let timer;
        let snapshot;
        try {
          snapshot = await Promise.race([
            query.readSession(id),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('history query exceeded 5s; live capture continues')), 5000); }),
          ]);
        } finally { clearTimeout(timer); }
        if (disposed || !active()) return;
        const events = snapshot?.events;
        if (!Array.isArray(events)) throw new Error('sessionQuery.readSession returned no events');
        if (disposed) return;
        // Historical hydration is already an in-memory snapshot. Persist it under
        // one archive lock/fsync cycle instead of blocking the host event loop with
        // several durable transactions per event.
        for (const event of events) rememberPosition(id, event);
        attempt(id, () => storage().recordEvents(id, events));
        flush(id);
      } catch (error) { report(id, error); }
    })();
    hydrated.set(id, job);
    pending.add(job);
    job.finally(() => pending.delete(job));
    return job;
  };

  ctx.on('session/event', (session, event) => {
    if (!disposed && active()) {
      record(String(session.id), event);
      void ensure(String(session.id));
    }
  });
  ctx.on('session/created', session => { if (!disposed && active()) void ensure(String(session.id)); });
  ctx.on('session/flush', async session => {
    if (!active()) return;
    await ensure(String(session.id));
    flush(String(session.id));
  });
  ctx.on('session/disposed', session => {
    flush(String(session.id));
    positions.delete(String(session.id));
    hydrated.delete(String(session.id));
  });
  ctx.effect(() => () => {
    disposed = true;
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    attempt(null, () => archive?.flush());
  });

  function request(options) {
    const id = options?.sessionId ? String(options.sessionId) : null;
    if (!id || !active()) return null;
    const requestId = randomUUID();
    const saved = new Map();
    let position = positions.get(id) ? { ...positions.get(id) } : {};
    const startSeq = position.seq ?? null;
    const metadata = index => ({ request: requestId, block: index, ...position, startSeq, capturedAt: new Date().toISOString(), provider: options.provider ?? null, model: options.model ?? null });
    const persistOriginal = (index, text, status) => {
      const result = attempt(id, () => storage().recordReasoning(id, {
        id: randomUUID(), ...metadata(index), phase: 'original', original: text, sha256: createHash('sha256').update(text).digest('hex'), bytes: Buffer.byteLength(text), status,
      }));
      if (result) saved.set(index, createHash('sha256').update(text).digest('hex'));
      schedule(id);
      return Boolean(result);
    };
    return {
      async *observe(source) {
        // Preserve the initialization barrier so captured reasoning keeps the exact
        // turn/step association. Batched hydration makes this bounded: one archive
        // transaction instead of several durable transactions per historical event.
        await ensure(id);
        if (position.turn === undefined) position = positions.get(id) ?? attempt(id, () => storage().getPosition?.(id)) ?? {};
        const blocks = new Map();
        try {
          for await (const chunk of source) {
            if (chunk?.type === 'block-start' && chunk.blockType === 'reasoning') blocks.set(chunk.index, '');
            if (chunk?.type === 'reasoning-delta') blocks.set(chunk.index, (blocks.get(chunk.index) ?? '') + String(chunk.text ?? ''));
            if (chunk?.type === 'block-end' && chunk.block?.type === 'reasoning') {
              const text = typeof chunk.block.text === 'string' ? chunk.block.text : blocks.get(chunk.index) ?? '';
              persistOriginal(chunk.index, text, 'complete');
              blocks.delete(chunk.index);
            }
            yield chunk;
          }
        } finally {
          for (const [index, text] of blocks) persistOriginal(index, text, 'interrupted');
          schedule(id);
        }
      },
      replacement({ index, original, processed, advice, strategy }) {
        // A false return is a strict veto: unarchived originals must stay intact.
        if (saved.get(index) !== createHash('sha256').update(original).digest('hex')) return false;
        const result = attempt(id, () => storage().recordReasoning(id, {
          id: randomUUID(), ...metadata(index), phase: 'processed', processed, advice: advice ?? '', strategy,
          status: 'complete', originalChars: original.length,
        }));
        schedule(id);
        return Boolean(result);
      },
    };
  }
  return { get archive() { return storage(); }, request, ensure, flush, async settled() { await Promise.all([...pending]); attempt(null, () => storage().flush()); } };
}
