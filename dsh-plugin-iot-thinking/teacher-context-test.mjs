import assert from 'node:assert/strict';
import { teacherContextText, registerTeacherContext, TEACHER_CONTEXT_MAX_CHARS } from './lib/teacher-prompt.mjs';
import { markConsulted, noteBlock, resetTeacherState } from './lib/teacher.mjs';
resetTeacherState();
for (const limits of [undefined, {}, {depthRange:[5,1]}, {depthRange:[3,5],cooldownBlocks:20}]) {
  const text = teacherContextText({enabled:true,sessionId:'test',limits});
  assert.ok(text.length <= TEACHER_CONTEXT_MAX_CHARS);
  assert.doesNotMatch(text, /undefined|NaN/);
  for(const parameter of ['ask','depth','goal','progress']) assert.ok(text.includes(parameter));
  assert.match(text,/由你决定/);
}
assert.equal(teacherContextText({enabled:false,sessionId:'test'}),'');
assert.equal(teacherContextText({enabled:true,sessionId:'unknown'}),'');
assert.equal(teacherContextText({enabled:true}),'');
markConsulted('test',{cooldownBlocks:3});
assert.match(teacherContextText({enabled:true,sessionId:'test'}),/冷却中，还需 3/);
for(let i=0;i<3;i++) noteBlock('test');
assert.match(teacherContextText({enabled:true,sessionId:'test'}),/可用/);
let entry, reports=0;
registerTeacherContext({inject: (_deps, fn)=>fn({systemPrompt:{context:e=>{entry=e;return ()=>{};}}})},()=>{throw new Error('config failed');},()=>{reports++;throw new Error('log failed');});
assert.equal(entry.interpolate,false);
for(let i=0;i<10;i++) assert.equal(entry.text({agent:{session:{id:'test'}}}),'');
assert.equal(reports,1,'errors bounded, logging failure cannot break rendering');
assert.equal(entry.text({}),'');
resetTeacherState();
console.log('PASS context text: bounded length, parameter discovery, missing identity, cooldown, fail-open callback and bounded diagnostics');
