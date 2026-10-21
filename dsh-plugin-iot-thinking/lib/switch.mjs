// /iot 命令的纯逻辑。与 HTTP 桥写同一张 sessions 表，语义必须一致。

import { normalizeRouteEntry, resolveConfig } from "./route.mjs";
import { strategyById, strategyIds } from "./strategies.mjs";

/** 把一条路由（含来源）翻成人话；设置卡与命令共用。 */
export function sourceText(route, preset) {
  switch (route?.source) {
    case "session":
      return "本会话的单独设置";
    case "preset":
      return `agent preset「${route.key}」的映射`;
    case "default":
      return "全局默认策略";
    case "profile":
      return `旧配置里的 profile（${route.key}）`;
    default:
      return `默认兜底${preset ? `（本会话 preset：${preset}）` : ""}`;
  }
}

/** `auto` / `reset` / `默认` / 空 = 清除覆盖。 */
function isClearWord(word) {
  return word === "auto" || word === "reset" || word === "默认";
}

/** 执行一次 /iot：空 = 查状态，auto = 清除覆盖，其余按值域解析。 */
export function switchSessionStrategy({ store, sessionId, preset, rawInput, mark }) {
  if (!sessionId) return { kind: "error", text: "拿不到会话 id，无法切换" };

  const ids = strategyIds();
  const words = String(rawInput ?? "").trim().split(/\s+/).filter(Boolean);
  const word = words[0] ?? "";
  const cfg = store.load();

  if (!word) {
    const route = resolveConfig(cfg, { sessionId, agentPreset: preset }).route;
    const strategy = strategyById(route.id);
    return {
      kind: "success",
      strategy: route.id,
      text: [
        `本会话档位：${route.id}（${strategy?.label ?? "未知"}）`,
        `来源：${sourceText(route, preset)}`,
        `可用：${ids.join(" / ")}`,
        "切换：/iot <档位>（自定义预设写 /iot custom:<文件名>）；回到跟随 preset 默认：/iot auto",
      ].join("\n"),
    };
  }

  const sessions = { ...(cfg.sessions ?? {}) };
  if (isClearWord(word)) {
    delete sessions[sessionId];
    store.save({ sessions });
    const route = resolveConfig(store.load(), { sessionId, agentPreset: preset }).route;
    mark?.("strategy-switched", { sessionId, agentPreset: preset ?? null, strategy: null, via: "clear" });
    return {
      kind: "success",
      strategy: route.id,
      text: `已清除本会话覆盖；现在跟随 preset 默认 → ${route.id}（${sourceText(route, preset)}）`,
    };
  }

  // `custom:<文件名>` —— 第 4 项：`:` 后面是预设目录里的文件名；
  // 只写 `custom` 则用设置卡里贴进来的那段文本。
  const [plainId, ...rest] = word.split(":");
  const arg = rest.join(":").trim();
  const entry = normalizeRouteEntry(word) ?? normalizeRouteEntry(plainId);
  if (!entry) {
    return { kind: "error", text: `未知策略「${plainId}」。可用：${ids.join(" / ")} / custom[:预设名] / auto` };
  }
  const strategy = strategyById(entry.id);

  if (entry.id === "custom") {
    const name = entry.custom ?? null;
    // 贴入文本（`custom` 不带名字）时，至少得有一段内容，否则等于静默关掉改写。
    if (!name && !String(store.load().customText ?? "").trim()) {
      return {
        kind: "error",
        text: "还没贴入任何文本：要么 `/iot custom:<预设名>` 用目录里的文件，要么先在设置卡里贴一段。",
      };
    }
    sessions[sessionId] = name ? `custom:${name}` : "custom";
    store.save({ sessions });
    mark?.("strategy-switched", { sessionId, agentPreset: preset ?? null, strategy: "custom", preset: name, via: "command" });
    return {
      kind: "success",
      strategy: "custom",
      text: `本会话已切到自定义预设（${name ?? "贴入的文本"}）。下一个模型请求生效。`,
    };
  }

  if (arg && strategy?.family !== "prose") {
    return { kind: "error", text: `「${entry.id}」不是角色档，没有规格可选（只有 voice 支持 <策略>:<规格>）` };
  }

  sessions[sessionId] = arg ? { strategy: entry.id, style: arg } : entry.id;
  store.save({ sessions });
  mark?.("strategy-switched", {
    sessionId,
    agentPreset: preset ?? null,
    strategy: entry.id,
    ...(arg ? { style: arg } : {}),
    via: "command",
  });
  return {
    kind: "success",
    strategy: entry.id,
    text: `本会话已切到 ${entry.id}（${strategy.label}）${arg ? `，规格 ${arg}` : ""}。下一个模型请求生效。`,
  };
}
