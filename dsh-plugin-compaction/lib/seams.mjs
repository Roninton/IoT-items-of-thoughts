// lib/seams.mjs — 从宿主 dsh 安装锚点解析 seam 包（绝对 file:// URL 导入）
//
// 为什么必须这么绕（harness-evolve/knowledge/harness-debug.md 实证）：
//   profile 本地文件插件（name 以 `.` 开头）由 loader 用 Node 原生 import() 加载，
//   其内部的裸 `@deepseek-ai/*` **不会**走宿主 anchor 回退 → ERR_MODULE_NOT_FOUND。
//   按绝对路径导入还能与宿主共享同一个模块缓存，避免 Service/instanceof 分裂
//   （分裂的后果是 "ctx.compaction 已经有人注册" 或类型判断失效）。
//
// 锚点：env IOT_DSH_SEAM_ANCHORS（按 path.delimiter 分隔，追加在前）优先。

import { existsSync, readFileSync } from "node:fs";
import { join, delimiter } from "node:path";
import { pathToFileURL } from "node:url";
import { execSync } from "node:child_process";

function defaultNodeModulesRoots() {
  const roots = [];
  const push = (p) => { if (p && !roots.includes(p)) roots.push(p); };

  // 1) DSH_HOME 推导：profiles/node_modules 是 profile 解析根（优先），上一级是安装根
  const home = process.env.DSH_HOME;
  if (home) {
    push(join(home, "profiles", "node_modules"));
    push(join(home, "..", "node_modules"));
  }
  // 2) npm 全局根
  try {
    const g = execSync("npm root -g", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    push(g);
  } catch { /* 无 npm 跳过 */ }
  // 3) 传统 Linux 路径
  push("/usr/lib/node_modules");
  push("/usr/local/lib/node_modules");
  // 4) dsh 安装内未 hoist 时的 scope 目录
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
