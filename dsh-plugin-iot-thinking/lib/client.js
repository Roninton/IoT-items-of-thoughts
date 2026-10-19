// Client half — Settings → Plugins → "IoT 思考压缩".
//
// dsh loads a loose plugin's client bundle through window.__ModuleLoader__ and
// serves the file named by package.json exports["./client"]. Plain JS + require,
// no build step.
window.__ModuleLoader__.load({
  id: "dsh-plugin-iot-thinking",
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    const react = require("react");

    const inject = ["slots"];
    const API = "/iot-thinking/api/config";
    const STATUS = "/iot-thinking/api/status";
    // The host webserver authenticates nothing; the card proves itself with a
    // header a cross-site page cannot set without a preflight the server
    // refuses (see lib/config-api.mjs guardRequest).
    const GUARD = { "x-iot-thinking": "1" };
    const FALLBACK = {
      enabled: true,
      profile: "full",
      minChars: 400,
      maxChars: 24000,
      maxTokens: 2000,
      maxNodes: 8,
      maxNeutral: 2,
      provider: null,
      model: null,
      archive: true,
    };

    const PROFILE_DEFAULTS = {
      compact: { maxNodes: 5, maxNeutral: 0 },
      balanced: { maxNodes: 6, maxNeutral: 0 },
      full: { maxNodes: 8, maxNeutral: 2 },
    };

    const h = react.createElement;
    const box = { border: "1px solid var(--dsw-alias-border-l2, #e3e6ea)", borderRadius: 10, padding: 14, maxWidth: 760 };
    const row = { display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", padding: "8px 0" };
    const label = { fontSize: 13, color: "var(--dsw-alias-label-primary, #222)" };
    const hint = { fontSize: 11.5, color: "var(--dsw-alias-label-tertiary, #8a8f98)", lineHeight: 1.6 };
    const input = { width: 110, padding: "5px 8px", borderRadius: 6, border: "1px solid var(--dsw-alias-border-l2, #ccc)", background: "transparent", color: "inherit", fontSize: 12 };
    const wide = { ...input, width: "100%" };

    function NumberRow({ title, desc, value, onChange, min }) {
      return h("div", { style: row },
        h("div", null, h("div", { style: label }, title), desc ? h("div", { style: hint }, desc) : null),
        h("input", { type: "number", min: min ?? 0, value: value ?? "", style: input, onChange: (e) => onChange(e.target.value === "" ? null : Number(e.target.value)) })
      );
    }

    function IotThinkingCard() {
      const { useState, useEffect, useRef } = react;
      const [cfg, setCfg] = useState(null);
      const [events, setEvents] = useState([]);
      const [msg, setMsg] = useState(null);
      const [err, setErr] = useState(null);
      const [busy, setBusy] = useState(false);
      const alive = useRef(true);

      useEffect(() => () => { alive.current = false; }, []);

      const load = async (announce) => {
        setBusy(true);
        setErr(null);
        if (announce) setMsg(null);
        try {
          const resp = await fetch(STATUS, { method: "GET", headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          setCfg(data.config ?? FALLBACK);
          setEvents(Array.isArray(data.events) ? data.events : []);
          if (announce) setMsg("已从 host 重新加载");
        } catch (error) {
          setErr("host 配置 API 不可用（" + String(error.message || error) + "）。先确认 dsh web 已重启且插件已加载。");
          setCfg((current) => current ?? FALLBACK);
        } finally {
          if (alive.current) setBusy(false);
        }
      };

      useEffect(() => { load(false); }, []);

      const set = (patch) => setCfg((current) => ({ ...(current ?? FALLBACK), ...patch }));

      const save = async () => {
        setBusy(true);
        setErr(null);
        setMsg(null);
        try {
          const resp = await fetch(API, { method: "PUT", headers: { ...GUARD, "content-type": "application/json" }, body: JSON.stringify(cfg) });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          if (data && data.config) setCfg(data.config);
          setMsg("已保存，下一次模型请求即时生效");
          const status = await fetch(STATUS, { headers: GUARD }).then((r) => r.json()).catch(() => null);
          if (status && Array.isArray(status.events)) setEvents(status.events);
        } catch (error) {
          setErr("保存失败：" + String(error.message || error));
        } finally {
          if (alive.current) setBusy(false);
        }
      };

      if (cfg === null) return h("div", { style: { padding: 24, color: "#777" } }, "加载配置…");

      const recent = events.slice(-4).reverse();
      return h("div", { style: box },
        h("div", { style: { display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between" } },
          h("div", null,
            h("div", { style: { fontSize: 15, fontWeight: 600, marginBottom: 4 } }, "IoT 思考压缩"),
            h("div", { style: hint }, "每个 thinking block 结束时把它压成带标签的思维节点（[支持]/[反对]/[中性]/[计划]/[权衡]/[待定]/[风险]/[决定]），节点之间空一行，并原地替换该 block。不会插入“上下文已压缩”消息，也不会产生 compaction checkpoint。")
          ),
          h("label", { style: { display: "flex", alignItems: "center", gap: 8, cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" } },
            h("input", { type: "checkbox", checked: cfg.enabled === true, onChange: (e) => set({ enabled: e.target.checked }) }),
            h("span", { style: { fontSize: 13, fontWeight: 600 } }, cfg.enabled === true ? "已启用" : "已关闭")
          )
        ),
        h("div", { style: { height: 10 } }),
        h("div", { style: row },
          h("div", null,
            h("div", { style: label }, "压缩档位"),
            h("div", { style: hint }, "极简 = 3 标签 / 最高压缩；均衡 = 5 标签；完整 = 8 标签 / 质量优先。切换档位会设置节点上限，仍可手动覆盖（受档位硬上限约束）。")
          ),
          h("select", {
            value: cfg.profile ?? "full",
            style: { ...input, width: 200 },
            onChange: (e) => {
              const profile = e.target.value;
              set({ profile, ...(PROFILE_DEFAULTS[profile] || {}) });
            },
          },
            h("option", { value: "compact" }, "极简（3 标签 / 高压缩）"),
            h("option", { value: "balanced" }, "均衡（5 标签）"),
            h("option", { value: "full" }, "完整（8 标签 / 质量优先）")
          )
        ),
        h(NumberRow, { title: "最小压缩字符数", desc: "短于这个长度的 thinking 不压，避免为几句话多调一次模型。", value: cfg.minChars, onChange: (v) => set({ minChars: v ?? 0 }) }),
        h(NumberRow, { title: "最大输入字符数", desc: "超过这个长度的 thinking 直接保留原文，避免压缩调用自己撑爆窗口。", value: cfg.maxChars, onChange: (v) => set({ maxChars: v ?? 24000 }), min: 1 }),
        h(NumberRow, { title: "节点输出上限 (tokens)", desc: "压缩器的 maxTokens；节点通常远小于这个值。", value: cfg.maxTokens, onChange: (v) => set({ maxTokens: v ?? 2000 }), min: 1 }),
        h(NumberRow, { title: "最多节点数", desc: "超过后按优先级保留：决定 > 反对 > 权衡 > 风险 > 待定 > 计划 > 支持 > 中性。", value: cfg.maxNodes, onChange: (v) => set({ maxNodes: v ?? 8 }), min: 1 }),
        h(NumberRow, { title: "最多 [中性] 节点", desc: "多余的 [中性] 会合并成一条；0 = 完全不要中性节点。", value: cfg.maxNeutral, onChange: (v) => set({ maxNeutral: v ?? 2 }) }),
        h("div", { style: row },
          h("div", null,
            h("div", { style: label }, "压缩器路由（可选）"),
            h("div", { style: hint }, "留空 = 跟随当前请求的 provider/model。")
          )
        ),
        h("div", { style: { display: "flex", gap: 8 } },
          h("input", { placeholder: "provider（留空跟随当前路由）", value: cfg.provider ?? "", style: wide, onChange: (e) => set({ provider: e.target.value }) }),
          h("input", { placeholder: "model（留空跟随当前路由）", value: cfg.model ?? "", style: wide, onChange: (e) => set({ model: e.target.value }) })
        ),
        h("div", { style: { ...row, justifyContent: "flex-start", gap: 10, marginTop: 12 } },
          h("button", { style: { padding: "7px 16px", borderRadius: 6, border: "none", background: busy ? "#9db8f0" : "#2f6fed", color: "#fff", cursor: busy ? "wait" : "pointer" }, disabled: busy, onClick: save }, busy ? "保存中…" : "保存"),
          h("button", { style: { padding: "7px 14px", borderRadius: 6, border: "1px solid #ccc", background: "transparent", color: "inherit", cursor: "pointer" }, disabled: busy, onClick: () => load(true) }, "刷新"),
          h("span", { style: hint }, "原始 thinking 归档在 $DSH_HOME/plugin-data/iot-thinking/originals/")
        ),
        err ? h("div", { style: { color: "#c0392b", marginTop: 10, fontSize: 12, whiteSpace: "pre-wrap" } }, String(err)) : null,
        msg ? h("div", { style: { color: "#188a4b", marginTop: 10, fontSize: 12 } }, String(msg)) : null,
        recent.length ? h("div", { style: { marginTop: 14, borderTop: "1px solid var(--dsw-alias-border-l2, #eee)", paddingTop: 10 } },
          h("div", { style: { ...hint, marginBottom: 6 } }, "最近事件"),
          ...recent.map((event, index) => h("div", { key: index, style: { fontSize: 11.5, color: "var(--dsw-alias-label-tertiary, #777)", fontFamily: "ui-monospace, monospace", padding: "1px 0" } },
            `${event.event || "?"}${event.charsIn != null ? `  ${event.charsIn}→${event.charsOut ?? "?"} chars` : ""}${event.reason ? `  (${event.reason})` : ""}${event.ms != null ? `  ${event.ms}ms` : ""}`
          ))
        ) : null
      );
    }

    function apply(ctx) {
      const slots = ctx.get("slots");
      if (slots === undefined) return;
      ctx.effect(() => slots.inject("settings.plugins.tab", () => slots.register({
        name: "settings.plugins.tab",
        id: "iot-thinking",
        order: 90,
        label: () => "IoT 思考压缩",
        inject: () => ({}),
      }, IotThinkingCard)), "iot-thinking: settings tab");
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
