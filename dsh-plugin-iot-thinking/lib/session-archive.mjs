// Pure Node archive. Host contracts verified against installed dsh-session/src/types.ts
// and dsh-llm/src/{message,assistant-stream,types}.ts (Session format 4).
// JSONL is authoritative; generated Markdown never replaces original records.
import {
  constants, openSync, closeSync, readFileSync, writeSync, fsyncSync, fstatSync,
  lstatSync, mkdirSync, readdirSync, renameSync, unlinkSync,
} from 'node:fs';
import { resolve, join, dirname, parse, sep } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const HASH = /^[a-f0-9]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const digest = value => createHash('sha256').update(value).digest('hex');
function error(code, message) { return Object.assign(new Error(message), { code }); }
function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value) && !Object.is(value, -0)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object' && [Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  }
  throw error('ARCHIVE_INVALID_JSON', 'Records must be lossless JSON (no undefined, nonfinite numbers, or class instances)');
}
function integer(value) { return Number.isSafeInteger(value) && value >= 0 && !Object.is(value, -0); }
function idOf(id) {
  if (typeof id !== 'string' || !id || /[\0/\\]/.test(id) || id === '.' || id === '..') {
    throw error('ARCHIVE_INVALID_SESSION', 'sessionId must be a nonempty identifier without path separators');
  }
  return id;
}
function stat(path) { try { return lstatSync(path); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } }
// Check every ancestor, not just the leaf; never follow symbolic links.
function directory(path, create = false) {
  const absolute = resolve(path), root = parse(absolute).root;
  let current = root;
  for (const part of absolute.slice(root.length).split(sep).filter(Boolean)) {
    current = join(current, part);
    let info = stat(current);
    if (!info && create) {
      try { mkdirSync(current, { mode: 0o700 }); } catch (e) { if (e.code !== 'EEXIST') throw e; }
      info = stat(current);
    }
    if (!info || !info.isDirectory() || info.isSymbolicLink()) throw error('ARCHIVE_UNSAFE_PATH', `Not a real directory: ${current}`);
  }
  return absolute;
}
function regular(path, optional = false) {
  directory(dirname(path));
  const info = stat(path);
  if (!info && optional) return null;
  if (!info || !info.isFile() || info.isSymbolicLink() || info.nlink !== 1) {
    throw error('ARCHIVE_UNSAFE_PATH', `Not a private regular file: ${path}`);
  }
  return info;
}
function openFile(path, flags) {
  regular(path, true);
  const fd = openSync(path, flags | constants.O_NOFOLLOW, 0o600);
  if (!fstatSync(fd).isFile() || fstatSync(fd).nlink !== 1) { closeSync(fd); throw error('ARCHIVE_UNSAFE_PATH', `Unsafe file: ${path}`); }
  return fd;
}
function readFile(path) {
  const fd = openFile(path, constants.O_RDONLY);
  try { return readFileSync(fd, 'utf8'); } finally { closeSync(fd); }
}
function writeAll(fd, text) {
  const bytes = Buffer.from(text); let offset = 0;
  while (offset < bytes.length) { const n = writeSync(fd, bytes, offset, bytes.length - offset); if (!n) throw error('ARCHIVE_WRITE', 'Zero-length write'); offset += n; }
  fsyncSync(fd);
}
function syncDir(dir) {
  // Windows Node cannot fsync directory handles. Refuse that platform explicitly
  // rather than claim crash-durable metadata after a partially successful write.
  if (process.platform === 'win32') throw error('ARCHIVE_UNSUPPORTED_PLATFORM', 'Durable archive requires POSIX directory fsync');
  const fd = openSync(directory(dir), constants.O_RDONLY); try { fsyncSync(fd); } finally { closeSync(fd); }
}
function exclusive(path, text) {
  const fd = openFile(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL);
  try { writeAll(fd, text); } finally { closeSync(fd); }
  syncDir(dirname(path));
}
function atomic(path, text) {
  const dir = directory(dirname(path)); regular(path, true);
  const temp = join(dir, `.render-${randomUUID()}.tmp`);
  exclusive(temp, text);
  // Both resolved source and target are checked in the same verified directory.
  if (dirname(resolve(temp)) !== dir || dirname(resolve(path)) !== dir) throw error('ARCHIVE_UNSAFE_PATH', 'Invalid atomic target');
  regular(temp); regular(path, true); renameSync(temp, path); syncDir(dir);
}
// Markdown is a rebuildable projection of durable events.jsonl. Avoid an fsync +
// directory fsync for every turn: a crash may leave a missing/stale projection,
// which the next flush detects and rebuilds without affecting authoritative data.
function atomicProjection(path, text) {
  const dir = directory(dirname(path)); regular(path, true);
  const temp = join(dir, `.render-${randomUUID()}.tmp`);
  const fd = openFile(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL);
  try {
    const bytes = Buffer.from(text); let offset = 0;
    while (offset < bytes.length) {
      const count = writeSync(fd, bytes, offset, bytes.length - offset);
      if (!count) throw error('ARCHIVE_WRITE', 'Zero-length write');
      offset += count;
    }
  } finally { closeSync(fd); }
  if (dirname(resolve(temp)) !== dir || dirname(resolve(path)) !== dir) throw error('ARCHIVE_UNSAFE_PATH', 'Invalid atomic target');
  regular(temp); regular(path, true); renameSync(temp, path);
}
function fenced(text, language = '') {
  text = String(text);
  let longest = 0;
  for (const match of text.matchAll(/`+/g)) longest = Math.max(longest, match[0].length);
  const fence = '`'.repeat(Math.max(3, longest + 1));
  return `${fence}${language}\n${text}${text.endsWith('\n') ? '' : '\n'}${fence}\n`;
}
const json = value => fenced(JSON.stringify(value, null, 2), 'json');
const turnKey = turn => integer(turn) ? String(turn) : 'unassigned';
function turnFile(sessionId, key) {
  const hash = digest(`${sessionId}\0turn\0${key}`);
  const uuid = `${hash.slice(0, 8)}-${hash.slice(8, 12)}-5${hash.slice(13, 16)}-a${hash.slice(17, 20)}-${hash.slice(20, 32)}`;
  return `turn-${key === 'unassigned' ? key : key.padStart(12, '0')}-${uuid}.md`;
}
const identityOf = (kind, payload) => `${kind}:${kind === 'event' ? payload.seq : payload.id.toLowerCase()}`;
function fingerprint(path) {
  const info = regular(path, true);
  return info ? `${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}:${info.ctimeMs}` : 'absent';
}


function appendView(view, row) {
  const get = key => {
    let turn = view.turnMap.get(key);
    if (!turn) {
      turn = { key, events: [], reasoning: [], status: 'in-progress-or-interrupted' };
      view.turnMap.set(key, turn); view.turns.push(turn);
      view.turns.sort((a, b) => a.key === 'unassigned' ? -1 : b.key === 'unassigned' ? 1 : Number(a.key) - Number(b.key));
    }
    return turn;
  };
  if (row.kind === 'reasoning') {
    view.reasoning.push(row.record); get(turnKey(row.record.turn)).reasoning.push(row.record); return view;
  }
  const event = row.event, data = event.data, explicit = integer(data?.turn) ? data.turn : undefined;
  if (explicit !== undefined) {
    if (view.position.turn !== explicit || event.type === 'turn/start') delete view.position.step;
    view.position.turn = explicit;
  }
  if (integer(data?.step)) view.position.step = data.step;
  if (event.type === 'turn/start') view.activeTurn = explicit;
  const assigned = explicit ?? view.activeTurn, turn = get(turnKey(assigned));
  turn.events.push({ event, turn: assigned, step: data?.step ?? (view.activeTurn !== undefined ? view.position.step : undefined), inferred: explicit === undefined });
  if (event.type === 'turn/end') { turn.status = data.reason?.kind ?? 'ended'; view.activeTurn = undefined; }
  const expected = view.coverage.lastSeq === null ? 0 : view.coverage.lastSeq + 1;
  if (event.seq > expected) view.coverage.gaps.push({ from: expected, to: event.seq - 1 });
  view.coverage.firstSeq ??= event.seq;
  view.coverage.lastSeq = event.seq;
  view.coverage.completePrefix = view.coverage.gaps.length === 0;
  view.events.push(event); return view;
}
function model(rows) {
  const view = { events: [], reasoning: [], turns: [], turnMap: new Map(), position: {}, activeTurn: undefined, coverage: { completePrefix: false, gaps: [], firstSeq: null, lastSeq: null, note: 'Coverage concerns observed seq only; events before capture or after lastSeq may be absent.' } };
  for (const row of rows.filter(r => r.kind === 'event').sort((a, b) => a.event.seq - b.event.seq)) appendView(view, row);
  for (const row of rows.filter(r => r.kind === 'reasoning')) appendView(view, row);
  return view;
}
function turnCoverage(turn, coverage) {
  const lastSeq = turn.events.at(-1)?.event.seq ?? null;
  const gaps = coverage.gaps.filter(g => lastSeq === null || g.from <= lastSeq).map(g => ({ from: g.from, to: lastSeq === null ? g.to : Math.min(g.to, lastSeq) }));
  return { completePrefix: lastSeq !== null && gaps.length === 0 && coverage.firstSeq === 0, gaps, throughSeq: lastSeq, note: 'Prefix coverage through this turn only; see index.md for latest observed session coverage.' };
}
const RENDER_VERSION = 'readable-v2';
function safeText(value) {
  return String(value ?? '').replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/([\\`*_{}\[\]()#!|])/g, '\\$1');
}
export function archiveTurnFilename(sessionId, turn) {
  idOf(sessionId);
  if (turn !== null && !integer(turn)) throw error('ARCHIVE_INVALID_TURN', 'turn must be nonnegative or null');
  return turnFile(sessionId, turnKey(turn));
}
export function archiveSourceLink(sessionId, { turn, seq } = {}) {
  if (seq !== undefined && !integer(seq)) throw error('ARCHIVE_INVALID_SOURCE', 'seq must be nonnegative');
  return archiveTurnFilename(sessionId, turn) + (seq === undefined ? '' : `#seq-${seq}`);
}
const realUser = event => event.type === 'user/message' && ['user', 'user-rpc'].includes(event.data?.source?.kind);
function messageText(message) {
  if (typeof message?.content === 'string') return message.content;
  return (message?.content ?? []).filter(b => ['text', 'input_text', 'output_text'].includes(b?.type) && typeof b.text === 'string').map(b => b.text).join('\n');
}
function iso(value) {
  // Do not interpret zone-less strings as host-local time.
  if (!(typeof value === 'number' && Number.isFinite(value)) && !(typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value))) return null;
  const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}
function readable(turn) {
  const events = turn.events.map(x => x.event);
  const firstUser = events.find(realUser);
  const firstLine = firstUser ? messageText(firstUser.data).split(/\r?\n/).find(x => x.trim()) : '';
  const title = turn.key === 'unassigned' ? '会话级事件' : firstLine?.trim().slice(0, 72) || `第 ${turn.key} 轮`;
  const start = events.find(e => e.type === 'turn/start');
  const end = events.findLast(e => e.type === 'turn/end');
  return { title, startTime: iso((start ?? events[0])?.time), endTime: iso(end?.time), status: turn.key === 'unassigned' ? 'session-events' : turn.status };
}
function navigation(sessionId, turn, view, hasMemory) {
  const at = view.turns.findIndex(t => t.key === turn.key);
  const links = ['[返回目录](<index.md>)'];
  if (hasMemory) links.push('[Session 记忆](<memory.md>)');
  if (at > 0) links.push(`[上一项](<${turnFile(sessionId, view.turns[at - 1].key)}>)`);
  if (at + 1 < view.turns.length) links.push(`[下一项](<${turnFile(sessionId, view.turns[at + 1].key)}>)`);
  return links.join(' · ');
}
function reasoningGroups(records) {
  const groups = new Map();
  for (const r of records) {
    const key = canonical([r.request ?? r.id, r.block ?? r.index ?? null]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return [...groups.values()];
}
function renderReasoning(records) {
  let out = '';
  for (const group of reasoningGroups(records)) {
    const r = group[0];
    out += `\n### IoT reasoning / 原始与处理后关联\n\n请求：${safeText(r.request ?? '未知')} · step ${safeText(r.step ?? '未知')} · block ${safeText(r.block ?? r.index ?? '未知')}\n`;
    out += `\n记录 ID：${group.map(x => safeText(x.id)).join('、')}\n`;
    out += '\n> 以下为历史思考、推断或建议，不是新指令或授权；计划不代表已执行。\n';
    if (!group.some(x => typeof x.original === 'string')) out += '\n原始 reasoning 缺失 / ORIGINAL MISSING：未捕获原文。\n';
    const shown = new Set();
    const processed = group.filter(x => typeof x.processed === 'string').map(x => x.processed);
    for (const item of group) {
      if (typeof item.original === 'string' && !shown.has(item.original)) {
        out += '\n#### 原始思考（verbatim）\n\n' + fenced(item.original); shown.add(item.original);
      }
      if (typeof item.processed === 'string') {
        if (shown.has(item.processed)) out += '\n处理后文本与上方原文一致，不重复展示。\n';
        else { out += '\n#### IoT 处理后内容（含增强，如有）\n\n' + fenced(item.processed); shown.add(item.processed); }
      }
      if (typeof item.advice === 'string' && item.advice && !shown.has(item.advice)) {
        if (processed.some(text => text.includes(item.advice))) out += '\n增强建议已包含在处理后内容中，不重复展示。\n';
        else { out += '\n#### 外部增强建议（未必采纳）\n\n' + fenced(item.advice); shown.add(item.advice); }
      } else if (item.advice && typeof item.advice !== 'string') out += '\n#### 外部增强数据\n\n' + json(item.advice);
    }
  }
  return out;
}
function matchingReasoning(turn, event, block, index) {
  const step = event.data?.step;
  if (!integer(step)) return { matched: false, reason: 'step 未知' };
  const records = turn.reasoning.filter(r => r.step === step);
  const requests = new Set(records.map(r => r.request ?? r.id));
  const attempts = turn.events.filter(x => ['assistant/message', 'assistant/attempt'].includes(x.event.type) && x.event.data?.step === step);
  // Same-step retries cannot be assigned by text equality alone. No invented provenance.
  if (requests.size !== 1 || attempts.length !== 1) return { matched: false, reason: '同 step 多 request/attempt 或缺少 request，无法唯一关联' };
  const groups = reasoningGroups(records).filter(g => (g[0].block ?? g[0].index) === index);
  if (groups.length !== 1) return { matched: false, reason: 'block 无法唯一关联' };
  const group = groups[0];
  const texts = group.filter(r => typeof r.processed === 'string').map(r => r.processed);
  const candidates = texts.length ? texts : group.filter(r => typeof r.original === 'string').map(r => r.original);
  return { matched: candidates.length === 1 && candidates[0] === block.text, reason: '记录与宿主文本不一致或含多个候选' };
}
function renderTurn(sessionId, turn, view, warnings, hasMemory = false) {
  const meta = readable(turn), coverage = turnCoverage(turn, view.coverage);
  let out = `# ${turn.key === 'unassigned' ? '会话级事件' : `第 ${turn.key} 轮：${safeText(meta.title)}`}\n\n`;
  out += `渲染版本：${RENDER_VERSION}\n\n${navigation(sessionId, turn, view, hasMemory)}\n\n`;
  out += `- Session：${safeText(sessionId)}\n- 状态：${safeText(meta.status)}\n- 开始：${meta.startTime ?? '未记录'}（UTC）\n- 结束：${meta.endTime ?? (turn.key === 'unassigned' ? '不适用' : '未记录')}（UTC）\n`;
  out += `- 事件覆盖：${coverage.completePrefix ? '连续前缀' : '不完整 / INCOMPLETE HISTORY'}，截至 seq ${coverage.throughSeq ?? '未知'}\n`;
  if (coverage.gaps.length) out += `- 缺口：${coverage.gaps.map(g => `${g.from}–${g.to}`).join('、')}\n`;
  if (turn.key === 'unassigned') out += '\n这些是未归入轮次的会话级/初始化事件，不代表一轮未完成对话。\n';
  else if (turn.status === 'in-progress-or-interrupted') out += '\n进行中或中断：未观察到本轮结束，不推断完成。\n';
  for (const warning of warnings) out += `\n注意：${safeText(warning)}\n`;
  out += '\n完整原始事件保留在 [events.jsonl](<events.jsonl>)，按 seq 查找；此链接不依赖阅读器支持行号跳转。\n';
  const shownSteps = new Set();
  for (const { event, step } of turn.events) {
    if (integer(step) && !shownSteps.has(step)) {
      shownSteps.add(step);
      out += `\n## 第 ${step} 步\n` + renderReasoning(turn.reasoning.filter(r => r.step === step));
    }
    const data = event.data, type = event.type;
    const message = type === 'user/message' ? data : data?.message;
    const labels = { 'user/message': realUser(event) ? '用户' : '上下文消息（非真人输入）', 'assistant/message': '助手', 'tool/call': '工具调用', 'tool/result': '工具结果', 'assistant/attempt': '未提交的助手尝试' };
    out += `\n<a id="seq-${event.seq}"></a>\n\n### seq ${event.seq} · ${labels[type] ?? safeText(type)}\n\n`;
    if (!Object.hasOwn(labels, type)) {
      out += `类型：${safeText(type)} · [原始事件](<events.jsonl>)（seq ${event.seq}）\n`; continue;
    }
    if (message?.source?.kind) out += `来源：${safeText(message.source.kind)}\n\n`;
    if (data?.interrupted) out += '状态：中断，只保留实际已提交内容。\n\n';
    if (type === 'tool/call') {
      out += `工具：${safeText(data.name)} · 调用 ID：${safeText(data.callId)}\n\n` + fenced(data.arguments ?? '', 'json'); continue;
    }
    if (type === 'tool/result') out += `调用 ID：${safeText(message?.toolCallId ?? '未知')} · ${message?.isError ? '失败' : '状态以结果正文为准'}\n\n`;
    if (typeof message?.content === 'string') out += fenced(message.content);
    else if (Array.isArray(message?.content)) for (let index = 0; index < message.content.length; index++) {
      const block = message.content[index];
      if (block.type === 'tool-call') {
        const calls = turn.events.filter(x => x.event.type === 'tool/call' && x.event.data?.callId === block.id && x.event.data?.arguments === block.arguments && x.event.data?.name === block.name);
        if (calls.length === 1) { out += `工具请求见 [seq ${calls[0].event.seq}](<#seq-${calls[0].event.seq}>)。\n`; continue; }
        out += '\n工具请求（没有唯一对应的已记录调用）：\n\n' + json(block); continue;
      }
      if (block.type === 'reasoning') {
        const match = matchingReasoning(turn, event, block, index);
        if (match.matched) { out += '宿主 reasoning 与上方 IoT 记录一致，不重复展示。\n'; continue; }
        out += `\n宿主 reasoning（可能已由 IoT 改写；${match.reason}）。原思关联未确认 / ORIGINAL MISSING。\n\n`;
      }
      if (typeof block.text === 'string') out += fenced(block.text);
      else out += json(block); // Unknown media stay inert data, never HTML/image embedding.
    }
    if (type === 'assistant/attempt') {
      out += '未提交到对话的尝试；不能据此推断工具已执行。\n';
      const parts = new Map();
      for (const chunk of data.stream ?? []) {
        if (['text-chunks', 'reasoning-chunks'].includes(chunk.type) && Array.isArray(chunk.texts)) {
          const key = `${chunk.type}:${chunk.index}`;
          parts.set(key, (parts.get(key) ?? '') + chunk.texts.join(''));
        }
      }
      for (const [key, text] of parts) out += `\n${key.startsWith('reasoning') ? 'attempt reasoning（无法唯一关联原思）' : 'attempt 正文'}\n\n` + fenced(text);
      if (!parts.size) out += '没有可直接展示的文本块，详见原始事件。\n';
    }
  }
  out += renderReasoning(turn.reasoning.filter(r => !shownSteps.has(r.step)));
  return out + `\n---\n\n${navigation(sessionId, turn, view, hasMemory)}\n`;
}
function renderIndex(sessionId, view, warnings, hasMemory) {
  let out = `# Session 对话目录\n\n渲染版本：${RENDER_VERSION}\n\nSession：${safeText(sessionId)}\n\n`;
  if (hasMemory) out += '[当前状态与关键记忆](<memory.md>) · ';
  out += '[完整事件 JSONL](<events.jsonl>)\n\n';
  out += `覆盖：${view.coverage.completePrefix ? '连续前缀' : '不完整'}，最后观察 seq ${view.coverage.lastSeq ?? '未知'}。\n\n`;
  for (const warning of warnings) out += `注意：${safeText(warning)}\n\n`;
  out += '| 轮次 | 主题 | 开始（UTC） | 结束（UTC） | 状态 |\n|---|---|---|---|---|\n';
  for (const turn of view.turns) {
    const meta = readable(turn);
    out += `| [${turn.key === 'unassigned' ? '会话级事件' : turn.key}](<${turnFile(sessionId, turn.key)}>) | ${safeText(meta.title)} | ${meta.startTime ?? '未记录'} | ${meta.endTime ?? '—'} | ${safeText(meta.status)} |\n`;
  }
  return out;
}

/**
 * Synchronous, append-only archive; root MUST be supplied (no profile defaults).
 * recordEvent(id,event) -> {stored,duplicate,path}; original host event preserved.
 * recordReasoning(id,record) -> same; UUID id immutable, request+block groups phases.
 * flush(id?) -> {sessions,files}; derived Markdown written only when changed.
 * inspect(id), list({offset,limit}), getPosition(id), read(id,{format,turn,offset,limit}),
 * search(id,query,{offset,limit}) are local deterministic reads; no host/model calls.
 * Read formats: markdown (default, string), events/reasoning (arrays), jsonl (string).
 * Operation-level exclusive lock refuses concurrent processes. A crash-held lock
 * fails closed: verify its owner is dead before manually removing .archive.lock.
 * A torn trailing JSONL line remains untouched, readable-prefix warnings are
 * returned, and subsequent append refuses until an operator repairs it.
 */
export const archiveFiles = Object.freeze({ exists: path => Boolean(regular(path, true)), read: readFile, atomic });

export class SessionArchive {
  constructor({ root, onError } = {}) {
    if (typeof root !== 'string' || !root || root.split(/[\\/]/).includes('..')) throw error('ARCHIVE_UNSAFE_PATH', 'An explicit root without parent traversal is required');
    if (process.platform === 'win32') throw error('ARCHIVE_UNSUPPORTED_PLATFORM', 'Durable archive requires POSIX directory fsync');
    this.root = directory(root, true);
    this.onError = onError;
    this._cache = new Map();
  }
  _run(fn) { try { return fn(); } catch (e) { try { this.onError?.(e); } catch {} throw e; } }
  _dir(sessionId, create = false) {
    idOf(sessionId); directory(this.root);
    const dir = join(this.root, digest(sessionId));
    if (!stat(dir) && !create) throw error('ARCHIVE_NOT_FOUND', 'Session archive not found');
    directory(dir, create); return dir;
  }
  _locked(sessionId, create, fn) {
    const dir = this._dir(sessionId, create), lock = join(dir, '.archive.lock');
    let fd;
    try { fd = openFile(lock, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL); }
    catch (e) { if (e.code === 'EEXIST') throw error('ARCHIVE_LOCKED', `Archive busy or crash-held lock; verify owner before removal: ${lock}`); throw e; }
    const lockIdentity = fstatSync(fd);
    try {
      writeAll(fd, JSON.stringify({ pid: process.pid, token: randomUUID(), at: new Date().toISOString() }));
      const meta = join(dir, 'session.json');
      if (!stat(meta)) exclusive(meta, JSON.stringify({ version: 1, sessionId }) + '\n');
      if (JSON.parse(readFile(meta)).sessionId !== sessionId) throw error('ARCHIVE_ID_COLLISION', 'Session hash collision / metadata mismatch');
      return fn(dir);
    } finally {
      closeSync(fd);
      // Do not unlink a replacement lock or follow a substituted directory.
      directory(dir); const currentLock = regular(lock);
      if (currentLock.dev !== lockIdentity.dev || currentLock.ino !== lockIdentity.ino) throw error('ARCHIVE_LOCK_REPLACED', 'Archive lock changed owner; refusing to remove replacement');
      if (dirname(resolve(lock)) !== dir) throw error('ARCHIVE_UNSAFE_PATH', 'Invalid lock target');
      unlinkSync(lock);
    }
  }
  _load(dir, sessionId) {
    const path = join(dir, 'events.jsonl'), stamp = fingerprint(path);
    const cached = this._cache.get(dir);
    if (cached?.stamp === stamp) return cached;
    if (!stat(path)) {
      const empty = { rows: [], warnings: [], torn: false, identities: new Map(), stamp, dirty: null, view: null, rendered: new Map(), lastSeq: -1, activeTurn: undefined };
      this._cache.set(dir, empty); return empty;
    }
    const text = readFile(path), rows = [], warnings = [], seen = new Map();
    const lines = text.split('\n'); const torn = text.length > 0 && !text.endsWith('\n');
    for (let index = 0; index < lines.length; index++) {
      const line = lines[index]; if (!line) continue;
      let row;
      try { row = JSON.parse(line); } catch {
        if (torn && index === lines.length - 1) { warnings.push('Torn final JSONL record preserved; only complete durable prefix rendered.'); break; }
        throw error('ARCHIVE_CORRUPT', `Invalid JSONL at line ${index + 1}`);
      }
      if (row.version !== 1 || row.sessionId !== sessionId || !['event', 'reasoning'].includes(row.kind)) throw error('ARCHIVE_CORRUPT', `Invalid archive envelope at line ${index + 1}`);
      const payload = row.kind === 'event' ? row.event : row.record;
      if (!payload || (row.kind === 'event' ? !integer(payload.seq) : !UUID.test(payload.id))) throw error('ARCHIVE_CORRUPT', `Invalid archive identity at line ${index + 1}`);
      const key = `${row.kind}:${row.kind === 'event' ? payload.seq : payload.id.toLowerCase()}`, value = canonical(payload);
      if (seen.has(key) && seen.get(key) !== value) throw error('ARCHIVE_CONFLICT', `Conflicting durable identity ${key}`);
      if (!seen.has(key)) rows.push(row);
      seen.set(key, value);
    }
    if (torn && !warnings.length) warnings.push('Final JSONL record lacks newline; append refused until repaired.');
    const view = model(rows);
    const last = view.events.at(-1);
    let activeTurn;
    for (const e of view.events) { if (e.type === 'turn/start') activeTurn = e.data.turn; if (e.type === 'turn/end') activeTurn = undefined; }
    const loaded = { rows, warnings, torn, identities: seen, stamp, dirty: null, view, rendered: cached?.rendered ?? new Map(), lastSeq: last?.seq ?? -1, activeTurn };
    this._cache.set(dir, loaded); return loaded;
  }
  _record(sessionId, kind, payload) {
    return this._run(() => {
      const encoded = canonical(payload); // Validate before creating session paths.
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw error('ARCHIVE_INVALID_RECORD', 'Record must be an object');
      if (kind === 'event') {
        if (!integer(payload.seq) || typeof payload.type !== 'string' || !payload.type || !Object.hasOwn(payload, 'data')) throw error('ARCHIVE_INVALID_EVENT', 'Event needs nonnegative seq, type and data');
      } else {
        if (typeof payload.id !== 'string' || !UUID.test(payload.id)) throw error('ARCHIVE_INVALID_UUID', 'record.id must be a UUID');
        for (const key of ['turn', 'step', 'block', 'index']) if (Object.hasOwn(payload, key) && payload[key] !== null && !integer(payload[key])) throw error('ARCHIVE_INVALID_RECORD', `${key} must be a nonnegative integer or null`);
        for (const key of ['original', 'processed']) if (Object.hasOwn(payload, key) && payload[key] !== null && typeof payload[key] !== 'string') throw error('ARCHIVE_INVALID_RECORD', `${key} must be text or null`);
      }
      return this._locked(sessionId, true, dir => {
        const loaded = this._load(dir, sessionId), path = join(dir, 'events.jsonl');
        if (loaded.torn) throw error('ARCHIVE_TORN_TAIL', 'Existing incomplete JSONL tail retained; refusing append rather than overwrite original bytes');
        const key = identityOf(kind, payload);
        const prior = loaded.identities.get(key);
        if (prior !== undefined) {
          if (prior !== encoded) throw error('ARCHIVE_CONFLICT', `Conflicting ${key}`);
          return { stored: false, duplicate: true, path };
        }
        const row = { version: 1, sessionId, kind, [kind === 'event' ? 'event' : 'record']: payload };
        const fd = openFile(path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND);
        try { writeAll(fd, JSON.stringify(row) + '\n'); } finally { closeSync(fd); }
        syncDir(dir);
        // Own successful append advances the cache; other writers invalidate via fingerprint.
        if (kind === 'event') {
          if (payload.seq !== loaded.lastSeq + 1) loaded.dirty = null;
          if (payload.type === 'turn/start') loaded.activeTurn = payload.data.turn;
          loaded.dirty?.add(turnKey(payload.data?.turn ?? loaded.activeTurn));
          if (payload.type === 'turn/end') loaded.activeTurn = undefined;
          loaded.lastSeq = Math.max(loaded.lastSeq, payload.seq);
        } else loaded.dirty?.add(turnKey(payload.turn));
        loaded.rows.push(JSON.parse(JSON.stringify(row)));
        loaded.identities.set(key, encoded);
        loaded.stamp = fingerprint(path);
        loaded.view = null;
        return { stored: true, duplicate: false, path };
      });
    });
  }
  recordEvent(sessionId, event) { return this._record(sessionId, 'event', event); }
  recordEvents(sessionId, events) {
    if (!Array.isArray(events)) throw error('ARCHIVE_INVALID_EVENT', 'events must be an array');
    if (!events.length) return { stored: 0, duplicates: 0, path: null };
    return this._run(() => {
      const prepared = events.map(payload => {
        const encoded = canonical(payload);
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)
          || !integer(payload.seq) || typeof payload.type !== 'string' || !payload.type
          || !Object.hasOwn(payload, 'data')) {
          throw error('ARCHIVE_INVALID_EVENT', 'Event needs nonnegative seq, type and data');
        }
        return { payload, encoded, key: identityOf('event', payload) };
      });
      return this._locked(sessionId, true, dir => {
        const loaded = this._load(dir, sessionId), path = join(dir, 'events.jsonl');
        if (loaded.torn) throw error('ARCHIVE_TORN_TAIL', 'Existing incomplete JSONL tail retained; refusing append rather than overwrite original bytes');
        const pending = new Map();
        let duplicates = 0;
        for (const item of prepared) {
          const prior = pending.has(item.key) ? pending.get(item.key) : loaded.identities.get(item.key);
          if (prior !== undefined) {
            if (prior !== item.encoded) throw error('ARCHIVE_CONFLICT', 'Conflicting ' + item.key);
            duplicates++;
          } else pending.set(item.key, item.encoded);
        }
        const freshByKey = new Map();
        for (const item of prepared) if (loaded.identities.get(item.key) === undefined && !freshByKey.has(item.key)) freshByKey.set(item.key, item);
        const fresh = [...freshByKey.values()];
        if (!fresh.length) return { stored: 0, duplicates, path };
        const rows = fresh.map(({ payload }) => ({ version: 1, sessionId, kind: 'event', event: payload }));
        const fd = openFile(path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND);
        try { writeAll(fd, rows.map(row => JSON.stringify(row) + '\n').join('')); } finally { closeSync(fd); }
        syncDir(dir);
        for (let index = 0; index < fresh.length; index++) {
          const { payload, encoded, key } = fresh[index], row = rows[index];
          if (payload.seq !== loaded.lastSeq + 1) loaded.dirty = null;
          if (payload.type === 'turn/start') loaded.activeTurn = payload.data.turn;
          loaded.dirty?.add(turnKey(payload.data?.turn ?? loaded.activeTurn));
          if (payload.type === 'turn/end') loaded.activeTurn = undefined;
          loaded.lastSeq = Math.max(loaded.lastSeq, payload.seq);
          loaded.rows.push(JSON.parse(JSON.stringify(row)));
          loaded.identities.set(key, encoded);
        }
        loaded.stamp = fingerprint(path);
        loaded.view = null;
        return { stored: fresh.length, duplicates, path };
      });
    });
  }
  recordReasoning(sessionId, record) { return this._record(sessionId, 'reasoning', record); }
  _snapshot(sessionId, fn) { return this._run(() => this._locked(sessionId, false, dir => { const loaded = this._load(dir, sessionId); return fn(dir, loaded, loaded.view ??= model(loaded.rows)); })); }
  _description(sessionId, dir, loaded, view) {
    return { sessionId, directory: dir, eventCount: view.events.length, reasoningCount: view.reasoning.length, position: view.position, coverage: view.coverage, warnings: loaded.warnings, turns: view.turns.map(t => ({ turn: t.key === 'unassigned' ? null : Number(t.key), ...readable(t), file: join(dir, turnFile(sessionId, t.key)) })), files: { events: join(dir, 'events.jsonl'), index: join(dir, 'index.md') } };
  }
  inspect(sessionId) { return this._snapshot(sessionId, (dir, loaded, view) => this._description(sessionId, dir, loaded, view)); }
  getPosition(sessionId) { try { return this.inspect(sessionId).position; } catch (e) { if (e.code === 'ARCHIVE_NOT_FOUND') return {}; throw e; } }
  list({ offset = 0, limit = 100 } = {}) {
    return this._run(() => {
      this._page(offset, limit); directory(this.root);
      const ids = readdirSync(this.root).filter(name => HASH.test(name)).sort().map(name => {
        const dir = directory(join(this.root, name)); const meta = JSON.parse(readFile(join(dir, 'session.json')));
        if (digest(idOf(meta.sessionId)) !== name) throw error('ARCHIVE_ID_COLLISION', 'Session directory does not match metadata');
        return meta.sessionId;
      });
      return ids.slice(offset, offset + limit).map(id => this.inspect(id));
    });
  }
  _page(offset, limit) { if (!integer(offset) || !integer(limit) || limit > 10000) throw error('ARCHIVE_INVALID_PAGE', 'offset/limit must be nonnegative integers; limit <= 10000'); }
  read(sessionId, { format = 'markdown', turn, offset = 0, limit = 1000 } = {}) {
    this._page(offset, limit);
    return this._snapshot(sessionId, (dir, loaded, view) => {
      if (turn !== undefined && turn !== null && !integer(turn)) throw error('ARCHIVE_INVALID_TURN', 'turn must be a nonnegative integer or null');
      const turns = view.turns.filter(t => turn === undefined || t.key === turnKey(turn));
      if (format === 'events') return turns.flatMap(t => t.events.map(e => e.event)).sort((a, b) => a.seq - b.seq).slice(offset, offset + limit);
      if (format === 'reasoning') return turns.flatMap(t => t.reasoning).slice(offset, offset + limit);
      if (format === 'jsonl') return loaded.rows.filter(r => turn === undefined || (r.kind === 'reasoning' ? turnKey(r.record.turn) === turnKey(turn) : turns.some(t => t.events.some(e => e.event.seq === r.event.seq)))).slice(offset, offset + limit).map(r => JSON.stringify(r)).join('\n');
      if (format !== 'markdown') throw error('ARCHIVE_INVALID_FORMAT', 'format must be markdown/events/reasoning/jsonl');
      return turns.slice(offset, offset + limit).map(t => renderTurn(sessionId, t, view, loaded.warnings, Boolean(regular(join(dir, 'memory.md'), true)))).join('\n');
    });
  }
  search(sessionId, query, { offset = 0, limit = 100 } = {}) {
    this._page(offset, limit);
    if (typeof query !== 'string' || !query) throw error('ARCHIVE_INVALID_QUERY', 'query must be nonempty text');
    return this._snapshot(sessionId, (_dir, loaded, view) => {
      const turnsBySeq = new Map(view.turns.flatMap(t => t.events.map(e => [e.event.seq, t.key === 'unassigned' ? null : Number(t.key)])));
      return loaded.rows.flatMap(row => {
      const payload = row.kind === 'event' ? row.event : row.record;
      // Literal substring, not a user-supplied regular expression. Traverse raw
      // string values so actual newlines and Unicode remain searchable.
      const matches = [];
      const visit = (value, path) => {
        if (typeof value === 'string') { const at = value.toLowerCase().indexOf(query.toLowerCase()); if (at !== -1) matches.push({ turn: row.kind === 'reasoning' ? row.record.turn ?? null : turnsBySeq.get(row.event.seq) ?? null, kind: row.kind, seq: row.event?.seq ?? null, id: row.record?.id ?? null, field: path, excerpt: value.slice(Math.max(0, at - 80), at + query.length + 160) }); }
        else if (value && typeof value === 'object') for (const [key, item] of Object.entries(value)) visit(item, path ? `${path}.${key}` : key);
      };
      visit(payload, ''); return matches;
    }).slice(offset, offset + limit);
    });
  }
  flush(sessionId) {
    const ids = sessionId === undefined ? this.list({ limit: 10000 }).map(s => s.sessionId) : [sessionId];
    const files = [];
    for (const id of ids) this._snapshot(id, (dir, loaded, view) => {
      const outputs = [];
      const hasMemory = Boolean(regular(join(dir, 'memory.md'), true));
      for (const t of view.turns) {
        const name = turnFile(id, t.key);
        // Rendering signature includes version and navigation dependencies, but
        // never the session-global lastSeq in an already completed turn.
        const signature = digest(canonical({ version: RENDER_VERSION, events: t.events.map(e => e.event), reasoning: t.reasoning, status: readable(t).status, coverage: turnCoverage(t, view.coverage), warnings: loaded.warnings, navigation: navigation(id, t, view, hasMemory) }));
        const path = join(dir, name);
        const existing = regular(path, true);
        if (loaded.rendered.get(name) === signature && existing) continue;
        outputs.push([name, renderTurn(id, t, view, loaded.warnings, hasMemory), signature]);
      }
      const index = renderIndex(id, view, loaded.warnings, hasMemory);
      outputs.push(['index.md', index, digest(index)]);
      let wroteProjection = false;
      for (const [name, text, signature] of outputs) {
        const path = join(dir, name);
        if (!stat(path) || readFile(path) !== text) { atomicProjection(path, text); files.push(path); wroteProjection = true; }
        loaded.rendered.set(name, signature);
      }
      if (wroteProjection) syncDir(dir);
      loaded.dirty = new Set();
    });
    return { sessions: ids.length, files };
  }
}
