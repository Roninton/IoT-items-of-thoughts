// Standalone entry integration regression test; no network or real profile writes.
// Run: node --experimental-vm-modules teacher-entry-test.mjs
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

// SourceTextModule 需要 --experimental-vm-modules：缺标志时给出可执行的提示，
// 而不是抛一个看不懂的 "not a constructor"。
if (typeof vm.SourceTextModule !== "function") {
  console.error("teacher-entry-test 需要 --experimental-vm-modules；请用 `pnpm run test:teacher` 或 `node --experimental-vm-modules teacher-entry-test.mjs`。");
  process.exit(2);
}
import { ConfigStore } from "./lib/config-store.mjs";
import { resetTeacherState, teacherStatus, consultTeacher, markConsulted } from "./lib/teacher.mjs";

const dir = mkdtempSync(join(tmpdir(), "iot-teacher-entry-"));
const events = [], calls = [];
let store, tool, streamHook, registrations = 0, mode = "success", closed = false;
// Match the installed defineTool contract: parameters are field specs;
// output.schema is a value spec, output.render must yield ContentBlock[].
function validate(spec, value) {
  if (spec.type === "object") {
    assert.equal(typeof value, "object");
    for (const [key, child] of Object.entries(spec.properties ?? {})) {
      if (child.required) assert.ok(Object.hasOwn(value, key), `missing ${key}`);
      if (Object.hasOwn(value, key)) validate(child, value[key]);
    }
    if (spec.additionalProperties === false) for (const key of Object.keys(value)) assert.ok(key in spec.properties, `extra ${key}`);
  } else assert.equal(typeof value, spec.type);
  if (spec.enum) assert.ok(spec.enum.includes(value));
}
const defineTool = (definition) => ({
  ...definition,
  async execute(args, execution) {
    for (const [key, spec] of Object.entries(definition.parameters)) {
      if (spec.required) assert.ok(Object.hasOwn(args, key));
      if (Object.hasOwn(args, key)) validate(spec, args[key]);
    }
    const result = await definition.execute(args, execution);
    validate(definition.output.schema, result);
    const content = definition.output.render(args, result);
    assert.ok(Array.isArray(content), "render must return ContentBlock[]");
    for (const block of content) { assert.equal(block.type, "text"); assert.equal(typeof block.text, "string"); }
    return result;
  },
});
const Llm = {
  isAgentLoopRequest: (options) => options.purpose === "agent-loop",
  createUserMessage: (message) => { assert.ok(Array.isArray(message.content)); return message; },
  BlockAssembler: class {
    text = "";
    push(chunk) { if (chunk.type === "text-delta") this.text += chunk.text; }
    blocks() { return [{ type: "text", text: this.text }]; }
  },
};
const context = vm.createContext({ console, setTimeout, clearTimeout, AbortController, DOMException });
const entry = new vm.SourceTextModule(readFileSync(new URL("./index.mjs", import.meta.url), "utf8"), { context, identifier: fileURLToPath(new URL("./index.mjs", import.meta.url)) });
await entry.link(async (specifier) => {
  let values;
  if (specifier === "./lib/seams.mjs") values = { importSeam: async (name) => name === "@deepseek-ai/dsh-tools" ? { defineTool } : Llm };
  else if (specifier === "./lib/config-store.mjs") values = {
    ConfigStore: class extends ConfigStore { constructor(_path, cfg) { super(join(dir, "config.json"), cfg); store = this; } },
    defaultConfigPath: () => join(dir, "config.json"),
  };
  else if (specifier === "./lib/ledger.mjs") values = { mark: (event, data) => events.push({ event, ...data }), archiveOriginal: () => {} };
  else values = await import(new URL(specifier, import.meta.url));
  const names = Object.keys(values);
  return new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, values[name]); }, { context });
});
await entry.evaluate();
const ctx = {
  get: () => undefined,
  on: (name, handler) => { if (name === "llm/stream") streamHook = handler; else assert.ok(['session/event', 'session/created', 'session/flush', 'session/disposed'].includes(name)); },
  effect: (fn) => fn(),
  // This fixture intentionally lacks systemPrompt, as a headless host may.
  // Actual registration/assembly/refresh is exercised by host-context-test.mjs.
  inject: (deps, fn) => deps.includes("systemPrompt") ? undefined : fn({ tools: { register: (definition, options) => { assert.equal(options?.when, undefined, "tool list must not depend on session switches"); registrations++; tool = definition; return () => {}; } } }),
  llm: { stream: async function* (options) {
    calls.push(options);
    if (mode === "success") { yield { type: "text-delta", text: "local teacher answer" }; return; }
    if (mode === "empty") return;
    try {
      yield { type: "text-delta", text: "partial must not become success" };
      await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
    } finally { closed = true; }
  } },
};
entry.namespace.apply(ctx, { webApi: false, command: false, sessionLog: false, sessionMemory: false });
const args = { depth: 5, ask: "review", goal: "x".repeat(600), progress: "y".repeat(1200) };
const exec = (id, signal = new AbortController().signal) => ({ agent: { session: { id } }, signal });
const save = (patch) => store.save(patch);
const run = (id = "a", signal) => tool.execute(args, exec(id, signal));
const declaration = JSON.stringify({ name: tool.name, description: tool.description, parameters: tool.parameters });
const stable = () => { assert.equal(registrations, 1); assert.equal(JSON.stringify({ name: tool.name, description: tool.description, parameters: tool.parameters }), declaration); };
resetTeacherState();
stable();
save({ enabled: true, strategy: "off", guidance: { enabled: false }, advisor: { enabled: false, route: { provider: "stub", model: "strong" }, teacher: { enabled: false, cooldownBlocks: 0 } }, sessions: { a: { teacher: { enabled: true } }, b: { teacher: { enabled: false } } } });
stable();
const disabledStart = performance.now();
assert.equal((await run("b")).ok, false); assert.equal(calls.length, 0);
assert.ok(performance.now() - disabledStart < 100, "disabled session must reject locally without waiting for a model");
assert.equal((await run()).ok, true); assert.equal(calls[0].provider, "stub"); assert.equal(calls[0].model, "strong");
console.log("ok: original entry resolves imported advisor route; guidance/strategy off do not disable independent teacher; render/schema validated");
save({ advisor: { teacher: { enabled: true, cooldownBlocks: 0 } } });
stable();
assert.equal((await run("b")).ok, false);
save({ enabled: false });
stable(); assert.equal((await run()).ok, false);
save({ enabled: true, sessions: { a: { teacher: { enabled: false } } } });
stable(); assert.equal((await run()).ok, false);
save({ sessions: { a: { teacher: { enabled: true } } } });
stable();
console.log("ok: stable tool declaration; execute uses dynamic session overrides and global master gate");
save({ advisor: { teacher: { enabled: true, cooldownBlocks: 0, maxTokens: 333, inputBudget: 1000, depthRange: [1, 1] } } });
await run(); const firstBudget = calls.at(-1).messages[0].content[0].text.length;
assert.equal(calls.at(-1).maxTokens, 333);
save({ advisor: { teacher: { enabled: true, cooldownBlocks: 4, maxTokens: 777, inputBudget: 12000, depthRange: [2, 2] } } });
const hot = await run(); assert.equal(calls.at(-1).maxTokens, 777); assert.ok(calls.at(-1).messages[0].content[0].text.length > firstBudget);
assert.equal(events.at(-1).depth, 2); assert.equal(hot.remainingBlocks, 4);
assert.equal((await run()).ok, false); assert.equal(teacherStatus("other").calls, 0);
assert.equal((await run("other")).ok, true);
console.log("ok: hot maxTokens/inputBudget/depth/cooldown budgets and session-isolated cooldown");
resetTeacherState();
save({ enabled: true, advisor: { route: { provider: null, model: null }, teacher: { enabled: true, cooldownBlocks: 0 } }, sessions: { a: { teacher: { enabled: true } }, b: { teacher: { enabled: true } } } });
const remember = async (sessionId, provider, model) => {
  const output = streamHook({ purpose: "agent-loop", sessionId, provider, model }, () => (async function* () {})());
  for await (const _ of output) {}
};
await remember("a", "provider-a", "model-a");
await remember("b", "provider-b", "model-b");
assert.equal((await run("a")).ok, true);
assert.equal(calls.at(-1).provider, "provider-a", "教师 fallback 路由必须按 session 隔离");
assert.equal(calls.at(-1).model, "model-a");
resetTeacherState();
mode = "wait"; closed = false;
const concurrentAbort = new AbortController();
const firstConcurrent = run("a", concurrentAbort.signal);
await new Promise((resolve) => setImmediate(resolve));
const callCountDuringFirst = calls.length;
const secondConcurrent = await run("a");
assert.equal(secondConcurrent.ok, false);
assert.match(secondConcurrent.reason, /正在进行/);
assert.equal(calls.length, callCountDuringFirst, "并发拒绝不能再发模型请求");
concurrentAbort.abort(new Error("end concurrent test"));
await assert.rejects(firstConcurrent, /end concurrent test/);
await new Promise((resolve) => setImmediate(resolve));
mode = "success";
assert.equal((await run("a")).ok, true, "失败/取消后预约必须释放");
console.log("ok: teacher route is session-scoped; concurrent consult is rejected and cancellation releases reservation");

resetTeacherState();
const aborted = new AbortController(); aborted.abort(new Error("already cancelled"));
const count = calls.length; await assert.rejects(run("a", aborted.signal), /already cancelled/); assert.equal(calls.length, count);
stable();
mode = "wait"; closed = false;
const abort = new AbortController(); const pending = run("a", abort.signal);
await new Promise((resolve) => setImmediate(resolve)); abort.abort(new Error("caller cancelled"));
await assert.rejects(pending, /caller cancelled/);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(calls.at(-1).signal.aborted, true); assert.equal(closed, true); assert.equal(teacherStatus("a").calls, 0);
console.log("ok: pre-abort prevents calls; active cancellation forwards signal, closes cooperative stream and never commits partial success/cooldown");
mode = "empty"; assert.equal((await run()).ok, false);
const timeout = await consultTeacher({ ctx: { llm: { stream: async function* () { await new Promise(() => {}); } } }, Llm, route: { provider: "stub", model: "stub" }, prompt: "timeout", options: {}, timeoutMs: 20 });
assert.equal(timeout.code, "TEACHER_TIMEOUT");
console.log("ok: empty response schema and bounded timeout for uncooperative stream");
resetTeacherState();
save({ enabled: true, strategy: "off", advisor: { teacher: { enabled: true } } });
markConsulted("a", { cooldownBlocks: 3 });
const chunks = [{ type: "block-start", index: 0, blockType: "reasoning" }, { type: "reasoning-delta", index: 0, text: "unaltered" }, { type: "block-end", index: 0, block: { type: "reasoning", text: "unaltered" } }];
const bypass = async (purpose = "agent-loop") => {
  const output = [];
  for await (const chunk of streamHook({ purpose, sessionId: "a" }, () => (async function* () { yield* chunks; })())) output.push(chunk);
  assert.deepEqual(output, chunks);
};
const beforeBypass = calls.length;
await bypass(); assert.equal(teacherStatus("a").remainingBlocks, 2);
await bypass("compaction"); assert.equal(teacherStatus("a").remainingBlocks, 2);
save({ enabled: false }); await bypass(); assert.equal(teacherStatus("a").remainingBlocks, 2);
save({ enabled: true, sessions: { a: { teacher: { enabled: false } } } });
await bypass(); assert.equal(teacherStatus("a").remainingBlocks, 2); assert.equal(calls.length, beforeBypass);
console.log("ok: compression-off bypass advances teacher cooldown only for enabled teacher agent loops, preserving chunks and issuing no model calls");
console.log("PASS teacher entry regression tests; no network calls, config isolated at " + dir);
