import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, delimiter } from "node:path";
import { pathToFileURL } from "node:url";
import { execSync } from "node:child_process";

/** 入口路径 → node_modules 根。取最后一层（pnpm 嵌套时只有最里层放得下 @deepseek-ai/*）。 */
export function rootFromEntryPath(entry) {
  if (typeof entry !== "string" || !entry) return null;
  const re = /[\\/]node_modules(?=[\\/]|$)/gi;
  let last = null;
  for (let m = re.exec(entry); m; m = re.exec(entry)) last = m;
  if (!last) return null;
  return entry.slice(0, last.index) + last[0][0] + "node_modules";
}

/** $DSH_HOME/dsh-<ver>/node_modules，按 mtime 降序（刚装上的先试）。 */
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
 * 候选根按可信度排序：正在跑的入口必须排第一，否则可能拿到另一个 dsh 版本的
 * @deepseek-ai/*，导致 Service/instanceof 分裂。
 */
export function defaultNodeModulesRoots({ entry = process.argv[1], home = process.env.DSH_HOME, fs } = {}) {
  const roots = [];
  const push = (p) => { if (p && !roots.includes(p)) roots.push(p); };

  // 1) 正在跑的宿主入口（唯一确定"就是这个 dsh"的锚点）
  push(rootFromEntryPath(entry));

  // 2) profile 解析根（可能装着别的 dsh 版本）
  if (home) push(join(home, "profiles", "node_modules"));

  // 3) DSH_HOME 下的版本化安装
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
