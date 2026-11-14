import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createArchiveRuntime } from './lib/archive-runtime.mjs';
import { transformStream } from './lib/stream.mjs';

function context(events = []) {
  const hooks = new Map(), effects = [];
  return {
    hooks, effects,
    on(name, fn) { hooks.set(name, fn); },
    effect(fn) { effects.push(fn()); },
    get(name) { return name === 'sessionQuery' ? { async readSession() { return { events }; } } : undefined; },
  };
}
const root = realpathSync(mkdtempSync(join(tmpdir(), 'iot-archive-runtime-')));
process.env.IOT_THINKING_DATA_DIR = root;
const events = [
  {seq:0,type:'turn/start',timestamp:1,data:{turn:1}},
  {seq:1,type:'user/message',timestamp:2,data:{role:'user',content:[{type:'text',text:'用户需求'}],source:{kind:'user'}}},
  {seq:2,type:'step/start',timestamp:3,data:{turn:1,step:1}},
];
const ctx = context(events), runtime = createArchiveRuntime(ctx, { getConfig: () => ({sessionLog:true}) });
const source = async function* () {
  yield {type:'block-start',index:0,blockType:'reasoning'};
  yield {type:'reasoning-delta',index:0,text:'original long reasoning for reliable archive'};
  yield {type:'block-end',index:0,block:{type:'reasoning',text:'original long reasoning for reliable archive'}};
  yield {type:'block-start',index:1,blockType:'text'};
  yield {type:'text-delta',index:1,text:'正文'};
  yield {type:'block-end',index:1,block:{type:'text',text:'正文'}};
};
const request = runtime.request({sessionId:'s1',provider:'test',model:'test'});
const output = [];
for await (const item of transformStream(request.observe(source()), {sessionId:'s1'}, {
  shouldTransform:()=>true,getConfig:()=>({enabled:true,strategy:'iot-full',minChars:1}),
  rewrite:async()=>({text:'[决定] archive', advice:'[旁观者建议] 验证。'}),beforeReplace:request.replacement,
})) output.push(item);
assert.ok(output.some(x=>x.type==='reasoning-delta'&&x.text.includes('[决定] archive')));
assert.ok(output.some(x=>x.type==='text-delta'&&x.text==='正文'));
ctx.hooks.get('session/event')({id:'s1'}, {seq:3,type:'assistant/message',timestamp:4,data:{turn:1,step:1,message:{role:'assistant',content:[{type:'text',text:'正文'}]},stream:[]}});
ctx.hooks.get('session/event')({id:'s1'}, {seq:4,type:'turn/end',timestamp:5,data:{turn:1,reason:'completed'}});
await runtime.settled();
const raw = runtime.archive.read('s1',{format:'reasoning'});
assert.ok(JSON.stringify(raw).includes('original long reasoning'));
assert.ok(JSON.stringify(raw).includes('[决定] archive'));
assert.ok(JSON.stringify(raw).includes('旁观者建议'));
const md = runtime.archive.read('s1',{turn:1});
assert.match(md,/用户需求/); assert.match(md,/正文/);
assert.match(md,/original long reasoning/);
assert.match(md,/archive/);

// Failed archival must veto replacement, not fail the provider stream.
const bad = createArchiveRuntime(context(), {getConfig:()=>({sessionLog:true}),archive:{recordReasoning(){throw new Error('disk full');},getPosition(){return{};},flush(){}}});
const capture = bad.request({sessionId:'s2'}), kept=[];
for await (const x of transformStream(capture.observe(source()), {sessionId:'s2'}, {
  shouldTransform:()=>true,getConfig:()=>({enabled:true,strategy:'iot-full',minChars:1}),rewrite:async()=>'[决定] short',beforeReplace:capture.replacement,
})) kept.push(x);
assert.ok(kept.some(x=>x.type==='reasoning-delta'&&x.text==='original long reasoning for reliable archive'));
assert.ok(!kept.some(x=>x.type==='reasoning-delta'&&x.text==='[决定] short'));

// Strategy off still captures unchanged original; interrupted stream archives partial.
const off = runtime.request({sessionId:'s1'});
for await(const _ of off.observe((async function*(){yield {type:'reasoning-delta',index:3,text:'partial interrupted'};})())){}
await runtime.settled();
assert.match(JSON.stringify(runtime.archive.read('s1',{format:'reasoning'})),/partial interrupted/);
// A provider whose assembled block disagrees with its deltas cannot claim the
// archived block is the exact original that transformStream will replace.
const mismatch=runtime.request({sessionId:'s1'});
for await(const _ of mismatch.observe((async function*(){yield {type:'block-end',index:9,block:{type:'reasoning',text:'archived version'}};})())){}
assert.equal(mismatch.replacement({index:9,original:'different buffered version',processed:'short',strategy:'iot-full'}),false);
for (const dispose of ctx.effects) dispose?.();
console.log('PASS archive runtime: event backfill, original/processed association, final body, fail closed and interrupted block');
