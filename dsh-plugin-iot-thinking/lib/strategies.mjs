// 档位注册表：三档基础模式 + 自定义预设 + 关闭。
// 每个档位给出五个部件（prepare / skipInput / buildPrompt / finalize / accepts），
// 引擎不认识具体档位。

import {
  buildCompressionPrompt,
  fallbackItems,
  formatNodes,
  isDegenerateDraft,
  isDegenerateItems,
  PROFILES,
  sanitizeDraft,
} from "./think.mjs";
import { judge, stylometry } from "./gates.mjs";
import { buildCustomPrompt, readPreset } from "./custom.mjs";
import { builtinPresetById, builtinPresetText } from "./builtin-presets.mjs";

/** 基础三档的替换规则：候选必须非空且严格更短。 */
export function isShorterReplacement(original, candidate) {
  if (typeof candidate !== "string") return false;
  const next = candidate.trim();
  return next.length > 0 && next.length < String(original ?? "").trim().length;
}

/** 自定义预设的默认规则：只要不是原样即可。 */
export function isDifferentReplacement(original, candidate) {
  if (typeof candidate !== "string") return false;
  const next = candidate.trim();
  return next.length > 0 && next !== String(original ?? "").trim();
}

function nodeCount(text) {
  return String(text ?? "")
    .split(/\n{2,}/)
    .map((node) => node.trim())
    .filter(Boolean).length;
}

function itemsStrategy({ id, label, profile, summary, defaults }) {
  const profileDef = PROFILES[profile] ?? PROFILES.full;
  return Object.freeze({
    id,
    label,
    family: "items",
    summary,
    defaults: Object.freeze({
      minChars: 400,
      maxChars: 24000,
      maxTokens: 3000,
      temperature: 0.2,
      maxNodes: profileDef.maxNodes,
      maxNeutral: profileDef.maxNeutral,
      ...defaults,
    }),
    accept: "shorter",
    /** 合法标签集：输出必须至少命中一个，否则判 no-label。 */
    labels: profileDef.labels,
    retryEvent: "thinking-degenerate",
    accepts: (original, candidate) => isShorterReplacement(original, candidate),
    prepare: () => ({ state: null, error: null }),
    skipInput: (draft) => (isDegenerateDraft(draft) || !sanitizeDraft(draft).trim() ? "degenerate-input" : null),
    // 用户补充层：三档基础模式也接受一段用户文本作为标签补充。
    // 空文本时行为与旧版完全一致（不注入任何额外要求）。
    buildPrompt: (draft, { strict, limits, extraPrompt, allowAdvice }) =>
      buildCompressionPrompt(draft, { profile, strict, limits, extraPrompt, allowAdvice }),
    finalize: (raw, { limits, allowAdvice }) => {
      // 先看原始输出：formatNodes 会把退化输出整理得"看起来正常"。
      const rawDegenerate = isDegenerateItems(raw);
      const text = formatNodes(raw, {
        profile,
        maxNodes: limits.maxNodes,
        maxNeutral: limits.maxNeutral,
        // 内置建议模式：建议节点不计入预算，也不能被当成未知标签裁掉。
        allowAdvice: allowAdvice === true,
      });
      if (rawDegenerate) return { ok: false, text, reason: "degenerate", detail: "raw" };
      if (!text) return { ok: false, text, reason: "empty" };
      if (isDegenerateItems(text)) return { ok: false, text, reason: "degenerate" };
      return { ok: true, text };
    },
    fallback: (draft) => {
      const items = fallbackItems(draft);
      return items && items.length < draft.length ? items : null;
    },
    metrics: (text) => ({ nodes: nodeCount(text) }),
  });
}

function customStrategy() {
  return Object.freeze({
    id: "custom",
    label: "用户配置",
    family: "custom",
    summary: "用你自己的文本当改写指令：从你指定的预设目录选一份，或直接粘进来。",
    defaults: Object.freeze({
      minChars: 100,
      maxChars: 24000,
      maxTokens: 3000,
      temperature: 0.4,
      acceptRule: "different",
      preset: null,
    }),
    accept: "different",
    labels: PROFILES.full.labels,
    retryEvent: "thinking-rejected",
    accepts: (original, candidate, limits) => {
      const rule = limits?.acceptRule === "shorter" ? "shorter" : "different";
      return rule === "shorter" ? isShorterReplacement(original, candidate) : isDifferentReplacement(original, candidate);
    },
    prepare: (limits) => {
      const preset = limits?.preset;
      if (!preset) return { state: null, error: "还没选自定义预设" };
      // 内置预设：只读、随插件发布，不需要 presetDir。
      if (preset.kind === "builtin" || preset.builtin) {
        const builtin = builtinPresetById(preset.name ?? preset.builtin);
        if (!builtin) return { state: null, error: `没有这个内置预设：${String(preset.name ?? preset.builtin)}` };
        // 内置预设可编辑：用户在 config.builtinOverrides 里存的正文优先。
        const text = builtinPresetText(builtin.id, preset.overrides ?? {});
        return { state: { text, label: builtin.name, path: `builtin:${builtin.id}`, edited: text !== builtin.text }, error: null };
      }
      if (preset.inline) {
        const text = String(preset.inline).trim();
        if (!text) return { state: null, error: "自定义预设是空的（贴一段文本，或从目录选一份）" };
        return { state: { text, label: preset.name ?? "（贴入的文本）" }, error: null };
      }
      if (!preset.name) return { state: null, error: "自定义预设没有指定名字" };
      const loaded = readPreset(preset.dir, preset.name);
      if (loaded.error === "preset-dir-not-set") return { state: null, error: "还没设置预设目录" };
      if (loaded.error) return { state: null, error: loaded.error };
      return { state: { text: loaded.text, label: preset.name }, error: null };
    },
    skipInput: (draft) => (String(draft ?? "").trim() ? null : "empty-draft"),
    buildPrompt: (draft, { strict, reason, detail, state, limits, allowAdvice }) =>
      buildCustomPrompt(state?.text ?? "", draft, { strict, reason, detail, limits, allowAdvice }),
    finalize: (raw, { original, limits }) => {
      // 必须带上 text：引擎在成功分支里读 verdict.text（items 档位也是这么返回的）。
      const out = String(raw ?? "").trim();
      return {
        ...judge(out, {
          original,
          minOut: 4,
          // 输出预算与"不许在输出里思考"是压缩器自己的约束：模型不遵守时
          // 要有确定性后果，而不是靠提示词求它。
          maxOut: Number.isFinite(limits?.maxOut) ? limits.maxOut : 0,
          noThink: limits?.noThink === true,
        }),
        text: out,
      };
    },
    fallback: () => null,
    metrics: (text) => stylometry(text),
  });
}

function offStrategy() {
  return Object.freeze({
    id: "off",
    label: "关闭（这段会话不做任何改写）",
    family: "off",
    summary: "保留原生 thinking，一个字节都不改。",
    defaults: Object.freeze({ minChars: 0, maxChars: 0, maxTokens: 0, temperature: 0 }),
    accept: "none",
    accepts: () => false,
    prepare: () => ({ state: null, error: null }),
    skipInput: () => "off",
    buildPrompt: () => "",
    finalize: (raw) => ({ ok: false, text: String(raw ?? ""), reason: "off" }),
    fallback: () => null,
    metrics: () => ({}),
  });
}

export const STRATEGIES = Object.freeze([
  itemsStrategy({
    id: "iot-compact",
    label: "压缩 · 极简（3 标签）",
    profile: "compact",
    summary: "3 标签、3~4 条节点。压得最狠，适合只想赶快接着干的短任务。",
    defaults: {},
  }),
  itemsStrategy({
    id: "iot-balanced",
    label: "压缩 · 均衡（5 标签）",
    profile: "balanced",
    summary: "5 标签、4~6 条节点，保留 [计划] 与 [决定]。",
    defaults: {},
  }),
  itemsStrategy({
    id: "iot-full",
    label: "压缩 · 完整（8 标签）",
    profile: "full",
    summary: "8 标签、4~6 条节点。[权衡]/[风险]/[待定] 都保留，质量优先。",
    defaults: {},
  }),
  customStrategy(),
  offStrategy(),
]);

const BY_ID = new Map(STRATEGIES.map((strategy) => [strategy.id, strategy]));

export function strategyById(id) {
  return BY_ID.get(String(id ?? ""));
}

export const DEFAULT_STRATEGY = "iot-full";

export const LEGACY_PROFILE_TO_STRATEGY = Object.freeze({
  compact: "iot-compact",
  balanced: "iot-balanced",
  full: "iot-full",
});

export function strategyIds() {
  return STRATEGIES.map((strategy) => strategy.id);
}

export const LIMIT_KEYS = Object.freeze([
  "minChars",
  "maxChars",
  "maxTokens",
  "temperature",
  "maxNodes",
  "maxNeutral",
  "minOut",
  "minRatio",
  "collapseFloorChars",
]);

/** v0.1 的平铺字段，只在没有逐档覆盖且属于 items 家族时兜底。 */
export const LEGACY_LIMIT_KEYS = Object.freeze(["minChars", "maxChars", "maxTokens", "maxNodes", "maxNeutral"]);

export function limitsOf(id, overrides = {}, legacy = null) {
  const strategy = strategyById(id) ?? strategyById(DEFAULT_STRATEGY);
  const limits = { ...strategy.defaults };
  const own = overrides && typeof overrides === "object" && !Array.isArray(overrides) ? overrides : null;
  const hasOwn = own !== null && Object.keys(own).length > 0;

  if (!hasOwn && legacy && strategy.family === "items") {
    for (const key of LEGACY_LIMIT_KEYS) {
      if (Number.isFinite(legacy[key])) limits[key] = legacy[key];
    }
  }

  if (own) {
    for (const [key, value] of Object.entries(own)) {
      if (value === undefined || value === null) continue;
      if (LIMIT_KEYS.includes(key)) {
        if (Number.isFinite(value)) limits[key] = value;
        continue;
      }
      limits[key] = value;
    }
  }
  return limits;
}
