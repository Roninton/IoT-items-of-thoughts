// 三层解析：sessions[sessionId] > agentPresets[presetId] > strategy。
// 必须在钩子内部做：llm/stream 不过 scope filter，挂载位置表达不了"这个会话用哪档"。
// agentPreset 从会话投影读（init: header.agentPreset，apply: agent-preset/selected）。

import {
  DEFAULT_STRATEGY,
  LEGACY_PROFILE_TO_STRATEGY,
  limitsOf,
  strategyById,
  strategyIds,
} from "./strategies.mjs";

/** 归一化一个策略 id；不认识就返回 null（调用方继续往下一层找）。 */
export function normalizeStrategyId(value) {
  if (typeof value !== "string") return null;
  const id = value.trim();
  if (!id) return null;
  return strategyById(id) ? id : null;
}

/**
 * 一条映射项的形状：`"iot-full"` / `"custom"` / `"custom:my-style"`。
 *
 * `custom:<名字>` 不是普通策略 id：自定义预设的**内容**在 presetDir 里，
 * 名字是用户的文件名，不可能是编译期常量。所以值域是"策略 id ∪ custom[:名字]"。
 *
 * 对象形态（`{ strategy: "…" }`）仍被接受，是为了读得懂更早版本写下的配置文件；
 * 写回时统一是字符串。
 *
 * @param {unknown} value
 * @returns {{id: string, custom?: string|null}|null}
 */
export function normalizeRouteEntry(value) {
  if (typeof value === "string") return normalizeEntryString(value);
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const base = normalizeEntryString(value.strategy);
  if (!base) return null;
  const custom = typeof value.custom === "string" && value.custom.trim() ? value.custom.trim() : base.custom ?? null;
  return {
    id: base.id,
    ...(base.id === "custom" ? { custom } : {}),
  };
}

/** 字符串形态的值 → `{ id, custom? }`。 */
function normalizeEntryString(raw) {
  const text = typeof raw === "string" ? raw.trim() : "";
  if (!text) return null;
  // `custom` 与 `custom:` 都是"用贴进来的那段文本"；`custom:名字` 指向 presetDir 里的文件。
  if (text === "custom" || text === "custom:") return { id: "custom", custom: null };
  if (text.startsWith("custom:")) {
    const name = text.slice("custom:".length).trim();
    return { id: "custom", custom: name || null };
  }
  const id = normalizeStrategyId(text);
  return id ? { id } : null;
}

/** 取一个映射表里某个键的生效项；表不存在/值不合法都视作"没有"。 */
function pick(table, key) {
  if (!key) return null;
  if (!table || typeof table !== "object" || Array.isArray(table)) return null;
  return normalizeRouteEntry(table[String(key)]);
}

/**
 * @param {object} cfg
 * @param {{sessionId?: unknown, agentPreset?: unknown}} [who]
 * @returns {{id: string, source: "session"|"preset"|"default"|"profile"|"fallback", key?: string}}
 */
export function resolveRoute(cfg, who = {}) {
  const sessionId = who.sessionId === undefined || who.sessionId === null ? "" : String(who.sessionId);
  const agentPreset = who.agentPreset === undefined || who.agentPreset === null ? "" : String(who.agentPreset);

  const fromSession = pick(cfg?.sessions, sessionId);
  if (fromSession) return { ...fromSession, source: "session", key: sessionId };

  const fromPreset = pick(cfg?.agentPresets, agentPreset);
  if (fromPreset) return { ...fromPreset, source: "preset", key: agentPreset };

  // custom 也是注册过的策略 id：先按 entry 解析，才不丢名字。
  const globalEntry = normalizeEntryString(cfg?.strategy);
  if (globalEntry?.id === "custom") return { id: "custom", custom: globalEntry.custom ?? null, source: "default" };
  const global = normalizeStrategyId(cfg?.strategy);
  if (global) return { id: global, source: "default" };

  const legacy = normalizeStrategyId(LEGACY_PROFILE_TO_STRATEGY[String(cfg?.profile ?? "")]);
  if (legacy) return { id: legacy, source: "profile", key: String(cfg?.profile) };

  return { id: DEFAULT_STRATEGY, source: "fallback" };
}

/**
 * @param {object} cfg
 * @param {{sessionId?: unknown, agentPreset?: unknown}} [who]
 * @returns {object} 生效配置（浅拷贝；limits 已合并，minChars/maxChars 抬到顶层）
 */
export function resolveConfig(cfg, who = {}) {
  const base = cfg && typeof cfg === "object" ? cfg : {};
  const route = resolveRoute(base, who);
  const strategy = strategyById(route.id) ?? strategyById(DEFAULT_STRATEGY);
  const overrides = base.strategies && typeof base.strategies === "object" ? base.strategies[route.id] : undefined;
  const limits = limitsOf(route.id, overrides, base);

  if (route.id === "custom") {
    limits.preset = route.custom
      ? { name: route.custom, dir: base.presetDir }
      : { inline: base.customText ?? "", name: "（贴入的文本）" };
    limits.acceptRule = base.presetAccept === "shorter" ? "shorter" : "different";
  }

  const off = strategy?.family === "off";
  return {
    ...base,
    strategy: route.id,
    route,
    limits,
    minChars: Number.isFinite(limits.minChars) ? Math.max(0, limits.minChars) : 0,
    maxChars: Number.isFinite(limits.maxChars) ? Math.max(1, limits.maxChars) : 24000,
    enabled: base.enabled !== false && !off,
  };
}

const LEGACY_FLAT_KEYS = Object.freeze(["profile", "minChars", "maxChars", "maxTokens", "maxNodes", "maxNeutral"]);

/**
 * 旧配置一次性迁移（行为不变）：平铺字段搬进 strategies[<档位>]，profile 翻成 strategy。
 * 搬完必须删旧键 —— 留着会让 limitsOf 在切档位时把旧数字带过去。
 */
export function migrateLegacyConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return raw;

  // 已迁过但还留着旧键（早期迁移只加不删）：补一次清理。
  // 只在"这些值确实已经被搬进 strategies[当前策略]"时删，避免把用户
  // 手写的、还没被搬走的字段一起清掉（那会静默改变行为）。
  if (raw.strategy !== undefined || raw.strategies !== undefined) {
    const moved = raw.strategies && typeof raw.strategies === "object" ? raw.strategies[raw.strategy] : undefined;
    const stray = LEGACY_FLAT_KEYS.filter((key) => raw[key] !== undefined);
    if (!moved || stray.length === 0) return raw;
    const next = { ...raw };
    for (const key of stray) delete next[key];
    return next;
  }

  const legacyId = LEGACY_PROFILE_TO_STRATEGY[String(raw.profile ?? "")];
  if (!legacyId) return raw;

  const limits = {};
  for (const key of ["minChars", "maxChars", "maxTokens", "maxNodes", "maxNeutral"]) {
    if (Number.isFinite(raw[key])) limits[key] = raw[key];
  }
  const next = {
    ...raw,
    strategy: legacyId,
    strategies: { [legacyId]: limits },
  };
  for (const key of LEGACY_FLAT_KEYS) delete next[key];
  return next;
}

/** 设置卡要展示的"有哪些策略可选"。`defaults` 让卡片能算出"这一档没被覆盖时长什么样"。 */
export function strategyCatalog() {
  return strategyIds().map((id) => {
    const strategy = strategyById(id);
    return { id, label: strategy.label, family: strategy.family, summary: strategy.summary, defaults: { ...strategy.defaults } };
  });
}
