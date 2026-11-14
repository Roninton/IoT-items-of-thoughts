// lib/config-store.mjs — live config for the IoT thinking rewriter.
//
// Two layers:
//   1. row config from cordis.patch.yml (deployment defaults)
//   2. $DSH_HOME/plugin-data/iot-thinking/config.json (user switch + limits)
//
// The stream hook reads this on every request (mtime cache), so the Settings
// toggle and any per-session switch take effect immediately — no dsh restart,
// no profile reload.
//
// 配置形状（v0.2 起）：
//   strategy     全局默认策略 id（"iot-full" 保持旧行为）
//   strategies   { "<档位 id>": { minChars, maxNodes, … } } 逐策略覆盖
//   agentPresets { "<agent preset id>": "<策略 id>" }  按 preset 映射
//   sessions     { "<sessionId>": "<策略 id>" }        会话级覆盖（会话内切换写这里）
//
// 旧文件里只有 `profile` + 几个平铺数值字段。load() 会**一次性迁移**成上面的形状
// （见 lib/route.mjs 的 migrateLegacyConfig），迁移语义是"行为不变"。

import { existsSync, readFileSync, writeFileSync, renameSync, mkdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";
import { migrateLegacyConfig, migrateRewriteConfig, normalizeRouteEntry } from "./route.mjs";
import { normalizeEffort } from "./effort.mjs";
import { DEFAULT_STRATEGY, strategyById } from "./strategies.mjs";
import { builtinPresetById } from "./builtin-presets.mjs";

export const DEFAULTS = Object.freeze({
  enabled: true,
  /** 是否记录每次调用的原始 chunk 形状（不含正文）；false = 完全不记。 */
  debugChunks: true,
  // 默认保持旧 IoT 行为：8 标签全量。
  strategy: DEFAULT_STRATEGY,
  /** 逐策略限制覆盖；键是策略 id。 */
  strategies: {},
  /** agent preset id → 策略 id。 */
  agentPresets: {},
  /** sessionId → 策略 id。会话内切换只写这一层。 */
  sessions: {},
  /**
   * 改写器路由（算力卸载）：显式给出 provider/model 时，压缩调用走这条路，
   * 与主会话路由无关；null 表示跟随当前请求。
   * `effort` 是改写器自己那一次调用的推理档位；`fallback` 决定卸载路由失败时
   * 是保留原文（keep）还是回退到会话路由再试一次（session）。
   */
  rewrite: { provider: null, model: null, effort: "auto", fallback: "keep" },
  /**
   * 指导层：压缩之后让另一个模型看骨架并给建议。
   * 与压缩档位正交 —— 压缩是减 token，指导是加信息。
   */
  /**
   * 指导：与压缩档位正交的开关与模式。
   * - enabled：是否对会话启用指导（全局默认，session 可覆盖）；
   * - mode：`builtin`（压缩器自己输出 [指导]）或 `external`（并发调用外部强模型）。
   * `advisor` 保留为外部指导的路由与预算配置；`advisor.enabled` 是旧的开关别名，
   * 迁移时折进 guidance.enabled。
   */
  guidance: {
    enabled: false,
    mode: "external",
  },
  advisor: {
    enabled: false,
    route: { provider: null, model: null },
    /** 指导模型的思考档位：auto = 先试 off，被拒再探最低档，再不行不发字段。 */
    effort: "auto",
    /** 指导模型的 temperature；null = 不发该字段（有的模型只接受默认值）。 */
    temperature: 0.3,
    /**
     * 是否等待建议返回。false = 可选建议：压缩一完成就继续，建议没赶上就跳过。
     * 慢的指导模型不该拖住整个任务流程。
     */
    wait: true,
    /** 建议输出 token 预算；不配时按 maxChars 推算（中文约 2 token/字）。 */
    maxTokens: null,
    /**
     * 教师咨询（由模型主动调用 iot_consult_teacher）：
     * - enabled：session 开关，打开后 think 里注入冷却状态行；
     * - cooldownBlocks：调用后冷却多少个 thinking 块；
     * - depthRange：允许模型要求的上下文轮次范围；
     * - inputBudget：咨询提示词字符上限（插件裁剪）。
     */
    teacher: {
      enabled: false,
      cooldownBlocks: 3,
      depthRange: [1, 5],
      inputBudget: 12000,
      maxTokens: 1500,
      timeoutMs: 30000,
    },
    timeoutMs: 30000,
    maxAdvice: 3,
    maxChars: 600,
    goal: "",
  },
  /**
   * 全局默认的"自定义预设"：`builtin:<id>` / `custom:<文件名>` / `custom`（贴入的文本）。
   * 只在 custom 档位且没有会话/映射覆盖时使用；null = 沿用旧行为（贴入文本）。
   */
  defaultPreset: null,
  /** 用户改过的内置预设正文：`{ "<id>": "文本" }`；没改的键不出现。 */
  builtinOverrides: {},
  /** 单块改写调用的硬超时（毫秒）与总调用预算；不填用 lib/compress.mjs 的默认值。 */
  rewriteTimeoutMs: undefined,
  /** custom 档位的输出预算（字符数）与"输出里不许出现 think 标签"开关。 */
  maxOut: undefined,
  noThink: true,
  rewriteBudget: undefined,
  // 旧形状，只读兼容：load() 会把它们搬进 rewrite 并删键。
  provider: undefined,
  model: undefined,
  reasoningEffort: undefined,
  archive: true,
  // Independent of thinking strategy: mirror session events + pre-rewrite reasoning.
  sessionLog: true,
  sessionMemory: true,
  // 会话级档位下拉（对话头部）：默认关。多数人用不到 per-session 覆盖，
  // 那行控件反而让人以为"档位要被它管"；/iot 命令始终可用。
  sessionSwitch: false,
  // 自定义预设：目录必须显式配置（空 = 没有目录，文件类预设不可用）。
  presetDir: "",
  customText: "",
  /**
   * 用户注入（叠加层）：追加到内置三档的固定提示词后面，只能补充标签规则。
   * 空串 = 不注入。值域与自定义预设一致：`builtin:<id>` / `custom:<文件名>` / `inline:<文本>`。
   * 与 customText 分开：customText 是"贴入的文本"这一种来源，inject 可以指向任意来源。
   */
  inject: "",
  presetAccept: "different",
  // 旧字段，只读兼容：迁移后不再写回，但保留在主键集合里，避免旧文件被"清空"。
  // 只对 items 家族兜底，别让旧档位的数字盖掉当前档位自己的默认值。
  profile: undefined,
  minChars: undefined,
  maxChars: undefined,
  maxTokens: undefined,
  maxNodes: undefined,
  maxNeutral: undefined,
});

export function defaultConfigPath() {
  if (process.env.IOT_THINKING_CONFIG_PATH) return process.env.IOT_THINKING_CONFIG_PATH;
  const home = process.env.DSH_HOME || join(homedir(), ".dsh");
  return join(home, "plugin-data", "iot-thinking", "config.json");
}

function readJson(path) {
  if (!existsSync(path)) return null;
  let text;
  try {
    text = readFileSync(path, "utf8");
  } catch (cause) {
    if (cause?.code === "ENOENT") return null;
    throw Object.assign(new Error(`无法读取 IoT 配置，已保留原文件：${path}`, { cause }), { code: "CONFIG_READ_FAILED", path });
  }
  // Windows PowerShell's `Set-Content -Encoding utf8` writes a BOM. JSON.parse
  // rejects it, which used to fall back to DEFAULTS and silently overwrite the
  // user's profile with `full`. Strip the BOM before parsing.
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  try {
    return JSON.parse(text);
  } catch (cause) {
    throw Object.assign(new Error(`IoT 配置不是合法 JSON，已保留原文件：${path}`, { cause }), { code: "CONFIG_PARSE_FAILED", path });
  }
}

/**
 * 一个手写的 `sessions` 表里混进一个不存在的策略名，正解是**丢掉那一项**，
 * 而不是整张表作废 —— 后者会让用户下次打开设置卡时看到"配置没了"。
 *
 * 值可以是字符串（`"iot-full"`）或 `custom[:预设名]` 形态：后者指向 presetDir 里的
 * 一份文本（或贴进来的那段）。
 */
function strategyMap(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof key !== "string" || !key) continue;
    const normalized = normalizeRouteEntry(entry);
    // 会话级指导覆盖：对象形态 `{ ..., guidance: { enabled, mode } }`。
    // `null` 表示"跟随全局"，必须原样保留，不能当成没配。
    const sessionGuidance = entry && typeof entry === "object" && !Array.isArray(entry) && "guidance" in entry
      ? sessionGuidanceValue(entry.guidance)
      : undefined;
    const sessionTeacher = entry && typeof entry === "object" && !Array.isArray(entry) && "teacher" in entry
      ? sessionTeacherValue(entry.teacher)
      : undefined;
    // 只有 guidance/teacher、没有档位的条目也是合法的（"这个会话只改开关，档位跟随默认"）。
    if (!normalized) {
      const extra = {};
      if (sessionGuidance !== undefined) extra.guidance = sessionGuidance;
      if (sessionTeacher !== undefined) extra.teacher = sessionTeacher;
      if (Object.keys(extra).length) out[key] = extra;
      continue;
    }
    const withGuidance = (base) => {
      let result = sessionGuidance === undefined
        ? base
        : { ...(typeof base === "object" ? base : { strategy: base }), guidance: sessionGuidance };
      if (sessionTeacher !== undefined) {
        result = { ...(typeof result === "object" ? result : { strategy: result }), teacher: sessionTeacher };
      }
      return result;
    };
    // 自定义预设：`custom:名字` 要保住名字（那是 presetDir 里的文件名）；
    // 会话级 prompt 是对象形态 `{ strategy: "custom", preset }`，也原样保留 ——
    // 否则"选了 prompt"会在下一次 load 时被归一成字符串丢掉。
    if (normalized.id === "custom") {
      if (normalized.builtin) {
        out[key] = withGuidance({ strategy: "custom", preset: `builtin:${normalized.builtin}` });
        continue;
      }
      out[key] = withGuidance(normalized.custom ? `custom:${normalized.custom}` : "custom");
      continue;
    }
    out[key] = withGuidance(normalized.id);
  }
  return out;
}

/** 会话级教师开关：`null` = 跟随全局；对象只收 enabled（其余参数跟全局走）。 */
function sessionTeacherValue(value) {
  if (value === null) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  if (typeof value.enabled !== "boolean") return undefined;
  return { enabled: value.enabled };
}

/** 会话级指导覆盖：`null` = 跟随全局；对象只收 enabled/mode。 */
function sessionGuidanceValue(value) {
  if (value === null) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const out = {};
  if (typeof value.enabled === "boolean") out.enabled = value.enabled;
  if (value.mode === "builtin" || value.mode === "external") out.mode = value.mode;
  return Object.keys(out).length ? out : undefined;
}

/**
 * 指导开关与模式。
 *
 * 这里最容易出错的是"显式配置"与"默认值"的区分：merge 以 DEFAULTS 为底，
 * `guidance.enabled=false` 恒是 boolean，如果只看 `typeof raw.enabled`，
 * 旧配置里的 `advisor.enabled: true` 永远不会被折叠进来（迁移成了死代码）。
 * 所以由调用方明确告知：文件里到底有没有写过 guidance。
 */
function guidanceConfig(value, advisor, explicit) {
  const raw = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const legacyOn = advisor?.enabled === true;
  const enabled = explicit && typeof raw.enabled === "boolean" ? raw.enabled : legacyOn;
  const mode = raw.mode === "builtin" ? "builtin" : raw.mode === "external" ? "external" : "external";
  return { enabled, mode };
}

/** 教师咨询配置：session 开关、冷却块数、深度范围与输入预算。 */
function teacherConfig(value) {
  const raw = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const range = Array.isArray(raw.depthRange) && raw.depthRange.length === 2
    ? [clampInt(raw.depthRange[0], 1, 5, 1), clampInt(raw.depthRange[1], 1, 5, 5)]
    : [1, 5];
  const [low, high] = range[0] <= range[1] ? range : [range[1], range[0]];
  return {
    enabled: raw.enabled === true,
    cooldownBlocks: clampInt(raw.cooldownBlocks, 0, 20, 3),
    depthRange: [low, high],
    inputBudget: clampInt(raw.inputBudget, 1000, 40000, 12000),
    maxTokens: clampInt(raw.maxTokens, 200, 8000, 1500),
    timeoutMs: clampInt(raw.timeoutMs, 3000, 120000, 30000),
  };
}

/** 指导层配置：开关、路由、超时与条数上限；非法值一律落回安全默认。 */
function advisorConfig(value) {
  const raw = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const route = raw.route && typeof raw.route === "object" && !Array.isArray(raw.route) ? raw.route : {};
  const provider = typeof route.provider === "string" && route.provider.trim() ? route.provider.trim() : null;
  const model = typeof route.model === "string" && route.model.trim() ? route.model.trim() : null;
  const effort = normalizeEffort(raw.effort);
  return {
    enabled: raw.enabled === true,
    route: { provider, model },
    effort: effort ?? "auto",
    temperature: raw.temperature === null ? null : Number.isFinite(raw.temperature) ? raw.temperature : 0.3,
    wait: raw.wait !== false,
    maxTokens: Number.isFinite(raw.maxTokens) ? clampInt(raw.maxTokens, 200, 8000, 1200) : null,
    teacher: teacherConfig(raw.teacher),
    timeoutMs: clampInt(raw.timeoutMs, 3000, 120000, 30000),
    maxAdvice: clampInt(raw.maxAdvice, 1, 3, 3),
    maxChars: clampInt(raw.maxChars, 80, 600, 600),
    goal: typeof raw.goal === "string" ? raw.goal.slice(0, 400) : "",
  };
}

function clampInt(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.floor(n)));
}

/** 内置预设覆盖：键必须是存在的内置 id，值是字符串；空串等于恢复默认（删键）。 */
function builtinOverridesMap(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out = {};
  for (const [id, text] of Object.entries(value)) {
    if (!builtinPresetById(id)) continue;
    if (typeof text !== "string" || !text.trim()) continue;
    out[id] = text;
  }
  return out;
}

/** 逐策略覆盖：只收对象值，非法策略 id 丢弃。 */
function strategiesMap(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out = {};
  for (const [id, overrides] of Object.entries(value)) {
    if (!strategyById(id)) continue;
    if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) continue;
    out[id] = { ...overrides };
  }
  return out;
}

/**
 * 改写器路由块的归一化：只认已知键，provider/model 只收非空字符串，
 * effort 走 normalizeEffort（未知值落回 auto），fallback 只认 keep/session。
 * 坏值一律降级而不是整块丢弃 —— 一块坏字段不该让插件停摆。
 */
function rewriteConfig(value) {
  const out = { provider: null, model: null, effort: "auto", fallback: "keep" };
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const key of ["provider", "model"]) {
    if (value[key] === null) out[key] = null;
    else if (typeof value[key] === "string") out[key] = value[key].trim() || null;
  }
  const effort = normalizeEffort(value.effort);
  out.effort = effort ?? "auto";
  out.fallback = value.fallback === "session" ? "session" : "keep";
  return out;
}

function merge(base, overlay) {
  const out = { ...base };
  if (!overlay || typeof overlay !== "object" || Array.isArray(overlay)) return out;
  // 文件里是否**真的**写过 guidance：决定旧 advisor.enabled 要不要折叠，
  // 以及 iot-guided 档位要不要按档位推断内置建议。
  const guidanceExplicit = overlay.guidance !== undefined && overlay.guidance !== null;
  for (const key of Object.keys(DEFAULTS)) {
    if (overlay[key] !== undefined) out[key] = overlay[key];
  }
  out.strategies = strategiesMap(out.strategies);
  out.agentPresets = strategyMap(out.agentPresets);
  out.sessions = strategyMap(out.sessions);
  out.rewrite = rewriteConfig(out.rewrite);
  out.advisor = advisorConfig(out.advisor);
  out.guidance = guidanceConfig(out.guidance, out.advisor, guidanceExplicit);
  out.builtinOverrides = builtinOverridesMap(out.builtinOverrides);
  // 非持久标记：只活在内存里，save 落盘前会删掉。
  Object.defineProperty(out, "__guidanceExplicit", {
    value: guidanceExplicit,
    enumerable: false,
    configurable: true,
    writable: true,
  });
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
    this.lastError = null;
    /** @type {{from: string, to: string}|null} 最近一次迁移的记录，账本要能查到。 */
    this.migrated = null;
  }

  load() {
    const stat = existsSync(this.path) ? statSync(this.path) : null;
    const mtime = stat ? stat.mtimeMs : null;
    if (this._cache !== null && mtime === this._mtimeMs) return this._cache;

    let raw;
    try {
      raw = readJson(this.path);
      this.lastError = null;
    } catch (error) {
      // A half-written or unreadable config must never mean "first install". Keep
      // the last known-good value; on cold start fail closed without touching the
      // user's bytes. A later mtime change retries the read automatically.
      this.lastError = error;
      if (this._cache !== null) {
        this._mtimeMs = mtime;
        return this._cache;
      }
      const safe = merge(this.rowDefaults, { enabled: false });
      this._cache = safe;
      this._mtimeMs = mtime;
      return safe;
    }
    // 旧文件 → 新形状。迁移只在"文件里有旧 `profile`、却没有新 `strategy`"时发生，
    // 且立刻落盘：否则每次 load 都要重算，而 user 在设置卡里存的任何值都会覆盖它。
    const strategyMigrated = raw === null ? null : migrateLegacyConfig(raw);
    // 旧形状的 provider/model/reasoningEffort 也搬进 rewrite 块：迁移是行为不变的，
    // 原来"填了就卸载、留空就跟随会话"，迁移后完全一样。
    const migrated = strategyMigrated === null ? null : migrateRewriteConfig(strategyMigrated);
    if (strategyMigrated !== null && strategyMigrated !== raw) {
      this.migrated = { from: String(raw.profile ?? ""), to: String(strategyMigrated.strategy ?? "") };
    }

    const cfg = merge(this.rowDefaults, migrated ?? raw);
    if (raw === null) this.save(cfg); // seed the file so the user can see it
    else if (migrated !== null && migrated !== raw) this.save(migrated);

    this._cache = cfg;
    this._mtimeMs = existsSync(this.path) ? statSync(this.path).mtimeMs : null;
    return cfg;
  }

  save(next) {
    if (this.lastError && existsSync(this.path)) throw this.lastError;
    const current = this._cache ?? readJson(this.path) ?? this.rowDefaults;
    const overlay = { ...current, ...(next || {}) };
    // rewrite 是嵌套块：增量写入（只改 model / 只改 fallback）必须和当前值合并，
    // 否则浅合并会把没提到的字段一起抹掉 —— 用户会看到"保存完 provider 没了"。
    if (current?.rewrite && typeof current.rewrite === "object" && next?.rewrite && typeof next.rewrite === "object") {
      overlay.rewrite = { ...current.rewrite, ...next.rewrite };
    }
    // guidance 是嵌套块：只改 mode 不能把 enabled 抹掉（反之亦然）。
    if (current?.guidance && typeof current.guidance === "object" && next?.guidance && typeof next.guidance === "object") {
      overlay.guidance = { ...current.guidance, ...next.guidance };
    }
    // advisor 同样有嵌套 route：增量改开关不能把已配的 provider/model 抹掉。
    if (current?.advisor && typeof current.advisor === "object" && next?.advisor && typeof next.advisor === "object") {
      overlay.advisor = {
        ...current.advisor,
        ...next.advisor,
        route: { ...(current.advisor.route ?? {}), ...(next.advisor.route ?? {}) },
        teacher: { ...(current.advisor.teacher ?? {}), ...(next.advisor.teacher ?? {}) },
      };
    }
    const merged = merge(this.rowDefaults, overlay);
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp-${process.pid}`;
    writeFileSync(tmp, `${JSON.stringify(merged, null, 2)}\n`, "utf8");
    renameSync(tmp, this.path);
    this._cache = merged;
    this._mtimeMs = existsSync(this.path) ? statSync(this.path).mtimeMs : null;
    return merged;
  }
}
