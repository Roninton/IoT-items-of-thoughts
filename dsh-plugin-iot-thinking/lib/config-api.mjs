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

import { tailEvents } from "./ledger.mjs";
import { PROFILES } from "./think.mjs";

const MAX_BODY_BYTES = 256 * 1024;

/** Header the Settings card adds; a cross-site form/fetch cannot forge it without a preflight. */
const GUARD_HEADER = "x-iot-thinking";
const GUARD_VALUE = "1";

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

/** Ledger rows as the card needs them: no session ids, no archive paths. */
export function redactEvents(events) {
  return events.map((entry) => {
    const row = {};
    for (const key of ["event", "t", "index", "charsIn", "charsOut", "chars", "ms", "reason", "profile", "retried", "fallback", "truncated", "finish"]) {
      if (entry[key] !== undefined) row[key] = entry[key];
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
  for (const key of ["provider", "model"]) {
    if (payload[key] === null) next[key] = null;
    else if (typeof payload[key] === "string") next[key] = payload[key].trim() || null;
  }
  return next;
}

function doRegister(ctx, ws, store) {
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
        const text = await readBody(req);
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

  const disposers = [];
  try {
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/config", handler: config }));
    disposers.push(ws.register({ kind: "exact", path: "/iot-thinking/api/status", handler: status }));
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
 */
export function registerConfigApi(ctx, store) {
  const ws = ctx.get("webServer");
  if (ws !== undefined && typeof ws.register === "function") return doRegister(ctx, ws, store);
  ctx.inject({ webServer: null }, (ready) => {
    const server = ready.get("webServer");
    if (server !== undefined && typeof server.register === "function") doRegister(ready, server, store);
  });
  return false;
}
