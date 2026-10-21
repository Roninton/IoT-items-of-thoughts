/**
 * Pure-logic selftest for the llm/stream transformer.
 * No dsh boot, no model call.
 *
 *   node selftest.mjs
 */
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Keep the ledger out of the operator's real $DSH_HOME: the self-test route reads
// the ledger back to name a failure, and the tests below write rows into it.
process.env.IOT_THINKING_DATA_DIR = mkdtempSync(join(tmpdir(), "iot-thinking-selftest-"));
process.env.IOT_THINKING_CONFIG_PATH = join(process.env.IOT_THINKING_DATA_DIR, "config.json");

import { isShorterReplacement, transformStream } from "./lib/stream.mjs";
import {
  buildCompressionPrompt,
  fallbackItems,
  formatNodes,
  isDegenerateDraft,
  isDegenerateItems,
  sanitizeDraft,
} from "./lib/think.mjs";
import { compressThinking, EFFORT_PROBE } from "./lib/compress.mjs";
import {
  AUTO_EFFORT,
  EFFORT_IDS,
  isUnsupportedEffortFailure,
  knownEffortPlan,
  normalizeEffort,
  pickLowEffort,
  planKey,
  rememberEffortPlan,
  resetEffortPlans,
} from "./lib/effort.mjs";
import { defaultNodeModulesRoots, rootFromEntryPath, versionedRoots } from "./lib/seams.mjs";
import { guardRequest, redactEvents, registerConfigApi } from "./lib/config-api.mjs";
import { mark } from "./lib/ledger.mjs";

let failures = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`FAIL ${name}\n     ${error.message}`);
  }
}

async function* chunks(list) {
  for (const chunk of list) yield chunk;
}
async function collect(iterable) {
  const out = [];
  for await (const chunk of iterable) out.push(chunk);
  return out;
}
const start = (index = 0) => ({ type: "block-start", index, blockType: "reasoning" });
const delta = (text, index = 0) => ({ type: "reasoning-delta", index, text });
const end = (text, index = 0) => ({ type: "block-end", index, block: { type: "reasoning", text } });
const textDelta = (text, index = 1) => ({ type: "text-delta", index, text });
const finish = () => ({ type: "finish", reason: { kind: "stop" } });

const baseConfig = { enabled: true, minChars: 1, maxChars: 24000, maxTokens: 1200 };
const baseOptions = { provider: "p", model: "m" };
const deps = (compress, overrides = {}) => ({
  shouldTransform: () => true,
  getConfig: () => ({ ...baseConfig, ...overrides }),
  compress,
  onEvent: () => {},
});

const source = () => chunks([start(), delta("abcde"), delta("fghij"), end("abcdefghij"), textDelta("hello"), finish()]);

await check("完成块被压缩替换：不再出现原始 deltas", async () => {
  const out = await collect(transformStream(source(), baseOptions, deps(async () => "· X")));
  assert.equal(out[0].type, "block-start");
  assert.deepEqual(out[1], { type: "reasoning-delta", index: 0, text: "· X" });
  assert.equal(out[2].type, "block-end");
  assert.equal(out[2].block.text, "· X");
  assert.equal(out[3].type, "text-delta");
  assert.equal(out[4].type, "finish");
  assert.ok(!out.some((chunk) => chunk.type === "reasoning-delta" && chunk.text === "abcde"));
  assert.ok(!out.some((chunk) => chunk.type === "reasoning-delta" && chunk.text === "fghij"));
});

await check("enabled=false 时原样透传", async () => {
  const input = [start(), delta("abc"), end("abc"), textDelta("hello"), finish()];
  const out = await collect(transformStream(chunks(input), baseOptions, deps(async () => "X", { enabled: false })));
  assert.deepEqual(out, input);
});

await check("非 agent-loop 请求时原样透传", async () => {
  const input = [start(), delta("abc"), end("abc"), finish()];
  const local = { ...deps(async () => "X"), shouldTransform: () => false };
  const out = await collect(transformStream(chunks(input), baseOptions, local));
  assert.deepEqual(out, input);
});

await check("低于 minChars 时保留原始 deltas + block-end", async () => {
  const input = [start(), delta("abc"), end("abc")];
  const out = await collect(transformStream(chunks(input), baseOptions, deps(async () => "X", { minChars: 100 })));
  assert.deepEqual(out, input);
});

await check("压缩器抛错时保留原文", async () => {
  const input = [start(), delta("abc"), end("abc")];
  const out = await collect(transformStream(chunks(input), baseOptions, deps(async () => { throw new Error("boom"); })));
  assert.deepEqual(out, input);
});

await check("压缩结果为空或更长的，保留原文", async () => {
  const input = [start(), delta("abcdef"), end("abcdef")];
  for (const value of ["", "   ", "a much longer output than the original"]) {
    const out = await collect(transformStream(chunks(input), baseOptions, deps(async () => value)));
    assert.deepEqual(out, input, JSON.stringify(value));
  }
});

await check("多个 reasoning 块分别压缩", async () => {
  const input = [
    start(0), delta("aaaaaaaa", 0), end("aaaaaaaa", 0),
    start(2), delta("bbbbbbbb", 2), end("bbbbbbbb", 2),
    finish(),
  ];
  const out = await collect(transformStream(chunks(input), baseOptions, deps(async (text) => `·${text[0]}`)));
  assert.equal(out.filter((chunk) => chunk.type === "reasoning-delta").length, 2);
  assert.equal(out.find((chunk) => chunk.type === "reasoning-delta" && chunk.index === 0).text, "·a");
  assert.equal(out.find((chunk) => chunk.type === "reasoning-delta" && chunk.index === 2).text, "·b");
});

await check("流在 block-end 前结束：原始 deltas 被完整 flush", async () => {
  const input = [start(), delta("abc"), delta("def")];
  const out = await collect(transformStream(chunks(input), baseOptions, deps(async () => "X")));
  assert.deepEqual(out, input);
});

await check("只有 block-end、没有 deltas：也能压缩 block.text", async () => {
  const out = await collect(transformStream(chunks([start(), end("abcdefghij"), finish()]), baseOptions, deps(async () => "· X")));
  assert.deepEqual(out[1], { type: "reasoning-delta", index: 0, text: "· X" });
  assert.equal(out[2].block.text, "· X");
});

await check("压缩器收到 block 序号（用于归档命名）", async () => {
  let seen = -1;
  await collect(transformStream(chunks([start(7), delta("abcdef", 7), end("abcdef", 7)]), baseOptions, deps(async (_text, _options, _cfg, index) => { seen = index; return "· X"; })));
  assert.equal(seen, 7);
});

await check("thinking-compressed 带 replaced：真的替换了才是 true", async () => {
  const events = [];
  const captured = (compress) => ({
    shouldTransform: () => true,
    getConfig: () => ({ ...baseConfig, minChars: 1 }),
    compress,
    onEvent: (event, payload) => events.push({ event, ...payload }),
  });
  const payload = { index: 0, charsIn: 10, charsOut: 4, profile: "full" };

  const input = [start(), delta("abcdefghij"), end("abcdefghij")];
  await collect(transformStream(chunks(input), baseOptions, captured(async () => ({ text: "· X", event: payload }))));
  const replaced = events.find((entry) => entry.event === "thinking-compressed");
  assert.equal(replaced.replaced, true);
  assert.equal(replaced.charsOut, 4);

  events.length = 0;
  await collect(transformStream(chunks(input), baseOptions, captured(async () => ({ text: "a much longer output", event: payload }))));
  const kept = events.find((entry) => entry.event === "thinking-compressed");
  assert.equal(kept.replaced, false);
  assert.equal(kept.reason, "not-shorter");
  assert.ok(events.some((entry) => entry.event === "block-kept"));
});

await check("formatNodes 映射旧标签、去 bullet、节点之间空行", () => {
  const out = formatNodes("```text\n条目：\n· 正节点：X\n- 否掉：Y\n待定：Z\n```");
  assert.equal(out, "[支持] X\n\n[反对] Y\n\n[待定] Z");
});

await check("formatNodes 保留新标签并去掉编号", () => {
  const out = formatNodes("1. [中性] A\n2. [计划] B\n3. [权衡] C");
  assert.equal(out, "[中性] A\n\n[计划] B\n\n[权衡] C");
});

await check("formatNodes 无标签行并入上一个节点", () => {
  const out = formatNodes("[支持] A\n补充一句\n\n[计划] B");
  assert.equal(out, "[支持] A 补充一句\n\n[计划] B");
});

await check("formatNodes 整段无标签时补 [中性]", () => {
  assert.equal(formatNodes("只有事实"), "[中性] 只有事实");
});

await check("formatNodes 空输出仍是空", () => {
  assert.equal(formatNodes("   \n\n"), "");
});

await check("formatNodes 合并多余 [中性]（maxNeutral=2）", () => {
  const out = formatNodes("[中性] A\n\n[中性] B\n\n[中性] C\n\n[中性] D\n\n[中性] E", { maxNodes: 8, maxNeutral: 2 });
  const nodes = out.split("\n\n");
  assert.equal(nodes.filter((node) => node.startsWith("[中性]")).length, 2);
  assert.match(nodes[1], /B/);
  assert.match(nodes[1], /E/);
});

await check("formatNodes maxNeutral=0 删除所有 [中性]", () => {
  const out = formatNodes("[中性] A\n\n[计划] B", { maxNodes: 8, maxNeutral: 0 });
  assert.equal(out, "[计划] B");
});

await check("formatNodes maxNodes 按优先级截断并保持原顺序", () => {
  const input = [
    "[中性] n",
    "[计划] p",
    "[支持] s",
    "[决定] d",
    "[反对] o",
    "[权衡] t",
    "[风险] r",
    "[待定] q",
    "[计划] p2",
  ].join("\n\n");
  const out = formatNodes(input, { maxNodes: 4, maxNeutral: 2 });
  assert.deepEqual(out.split("\n\n"), ["[决定] d", "[反对] o", "[权衡] t", "[风险] r"]);
});

await check("buildCompressionPrompt 带上新标签体系与空行要求", () => {
  const prompt = buildCompressionPrompt("原始思考");
  assert.match(prompt, /<thinking>\n原始思考\n<\/thinking>/);
  assert.match(prompt, /只压不补/);
  assert.match(prompt, /\[支持\]/);
  assert.match(prompt, /\[反对\]/);
  assert.match(prompt, /\[中性\]/);
  assert.match(prompt, /\[计划\]/);
  assert.match(prompt, /\[权衡\]/);
  assert.match(prompt, /节点之间空一行/);
  assert.match(prompt, /最多 8 条/);
  assert.match(prompt, /\[中性\] 最多 2 条/);
  assert.match(prompt, /禁止退化重复/);
  assert.match(prompt, /至少 12 个汉字/);
  const strict = buildCompressionPrompt("原始思考", { strict: true });
  assert.match(strict, /严格模式/);
});

await check("退化节点被识别（单词 / 重复 / 自我循环评论）", () => {
  const bad = [
    "[中性] 现在",
    "[中性] 好",
    "[中性] 执行",
    "[中性] 写",
    "[中性] 结束",
    "[中性] 好",
    "[中性] 现在",
    "[中性] 写",
    "[中性] 我注意到这轮思考陷入了重复，正是 IOT_INSTRUCTION 要求压掉的填充。",
  ].join("\n\n");
  assert.equal(isDegenerateItems(bad), true);
  assert.equal(isDegenerateItems(formatNodes(bad, { maxNodes: 8, maxNeutral: 2 })), true);
  const pureFiller = [
    "[中性] 好",
    "[中性] 现在",
    "[中性] 写",
    "[中性] 结束",
    "[中性] 好",
    "[中性] 现在",
    "[中性] 写",
    "[中性] 结束",
  ].join("\n\n");
  assert.equal(isDegenerateItems(pureFiller), true);
  assert.equal(isDegenerateItems(formatNodes(pureFiller, { maxNodes: 8, maxNeutral: 2 })), true);
  assert.equal(isDegenerateItems("[中性] 好"), true);
  assert.equal(isDegenerateItems("[决定] 最终答案只给时间 17:35，不再展开推导。"), false);
});

await check("sanitizeDraft 去掉标签与单词填充，保留有效内容", () => {
  const cleaned = sanitizeDraft("[中性] 插件自测 16/16 通过\n\n[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束");
  assert.match(cleaned, /插件自测 16\/16 通过/);
  assert.ok(!cleaned.includes("现在"));
});

await check("isDegenerateDraft 识别纯单词草稿", () => {
  assert.equal(isDegenerateDraft("[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束"), true);
  assert.equal(
    isDegenerateDraft("[中性] 插件自测 16/16 通过，仓库无新增污染。\n现在需要生成最终数据并整理报告。"),
    false,
  );
});

await check("fallbackItems 不再产生循环，且有计划节点", () => {
  const out = fallbackItems(
    "[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束\n[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束",
  );
  assert.match(out, /\[计划\]/);
  assert.equal(isDegenerateItems(out), false);
});

await check("压缩器退化输出会严格重试，成功后返回新节点", async () => {
  const calls = [];
  const events = [];
  const bad = [
    "[中性] 现在",
    "[中性] 好",
    "[中性] 执行",
    "[中性] 写",
    "[中性] 结束",
    "[中性] 好",
    "[中性] 现在",
    "[中性] 写",
    "[中性] 我注意到这轮思考陷入了重复，正是 IOT_INSTRUCTION 要求压掉的填充。",
  ].join("\n\n");
  const good = [
    "[中性] 插件自测 16/16 通过，仓库无新增污染。",
    "[决定] 保留现有实现，不再改动核心压缩器。",
    "[风险] 长草稿可能再次触发重复，需要质量门拦截。",
    "[计划] 先重启 web，再用真实会话验证节点质量。",
  ].join("\n\n");
  const Llm = {
    BlockAssembler: class {
      constructor() {
        this.text = "";
        this.finish = undefined;
      }
      push(chunk) {
        if (chunk.type === "text-delta") this.text += chunk.text;
        if (chunk.type === "finish") this.finish = { kind: chunk.reason?.kind ?? "stop" };
      }
      blocks() {
        return this.text ? [{ type: "text", text: this.text }] : [];
      }
    },
    createUserMessage: (input) => input,
  };
  const ctx = {
    llm: {
      stream: (options) => {
        calls.push(options);
        const prompt = options.messages[0].content[0].text;
        const text = prompt.includes("严格模式") ? good : bad;
        return chunks([
          { type: "text-delta", index: 0, text },
          { type: "finish", reason: { kind: "stop" } },
        ]);
      },
    },
  };
  const result = await compressThinking({
    Llm,
    ctx,
    text: "插件自测 16/16 通过，仓库无新增污染。现在需要生成最终数据并整理报告，先重新跑一次 quality 统计，再固定一份快照；报告里要说明样本仍在增长，并注明快照时间。另外要检查 git status 是否与之前一致，确认没有改动 repo 文件；如果输出目录被 gitignore，就把证据文件放在 out/iot-self-eval 下。",
    options: { provider: "p", model: "m", sessionId: "s" },
    cfg: { ...baseConfig, maxNodes: 8, maxNeutral: 2 },
    index: 0,
    archive: () => null,
    mark: (event, payload) => events.push({ event, ...payload }),
  });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].temperature, 0.2);
  assert.equal(calls[1].temperature, 0.1);
  assert.match(calls[1].messages[0].content[0].text, /严格模式/);
  assert.match(result.text, /\[决定\]/);
  assert.equal(result.event.fallback, undefined);
  assert.ok(events.some((entry) => entry.event === "thinking-degenerate" && entry.action === "retry"));
});

await check("退化草稿不调用模型，直接走确定性兜底", async () => {
  let calls = 0;
  const events = [];
  const Llm = { BlockAssembler: class {}, createUserMessage: (input) => input };
  const ctx = {
    llm: {
      stream: () => {
        calls += 1;
        return chunks([]);
      },
    },
  };
  const result = await compressThinking({
    Llm,
    ctx,
    text: "[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束\n[中性] 好\n[中性] 现在\n[中性] 写\n[中性] 结束",
    options: { provider: "p", model: "m", sessionId: "s" },
    cfg: { ...baseConfig, maxNodes: 8, maxNeutral: 2 },
    index: 0,
    archive: () => null,
    mark: (event, payload) => events.push({ event, ...payload }),
  });
  assert.equal(calls, 0);
  assert.match(result.text, /\[计划\]/);
  // The compressed event is written by stream.mjs, which alone knows whether the
  // block was replaced; compressThinking hands over the payload.
  assert.equal(result.event.fallback, true);
});

await check("非退化但 sanitize 后为空：不调模型，也不硬造节点", async () => {
  // Two filler lines are below the filler threshold (>=3), and there is no
  // substantive line to make a loop, so the draft gate lets it through. After
  // sanitization there is nothing left to compress — that must not become a
  // model call or a fabricated node.
  const draft = "[中性]\n好\n现在";
  assert.equal(isDegenerateDraft(draft), false);
  assert.equal(sanitizeDraft(draft), "");
  let calls = 0;
  const events = [];
  const Llm = { BlockAssembler: class {}, createUserMessage: (input) => input };
  const ctx = {
    llm: {
      stream: () => {
        calls += 1;
        return chunks([]);
      },
    },
  };
  const result = await compressThinking({
    Llm,
    ctx,
    text: draft,
    options: { provider: "p", model: "m", sessionId: "s" },
    cfg: { ...baseConfig, maxNodes: 8, maxNeutral: 2 },
    index: 0,
    archive: () => null,
    mark: (event, payload) => events.push({ event, ...payload }),
  });
  assert.equal(calls, 0);
  assert.equal(result, null);
  assert.ok(events.some((entry) => entry.event === "thinking-degenerate" && entry.stage === "input"));
});

await check("compact 档位：3 标签提示词 + 8 标签归并", () => {
  const prompt = buildCompressionPrompt("原始思考", { profile: "compact" });
  assert.match(prompt, /只输出三类节点/);
  assert.match(prompt, /目标 3~4 条/);
  assert.match(prompt, /\[支持\]/);
  assert.match(prompt, /\[反对\]/);
  assert.match(prompt, /\[待定\]/);
  const out = formatNodes("[决定] D\n\n[计划] P\n\n[中性] N\n\n[权衡] T\n\n[风险] R", {
    profile: "compact",
    maxNodes: 8,
    maxNeutral: 2,
  });
  assert.deepEqual(
    out.split("\n\n").map((node) => node.slice(0, 4)),
    ["[支持]", "[待定]", "[支持]", "[支持]", "[反对]"],
  );
  assert.ok(!/\[(决定|计划|中性|权衡|风险)\]/.test(out));
});

await check("compact 档位：maxNodes 硬上限 5", () => {
  const input = ["[支持] A", "[支持] B", "[支持] C", "[支持] D", "[支持] E", "[支持] F", "[支持] G"].join("\n\n");
  const out = formatNodes(input, { profile: "compact", maxNodes: 8, maxNeutral: 2 });
  assert.equal(out.split("\n\n").length, 5);
});

await check("balanced 档位：5 标签提示词", () => {
  const prompt = buildCompressionPrompt("原始思考", { profile: "balanced" });
  assert.match(prompt, /只输出五类节点/);
  assert.match(prompt, /\[决定\]/);
  assert.match(prompt, /\[计划\]/);
  assert.match(prompt, /目标 4~6 条，最多 6 条/);
});

await check("seam 锚点：版本化安装的入口能上溯出 node_modules", () => {
  assert.equal(
    rootFromEntryPath("D:\\dsh-home\\dsh-0.2.1-rc.2\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js"),
    "D:\\dsh-home\\dsh-0.2.1-rc.2\\node_modules"
  );
  assert.equal(
    rootFromEntryPath("/opt/dsh-home/dsh-0.2.1-rc.2/node_modules/@deepseek-ai/dsh/lib/bin.js"),
    "/opt/dsh-home/dsh-0.2.1-rc.2/node_modules"
  );
});

await check("seam 锚点：pnpm 嵌套时取最里层 node_modules", () => {
  assert.equal(
    rootFromEntryPath(
      "/home/u/.dsh/profiles/node_modules/.pnpm/@deepseek-ai+dsh@0.2.1/node_modules/@deepseek-ai/dsh/lib/bin.js"
    ),
    "/home/u/.dsh/profiles/node_modules/.pnpm/@deepseek-ai+dsh@0.2.1/node_modules"
  );
});

await check("seam 锚点：入口不在 node_modules 下时返回 null", () => {
  assert.equal(rootFromEntryPath("D:\\_playground\\selftest.mjs"), null);
  assert.equal(rootFromEntryPath(""), null);
  assert.equal(rootFromEntryPath(undefined), null);
});

await check("seam 锚点：版本化安装按 mtime 降序、忽略非 dsh- 目录", () => {
  const fakeFs = {
    readdirSync: () => [
      { name: "dsh-0.2.0-rc.2", isDirectory: () => true },
      { name: "dsh-0.2.1-rc.2", isDirectory: () => true },
      { name: "profiles", isDirectory: () => true },
      { name: "dsh-broken-mtime", isDirectory: () => true },
    ],
    statSync: (p) => {
      if (p.endsWith("dsh-0.2.1-rc.2")) return { mtimeMs: 200 };
      if (p.endsWith("dsh-0.2.0-rc.2")) return { mtimeMs: 100 };
      throw new Error("ENOENT");
    },
  };
  const roots = versionedRoots("/home/u/.dsh", fakeFs);
  assert.equal(roots.length, 3); // 三个 dsh- 目录都算候选，取不到 mtime 的排最后
  assert.ok(roots[0].includes("dsh-0.2.1-rc.2"));
  assert.ok(roots[1].includes("dsh-0.2.0-rc.2"));
  assert.ok(roots[2].includes("dsh-broken-mtime"));
  assert.deepEqual(versionedRoots("", fakeFs), []);
});

await check("seam 锚点：argv[1] 推导排在 profiles/node_modules 之前", () => {
  // 多版本共存：profiles/node_modules 里可能 hoist 着另一个 dsh 版本的 seam。
  // 先命中它 = 插件与宿主拿到两份不同实例 —— 必须让"正在跑的入口"先赢。
  const roots = defaultNodeModulesRoots({
    entry: "D:\\dsh-home\\dsh-0.2.1-rc.2\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js",
    home: "D:\\dsh-home",
    fs: { readdirSync: () => [], statSync: () => ({ mtimeMs: 0 }) },
  });
  assert.equal(roots[0], "D:\\dsh-home\\dsh-0.2.1-rc.2\\node_modules");
  const profilesAt = roots.indexOf("D:\\dsh-home\\profiles\\node_modules");
  assert.ok(profilesAt > 0, "profiles 根仍在候选里，只是排在后面");
  assert.ok(!roots.slice(0, profilesAt).some((r) => r.endsWith("profiles\\node_modules")));
});

await check("seam 锚点：入口落空时才用 profiles 根", () => {
  const roots = defaultNodeModulesRoots({
    entry: "D:\\tmp\\not-under-node-modules\\selftest.mjs",
    home: "D:\\dsh-home",
    fs: { readdirSync: () => [], statSync: () => ({ mtimeMs: 0 }) },
  });
  assert.equal(roots[0], "D:\\dsh-home\\profiles\\node_modules");
});

await check("config API 门禁：缺自定义头一律 403", () => {
  const loopback = { socket: { remoteAddress: "127.0.0.1" }, headers: {} };
  assert.equal(guardRequest(loopback).ok, false);
  assert.equal(guardRequest(loopback).status, 403);
  const withHeader = { socket: { remoteAddress: "127.0.0.1" }, headers: { "x-iot-thinking": "1" } };
  assert.equal(guardRequest(withHeader).ok, true);
});

await check("config API 门禁：跨站 / 伪装 Origin / 非 loopback 全拒", () => {
  const base = (extra, remote = "127.0.0.1") => ({ socket: { remoteAddress: remote }, headers: { "x-iot-thinking": "1", host: "127.0.0.1:3080", ...extra } });
  assert.equal(guardRequest(base({ "sec-fetch-site": "same-origin" })).ok, true);
  assert.equal(guardRequest(base({ "sec-fetch-site": "cross-site" })).ok, false);
  assert.equal(guardRequest(base({ "sec-fetch-site": "same-site" })).ok, false);
  assert.equal(guardRequest(base({ origin: "https://127.0.0.1:3080" })).ok, true);
  // A rebinding/cross-site page carries its own origin, which never matches the host.
  assert.equal(guardRequest(base({ origin: "https://evil.example" })).ok, false);
  assert.equal(guardRequest(base({ origin: "null" })).ok, false);
  // A LAN peer cannot replay observed headers; loopback is required by default.
  assert.equal(guardRequest(base({}, "192.168.1.9")).ok, false);
  // An absent socket proves nothing.
  assert.equal(guardRequest({ headers: { "x-iot-thinking": "1" } }).ok, false);
});

await check("status 事件脱敏：去掉 sessionId / archived", () => {
  const rows = redactEvents([
    { event: "block-replaced", t: "2026-10-19T00:00:00Z", sessionId: "s-1", archived: "D:\\x\\block0.txt", charsIn: 10, charsOut: 3, ms: 5 },
    { event: "thinking-compressed", sessionId: "s-2", provider: "p", model: "m", profile: "full", fallback: true },
  ]);
  assert.equal(rows[0].sessionId, undefined);
  assert.equal(rows[0].archived, undefined);
  assert.equal(rows[0].charsIn, 10);
  assert.equal(rows[1].sessionId, undefined);
  assert.equal(rows[1].provider, undefined);
  assert.equal(rows[1].model, undefined);
  assert.equal(rows[1].profile, "full");
  assert.equal(rows[1].fallback, true);
});

// ---------------------------------------------------------------------------
// issue #3 — effort policy, degenerate-draft false positives, visible failures
// ---------------------------------------------------------------------------

await check("normalizeEffort：null/空/auto 归为 auto，非法形态返回 null，适配器自定义 id 保留", () => {
  assert.equal(normalizeEffort(undefined), null);
  assert.equal(normalizeEffort(null), AUTO_EFFORT);
  assert.equal(normalizeEffort(""), AUTO_EFFORT);
  assert.equal(normalizeEffort(" AUTO "), AUTO_EFFORT);
  assert.equal(normalizeEffort("HIGH"), "high");
  assert.equal(normalizeEffort("xhigh"), "xhigh");
  for (const id of EFFORT_IDS) assert.equal(normalizeEffort(id), id, id);
  // ReasoningEffortId is opaque; an adapter may advertise a level we do not list.
  assert.equal(normalizeEffort("turbo"), "turbo");
  assert.equal(normalizeEffort("-nope"), null);
  assert.equal(normalizeEffort("has space"), null);
  assert.equal(normalizeEffort("a".repeat(40)), null);
  assert.equal(normalizeEffort(3), null);
  assert.equal(AUTO_EFFORT, "auto");
});

await check("pickLowEffort：只认 minimal/low，忽略 off 与未知值", () => {
  assert.equal(pickLowEffort(["low", "medium", "high"]), "low");
  assert.equal(pickLowEffort(["medium", "minimal"]), "minimal");
  assert.equal(pickLowEffort(["off", "medium", "max"]), null);
  assert.equal(pickLowEffort(undefined), null);
  assert.equal(pickLowEffort([undefined, 42, "high"]), null);
});

await check("isUnsupportedEffortFailure：认 code，也认老宿主只有文案的情况", () => {
  assert.equal(isUnsupportedEffortFailure({ error: "x", code: "UNSUPPORTED_REASONING_EFFORT" }), true);
  assert.equal(
    isUnsupportedEffortFailure({ error: 'provider "p" model "m" does not support reasoning effort "off"' }),
    true,
  );
  assert.equal(isUnsupportedEffortFailure({ error: "quota", code: "QUOTA" }), false);
  assert.equal(isUnsupportedEffortFailure({ output: "ok" }), false);
  assert.equal(isUnsupportedEffortFailure(null), false);
});

await check("路由决策缓存：按 route+mode 记住，可整体重置", () => {
  resetEffortPlans();
  const key = planKey("p", "m", AUTO_EFFORT);
  assert.equal(knownEffortPlan(key), undefined);
  rememberEffortPlan(key, "low");
  assert.equal(knownEffortPlan(key), "low");
  assert.equal(knownEffortPlan(planKey("p", "m", "high")), undefined);
  rememberEffortPlan(key, null);
  assert.equal(knownEffortPlan(key), null);
  resetEffortPlans();
  assert.equal(knownEffortPlan(key), undefined);
});

// Shared fixtures for the compressor tests below.
const GOOD = [
  "[中性] 插件自测 16/16 通过，仓库无新增污染。",
  "[决定] 保留现有实现，不再改动核心压缩器。",
  "[风险] 长草稿可能再次触发重复，需要质量门拦截。",
  "[计划] 先重启 web，再用真实会话验证节点质量。",
].join("\n\n");
const DRAFT = "插件自测 16/16 通过，仓库无新增污染。现在需要生成最终数据并整理报告，先重新跑一次 quality 统计，再固定一份快照；报告里要说明样本仍在增长，并注明快照时间。另外要检查 git status 是否与之前一致，确认没有改动 repo 文件；如果输出目录被 gitignore，就把证据文件放在 out/iot-self-eval 下。";

const fakeLlm = () => ({
  BlockAssembler: class {
    constructor() {
      this.text = "";
      this.finish = undefined;
    }
    push(chunk) {
      if (chunk.type === "text-delta") this.text += chunk.text;
      if (chunk.type === "finish") this.finish = { kind: chunk.reason?.kind ?? "stop", failure: chunk.reason?.failure };
    }
    blocks() {
      return this.text ? [{ type: "text", text: this.text }] : [];
    }
  },
  createUserMessage: (input) => input,
});

const compressWith = (ctx, { cfg = {}, events = [], text = DRAFT } = {}) =>
  compressThinking({
    Llm: fakeLlm(),
    ctx,
    text,
    options: { provider: "p", model: "m", sessionId: "s" },
    cfg: { maxChars: 24000, maxTokens: 1200, maxNodes: 8, maxNeutral: 2, ...cfg },
    index: 0,
    archive: () => null,
    mark: (event, payload) => events.push({ event, ...payload }),
  });

const effortRejectingCtx = (calls, { efforts } = {}) => ({
  llm: {
    stream: (options) => {
      calls.push({ ...options });
      if (options.reasoningEffort === "off") {
        return chunks([
          {
            type: "finish",
            reason: {
              kind: "error",
              failure: {
                message: 'provider "p" model "m" does not support reasoning effort "off"',
                code: "UNSUPPORTED_REASONING_EFFORT",
              },
            },
          },
        ]);
      }
      return chunks([{ type: "text-delta", index: 0, text: GOOD }, { type: "finish", reason: { kind: "stop" } }]);
    },
    ...(efforts === undefined
      ? {}
      : { resolveModelInfo: async () => ({ reasoning: { efforts: efforts.map((id) => ({ id })) } }) }),
  },
});

await check("auto 档位：off 被拒后改用该路由公开的低档位，并记账", async () => {
  resetEffortPlans();
  const calls = [];
  const events = [];
  const result = await compressWith(effortRejectingCtx(calls, { efforts: ["medium", "low", "high"] }), { events });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].reasoningEffort, "off");
  assert.equal(calls[1].reasoningEffort, "low");
  assert.ok(events.some((entry) => entry.event === "thinking-effort-fallback" && entry.requested === "off"));
  assert.equal(result.event.effort, "low");
  assert.equal(result.event.effortFallback, true);
  assert.match(result.text, /\[决定\]/);
});

await check("auto 档位：路由没有低档位时不发该字段（每个路由都接受）", async () => {
  resetEffortPlans();
  const calls = [];
  const events = [];
  const result = await compressWith(effortRejectingCtx(calls, { efforts: ["medium", "high"] }), { events });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].reasoningEffort, "off");
  assert.equal("reasoningEffort" in calls[1], false);
  assert.equal(result.event.effort, null);
  assert.equal(result.event.effortFallback, true);
});

await check("auto 档位：宿主查不到档位（无 resolveModelInfo）时也能降级", async () => {
  resetEffortPlans();
  const calls = [];
  const ctx = effortRejectingCtx(calls);
  assert.equal(typeof ctx.llm.resolveModelInfo, "undefined");
  await compressWith(ctx);
  assert.equal(calls.length, 2);
  assert.equal("reasoningEffort" in calls[1], false);
});

await check("auto 档位：路由目录乐观报低档位但实际仍拒 → 再退到不发该字段并记住", async () => {
  resetEffortPlans();
  const calls = [];
  const ctx = {
    llm: {
      stream: (options) => {
        calls.push({ ...options });
        if (options.reasoningEffort !== undefined) {
          return chunks([
            {
              type: "finish",
              reason: {
                kind: "error",
                failure: {
                  message: `does not support reasoning effort "${options.reasoningEffort}"`,
                  code: "UNSUPPORTED_REASONING_EFFORT",
                },
              },
            },
          ]);
        }
        return chunks([{ type: "text-delta", index: 0, text: GOOD }, { type: "finish", reason: { kind: "stop" } }]);
      },
      resolveModelInfo: async () => ({ reasoning: { efforts: [{ id: "low" }] } }),
    },
  };
  const key = planKey("p", "m", AUTO_EFFORT);
  const result = await compressWith(ctx);
  assert.equal(calls.length, 3, "off, advertised low, then no field");
  assert.equal(calls[0].reasoningEffort, "off");
  assert.equal(calls[1].reasoningEffort, "low");
  assert.equal("reasoningEffort" in calls[2], false);
  assert.equal(knownEffortPlan(key), null, "route pinned to omit, so later blocks do not retry");
  assert.equal(result.event.effort, null);
  assert.equal(result.event.effortFallback, true);

  const again = await compressWith(ctx);
  assert.equal(calls.length, 4, "next block calls once");
  assert.equal("reasoningEffort" in calls[3], false);
  assert.ok(again.text.includes("[决定]"));
});

await check("auto 档位：目录查询超时也降级为不发该字段，不卡住响应", async () => {
  resetEffortPlans();
  const calls = [];
  const events = [];
  const ctx = {
    llm: {
      stream: (options) => {
        calls.push({ ...options });
        if (options.reasoningEffort === "off") {
          return chunks([
            {
              type: "finish",
              reason: {
                kind: "error",
                failure: { message: 'does not support reasoning effort "off"', code: "UNSUPPORTED_REASONING_EFFORT" },
              },
            },
          ]);
        }
        return chunks([{ type: "text-delta", index: 0, text: GOOD }, { type: "finish", reason: { kind: "stop" } }]);
      },
      // Slow, not never-settling: the plugin's timeout timer is unref'd, so a
      // promise that never resolves would let the selftest process exit instead
      // of exercising the timeout path.
      resolveModelInfo: () => new Promise((resolve) => setTimeout(() => resolve({ reasoning: { efforts: [{ id: "low" }] } }), 400)),
    },
  };
  const original = EFFORT_PROBE.timeoutMs;
  EFFORT_PROBE.timeoutMs = 25;
  try {
    const started = Date.now();
    await compressWith(ctx, { events });
    assert.ok(Date.now() - started < 300, `probe timeout bounds the wait (${Date.now() - started}ms)`);
  } finally {
    EFFORT_PROBE.timeoutMs = original;
  }
  assert.equal(calls.length, 2);
  assert.equal("reasoningEffort" in calls[1], false, "timed-out probe means: omit the field");
  assert.ok(events.some((entry) => entry.event === "thinking-effort-probe-failed" && /timed out/.test(entry.message)));
  // A probe that failed answered nothing, so it must not be cached: the next
  // block has to start from `off` again instead of losing the fast path forever.
  assert.equal(knownEffortPlan(planKey("p", "m", AUTO_EFFORT)), undefined);
});

await check("路由决策被记住：同一路由第二块不再白试一次 off", async () => {
  resetEffortPlans();
  const calls = [];
  const ctx = effortRejectingCtx(calls, { efforts: ["low"] });
  await compressWith(ctx, { text: `${DRAFT}多补一句，让第二块也超过最小长度阈值。` });
  await compressWith(ctx, { text: `${DRAFT}第二块也要压，且不应再重复失败一次。` });
  assert.equal(calls.length, 3, "第一次 2 次（off + low），第二次 1 次（直接 low）");
  assert.equal(calls[2].reasoningEffort, "low");
});

await check("显式档位：支持时原样发送，不探测、不降级", async () => {
  resetEffortPlans();
  const calls = [];
  const ctx = effortRejectingCtx(calls, { efforts: ["high"] });
  const result = await compressWith(ctx, { cfg: { reasoningEffort: "high" } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].reasoningEffort, "high");
  assert.equal(result.event.effort, "high");
  assert.equal(result.event.effortFallback, undefined);
});

await check("显式档位被拒：降级为不发该字段，不擅自换档", async () => {
  resetEffortPlans();
  const calls = [];
  const events = [];
  const ctx = {
    llm: {
      stream: (options) => {
        calls.push({ ...options });
        if (options.reasoningEffort === "max") {
          return chunks([
            { type: "finish", reason: { kind: "error", failure: { message: "does not support reasoning effort \"max\"", code: "UNSUPPORTED_REASONING_EFFORT" } } },
          ]);
        }
        return chunks([{ type: "text-delta", index: 0, text: GOOD }, { type: "finish", reason: { kind: "stop" } }]);
      },
      resolveModelInfo: async () => ({ reasoning: { efforts: [{ id: "off" }, { id: "low" }] } }),
    },
  };
  const result = await compressWith(ctx, { cfg: { reasoningEffort: "max" }, events });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].reasoningEffort, "max");
  assert.equal("reasoningEffort" in calls[1], false);
  assert.equal(result.event.effort, null);
  assert.equal(result.event.effortFallback, true);
});

await check("非档位类失败仍记 thinking-failed，并带上宿主的 code", async () => {
  resetEffortPlans();
  const events = [];
  const ctx = {
    llm: {
      stream: () =>
        chunks([{ type: "finish", reason: { kind: "error", failure: { message: "quota exhausted", code: "QUOTA" } } }]),
    },
  };
  const result = await compressWith(ctx, { events });
  assert.equal(result, null);
  const failed = events.find((entry) => entry.event === "thinking-failed");
  assert.equal(failed.code, "QUOTA");
  assert.equal(failed.message, "quota exhausted");
  assert.equal(failed.stage, "first");
});

await check("中止（Esc/超时）不是失败：不重试、不写退化、原块保留", async () => {
  resetEffortPlans();
  const events = [];
  let calls = 0;
  const ctx = {
    llm: {
      stream: () => {
        calls += 1;
        return chunks([{ type: "finish", reason: { kind: "aborted", failure: { message: "aborted by user", code: "ABORTED" } } }]);
      },
    },
  };
  const result = await compressWith(ctx, { events });
  assert.equal(result, null, "an aborted call must never yield a replacement");
  assert.equal(calls, 1, "no retry after an abort");
  assert.ok(!events.some((entry) => entry.event === "thinking-degenerate"), "abort is not degeneration");
  assert.ok(!events.some((entry) => entry.event === "thinking-failed"), "a user cancel is not a plugin failure");
  const skipped = events.find((entry) => entry.event === "thinking-skipped");
  assert.equal(skipped.reason, "aborted");
  assert.equal(skipped.stage, "first");
});

await check("严格重试也报错：保留原文，不拿兜底节点顶替", async () => {
  resetEffortPlans();
  const events = [];
  let calls = 0;
  const ctx = {
    llm: {
      stream: () => {
        calls += 1;
        if (calls === 1) {
          // Degenerate-but-successful first answer, to reach the strict retry.
          return chunks([{ type: "text-delta", index: 0, text: "[中性] 好\n\n[中性] 现在" }, { type: "finish", reason: { kind: "stop" } }]);
        }
        return chunks([{ type: "finish", reason: { kind: "error", failure: { message: "upstream 503", code: "PROVIDER" } } }]);
      },
    },
  };
  const result = await compressWith(ctx, { events });
  assert.equal(calls, 2);
  assert.equal(result, null, "a failed retry keeps the original block");
  assert.ok(events.some((entry) => entry.event === "thinking-failed" && entry.stage === "retry" && entry.code === "PROVIDER"));
  assert.ok(!events.some((entry) => entry.event === "thinking-degenerate" && entry.action === "fallback"));
});

await check("不发档位也被拒（适配器默认档位非法）：命名该原因，且不缓存死路", async () => {
  resetEffortPlans();
  const events = [];
  const calls = [];
  const ctx = {
    llm: {
      stream: (options) => {
        calls.push("reasoningEffort" in options ? options.reasoningEffort : null);
        // Every level, including "no field at all", is refused: the adapter's
        // configured default effort is not in its own advertised list.
        return chunks([
          {
            type: "finish",
            reason: {
              kind: "error",
              failure: { message: "adapter default effort is not advertised", code: "UNSUPPORTED_REASONING_EFFORT" },
            },
          },
        ]);
      },
      resolveModelInfo: async () => ({ reasoning: { efforts: [{ id: "medium" }] } }),
    },
  };
  const result = await compressWith(ctx, { events });
  assert.equal(result, null);
  assert.deepEqual(calls, ["off", null], "off, then field-less; nothing else can help");
  const reported = events.find((entry) => entry.event === "thinking-effort-unsupported");
  assert.equal(reported.requested, null);
  assert.equal(reported.reason, "adapter-default-unsupported");
  // Not cached: a misconfigured adapter can be fixed, and re-trying costs two
  // local round-trips that never reach the provider.
  assert.equal(knownEffortPlan(planKey("p", "m", AUTO_EFFORT)), undefined);
});

await check("退化判定：写代码的正常草稿不再被围栏/短行重复误判", () => {
  const lines = [];
  // issue #3 的原样形状：一段真在写代码的草稿，```python 围栏出现 13 次，
  // `return out` / `)` / `}` 这类短行满天飞，长句里只有 7 条重复。
  const short = ["```python", "```", "}", ")", "    return out", "    pass"];
  for (let i = 0; i < 13; i += 1) {
    lines.push(...short);
    lines.push(`def handler_${i}(request, context):`);
    lines.push(`    payload_${i} = request.json() or {}`);
    lines.push(`    # 第 ${i} 段要确认空 body 与缺字段这两种情况分别返回什么`);
  }
  for (let i = 0; i < 40; i += 1) {
    lines.push(`补充第 ${i} 点：还要确认第 ${i} 条路径上的错误处理是否把异常吞掉了，吞掉就要补日志。`);
  }
  // 报告里「只有 7 条真正重复的长句」：数量够 5，但占比远低于阈值，且同一句没到主导线。
  const repeated = "这里要确认 request.json() 在空 body 上是否抛异常，抛了要兜住。";
  for (let i = 0; i < 7; i += 1) lines.push(repeated);
  const draft = lines.join("\n");
  assert.ok(draft.length > 4000, `fixture 太短：${draft.length}`);
  assert.equal(isDegenerateDraft(draft), false);
});

await check("退化判定：整句循环 / 主导句重复 / 纯单词循环仍识别", () => {
  const loopLine = "现在需要再确认一下这个参数的单位和取值范围，然后继续下一步。";
  assert.equal(isDegenerateDraft(Array.from({ length: 30 }, () => loopLine).join("\n")), true);
  // 一半正常推理 + 一半同一句，占比过线。
  const mixed = [
    ...Array.from({ length: 10 }, (_, i) => `第 ${i} 条独立推理：这条路径要单独核对编号 ${i} 的边界条件与返回值。`),
    ...Array.from({ length: 10 }, () => loopLine),
  ].join("\n");
  assert.equal(isDegenerateDraft(mixed), true);
  // 短句循环：没有实质行，靠 filler 判据。
  assert.equal(isDegenerateDraft(Array.from({ length: 12 }, () => "现在").join("\n")), true);
  assert.equal(isDegenerateDraft(Array.from({ length: 12 }, () => "[中性] 好").join("\n")), true);
  assert.equal(isDegenerateDraft(""), true);
  assert.equal(isDegenerateDraft("[决定] 最终答案只给时间 17:35，不再展开推导。"), false);
});

await check("替换闸门：只有严格变短（trim 后）才允许替换", () => {
  assert.equal(isShorterReplacement("abcdef", "abc"), true);
  assert.equal(isShorterReplacement("  abcdef  ", "  abc  "), true);
  assert.equal(isShorterReplacement("abcdef", "abcdef"), false);
  assert.equal(isShorterReplacement("abcdef", "  abcdef  "), false);
  assert.equal(isShorterReplacement("abcdef", "   "), false);
  assert.equal(isShorterReplacement("abcdef", null), false);
  assert.equal(isShorterReplacement("abcdef", { text: "abc" }), false);
});

await check("status 脱敏包含失败原因：message 单行截断，仍不外泄 sessionId/provider/model", () => {
  const rows = redactEvents([
    {
      event: "thinking-failed",
      t: "2026-10-20T00:00:00Z",
      sessionId: "s-1",
      provider: "p",
      model: "m",
      stage: "first",
      code: "UNSUPPORTED_REASONING_EFFORT",
      effort: null,
      message: `第一行\n第二行 ${"x".repeat(400)}`,
    },
  ]);
  assert.equal(rows[0].stage, "first");
  assert.equal(rows[0].code, "UNSUPPORTED_REASONING_EFFORT");
  assert.equal(rows[0].effort, null);
  assert.equal(rows[0].message.length, 240);
  assert.ok(!rows[0].message.includes("\n"));
  assert.ok(rows[0].message.startsWith("第一行 第二行"));
  assert.equal(rows[0].sessionId, undefined);
  assert.equal(rows[0].provider, undefined);
  assert.equal(rows[0].model, undefined);
});

// A tiny HTTP double: register the plugin's routes, then call them directly.
function fakeWebServer() {
  const routes = new Map();
  const loopback = { remoteAddress: "127.0.0.1" };
  const defaultHeaders = { "x-iot-thinking": "1", host: "127.0.0.1:3080" };
  return {
    routes,
    register({ kind, path, handler }) {
      assert.equal(kind, "exact");
      routes.set(path, handler);
      return () => routes.delete(path);
    },
    call(path, { method = "GET", url = path, headers = defaultHeaders, body } = {}) {
      const handler = routes.get(path);
      assert.ok(handler, `route ${path} not registered`);
      return new Promise((resolve) => {
        const listeners = { data: [], end: [], error: [] };
        const req = {
          method,
          url,
          headers,
          socket: loopback,
          on(event, fn) {
            if (listeners[event]) listeners[event].push(fn);
            return req;
          },
        };
        const res = {
          statusCode: null,
          headers: null,
          payload: null,
          writeHead(status, responseHeaders) {
            this.statusCode = status;
            this.headers = responseHeaders;
            return this;
          },
          end(text) {
            this.payload = text === undefined || text === "" ? null : JSON.parse(text);
            resolve(this);
          },
        };
        Promise.resolve(handler(req, res)).catch((error) =>
          resolve({ statusCode: 500, payload: { error: String(error?.message ?? error) } }),
        );
        // The handlers above attach their listeners synchronously; deliver the
        // body afterwards so readBody() sees both events.
        queueMicrotask(() => {
          if (body !== undefined) for (const fn of listeners.data) fn(Buffer.from(body));
          for (const fn of listeners.end) fn();
        });
      });
    },
  };
}

const testCtx = (ws) => ({
  get: (name) => (name === "webServer" ? ws : undefined),
  // Collect disposers instead of running them: the real host calls them on
  // teardown, and running them here would unregister the routes being tested.
  effect: (fn) => {
    const dispose = fn();
    if (typeof dispose === "function") testCtx.disposers.push(dispose);
  },
  inject: () => {},
  logger: { warn: () => {} },
  llm: {
    listProviders: () => [
      { id: "deepseek-official", name: "DeepSeek" },
      { id: "tyds", name: "tyds" },
    ],
    listModels: async (provider) =>
      provider === "deepseek-official"
        ? [{ provider, id: "deepseek-flash", name: "DeepSeek Flash" }]
        : [{ provider, id: "gapi-gemini-3.8-flash", name: "Gemini Flash" }],
    resolveModelInfo: async (provider, model) => {
      if (provider === "deepseek-official") {
        return { provider, id: model, reasoning: { efforts: [{ id: "off" }, { id: "low" }, { id: "high" }, { id: "max" }], defaultEffort: "high" } };
      }
      return { provider, id: model, reasoning: { efforts: [{ id: "minimal" }, { id: "low" }, { id: "medium" }] } };
    },
  },
});
testCtx.disposers = [];

const fakeStore = (config = {}) => {
  const state = {
    enabled: true,
    profile: "full",
    provider: null,
    model: null,
    reasoningEffort: "auto",
    archive: true,
    minChars: 400,
    maxChars: 24000,
    ...config,
  };
  return {
    state,
    load: () => ({ ...state }),
    save: (next) => Object.assign(state, next),
  };
};

await check("models 路由：provider 目录 + 该路由真实档位，全部经过门禁", async () => {
  const ws = fakeWebServer();
  registerConfigApi(testCtx(ws), fakeStore(), {});
  assert.ok(ws.routes.has("/iot-thinking/api/models"));
  assert.ok(ws.routes.has("/iot-thinking/api/test"));

  const denied = await ws.call("/iot-thinking/api/models", { headers: {} });
  assert.equal(denied.statusCode, 403);

  const providers = await ws.call("/iot-thinking/api/models", { url: "/iot-thinking/api/models" });
  assert.equal(providers.statusCode, 200);
  assert.deepEqual(providers.payload.providers.map((entry) => entry.id), ["deepseek-official", "tyds"]);
  assert.deepEqual(providers.payload.models, []);

  const route = await ws.call("/iot-thinking/api/models", {
    url: "/iot-thinking/api/models?provider=tyds&model=gapi-gemini-3.8-flash",
  });
  assert.deepEqual(route.payload.models.map((entry) => entry.id), ["gapi-gemini-3.8-flash"]);
  assert.deepEqual(route.payload.reasoning.efforts.map((entry) => entry.id), ["minimal", "low", "medium"]);
  assert.equal(route.payload.reasoning.defaultEffort, undefined);
});

await check("models 路由：目录查询超时/报错只降级成 note，不 500", async () => {
  const ws = fakeWebServer();
  const ctx = testCtx(ws);
  ctx.llm.listModels = async () => {
    throw new Error("catalog exploded");
  };
  ctx.llm.resolveModelInfo = async () => {
    throw new Error("no metadata");
  };
  registerConfigApi(ctx, fakeStore(), {});
  const response = await ws.call("/iot-thinking/api/models", {
    url: "/iot-thinking/api/models?provider=tyds&model=x",
  });
  assert.equal(response.statusCode, 200);
  assert.equal(response.payload.catalogError, "catalog exploded");
  assert.equal(response.payload.reasoningError, "no metadata");
  assert.equal(response.payload.reasoning, null);
});

const LONG_DRAFT = Array.from(
  { length: 8 },
  (_, i) => `第 ${i + 1} 段推理：这一版要先确认请求里的 reasoningEffort 到底该发什么，再决定退化判定的阈值怎么取，最后把两条裁决写进文档并跑一遍自测。`,
).join("\n");

await check("test 路由：没有可用路由时 409 并说清怎么补", async () => {
  const ws = fakeWebServer();
  registerConfigApi(testCtx(ws), fakeStore(), { compress: async () => "x", getRoute: () => ({ provider: null, model: null }) });
  const response = await ws.call("/iot-thinking/api/test", { method: "POST", body: "{}" });
  assert.equal(response.statusCode, 409);
  assert.match(response.payload.error, /provider\/model/);
});

await check("test 路由：跟随最近一次 agent-loop 路由，并按“必须变短”判定", async () => {
  const ws = fakeWebServer();
  const seen = [];
  const marked = [];
  let mode = "short";
  const compress = async (text, options, cfg) => {
    seen.push({ text, options, cfg });
    if (mode === "null") {
      // Exactly what the real compressor does on failure: write the row, then
      // return null so the caller keeps the original block.
      mark("thinking-failed", { message: "provider exploded", code: "PROVIDER", stage: "first" });
      return null;
    }
    // "short" = a real compression; "echo" = the compressor returned the draft
    // itself, which the real link would keep (`not-shorter`).
    return { text: mode === "short" ? "[决定] 缩短后的节点" : text, event: { effort: "off", profile: cfg.profile } };
  };
  registerConfigApi(testCtx(ws), fakeStore(), {
    compress,
    mark: (event, payload) => marked.push({ event, ...payload }),
    getRoute: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
  });

  const short = await ws.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: LONG_DRAFT }) });
  assert.equal(short.statusCode, 200);
  assert.equal(short.payload.ok, true);
  assert.equal(short.payload.replaced, true);
  assert.equal(short.payload.provider, "deepseek-official");
  assert.equal(short.payload.effort, "off");
  assert.equal(seen[0].options.provider, "deepseek-official");
  assert.equal(seen[0].cfg.archive, false, "自测不写归档");

  const sameLength = await ws.call("/iot-thinking/api/test", {
    method: "POST",
    body: JSON.stringify({ text: "[决定] 缩短后的节点" }),
  });
  assert.equal(sameLength.payload.ok, false);
  assert.equal(sameLength.payload.replaced, false);
  assert.equal(sameLength.payload.reason, "below-min-chars", "短输入在真实链路上根本不会被压");

  mode = "echo";
  const kept = await ws.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: LONG_DRAFT }) });
  assert.equal(kept.payload.ok, false);
  assert.equal(kept.payload.reason, "not-shorter");

  // A failed compressor call must not be reported as "not shorter".
  mode = "null";
  const failed = await ws.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: LONG_DRAFT }) });
  assert.equal(failed.payload.ok, false);
  assert.equal(failed.payload.reason, "compressor-failed");
  assert.equal(failed.payload.error, "provider exploded");

  assert.ok(marked.some((entry) => entry.event === "thinking-self-test"));
});

await check("test 路由：开关关掉 / 输入短于 minChars 时如实说“真实链路会跳过”", async () => {
  const ws = fakeWebServer();
  let calls = 0;
  const compress = async () => {
    calls += 1;
    return { text: "[决定] 缩短", event: { effort: "off" } };
  };
  registerConfigApi(testCtx(ws), fakeStore({ minChars: 500 }), {
    compress,
    getRoute: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
  });

  const tooShort = await ws.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: "短短一段思考，远不到 500 字。" }) });
  assert.equal(tooShort.statusCode, 200);
  assert.equal(tooShort.payload.ok, false);
  assert.equal(tooShort.payload.reason, "below-min-chars");
  assert.equal(tooShort.payload.minChars, 500);
  assert.equal(tooShort.payload.text, null);
  assert.equal(calls, 0, "短输入连压缩器都不该调");

  const long = await ws.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: LONG_DRAFT.repeat(3) }) });
  assert.equal(long.payload.ok, true);
  assert.equal(calls, 1);

  const off = fakeWebServer();
  registerConfigApi(testCtx(off), fakeStore({ enabled: false }), {
    compress,
    getRoute: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
  });
  const disabled = await off.call("/iot-thinking/api/test", { method: "POST", body: JSON.stringify({ text: LONG_DRAFT }) });
  assert.equal(disabled.payload.ok, false);
  assert.equal(disabled.payload.reason, "disabled");
  assert.equal(calls, 1, "关掉时也不该调压缩器");
});

await check("test 路由：自带样稿超过默认 minChars，全新安装点下去不会假绿", async () => {
  const ws = fakeWebServer();
  let looksLikeSample = false;
  registerConfigApi(testCtx(ws), fakeStore(), {
    compress: async (text) => {
      looksLikeSample = text.includes("块级替换") && text.includes("退化判定");
      return { text: "[决定] 缩短", event: { effort: "off" } };
    },
    getRoute: () => ({ provider: "deepseek-official", model: "deepseek-flash" }),
  });
  const response = await ws.call("/iot-thinking/api/test", { method: "POST", body: "{}" });
  assert.equal(response.statusCode, 200);
  assert.equal(response.payload.ok, true);
  assert.equal(response.payload.reason, null);
  assert.ok(response.payload.charsIn >= 400, `样稿长度 ${response.payload.charsIn} 应达到默认 minChars`);
  assert.equal(looksLikeSample, true, "默认样稿应是一段真实的推理草稿");
});

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
