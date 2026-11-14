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

export function planKey(provider, model, mode, role = "rewrite") {
  // role 必须进 key：建议链与改写链的降级结论不能互相复用。
  return `${role ?? "rewrite"}\u0000${provider ?? ""}\u0000${model ?? ""}\u0000${mode ?? AUTO_EFFORT}`;
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

/**
 * 有些模型只接受默认 temperature（例如 GPT-6 Astra 只认 1），发 0.2/0.3 会 400。
 * 这类拒绝是**参数约束**，不是网络错误：正确做法是去掉该字段重试一次，并记住这个路由。
 */
export function isUnsupportedTemperatureFailure(result) {
  if (!result || !result.error) return false;
  if (result.code === "UNSUPPORTED_TEMPERATURE") return true;
  const message = String(result.error);
  return /temperature/i.test(message) && /(unsupported|does not support|only.*default|invalid)/i.test(message);
}

/** temperature 决策缓存：与 effort 分开，避免两条降级链互相污染。 */
const TEMPERATURE_PLANS = new Map();

export function temperaturePlanKey(provider, model, mode) {
  return `temperature:${String(provider ?? "")}/${String(model ?? "")}/${String(mode ?? "")}`;
}

export function knownTemperaturePlan(key) {
  return TEMPERATURE_PLANS.get(key);
}

export function rememberTemperaturePlan(key, value) {
  TEMPERATURE_PLANS.set(key, value);
}

export function resetTemperaturePlans() {
  TEMPERATURE_PLANS.clear();
}

/**
 * 把宿主的取消信号和本插件自己的超时控制器合成一个。
 *
 * 直接 `controller.signal ?? options.signal` 是错的：AbortController 在 Node 20
 * 恒存在，宿主的 Esc/取消信号永远拿不到，用户中断后请求还会跑满超时，白烧 token。
 */
export function combineSignals(controllerSignal, hostSignal) {
  if (!controllerSignal) return hostSignal;
  if (!hostSignal) return controllerSignal;
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.any === "function") {
    return AbortSignal.any([controllerSignal, hostSignal]);
  }
  // 老环境：手动转发一次，只转发 abort（不复制 reason，够用）。
  const controller = new AbortController();
  const forward = () => controller.abort();
  if (controllerSignal.aborted || hostSignal.aborted) controller.abort();
  else {
    controllerSignal.addEventListener("abort", forward, { once: true });
    hostSignal.addEventListener("abort", forward, { once: true });
  }
  return controller.signal;
}

/**
 * 把任意失败形态转成可读的一行。
 *
 * 宿主的失败载荷不一定是 Error：`finish.failure` 与代理层经常给一个**对象**
 * （`{ message, code, param, cause }`）。直接 `String(error)` 会得到
 * `[object Object]`，账本里就只剩这一句废话，完全没法定位。
 */
export function describeError(error) {
  if (error === null || error === undefined) return "unknown";
  if (typeof error === "string") return error.slice(0, 800);
  if (error instanceof Error) return String(error.message || error.name || "Error").slice(0, 800);
  if (typeof error !== "object") return String(error).slice(0, 800);
  // 常见失败对象：优先取 message / error，其次序列化整个对象。
  const message = error.message ?? error.error ?? error.reason ?? error.cause;
  if (typeof message === "string" && message.trim()) return message.slice(0, 800);
  try {
    return JSON.stringify(error).slice(0, 800);
  } catch {
    return String(error).slice(0, 800);
  }
}

/** 从失败载荷里取 code：对象形态与 Error 形态都认。 */
export function errorCodeOf(error) {
  if (error && typeof error === "object") {
    if (typeof error.code === "string") return error.code;
    if (typeof error.failure?.code === "string") return error.failure.code;
  }
  return undefined;
}
