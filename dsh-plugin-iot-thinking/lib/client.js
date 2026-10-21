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
    const MODELS = "/iot-thinking/api/models";
    const TEST = "/iot-thinking/api/test";
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
      reasoningEffort: "auto",
      archive: true,
    };

    const PROFILE_DEFAULTS = {
      compact: { maxNodes: 5, maxNeutral: 0 },
      balanced: { maxNodes: 6, maxNeutral: 0 },
      full: { maxNodes: 8, maxNeutral: 2 },
    };

    // Mirrors lib/effort.mjs EFFORT_IDS. Duplicated because this bundle is
    // plain JS loaded by the host module loader; it cannot import host code.
    const EFFORT_IDS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
    const EFFORT_LABELS = {
      auto: "自动（先试 off，被拒则降级）",
      off: "off — 不花预算在压缩器自己的思考上",
      minimal: "minimal",
      low: "low",
      medium: "medium",
      high: "high",
      xhigh: "xhigh",
      max: "max",
    };

    const h = react.createElement;
    const box = { border: "1px solid var(--dsw-alias-border-l2, #e3e6ea)", borderRadius: 10, padding: 14, maxWidth: 760 };
    const row = { display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", padding: "8px 0" };
    const label = { fontSize: 13, color: "var(--dsw-alias-label-primary, #222)" };
    const hint = { fontSize: 11.5, color: "var(--dsw-alias-label-tertiary, #8a8f98)", lineHeight: 1.6 };
    const input = { width: 110, padding: "5px 8px", borderRadius: 6, border: "1px solid var(--dsw-alias-border-l2, #ccc)", background: "transparent", color: "inherit", fontSize: 12 };
    const wide = { ...input, width: "100%" };
    const mono = { fontFamily: "ui-monospace, monospace", fontSize: 11.5, whiteSpace: "pre-wrap", overflowWrap: "anywhere" };

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
      const [providers, setProviders] = useState([]);
      const [models, setModels] = useState([]);
      const [reasoning, setReasoning] = useState(null);
      const [catalogNote, setCatalogNote] = useState(null);
      const [testBusy, setTestBusy] = useState(false);
      const [test, setTest] = useState(null);
      const alive = useRef(true);

      // Reset on mount: a StrictMode remount runs the cleanup below and would
      // otherwise leave alive.current false forever, freezing every busy flag.
      useEffect(() => {
        alive.current = true;
        return () => {
          alive.current = false;
        };
      }, []);

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

      const loadCatalog = async (provider, model) => {
        setCatalogNote(null);
        try {
          const qs = new URLSearchParams();
          if (provider) qs.set("provider", provider);
          if (provider && model) qs.set("model", model);
          const suffix = qs.toString() ? "?" + qs.toString() : "";
          const resp = await fetch(MODELS + suffix, { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          if (!alive.current) return;
          if (Array.isArray(data.providers)) setProviders(data.providers);
          setModels(Array.isArray(data.models) ? data.models : []);
          setReasoning(data.reasoning ?? null);
          const note = data.reasoningError || data.catalogError;
          if (note) setCatalogNote(String(note));
        } catch (error) {
          if (!alive.current) return;
          setModels([]);
          setReasoning(null);
          setCatalogNote("模型目录不可用：" + String(error.message || error) + "（仍可手填 provider/model）");
        }
      };

      useEffect(() => { load(false); }, []);
      useEffect(() => {
        if (cfg === null) return;
        // Typing a provider/model by hand fires this on every keystroke; debounce
        // so the catalog is queried once the value settles.
        const timer = setTimeout(() => loadCatalog(cfg.provider ?? "", cfg.model ?? ""), 400);
        return () => clearTimeout(timer);
      }, [cfg && cfg.provider, cfg && cfg.model]);

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

      const runTest = async () => {
        setTestBusy(true);
        setTest(null);
        setErr(null);
        try {
          const resp = await fetch(TEST, { method: "POST", headers: { ...GUARD, "content-type": "application/json" }, body: JSON.stringify({}) });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          if (alive.current) setTest(data);
          const status = await fetch(STATUS, { headers: GUARD }).then((r) => r.json()).catch(() => null);
          if (status && Array.isArray(status.events) && alive.current) setEvents(status.events);
        } catch (error) {
          if (alive.current) setTest({ ok: false, error: String(error.message || error) });
        } finally {
          if (alive.current) setTestBusy(false);
        }
      };

      if (cfg === null) return h("div", { style: { padding: 24, color: "#777" } }, "加载配置…");

      const advertised = reasoning && Array.isArray(reasoning.efforts) ? reasoning.efforts : null;
      const currentEffort = cfg.reasoningEffort ?? "auto";
      const effortChoices = ["auto", ...(advertised ? advertised.map((entry) => entry.id) : [...EFFORT_IDS])];
      if (!effortChoices.includes(currentEffort)) effortChoices.push(currentEffort);

      // A route typed by hand may not be in the catalog; keep it selectable.
      const providerChoices = providers.map((entry) => entry.id);
      if (cfg.provider && !providerChoices.includes(cfg.provider)) providerChoices.push(cfg.provider);
      const modelChoices = models.map((entry) => entry.id);
      if (cfg.model && !modelChoices.includes(cfg.model)) modelChoices.push(cfg.model);

      // Latest outcome decides the banner, not "was there ever a success": a route
      // that failed 24 blocks and succeeded once is still broken. A deterministic
      // fallback counts as a degradation too — that replaces real thinking with the
      // two-node stub, which is the issue-#3 symptom users notice as "effect gone".
      const outcome = (entry) => {
        if (entry.event === "block-replaced") return "ok";
        if (entry.event === "block-failed") return "fail";
        if (entry.event === "thinking-failed") return "fail";
        if (entry.event === "thinking-degenerate" && entry.action === "fallback") return "fail";
        return null;
      };
      const judged = events.map((entry, index) => ({ entry, index, kind: outcome(entry) })).filter((row) => row.kind !== null);
      const lastJudged = judged.length ? judged[judged.length - 1] : null;
      const lastFailure = lastJudged && lastJudged.kind === "fail" ? lastJudged.entry : null;
      const showFailure = lastFailure !== null;
      const recent = events.slice(-5).reverse();

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
        showFailure ? h("div", { style: { marginTop: 10, padding: "8px 10px", borderRadius: 8, background: "rgba(192,57,43,0.08)", border: "1px solid rgba(192,57,43,0.25)", fontSize: 12, color: "#c0392b" } },
          "最近的判定是失败：" + String(lastFailure.message || lastFailure.event),
          h("div", { style: { ...hint, color: "inherit", marginTop: 4 } }, "压缩失败会按设计保留原文，所以界面上只会看到“没效果”。修好路由或档位后再点“一键测试”。")
        ) : null,
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
            h("div", { style: label }, "压缩器推理档位"),
            h("div", { style: hint }, advertised
              ? "该路由公开的档位：" + advertised.map((entry) => (entry.name && entry.name !== entry.id ? entry.id + "（" + entry.name + "）" : entry.id)).join(" / ") + (reasoning.defaultEffort ? "；默认 " + reasoning.defaultEffort : "")
              : "自动 = 先发 off；宿主拒绝该档位时不发这个字段，由供应商取默认值（压缩器会自己思考，慢一些但一定能跑）。选固定档位则原样发送，被拒后同样降级。")
          ),
          h("select", {
            value: currentEffort,
            style: { ...input, width: 220 },
            onChange: (e) => set({ reasoningEffort: e.target.value }),
          },
            ...effortChoices.map((id) => h("option", { key: id, value: id }, EFFORT_LABELS[id] ?? id))
          )
        ),
        h("div", { style: row },
          h("div", null,
            h("div", { style: label }, "压缩器路由（可选）"),
            h("div", { style: hint }, "留空 = 跟随当前请求的 provider/model。也可以从宿主的模型目录里选，避免填到不存在的模型。")
          )
        ),
        h("div", { style: { display: "flex", gap: 8 } },
          h("input", { placeholder: "provider（留空跟随当前路由）", value: cfg.provider ?? "", style: wide, onChange: (e) => set({ provider: e.target.value || null }) }),
          h("input", { placeholder: "model（留空跟随当前路由）", value: cfg.model ?? "", style: wide, onChange: (e) => set({ model: e.target.value || null }) })
        ),
        h("div", { style: { display: "flex", gap: 8, marginTop: 8 } },
          h("select", {
            value: cfg.provider ?? "",
            style: { ...input, width: "50%" },
            onChange: (e) => set({ provider: e.target.value || null, model: null }),
          },
            h("option", { value: "" }, "从模型目录选 provider…"),
            ...providerChoices.map((id) => {
              const entry = providers.find((candidate) => candidate.id === id);
              return h("option", { key: id, value: id }, entry && entry.name && entry.name !== id ? entry.name + "（" + id + "）" : id);
            })
          ),
          h("select", {
            value: cfg.model ?? "",
            style: { ...input, width: "50%" },
            disabled: !cfg.provider,
            onChange: (e) => set({ model: e.target.value || null }),
          },
            h("option", { value: "" }, cfg.provider ? "model（留空跟随当前请求）" : "先选 provider"),
            ...modelChoices.map((id) => {
              const entry = models.find((candidate) => candidate.id === id);
              return h("option", { key: id, value: id }, entry && entry.name && entry.name !== id ? entry.name + "（" + id + "）" : id);
            })
          )
        ),
        catalogNote ? h("div", { style: { ...hint, marginTop: 6 } }, String(catalogNote)) : null,
        h("div", { style: { ...row, justifyContent: "flex-start", flexWrap: "wrap", gap: 10, marginTop: 12 } },
          h("button", { style: { padding: "7px 16px", borderRadius: 6, border: "none", background: busy ? "#9db8f0" : "#2f6fed", color: "#fff", cursor: busy ? "wait" : "pointer" }, disabled: busy, onClick: save }, busy ? "保存中…" : "保存"),
          h("button", { style: { padding: "7px 14px", borderRadius: 6, border: "1px solid #ccc", background: "transparent", color: "inherit", cursor: busy ? "default" : "pointer" }, disabled: busy, onClick: () => load(true) }, "刷新"),
          h("button", { style: { padding: "7px 14px", borderRadius: 6, border: "1px solid #2f6fed", background: "transparent", color: "#2f6fed", cursor: testBusy ? "wait" : "pointer" }, disabled: testBusy || busy, onClick: runTest }, testBusy ? "测试中…" : "一键测试"),
          h("span", { style: hint }, "原始 thinking 归档在 $DSH_HOME/plugin-data/iot-thinking/originals/")
        ),
        h("div", { style: { ...hint, marginTop: 4 } }, "一键测试会用当前设置真实调用一次压缩器（约 10–30 s，按一次请求计费），并按与真实链路相同的“必须变短才替换”规则判定，所以不会假绿。"),
        err ? h("div", { style: { color: "#c0392b", marginTop: 10, fontSize: 12, whiteSpace: "pre-wrap" } }, String(err)) : null,
        msg ? h("div", { style: { color: "#188a4b", marginTop: 10, fontSize: 12 } }, String(msg)) : null,
        test ? h("div", { style: { marginTop: 10, fontSize: 12 } },
          h("div", { style: { color: test.ok ? "#188a4b" : "#b76e00", fontWeight: 600 } },
            test.ok
              ? "测试通过：" + test.charsIn + " → " + test.charsOut + " 字（" + test.ms + "ms" + (test.effort ? "，档位 " + test.effort : "，未发档位") + (test.effortFallback ? "，已自动降级" : "") + "）"
              : test.error
                ? "测试失败：" + String(test.error)
                : test.reason === "disabled"
                  ? "未测试：插件当前是关闭状态，真实链路上任何块都不会被压"
                  : test.reason === "below-min-chars"
                    ? "未测试：这段输入比最小压缩字符数（" + test.minChars + "）短，真实链路上会被直接跳过"
                    : test.reason === "compressor-failed"
                      ? "测试未通过：压缩器没有给出结果（原文已保留，详情见最近事件）"
                      : test.reason === "empty-output"
                        ? "测试未通过：压缩器返回了空内容，真实链路上这一块会保留原文"
                        : "测试未通过：压缩结果没有变短（" + test.charsIn + " → " + test.charsOut + " 字），真实链路上这一块会保留原文"
          ),
          test.text ? h("div", { style: { ...mono, marginTop: 6, maxHeight: 220, overflow: "auto", background: "rgba(127,127,127,0.08)", padding: 8, borderRadius: 6 } }, String(test.text)) : null
        ) : null,
        recent.length ? h("div", { style: { marginTop: 14, borderTop: "1px solid var(--dsw-alias-border-l2, #eee)", paddingTop: 10 } },
          h("div", { style: { ...hint, marginBottom: 6 } }, "最近事件"),
          ...recent.map((event, index) => h("div", { key: index, style: { ...mono, color: "var(--dsw-alias-label-tertiary, #777)", padding: "1px 0" } },
            (event.event || "?")
              + (event.charsIn != null ? "  " + event.charsIn + "→" + (event.charsOut ?? "?") + " chars" : "")
              + (event.reason ? "  (" + event.reason + ")" : "")
              + (event.effort != null ? "  effort=" + String(event.effort) : "")
              + (event.effortFallback ? "  (downgraded)" : "")
              + (event.ms != null ? "  " + event.ms + "ms" : "")
              + (event.message ? "  " + String(event.message) : "")
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
