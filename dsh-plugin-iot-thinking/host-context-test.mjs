// Run with the installed Electron runtime (ASAR-aware), not an unrelated seam copy.
// ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" host-context-test.mjs [plugin-directory]
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
const anchor = process.env.DSH_INSTALL_DIR;
assert.ok(anchor, 'DSH_INSTALL_DIR must identify the running installation');
const require = createRequire(join(anchor, 'package.json'));
const { Context, Service } = await import(require.resolve('@deepseek-ai/cordis'));
const { default: SystemPrompt, renderContextSnapshot } = await import(require.resolve('@deepseek-ai/dsh-system-prompt'));
const { default: Tools } = await import(require.resolve('@deepseek-ai/dsh-tools'));
const Native = await import(require.resolve('@deepseek-ai/dsh-tool-executor-native'));
const dir = mkdtempSync(join(tmpdir(), 'iot-host-context-'));
process.env.IOT_THINKING_CONFIG_PATH = join(dir, 'config.json');
process.env.IOT_THINKING_DATA_DIR = dir;
// Explicitly use the installed node_modules root rather than a package directory.
process.env.IOT_DSH_SEAM_ANCHORS = resolve(dirname(require.resolve('@deepseek-ai/dsh-system-prompt')), '../../..');
// Usage: [plugin-directory] --expect-context for a candidate containing the injection;
// omit the flag to assert that the currently rolled-back baseline has NO context.
const target = resolve(process.argv.slice(2).find(arg => !arg.startsWith('--')) ?? dirname(fileURLToPath(import.meta.url)));
const expectContext = process.argv.includes('--expect-context');
const plugin = await import(pathToFileURL(join(target, 'index.mjs')));
const { ConfigStore } = await import(pathToFileURL(join(target, 'lib/config-store.mjs')));
const { markConsulted, noteBlock, resetTeacherState } = await import(pathToFileURL(join(target, 'lib/teacher.mjs')));
let modelCalls = 0;
class FakeLlm extends Service { constructor(ctx) { super(ctx, 'llm'); } stream() { modelCalls++; throw new Error('context assembly must not invoke models'); } }
const root = new Context();
await root.plugin(SystemPrompt, {});
await root.plugin(FakeLlm);
await root.plugin(Tools);
await root.plugin(Native);
const fiber = await root.plugin(plugin, { webApi: false, command: false });
// Exercise optional injection fibers using the actual Cordis scheduler.
await new Promise(resolve => setImmediate(resolve));

const store = new ConfigStore(process.env.IOT_THINKING_CONFIG_PATH);
store.save({enabled:true, strategy:'off', advisor:{teacher:{enabled:false, depthRange:[3,5], cooldownBlocks:3}}, sessions:{a:{teacher:{enabled:true}},b:{teacher:{enabled:false}}}});
const agent = id => ({ session: { id } });
const a = agent('a'), b = agent('b');
const context = agent => ({ agent, scope:agent });
const start = performance.now();
const assembly = await root.systemPrompt.assemble(context(a));
const text = renderContextSnapshot(assembly);
const tool = assembly.tools.find(t => t.name === 'iot_consult_teacher');
assert.ok(tool, 'a new session receives the actual tool schema');
for (const key of ['ask', 'depth', 'goal', 'progress']) assert.ok(tool.parameters.properties[key]);
assert.deepEqual(tool.parameters.properties.ask.enum, ['advice','plan','framework','approach','review']);
assert.match(tool.description, /咨询/);
assert.doesNotMatch(tool.description, /思考里/);
if (expectContext) {
assert.match(text, /iot_consult_teacher/);
assert.doesNotMatch(text, /undefined|NaN/);
assert.equal(renderContextSnapshot(await root.systemPrompt.assemble(context(b))), '');
assert.match(text, /3-5/);
markConsulted('a', {cooldownBlocks:3});
assert.match(renderContextSnapshot(root.systemPrompt.refreshContext(assembly, context(a))), /冷却.*3/);
for(let i=0;i<3;i++) noteBlock('a');
assert.match(renderContextSnapshot(root.systemPrompt.refreshContext(assembly, context(a))), /可用/);
assert.equal(renderContextSnapshot(await root.systemPrompt.assemble()), '', 'no agent: no unknown-session state');
store.save({enabled:false});
assert.equal(renderContextSnapshot(root.systemPrompt.refreshContext(assembly, context(a))), '', 'master off');
store.save({enabled:true, advisor:{teacher:{depthRange:[2,4]}}});
assert.match(renderContextSnapshot(root.systemPrompt.refreshContext(assembly, context(a))), /2-4/);
const optional = root.systemPrompt.suppressRuntimeContext();
assert.equal(renderContextSnapshot(await root.systemPrompt.assemble(context(a))), '', 'honor host suppression');
optional();
assert.ok(renderContextSnapshot(await root.systemPrompt.assemble(context(a))));
} else {
  assert.equal(text, '', 'rollback baseline must not register teacher context');
}
for(let i=0;i<100;i++) await root.systemPrompt.assemble(context(a));
assert.ok(performance.now()-start < 3000, 'assembly/refresh must remain bounded');
assert.equal(modelCalls, 0);
await fiber.dispose();
assert.equal(renderContextSnapshot(await root.systemPrompt.assemble(context(a))), '', 'unload disposes context');
await root.fiber.dispose();
resetTeacherState();
console.log(`PASS installed Cordis/SystemPrompt (${expectContext ? 'candidate context' : 'rollback baseline'}): lifecycle/assembly bounded; no model/network calls`);
