/**
 * iot-compaction selftest —— 纯逻辑，不启动 dsh、不连模型。
 *
 *   node dsh-plugin-compaction/selftest.mjs
 *
 * 覆盖的是**最容易静默出错**的三块：
 *   1. thinking 的识别与抽取（漏抽 = 提纯变成摘要；多抽 = 把正文当思考压掉）
 *   2. 条目渲染约束（提纯指令必须带"只压不补 / 禁完成态断言"这两条硬规则）
 *   3. 引擎闸门（没有 thinking 就不该白调一次模型）
 */
import assert from "node:assert/strict";
import { extractThinking, hasThinking, IOT_INSTRUCTION } from "./lib/think.mjs";
import { messagesForSeqs, selectEveryStepRange, surfaceMessages } from "./lib/surface.mjs";

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`FAIL ${name}\n     ${error.message}`);
  }
}

const msg = (content) => ({ id: "m", role: "assistant", content, source: { kind: "test" } });
const text = (t) => ({ type: "text", text: t });
const reasoning = (t) => ({ type: "reasoning", text: t });
const toolCall = (name) => ({ type: "tool-call", id: "c1", name, arguments: "{}" });

// --------------------------------------------------------------------------- //
// 1. 抽取
// --------------------------------------------------------------------------- //
const conversation = [
  msg([text("我先看看代码")]),
  msg([reasoning("考虑用 A 方案，因为它省内存"), text("定了"), toolCall("read")]),
  msg([reasoning("但 B 方案更快；否决 A"), text("改主意了")]),
  msg([text("完成")]),
];

check("reasoning 全部被抽出（跨消息累积）", () => {
  const { reasoning: r } = extractThinking(conversation);
  assert.match(r, /A 方案/);
  assert.match(r, /B 方案更快/);
});

check("正文没有被当成 thinking", () => {
  const { reasoning: r, visible } = extractThinking(conversation);
  assert.ok(!r.includes("我先看看代码"));
  assert.match(visible, /我先看看代码/);
});

check("工具名被收集（条目里要用它做物证索引）", () => {
  const { toolCalls } = extractThinking(conversation);
  assert.deepEqual(toolCalls, ["read"]);
});

check("没有 reasoning 时 hasThinking 为假", () => {
  assert.equal(hasThinking([msg([text("只是正文")]), msg([toolCall("pwsh")])]), false);
});

check("有 reasoning 时 hasThinking 为真", () => {
  assert.equal(hasThinking([msg([reasoning("想一想")])]), true);
});

check("空块 / 空白 reasoning 不算 thinking", () => {
  assert.equal(hasThinking([msg([reasoning("   ")])]), false);
});

check("字符串形态的 content 不会崩（旧消息兼容）", () => {
  const { reasoning: r } = extractThinking([{ role: "assistant", content: "纯字符串" }]);
  assert.equal(r, "");
});

// --------------------------------------------------------------------------- //
// 2. 提纯指令的硬规则（提示词回归：缺了它们会退回"摘要"）
// --------------------------------------------------------------------------- //
check("指令包含「只压不补」", () => assert.match(IOT_INSTRUCTION, /只压不补/));
check("指令包含「禁止完成态断言」", () => assert.match(IOT_INSTRUCTION, /完成态断言/));
check("指令要求否掉的路线单独成条", () => assert.match(IOT_INSTRUCTION, /否掉的路线必须单独成条/));
check("指令要求未定的写成待定", () => assert.match(IOT_INSTRUCTION, /待定/));

// --------------------------------------------------------------------------- //
// 3. surface 投影与选段（0.2.x 的 surface.nodes 是 seq 数组）
// --------------------------------------------------------------------------- //
const seqSession = (events) => ({
  surface: { nodes: Object.keys(events).map(Number) },
  eventAt: (seq) => events[seq],
  deriveEventMessage: (event) => event?.message ?? null,
});

check("0.2.x seq surface：按 deriveEventMessage 投影出消息", () => {
  const session = seqSession({
    0: { type: "system/message", message: { role: "system", content: [text("sys")] } },
    1: { type: "user/message", message: msg([text("只有正文")]) },
  });
  const messages = surfaceMessages(session);
  assert.equal(messages.length, 2);
  assert.equal(hasThinking(messages), false);
});

check("messagesForSeqs 只投影选中的 seq", () => {
  const session = seqSession({
    0: { type: "system/message", message: { role: "system", content: [text("sys")] } },
    1: { type: "assistant/message", message: msg([text("旧正文")]) },
    2: { type: "assistant/message", message: msg([reasoning("新 Thinking")]) },
  });
  assert.deepEqual(messagesForSeqs(session, [2]).length, 1);
  assert.equal(hasThinking(messagesForSeqs(session, [2])), true);
  assert.equal(hasThinking(messagesForSeqs(session, [1])), false);
});

check("0.1.x 对象节点仍可读（升级期兼容）", () => {
  const session = { surface: { nodes: [{ message: msg([text("旧形状")]) }] } };
  assert.equal(surfaceMessages(session).length, 1);
});

check("surface 取不到 → 空数组（闸门因此不拦）", () => {
  assert.equal(surfaceMessages({}).length, 0);
  assert.equal(surfaceMessages({ session: {} }).length, 0);
  assert.equal(surfaceMessages({ surface: {} }).length, 0);
});

// selectEveryStepRange ------------------------------------------------------- //
const pairingAlwaysBalanced = {
  balancedBefore: () => true,
  balancedAfter: () => true,
};
const rangeSession = (seqs, headType = "system/message") => ({
  surface: { nodes: seqs },
  eventAt: (seq) => (seq === seqs[0] ? { type: headType } : { type: "assistant/message" }),
});
const measurementFor = (seqs) => ({ nodes: seqs.map((seq) => ({ seq, tokens: 10 })) });

check("选段跳过 system 头，覆盖到尾部", () => {
  const seqs = [0, 1, 2, 3];
  const range = selectEveryStepRange(rangeSession(seqs), measurementFor(seqs), pairingAlwaysBalanced);
  assert.deepEqual(range, {
    startIdx: 1,
    endIdx: 3,
    start: 1,
    end: 3,
    seqs: [1, 2, 3],
  });
});

check("没有 system 头时从 0 开始", () => {
  const seqs = [0, 1, 2];
  const range = selectEveryStepRange(
    rangeSession(seqs, "user/message"),
    measurementFor(seqs),
    pairingAlwaysBalanced,
  );
  assert.equal(range.start, 0);
  assert.equal(range.end, 2);
});

check("尾部未配对时向前退到最近的平衡点", () => {
  const seqs = [0, 1, 2, 3];
  const pairing = {
    balancedBefore: () => true,
    balancedAfter: (_session, seq) => seq !== 3,
  };
  const range = selectEveryStepRange(rangeSession(seqs), measurementFor(seqs), pairing);
  assert.equal(range.end, 2);
  assert.equal(range.endIdx, 2);
});

check("起点未配对时向后推到最近的平衡点", () => {
  const seqs = [0, 1, 2, 3];
  const pairing = {
    balancedBefore: (_session, seq) => seq !== 1,
    balancedAfter: () => true,
  };
  const range = selectEveryStepRange(rangeSession(seqs), measurementFor(seqs), pairing);
  assert.equal(range.start, 2);
  assert.deepEqual(range.seqs, [2, 3]);
});

check("measurement 与 surface 不一致时拒绝选段（不猜）", () => {
  const seqs = [0, 1, 2];
  assert.equal(
    selectEveryStepRange(rangeSession(seqs), { nodes: [{ seq: 0 }, { seq: 9 }] }, pairingAlwaysBalanced),
    null,
  );
  assert.equal(selectEveryStepRange(rangeSession(seqs), { nodes: [] }, pairingAlwaysBalanced), null);
});

check("只有 system 头 / 空 surface 时返回 null", () => {
  assert.equal(
    selectEveryStepRange(rangeSession([0]), measurementFor([0]), pairingAlwaysBalanced),
    null,
  );
  assert.equal(selectEveryStepRange(rangeSession([]), { nodes: [] }, pairingAlwaysBalanced), null);
});

check("配对函数抛错时返回 null（不把宿主的选择逻辑带崩）", () => {
  const seqs = [0, 1, 2];
  const pairing = {
    balancedBefore: () => true,
    balancedAfter: () => {
      throw new Error("not a surface seq");
    },
  };
  assert.equal(selectEveryStepRange(rangeSession(seqs), measurementFor(seqs), pairing), null);
});

// --------------------------------------------------------------------------- //
// 4. thresholdRatio 的合法区间（真机事故回归：0 会让 dsh web 起不来）
// --------------------------------------------------------------------------- //
const { MIN_THRESHOLD_RATIO, normalizeThresholdRatio, assertRetentionBelowThreshold } =
  await import("./lib/config-check.mjs");

check("normalizeThresholdRatio 对省略值返回合法极小正数（>0 且 <=1）", () => {
  assert.equal(normalizeThresholdRatio(undefined), MIN_THRESHOLD_RATIO);
  assert.ok(MIN_THRESHOLD_RATIO > 0, "默认值必须 > 0，0 会被宿主拒绝");
  assert.ok(MIN_THRESHOLD_RATIO <= 1);
});

check("retainRatio 必须小于 thresholdRatio（第二次崩溃的那条不变量）", () => {
  // 真机：retainRatio 0.12 + thresholdRatio 1e-6 → 宿主拒绝
  assert.throws(() => assertRetentionBelowThreshold(0.12, 1e-6), /retainRatio .* 必须小于 thresholdRatio/);
  assert.throws(() => assertRetentionBelowThreshold(0.8, 0.8), /必须小于/);
  // 不传 thresholdRatio（交给宿主默认）时不校验、不抛
  assert.doesNotThrow(() => assertRetentionBelowThreshold(0.12, undefined));
  // 合法组合
  assert.doesNotThrow(() => assertRetentionBelowThreshold(0.12, 0.8));
  assert.doesNotThrow(() => assertRetentionBelowThreshold(0.01, 0.05));
});

check("thresholdRatio=0 被明确拒绝（这是导致 plugin tree 崩溃的那个值）", () => {
  assert.throws(() => normalizeThresholdRatio(0), /thresholdRatio 必须是 \(0, 1\]/);
});

check("负数 / >1 / NaN 都被拒绝且错误信息带配置项名", () => {
  for (const bad of [-0.1, -1, 1.5, NaN, "abc"]) {
    assert.throws(() => normalizeThresholdRatio(bad), /iot-compaction: thresholdRatio/, String(bad));
  }
});

check("合法值原样保留（1 与 1e-6 都在区间内）", () => {
  assert.equal(normalizeThresholdRatio(1), 1);
  assert.equal(normalizeThresholdRatio(1e-6), 1e-6);
  assert.equal(normalizeThresholdRatio(0.8), 0.8);
});

check("字符串数字被接受（Loader 归一化后可能拿到字符串）", () => {
  assert.equal(normalizeThresholdRatio("0.5"), 0.5);
});

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
