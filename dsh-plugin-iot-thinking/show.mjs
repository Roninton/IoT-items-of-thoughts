#!/usr/bin/env node
// show.mjs — 在**不启动 dsh** 的前提下，把"具体内容"打成文本。
//
// 为什么需要它：设置卡里能看到的东西（提示词、preset 组成）只在那台跑着的 web 里，
// 而排查/写文档/贴到 issue 里都需要**文本**。这里直接跑插件自己的模块，
// 所以打出来的就是真实链路会用的那份东西。
//
// 用法：
//   node show.mjs                          列出全部策略（id / 家族 / 替换规则 / 关键默认值）
//   node show.mjs research                 打印该策略**真正发给模型的提示词**
//   node show.mjs --json research          同一份内容，JSON（给脚本用）
//   node show.mjs --presets [--profile D:\path\to\profile]
//                                          打印 preset 清单与组成（读声明文件，不连宿主）
//
// ⚠️ `--presets` 读的是**声明**（bundle patch + profile 的 cordis.patch.yml），
// 不是运行态注册表：它不评估 `!!js` 条件，只把表达式原样打出来。
// 要看运行态（含加载失败诊断），用跑着的 web 里的 `看组成`，或
// `GET /iot-thinking/api/presets?detail=<id>`。

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { DEFAULT_STRATEGY, STRATEGIES, strategyById, strategyIds } from "./lib/strategies.mjs";
import { resolveSeamDir } from "./lib/seams.mjs";

/** 一段示例草稿：够长、有决定、有被否的路线、有未决问题，四个家族都有东西可保。 */
const SAMPLE_DRAFT = [
  "先看现状：插件是在 llm/stream 上做块级替换，块结束时才改写，所以客户端会顿一下。",
  "要不要改成边压边回？不行，块结束才是唯一稳定锚点，边压边回会把 delta 顺序搞乱。",
  "另一个方向是直接删掉 reasoningEffort 字段。这样每个路由都能跑，但原生 DeepSeek 路由会丢掉 off 的快路径。",
  "所以默认档位应该是「先试 off，被拒再不带该字段」，并且把路由决策记住，别每块都白试一次。",
  "还没定的是设置卡要不要列模型真实档位：宿主 resolveModelInfo 有 reasoning.efforts，但目录查询是异步的。",
].join("\n");

function parseArgs(argv) {
  const pos = [];
  const opt = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        opt[key] = next;
        i += 1;
      } else opt[key] = true;
    } else pos.push(arg);
  }
  return { pos, opt };
}

/** 渲染一条策略的提示词（与 host 的 /api/strategy 走同一套调用）。 */
function renderPrompt(id) {
  const strategy = strategyById(id);
  if (!strategy) return { ok: false, error: `未知策略：${id}（可用：${strategyIds().join(" / ")}）` };
  const limits = { ...strategy.defaults };
  try {
    const prepared = strategy.prepare(limits);
    if (prepared?.error) return { ok: false, error: prepared.error, strategy, limits };
    const prompt = strategy.buildPrompt(SAMPLE_DRAFT, {
      strict: false,
      reason: null,
      detail: null,
      limits,
      state: prepared?.state ?? null,
    });
    return { ok: true, prompt: String(prompt ?? ""), strategy, limits };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error), strategy, limits };
  }
}

function listStrategies() {
  const lines = ["可用策略（默认：" + DEFAULT_STRATEGY + "）", ""];
  for (const strategy of STRATEGIES) {
    const defaults = strategy.defaults;
    const bits = [`minChars=${defaults.minChars}`, `maxChars=${defaults.maxChars}`, `maxTokens=${defaults.maxTokens}`];
    if (strategy.family === "items") bits.push(`maxNodes=${defaults.maxNodes}`, `maxNeutral=${defaults.maxNeutral}`);
    lines.push(`${strategy.id.padEnd(14)} ${strategy.family.padEnd(7)} accept=${strategy.accept.padEnd(9)} ${bits.join(" ")}`);
    lines.push(`                ${strategy.label}`);
    lines.push(`                ${strategy.summary}`);
    lines.push("");
  }
  lines.push("看某一档真正发给模型的原文：");
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// --presets：读声明文件（不是运行态注册表）
// ---------------------------------------------------------------------------

/** 插件包根（show.mjs 所在目录）→ 上溯是仓库根，不参与解析。 */
const HERE = new URL(".", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function defaultProfileDir() {
  const home = process.env.DSH_HOME;
  if (home && existsSync(join(home, "profiles", "web"))) return join(home, "profiles", "web");
  return null;
}

/**
 * 把一份 patch 文件里的 preset 行抽出来。
 *
 * 按**缩进**判定层级，不靠"看起来像"：
 *   `    - id: preset-x`        行本身（rowIndent）
 *   `      name: '…'`           行级 name（= preset 的插件名，不是 preset 的名字）
 *   `      config:`             行配置开始
 *   `        id: / name: / description:`   configIndent 里的字段 —— 这才是 preset 的名称
 *   `        plugins:`          子插件列表
 *   `          - id: persona`   子插件行（会比 configIndent 更深）
 */
function presetsInPatch(text, origin) {
  const lines = String(text ?? "").split(/\r?\n/);
  const out = [];
  let cur = null;
  let configIndent = null;
  let pluginsIndent = null;
  let pendingGroup = false;

  for (const line of lines) {
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const indent = /^\s*/.exec(line)[0].length;

    const row = /^(\s*)-\s*id:\s*(preset-\S+)\s*$/.exec(line);
    if (row) {
      cur = { row: row[2], origin, id: null, name: null, description: null, rows: [] };
      out.push(cur);
      configIndent = null;
      pluginsIndent = null;
      pendingGroup = false;
      continue;
    }
    if (!cur) continue;

    // 回到行级（或更浅）就说明这一条结束了。
    if (indent <= 4 && /^\s*-\s*(id|name):/.test(line)) {
      cur = null;
      continue;
    }

    if (configIndent === null) {
      if (/^\s*config:\s*$/.test(line)) configIndent = indent;
      continue;
    }

    // 退回到 config 这一层的兄弟键 → 该行结束。
    if (indent <= configIndent && !/^\s*$/.test(line)) {
      cur = null;
      continue;
    }

    const fieldIndent = configIndent + 2;
    if (pluginsIndent === null && indent === fieldIndent) {
      if (/^id:\s*\S+$/.test(line.trim())) {
        cur.id = line.trim().slice(3).trim();
        continue;
      }
      if (/^name:\s*/.test(line.trim()) && cur.name === null) {
        cur.name = line.trim().slice(5).trim().replace(/^['"]|['"]$/g, "");
        continue;
      }
      if (/^description:\s*/.test(line.trim()) && cur.description === null) {
        cur.description = line.trim().slice(12).trim().replace(/^['"]|['"]$/g, "");
        continue;
      }
      if (/^plugins:\s*$/.test(line.trim())) {
        pluginsIndent = fieldIndent;
        continue;
      }
    }

    if (pluginsIndent === null) continue;
    if (indent <= pluginsIndent) {
      // 子插件列表结束（回到 config 字段层）
      if (indent <= fieldIndent) pluginsIndent = null;
      continue;
    }

    if (/^\s*-\s*group:\s*true\s*$/.test(line)) {
      cur.rows.push({ kind: "group", value: "(group)", condition: null, group: true });
      pendingGroup = true;
      continue;
    }
    const sub = /^\s*-\s*(id|name):\s*(.+)$/.exec(line);
    if (sub) {
      const value = sub[2].trim().replace(/^['"]|['"]$/g, "");
      cur.rows.push({
        kind: sub[1],
        value,
        name: sub[1] === "name" ? value : null,
        condition: null,
        group: pendingGroup,
      });
      pendingGroup = false;
      continue;
    }
    // 子行自己的 `name:`（模块名）跟在 `- id:` 后面，补到刚推入的那一行上。
    const subName = /^\s+name:\s*(.+)$/.exec(line);
    if (subName && cur.rows.length > 0) {
      const last = cur.rows[cur.rows.length - 1];
      if (!last.name) {
        last.name = subName[1].trim().replace(/^['"]|['"]$/g, "");
        continue;
      }
    }
    const cond = /^\s*disabled:\s*!!js\s*(.+)$/.exec(line);
    if (cond && cur.rows.length > 0) {
      cur.rows[cur.rows.length - 1].condition = cond[1].trim();
    }
  }
  return out;
}

function printPresets(profileDir) {
  const files = [];
  // 1) 随包出货的 preset（dsh-web-app 的 dsh.bundle.patch 是个数组）
  try {
    const appDir = resolveSeamDir("@deepseek-ai/dsh-web-app");
    const shipped = join(appDir, "presets");
    if (existsSync(shipped)) {
      for (const name of readdirSync(shipped).filter((f) => f.endsWith(".patch.yml"))) {
        files.push({ path: join(shipped, name), origin: "shipped" });
      }
    }
  } catch (error) {
    files.push({ error: `找不到 @deepseek-ai/dsh-web-app：${String(error?.message ?? error)}` });
  }
  // 2) profile 自己的 patch
  const profile = profileDir ?? defaultProfileDir();
  if (profile && existsSync(join(profile, "cordis.patch.yml"))) {
    files.push({ path: join(profile, "cordis.patch.yml"), origin: "profile" });
  } else {
    files.push({ error: `没找到 profile 的 cordis.patch.yml：${profile ?? "(无 DSH_HOME)"}` });
  }

  const lines = [
    "preset 来自**声明**，不是目录扫描（`dsh-agent-preset-registry` 的 README 写明：",
    "「注册表不扫描目录，也不接受 preset 路径」）。登记处只有两处：",
    "  1) bundle 的 dsh.bundle.patch（dsh-web-app 是数组：cordis + presets/*.patch.yml）",
    "  2) profile 的 cordis.patch.yml 里的 `- id: preset-*` 行",
    "`$DSH_HOME/.agent-presets/` 是**旧的目录式来源**，运行时不读它（只是被迁移成上面的声明行）。",
    "",
  ];

  const found = [];
  for (const file of files) {
    if (file.error) {
      lines.push(`⚠️ ${file.error}`);
      continue;
    }
    try {
      found.push(...presetsInPatch(readFileSync(file.path, "utf8"), file.origin));
    } catch (error) {
      lines.push(`⚠️ 读不了 ${file.path}：${String(error?.message ?? error)}`);
    }
  }

  lines.push(`共 ${found.length} 个声明式 preset：`, "");
  for (const preset of found) {
    lines.push(`▸ ${preset.id ?? preset.row}    [${preset.origin}]`);
    if (preset.name) lines.push(`  name: ${preset.name}`);
    if (preset.description) lines.push(`  desc: ${preset.description.replace(/\s+/g, " ").slice(0, 160)}`);
    if (preset.rows.length === 0) lines.push("  rows: (没有抽到子插件行 —— 该行可能用了 group 或引用)");
    for (const row of preset.rows) {
      const cond = row.condition ? `   if ${row.condition}` : "";
      const label = row.group ? "  (group)" : row.name && row.name !== row.value ? `${row.value}  →  ${row.name}` : row.value;
      lines.push(`  ${row.group ? "    " : ""}- ${label}${cond}`);
    }
    lines.push("");
  }
  lines.push("⚠️ 这是**声明**视图：`!!js` 条件原样打印、不求值；也不含注册表在运行时的加载诊断。");
  lines.push("   运行态（含 broken / 默认标记）：跑着的 web 里点「看组成」，或 GET /iot-thinking/api/presets?detail=<id>");
  return lines.join("\n");
}

// ---------------------------------------------------------------------------

const { pos, opt } = parseArgs(process.argv.slice(2));

if (opt.presets) {
  process.stdout.write(`${printPresets(typeof opt.profile === "string" ? resolve(opt.profile) : null)}\n`);
  process.exit(0);
}

const id = pos[0];
if (!id) {
  process.stdout.write(`${listStrategies()}\n`);
  process.exit(0);
}

const rendered = renderPrompt(id);
if (!rendered.ok) {
  process.stderr.write(`渲染失败：${rendered.error}\n`);
  process.exit(1);
}

if (opt.json) {
  process.stdout.write(`${JSON.stringify({
    id: rendered.strategy.id,
    label: rendered.strategy.label,
    family: rendered.strategy.family,
    accept: rendered.strategy.accept,
    limits: rendered.limits,
    prompt: rendered.prompt,
  }, null, 2)}\n`);
  process.exit(0);
}

const header = [
  `# ${rendered.strategy.id} — ${rendered.strategy.label}`,
  `family=${rendered.strategy.family}  accept=${rendered.strategy.accept}`,
  "",
  "## 下面这段就是**真正发给模型**的提示词（用固定样稿渲染）：",
  "",
].join("\n");
process.stdout.write(`${header}${rendered.prompt}\n`);
