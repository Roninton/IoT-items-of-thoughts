// lib/seams.mjs — 从宿主 dsh 安装锚点解析 seam 包（绝对 file:// URL 导入）
//
// 为什么必须这么绕（harness-evolve/knowledge/harness-debug.md 实证）：
//   profile 本地文件插件（name 以 `.` 开头）由 loader 用 Node 原生 import() 加载，
//   其内部的裸 `@deepseek-ai/*` **不会**走宿主 anchor 回退 → ERR_MODULE_NOT_FOUND。
//   按绝对路径导入还能与宿主共享同一个模块缓存，避免 Service/instanceof 分裂
//   （分裂的后果是 "ctx.compaction 已经有人注册" 或类型判断失效）。
//
// 锚点顺序：env IOT_DSH_SEAM_ANCHORS（按 path.delimiter 分隔，追加在前）
//   → $DSH_HOME/profiles/node_modules
//   → 从 process.argv[1]（宿主入口）上溯出的 node_modules
//   → $DSH_HOME 下版本化安装 dsh-<ver>/node_modules（按 mtime 降序）
//   → 安装根 / npm 全局 / 传统 Linux 路径。

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, delimiter } from "node:path";
import { pathToFileURL } from "node:url";
import { execSync } from "node:child_process";

/**
 * 从入口脚本路径上溯出 node_modules 根。
 * 取**最后一层** node_modules：pnpm 的 `<nm>/.pnpm/<pkg>@<ver>/node_modules/<pkg>/...`
 * 只有最里层才真的放得下 @deepseek-ai/*。
 *
 *   <DSH_HOME>/dsh-0.2.1-rc.2/node_modules/@deepseek-ai/dsh/lib/bin.js
 *     -> <DSH_HOME>/dsh-0.2.1-rc.2/node_modules
 *
 * 纯字符串处理（两种分隔符都认），所以自测能直接喂任意平台的假路径。
 */
export function rootFromEntryPath(entry) {
  if (typeof entry !== "string" || !entry) return null;
  const re = /[\\/]node_modules(?=[\\/]|$)/gi;
  let last = null;
  for (let m = re.exec(entry); m; m = re.exec(entry)) last = m;
  if (!last) return null;
  return entry.slice(0, last.index) + last[0][0] + "node_modules";
}

/**
 * $DSH_HOME 下的版本化安装：`dsh-<ver>/node_modules`。
 * 多版本共存时按目录 mtime 降序 —— 刚装上的那个先试。
 * fs 可注入，自测不必真建目录。
 */
export function versionedRoots(home, fs = { readdirSync, statSync }) {
  if (typeof home !== "string" || !home) return [];
  let entries = [];
  try {
    entries = fs.readdirSync(home, { withFileTypes: true });
  } catch {
    return [];
  }
  const found = [];
  for (const entry of entries) {
    if (typeof entry?.isDirectory !== "function" || !entry.isDirectory()) continue;
    if (!/^dsh-/i.test(entry.name)) continue;
    const root = join(home, entry.name, "node_modules");
    let mtime = 0;
    try {
      mtime = fs.statSync(join(home, entry.name)).mtimeMs || 0;
    } catch {
      /* 取不到 mtime 就排最后，别让一个目录拖垮解析 */
    }
    found.push({ root, mtime });
  }
  return found.sort((a, b) => b.mtime - a.mtime).map((x) => x.root);
}

/**
 * 宿主 node_modules 候选根，按可信度排序。
 *
 * 顺序是语义，不是风格：`profiles/node_modules` 里可能 hoist 着另一个 dsh
 * 版本的 `@deepseek-ai/dsh-llm`，先命中它等于让插件与宿主拿到两份不同实例的
 * seam（Service/instanceof 会分裂）。所以"正在跑的入口"永远排第一，profile 根
 * 只在入口推导落空时用。
 *
 * fs / entry / home 可注入，自测不必真建目录、也不必改 process。
 */
export function defaultNodeModulesRoots({ entry = process.argv[1], home = process.env.DSH_HOME, fs } = {}) {
  const roots = [];
  const push = (p) => { if (p && !roots.includes(p)) roots.push(p); };

  // 1) 正在跑的宿主入口 —— 唯一"确定就是当前这个 dsh"的锚点。
  //    issue #1：旧候选表全是 profiles/全局/系统路径，五个全部落空 → importSeam
  //    抛错 → cordis 把这条 entry 静默跳过（只留一行 warning），thinking 完全没被压缩。
  push(rootFromEntryPath(entry));

  // 2) profile 解析根。只在入口推导落空时使用：它可能装着别的 dsh 版本的副本。
  if (home) push(join(home, "profiles", "node_modules"));

  // 3) 兜底：扫 DSH_HOME 下的版本化安装（入口被 wrapper 改写、argv[1] 不可用时）
  if (home) for (const r of versionedRoots(home, fs)) push(r);

  // 4) 安装根
  if (home) push(join(home, "..", "node_modules"));

  // 5) npm 全局根
  try {
    const g = execSync("npm root -g", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    push(g);
  } catch { /* 无 npm 跳过 */ }

  // 6) 传统 Linux 路径
  push("/usr/lib/node_modules");
  push("/usr/local/lib/node_modules");

  // 7) dsh 安装内未 hoist 时的 scope 目录
  for (const r of [...roots]) push(join(r, "@deepseek-ai", "dsh", "node_modules"));
  return roots;
}

function scopeAnchors() {
  const out = [];
  for (const nm of defaultNodeModulesRoots()) {
    const sc = join(nm, "@deepseek-ai");
    if (existsSync(sc)) out.push(sc);
  }
  return out;
}

function candidates(pkg) {
  // ⚠️ 分隔符用 path.delimiter：Windows 是 ";"，硬编码 ":" 会把 D:\ 切碎
  const env = (process.env.IOT_DSH_SEAM_ANCHORS || "").split(delimiter).filter(Boolean);
  const dirs = [];
  const scoped = pkg.startsWith("@") && pkg.includes("/");
  const bare = scoped ? pkg.split("/").slice(1).join("/") : pkg;
  for (const nm of [...env, ...defaultNodeModulesRoots()]) {
    dirs.push(join(nm, pkg));
    if (scoped && nm.endsWith("@deepseek-ai") === false) dirs.push(join(join(nm, "@deepseek-ai"), bare));
  }
  for (const sc of scopeAnchors()) dirs.push(join(sc, bare));
  return dirs;
}

/** 解析 seam 包目录（存在 package.json 且 name 匹配）。 */
export function resolveSeamDir(pkg) {
  for (const dir of candidates(pkg)) {
    try {
      const pj = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
      if (pj.name === pkg) return dir;
    } catch {
      /* 继续下一个候选 */
    }
  }
  throw new Error(
    `iot-compaction: 找不到宿主 seam 包 ${pkg}（试过 ${candidates(pkg).slice(0, 4).join(", ")}…）。` +
    `可用环境变量 IOT_DSH_SEAM_ANCHORS 指定 @deepseek-ai 的 node_modules 锚点。`
  );
}

function mainOf(dir, pj) {
  const rel = pj.main || "lib/index.js";
  const p = join(dir, rel);
  if (existsSync(p)) return p;
  for (const f of ["lib/index.js", "index.js", "dist/index.js"]) {
    const q = join(dir, f);
    if (existsSync(q)) return q;
  }
  return p;
}

/** 动态导入宿主 seam 包，返回其模块命名空间（与宿主同实例）。 */
export async function importSeam(pkg) {
  const dir = resolveSeamDir(pkg);
  let pj = {};
  try {
    pj = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  } catch { /* ignore */ }
  const entry = mainOf(dir, pj);
  if (!existsSync(entry)) {
    throw new Error(`iot-compaction: seam 包 ${pkg} 入口缺失: ${entry}`);
  }
  return import(pathToFileURL(entry).href);
}
