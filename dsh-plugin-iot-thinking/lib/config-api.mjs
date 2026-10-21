// 设置卡 / 头部下拉的 HTTP 桥（挂在 ctx.webServer 上）。
//
// 门禁（宿主不做鉴权，所以每条 handler 自己守）：自定义头 x-iot-thinking、
// 同源（Sec-Fetch-Site / Origin==Host）、默认只收 loopback 对端
// （IOT_THINKING_ALLOW_REMOTE=1 可放开）。不加这些，一个未授权的 PUT 就能改路由
// 并把 thinking 发到别处，GET 还会漏配置路径与会话 id。
//
// 路由：config GET/PUT、status、models、test、session GET/PUT、presets、strategy、preset-dir。

import { normalizeEffort } from "./effort.mjs";
import { tailEvents } from "./ledger.mjs";
import { strategyById, strategyIds } from "./strategies.mjs";
import { normalizeRouteEntry, resolveConfig, strategyCatalog } from "./route.mjs";
import { listPresets, readPreset, writePreset } from "./custom.mjs";

const MAX_BODY_BYTES = 256 * 1024;

/** Header the Settings card adds; a cross-site form/fetch cannot forge it without a preflight. */
const GUARD_HEADER = "x-iot-thinking";
const GUARD_VALUE = "1";

/** Bound on one catalog lookup, so a slow adapter cannot pin the card open. */
const CATALOG_TIMEOUT_MS = 8000;

/** Longest failure message echoed to the card; enough to name the cause. */
const MESSAGE_MAX = 240;

/** Longest draft the self-test route accepts, even when maxChars is larger. */
const SELF_TEST_MAX_CHARS = 24000;

/** A self-test is one real model call; give it room, then fail rather than hang. */
const SELF_TEST_TIMEOUT_MS = 60000;

/**
 * A draft the self-test can compress when the card does not supply one. It is
 * ordinary coding-session reasoning: a decision, a rejected route, an open
 * question, and a next step, so every strategy has something to keep.
 */
const SAMPLE_DRAFT = [
  "先看现状：插件是在 llm/stream 上做块级替换，块结束时才压缩，所以客户端会顿一下。",
  "要不要改成边压边回？不行，stream.mjs 已经裁定块结束才是唯一稳定锚点，边压边回会把 delta 顺序搞乱。",
  "另一个方向是直接删掉 reasoningEffort 字段。这样每个路由都能跑，但原生 DeepSeek 路由会丢掉 off 的快路径，压缩器自己开始思考，实测每块多十几秒。",
  "所以默认档位应该是「先试 off，被拒再不带该字段」，并且把路由决策记住，别每块都白试一次。",
  "退化判定也得改：旧判据 duplicate >= 3 把所有重复行都算上，而一段真在写代码的草稿里 ```python 围栏能出现十几次，正常内容会被误判成循环。",
  "新判据只统计长度不小于 20 字的实质行，并要求重复占比够高，或者同一句长行反复到成为草稿主体。",
  "还没定的是设置卡要不要列模型真实档位：宿主 resolveModelInfo 有 reasoning.efforts，但目录查询是异步的，卡片得接受它失败。",
  "接下来先跑 selftest 覆盖解析与降级，再更新 README 的设置项和事件表，最后用真实会话验一遍压缩比。",
].join("\n");

export function sendJson(res, status, body) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(body));
}

/**
 * Whether a request may touch the config bridge.
 *
 * Three independent signals, because each alone is bypassable: the guard header
 * is a preflight barrier for browsers (and a shared secret against a bare
 * curl only in the sense the UI always sends it — it is NOT auth), Origin /
 * Sec-Fetch-Site rejects cross-site documents, and the peer/loopback rule keeps
 * a LAN peer out even when it replays every header it observed.
 *
 * @param {object} req - node IncomingMessage.
 * @returns {{ok: true} | {ok: false, status: number, reason: string}}
 */
export function guardRequest(req) {
  const headers = req.headers ?? {};
  if (headers[GUARD_HEADER] !== GUARD_VALUE) {
    return { ok: false, status: 403, reason: "missing guard header" };
  }

  // Browsers set Sec-Fetch-Site; a cross-site document is cross-site or none.
  const site = headers["sec-fetch-site"];
  if (site !== undefined && site !== "same-origin" && site !== "none") {
    return { ok: false, status: 403, reason: `cross-site request (${site})` };
  }

  // An Origin that is present must match the Host this server answers on.
  const origin = headers.origin;
  if (origin !== undefined && origin !== "null") {
    let originHost;
    try {
      originHost = new URL(origin).host;
    } catch {
      return { ok: false, status: 403, reason: "unparsable origin" };
    }
    if (String(headers.host ?? "") !== originHost) {
      return { ok: false, status: 403, reason: `origin ${originHost} != host ${String(headers.host ?? "")}` };
    }
  } else if (origin === "null") {
    // A sandboxed/opaque origin is a cross-site document by definition.
    return { ok: false, status: 403, reason: "opaque origin" };
  }

  if (process.env.IOT_THINKING_ALLOW_REMOTE !== "1") {
    // 默认只认 loopback 对端（Host 头可伪造）。拿不到 socket 一律拒。
    const remote = req.socket?.remoteAddress;
    const loopback = remote === "127.0.0.1" || remote === "::1" || remote === "::ffff:127.0.0.1";
    if (!loopback) return { ok: false, status: 403, reason: `peer ${String(remote)} is not loopback` };
  }

  return { ok: true };
}

/**
 * Ledger rows as the card needs them: no session ids, no archive paths.
 *
 * `message` is included (trimmed, one line, capped) because a silent failure is
 * what made issue #3 hard to notice: the first symptom was 102 identical
 * `thinking-failed` rows nobody was looking at. The message names the cause
 * ("... does not support reasoning effort \"off\""), never the credential.
 */
export function redactEvents(events) {
  return events.map((entry) => {
    const row = {};
    for (const key of [
      "event",
      "t",
      "index",
      "charsIn",
      "charsOut",
      "chars",
      "ms",
      "reason",
      "strategy",
      "profile",
      "nodes",
      "lines",
      "fragmentRate",
      "retried",
      "fallback",
      "truncated",
      "finish",
      "stage",
      "code",
      "effort",
      "effortFallback",
      "requested",
      "action",
      "detail",
    ]) {
      if (entry[key] !== undefined) row[key] = entry[key];
    }
    if (typeof entry.message === "string") {
      const message = entry.message.replace(/\s+/g, " ").trim().slice(0, MESSAGE_MAX);
      if (message) row.message = message;
    }
    return row;
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let size = 0;
    const chunks = [];
    const fail = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    req.on("data", (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        fail(Object.assign(new Error("request body too large"), { statusCode: 413 }));
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", fail);
  });
}

/**
 * 一个"策略值"：合法就收（策略 id 或 `custom` / `custom:名字`），
 * `null`/空串表示"删掉这一层覆盖"，非法返回 `undefined`（调用方据此 400）。
 */
function strategyValue(value) {
  if (value === null || value === "") return null;
  const entry = normalizeRouteEntry(value);
  if (!entry) return undefined;
  if (entry.id === "custom") return entry.custom ? `custom:${entry.custom}` : "custom";
  return entry.id;
}

/**
 * `{ key: entry | null }` → 一张完整的映射表。
 *
 * ⚠️ 这是**整表替换**，不是并集。并集看起来"更安全"（并发编辑不互相抹掉），
 * 但它让"删除"永远不可能落盘：卡片删掉一行后仍把同一张表 PUT 上来，
 * 并集会把刚删的那行重新合并回去 —— 用户看到的是"点了删除、保存、一刷新又回来了"。
 * 表很小、并发编辑很罕见；删除正确性优先。
 *
 * 值就是档位名（三档基础模式的 id，或 `custom` / `custom:预设名`）。
 * `null` 值表示删这一键（单键增量写法仍可用：只发 `{ presetId: null }` 就给这一键）。
 *
 * @param {object|null} patch - 请求体里的映射表。
 * @returns {object} 校验后的完整表。
 */
function strategyTable(patch) {
  const out = {};
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return out;
  for (const [key, value] of Object.entries(patch)) {
    if (typeof key !== "string" || !key) continue;
    const entry = normalizeRouteEntry(value);
    if (!entry) continue;
    // 自定义预设要保住名字（那是 presetDir 里的文件名）：存成 `custom:名字`。
    if (entry.id === "custom") {
      out[key] = entry.custom ? `custom:${entry.custom}` : "custom";
      continue;
    }
    out[key] = entry.id;
  }
  return out;
}

function sanitize(payload) {
  const next = {};
  if (typeof payload.enabled === "boolean") next.enabled = payload.enabled;
  // 全局默认同样可以是 `custom` / `custom:名字`。
  const globalEntry = normalizeRouteEntry(payload.strategy);
  if (globalEntry) {
    next.strategy = globalEntry.id === "custom"
      ? globalEntry.custom ? `custom:${globalEntry.custom}` : "custom"
      : globalEntry.id;
  }
  if (payload.agentPresets !== undefined) next.agentPresets = strategyTable(payload.agentPresets);
  if (payload.sessions !== undefined) next.sessions = strategyTable(payload.sessions);
  if (payload.strategies !== undefined) next.strategies = payload.strategies;
  if (Number.isFinite(payload.maxChars)) next.maxChars = Math.max(1, Math.floor(payload.maxChars));
  if (typeof payload.archive === "boolean") next.archive = payload.archive;
  if (typeof payload.sessionSwitch === "boolean") next.sessionSwitch = payload.sessionSwitch;
  // 自定义预设三件：目录、贴入的文本、替换规则。
  if (typeof payload.presetDir === "string") next.presetDir = payload.presetDir.trim();
  if (typeof payload.customText === "string") next.customText = payload.customText;
  if (payload.presetAccept === "shorter" || payload.presetAccept === "different") next.presetAccept = payload.presetAccept;
  // 未知档位不落盘；null 表示 auto。

  const effort = normalizeEffort(payload.reasoningEffort);
  if (effort !== null) next.reasoningEffort = effort;
  for (const key of ["provider", "model"]) {
    if (payload[key] === null) next[key] = null;
    else if (typeof payload[key] === "string") next[key] = payload[key].trim() || null;
  }
  return next;
}

/**
 * 逐策略覆盖的写入：`{ "<strategy id>": { minChars, ... } }`。
 *
 * 与顶层字段分开走：顶层 `sanitize` 只认全局项，逐策略的限制是策略相关的，
 * 在这里校验"键是已知策略、值是对象"，数值字段的合法性留给 engine 兜（它本来就会）。
 */
function sanitizeStrategies(payload, current) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return current;
  const out = { ...(current && typeof current === "object" ? current : {}) };
  for (const [id, overrides] of Object.entries(payload)) {
    if (!strategyById(id)) continue;
    if (overrides === null) {
      delete out[id];
      continue;
    }
    if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) continue;
    const next = { ...(out[id] ?? {}) };
    for (const [key, value] of Object.entries(overrides)) {
      if (!Number.isFinite(value)) continue;
      next[key] = Math.max(key === "temperature" || key === "minRatio" ? 0 : 0, value);
    }
    out[id] = next;
  }
  return out;
}

/** Reject after `ms` without leaving the timer holding the process open. */
function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms} ms`)), ms);
    timer.unref?.();
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/** One catalog description, tolerant of adapters that answer with less. */
function describeModel(model) {
  const id = typeof model?.id === "string" ? model.id : "";
  if (!id) return null;
  return {
    id,
    name: typeof model?.name === "string" && model.name ? model.name : id,
    ...(typeof model?.description === "string" && model.description ? { description: model.description } : {}),
  };
}

function describeEfforts(reasoning) {
  if (!reasoning || !Array.isArray(reasoning.efforts)) return null;
  const efforts = reasoning.efforts
    .filter((effort) => effort && typeof effort.id === "string")
    .map((effort) => ({
      id: effort.id,
      name: typeof effort.name === "string" && effort.name ? effort.name : effort.id,
      ...(typeof effort.description === "string" && effort.description ? { description: effort.description } : {}),
    }));
  if (efforts.length === 0) return null;
  return {
    efforts,
    ...(typeof reasoning.defaultEffort === "string" ? { defaultEffort: reasoning.defaultEffort } : {}),
  };
}

/**
 * 两级值域。第一级是档位（三档 + 自定义预设 + 关闭），第二级只在选到自定义预设
 * 时才有意义（具体用哪份文本）。不把每个预设文件铺进第一级 —— 那会让"档位"这个
 * 选择题变成一长串文件名。
 */
function valueChoices(cfg = {}) {
  const listing = listPresets(cfg.presetDir);
  const tiers = strategyCatalog().map((entry) => ({
    value: entry.id,
    label: entry.label,
    family: entry.family,
    isCustom: entry.family === "custom",
  }));
  const presets = [];
  if (String(cfg.customText ?? "").trim()) {
    presets.push({ value: "custom", label: "贴入的文本", bytes: null });
  }
  for (const preset of listing.presets) {
    presets.push({ value: `custom:${preset.name}`, label: preset.name, bytes: preset.bytes });
  }
  return { tiers, presets, presetDirSet: listing.configured };
}

/** 会话 id → agent preset（会话投影里的值）。取不到返回 undefined。 */
function agentPresetOf(ctx, sessionId) {
  if (!sessionId) return undefined;
  try {
    const session = ctx.get?.("sessions")?.get?.(sessionId);
    if (!session) return undefined;
    const preset = ctx.get?.("sessionProjections")?.stateOf?.(session, "agentPreset");
    return typeof preset === "string" && preset ? preset : undefined;
  } catch {
    return undefined;
  }
}

/** 设置卡与头部下拉都要的一份"可选项"。 */
function catalogPayload(store) {
  const cfg = store?.load?.() ?? {};
  const listing = listPresets(cfg.presetDir);
  return {
    strategies: strategyCatalog(),
    ids: strategyIds(),
    // 自定义预设（第 4 项）：目录、目录里有什么、贴入的那段文本在不在。
    presetDirSet: listing.configured,
    presetFiles: listing.presets,
    hasCustomText: typeof cfg.customText === "string" && cfg.customText.trim().length > 0,
    presetAccept: cfg.presetAccept === "shorter" ? "shorter" : "different",
    /** 所有下拉共用的一份"可选值"。 */
    choices: valueChoices(cfg),
  };
}

/**
 * 一条策略的"原文"：用它自己的 prepare + buildPrompt 渲染一次，
 * 把**真正会发给模型的那段提示词**摊开给用户看。
 *
 * 这不是把代码里的常量贴出来（那样看到的和实际发出的可能不一样：
 * 三档的标签集合、节点预算、准入判据，以及自定义预设的整段文本，
 * 全在这段提示词里）。
 *
 * @param {{id: string}} wanted - 档位 id。
 * @returns {{ok: true, prompt: string} | {ok: false, error: string}}
 */
function renderStrategyPrompt(wanted, preset = null) {
  const strategy = strategyById(wanted.id);
  if (!strategy) return { ok: false, error: `unknown strategy: ${wanted.id}` };
  const limits = limitsMerge(strategy.id, {});
  // 自定义预设：把"哪份内容"塞进 limits，与真实请求走同一个 prepare。
  if (preset) limits.preset = preset;
  try {
    const prepared = strategy.prepare(limits);
    if (prepared?.error) return { ok: false, error: prepared.error };
    const prompt = strategy.buildPrompt(SAMPLE_DRAFT, {
      strict: false,
      reason: null,
      detail: null,
      limits,
      state: prepared?.state ?? null,
    });
    return {
      ok: true,
      prompt: String(prompt ?? ""),
    };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) };
  }
}

/** 只算限制，不读整份配置（预览用）。 */
function limitsMerge(id, overrides) {
  const strategy = strategyById(id);
  if (!strategy) return {};
  return { ...strategy.defaults, ...(overrides ?? {}) };
}

function doRegister(ctx, ws, store, deps = {}) {
  const config = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking config api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    try {
      if (req.method === "GET") {
        // `path` is the user's home directory under $DSH_HOME: keep it out of the
        // wire format (F2), the card never rendered it.
        const { path: _omit, ...safe } = store.load();
        sendJson(res, 200, { config: safe, ...catalogPayload(store) });
        return;
      }
      if (req.method === "PUT") {
        let text;
        try {
          text = await readBody(req);
        } catch (error) {
          // An oversized body is not a server fault; report what actually happened.
          if (error?.statusCode === 413) {
            sendJson(res, 413, { error: "request body too large" });
            return;
          }
          throw error;
        }
        let payload;
        try {
          payload = JSON.parse(text);
        } catch {
          sendJson(res, 400, { error: "body must be valid JSON" });
          return;
        }
        if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
          sendJson(res, 400, { error: "body must be a JSON object" });
          return;
        }
        const current = store.load();
        const patch = sanitize(payload);
        // 映射表按**整表替换**落盘：卡片发上来的就是它看到的全表，
        // 所以"删掉一行 / 清空所有会话覆盖"才能真的落盘（见 strategyTable 的说明）。
        if (payload.agentPresets !== undefined) patch.agentPresets = strategyTable(payload.agentPresets);
        if (payload.sessions !== undefined) patch.sessions = strategyTable(payload.sessions);
        if (payload.strategies !== undefined) patch.strategies = sanitizeStrategies(payload.strategies, current.strategies);
        const saved = store.save(patch);
        const { path: _omit, ...safe } = saved;
        sendJson(res, 200, { ok: true, config: safe });
        return;
      }
      sendJson(res, 405, { error: "method not allowed" });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      // Log the reason, not the request body: it can hold a provider/model the
      // attacker chose, and the operator only needs to know the write failed.
      ctx.logger?.warn?.(`iot-thinking config api: ${message}`);
      sendJson(res, 500, { error: message });
    }
  };

  const status = (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking status api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    try {
      const cfg = store.load();
      const { path: _omit, ...safe } = cfg;
      const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");
      const sessionId = url.searchParams.get("sessionId");
      const preset = url.searchParams.get("agentPreset");
      // 卡片要能回答"为什么这个会话现在用的是这一档"：把解析结果与来源一起回。
      const resolved = sessionId || preset ? resolveConfig(cfg, { sessionId, agentPreset: preset }).route : null;
      sendJson(res, 200, { config: safe, events: redactEvents(tailEvents(25)), ...catalogPayload(store), resolved });
    } catch (error) {
      sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  /** 会话级读写：头部下拉与 /iot 命令共用的那一层。 */
  const session = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking session api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    try {
      if (req.method === "GET") {
        const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");
        const sessionId = url.searchParams.get("sessionId") ?? "";
        // 前端不一定知道 preset（新会话尤其），所以按会话自己查一次。
        const agentPreset = url.searchParams.get("agentPreset") ?? agentPresetOf(ctx, sessionId);
        const cfg = store.load();
        const effective = resolveConfig(cfg, { sessionId, agentPreset });
        // 「跟随 preset 默认」要显示的是**没有本会话覆盖时**的结果，
        // 否则那句标签会把覆盖值说成 preset 默认值。
        const presetDefault = resolveConfig(cfg, { agentPreset }).route;
        sendJson(res, 200, {
          sessionId,
          agentPreset: agentPreset ?? null,
          route: effective.route,
          presetDefault: { id: presetDefault.id, source: presetDefault.source, key: presetDefault.key ?? null },
          enabled: effective.enabled,
          /** 头部下拉是否显示（默认关，卡片里可开）。 */
          sessionSwitch: cfg.sessionSwitch === true,
          strategies: strategyCatalog(),
          /** 头部下拉用这一份：内置档位 + 目录里的自定义预设 + 贴入的文本。 */
          choices: valueChoices(cfg),
          /** 这个会话自己那条覆盖（没配就是 null）。 */
          override: cfg.sessions?.[sessionId] ?? null,
          /** 它所属 preset 的映射（没配就是 null）。 */
          presetStrategy: agentPreset ? cfg.agentPresets?.[agentPreset] ?? null : null,
        });
        return;
      }
      if (req.method === "PUT") {
        let payload;
        try {
          const text = await readBody(req);
          payload = JSON.parse(text);
        } catch (error) {
          if (error?.statusCode === 413) {
            sendJson(res, 413, { error: "request body too large" });
            return;
          }
          sendJson(res, 400, { error: "body must be valid JSON" });
          return;
        }
        if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
          sendJson(res, 400, { error: "body must be a JSON object" });
          return;
        }
        const sessionId = String(payload.sessionId ?? "").trim();
        if (!sessionId) {
          sendJson(res, 400, { error: "sessionId is required" });
          return;
        }
        // `strategy` 是档位名（或 `custom[:预设名]`），`null` 表示清除这层覆盖。
        const cleared = payload.strategy === null || payload.strategy === "";
        const entry = cleared ? null : normalizeRouteEntry(payload.strategy);
        if (!cleared && !entry) {
          sendJson(res, 400, { error: `unknown strategy: ${JSON.stringify(payload.strategy)}` });
          return;
        }
        const cfg = store.load();
        const sessions = { ...(cfg.sessions ?? {}) };
        // `null` = 回到"跟随 preset / 全局默认"，这正是"清除本会话覆盖"的语义。
        if (!entry) delete sessions[sessionId];
        else if (entry.id === "custom") sessions[sessionId] = entry.custom ? `custom:${entry.custom}` : "custom";
        else sessions[sessionId] = entry.id;
        store.save({ sessions });
        // 前端不一定传 preset；查不到就当"少一层映射"，不影响会话覆盖生效。
        const preset = payload.agentPreset ?? agentPresetOf(ctx, sessionId);
        const reloaded = store.load();
        const effective = resolveConfig(reloaded, { sessionId, agentPreset: preset });
        const presetDefault = resolveConfig(reloaded, { agentPreset: preset }).route;
        sendJson(res, 200, {
          ok: true,
          sessionId,
          route: effective.route,
          presetDefault: { id: presetDefault.id, source: presetDefault.source, key: presetDefault.key ?? null },
          enabled: effective.enabled,
          override: sessions[sessionId] ?? null,
        });
        return;
      }
      sendJson(res, 405, { error: "method not allowed" });
    } catch (error) {
      sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  /**
   * Preset 清单（list）与单条组成（detail）。
   *
   * 为什么要有这条：设置卡里那张"按 preset 映射"的表如果只能手输 preset id，
   * 用户就得知道 `standard` / `jingjing` / `local-slim` 这些名字从哪来 ——
   * 而宿主本来就有清单（`agentPresets.list()`）和组成（`compositionInventory()`）。
   *
   * 返回里的 `available: false` 是**正常情况**（headless profile 不挂 preset 服务），
   * 不是错误：卡片会退化成"手输 id"。
   */
  const presets = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking presets api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    try {
      const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");
      const detailOf = String(url.searchParams.get("detail") ?? "").trim();
      // `agentPresets` 不是本插件的注入依赖（headless 里根本没有），所以只能试探着拿：
      // 拿不到是正常情况，卡片会退化成"手输 id"。
      let registry;
      try {
        registry = (typeof ctx.get === "function" ? ctx.get("agentPresets") : undefined) ?? ctx.agentPresets;
      } catch {
        registry = undefined;
      }
      if (!registry || typeof registry.list !== "function") {
        sendJson(res, 200, { available: false, presets: [], mapped: {}, reason: "no-agentPresets-service" });
        return;
      }

      const rows = await withTimeout(registry.list(), CATALOG_TIMEOUT_MS, "agentPresets.list");
      const defaultId = typeof registry.defaultId === "string" ? registry.defaultId : null;
      const cfg = store.load();
      const mapped = cfg.agentPresets ?? {};
      const known = new Set(rows.map((row) => row.id));

      let detail = null;
      if (detailOf) {
        try {
          // 组成清单：每行是一个插件行（moduleName + 该行是否启用/是否加载失败）。
          // 它比整份 YAML 更适合展示 —— 用户问的是"这个 preset 到底是什么"，
          // 而不是"它的配置原文长什么样"。
          if (typeof registry.compositionInventory === "function") {
            const inventory = await withTimeout(registry.compositionInventory(), CATALOG_TIMEOUT_MS, "compositionInventory");
            const found = inventory.find((entry) => entry.id === detailOf);
            if (found) {
              detail = {
                id: found.id,
                name: found.name ?? null,
                description: found.description ?? null,
                isDefault: found.isDefault === true,
                broken: found.broken ?? null,
                rows: (found.rows ?? []).map((row) => ({
                  entryId: String(row.entryId ?? ""),
                  moduleName: String(row.moduleName ?? ""),
                  enabled: row.enabled !== false,
                  ...(row.condition ? { condition: String(row.condition) } : {}),
                })),
              };
            }
          }
        } catch (error) {
          ctx.logger?.warn?.(`iot-thinking presets api: detail failed: ${String(error?.message ?? error)}`);
        }
      }

      sendJson(res, 200, {
        available: true,
        defaultPreset: defaultId,
        presets: rows.map((row) => ({
          id: row.id,
          name: row.name ?? null,
          description: row.description ?? null,
          isDefault: row.id === defaultId,
          broken: row.broken ?? null,
          /** 这个 preset 在配置里被映射到哪个策略；没映射就是 null。 */
          mappedTo: mapped[row.id] ?? null,
        })),
        // 配置里映射了、但当前注册表里不存在的 preset：多半是 preset 被删/改名了。
        // 卡片要把它们显示出来并标红，否则用户只看到"我的映射不见了"。
        orphaned: Object.keys(mapped).filter((id) => !known.has(id)),
        detail,
      });
    } catch (error) {
      sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  /**
   * 一条策略的详情：声明 + **真正会发给模型的提示词原文**。
   *
   * 用户问"这两个到底做什么"，只给 label 是不够的 —— 决策档与 research 的差别
   * 全在提示词里，角色档还会把规格文件的内容拼进去。所以这里直接渲染一次。
   */
  const strategyDetail = (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking strategy api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    try {
      const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");
      const id = String(url.searchParams.get("id") ?? "").trim();
      // 自定义预设（第 4 项）：`?id=custom&preset=<名字>` 显示目录里那份，
      // 不传 preset 就显示贴进来的那段文本。
      if (id === "custom") {
        const presetName = String(url.searchParams.get("preset") ?? "").trim();
        const cfg = store.load();
        const source = presetName
          ? readPreset(cfg.presetDir, presetName)
          : { text: String(cfg.customText ?? ""), error: String(cfg.customText ?? "").trim() ? null : "还没贴入任何文本" };
        const strategy = strategyById("custom");
        const rendered = source.text
          ? renderStrategyPrompt({ id: "custom" }, { preset: presetName ? { name: presetName, dir } : { inline: source.text } })
          : { ok: false, error: source.error };
        sendJson(res, 200, {
          id: "custom",
          label: strategy.label,
          summary: strategy.summary,
          family: strategy.family,
          accept: cfg.presetAccept === "shorter" ? "shorter" : "different",
          defaults: { ...strategy.defaults },
          preset: presetName || null,
          presetText: source.text,
          prompt: rendered.ok ? rendered.prompt : null,
          ...(rendered.ok ? {} : { error: rendered.error }),
          presetFiles: listPresets(cfg.presetDir).presets,
        });
        return;
      }
      const strategy = strategyById(id);
      if (!strategy) {
        sendJson(res, 404, { error: `unknown strategy: ${id}`, available: strategyIds() });
        return;
      }
      const rendered = renderStrategyPrompt({ id });
      sendJson(res, 200, {
        id: strategy.id,
        label: strategy.label,
        summary: strategy.summary,
        family: strategy.family,
        accept: strategy.accept,
        defaults: { ...strategy.defaults },
        prompt: rendered.ok ? rendered.prompt : null,
        ...(rendered.ok ? {} : { error: rendered.error }),
      });
    } catch (error) {
      sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  /** 预设目录：列出 / 读内容 / 另存为。目录必须显式配置，不设默认值。 */
  const presetDir = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking preset-dir api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    try {
      const cfg = store.load();
      const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");

      if (req.method === "GET") {
        const wanted = url.searchParams.get("read");
        if (wanted) {
          const loaded = readPreset(cfg.presetDir, wanted);
          sendJson(res, 200, { name: wanted, text: loaded.text, ...(loaded.error ? { error: loaded.error } : {}) });
          return;
        }
        const listing = listPresets(cfg.presetDir);
        sendJson(res, 200, { configured: listing.configured, presets: listing.presets });
        return;
      }

      if (req.method === "POST") {
        let payload;
        try {
          payload = JSON.parse(await readBody(req));
        } catch (error) {
          if (error?.statusCode === 413) {
            sendJson(res, 413, { error: "request body too large" });
            return;
          }
          sendJson(res, 400, { error: "body must be valid JSON" });
          return;
        }
        if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
          sendJson(res, 400, { error: "body must be a JSON object" });
          return;
        }
        const written = writePreset(cfg.presetDir, payload.name, payload.text);
        if (!written.ok) {
          sendJson(res, 400, { error: written.error });
          return;
        }
        sendJson(res, 200, { ok: true, name: written.name, presets: listPresets(cfg.presetDir).presets });
        return;
      }

      sendJson(res, 405, { error: "method not allowed" });
    } catch (error) {
      sendJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  // The card used to require typing provider/model by hand, which invited
  // nonexistent routes and, with the old hardcoded "off", an all-failure loop
  // nobody could see. This route answers the same catalog the Models page uses,
  // plus the one thing the card needs before offering levels: the exact route's
  // advertised reasoning efforts.
  const models = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking models api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    // `ctx.llm` is the service accessor; `ctx.get` covers the child fiber this
    // route may be registered on (web profiles mount webServer later).
    const llm = (typeof ctx.get === "function" ? ctx.get("llm") : undefined) ?? ctx.llm;
    const url = new URL(String(req.url ?? "/"), "http://127.0.0.1");
    const provider = String(url.searchParams.get("provider") ?? "").trim();
    const model = String(url.searchParams.get("model") ?? "").trim();

    let providers = [];
    try {
      providers = (llm?.listProviders?.() ?? [])
        .map((entry) => describeModel(entry))
        .filter(Boolean);
    } catch (error) {
      ctx.logger?.warn?.(`iot-thinking models api: listProviders failed: ${String(error?.message ?? error)}`);
    }

    let models = [];
    let catalogError;
    if (provider && typeof llm?.listModels === "function") {
      try {
        const listed = await withTimeout(llm.listModels(provider), CATALOG_TIMEOUT_MS, "listModels");
        models = (listed ?? []).map((entry) => describeModel(entry)).filter(Boolean);
      } catch (error) {
        catalogError = String(error?.message ?? error);
      }
    }

    let reasoning = null;
    let reasoningError;
    if (provider && model && typeof llm?.resolveModelInfo === "function") {
      try {
        const info = await withTimeout(llm.resolveModelInfo(provider, model), CATALOG_TIMEOUT_MS, "resolveModelInfo");
        reasoning = describeEfforts(info?.reasoning);
      } catch (error) {
        reasoningError = String(error?.message ?? error);
      }
    }

    sendJson(res, 200, {
      providers,
      models,
      reasoning,
      ...(catalogError === undefined ? {} : { catalogError }),
      ...(reasoningError === undefined ? {} : { reasoningError }),
    });
  };

  // One real rewrite call on demand. It runs the same compressThinking the
  // stream hook runs and applies the same acceptance rule the stream applies —
  // a self-test that only checks for a non-empty reply reports green on a
  // rewrite the real link would never substitute.
  const test = async (req, res) => {
    const gate = guardRequest(req);
    if (!gate.ok) {
      ctx.logger?.warn?.(`iot-thinking self-test api refused: ${gate.reason}`);
      sendJson(res, gate.status, { error: "forbidden" });
      return;
    }
    if (req.method !== "POST") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    if (typeof deps.rewrite !== "function") {
      sendJson(res, 503, { error: "改写器未接线，无法自测" });
      return;
    }

    let payload = {};
    try {
      const text = await readBody(req);
      if (text.trim()) payload = JSON.parse(text);
    } catch (error) {
      // A body over the cap is not a JSON syntax problem; keep its status.
      if (error?.statusCode === 413) {
        sendJson(res, 413, { error: "request body too large" });
        return;
      }
      sendJson(res, 400, { error: "body must be valid JSON" });
      return;
    }
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
      sendJson(res, 400, { error: "body must be a JSON object" });
      return;
    }

    // 自测可以针对任一策略：默认测"全局默认那一档"，也可以点名测某档
    // （改档位之前先验一次，比改完回会话里试便宜）。
    const wanted = strategyValue(payload.strategy ?? null);
    const cfgNow = store.load();
    const requestedId = typeof wanted === "string" ? wanted : null;
    const scoped = requestedId
      ? resolveConfig({ ...cfgNow, strategy: requestedId }, { sessionId: payload.sessionId ?? undefined, agentPreset: payload.agentPreset ?? undefined })
      : resolveConfig(cfgNow, { sessionId: payload.sessionId ?? undefined, agentPreset: payload.agentPreset ?? undefined });
    const cfg = scoped;
    const strategy = strategyById(cfg.strategy);

    // The card's route fields may be blank ("follow the current request"), and a
    // test has no request to follow. index.mjs records the last agent-loop route
    // for exactly this case.
    const lastRoute = typeof deps.getRoute === "function" ? deps.getRoute() : null;
    const provider = cfg.provider || lastRoute?.provider || null;
    const model = cfg.model || lastRoute?.model || null;
    if (!provider || !model) {
      sendJson(res, 409, {
        ok: false,
        error: "还没有可用的改写路由：在卡片里选一个 provider/model，或先在本会话发一条消息（插件会记住当前请求路由）。",
      });
      return;
    }

    const draft = typeof payload.text === "string" && payload.text.trim() ? payload.text : SAMPLE_DRAFT;
    const maxChars = Number.isFinite(cfg.maxChars) ? Math.max(1, cfg.maxChars) : 24000;
    const minChars = Number.isFinite(cfg.minChars) ? Math.max(0, cfg.minChars) : 400;
    const input = draft.slice(0, Math.min(maxChars, SELF_TEST_MAX_CHARS));
    const started = Date.now();

    // A test that reports green on input the real link would skip is the false
    // green this route exists to prevent, so the same gates apply up front.
    // 先问策略（为什么这段不该跑），再问长度闸 —— 两者的原因不一样，
    // 合并成一个 "below-min-chars" 会让卡片撒谎。
    const skipped = strategy.skipInput?.(input) ?? null;
    let why = null;
    if (cfg.enabled === false) why = "disabled";
    else if (skipped) why = skipped;
    else if (input.length < minChars) why = "below-min-chars";
    else if (input.length > maxChars) why = "above-max-chars";
    if (why !== null) {
      sendJson(res, 200, {
        ok: false,
        replaced: false,
        reason: why,
        strategy: cfg.strategy,
        provider,
        model,
        charsIn: input.length,
        charsOut: 0,
        text: null,
        minChars,
        maxChars,
        ms: Date.now() - started,
      });
      return;
    }

    let result = null;
    try {
      result = await withTimeout(
        deps.rewrite(input, { provider, model, sessionId: "iot-thinking-self-test" }, { ...cfg, archive: false }, 0, {}),
        SELF_TEST_TIMEOUT_MS,
        "self-test",
      );
    } catch (error) {
      sendJson(res, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
      return;
    }

    // `null` means "keep the original": a failed call, an aborted one, a
    // degenerate draft, or an output that lost the quality gate. Say which by
    // naming the latest failure row from *this* run instead of claiming
    // "not shorter" (the card would otherwise report "N → 0 字").
    const since = new Date(started).toISOString();
    const lastFailure =
      result === null
        ? tailEvents(8)
            .filter((entry) => entry.event === "thinking-failed" && typeof entry.t === "string" && entry.t >= since)
            .pop()
        : null;
    const candidate = typeof result === "string" ? result : result?.text;
    const text = typeof candidate === "string" ? candidate.trim() : "";
    // The same rule stream.mjs applies before it may substitute a block.
    const accepted = strategy.accepts(input, candidate, cfg.limits);
    const reason = accepted
      ? null
      : result === null
        ? "compressor-failed"
        : text.length === 0
          ? "empty-output"
          : `not-${strategy.accept}`;
    const response = {
      ok: accepted,
      replaced: accepted,
      reason,
      strategy: cfg.strategy,
      provider,
      model,
      charsIn: input.length,
      charsOut: text.length,
      text: text || null,
      effort: result?.event?.effort ?? null,
      effortFallback: result?.event?.effortFallback === true,
      minChars,
      maxChars,
      ms: Date.now() - started,
      ...(lastFailure?.message ? { error: String(lastFailure.message) } : {}),
      event: result?.event
        ? {
            strategy: result.event.strategy,
            finish: result.event.finish,
            retried: result.event.retried,
            fallback: result.event.fallback,
          }
        : null,
    };
    deps.mark?.("thinking-self-test", {
      ok: accepted,
      strategy: cfg.strategy,
      provider,
      model,
      charsIn: response.charsIn,
      charsOut: response.charsOut,
      reason: response.reason,
      effort: response.effort,
      ms: response.ms,
    });
    sendJson(res, 200, response);
  };

  const disposers = [];
  try {
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/config", handler: config }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/status", handler: status }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/models", handler: models }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/test", handler: test }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/session", handler: session }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/presets", handler: presets }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/strategy", handler: strategyDetail }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/preset-dir", handler: presetDir }));
  } catch (error) {
    ctx.logger?.warn?.(`iot-thinking config api route register failed: ${String(error?.message ?? error)}`);
    return false;
  }
  ctx.effect(() => () => {
    for (const dispose of disposers) {
      try {
        dispose();
      } catch {
        /* already gone */
      }
    }
  });
  return true;
}

/**
 * Register the routes on ctx.webServer. webServer may mount after this plugin
 * (the web-app rows come later), so inject a waiting child fiber.
 *
 * @param {object} ctx - plugin context.
 * @param {object} store - ConfigStore.
 * @param {{rewrite?: Function, mark?: Function, getRoute?: () => ({provider: string|null, model: string|null}|null)}} [deps]
 *   host-side collaborators: the rewriter for the self-test route, the ledger
 *   writer, and the last agent-loop route the self-test can fall back to.
 */
export function registerConfigApi(ctx, store, deps = {}) {
  const ws = ctx.get("webServer");
  if (ws !== undefined && typeof ws.register === "function") return doRegister(ctx, ws, store, deps);
  ctx.inject({ webServer: null }, (ready) => {
    const server = ready.get("webServer");
    if (server !== undefined && typeof server.register === "function") doRegister(ready, server, store, deps);
  });
  return false;
}
