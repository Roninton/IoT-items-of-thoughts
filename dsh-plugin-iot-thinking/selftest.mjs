/**
 * Pure-logic selftest for the llm/stream transformer.
 * No dsh boot, no model call.
 *
 *   node selftest.mjs
 */
import assert from "node:assert/strict";
import { transformStream } from "./lib/stream.mjs";
import {
  buildCompressionPrompt,
  fallbackItems,
  formatNodes,
  isDegenerateDraft,
  isDegenerateItems,
  sanitizeDraft,
} from "./lib/think.mjs";
import { compressThinking } from "./lib/compress.mjs";
import { defaultNodeModulesRoots, rootFromEntryPath, versionedRoots } from "./lib/seams.mjs";
import { guardRequest, redactEvents } from "./lib/config-api.mjs";

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

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
