// smoke.mjs — host-side smoke test for the llm/stream wiring.
//
// selftest.mjs proves the pure logic; this file proves the plugin is actually
// wired into dsh. It loads the real entry (`index.mjs`) with the real
// `@deepseek-ai/dsh-llm` seam, drives one agent-loop reasoning block through the
// `llm/stream` hook, and then calls the Settings routes. Everything dsh would
// provide is faked: the plugin context, the webServer, and the compressor's model
// call.
//
// Why it exists: the hardcoded `reasoningEffort: "off"` (issue #3) failed 100% of
// compressions on pi-ai routes while every pure test stayed green, because no
// test ever asserted what the compressor request carried. This one does.
//
//   node dsh-plugin-iot-thinking/smoke.mjs
//
// Needs a dsh install on disk for the seam. Resolution order: IOT_DSH_SEAM_ANCHORS,
// $DSH_HOME/profiles/node_modules, $DSH_HOME/dsh-*/node_modules, <repo>/../node_modules,
// ~/.dsh/profiles/node_modules.
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import vm from "node:vm";

// engines says node >= 20, so avoid `import.meta.dirname` (20.11+).
const pluginDir = fileURLToPath(new URL(".", import.meta.url)).replace(/[\\/]$/, "");
const repoRoot = join(pluginDir, "..");

/**
 * Locate the node_modules root that holds the host's @deepseek-ai packages.
 * @returns {string|null} the root, or null when no dsh install is reachable.
 */
function findSeamRoot() {
  const home = process.env.DSH_HOME;
  const candidates = [
    ...(process.env.IOT_DSH_SEAM_ANCHORS ?? "").split(delimiter).filter(Boolean),
    ...(home ? [join(home, "profiles", "node_modules")] : []),
    ...(home && existsSync(home)
      ? readdirSync(home, { withFileTypes: true })
          .filter((entry) => entry.isDirectory() && /^dsh-/i.test(entry.name))
          .map((entry) => ({ dir: join(home, entry.name), mtime: safeMtime(join(home, entry.name)) }))
          .sort((a, b) => b.mtime - a.mtime)
          .map((entry) => join(entry.dir, "node_modules"))
      : []),
    join(repoRoot, "..", "node_modules"),
    join(homedir(), ".dsh", "profiles", "node_modules"),
  ];
  return candidates.find((root) => existsSync(join(root, "@deepseek-ai", "dsh-llm", "package.json"))) ?? null;
}

function safeMtime(path) {
  try {
    return statSync(path).mtimeMs || 0;
  } catch {
    return 0;
  }
}

const seamRoot = findSeamRoot();
if (seamRoot === null) {
  console.error(
    "smoke: 找不到宿主的 @deepseek-ai/dsh-llm。\n" +
      "       设置 IOT_DSH_SEAM_ANCHORS 指向宿主 node_modules（或 $DSH_HOME/profiles/node_modules）。",
  );
  process.exit(1);
}
process.env.IOT_DSH_SEAM_ANCHORS = seamRoot;

// Keep this run out of the operator's real ledger and config.
const workDir = mkdtempSync(join(tmpdir(), "iot-thinking-smoke-"));
process.env.IOT_THINKING_DATA_DIR = join(workDir, "data");
process.env.IOT_THINKING_CONFIG_PATH = join(workDir, "config.json");
writeFileSync(
  process.env.IOT_THINKING_CONFIG_PATH,
  `${JSON.stringify(
    {
      enabled: true,
      profile: "full",
      minChars: 1,
      maxChars: 24000,
      maxTokens: 1200,
      maxNodes: 8,
      maxNeutral: 2,
      provider: null,
      model: null,
      reasoningEffort: "auto",
      archive: false,
    },
    null,
    2,
  )}\n`,
  "utf8",
);

const { importSeam } = await import(pathToFileURL(join(pluginDir, "lib", "seams.mjs")).href);
const Llm = await importSeam("@deepseek-ai/dsh-llm");

const plugin = await import(pathToFileURL(join(pluginDir, "index.mjs")).href);
assert.equal(plugin.name, "iot-thinking");
assert.deepEqual(plugin.inject, ["llm"]);
assert.equal(typeof plugin.apply, "function");

const GOOD = [
  "[中性] 冒烟测试跑通，仓库无新增污染。",
  "[决定] 保留现有实现，只在 effort 与退化判定上动手。",
  "[风险] 不支持 off 的路由上压缩器会自己思考，每块多十几秒。",
  "[计划] 重启 web 后用真实会话验证降级路径与一键测试。",
].join("\n\n");
const DRAFT = [
  "先看现状：插件是在 llm/stream 上做块级替换，块结束时才压缩。",
  "要不要改成边压边回？不行，块结束才是唯一稳定锚点，边压边回会把 delta 顺序搞乱。",
  "另一个方向是直接删掉 reasoningEffort 字段，这样每个路由都能跑，但原生 DeepSeek 路由会丢掉 off 的快路径。",
  "所以默认档位应该是先试 off，被拒再不带该字段，并且把路由决策记住，别每块都白试一次。",
].join("\n");

const routes = new Map();
const hooks = new Map();
const webServer = {
  register({ path, handler }) {
    routes.set(path, handler);
    return () => routes.delete(path);
  },
};

const compressorCalls = [];
let rejectOff = true;
const ctx = {
  llm: {
    listProviders: () => [{ id: "deepseek-official", name: "DeepSeek" }],
    listModels: async (provider) => [{ provider, id: "deepseek-flash", name: "DeepSeek Flash" }],
    resolveModelInfo: async (provider, model) => ({
      provider,
      id: model,
      reasoning: { efforts: [{ id: "off" }, { id: "low" }, { id: "high" }] },
    }),
    stream: (options) => {
      compressorCalls.push({ ...options });
      if (options.reasoningEffort === "off" && rejectOff) {
        rejectOff = false;
        return (async function* () {
          yield {
            type: "finish",
            reason: {
              kind: "error",
              failure: {
                message: 'provider "p" model "m" does not support reasoning effort "off"',
                code: "UNSUPPORTED_REASONING_EFFORT",
              },
            },
          };
        })();
      }
      return (async function* () {
        yield { type: "text-delta", index: 0, text: GOOD };
        yield { type: "finish", reason: { kind: "stop" } };
      })();
    },
  },
  // 没有 agentPresets 服务（headless 组合就是这样）：/api/presets 必须答
  // `available: false` 而不是 500，卡片据此退化成手输 id。
  get: (name) => (name === "webServer" ? webServer : undefined),
  effect: () => {},
  inject: () => {},
  logger: { warn: (message) => console.log("warn:", message) },
};
ctx.on = (event, handler) => hooks.set(event, handler);

plugin.apply(ctx, {});
for (const path of ["/iot-thinking/api/config", "/iot-thinking/api/status", "/iot-thinking/api/models", "/iot-thinking/api/test", "/iot-thinking/api/session", "/iot-thinking/api/presets"]) {
  assert.ok(routes.has(path), `${path} registered`);
}
const streamHook = hooks.get("llm/stream");
assert.equal(typeof streamHook, "function", "llm/stream hook registered");

// One real agent-loop request: a reasoning block that must be compressed in
// place. The compressor rejects `off` first, so this also exercises the
// effort downgrade the Settings card relies on.
const options = Llm.markAgentLoopRequest({
  provider: "deepseek-official",
  model: "deepseek-flash",
  sessionId: "smoke",
});
const source = async function* () {
  yield { type: "block-start", index: 0, blockType: "reasoning" };
  for (const line of DRAFT.split("\n")) yield { type: "reasoning-delta", index: 0, text: `${line}\n` };
  yield { type: "block-end", index: 0, block: { type: "reasoning", text: `${DRAFT}\n` } };
  yield { type: "text-delta", index: 1, text: "好的。" };
  yield { type: "finish", reason: { kind: "stop" } };
};
const emitted = [];
for await (const chunk of streamHook(options, () => source())) emitted.push(chunk);

const replacedDelta = emitted.find((chunk) => chunk.type === "reasoning-delta");
assert.ok(replacedDelta, "a compact reasoning delta must be emitted");
assert.ok(replacedDelta.text.includes("[决定]"), replacedDelta.text);
assert.ok(
  !emitted.some((chunk) => chunk.type === "reasoning-delta" && chunk.text.includes("边压边回")),
  "original reasoning deltas must be dropped",
);
const blockEnd = emitted.find((chunk) => chunk.type === "block-end");
assert.equal(blockEnd.block.text, replacedDelta.text, "block-end carries the compact text");
assert.ok(emitted.some((chunk) => chunk.type === "text-delta" && chunk.text === "好的。"), "body untouched");

assert.equal(compressorCalls.length, 2, `expected 2 compressor calls, got ${compressorCalls.length}`);
assert.equal(compressorCalls[0].reasoningEffort, "off");
assert.equal(compressorCalls[1].reasoningEffort, "low", "auto mode picks the route's cheap level");
assert.equal(compressorCalls[0].purpose, "compaction");
assert.deepEqual(compressorCalls[0].messages[0].source, { kind: "plugin", plugin: "iot-thinking" });

const ledger = join(process.env.IOT_THINKING_DATA_DIR, "events.jsonl");
assert.ok(existsSync(ledger), "ledger written");
const events = readFileSync(ledger, "utf8")
  .split(/\r?\n/)
  .filter(Boolean)
  .map((line) => JSON.parse(line));
assert.ok(events.some((entry) => entry.event === "constructed"));
assert.ok(events.some((entry) => entry.event === "thinking-effort-fallback" && entry.requested === "off"));
const compressed = events.find((entry) => entry.event === "thinking-rewritten");
assert.ok(compressed, "thinking-compressed recorded");
assert.equal(compressed.replaced, true);
assert.equal(compressed.effort, "low");
assert.equal(compressed.effortFallback, true);
assert.ok(events.some((entry) => entry.event === "block-replaced"));

function call(target, { method = "GET", body, headers } = {}) {
  return new Promise((resolve) => {
    const [path, query] = target.split("?");
    const handler = routes.get(path);
    assert.ok(handler, `route ${path}`);
    const listeners = { data: [], end: [], error: [] };
    const req = {
      method,
      url: query ? `${path}?${query}` : path,
      headers: headers ?? { "x-iot-thinking": "1", host: "127.0.0.1:3080" },
      socket: { remoteAddress: "127.0.0.1" },
      on(event, fn) {
        if (listeners[event]) listeners[event].push(fn);
        return req;
      },
    };
    const res = {
      statusCode: null,
      payload: null,
      writeHead(status) {
        this.statusCode = status;
        return this;
      },
      end(text) {
        this.payload = text ? JSON.parse(text) : null;
        resolve(this);
      },
    };
    Promise.resolve(handler(req, res)).catch((error) => resolve({ statusCode: 500, payload: { error: String(error) } }));
    queueMicrotask(() => {
      if (body !== undefined) for (const fn of listeners.data) fn(Buffer.from(body));
      for (const fn of listeners.end) fn();
    });
  });
}

// The route cache now knows this route needs `low`: the self-test must not try
// `off` again, and with provider/model blank it must follow the last route.
const testResponse = await call("/iot-thinking/api/test", { method: "POST", body: "{}" });
assert.equal(testResponse.statusCode, 200, JSON.stringify(testResponse.payload));
assert.equal(testResponse.payload.ok, true);
assert.equal(testResponse.payload.provider, "deepseek-official", "self-test follows the last agent-loop route");
assert.equal(testResponse.payload.model, "deepseek-flash");
assert.equal(testResponse.payload.effort, "low", "route decision reused by the self-test");
assert.ok(testResponse.payload.charsOut < testResponse.payload.charsIn);
assert.ok(testResponse.payload.text.includes("[决定]"));
assert.equal(compressorCalls.length, 3, "self-test made exactly one compressor call");

const sameLength = await call("/iot-thinking/api/test", {
  method: "POST",
  body: JSON.stringify({ text: GOOD }),
});
assert.equal(sameLength.payload.ok, false);
assert.equal(sameLength.payload.reason, "not-shorter", "charsIn == charsOut must not report green");

const modelsResponse = await call("/iot-thinking/api/models?provider=deepseek-official&model=deepseek-flash");
assert.equal(modelsResponse.statusCode, 200);
assert.deepEqual(modelsResponse.payload.providers.map((entry) => entry.id), ["deepseek-official"]);
assert.deepEqual(modelsResponse.payload.reasoning.efforts.map((entry) => entry.id), ["off", "low", "high"]);

const denied = await call("/iot-thinking/api/status", { headers: {} });
assert.equal(denied.statusCode, 403, "no guard header = refused");

const status = await call("/iot-thinking/api/status");
assert.equal(status.statusCode, 200);
assert.ok(status.payload.events.length > 0);
assert.ok(!("path" in status.payload.config), "config path stays off the wire");

// The client half is plain JS served straight from disk, so nothing else would
// catch a broken settings card. Load it through a minimal __ModuleLoader__ and
// React stand-in, then render it once.
{
  const source = readFileSync(join(pluginDir, "lib", "client.js"), "utf8");
  let definition = null;
  const context = vm.createContext({ window: { __ModuleLoader__: { load: (value) => { definition = value; } } }, console });
  vm.runInContext(source, context, { filename: "client.js" });
  assert.ok(definition, "client bundle called __ModuleLoader__.load");
  assert.equal(definition.id, "dsh-plugin-iot-thinking");
  const react = {
    createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState: (initial) => [typeof initial === "function" ? initial() : initial, () => {}],
    useEffect: () => {},
    useRef: (initial) => ({ current: initial }),
  };
  const client = definition.factory((name) => {
    assert.equal(name, "react");
    return react;
  });
  assert.equal(Array.from(client.inject).join(","), "slots");
  /** 两个入口都要注册：设置卡 + 会话头部下拉。 */
  const registered = [];
  const slots = {
    inject: (_name, factory) => factory(),
    register: (descriptor, component) => {
      registered.push({ descriptor, component });
      return () => {};
    },
  };
  client.apply({
    get: (name) => (name === "slots" ? slots : undefined),
    effect: (fn) => fn(),
  });
  const card = registered.find((entry) => entry.descriptor.id === "iot-thinking");
  assert.ok(card, "settings tab registered");
  assert.equal(card.descriptor.name, "settings.plugins.tab");
  assert.equal(typeof card.component, "function");
  const seat = registered.find((entry) => entry.descriptor.id === "iot-strategy");
  assert.ok(seat, "session header strategy seat registered");
  assert.equal(seat.descriptor.name, "conversation.session.header.actions");
  // 顶栏那一行默认**不渲染**（sessionSwitch 默认关）：拿不到状态时静默，
  // 拿到状态但开关关着时也静默。开关打开后的渲染由 selftest 的
  // 「头部下拉默认关闭」用例与卡片的渲染验证覆盖（这里没有 fetch/effect 环境）。
  assert.equal(seat.component({ sessionId: "s-1" }), null, "default off");
  assert.equal(seat.component({}), null, "no state yet");
  // 保存只提交改动过的字段：整份 PUT 会把加载时的 sessions 快照写回去。
  // 跨 realm 的对象原型不同，用 JSON 比结构。
  const asJson = (value) => JSON.stringify(value);
  assert.equal(asJson(client.changedFields({ a: 1, b: 2 }, { a: 1, b: 1 })), asJson({ b: 2 }));
  assert.equal(asJson(client.changedFields({ a: 1 }, null)), asJson({ a: 1 }), "第一次保存走全量");
  assert.equal(asJson(client.changedFields({ a: [1, 2] }, { a: [1, 2] })), "{}", "值相同就不提交");
  // 新建预设后，下拉第二级要立刻能看到它（不用重进设置页）。
  const options = client.presetOptions([{ name: "a.md", bytes: 10 }, { name: "b.txt", bytes: 20 }], true);
  assert.equal(asJson(options), asJson([
    { value: "custom", label: "贴入的文本", bytes: null },
    { value: "custom:a.md", label: "a.md", bytes: 10 },
    { value: "custom:b.txt", label: "b.txt", bytes: 20 },
  ]));
  assert.equal(asJson(client.presetOptions([], false)), "[]", "没有文件也没有贴入文本时为空");
  assert.equal(asJson(client.presetOptions(null, true)), asJson([{ value: "custom", label: "贴入的文本", bytes: null }]));
  const tree = card.component({});
  assert.ok(tree && typeof tree.type === "string", "card renders while loading");
}

console.log("smoke ok: block replaced in place, effort downgraded + cached, self-test and models routes live, client half loads");
console.log(`ledger: ${ledger}`);
