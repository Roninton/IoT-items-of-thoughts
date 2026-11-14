// 自定义预设的最小闸门：empty / unchanged / too-thin / degenerate。
// 刻意不做禁词、口头禅、塌缩比例 —— 那些需要一份规格才说得清，预设只是自由文本。

const LEADING_PUNCT = /^[\s，。、…—‥·「」『』"'“”‘’（）()【】\[\]!！?？~～*#>+\-]+/u;

/** 退化检测：完全重复的行，或复读单字。 */
export function isDegenerate(text) {
  const raw = String(text ?? "");
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) return true;
  if (/(\S)\1{5,}/u.test(raw)) return true;

  const meaningful = lines.filter((line) => line.replace(LEADING_PUNCT, "").length >= 2);
  const seen = new Set();
  let dup = 0;
  for (const line of meaningful) {
    if (seen.has(line)) dup += 1;
    else seen.add(line);
  }
  if (meaningful.length >= 3 && dup >= 2) return true;
  if (meaningful.length >= 2 && dup === meaningful.length) return true;
  return false;
}

/**
 * 压缩器把"思考"写回正文的痕迹：`<thinking>` / `<think>` / `[思考]` / `思考：`。
 *
 * 压缩器的任务是**只输出结论**。一旦输出里出现这些标记，说明它在输出里又开始
 * 推理了 —— 这类输出通常又长又碎，是超时与截断的主要来源。
 * 检测放在裁决里，而不是靠提示词"请求"它别想：模型不遵守时要有确定性后果。
 */
const THINK_TAG = /<\/?think(?:ing)?\b[^>]*>|\[思考\]|^\s*思考\s*[:：]/imu;

/**
 * 输出里是否至少有一个**合法标签**（行首 `[决定]` 这类）。
 *
 * 标签是"模型在压缩、而不是在继续思考"的唯一可机检信号：一个标签都没有，
 * 说明它很可能只是把草稿复述了一遍。这个检查必须在 `formatNodes` 之前做 ——
 * 后者会给无标签行补 `[中性]`，补完就永远"有标签"了。
 */
export function hasLabel(text, allowed = null) {
  const set = Array.isArray(allowed) && allowed.length ? new Set(allowed) : null;
  for (const line of String(text ?? "").split(/\r?\n/)) {
    const match = /^\s*(?:[-*•]\s*)?[\[【]([^\]】]{1,8})[\]】]/u.exec(line);
    if (!match) continue;
    const label = match[1].trim();
    if (set === null || set.has(label)) return true;
  }
  return false;
}

export function hasThinkTag(text) {
  return THINK_TAG.test(String(text ?? ""));
}

/**
 * 一次改写结果的裁决。
 * @param {string} text - 模型给出的内容（已 trim）。
 * @param {{ original?: string, minOut?: number, maxOut?: number, noThink?: boolean, requireLabel?: boolean, labels?: string[] }} [options]
 */
export function judge(text, { original = "", minOut = 4, maxOut = 0, noThink = false, requireLabel = false, labels = null } = {}) {
  const out = String(text ?? "").trim();
  if (!out) return { ok: false, reason: "empty" };
  if (noThink && hasThinkTag(out)) return { ok: false, reason: "has-think-tag" };
  if (requireLabel && !hasLabel(out, labels)) return { ok: false, reason: "no-label" };
  if (out === String(original ?? "").trim()) return { ok: false, reason: "unchanged" };
  if (out.length < minOut) return { ok: false, reason: "too-thin" };
  // 软预算：超过就是"写多了"，不在这里截断 —— 交给上层按原因重试一次。
  if (Number.isFinite(maxOut) && maxOut > 0 && out.length > maxOut) return { ok: false, reason: "over-budget" };
  if (isDegenerate(out)) return { ok: false, reason: "degenerate" };
  return { ok: true };
}

/** 记账用的形态学指标（不是质量分，用来观察漂移）。 */
export function stylometry(text) {
  const raw = String(text ?? "");
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const fragments = lines.filter((line) => !/[。！？!?]$/u.test(line)).length;
  return {
    lines: lines.length,
    fragments,
    fragmentRate: lines.length === 0 ? 0 : Number((fragments / lines.length).toFixed(2)),
    avgLine: lines.length === 0 ? 0 : Math.round(raw.replace(/\s+/g, "").length / lines.length),
  };
}
