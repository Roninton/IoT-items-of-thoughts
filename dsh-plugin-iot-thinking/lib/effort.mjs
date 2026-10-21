// reasoningEffort 策略：先试 off，被拒则改用该路由公开的低声档，再不行就不发这个字段。
// 决定按 (provider, model, mode) 缓存；探测失败不缓存。

export const EFFORT_IDS = Object.freeze(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);

export const AUTO_EFFORT = "auto";

/** 适配器可能自报别的 id，所以只做形态校验，不比对 EFFORT_IDS。 */
const EFFORT_SHAPE = /^[a-z][a-z0-9._-]{0,31}$/;

const LOW_EFFORTS = Object.freeze(["minimal", "low"]);

const PLAN_LIMIT = 64;

/** route+mode → 要发的档位（null = 不发该字段） */
const plans = new Map();

export function normalizeEffort(value) {
  if (value === undefined) return null;
  if (value === null) return AUTO_EFFORT;
  if (typeof value !== "string") return null;
  const effort = value.trim().toLowerCase();
  if (effort === "" || effort === AUTO_EFFORT) return AUTO_EFFORT;
  return EFFORT_SHAPE.test(effort) ? effort : null;
}

export function planKey(provider, model, mode) {
  return `${provider ?? ""}\u0000${model ?? ""}\u0000${mode ?? AUTO_EFFORT}`;
}

export function knownEffortPlan(key) {
  return plans.get(key);
}

export function rememberEffortPlan(key, effort) {
  if (!plans.has(key) && plans.size >= PLAN_LIMIT) {
    const oldest = plans.keys().next();
    if (!oldest.done) plans.delete(oldest.value);
  }
  plans.set(key, effort ?? null);
  return effort ?? null;
}

export function resetEffortPlans() {
  plans.clear();
}

/** 只在 off 被拒后才调用：挑一个便宜的公开档位，minimal 优先于 low。 */
export function pickLowEffort(ids) {
  const list = Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
  return LOW_EFFORTS.find((id) => list.includes(id)) ?? null;
}

/** 宿主把"不支持该档位"归一成 UNSUPPORTED_REASONING_EFFORT；文案匹配是兜底。 */
export function isUnsupportedEffortFailure(result) {
  if (!result || !result.error) return false;
  if (result.code === "UNSUPPORTED_REASONING_EFFORT") return true;
  return /does not support reasoning effort/i.test(String(result.error));
}
