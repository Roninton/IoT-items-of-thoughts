// 账本 + 原文归档。插件没有别的可靠证据：替换后原文只存在于 originals/。

import { appendFileSync, mkdirSync, writeFileSync, readFileSync, existsSync, renameSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

function dataDir() {
  if (process.env.IOT_THINKING_DATA_DIR) return process.env.IOT_THINKING_DATA_DIR;
  const home = process.env.DSH_HOME || join(homedir(), ".dsh");
  return join(home, "plugin-data", "iot-thinking");
}

function safeId(value) {
  return String(value ?? "unknown").replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 160) || "unknown";
}

/** Append one JSON line to the plugin ledger. Never throws. */
export function mark(event, payload = {}) {
  try {
    const dir = dataDir();
    mkdirSync(dir, { recursive: true });
    appendFileSync(
      join(dir, "events.jsonl"),
      `${JSON.stringify({ event, t: new Date().toISOString(), pid: process.pid, ...payload })}\n`,
    );
  } catch {
    /* observation must never break a model call */
  }
}

/** Return the last N ledger events (oldest first). */
export function tailEvents(limit = 20) {
  try {
    const file = join(dataDir(), "events.jsonl");
    if (!existsSync(file)) return [];
    const lines = readFileSync(file, "utf8").split(/\r?\n/).filter(Boolean);
    return lines.slice(-Math.max(1, limit)).map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return { event: "unparsed", raw: line };
      }
    });
  } catch {
    return [];
  }
}

/**
 * Persist one original reasoning block before it is replaced in the stream.
 * @returns {string|null} the archived path, or null when archiving failed.
 */
export function archiveOriginal({ sessionId, index, text, provider, model, strategy, style }) {
  try {
    const dir = join(dataDir(), "originals", safeId(sessionId));
    mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const file = join(dir, `${stamp}-block${Number.isInteger(index) ? index : 0}.txt`);
    // 头里写清"当时是哪个策略 / 哪份规格"：事后要判断"是改写坏了，还是规格本来就不对"，
    // 只有原文不够 —— 得知道是谁改的。
    const header = [
      `session=${sessionId ?? ""}`,
      `provider=${provider ?? ""}`,
      `model=${model ?? ""}`,
      ...(strategy ? [`strategy=${strategy}`] : []),
      ...(style ? [`style=${style}`] : []),
      `chars=${text.length}`,
      "",
    ].join("\n");
    const tmp = `${file}.tmp-${process.pid}`;
    writeFileSync(tmp, header + text, "utf8");
    renameSync(tmp, file);
    return file;
  } catch {
    return null;
  }
}
