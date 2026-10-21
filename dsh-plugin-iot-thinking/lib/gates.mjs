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
 * 一次改写结果的裁决。
 * @param {string} text - 模型给出的内容（已 trim）。
 * @param {{ original?: string, minOut?: number }} [options]
 */
export function judge(text, { original = "", minOut = 4 } = {}) {
  const out = String(text ?? "").trim();
  if (!out) return { ok: false, reason: "empty" };
  if (out === String(original ?? "").trim()) return { ok: false, reason: "unchanged" };
  if (out.length < minOut) return { ok: false, reason: "too-thin" };
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
