// lib/config-store.mjs — live config for the IoT thinking compressor.
//
// Two layers:
//   1. row config from cordis.patch.yml (deployment defaults)
//   2. $DSH_HOME/plugin-data/iot-thinking/config.json (user switch + limits)
//
// The stream hook reads this on every request (mtime cache), so the Settings
// toggle takes effect immediately — no dsh restart, no profile reload.

import { existsSync, readFileSync, writeFileSync, renameSync, mkdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";

export const DEFAULTS = Object.freeze({
  enabled: true,
  minChars: 400,
  maxChars: 24000,
  maxTokens: 2000,
  maxNodes: 8,
  maxNeutral: 2,
  provider: null,
  model: null,
  archive: true,
});

export function defaultConfigPath() {
  if (process.env.IOT_THINKING_CONFIG_PATH) return process.env.IOT_THINKING_CONFIG_PATH;
  const home = process.env.DSH_HOME || join(homedir(), ".dsh");
  return join(home, "plugin-data", "iot-thinking", "config.json");
}

function readJson(path) {
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function merge(base, overlay) {
  const out = { ...base };
  if (!overlay || typeof overlay !== "object" || Array.isArray(overlay)) return out;
  for (const key of Object.keys(DEFAULTS)) {
    if (overlay[key] !== undefined) out[key] = overlay[key];
  }
  return out;
}

export class ConfigStore {
  /**
   * @param {string} path - JSON file edited by the Settings card.
   * @param {object} rowConfig - defaults from the cordis row; the file wins.
   */
  constructor(path = defaultConfigPath(), rowConfig = {}) {
    this.path = path;
    this.rowDefaults = merge(DEFAULTS, rowConfig);
    this._cache = null;
    this._mtimeMs = null;
  }

  load() {
    const stat = existsSync(this.path) ? statSync(this.path) : null;
    const mtime = stat ? stat.mtimeMs : null;
    if (this._cache !== null && mtime === this._mtimeMs) return this._cache;
    const raw = readJson(this.path);
    const cfg = merge(this.rowDefaults, raw);
    if (raw === null) this.save(cfg); // seed the file so the user can see it
    this._cache = cfg;
    this._mtimeMs = mtime;
    return cfg;
  }

  save(next) {
    const current = this._cache ?? readJson(this.path) ?? this.rowDefaults;
    const merged = merge(this.rowDefaults, { ...current, ...(next || {}) });
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp-${process.pid}`;
    writeFileSync(tmp, `${JSON.stringify(merged, null, 2)}\n`, "utf8");
    renameSync(tmp, this.path);
    this._cache = merged;
    this._mtimeMs = existsSync(this.path) ? statSync(this.path).mtimeMs : null;
    return merged;
  }
}
