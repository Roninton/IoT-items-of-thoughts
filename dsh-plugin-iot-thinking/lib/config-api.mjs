// lib/config-api.mjs — browser bridge for the Settings card.
//
// Static client bundles cannot extend the typert remote catalog, so the plugin
// uses the same channel as context-memory: an exact HTTP route on ctx.webServer
// plus a same-origin fetch from the client half. dsh web binds to 127.0.0.1.

import { tailEvents } from "./ledger.mjs";

const MAX_BODY_BYTES = 256 * 1024;

function sendJson(res, status, body) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(body));
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
    try {
      if (req.method === "GET") {
        sendJson(res, 200, { config: store.load(), path: store.path });
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
      ctx.logger?.warn?.(`iot-thinking config api: ${message}`);
      sendJson(res, 500, { error: message });
    }
  };

  const status = (req, res) => {
    if (req.method !== "GET") {
      sendJson(res, 405, { error: "method not allowed" });
      return;
    }
    try {
      sendJson(res, 200, { config: store.load(), path: store.path, events: tailEvents(25) });
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
