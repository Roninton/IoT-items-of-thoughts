import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync, readFileSync, appendFileSync, writeFileSync, mkdirSync, symlinkSync, statSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { SessionArchive, archiveTurnFilename, archiveSourceLink } from './lib/session-archive.mjs';

function fixture(t) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'iot-session-archive-')));
  t.after(() => { assert.ok(resolve(root).startsWith(realpathSync(tmpdir()) + '/iot-session-archive-')); rmSync(root, { recursive: true, force: true }); });
  return { root, archive: new SessionArchive({ root }) };
}
const event = (seq, type, data) => ({ seq, type, time: 1700000000000 + seq, data });
const message = (role, text, extra = []) => ({ id: randomUUID(), role, source: role === 'assistant' ? { kind: 'model', provider: 'test', model: 'model' } : role === 'tool' ? { kind: 'tool', callId: 'call-1' } : { kind: 'user' }, content: [{ type: 'text', text }, ...extra] });
function append(archive, sid, seq, type, data) { const e = event(seq, type, data); if (type.endsWith('/message') || type === 'tool/result') e.surfaceOp = 'append'; archive.recordEvent(sid, e); return e; }

test('real host shapes: multistep turn preserves all messages, attachments and tools', t => {
  const { archive } = fixture(t), sid = 'session';
  append(archive, sid, 0, 'turn/start', { turn: 0 });
  const u = message('user', '完整用户正文\nsecond line', [{ type: 'file', attachment: { id: 'f', name: 'file.js', path: '/not-read/file.js' } }]);
  append(archive, sid, 1, 'user/message', u);
  append(archive, sid, 2, 'step/start', { turn: 0, step: 0 });
  append(archive, sid, 3, 'assistant/message', { turn: 0, step: 0, message: message('assistant', 'assistant first'), stream: [] });
  append(archive, sid, 4, 'tool/call', { turn: 0, step: 0, callId: 'call-1', name: 'read', arguments: '{"path":"file.js"}' });
  append(archive, sid, 5, 'tool/result', { turn: 0, step: 0, message: { ...message('tool', '全部tool输出'), toolCallId: 'call-1' }, meta: { file: '/path' } });
  append(archive, sid, 6, 'step/end', { turn: 0, step: 0 });
  append(archive, sid, 7, 'step/start', { turn: 0, step: 1 });
  append(archive, sid, 8, 'assistant/message', { turn: 0, step: 1, message: message('assistant', 'assistant final'), stream: [] });
  append(archive, sid, 9, 'turn/end', { turn: 0, reason: { kind: 'completed' } });
  const md = archive.read(sid);
  for (const text of ['完整用户正文\nsecond line', 'assistant first', 'assistant final', '全部tool输出', 'file.js', '第 1 步', '状态：completed']) assert.ok(md.includes(text), text);
  assert.deepEqual(archive.read(sid, { format: 'events' })[1].data, u);
  assert.deepEqual(archive.getPosition(sid), { turn: 0, step: 1 });
  assert.equal(archive.inspect(sid).coverage.completePrefix, true);
  assert.equal(archive.flush(sid).files.length, 2);
  assert.equal(archive.flush(sid).files.length, 0);
});

test('restart, seq deduplication, out-of-order replay, and conflicts are lossless', t => {
  const { archive, root } = fixture(t), sid = 'restarted';
  const e2 = append(archive, sid, 2, 'step/start', { turn: 1, step: 0 });
  assert.equal(archive.inspect(sid).coverage.completePrefix, false);
  const second = new SessionArchive({ root });
  assert.equal(second.recordEvent(sid, e2).duplicate, true);
  append(second, sid, 0, 'turn/start', { turn: 1 });
  append(second, sid, 1, 'user/message', message('user', 'replayed'));
  assert.deepEqual(second.read(sid, { format: 'events' }).map(e => e.seq), [0, 1, 2]);
  assert.equal(second.inspect(sid).coverage.completePrefix, true);
  assert.throws(() => second.recordEvent(sid, { ...e2, data: { turn: 2, step: 0 } }), { code: 'ARCHIVE_CONFLICT' });
  assert.equal(second.inspect(sid).eventCount, 3);
  const raw = readFileSync(second.inspect(sid).files.events, 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(raw.map(r => r.event.seq), [2, 0, 1]);
});

test('batch event backfill is lossless, idempotent, and conflict-safe', t => {
  const { archive } = fixture(t), sid = 'batch';
  const events = [
    event(0, 'turn/start', { turn: 1 }),
    event(1, 'user/message', message('user', 'batch replay')),
    event(2, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
  ];
  assert.deepEqual(archive.recordEvents(sid, events), {
    stored: 3, duplicates: 0, path: archive.inspect(sid).files.events,
  });
  assert.deepEqual(archive.read(sid, { format: 'events' }).map(item => item.seq), [0, 1, 2]);
  assert.deepEqual(archive.recordEvents(sid, [...events, events[2]]), {
    stored: 0, duplicates: 4, path: archive.inspect(sid).files.events,
  });
  assert.throws(() => archive.recordEvents(sid, [{ ...events[1], data: message('user', 'conflict') }]), { code: 'ARCHIVE_CONFLICT' });
  assert.equal(archive.inspect(sid).eventCount, 3);
});

test('UUID original and processed phases associate by request/block, never overwrite', t => {
  const { archive } = fixture(t), sid = 'rewrite', request = randomUUID();
  const original = { id: randomUUID(), request, turn: 2, step: 3, block: 0, phase: 'original', original: 'raw long reasoning\n```\nraw', status: 'complete' };
  const processed = { id: randomUUID(), request, turn: 2, step: 3, block: 0, phase: 'processed', processed: 'IoT tags', advice: '旁观者建议', strategy: 'iot-full' };
  archive.recordReasoning(sid, original); archive.recordReasoning(sid, processed);
  assert.equal(archive.recordReasoning(sid, original).duplicate, true);
  assert.throws(() => archive.recordReasoning(sid, { ...original, original: 'changed' }), { code: 'ARCHIVE_CONFLICT' });
  const md = archive.read(sid);
  for (const text of ['raw long reasoning\n```\nraw', 'IoT tags', '旁观者建议', original.id, processed.id, request]) assert.ok(md.includes(text));
  assert.equal((md.match(/### IoT reasoning/g) ?? []).length, 1);
  assert.deepEqual(archive.read(sid, { format: 'reasoning' }), [original, processed]);
});

test('orphan and interrupted records plus assistant attempts survive without turn/end', t => {
  const { archive } = fixture(t), sid = 'interrupted';
  append(archive, sid, 4, 'user/message', message('user', 'orphan prompt'));
  append(archive, sid, 5, 'assistant/attempt', { turn: 8, step: 1, stream: [{ type: 'text-chunks', time0: 1, index: 1, dt: [1], texts: ['attempt ', 'body'] }, { type: 'reasoning-chunks', time0: 3, index: 0, dt: [], texts: ['thinking prefix'] }] });
  archive.recordReasoning(sid, { id: randomUUID(), request: randomUUID(), turn: 8, step: 1, block: 0, phase: 'original', original: 'partial', status: 'interrupted' });
  const md = archive.read(sid);
  for (const text of ['会话级事件', 'orphan prompt', 'attempt body', 'thinking prefix', 'in-progress-or-interrupted', 'INCOMPLETE HISTORY', 'partial']) assert.ok(md.includes(text), text);
  assert.equal(archive.flush(sid).files.length, 3);
});

test('missing original is explicit, and model reasoning is not labeled original', t => {
  const { archive } = fixture(t), sid = 'missing';
  archive.recordReasoning(sid, { id: randomUUID(), request: randomUUID(), turn: 0, step: 0, block: 1, phase: 'processed', processed: 'only rewritten' });
  append(archive, sid, 0, 'assistant/message', { turn: 0, step: 0, message: message('assistant', 'body', [{ type: 'reasoning', text: 'already rewritten' }]), stream: [], interrupted: true });
  const md = archive.read(sid);
  assert.match(md, /ORIGINAL MISSING/); assert.match(md, /可能已由 IoT 改写/); assert.match(md, /body/);
});

test('safe hashes, collision resistance for long identifiers, traversal rejection and private modes', t => {
  const { archive, root } = fixture(t);
  const first = 'a'.repeat(200) + '1', second = 'a'.repeat(200) + '2';
  for (const sid of [first, second]) append(archive, sid, 0, 'turn/start', { turn: 0 });
  assert.notEqual(archive.inspect(first).directory, archive.inspect(second).directory);
  assert.equal(archive.inspect(first).directory, join(root, createHash('sha256').update(first).digest('hex')));
  for (const sid of ['../outside', '/absolute', 'a/b', 'a\\b', '..', '\0']) assert.throws(() => archive.recordEvent(sid, event(0, 'turn/start', { turn: 0 })), { code: 'ARCHIVE_INVALID_SESSION' });
  archive.flush(first);
  const dir = archive.inspect(first).directory;
  assert.equal(statSync(dir).mode & 0o777, 0o700);
  for (const name of readdirSync(dir)) assert.equal(statSync(join(dir, name)).mode & 0o777, 0o600);
  assert.throws(() => new SessionArchive({ root: join(root, 'x', '..', 'y') + '/../z' }), { code: 'ARCHIVE_UNSAFE_PATH' });
});

test('reject symbolic links at root, session, journal and Markdown', t => {
  const { archive, root } = fixture(t), outside = join(root, 'outside'); mkdirSync(outside);
  const link = join(root, 'linked'); symlinkSync(outside, link);
  assert.throws(() => new SessionArchive({ root: join(link, 'nested') }), { code: 'ARCHIVE_UNSAFE_PATH' });
  const sid = 'symlink'; const dir = join(root, createHash('sha256').update(sid).digest('hex')); symlinkSync(outside, dir);
  assert.throws(() => append(archive, sid, 0, 'turn/start', { turn: 0 }), { code: 'ARCHIVE_UNSAFE_PATH' });
  append(archive, 'journal', 0, 'turn/start', { turn: 0 });
  const journalDir = archive.inspect('journal').directory;
  symlinkSync(join(outside, 'target'), archive.inspect('journal').turns[0].file);
  assert.throws(() => archive.flush('journal'), { code: 'ARCHIVE_UNSAFE_PATH' });
  const sid2 = 'unsafe-journal', dir2 = join(root, createHash('sha256').update(sid2).digest('hex')); mkdirSync(dir2);
  symlinkSync(join(outside, 'target'), join(dir2, 'events.jsonl'));
  assert.throws(() => append(archive, sid2, 0, 'turn/start', { turn: 0 }), { code: 'ARCHIVE_UNSAFE_PATH' });
  assert.deepEqual(readdirSync(outside), []);
});

test('dynamic fences keep XSS and embedded Markdown inert, full text retained', t => {
  const { archive } = fixture(t), sid = 'markdown';
  const text = '<script>alert(1)</script>\n``````\n# injected heading\n[attack](javascript:alert(1))\n```';
  append(archive, sid, 0, 'user/message', message('user', text));
  const md = archive.read(sid);
  assert.ok(md.includes('```````\n' + text + '\n```````'));
  assert.ok(!md.includes('<details>'));
  assert.deepEqual(archive.read(sid, { format: 'events' })[0].data.content[0].text, text);
});

test('torn tail preserves durable prefix, does not overwrite bytes, callers see error', t => {
  const { archive, root } = fixture(t), sid = 'torn';
  append(archive, sid, 0, 'user/message', message('user', 'durable user'));
  const path = archive.inspect(sid).files.events;
  appendFileSync(path, '{"broken":');
  const bytes = readFileSync(path), errors = [];
  const restarted = new SessionArchive({ root, onError: e => errors.push(e.code) });
  assert.ok(restarted.read(sid).includes('durable user'));
  assert.equal(restarted.inspect(sid).warnings.length, 1);
  restarted.flush(sid);
  assert.throws(() => append(restarted, sid, 1, 'turn/start', { turn: 1 }), { code: 'ARCHIVE_TORN_TAIL' });
  assert.deepEqual(readFileSync(path), bytes);
  assert.ok(errors.includes('ARCHIVE_TORN_TAIL'));
});

test('cross-process lock refuses concurrent writer and leaves existing data intact', t => {
  const { archive, root } = fixture(t), sid = 'locked';
  append(archive, sid, 0, 'turn/start', { turn: 0 });
  const { directory: dir, files } = archive.inspect(sid), before = readFileSync(files.events);
  const lock = join(dir, '.archive.lock'); writeFileSync(lock, JSON.stringify({ pid: process.pid }), { mode: 0o600 });
  const code = `import {SessionArchive} from ${JSON.stringify(new URL('./lib/session-archive.mjs', import.meta.url).href)};try{new SessionArchive({root:process.argv[1]}).recordEvent('locked',{seq:1,type:'turn/start',data:{turn:1}});process.exit(2)}catch(e){console.log(e.code);process.exit(e.code==='ARCHIVE_LOCKED'?0:3)}`;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', code, root], { encoding: 'utf8', env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' } });
  assert.equal(result.status, 0, result.stderr); assert.match(result.stdout, /ARCHIVE_LOCKED/);
  assert.deepEqual(readFileSync(files.events), before);
});

test('list/read/search bounded APIs and record validation', t => {
  const { archive } = fixture(t);
  for (const sid of ['alpha', 'beta']) append(archive, sid, 0, 'user/message', message('user', 'needle\n正文'));
  assert.equal(archive.list({ limit: 1 }).length, 1);
  assert.equal(archive.read('alpha', { format: 'events', limit: 0 }).length, 0);
  assert.equal(archive.search('alpha', 'needle\n正文').length, 1);
  assert.equal(archive.search('alpha', 'needle', { offset: 1 }).length, 0);
  assert.throws(() => archive.search('alpha', 'x', { limit: 10001 }), { code: 'ARCHIVE_INVALID_PAGE' });
  assert.throws(() => archive.recordReasoning('alpha', { id: 'not-uuid', original: 'text' }), { code: 'ARCHIVE_INVALID_UUID' });
  assert.throws(() => archive.recordEvent('alpha', { seq: 2, type: 'unknown', data: { invalid: undefined } }), { code: 'ARCHIVE_INVALID_JSON' });
  assert.equal(archive.inspect('alpha').eventCount, 1);
});


const occurrences = (text, part) => text.split(part).length - 1;
test('readable view renders content once; exact event metadata remains in JSONL', t => {
  const { archive } = fixture(t), sid = 'readable';
  append(archive, sid, 0, 'turn/start', { turn: 1 });
  append(archive, sid, 1, 'user/message', message('user', '主题\nunique-user-body'));
  append(archive, sid, 2, 'step/start', { turn: 1, step: 1 });
  const tool = { type: 'tool-call', id: 'call-unique', name: 'read', arguments: '{"path":"unique-path"}' };
  append(archive, sid, 3, 'assistant/message', { turn: 1, step: 1, message: message('assistant', 'unique-assistant-body', [tool]), stream: [{ type: 'text-chunks', texts: ['unique-assistant-body'] }], usage: { outputTokens: 17 } });
  append(archive, sid, 4, 'tool/call', { turn: 1, step: 1, callId: tool.id, name: tool.name, arguments: tool.arguments });
  append(archive, sid, 5, 'tool/result', { turn: 1, step: 1, message: { ...message('tool', 'unique-tool-result'), toolCallId: tool.id }, meta: { privateRendering: 'retained-only-in-jsonl' } });
  append(archive, sid, 6, 'turn/end', { turn: 1, reason: { kind: 'completed' } });
  const md = archive.read(sid, { turn: 1 });
  for (const text of ['unique-user-body','unique-assistant-body','unique-path','unique-tool-result']) assert.equal(occurrences(md,text),1,text);
  assert.doesNotMatch(md, /Exact host event|outputTokens|privateRendering/);
  assert.match(md, /工具请求见 \[seq 4\]\(<#seq-4>\)/);
  assert.match(md, /<a id="seq-5"><\/a>/);
  const rows = archive.read(sid, { format: 'events' });
  assert.equal(rows[3].data.usage.outputTokens,17);
  assert.equal(rows[5].data.meta.privateRendering,'retained-only-in-jsonl');
  archive.flush(sid);
  assert.equal(readFileSync(archive.inspect(sid).turns[0].file,'utf8'),md);
});

test('titles, UTC times, source navigation and memory presence are deterministic', t => {
  const { archive, root } = fixture(t), sid = 'navigation';
  append(archive,sid,0,'session/header',{note:'initialization'});
  append(archive,sid,1,'turn/start',{turn:1});
  append(archive,sid,2,'user/message',{...message('user','not the title'),source:{kind:'spine-checkpoint'}});
  append(archive,sid,3,'user/message',message('user','<img src=x> [恶意](javascript:x) | topic\n正文'));
  append(archive,sid,4,'turn/end',{turn:1,reason:{kind:'completed'}});
  append(archive,sid,5,'turn/start',{turn:2});
  append(archive,sid,6,'user/message',message('user','后续主题'));
  append(archive,sid,7,'turn/end',{turn:2,reason:{kind:'completed'}});
  archive.flush(sid);
  const info=archive.inspect(sid), dir=info.directory;
  assert.equal(info.turns[0].status,'session-events');
  assert.equal(info.turns[1].title,'<img src=x> [恶意](javascript:x) | topic');
  assert.equal(info.turns[1].startTime,new Date(1700000000001).toISOString());
  let first=archive.read(sid,{turn:1});
  assert.match(first,/开始：2023-11-14T22:13:20.001Z（UTC）/);
  assert.ok(first.includes('下一项](<'+archiveTurnFilename(sid,2)+'>)'));
  assert.ok(!first.includes('memory.md'));
  const header=first.split('\n')[0];
  assert.ok(header.includes('&lt;img src=x&gt;'));assert.ok(!header.includes('<img'));
  assert.ok(header.includes('\\|'));assert.ok(header.includes('\\[恶意\\]'));
  assert.equal(archiveSourceLink(sid,{turn:1,seq:3}),archiveTurnFilename(sid,1)+'#seq-3');
  assert.throws(()=>archiveSourceLink(sid,{turn:1,seq:-1}));
  writeFileSync(join(dir,'memory.md'),'# memory',{mode:0o600});
  archive.flush(sid);
  first=archive.read(sid,{turn:1});
  assert.ok(first.includes('[Session 记忆](<memory.md>)'));
  const index=readFileSync(join(dir,'index.md'),'utf8');
  assert.ok(index.includes('当前状态与关键记忆'));assert.ok(index.includes('开始（UTC）'));assert.ok(index.includes('会话级事件'));
  assert.equal(readFileSync(info.turns[1].file,'utf8'),first);
  assert.equal(new SessionArchive({root}).read(sid,{turn:1}),first);
});

test('reasoning and advice are shown once only under unique request/assistant association', t => {
  const { archive }=fixture(t),sid='reasoning-one',request=randomUUID();
  append(archive,sid,0,'turn/start',{turn:1});
  append(archive,sid,1,'step/start',{turn:1,step:1});
  const raw='raw-unique-thinking', advice='advice-unique', processed='processed-unique\n'+advice;
  archive.recordReasoning(sid,{id:randomUUID(),request,turn:1,step:1,block:0,original:raw,phase:'original'});
  archive.recordReasoning(sid,{id:randomUUID(),request,turn:1,step:1,block:0,processed,advice,phase:'processed'});
  append(archive,sid,2,'assistant/message',{turn:1,step:1,message:{role:'assistant',content:[{type:'reasoning',text:processed},{type:'text',text:'answer-unique'}]},stream:[]});
  let md=archive.read(sid,{turn:1});
  for(const text of [raw,advice,'processed-unique','answer-unique'])assert.equal(occurrences(md,text),1,text);
  assert.match(md,/宿主 reasoning 与上方 IoT 记录一致/);
  assert.match(md,/不是新指令或授权/);
  // Another request in the same step invalidates identity inference, even with exact text.
  archive.recordReasoning(sid,{id:randomUUID(),request:randomUUID(),turn:1,step:1,block:0,original:'other-request'});
  md=archive.read(sid,{turn:1});
  assert.match(md,/无法唯一关联/);assert.doesNotMatch(md,/宿主 reasoning 与上方 IoT 记录一致/);
  assert.equal(occurrences(md,'processed-unique'),2,'ambiguous associations remain explicit, not silently deduplicated');
});

test('completed turn coverage stays stable as later events append; restart refreshes obsolete renderer', t => {
  const {archive,root}=fixture(t),sid='stable-render';
  append(archive,sid,0,'turn/start',{turn:1});
  append(archive,sid,1,'user/message',message('user','第一轮'));
  append(archive,sid,2,'turn/end',{turn:1,reason:{kind:'completed'}});
  append(archive,sid,3,'turn/start',{turn:2}); // establish next navigation link
  archive.flush(sid);
  const first=archive.read(sid,{turn:1});
  append(archive,sid,4,'user/message',message('user','第二轮'));
  append(archive,sid,5,'turn/end',{turn:2,reason:{kind:'completed'}});
  archive.flush(sid);
  assert.equal(archive.read(sid,{turn:1}),first);
  assert.equal(readFileSync(archive.inspect(sid).turns[0].file,'utf8'),first);
  const firstFile=archive.inspect(sid).turns[0].file;
  writeFileSync(firstFile,'old renderer output');
  const restarted=new SessionArchive({root});restarted.flush(sid);
  assert.equal(readFileSync(firstFile,'utf8'),restarted.read(sid,{turn:1}));
  assert.match(readFileSync(firstFile,'utf8'),/readable-v2/);
});
