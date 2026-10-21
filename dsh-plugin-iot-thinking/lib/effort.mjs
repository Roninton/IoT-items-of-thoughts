// lib/effort.mjs — reasoning-effort policy for the compressor call.
//
// The compressor used to hardcode `reasoningEffort: "off"`. That is the one
// value least likely to be accepted: a route that declares reasoning rejects
// `off` (dsh-llm filters it out of `getSupportedThinkingLevels`), and the host
// refuses the request with UNSUPPORTED_REASONING_EFFORT before any network I/O.
// On such a route the compressor never once succeeded and every failure was
// silent — the reasoning block just streamed through unchanged (issue #3).
//
// The only value the *host* accepts without consulting the catalog is "no
// field" — it then resolves the adapter's own default level. That is not the
// same as "every route works": dsh-llm resolves `requested ?? defaultEffort` and
// rejects the default too when the adapter advertises one that its effort list
// does not contain. That case is reported as `thinking-effort-unsupported`
// rather than silently retried, because nothing this plugin can send will work.
//
//   auto (default) — try `off`; if the host refuses that level, ask the route
//                    which low level it does advertise (minimal/low) and use
//                    that, otherwise omit the field.
//   <effort id>    — send exactly that; on refusal fall back the same way rather
//                    than failing the block.
//
// The decision is remembered per (provider, model, mode), so the doomed attempt
// happens at most once per route per process *once the catalog answers*: a probe
// that failed is deliberately not cached, so a transient catalog hiccup cannot
// pin a route to "omit" forever.

/** Effort ids the bundled adapters advertise (pi-ai THINKING_LEVELS). */
export const EFFORT_IDS = Object.freeze(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);

/** "No explicit level": decide from the route at request time. */
export const AUTO_EFFORT = "auto";

/**
 * Shape of an effort id this plugin is willing to store. The ids are adapter
 * vocabulary (`ReasoningEffortId` is opaque to the harness), so the list above is
 * only the bundled adapters' set: the card offers what `resolveModelInfo()`
 * advertises, and an adapter may name a level we do not know. Refusing those
 * would silently drop a legitimate choice; sending an unknown one is harmless
 * because the route's refusal is handled like any other unsupported level.
 */
const EFFORT_SHAPE = /^[a-z][a-z0-9._-]{0,31}$/;

/** Levels worth trying when `off` is refused: cheap, and supported almost everywhere. */
const LOW_EFFORTS = Object.freeze(["minimal", "low"]);

/** Bound on remembered routes; a session talks to a handful, not hundreds. */
const PLAN_LIMIT = 64;

/** @type {Map<string, string|null>} route+mode -> effort to send (null = omit the field) */
const plans = new Map();

/**
 * Normalize one configured effort value.
 *
 * @param {unknown} value - config value (`null` and absent both mean auto).
 * @returns {string|null} an effort id, `AUTO_EFFORT`, or null when invalid.
 */
export function normalizeEffort(value) {
  if (value === undefined) return null;
  if (value === null) return AUTO_EFFORT;
  if (typeof value !== "string") return null;
  const effort = value.trim().toLowerCase();
  if (effort === "" || effort === AUTO_EFFORT) return AUTO_EFFORT;
  return EFFORT_SHAPE.test(effort) ? effort : null;
}

/**
 * Cache key. The mode is part of it so switching from auto to an explicit level
 * (or back) re-tests the route instead of inheriting another mode's answer.
 */
export function planKey(provider, model, mode) {
  return `${provider ?? ""}\u0000${model ?? ""}\u0000${mode ?? AUTO_EFFORT}`;
}

/** @returns {string|null|undefined} remembered effort, or undefined when unknown. */
export function knownEffortPlan(key) {
  return plans.get(key);
}

/** Remember what this route accepts. `null` means "omit the field". */
export function rememberEffortPlan(key, effort) {
  if (!plans.has(key) && plans.size >= PLAN_LIMIT) {
    const oldest = plans.keys().next();
    if (!oldest.done) plans.delete(oldest.value);
  }
  plans.set(key, effort ?? null);
  return effort ?? null;
}

/** Test hook: forget every route decision. */
export function resetEffortPlans() {
  plans.clear();
}

/**
 * Pick a cheap advertised level, ignoring `off` (we only ask after `off` was
 * refused). The caller's adapter reports efforts in its own preferred order,
 * but the preference here is deliberate: minimal < low.
 *
 * @param {readonly (string|undefined)[]|undefined} ids - advertised effort ids.
 * @returns {string|null} the level to send, or null to omit the field.
 */
export function pickLowEffort(ids) {
  const list = Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
  return LOW_EFFORTS.find((id) => list.includes(id)) ?? null;
}

/**
 * Whether a compressor attempt failed because the route rejects the requested
 * effort level. The host normalizes that failure to `UNSUPPORTED_REASONING_EFFORT`;
 * the message test is a defensive second signal for older shells that surface
 * the adapter message without the code.
 *
 * @param {{error?: unknown, code?: unknown}} result - one attempt outcome.
 */
export function isUnsupportedEffortFailure(result) {
  if (!result || !result.error) return false;
  if (result.code === "UNSUPPORTED_REASONING_EFFORT") return true;
  return /does not support reasoning effort/i.test(String(result.error));
}
