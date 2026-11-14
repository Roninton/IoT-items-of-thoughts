// Read/verify real experiment, rebuild derived Markdown only; no model calls.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { SessionArchive, archiveFiles } from './lib/session-archive.mjs';
import { renderSessionMemory } from './lib/session-memory.mjs';
const root='/Users/zhangtianxu/.dsh/plugin-data/iot-thinking/sessions';
const sessionId='session-dd64b0b5-8e85-439a-bc47-6794de58153d';
const archive=new SessionArchive({root});
const info=archive.inspect(sessionId), journal=info.files.events;
const hash=text=>createHash('sha256').update(text).digest('hex');
const before=readFileSync(journal), beforeHash=hash(before);
const turnFile=info.turns.find(t=>t.turn===1).file;
const oldMD=readFileSync(turnFile,'utf8');
const events=archive.read(sessionId,{format:'events',limit:10000});
const records=archive.read(sessionId,{format:'reasoning',limit:10000});
const original=records.filter(r=>r.phase==='original'), processed=records.filter(r=>r.phase==='processed');
assert.ok(original.length>0);assert.ok(processed.length>0);
for(const r of original){assert.equal(hash(r.original),r.sha256);assert.equal(Buffer.byteLength(r.original),r.bytes);}
const matched=[];
for(const r of processed){
 const raw=original.find(o=>o.request===r.request&&o.block===r.block);assert.ok(raw);
 const candidates=events.filter(e=>e.type==='assistant/message'&&e.data.turn===r.turn&&e.data.step===r.step);
 assert.ok(candidates.some(e=>e.data.message.content.some(b=>b.type==='reasoning'&&b.text===r.processed)));
 matched.push({step:r.step,rawChars:raw.original.length,processedChars:r.processed.length,adviceChars:r.advice.length});
}
const routes=events.filter(e=>e.type==='assistant/message').map(e=>e.data.message.source);
assert.ok(routes.length>0);for(const r of routes){assert.equal(r.provider,'dgx-spark-tp2');assert.equal(r.model,'DeepSeek-V4.1-Flash-1017');}
assert.equal(info.coverage.completePrefix,true);
archive.flush(sessionId);
const memoryPath=join(info.directory,'memories.json');
const memoryBefore=readFileSync(memoryPath);
archive._locked(sessionId,false,dir=>archiveFiles.atomic(join(dir,'memory.md'),renderSessionMemory(sessionId,JSON.parse(memoryBefore).history)));
archive.flush(sessionId);
assert.equal(hash(readFileSync(journal)),beforeHash,'render must never change journal');
assert.deepEqual(readFileSync(memoryPath),memoryBefore,'render must never change memory revisions');
const newMD=readFileSync(turnFile,'utf8');
assert.ok(!newMD.includes('Exact host event'));
const final=events.filter(e=>e.type==='assistant/message').at(-1).data.message.content.find(b=>b.type==='text').text;
assert.equal(newMD.split(final).length-1,1,'final answer is rendered once');
const report={sessionId,provider:'dgx-spark-tp2',model:'DeepSeek-V4.1-Flash-1017',eventCount:events.length,originalBlocks:original.length,processedBlocks:processed.length,withAdvice:processed.filter(r=>r.advice).length,sha256AllVerified:true,processedAllMatchHost:true,journalHash:beforeHash,journalUnchanged:true,memoryHistoryUnchanged:true,matched,markdown:{oldLines:oldMD.split('\n').length,newLines:newMD.split('\n').length,oldBytes:Buffer.byteLength(oldMD),newBytes:Buffer.byteLength(newMD)},files:{index:info.files.index,turn:turnFile,memory:join(info.directory,'memory.md')}};
console.log(JSON.stringify(report,null,2));
