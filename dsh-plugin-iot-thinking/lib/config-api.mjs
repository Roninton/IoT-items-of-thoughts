// lib/config-api.mjs — browser bridge for the Settings card.
//
// Static client bundles cannot extend the typert remote catalog, so the plugin
// uses the same channel as context-memory: an exact HTTP route on ctx.webServer
// plus a same-origin fetch from the client half. dsh web binds to 127.0.0.1.
//
// THREAT MODEL (review #01a153e4-75e4, "harmful"): these routes are reachable
// from anything that can speak HTTP to the port, and nothing upstream
// authenticates them — the host webserver only keeps a route table. So the
// handlers guard themselves:
//   * a custom request header the UI sets and a cross-site page cannot set
//     without a preflight this server refuses (no CORS approval is ever sent);
//   * same-origin checks on Origin / Sec-Fetch-Site / Host so a DNS-rebinding
//     page (same origin after rebind) and a LAN peer are both refused;
//   * loopback and same-host peers only, unless the operator set IOT_THINKING_ALLOW_REMOTE=1.
// Without this, an unauthenticated PUT rewrites provider/model and every
// reasoning block is streamed to the attacker's endpoint (F1), and GET leaks
// the config path plus session ids from the ledger (F2).

import { normalizeEffort } from "./effort.mjs";
import { tailEvents } from "./ledger.mjs";
import { isShorterReplacement } from "./stream.mjs";
import { PROFILES } from "./think.mjs";

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
 * question, and a next step, so every profile has something to keep.
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
    // The default Web composition binds 127.0.0.1, so a loopback peer is the
    // UI. A LAN-bound composition (dsh-lan-access) deliberately exposes the
    // card to the network; the operator opts into that with
    // IOT_THINKING_ALLOW_REMOTE=1 rather than this code trusting a Host header
    // (which any client can forge). An absent socket proves nothing, so it is
    // refused rather than treated as loopback.
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
      "profile",
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

function sanitize(payload) {
  const next = {};
  if (typeof payload.enabled === "boolean") next.enabled = payload.enabled;
  if (typeof payload.profile === "string" && Object.hasOwn(PROFILES, payload.profile)) next.profile = payload.profile;
  if (Number.isFinite(payload.minChars)) next.minChars = Math.max(0, Math.floor(payload.minChars));
  if (Number.isFinite(payload.maxChars)) next.maxChars = Math.max(1, Math.floor(payload.maxChars));
  if (Number.isFinite(payload.maxTokens)) next.maxTokens = Math.max(1, Math.floor(payload.maxTokens));
  if (Number.isFinite(payload.maxNodes)) next.maxNodes = Math.max(1, Math.floor(payload.maxNodes));
  if (Number.isFinite(payload.maxNeutral)) next.maxNeutral = Math.max(0, Math.floor(payload.maxNeutral));
  if (typeof payload.archive === "boolean") next.archive = payload.archive;
  // An unknown level is dropped rather than saved, so the card can never write a
  // value the compressor would have to guess about. `null` means auto.
  const effort = normalizeEffort(payload.reasoningEffort);
  if (effort !== null) next.reasoningEffort = effort;
  for (const key of ["provider", "model"]) {
    if (payload[key] === null) next[key] = null;
    else if (typeof payload[key] === "string") next[key] = payload[key].trim() || null;
  }
  return next;
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
        sendJson(res, 200, { config: safe });
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
        const saved = store.save(sanitize(payload));
        sendJson(res, 200, { ok: true, config: saved });
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
      const { path: _omit, ...safe } = store.load();
      sendJson(res, 200, { config: safe, events: redactEvents(tailEvents(25)) });
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

  // One real compressor call on demand. It runs the same compressThinking the
  // stream hook runs and applies the same "must be strictly shorter" rule
  // stream.mjs enforces — a self-test that only checks for a non-empty reply
  // reports green on a compression that would never be substituted.
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
    if (typeof deps.compress !== "function") {
      sendJson(res, 503, { error: "压缩器未接线，无法自测" });
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

    const cfg = store.load();
    // The card's route fields may be blank ("follow the current request"), and a
    // test has no request to follow. index.mjs records the last agent-loop route
    // for exactly this case.
    const lastRoute = typeof deps.getRoute === "function" ? deps.getRoute() : null;
    const provider = cfg.provider || lastRoute?.provider || null;
    const model = cfg.model || lastRoute?.model || null;
    if (!provider || !model) {
      sendJson(res, 409, {
        ok: false,
        error: "还没有可用的压缩路由：在卡片里选一个 provider/model，或先在本会话发一条消息（插件会记住当前请求路由）。",
      });
      return;
    }

    const draft = typeof payload.text === "string" && payload.text.trim() ? payload.text : SAMPLE_DRAFT;
    const maxChars = Number.isFinite(cfg.maxChars) ? Math.max(1, cfg.maxChars) : 24000;
    const minChars = Number.isFinite(cfg.minChars) ? Math.max(0, cfg.minChars) : 400;
    const input = draft.slice(0, Math.min(maxChars, SELF_TEST_MAX_CHARS));
    const started = Date.now();

    // A test that reports green on input the real link would skip is the false
    // green this route exists to prevent, so the same two gates apply up front.
    let why = null;
    if (cfg.enabled === false) why = "disabled";
    else if (input.length < minChars) why = "below-min-chars";
    else if (input.length > maxChars) why = "above-max-chars";
    if (why !== null) {
      sendJson(res, 200, {
        ok: false,
        replaced: false,
        reason: why,
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
        deps.compress(input, { provider, model, sessionId: "iot-thinking-self-test" }, { ...cfg, archive: false }, 0),
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
    const replaced = isShorterReplacement(input, candidate);
    const reason = replaced
      ? null
      : result === null
        ? "compressor-failed"
        : text.length === 0
          ? "empty-output"
          : "not-shorter";
    const response = {
      ok: replaced,
      replaced,
      reason,
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
            profile: result.event.profile,
            finish: result.event.finish,
            retried: result.event.retried,
            fallback: result.event.fallback,
          }
        : null,
    };
    deps.mark?.("thinking-self-test", {
      ok: replaced,
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
 * @param {{compress?: Function, mark?: Function, getRoute?: () => ({provider: string|null, model: string|null}|null)}} [deps]
 *   host-side collaborators: the compressor for the self-test route, the ledger
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
