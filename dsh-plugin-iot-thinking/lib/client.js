// 设置卡 + 会话头部的档位下拉。
//
// dsh loads a loose plugin's client bundle through window.__ModuleLoader__ and
// serves the file named by package.json exports["./client"]. Plain JS + require,
// no build step.
//
// 两个入口写的是同一张表（`sessions[sessionId]`）：
//   * 设置卡：全局默认档位、逐档限制、按 agent preset 的映射、自定义预设；
//   * 头部下拉：**这个会话**单独用哪一档（覆盖 preset 映射与全局默认）。
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
    const SESSION = "/iot-thinking/api/session";
    const PRESETS = "/iot-thinking/api/presets";
    const STRATEGY = "/iot-thinking/api/strategy";
    const PRESET_DIR = "/iot-thinking/api/preset-dir";
    // The host webserver authenticates nothing; the card proves itself with a
    // header a cross-site page cannot set without a preflight the server
    // refuses (see lib/config-api.mjs guardRequest).
    const GUARD = { "x-iot-thinking": "1" };

    const FALLBACK = {
      enabled: true,
      strategy: "iot-full",
      strategies: {},
      agentPresets: {},
      sessions: {},
      rewrite: { provider: null, model: null, effort: "auto", fallback: "keep" },
      archive: true,
    };

    // Mirrors lib/strategies.mjs. Duplicated because this bundle is plain JS
    // loaded by the host module loader; it cannot import host code. The host
    // always sends the real catalog, so this is only the pre-load placeholder
    // (and the degraded view when /status is unreachable).
    const STRATEGY_FALLBACK = [
      { id: "iot-compact", label: "压缩 · 极简（3 标签）", family: "items", summary: "压得最狠，短任务用。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 5, maxNeutral: 0 } },
      { id: "iot-balanced", label: "压缩 · 均衡（5 标签）", family: "items", summary: "保留计划与决定。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 6, maxNeutral: 0 } },
      { id: "iot-full", label: "压缩 · 完整（8 标签）", family: "items", summary: "8 标签全量，质量优先。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 8, maxNeutral: 2 } },
      { id: "off", label: "关闭（本会话不做任何改写）", family: "off", summary: "保留原生 thinking。", defaults: { minChars: 0, maxChars: 0, maxTokens: 0 } },
    ];

    // Mirrors lib/effort.mjs EFFORT_IDS.
    const EFFORT_IDS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
    const EFFORT_LABELS = {
      auto: "自动（先试 off，被拒则降级）",
      off: "off — 不花预算在改写器自己的思考上",
      minimal: "minimal",
      low: "low",
      medium: "medium",
      high: "high",
      xhigh: "xhigh",
      max: "max",
    };

    const LIMIT_FIELDS = [
      { key: "minChars", title: "最小改写字符数", desc: "短于这个长度的 thinking 不动，避免为几句话多调一次模型。", min: 0 },
      { key: "maxChars", title: "最大输入字符数", desc: "超过这个长度的 thinking 直接保留原文，避免改写调用自己撑爆窗口。", min: 1 },
      { key: "maxTokens", title: "输出上限 (tokens)", desc: "改写器的 maxTokens。", min: 1 },
      { key: "temperature", title: "temperature", desc: "留空 = 不发该字段，交给模型默认值（有的模型只接受默认值）。", min: 0, step: 0.1 },
    ];
    const ITEM_LIMIT_FIELDS = [
      { key: "maxNodes", title: "最多节点数", desc: "超过后按优先级保留：决定 > 反对 > 权衡 > 风险 > 待定 > 计划 > 支持 > 中性。", min: 1 },
      { key: "maxNeutral", title: "最多 [中性] 节点", desc: "多余的 [中性] 会合并成一条；0 = 完全不要中性节点。", min: 0 },
    ];

    const h = react.createElement;

    /**
     * 渲染期兜底：设置卡抛错时不要整页白屏，把错误显示出来。
     * 宿主 React 提供 Component 时用 class 边界；没有就退化成直接渲染。
     */
    function withErrorBoundary(Component, title) {
      const Base = react.Component;
      if (typeof Base !== "function") return Component;
      class Boundary extends Base {
        constructor(props) {
          super(props);
          this.state = { error: null };
        }
        static getDerivedStateFromError(error) {
          return { error };
        }
        componentDidCatch(error, info) {
          console.error("[iot-thinking] " + title + " 渲染失败", error, info);
        }
        render() {
          if (this.state.error) {
            return h("div", { style: { padding: 16, fontSize: 12, color: "#c0392b", whiteSpace: "pre-wrap" } },
              title + "渲染失败：" + String(this.state.error?.message ?? this.state.error),
              h("div", { style: { marginTop: 6, color: "#8a8f98" } }, "详情见浏览器控制台；也可先按 dsh-unlag 的刷新按钮重建界面。")
            );
          }
          return h(Component, this.props);
        }
      }
      return Boundary;
    }
    const box = { border: "1px solid var(--dsw-alias-border-l2, #e3e6ea)", borderRadius: 10, padding: 14, maxWidth: 760 };
    const hint = { fontSize: 11.5, color: "var(--dsw-alias-label-tertiary, #8a8f98)", lineHeight: 1.6 };
    const input = { width: 110, padding: "5px 8px", borderRadius: 6, border: "1px solid var(--dsw-alias-border-l2, #ccc)", background: "transparent", color: "inherit", fontSize: 12 };
    const mono = { fontFamily: "ui-monospace, monospace", fontSize: 11.5, whiteSpace: "pre-wrap", overflowWrap: "anywhere" };
    const section = { marginTop: 14, borderTop: "1px solid var(--dsw-alias-border-l2, #eee)", paddingTop: 12 };
    const sectionTitle = { fontSize: 13, fontWeight: 600, marginBottom: 4 };

    // 统一的控件形状：按钮、面板、字段标题、文本域。
    const btn = {
      padding: "6px 12px",
      borderRadius: 7,
      border: "1px solid var(--dsw-alias-border-l2, #d9dde3)",
      background: "transparent",
      color: "inherit",
      cursor: "pointer",
      fontSize: 12,
      lineHeight: 1.2,
      whiteSpace: "nowrap",
    };
    const btnOutline = { ...btn, border: "1px solid #2f6fed", color: "#2f6fed" };
    const btnSolid = { ...btn, border: "1px solid #2f6fed", background: "#2f6fed", color: "#fff" };
    const btnSmall = { ...btn, padding: "3px 9px", fontSize: 11.5 };
    const panel = {
      marginTop: 10,
      padding: 12,
      borderRadius: 10,
      border: "1px solid var(--dsw-alias-border-l2, #e6e9ee)",
      background: "rgba(127,127,127,0.035)",
    };
    const fieldName = { fontSize: 12, fontWeight: 600, marginBottom: 6, color: "var(--dsw-alias-label-secondary, #555)" };
    const grow = { ...input, flex: 1, minWidth: 0 };
    const textareaStyle = {
      width: "100%",
      boxSizing: "border-box",
      padding: 10,
      borderRadius: 8,
      border: "1px solid var(--dsw-alias-border-l2, #d9dde3)",
      background: "transparent",
      color: "inherit",
      fontFamily: "ui-monospace, monospace",
      fontSize: 12,
      lineHeight: 1.65,
      resize: "vertical",
    };
    const humanBytes = (n) => (Number.isFinite(n) && n >= 1024 ? (n / 1024).toFixed(1) + " KB" : (n ?? 0) + " B");

    /**
     * 预设文件列表 → 下拉的第二级选项。
     *
     * 新建/保存预设后必须重算这一份：下拉的选项来自 /api/status 的 choices，
     * 那是进页面时取的；只刷「目录里的文件」那块面板，下拉里看不到新文件。
     *
     * @param {{name: string, bytes?: number}[]} files
     * @param {boolean} hasInline - 是否有贴入的文本（有才列出「贴入的文本」）
     */
    function presetOptions(files, hasInline) {
      const options = [];
      if (hasInline) options.push({ value: "custom", label: "贴入的文本", bytes: null });
      for (const file of Array.isArray(files) ? files : []) {
        if (!file || typeof file.name !== "string") continue;
        options.push({ value: "custom:" + file.name, label: file.name, bytes: file.bytes ?? null });
      }
      return options;
    }

    /**
     * 本页改动 → 要提交的字段。
     *
     * 只带差异：整份 PUT 会把加载时的旧 sessions / agentPresets 覆盖回去，
     * 而这两张表是"别处也能写"的（顶栏下拉、/iot 命令、另一个标签页）。
     *
     * @param {object} next - 本页当前值。
     * @param {object|null} base - 上次落盘时的值（null = 第一次保存，全量提交）。
     * @returns {object} 只含变化的键。
     */
    function changedFields(next, base) {
      if (!base) return { ...next };
      const patch = {};
      for (const [key, value] of Object.entries(next ?? {})) {
        if (JSON.stringify(value) !== JSON.stringify(base[key])) patch[key] = value;
      }
      return patch;
    }

    /**
     * 切档时的"目标值"合成：切到 custom 时不能沿用旧档位的 target
     * （`targetOf("iot-balanced")` 返回 "iot-balanced"，会被当成预设名，
     * 结果 strategy 仍是原档位 —— 用户看到"选了预设但没生效"）。
     * 抽成纯函数是为了让这条规则能被自测直接覆盖。
     *
     * @param {string} tier
     * @param {string} target
     * @param {() => string} firstCustom
     */
    function composeTier(tier, target, firstCustom) {
      if (tier !== "custom") return tier;
      if (typeof target === "string" && (target === "custom" || target.startsWith("custom:"))) return target;
      return firstCustom();
    }

    const sectionHead = { display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 10 };
    const limitsGrid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 12, marginTop: 10 };
    const limitCell = { display: "flex", flexDirection: "column", gap: 4, minWidth: 0 };
    const mappingTable = { marginTop: 8, border: "1px solid var(--dsw-alias-border-l2, #e6e9ee)", borderRadius: 10, overflow: "hidden" };
    const mappingRow = {
      display: "grid",
      gridTemplateColumns: "minmax(0, 1fr) 18px minmax(0, 1.15fr) 148px",
      gap: 6,
      alignItems: "center",
      padding: "8px 10px",
      borderTop: "1px solid var(--dsw-alias-border-l2, #eef0f3)",
    };
    const mappingHeadRow = { padding: "5px 10px", borderTop: "none", background: "rgba(127,127,127,0.06)" };
    const monoLabel = { fontFamily: "ui-monospace, monospace", fontSize: 12, fontWeight: 600, overflowWrap: "anywhere" };
    const monoBlock = { fontFamily: "ui-monospace, monospace", fontSize: 11.5, whiteSpace: "pre-wrap", overflowWrap: "anywhere" };
    const panelTight = { marginTop: 0, padding: 10, borderRadius: 8, background: "rgba(127,127,127,0.06)" };


    /** 生效限制：目录默认值 ← 该档位的覆盖。 */
    const limitsFor = (catalog, cfg, id) => {
      const entry = catalog.find((candidate) => candidate.id === id) ?? STRATEGY_FALLBACK.find((candidate) => candidate.id === id);
      const overrides = (cfg.strategies && cfg.strategies[id]) || {};
      return { ...((entry && entry.defaults) || {}), ...overrides };
    };

    function IotThinkingCard() {
      const { useState, useEffect, useRef } = react;
      const [cfg, setCfg] = useState(null);
      const [catalog, setCatalog] = useState(STRATEGY_FALLBACK);
      const [events, setEvents] = useState([]);
      const [msg, setMsg] = useState(null);
      const [err, setErr] = useState(null);
      const [busy, setBusy] = useState(false);
      const [providers, setProviders] = useState([]);
      const [models, setModels] = useState([]);
      /** 指导模型自己的目录：与压缩路由的 models 分开，互不覆盖。 */
      const [advisorModels, setAdvisorModels] = useState([]);
      const [advisorReasoning, setAdvisorReasoning] = useState(null);
      const [reasoning, setReasoning] = useState(null);
      const [sessionRoute, setSessionRoute] = useState(null);
      const [catalogNote, setCatalogNote] = useState(null);
      const [testBusy, setTestBusy] = useState(false);
      const [test, setTest] = useState(null);
      const [testStrategy, setTestStrategy] = useState(null);
      /** 自测的指导模式：空 = 用当前配置；否则强制 builtin/external 便于对比。 */
      const [testGuidance, setTestGuidance] = useState("");
      const [presetDraft, setPresetDraft] = useState({ preset: "", strategy: "iot-full", style: "" });
      // preset 清单来自宿主（/api/presets）。拿不到时退化成手输 —— headless 组合里
      // 根本没有 agentPresets 服务，那不是错误。
      const [presetInfo, setPresetInfo] = useState(null);
      const [presetDetail, setPresetDetail] = useState(null);
      /** 档位提示词原文（/api/strategy），键是 `strategy:<preset>`。 */
      const [strategyDetail, setStrategyDetail] = useState(null);
      // 卡片是"改完再保存"的：所有控件（含"添加"/"删除"）只改内存，
      // 必须点保存才落盘。saved 保留上次落盘时的形状，用来显示"有未保存的修改"——
      // 没有这个提示，"添加"看起来像已经生效了。
      const [saved, setSaved] = useState(null);
      /** 所有下拉共用的一份"可选值"：内置档位 + 目录里的自定义预设 + 贴入的文本。 */
      const [choices, setChoices] = useState({ tiers: [], presets: [] });
      /** 用户配置（自定义预设）的目录与文件列表。 */
      const [presetFiles, setPresetFiles] = useState([]);
      /** 内置预设（随插件发布，只读）：清单来自 /api/status 的 choices。 */
      const [builtinPresets, setBuiltinPresets] = useState([]);
      /** 正在编辑的内置预设：{ id, text }；text 为空表示"还没动"。 */
      const [builtinEditor, setBuiltinEditor] = useState({ id: "", text: "" });
      const [presetDirSet, setPresetDirSet] = useState(false);
      /** 正在编辑的预设：`{ name, text }`；name 为空表示"还没起名"。 */
      const [editor, setEditor] = useState({ name: "", text: "", note: null });
      const [saveName, setSaveName] = useState("");
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
          setSaved(JSON.stringify(data.config ?? FALLBACK));
          setSessionRoute(data.sessionRoute && typeof data.sessionRoute === "object" ? data.sessionRoute : null);
          if (Array.isArray(data.strategies)) setCatalog(data.strategies);
          if (data.choices && typeof data.choices === "object" && !Array.isArray(data.choices)) setChoices(data.choices);
          if (Array.isArray(data.presetFiles)) setPresetFiles(data.presetFiles);
          if (data.choices && Array.isArray(data.choices.builtinPresets)) setBuiltinPresets(data.choices.builtinPresets);
          if (typeof data.presetDirSet === "boolean") setPresetDirSet(data.presetDirSet);
          setEvents(Array.isArray(data.events) ? data.events : []);
          if (announce) setMsg("已从 host 重新加载");
        } catch (error) {
          setErr("host 配置 API 不可用（" + String(error.message || error) + "）。先确认 dsh web 已重启且插件已加载。");
          setCfg((current) => current ?? FALLBACK);
        } finally {
          if (alive.current) setBusy(false);
        }
      };

      /**
       * 「保存并刷新」：列表用的是 host 里**已保存**的目录，所以输入框改了没保存时
       * 直接刷新只会看到旧结果（甚至"还没配置"）。这里先保存目录，再列。
       */
      const refreshPresetDir = async () => {
        const dir = String(cfg.presetDir ?? "").trim();
        if (!dir) {
          setErr("先填预设目录（没有默认值）");
          return;
        }
        try {
          const put = await fetch(API, {
            method: "PUT",
            headers: { ...GUARD, "content-type": "application/json" },
            body: JSON.stringify({ presetDir: dir }),
          });
          const saved = await put.json().catch(() => null);
          if (!put.ok) throw new Error((saved && saved.error) || "HTTP " + put.status);
          if (saved && saved.config) {
            setCfg(saved.config);
            setSaved(JSON.stringify(saved.config));
          }
          const resp = await fetch(PRESET_DIR, { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (!resp.ok || !data || !alive.current) return;
          if (Array.isArray(data.presets)) {
            setPresetFiles(data.presets);
            setChoices((current) => ({
              ...(current ?? {}),
              presets: presetOptions(data.presets, String(cfg.customText ?? "").trim().length > 0),
            }));
          }
          setPresetDirSet(data.configured === true);
          setMsg("已保存目录并刷新：" + (data.presets ? data.presets.length : 0) + " 个预设文件");
        } catch (error) {
          setErr("刷新失败：" + String(error.message || error));
        }
      };

      /** 读一份预设的内容进编辑器。 */
      const openPreset = async (name) => {
        setEditor({ name, text: "", note: "读取中…" });
        try {
          const resp = await fetch(PRESET_DIR + "?read=" + encodeURIComponent(name), { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (!alive.current) return;
          if (!resp.ok || !data) {
            setEditor({ name, text: "", note: "读取失败" });
            return;
          }
          setEditor({ name, text: String(data.text ?? ""), note: data.error ? String(data.error) : null });
        } catch (error) {
          if (alive.current) setEditor({ name, text: "", note: String(error.message || error) });
        }
      };

      /** 把编辑器里的内容写回目录（覆盖同名文件；没写后缀就补 .md）。 */
      const savePresetFile = async (name, text) => {
        const target = String(name ?? "").trim();
        if (!target) {
          setErr("先给它起个文件名（可以带 .md / .txt，不写就按 .md 存）");
          return false;
        }
        try {
          const resp = await fetch(PRESET_DIR, {
            method: "POST",
            headers: { ...GUARD, "content-type": "application/json" },
            body: JSON.stringify({ name: target, text }),
          });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          if (data && Array.isArray(data.presets) && alive.current) {
            setPresetFiles(data.presets);
            // 下拉的选项也得跟着变，否则刚存的预设要重进设置页才出现。
            setChoices((current) => ({
              ...(current ?? {}),
              presets: presetOptions(data.presets, String(cfg.customText ?? "").trim().length > 0),
            }));
          }
          const savedName = (data && data.name) || target;
          setEditor({ name: savedName, text, note: `已写入 ${savedName}` });
          setSaveName("");
          return true;
        } catch (error) {
          setErr("保存预设失败：" + String(error.message || error));
          return false;
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

      useEffect(() => { load(false); loadPresets(); }, []);

      // 指导模型的模型目录与真实档位：按 advisor.route 单独拉，不能复用压缩路由的目录。
      useEffect(() => {
        if (cfg === null) return;
        const route = cfg.advisor && typeof cfg.advisor === "object" && cfg.advisor.route && typeof cfg.advisor.route === "object"
          ? cfg.advisor.route
          : {};
        const provider = typeof route.provider === "string" ? route.provider : "";
        const model = typeof route.model === "string" ? route.model : "";
        let cancelled = false;
        const timer = setTimeout(async () => {
          try {
            const qs = new URLSearchParams();
            if (provider) qs.set("provider", provider);
            if (provider && model) qs.set("model", model);
            const suffix = qs.toString() ? "?" + qs.toString() : "";
            const resp = await fetch(MODELS + suffix, { headers: GUARD });
            const data = await resp.json().catch(() => null);
            if (cancelled || !resp.ok || !data) return;
            setAdvisorModels(Array.isArray(data.models) ? data.models : []);
            setAdvisorReasoning(data.reasoning ?? null);
          } catch {
            if (!cancelled) {
              setAdvisorModels([]);
              setAdvisorReasoning(null);
            }
          }
        }, 400);
        return () => {
          cancelled = true;
          clearTimeout(timer);
        };
      }, [cfg?.advisor?.route?.provider, cfg?.advisor?.route?.model]);
      useEffect(() => {
        if (cfg === null) return;
        // Typing a provider/model by hand fires this on every keystroke; debounce
        // so the catalog is queried once the value settles.
        const rewrite = cfg.rewrite && typeof cfg.rewrite === "object" ? cfg.rewrite : {};
        const provider = typeof rewrite.provider === "string" ? rewrite.provider : (typeof cfg.provider === "string" ? cfg.provider : "");
        const model = typeof rewrite.model === "string" ? rewrite.model : (typeof cfg.model === "string" ? cfg.model : "");
        const timer = setTimeout(() => loadCatalog(provider, model), 400);
        return () => clearTimeout(timer);
      }, [cfg && cfg.rewrite && cfg.rewrite.provider, cfg && cfg.rewrite && cfg.rewrite.model, cfg && cfg.provider, cfg && cfg.model]);

      const set = (patch) => setCfg((current) => ({ ...(current ?? FALLBACK), ...patch }));

      const save = async () => {
        setBusy(true);
        setErr(null);
        setMsg(null);
        try {
          // 只提交本页真的改过的字段：整份 PUT 会把加载时那张 sessions 快照写回去，
          // 于是"卡还没刷新时用顶栏/命令改的会话覆盖"会被静默抹掉。
          const patch = changedFields(cfg, saved === null ? null : JSON.parse(saved));
          if (Object.keys(patch).length === 0) {
            setMsg("没有改动");
            return;
          }
          const resp = await fetch(API, { method: "PUT", headers: { ...GUARD, "content-type": "application/json" }, body: JSON.stringify(patch) });
          const data = await resp.json().catch(() => null);
          if (!resp.ok) throw new Error((data && data.error) || "HTTP " + resp.status);
          if (data && data.config) {
            setCfg(data.config);
            setSaved(JSON.stringify(data.config));
          }
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
          const payload = {};
          if (testStrategy) payload.strategy = testStrategy;
          if (testGuidance) payload.guidance = { enabled: true, mode: testGuidance };
          const body = JSON.stringify(payload);
          const resp = await fetch(TEST, { method: "POST", headers: { ...GUARD, "content-type": "application/json" }, body });
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

      // 会话覆盖是**按会话**写的，这张卡看不到具体会话名，只能显示条数并允许全清。
      // 单会话的切换在会话头部的下拉里（同一个表的另一条入口）。
      const sessionKeys = cfg && cfg.sessions ? Object.keys(cfg.sessions) : [];
      const presetKeys = cfg && cfg.agentPresets ? Object.keys(cfg.agentPresets) : [];

      /** 拉 preset 清单（可选能力：失败就退化成手输 id）。 */
      const loadPresets = async () => {
        try {
          const resp = await fetch(PRESETS, { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (!resp.ok || !data) return;
          if (alive.current) setPresetInfo(data);
        } catch {
          /* 手输仍然可用 */
        }
      };

      /** 展开一个 preset 的组成（有哪些插件行）。 */
      const showPresetDetail = async (id) => {
        if (presetDetail && presetDetail.id === id) {
          setPresetDetail(null);
          return;
        }
        setPresetDetail({ id, loading: true });
        try {
          const resp = await fetch(PRESETS + "?detail=" + encodeURIComponent(id), { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (alive.current && resp.ok && data) setPresetDetail(data.detail ?? { id, rows: [], missing: true });
        } catch {
          if (alive.current) setPresetDetail({ id, rows: [], missing: true });
        }
      };

      /** 展开一条档位的提示词原文（真正会发给模型的那段）。 */
      const showStrategyPrompt = async ({ preset, strategy }) => {
        const id = "strategy:" + preset;
        if (strategyDetail && strategyDetail.id === id) {
          setStrategyDetail(null);
          return;
        }
        setStrategyDetail({ id, strategy, loading: true });
        // `custom:名字` / `builtin:名字` 不是注册表里的档位 id：拆成 id + preset，
        // 否则服务端 strategyById 找不到，直接 404。
        const text = typeof strategy === "string" ? strategy : "";
        const qs = new URLSearchParams();
        if (text.startsWith("custom:")) {
          qs.set("id", "custom");
          qs.set("preset", text.slice("custom:".length));
        } else if (text.startsWith("builtin:")) {
          qs.set("id", "custom");
          qs.set("preset", text);
        } else {
          qs.set("id", text);
        }
        try {
          const resp = await fetch(STRATEGY + "?" + qs.toString(), { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (alive.current) setStrategyDetail({ id, strategy, ...(resp.ok && data ? data : { error: "读取失败" }) });
        } catch (error) {
          if (alive.current) setStrategyDetail({ id, strategy, error: String(error.message || error) });
        }
      };

      /** 写一行映射。`entry` 是档位名（含 `custom:名字`）；`null` = 删这一行。 */
      const setPreset = (preset, entry) => {
        const next = { ...(cfg.agentPresets ?? {}) };
        if (!preset) return;
        if (entry === null) delete next[preset];
        else next[preset] = entry;
        set({ agentPresets: next });
      };

      /** 一行映射的规范化读法：老文件里的对象形态取 `strategy`。 */
      const entryOf = (preset) => {
        const raw = cfg.agentPresets?.[preset];
        if (typeof raw === "string") return { strategy: raw, preset: null };
        if (raw && typeof raw === "object") {
          return {
            strategy: String(raw.strategy ?? ""),
            // 对象形态带的 preset 是"用哪份规格"：丢了它，下拉会显示成"贴入的文本"。
            preset: typeof raw.preset === "string" && raw.preset.trim() ? raw.preset.trim() : null,
          };
        }
        return { strategy: "", preset: null };
      };

      // 新增行默认挑一个还没映射的 preset，省得用户面对一个空下拉不知道怎么添加。
      useEffect(() => {
        const list = presetInfo && Array.isArray(presetInfo.presets) ? presetInfo.presets : null;
        if (!list) return;
        setPresetDraft((draft) => {
          if (draft.preset) return draft;
          const mapped = cfg?.agentPresets ?? {};
          const next = list.find((entry) => !(entry.id in mapped));
          return next ? { ...draft, preset: next.id } : draft;
        });
      }, [presetInfo, cfg && cfg.agentPresets]);

      /** 「添加」= 把草案行写进本页内存的那张表；落盘仍要按「保存」。 */
      const addPreset = () => {
        const preset = presetDraft.preset.trim();
        if (!preset) return;
        setPreset(preset, presetDraft.strategy);
        setPresetDraft({ ...presetDraft, preset: "" });
      };

      // 与上次落盘快照比较，判断"有没有还没保存的改动"。
      const dirty = saved !== null && cfg !== null && JSON.stringify(cfg) !== saved;

      if (cfg === null) return h("div", { style: { padding: 24, color: "#777" } }, "加载配置…");

      // 改写器路由：新形状是 cfg.rewrite；旧 host 只有顶层 provider/model，
      // 这里兼容读取，写回时一律写 rewrite 块。
      const rewriteCfg = (() => {
        const rewrite = cfg.rewrite && typeof cfg.rewrite === "object" && !Array.isArray(cfg.rewrite) ? cfg.rewrite : {};
        const pick = (key, legacy) => {
          if (typeof rewrite[key] === "string") return rewrite[key];
          if (rewrite[key] === null) return null;
          return typeof legacy === "string" ? legacy : null;
        };
        return {
          provider: pick("provider", cfg.provider),
          model: pick("model", cfg.model),
          effort: typeof rewrite.effort === "string" ? rewrite.effort : (typeof cfg.reasoningEffort === "string" ? cfg.reasoningEffort : "auto"),
          fallback: rewrite.fallback === "session" ? "session" : "keep",
        };
      })();
      const setRewrite = (patch) => set({ rewrite: { ...rewriteCfg, ...patch } });

      const advisorCfg = (() => {
        const advisor = cfg.advisor && typeof cfg.advisor === "object" && !Array.isArray(cfg.advisor) ? cfg.advisor : {};
        const route = advisor.route && typeof advisor.route === "object" ? advisor.route : {};
        const guidance = cfg.guidance && typeof cfg.guidance === "object" && !Array.isArray(cfg.guidance) ? cfg.guidance : {};
        return {
          // 开关与模式统一走 guidance；advisor 只保留外部指导的路由与预算。
          enabled: typeof guidance.enabled === "boolean" ? guidance.enabled : advisor.enabled === true,
          mode: guidance.mode === "builtin" ? "builtin" : "external",
          provider: typeof route.provider === "string" ? route.provider : null,
          model: typeof route.model === "string" ? route.model : null,
          maxAdvice: Number.isFinite(advisor.maxAdvice) ? advisor.maxAdvice : 3,
          maxChars: Number.isFinite(advisor.maxChars) ? advisor.maxChars : 600,
          effort: typeof advisor.effort === "string" ? advisor.effort : "auto",
          temperature: advisor.temperature === null ? null : Number.isFinite(advisor.temperature) ? advisor.temperature : 0.3,
          wait: advisor.wait !== false,
          teacher: advisor.teacher && typeof advisor.teacher === "object" && !Array.isArray(advisor.teacher)
            ? {
                enabled: advisor.teacher.enabled === true,
                cooldownBlocks: Number.isFinite(advisor.teacher.cooldownBlocks) ? advisor.teacher.cooldownBlocks : 3,
                depthMin: Array.isArray(advisor.teacher.depthRange) ? Math.max(1, Math.min(5, Number(advisor.teacher.depthRange[0]) || 1)) : 1,
                depthMax: Array.isArray(advisor.teacher.depthRange) ? Math.max(1, Math.min(5, Number(advisor.teacher.depthRange[1]) || 5)) : 5,
                inputBudget: Number.isFinite(advisor.teacher.inputBudget) ? advisor.teacher.inputBudget : 12000,
                maxTokens: Number.isFinite(advisor.teacher.maxTokens) ? advisor.teacher.maxTokens : 1500,
              }
            : { enabled: false, cooldownBlocks: 3, depthMin: 1, depthMax: 5, inputBudget: 12000, maxTokens: 1500 },
        };
      })();
      const setAdvisor = (patch) => {
        const next = { ...advisorCfg, ...patch };
        set({
          guidance: { enabled: next.enabled, mode: next.mode },
          advisor: {
            route: { provider: next.provider, model: next.model },
            effort: next.effort,
            temperature: next.temperature,
            wait: next.wait,
            maxAdvice: next.maxAdvice,
            maxChars: next.maxChars,
            teacher: {
              enabled: next.teacher?.enabled === true,
              cooldownBlocks: next.teacher?.cooldownBlocks ?? 3,
              depthRange: [next.teacher?.depthMin ?? 1, next.teacher?.depthMax ?? 5],
              inputBudget: next.teacher?.inputBudget ?? 12000,
              maxTokens: next.teacher?.maxTokens ?? 1500,
            },
          },
        });
      };
      const routeLabel = (target) => (target && target.provider && target.model ? target.provider + "/" + target.model : "跟随会话");
      const strategyRouteOf = (id) => {
        const overrides = (cfg.strategies && cfg.strategies[id]) || {};
        return {
          provider: typeof overrides.provider === "string" ? overrides.provider : "",
          model: typeof overrides.model === "string" ? overrides.model : "",
        };
      };
      const setStrategyRoute = (id, patch) => {
        const next = { ...(cfg.strategies ?? {}) };
        const row = { ...(next[id] ?? {}) };
        for (const [key, value] of Object.entries(patch)) {
          if (value === "" || value === null) delete row[key];
          else row[key] = value;
        }
        next[id] = row;
        set({ strategies: next });
      };

      const advertised = reasoning && Array.isArray(reasoning.efforts) ? reasoning.efforts : null;
      const currentEffort = rewriteCfg.effort;
      const effortChoices = ["auto", ...(advertised ? advertised.map((entry) => entry.id) : [...EFFORT_IDS])];
      if (!effortChoices.includes(currentEffort)) effortChoices.push(currentEffort);

      // A route typed by hand may not be in the catalog; keep it selectable.
      const providerChoices = providers.map((entry) => entry.id);
      if (rewriteCfg.provider && !providerChoices.includes(rewriteCfg.provider)) providerChoices.push(rewriteCfg.provider);
      const modelChoices = models.map((entry) => entry.id);
      if (rewriteCfg.model && !modelChoices.includes(rewriteCfg.model)) modelChoices.push(rewriteCfg.model);

      // 两级值域：先选档位，选到"自定义预设"后才选具体用哪份文本。
      // 拿不到 host 的 choices（旧 host / 请求失败）时退回内置档位，功能不残。
      const tiers = Array.isArray(choices.tiers) && choices.tiers.length
        ? choices.tiers
        : [...catalog, ...STRATEGY_FALLBACK.filter((entry) => !catalog.some((known) => known.id === entry.id))]
            .map((entry) => ({ value: entry.id, label: entry.label, isCustom: entry.family === "custom" }));
      const presetChoices = Array.isArray(choices.presets) ? choices.presets : [];
      // 注入候选：空（不注入）+ 内置预设 + 贴入文本 + 目录文件。
      const injectChoices = Array.isArray(choices.injects) ? choices.injects : [{ value: "", label: "不注入", kind: "none" }];
      // 旧配置（strategy=custom）迁移后的注入值：让选择器能回显当前生效的注入。
      const legacyInjectOf = (strategy, defaultPreset) => {
        const raw = typeof strategy === "string" ? strategy : "";
        if (raw === "custom" || raw.startsWith("custom:")) {
          if (raw.startsWith("custom:")) return raw;
          return typeof defaultPreset === "string" && defaultPreset ? defaultPreset : (String(cfg.customText ?? "").trim() ? "inline" : "");
        }
        if (raw.startsWith("builtin:")) return raw;
        return "";
      };
      const activeInject = String(cfg.inject ?? "").trim() || legacyInjectOf(cfg.strategy, cfg.defaultPreset);
      // 旧 `custom` 档已迁移成「完整档 + 用户注入」：下拉里不再有它，值也归一成 iot-full。
      const tierOf = (value) => {
        const raw = typeof value === "string" ? value : "";
        if (raw === "custom" || raw.startsWith("custom:") || raw.startsWith("builtin:")) return "iot-full";
        return raw;
      };
      const targetOf = (value) => (typeof value === "string" && value.startsWith("custom:") ? value : "custom");
      const firstCustom = () => (presetChoices[0] ? presetChoices[0].value : "custom");
      // 切到"自定义预设"时给一个真实可用值：原来的 target 不是具体文件就用第一个文件。
      const compose = (tier, target) => composeTier(tier, target, firstCustom);
      // 会话覆盖可能是对象 `{ strategy, guidance }`：直接把它当 child 渲染会触发
      // React #31（Objects are not valid as a React child）。这里先取出档位字符串。
      const sessionStrategyOf = (value) => {
        if (value && typeof value === "object" && !Array.isArray(value)) {
          return typeof value.strategy === "string" ? value.strategy : "";
        }
        return typeof value === "string" ? value : "";
      };
      const choiceLabel = (value) => {
        const strategy = sessionStrategyOf(value);
        if (!strategy) return "跟随默认";
        const tier = tiers.find((entry) => entry.value === tierOf(strategy));
        if (tierOf(strategy) !== "custom") return tier ? tier.label : strategy;
        const preset = presetChoices.find((entry) => entry.value === strategy);
        // 自定义档位不叫"第 4 项"：直接显示用户配置的名字（内置预设名 / 文件名 / 贴入的文本）。
        return preset ? preset.label : strategy === "custom" ? "贴入的文本" : strategy;
      };

      const activeId = cfg.strategy ?? "iot-full";
      // custom 档位下"用哪份规格"有两条来源：strategy 里的 `custom:名字`（旧写法），
      // 或全局 defaultPreset（内置预设走这条，不占 strategy 名字空间）。
      // `custom:文件名` 已经把"用哪份规格"写在档位里了，优先它；
      // 只有 `strategy === "custom"`（没带名字）时才回落到 defaultPreset。
      // 反过来会让卡片显示 builtin，实际却走 file1。
      const activeTarget = targetOf(activeId);
      const activePresetValue = tierOf(activeId) === "custom"
        ? (activeTarget !== "custom"
            ? activeTarget
            : (typeof cfg.defaultPreset === "string" && cfg.defaultPreset ? cfg.defaultPreset : activeTarget))
        : "";
      const isCustomValue = (value) => typeof value === "string" && (value === "custom" || value.startsWith("custom:"));
      const active = catalog.find((entry) => entry.id === activeId)
        ?? STRATEGY_FALLBACK.find((entry) => entry.id === activeId)
        ?? (isCustomValue(activeId) ? { id: "custom", family: "custom", label: "用户配置", summary: "用你自己写的文本当改写指令。" } : null);
      const activeLimits = isCustomValue(activeId)
        ? { ...((catalog.find((entry) => entry.id === "custom") ?? STRATEGY_FALLBACK.find((entry) => entry.id === "custom") ?? {}).defaults ?? {}), ...((cfg.strategies ?? {}).custom ?? {}) }
        : limitsFor(catalog, cfg, activeId);
      const limitFields = [
        ...LIMIT_FIELDS,
        ...(active && active.family === "items" ? ITEM_LIMIT_FIELDS : []),
      ];
      const setLimit = (key, value) => {
        // 自定义预设的限制按 `strategies.custom` 存（一个"值域"共用一份限制）。
        const key0 = isCustomValue(activeId) ? "custom" : activeId;
        const next = { ...(cfg.strategies ?? {}) };
        const forThis = { ...(next[key0] ?? {}) };
        if (value === null) delete forThis[key];
        else forThis[key] = value;
        if (Object.keys(forThis).length === 0) delete next[key0];
        else next[key0] = forThis;
        set({ strategies: next });
      };

      const outcome = (entry) => {
        if (entry.event === "block-replaced") return "ok";
        if (entry.event === "block-failed") return "fail";
        if (entry.event === "thinking-failed") return "fail";
        if (entry.event === "thinking-degenerate" && entry.action === "fallback") return "fail";
        return null;
      };
      const judged = events.map((entry, index) => ({ entry, index, kind: outcome(entry) })).filter((entry) => entry.kind !== null);
      const lastJudged = judged.length ? judged[judged.length - 1] : null;
      const lastFailure = lastJudged && lastJudged.kind === "fail" ? lastJudged.entry : null;
      const showFailure = lastFailure !== null;
      const recent = events.slice(-6).reverse();

      return h("div", { style: box },
        h("div", { style: { display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between" } },
          h("div", { style: { minWidth: 0 } },
            h("div", { style: { fontSize: 15, fontWeight: 600, marginBottom: 4 } }, "IoT 思考改写"),
            h("div", { style: hint },
              "thinking 块结束时按选定档位改写它并原地替换。三档决策模式压成带标签的节点（必须更短）；",
              "用户配置按你的文本改写；off 原样放行。不插入「上下文已压缩」，也不产生 checkpoint。")
          ),
          h("label", { style: { display: "flex", alignItems: "center", gap: 8, cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" } },
            h("input", { type: "checkbox", checked: cfg.enabled === true, onChange: (e) => set({ enabled: e.target.checked }) }),
            h("span", { style: { fontSize: 13, fontWeight: 600 } }, cfg.enabled === true ? "已启用" : "已关闭")
          )
        ),

        showFailure ? h("div", { style: { marginTop: 10, padding: "8px 10px", borderRadius: 8, background: "rgba(192,57,43,0.08)", border: "1px solid rgba(192,57,43,0.25)", fontSize: 12, color: "#c0392b" } },
          "最近的判定是失败：" + String(lastFailure.message || lastFailure.event),
          h("div", { style: { ...hint, color: "inherit", marginTop: 4 } }, "改写失败会按设计保留原文，界面上只表现为「没效果」。修好路由或档位后再点「一键测试」。")
        ) : null,

        // ── 压缩强度 + 用户注入（两个正交维度，分开选） ──────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "压缩强度"),
            h("span", { style: hint }, "没被 preset / 会话覆盖时用这一档")
          ),
          h("div", { style: { display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" } },
            h("select", {
              value: tierOf(activeId),
              style: { ...input, flex: "0 0 260px", padding: "6px 8px" },
              onChange: (e) => set({ strategy: e.target.value }),
            },
              ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
            ),
            h("span", { style: { ...hint, minWidth: 0, flex: "1 1 100%" } }, active ? active.summary : "")
          ),
          // 用户注入：叠加层，追加在内置提示词后面，只能补充标签规则。
          h("div", { style: { display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" } },
            h("span", { style: { ...hint, flex: "0 0 84px" } }, "用户注入"),
            h("select", {
              value: activeInject,
              title: "追加在内置提示词后面的一层规则：选「不注入」就不加；内置预设只读，目录文件与贴入文本可编辑。",
              style: { ...input, flex: "0 0 320px", padding: "6px 8px" },
              onChange: (e) => set({ inject: e.target.value }),
            },
              ...injectChoices.map((entry) => h("option", { key: entry.value || "none", value: entry.value }, entry.label))
            ),
            h("span", { style: hint }, activeInject ? "已注入" : "不注入")
          ),
          h("div", { style: { ...hint, marginTop: 4 } },
            "注入是**叠加**：内置三档的标签规则不会被替掉，只会在它后面追加这段要求。想整段替换提示词就用下面「用户配置」里的预设（旧行为，等价于把注入指向那份文本）。"),
          activeInject && injectChoices.length <= 1
            ? h("div", { style: { ...hint, marginTop: 4, color: "#b76e00" } },
                choices.presetDirSet ? "目录里还没有预设文件 —— 在「用户配置」里新建一个，或直接用贴入的文本。" : "还没有配置预设目录 —— 只能先用贴入的文本。")
            : null,
          h("div", { style: limitsGrid },
            ...limitFields.map((field) => h("label", { key: field.key, style: limitCell },
              h("span", { style: fieldName }, field.title),
              h("input", {
                type: "number",
                min: field.min ?? 0,
                step: field.step ?? 1,
                value: activeLimits[field.key] ?? "",
                style: { ...input, width: "100%", boxSizing: "border-box" },
                onChange: (e) => setLimit(field.key, e.target.value === "" ? null : Number(e.target.value)),
              }),
              h("span", { style: { ...hint, fontSize: 11 } }, field.desc)
            ))
          )
        ),

        // ── 指导层（外部评审） ──────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "建议层（旁观者评审）"),
            h("span", { style: hint }, advisorCfg.enabled ? "已启用" : "默认关闭")
          ),
          h("div", { style: hint },
            "压缩之后，让另一个（通常更强的）模型看一遍推理骨架并给出下一步建议。它只读几百字的骨架，不读完整上下文 —— 比把整段对话喂给强模型便宜得多。"),
          h("div", { style: { display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" } },
            h("label", { style: { display: "flex", alignItems: "center", gap: 6, ...hint, cursor: "pointer" } },
              h("input", {
                type: "checkbox",
                checked: advisorCfg.enabled,
                onChange: (e) => setAdvisor({ enabled: e.target.checked }),
              }),
              "启用旁观者建议"
            ),
            h("select", {
              value: advisorCfg.mode,
              disabled: !advisorCfg.enabled,
              style: { ...input, flex: "0 0 240px", padding: "6px 8px" },
              onChange: (e) => setAdvisor({ mode: e.target.value === "builtin" ? "builtin" : "external" }),
            },
              h("option", { value: "builtin" }, "内置建议（压缩器自己输出 [旁观者建议]）"),
              h("option", { value: "external" }, "外部建议（并发调用强模型）")
            ),
            advisorCfg.mode === "external"
              ? h("select", {
                  value: advisorCfg.provider ?? "",
                  disabled: !advisorCfg.enabled,
                  title: "指导模型走哪条路由；留空 = 跟随压缩器路由",
                  style: { ...input, flex: "0 0 170px", padding: "6px 8px" },
                  onChange: (e) => setAdvisor({ provider: e.target.value || null, model: null }),
                },
                  h("option", { value: "" }, "跟随压缩器路由"),
                  ...providers.map((entry) => h("option", { key: entry.id, value: entry.id }, entry.id))
                )
              : null,
            advisorCfg.mode === "external" && advisorCfg.provider
              ? h("select", {
                  value: advisorCfg.model ?? "",
                  disabled: !advisorCfg.enabled,
                  title: "指导模型（从该 provider 的模型列表里选）",
                  style: { ...input, flex: "0 0 260px", padding: "6px 8px" },
                  onChange: (e) => setAdvisor({ model: e.target.value || null }),
                },
                  h("option", { value: "" }, "选择模型"),
                  ...advisorModels.map((entry) => h("option", { key: entry.id, value: entry.id }, entry.name ?? entry.id))
                )
              : null,
            advisorCfg.mode === "external" && advisorCfg.provider && advisorCfg.model
              ? h("select", {
                  value: advisorCfg.effort ?? "auto",
                  disabled: !advisorCfg.enabled,
                  title: "指导模型的思考档位；auto = 先试 off，被拒再探最低档，再不行不发该字段",
                  style: { ...input, flex: "0 0 130px", padding: "6px 8px" },
                  onChange: (e) => setAdvisor({ effort: e.target.value }),
                },
                  ...(() => {
                    const advertised = advisorReasoning && Array.isArray(advisorReasoning.efforts) ? advisorReasoning.efforts : null;
                    const list = ["auto", ...(advertised ? advertised.map((entry) => entry.id) : [...EFFORT_IDS])];
                    if (advisorCfg.effort && !list.includes(advisorCfg.effort)) list.push(advisorCfg.effort);
                    return list.map((id) => h("option", { key: id, value: id }, EFFORT_LABELS[id] ?? id));
                  })()
                )
              : null,
            advisorCfg.mode === "external"
              ? h("span", { style: hint }, advisorCfg.provider && advisorCfg.model
                  ? "建议请求发往：" + routeLabel({ provider: advisorCfg.provider, model: advisorCfg.model })
                    + (advisorReasoning && Array.isArray(advisorReasoning.efforts) && advisorReasoning.efforts.length
                      ? "（该模型公开档位：" + advisorReasoning.efforts.map((entry) => entry.id).join("/") + "）"
                      : "（未拿到该模型的档位目录，auto 会按实际拒绝情况降级）")
                  : "建议请求与压缩器同路由（同模型时收益有限）")
              : h("span", { style: hint }, "内置建议不额外调用模型，成本与普通压缩相同")
          ),
          advisorCfg.enabled
            ? h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 10 } },
                h("label", { style: limitCell },
                  h("span", { style: fieldName }, "建议条数上限"),
                  h("input", {
                    type: "number",
                    min: 1,
                    max: 3,
                    value: advisorCfg.maxAdvice,
                    style: { ...input, width: "100%", boxSizing: "border-box" },
                    onChange: (e) => setAdvisor({ maxAdvice: Number(e.target.value) || 3 }),
                  }),
                  h("span", { style: { ...hint, fontSize: 11 } }, "宁少勿多：泛泛而谈的建议不如不给。")
                ),
                h("label", { style: limitCell },
                  h("span", { style: fieldName }, "建议 temperature"),
                  h("input", {
                    type: "number",
                    min: 0,
                    max: 2,
                    step: 0.1,
                    placeholder: "0.3（留空 = 不发）",
                    value: advisorCfg.temperature ?? "",
                    style: { ...input, width: "100%", boxSizing: "border-box" },
                    onChange: (e) => setAdvisor({ temperature: e.target.value === "" ? null : Number(e.target.value) }),
                  }),
                  h("span", { style: { ...hint, fontSize: 11 } }, "有的模型只接受默认 temperature（如 GPT-6 Astra 只认 1）；留空就不发该字段。")
                ),
                h("label", { style: { ...limitCell, gridColumn: "1 / -1" } },
                  h("span", { style: { display: "flex", alignItems: "center", gap: 6, cursor: "pointer" } },
                    h("input", {
                      type: "checkbox",
                      checked: advisorCfg.wait !== false,
                      onChange: (e) => setAdvisor({ wait: e.target.checked }),
                    }),
                    h("span", { style: fieldName }, "等待建议返回")
                  ),
                  h("span", { style: { ...hint, fontSize: 11 } },
                    advisorCfg.wait !== false
                      ? "压缩会等建议一起返回再继续。建议质量更好，但慢模型会拖住这一块。"
                      : "可选建议：压缩一完成就继续推理，建议没赶上就跳过（记 advisor-skipped:not-ready）。慢模型不拖流程。")
                ),
                h("label", { style: limitCell },
                  h("span", { style: fieldName }, "建议总字数上限"),
                  h("input", {
                    type: "number",
                    min: 80,
                    max: 600,
                    step: 50,
                    value: advisorCfg.maxChars,
                    style: { ...input, width: "100%", boxSizing: "border-box" },
                    onChange: (e) => setAdvisor({ maxChars: Number(e.target.value) || 600 }),
                  }),
                  h("span", { style: { ...hint, fontSize: 11 } }, "指导会追加到压缩结果后面，占用下一轮上下文。")
                )
              )
            : null,
          // 教师咨询：时机由模型判断，插件只提供骨架、上下文与冷却。
          h("div", { style: { marginTop: 12, paddingTop: 10, borderTop: "1px dashed var(--dsw-alias-border-l2, #e6e9ee)" } },
            h("div", { style: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" } },
              h("label", { style: { display: "flex", alignItems: "center", gap: 6, ...hint, cursor: "pointer" } },
                h("input", {
                  type: "checkbox",
                  checked: advisorCfg.teacher.enabled,
                  onChange: (e) => setAdvisor({ teacher: { ...advisorCfg.teacher, enabled: e.target.checked } }),
                }),
                h("span", { style: { fontSize: 12.5, fontWeight: 600, color: "inherit" } }, "教师增强（模型主动咨询）")
              ),
              h("span", { style: hint }, advisorCfg.teacher.enabled ? "已启用 · 用法与 CD 状态进入运行时上下文" : "默认关闭")
            ),
            h("div", { style: { ...hint, marginTop: 4 } },
              "开启后模型可以调用 iot_consult_teacher：插件按它要求的轮次抽取会话正文，交给更强的模型做一次限时咨询。",
              "何时调用由模型判断（方案定型前、执行不可逆动作前、宣布完成前、或确实卡住），插件只负责固定提示词骨架、上下文格式化与冷却。"),
            advisorCfg.teacher.enabled
              ? h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginTop: 8 } },
                  h("label", { style: limitCell },
                    h("span", { style: fieldName }, "冷却块数"),
                    h("input", {
                      type: "number",
                      min: 0,
                      max: 20,
                      value: advisorCfg.teacher.cooldownBlocks,
                      style: { ...input, width: "100%", boxSizing: "border-box" },
                      onChange: (e) => setAdvisor({ teacher: { ...advisorCfg.teacher, cooldownBlocks: Math.max(0, Number(e.target.value) || 0) } }),
                    }),
                    h("span", { style: { ...hint, fontSize: 11 } }, "调用后要过多少个思考块才能再咨询。只显示冷却，不显示总额。")
                  ),
                  h("label", { style: limitCell },
                    h("span", { style: fieldName }, "深度范围（轮）"),
                    h("div", { style: { display: "flex", gap: 6 } },
                      h("input", {
                        type: "number", min: 1, max: 5,
                        value: advisorCfg.teacher.depthMin,
                        style: { ...input, width: "50%", boxSizing: "border-box" },
                        onChange: (e) => setAdvisor({ teacher: { ...advisorCfg.teacher, depthMin: Math.max(1, Math.min(5, Number(e.target.value) || 1)) } }),
                      }),
                      h("input", {
                        type: "number", min: 1, max: 5,
                        value: advisorCfg.teacher.depthMax,
                        style: { ...input, width: "50%", boxSizing: "border-box" },
                        onChange: (e) => setAdvisor({ teacher: { ...advisorCfg.teacher, depthMax: Math.max(1, Math.min(5, Number(e.target.value) || 5)) } }),
                      })
                    ),
                    h("span", { style: { ...hint, fontSize: 11 } }, "模型可以要求的上下文轮次上限；越大越贵。")
                  ),
                  h("label", { style: limitCell },
                    h("span", { style: fieldName }, "咨询输入上限（字符）"),
                    h("input", {
                      type: "number", min: 1000, max: 40000, step: 1000,
                      value: advisorCfg.teacher.inputBudget,
                      style: { ...input, width: "100%", boxSizing: "border-box" },
                      onChange: (e) => setAdvisor({ teacher: { ...advisorCfg.teacher, inputBudget: Math.max(1000, Number(e.target.value) || 12000) } }),
                    }),
                    h("span", { style: { ...hint, fontSize: 11 } }, "插件按这个上限裁剪提示词，超了从旧轮次开始砍。")
                  )
                )
              : null
          ),
          h("div", { style: { ...hint, marginTop: 8 } },
            "指导不参与压缩档位：压缩是减 token，指导是加信息。两者可以独立开关。")
        ),

        // ── 按 agent preset 映射 ────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "按 agent preset 映射"),
            dirty ? h("span", { style: { ...hint, color: "#b76e00" } }, "有未保存的修改") : null
          ),
          h("div", { style: { ...hint, marginTop: 4 } },
            "preset 是条件，档位是结论 —— 同一会话只会命中一行。优先级：会话自己的选择（顶部下拉 / `/iot`）> 下面这些行 > 默认档位。"),
          h("div", { style: mappingTable },
            h("div", { style: { ...mappingRow, ...mappingHeadRow } },
              h("span", { style: fieldName }, "当会话的 preset 是"),
              h("span", null),
              h("span", { style: fieldName }, "就改写为"),
              h("span", null)
            ),
            ...presetKeys.map((preset) => {
              const known = presetInfo && Array.isArray(presetInfo.presets)
                ? presetInfo.presets.find((entry) => entry.id === preset)
                : null;
              const orphan = presetInfo && presetInfo.available === true && !known;
              const openDetail = presetDetail && presetDetail.id === preset;
              const openPrompt = strategyDetail && strategyDetail.id === "strategy:" + preset;
              // 对象形态的 preset 要折回值里：`{ strategy:"custom", preset:"builtin:x" }`
              // 的第二级下拉必须显示 builtin:x，否则会显示成"贴入的文本"。
              const mapping = entryOf(preset);
              const value = mapping.strategy === "custom" && mapping.preset ? mapping.preset : mapping.strategy;
              return h("div", { key: preset, style: mappingRow },
                h("div", { style: { minWidth: 0 } },
                  h("div", { style: { ...monoLabel } }, preset),
                  known && known.name && known.name !== preset ? h("div", { style: hint }, known.name) : null,
                  orphan ? h("div", { style: { ...hint, color: "#b76e00" } }, "注册表里没有这个 preset") : null,
                  known && known.broken ? h("div", { style: { ...hint, color: "#c0392b" } }, "加载有问题：" + String(known.broken)) : null
                ),
                h("span", { style: { ...hint, textAlign: "center" } }, "→"),
                h("div", { style: { minWidth: 0, display: "flex", gap: 6, alignItems: "center" } },
                  h("select", {
                    value: tierOf(value),
                    style: { ...input, flex: "1 1 0", minWidth: 0, padding: "6px 8px" },
                    onChange: (e) => setPreset(preset, compose(e.target.value, targetOf(value))),
                  },
                    ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                  ),
                  tierOf(value) === "custom"
                    ? h("select", {
                        value: targetOf(value),
                        style: { ...input, flex: "1 1 0", minWidth: 0, padding: "6px 8px" },
                        onChange: (e) => setPreset(preset, e.target.value),
                      },
                        ...(presetChoices.length
                          ? presetChoices.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                          : [h("option", { key: "custom", value: "custom" }, "贴入的文本")])
                      )
                    : null
                ),
                h("div", { style: { display: "flex", gap: 4, justifyContent: "flex-end" } },
                  h("button", { style: btnSmall, onClick: () => showStrategyPrompt({ preset, strategy: value }) }, openPrompt ? "收起" : "原文"),
                  h("button", { style: btnSmall, onClick: () => showPresetDetail(preset) }, openDetail ? "收起" : "组成"),
                  h("button", { style: { ...btnSmall, color: "#c0392b", borderColor: "rgba(192,57,43,0.35)" }, onClick: () => setPreset(preset, null) }, "删除")
                ),
                openDetail ? h("div", { style: { gridColumn: "1 / -1", ...panelTight } },
                  presetDetail.loading
                    ? h("div", { style: hint }, "读取组成…")
                    : presetDetail.missing
                      ? h("div", { style: hint }, "拿不到这个 preset 的组成（可能已不存在，或未激活）。")
                      : h("div", null,
                          h("div", { style: { ...hint, marginBottom: 4 } }, "这个 preset 由下列插件行组成（○ = 未启用）："),
                          ...(presetDetail.rows ?? []).map((row, index) => h("div", {
                            key: index,
                            style: { ...monoLabel, fontWeight: 400, color: row.enabled === false ? "#8a8f98" : "inherit" },
                          }, (row.enabled === false ? "○ " : "● ") + row.moduleName + (row.entryId ? "  (" + row.entryId + ")" : "") + (row.condition ? "  if " + row.condition : "")))
                        )
                ) : null,
                openPrompt ? h("div", { style: { gridColumn: "1 / -1", ...panelTight } },
                  strategyDetail.loading
                    ? h("div", { style: hint }, "渲染原文…")
                    : strategyDetail.error
                      ? h("div", { style: { ...hint, color: "#c0392b" } }, "读不到原文：" + String(strategyDetail.error))
                      : h("div", null,
                          h("div", { style: { ...hint, marginBottom: 4 } },
                            (strategyDetail.label ?? "") + "｜" + String(strategyDetail.family ?? "") + "｜替换规则 " + String(strategyDetail.accept ?? "")),
                          h("div", { style: { ...monoBlock, maxHeight: 240, overflow: "auto" } }, String(strategyDetail.prompt ?? ""))
                        )
                ) : null
              );
            }),
            h("div", { style: { ...mappingRow, borderTop: "1px solid var(--dsw-alias-border-l2, #eef0f3)" } },
              presetInfo && Array.isArray(presetInfo.presets) && presetInfo.presets.length > 0
                ? h("select", {
                    value: presetDraft.preset,
                    style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
                    onChange: (e) => setPresetDraft({ ...presetDraft, preset: e.target.value }),
                  },
                    h("option", { value: "" }, "（选一个 preset）"),
                    ...presetInfo.presets
                      .filter((entry) => !presetKeys.includes(entry.id))
                      .map((entry) => h("option", { key: entry.id, value: entry.id },
                        entry.id + (entry.isDefault ? "（默认）" : "")))
                  )
                : h("input", {
                    placeholder: presetInfo && presetInfo.available === false ? "手输 preset id（如 standard）" : "preset id",
                    value: presetDraft.preset,
                    style: { ...input, width: "100%", boxSizing: "border-box" },
                    onChange: (e) => setPresetDraft({ ...presetDraft, preset: e.target.value }),
                    onKeyDown: (e) => { if (e.key === "Enter") addPreset(); },
                  }),
              h("span", { style: { ...hint, textAlign: "center" } }, "→"),
              h("div", { style: { display: "flex", gap: 6, minWidth: 0 } },
                h("select", {
                  value: tierOf(presetDraft.strategy),
                  style: { ...input, flex: "1 1 0", minWidth: 0, padding: "6px 8px" },
                  onChange: (e) => setPresetDraft({ ...presetDraft, strategy: compose(e.target.value, targetOf(presetDraft.strategy)) }),
                },
                  ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                ),
                tierOf(presetDraft.strategy) === "custom"
                  ? h("select", {
                      value: targetOf(presetDraft.strategy),
                      style: { ...input, flex: "1 1 0", minWidth: 0, padding: "6px 8px" },
                      onChange: (e) => setPresetDraft({ ...presetDraft, strategy: e.target.value }),
                    },
                      ...(presetChoices.length
                        ? presetChoices.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                        : [h("option", { key: "custom", value: "custom" }, "贴入的文本")])
                    )
                  : null
              ),
              h("div", { style: { display: "flex", justifyContent: "flex-end" } },
                h("button", {
                  style: btnOutline,
                  onClick: () => {
                    if (!presetDraft.preset.trim()) {
                      setErr("先在左边选一个 agent preset 作为条件（宿主里没有可选的就先加一个 preset）。");
                      return;
                    }
                    setErr(null);
                    addPreset();
                  },
                }, "添加")
              )
            )
          ),
          presetInfo && Array.isArray(presetInfo.orphaned) && presetInfo.orphaned.length > 0
            ? h("div", { style: { ...hint, marginTop: 6, color: "#b76e00" } }, "另有 " + presetInfo.orphaned.length + " 条映射指向已不存在的 preset：" + presetInfo.orphaned.join("、"))
            : null,
          !presetDraft.preset.trim()
            ? h("div", { style: { ...hint, marginTop: 6, color: "#b76e00" } },
                presetInfo && Array.isArray(presetInfo.presets) && presetInfo.presets.length > 0
                  ? "先在左边选一个 preset 作为条件；所有 preset 都有映射时这里没有可选项。"
                  : presetInfo && presetInfo.available === false
                    ? "宿主没有 agent preset 注册表 —— 只能手输 preset id。"
                    : "宿主里没有可选的 preset，先去宿主添加一个 preset。")
            : null,
          h("div", { style: { ...hint, marginTop: 6 } },
            "「添加」只改本页内存，和其他设置一样要点「保存」。「原文」看真正发给模型的提示词，「组成」看这个 preset 由哪些插件行构成。")
        ),

        // ── 会话级覆盖 ──────────────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "会话级覆盖"),
            sessionKeys.length ? h("span", { style: hint }, sessionKeys.length + " 个会话") : null
          ),
          h("div", { style: { ...hint, marginTop: 4 } },
            "每个会话可以单独选一档。下面这个开关只控制**对话顶部那个下拉**要不要出现；关掉之后 /iot 命令照样能切。"),
          h("label", { style: { display: "flex", alignItems: "flex-start", gap: 8, marginTop: 8, cursor: "pointer" } },
            h("input", {
              type: "checkbox",
              checked: cfg.sessionSwitch === true,
              style: { marginTop: 2 },
              onChange: (e) => set({ sessionSwitch: e.target.checked }),
            }),
            h("div", null,
              h("div", { style: { fontSize: 13, color: "var(--dsw-alias-label-primary, #222)" } }, "在对话顶部显示档位下拉（按会话覆盖）"),
              h("div", { style: hint }, "默认关闭。多数人只需要全局默认 + 按 preset 映射；开着的话每个会话标题旁都会多一行「思考 [跟随 preset 默认]」。"))
          ),
          sessionKeys.length === 0
            ? h("div", { style: { ...hint, marginTop: 8 } }, "当前没有任何会话覆盖。")
            : h("div", { style: { ...mappingTable, marginTop: 8 } },
                ...sessionKeys.map((sid) => h("div", { key: sid, style: { ...mappingRow, gridTemplateColumns: "minmax(0,1fr) 130px 110px 110px 60px" } },
                  h("span", { style: { ...monoLabel, fontWeight: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, sid),
                  h("span", { style: { ...hint, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, choiceLabel(cfg.sessions[sid])),
                  h("select", {
                    value: (() => {
                      const entry = cfg.sessions[sid];
                      const g = entry && typeof entry === "object" ? entry.guidance : undefined;
                      if (g === null || g === undefined) return "follow";
                      if (g.enabled === false) return "off";
                      return g.mode === "builtin" ? "builtin" : "external";
                    })(),
                    style: { ...input, padding: "2px 4px", fontSize: 12 },
                    onChange: (e) => {
                      const key = e.target.value;
                      const guidance = key === "follow" ? null : key === "off" ? { enabled: false } : { enabled: true, mode: key };
                      const entry = cfg.sessions[sid];
                      const base = typeof entry === "object" ? { ...entry } : { strategy: entry };
                      base.guidance = guidance;
                      set({ sessions: { ...(cfg.sessions ?? {}), [sid]: base } });
                    },
                  },
                    h("option", { value: "follow" }, "建议跟随"),
                    h("option", { value: "off" }, "建议关闭"),
                    h("option", { value: "builtin" }, "内置建议"),
                    h("option", { value: "external" }, "外部建议")
                  ),
                  // 教师开关：会话级独立于全局；全局没开时这里勾上也能用。
                  h("select", {
                    value: (() => {
                      const entry = cfg.sessions[sid];
                      const t = entry && typeof entry === "object" ? entry.teacher : undefined;
                      if (t === null || t === undefined) return "follow";
                      return t.enabled === true ? "on" : "off";
                    })(),
                    style: { ...input, padding: "2px 4px", fontSize: 12 },
                    onChange: (e) => {
                      const key = e.target.value;
                      const teacher = key === "follow" ? null : { enabled: key === "on" };
                      const entry = cfg.sessions[sid];
                      const base = typeof entry === "object" ? { ...entry } : { strategy: entry };
                      base.teacher = teacher;
                      set({ sessions: { ...(cfg.sessions ?? {}), [sid]: base } });
                    },
                  },
                    h("option", { value: "follow" }, "教师跟随"),
                    h("option", { value: "on" }, "教师开启"),
                    h("option", { value: "off" }, "教师关闭")
                  ),
                  h("button", {
                    style: { ...btnSmall, justifySelf: "end" },
                    onClick: () => {
                      const next = { ...(cfg.sessions ?? {}) };
                      delete next[sid];
                      set({ sessions: next });
                    },
                  }, "清除")
                ))
              ),
          sessionKeys.length
            ? h("button", {
                style: { ...btn, marginTop: 8 },
                onClick: () => set({ sessions: {} }),
              }, "清空所有会话覆盖（" + sessionKeys.length + "）")
            : null
        ),

        // ── 内置预设（开箱即用） ────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "内置预设（开箱即用）"),
            h("span", { style: hint }, "默认文本可编辑 · 随插件发布")
          ),
          h("div", { style: { ...hint, marginTop: 4 } },
            "不想自己写规格时，直接点一个。它们只在「用户配置」档位下生效：点「用这个」会同时把默认档位切到用户配置。正文可以改——改过的正文存在配置里，随时能恢复默认。"),
          builtinPresets.length === 0
            ? h("div", { style: { ...hint, marginTop: 6 } }, "宿主没有下发内置清单（旧版本或 headless 组合）。")
            : h("div", { style: { border: "1px solid var(--dsw-alias-border-l2, #e6e9ee)", borderRadius: 8, overflow: "hidden", marginTop: 8 } },
                ...builtinPresets.map((preset, index) => h("div", {
                  key: preset.id,
                  style: {
                    display: "flex",
                    gap: 10,
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "8px 10px",
                    borderTop: index === 0 ? "none" : "1px solid var(--dsw-alias-border-l2, #eef0f3)",
                  },
                },
                  h("div", { style: { minWidth: 0 } },
                    h("div", { style: { ...monoLabel, fontWeight: 600 } }, preset.name),
                    h("div", { style: { ...hint, fontSize: 11, marginTop: 2 } }, preset.summary || "")
                  ),
                  h("div", { style: { display: "flex", gap: 6, flexShrink: 0 } },
                    h("button", {
                      style: btnSmall,
                      onClick: () => setBuiltinEditor({ id: preset.id, text: (cfg.builtinOverrides ?? {})[preset.id] ?? preset.text ?? "" }),
                    }, "编辑"),
                    h("button", {
                      style: { ...btnSmall, borderColor: "#2f6fed", color: "#2f6fed" },
                      onClick: () => {
                        set({ strategy: "custom", defaultPreset: preset.value });
                        setMsg("默认档位已设为「" + preset.name + "」（还要点保存）");
                      },
                    }, cfg.strategy === "custom" && cfg.defaultPreset === preset.value ? "当前默认" : "用这个")
                  )
                ))
              )
          ,
          builtinEditor.id
            ? h("div", { style: panel },
                h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 } },
                  h("div", { style: fieldName }, "编辑内置预设：" + builtinEditor.id),
                  h("div", { style: { display: "flex", gap: 6 } },
                    h("button", {
                      style: btnSmall,
                      onClick: () => {
                        const next = { ...(cfg.builtinOverrides ?? {}) };
                        delete next[builtinEditor.id];
                        set({ builtinOverrides: next });
                        setBuiltinEditor({ id: "", text: "" });
                        setMsg("已恢复默认正文（还要点保存）");
                      },
                    }, "恢复默认"),
                    h("button", { style: btnSmall, onClick: () => setBuiltinEditor({ id: "", text: "" }) }, "关闭")
                  )
                ),
                h("textarea", {
                  value: builtinEditor.text || ((cfg.builtinOverrides ?? {})[builtinEditor.id] ?? ""),
                  rows: 12,
                  placeholder: "留空 = 用插件自带的默认正文；写入内容后覆盖默认。",
                  style: textareaStyle,
                  onChange: (e) => {
                    const next = { ...(cfg.builtinOverrides ?? {}) };
                    const text = e.target.value;
                    if (text.trim()) next[builtinEditor.id] = text;
                    else delete next[builtinEditor.id];
                    set({ builtinOverrides: next });
                    setBuiltinEditor({ id: builtinEditor.id, text });
                  },
                }),
                h("div", { style: { ...hint, marginTop: 6 } }, "内置正文只是默认值：改过的内容存在配置里，保存后下一个请求生效。")
              )
            : null
        ),

        // ── 用户配置（自定义预设） ──────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "用户配置（自定义预设）"),
            h("span", { style: hint }, presetDirSet ? presetFiles.length + " 个文件" : "未配置目录")
          ),
          h("div", { style: { ...hint, marginTop: 4 } },
            "预设是一份文本：告诉改写器「把 thinking 改成什么样」。建好之后会出现在上面所有下拉里，也可以只给某个会话用。"),

          h("div", { style: panel },
            h("div", { style: fieldName }, "预设目录"),
            h("div", { style: { display: "flex", gap: 8, alignItems: "center" } },
              h("input", {
                value: cfg.presetDir ?? "",
                placeholder: "绝对路径（必须显式指定，没有默认值）",
                style: grow,
                onChange: (e) => set({ presetDir: e.target.value }),
              }),
              h("button", { style: btn, onClick: () => refreshPresetDir() }, "保存并刷新")
            ),
            h("div", { style: { ...hint, marginTop: 6 } }, presetDirSet
              ? "已配置。列表只按文件名取，不回显解析后的路径。"
              : "还没配置：文件类预设不可用，下面贴入的文本仍然可用。")
          ),

          h("div", { style: panel },
            h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 6 } },
              h("div", { style: fieldName }, "目录里的文件"),
              h("span", { style: hint }, presetDirSet ? "文件名带后缀（.md / .txt 都行）" : "—")
            ),
            presetFiles.length === 0
              ? h("div", { style: { ...hint, padding: "2px 0" } }, presetDirSet
                  ? "这个目录里还没有预设文件。用下面的「新建」写一个。"
                  : "配置目录后这里会列出 .md / .txt 文件。")
              : h("div", { style: { border: "1px solid var(--dsw-alias-border-l2, #e6e9ee)", borderRadius: 8, overflow: "hidden" } },
                  ...presetFiles.map((file, index) => h("div", {
                    key: file.name,
                    style: {
                      display: "flex",
                      gap: 10,
                      alignItems: "center",
                      justifyContent: "space-between",
                      padding: "7px 10px",
                      borderTop: index === 0 ? "none" : "1px solid var(--dsw-alias-border-l2, #eef0f3)",
                      background: editor.name === file.name ? "rgba(47,111,237,0.06)" : "transparent",
                    },
                  },
                    h("div", { style: { minWidth: 0, display: "flex", alignItems: "baseline", gap: 8 } },
                      h("span", { style: { ...monoLabel } }, file.name),
                      h("span", { style: { ...hint, fontSize: 11, whiteSpace: "nowrap" } }, humanBytes(file.bytes))
                    ),
                    h("div", { style: { display: "flex", gap: 6, flexShrink: 0 } },
                      h("button", { style: btnSmall, onClick: () => openPreset(file.name) }, editor.name === file.name ? "编辑中" : "查看"),
                      h("button", {
                        style: { ...btnSmall, borderColor: "#2f6fed", color: "#2f6fed" },
                        onClick: () => { set({ strategy: "custom:" + file.name }); setMsg("默认档位已设为 " + file.name + "（还要点保存）"); },
                      }, "设为默认")
                    )
                  ))
                )
          ),

          editor.name
            ? h("div", { style: panel },
                h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 } },
                  h("div", { style: fieldName }, "编辑 " + editor.name),
                  h("button", { style: btnSmall, onClick: () => setEditor({ name: "", text: "", note: null }) }, "关闭")
                ),
                h("textarea", {
                  value: editor.text,
                  rows: 10,
                  style: textareaStyle,
                  onChange: (e) => setEditor({ ...editor, text: e.target.value, note: null }),
                }),
                editor.note ? h("div", { style: { ...hint, marginTop: 6, color: "#b76e00" } }, String(editor.note)) : null,
                h("div", { style: { display: "flex", gap: 8, marginTop: 10 } },
                  h("button", { style: btnSolid, onClick: () => savePresetFile(editor.name, editor.text) }, "保存"),
                  h("button", {
                    style: btn,
                    onClick: () => savePresetFile(editor.name, editor.text).then((ok) => ok && setEditor({ name: "", text: "", note: null })),
                  }, "保存并关闭"),
                  h("button", {
                    style: { ...btn, marginLeft: "auto" },
                    onClick: () => { set({ strategy: "custom:" + editor.name }); setMsg("默认档位已设为 " + editor.name + "（还要点保存）"); },
                  }, "设为默认")
                )
              )
            : h("div", { style: panel },
                h("div", { style: fieldName }, "新建预设"),
                // 这里写的是"给改写器的指令"，不是 thinking 正文本身 —— 先说清楚，
                // 否则很容易被当成一个写草稿的框。
                h("div", { style: { ...hint, marginBottom: 6 } },
                  "写一段指令：告诉改写器怎么改这段 thinking（语气、长度、格式都行），草稿会单独递给它。"),
                h("textarea", {
                  value: cfg.customText ?? "",
                  rows: 7,
                  style: textareaStyle,
                  placeholder: "例如：作为可爱的 DeepSeek 鲸鲸，我要用可爱的语气简短总结接下来的内容，并且段落以「本鲸鲸觉得」开头。",
                  onChange: (e) => set({ customText: e.target.value }),
                }),
                h("div", { style: { display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" } },
                  h("input", {
                    value: saveName,
                    placeholder: "文件名（如 my-style.md，也可 .txt）",
                    style: { ...grow, flex: "1 1 220px" },
                    onChange: (e) => setSaveName(e.target.value),
                  }),
                  h("button", {
                    style: btnOutline,
                    onClick: async () => {
                      const text = cfg.customText ?? "";
                      if (!String(text).trim()) { setErr("内容为空，先写点什么"); return; }
                      if (await savePresetFile(saveName.trim(), text)) refreshPresetDir();
                    },
                  }, "另存为预设"),
                  h("button", {
                    style: btn,
                    onClick: () => { set({ strategy: "custom" }); setMsg("默认档位已设为「贴入的文本」（还要点保存）"); },
                  }, "用这段文本")
                )
              ),

          h("label", { style: { display: "flex", alignItems: "center", gap: 6, ...hint, marginTop: 10, cursor: "pointer" } },
            h("input", {
              type: "checkbox",
              checked: cfg.presetAccept === "shorter",
              onChange: (e) => set({ presetAccept: e.target.checked ? "shorter" : "different" }),
            }),
            "必须更短才替换（压缩型预设；不确定就别勾）"
          ),
          h("div", { style: { ...hint, marginTop: 6 } },
            "预设里写 `{{draft}}` 就是完整提示词（引擎不再追加包装）；不写则按指令体处理，引擎把草稿包进 `<thinking>` 再发出去。")
        ),

        // ── 压缩器路由（算力卸载） ────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionTitle }, "压缩器路由（算力卸载）"),
          h("div", { style: hint },
            "压缩调用走哪条路由。跟随会话 = 与主对话同模型；指定后，压缩算力落在你选的那个模型上（例如本地小模型或另一家供应商）。"),
          h("div", { style: { ...hint, marginTop: 6 } },
            sessionRoute && sessionRoute.provider && sessionRoute.model
              ? "会话当前路由：" + sessionRoute.provider + "/" + sessionRoute.model
              : "会话当前路由：未知（发一条消息后插件会记住最近一次请求路由）"),
          h("div", { style: { display: "grid", gridTemplateColumns: "220px minmax(0,1fr)", gap: 8, marginTop: 10, alignItems: "center" } },
            h("select", {
              value: rewriteCfg.provider ? "explicit" : "follow",
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              onChange: (e) => setRewrite(e.target.value === "follow"
                ? { provider: null, model: null }
                : { provider: rewriteCfg.provider || providerChoices[0] || null, model: rewriteCfg.model || modelChoices[0] || null }),
            },
              h("option", { value: "follow" }, "跟随会话路由"),
              h("option", { value: "explicit" }, "指定路由（卸载）")
            ),
            h("div", { style: hint }, rewriteCfg.provider
              ? "压缩请求发往：" + routeLabel(rewriteCfg)
                + (sessionRoute && sessionRoute.provider === rewriteCfg.provider && sessionRoute.model === rewriteCfg.model ? "（与会话相同，等于没卸载）" : "")
              : "压缩请求与主对话同路由")
          ),
          rewriteCfg.provider ? h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 8 } },
            h("select", {
              value: rewriteCfg.provider ?? "",
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              onChange: (e) => setRewrite({ provider: e.target.value || null, model: null }),
            },
              h("option", { value: "" }, "从模型目录选 provider…"),
              ...providerChoices.map((id) => {
                const entry = providers.find((candidate) => candidate.id === id);
                return h("option", { key: id, value: id }, entry && entry.name && entry.name !== id ? entry.name + "（" + id + "）" : id);
              })
            ),
            h("select", {
              value: rewriteCfg.model ?? "",
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              disabled: !rewriteCfg.provider,
              onChange: (e) => setRewrite({ model: e.target.value || null }),
            },
              h("option", { value: "" }, rewriteCfg.provider ? "model（卸载路由必须明确）" : "先选 provider"),
              ...modelChoices.map((id) => {
                const entry = models.find((candidate) => candidate.id === id);
                return h("option", { key: id, value: id }, entry && entry.name && entry.name !== id ? entry.name + "（" + id + "）" : id);
              })
            )
          ) : null,
          h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,1fr) 220px", gap: 10, alignItems: "center", marginTop: 10 } },
            h("div", { style: hint }, advertised
              ? "该路由公开的档位：" + advertised.map((entry) => (entry.name && entry.name !== entry.id ? entry.id + "（" + entry.name + "）" : entry.id)).join(" / ")
              : "自动 = 先发 off；宿主拒绝时不发这个字段，由供应商取默认值（改写器会自己思考，慢一些但一定能跑）。"),
            h("select", {
              value: currentEffort,
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              onChange: (e) => setRewrite({ effort: e.target.value }),
            },
              ...effortChoices.map((id) => h("option", { key: id, value: id }, EFFORT_LABELS[id] ?? id))
            )
          ),
          h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,1fr) 260px", gap: 10, alignItems: "center", marginTop: 10 } },
            h("div", { style: hint }, "卸载路由失败（网络、鉴权、模型不存在、供应商 5xx）时怎么办。回退会多花一次调用，但这一块不会白白丢掉。"),
            h("select", {
              value: rewriteCfg.fallback,
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              onChange: (e) => setRewrite({ fallback: e.target.value }),
            },
              h("option", { value: "keep" }, "保留原文，不重试"),
              h("option", { value: "session" }, "回退到会话路由再试一次")
            )
          ),
          h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 10 } },
            h("label", { style: limitCell },
              h("span", { style: fieldName }, "单次改写超时（毫秒）"),
              h("input", {
                type: "number",
                min: 1000,
                step: 1000,
                placeholder: "默认 120000",
                value: cfg.rewriteTimeoutMs ?? "",
                style: { ...input, width: "100%", boxSizing: "border-box" },
                onChange: (e) => set({ rewriteTimeoutMs: e.target.value === "" ? null : Number(e.target.value) }),
              }),
              h("span", { style: { ...hint, fontSize: 11 } }, "模型一直吐字不结束时，到点放弃并保留原文，不再挂住会话。")
            ),
            h("label", { style: limitCell },
              h("span", { style: fieldName }, "单块调用预算（次）"),
              h("input", {
                type: "number",
                min: 1,
                step: 1,
                placeholder: "默认 4",
                value: cfg.rewriteBudget ?? "",
                style: { ...input, width: "100%", boxSizing: "border-box" },
                onChange: (e) => set({ rewriteBudget: e.target.value === "" ? null : Number(e.target.value) }),
              }),
              h("span", { style: { ...hint, fontSize: 11 } }, "effort 降级 + 重试 + 回退叠起来会让一块打很多次；这里给整块一个硬上限。")
            )
          ),
          h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 10 } },
            h("label", { style: { display: "flex", alignItems: "center", gap: 6, ...hint, cursor: "pointer" } },
              h("input", {
                type: "checkbox",
                checked: cfg.noThink !== false,
                onChange: (e) => set({ noThink: e.target.checked }),
              }),
              "禁止压缩器在输出里「思考」（出现 think 标签即判失败）"
            ),
            h("label", { style: limitCell },
              h("span", { style: fieldName }, "输出字数上限（custom 档位）"),
              h("input", {
                type: "number",
                min: 0,
                step: 100,
                placeholder: "0 = 不限制",
                value: cfg.maxOut ?? "",
                style: { ...input, width: "100%", boxSizing: "border-box" },
                onChange: (e) => set({ maxOut: e.target.value === "" ? null : Number(e.target.value) }),
              }),
              h("span", { style: { ...hint, fontSize: 11 } }, "超过就按「写多了」重试一次，而不是硬截断。")
            )
          ),
          h("div", { style: { ...hint, marginTop: 14 } }, "按档位覆盖（可选）：让某一档单独走另一条路由；provider/model 留空就跟随上面的设置。"),
          h("div", { style: mappingTable },
            h("div", { style: { ...mappingRow, ...mappingHeadRow, gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr) minmax(0,1fr)" } },
              h("div", { style: hint }, "档位"),
              h("div", { style: hint }, "provider"),
              h("div", { style: hint }, "model")
            ),
            ...catalog.filter((entry) => entry.family !== "off").map((entry) => {
              const route = strategyRouteOf(entry.id);
              return h("div", { key: entry.id, style: { ...mappingRow, gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr) minmax(0,1fr)" } },
                h("div", { style: monoLabel }, entry.id),
                h("input", { placeholder: "跟随", value: route.provider, style: { ...input, width: "100%", boxSizing: "border-box" }, onChange: (e) => setStrategyRoute(entry.id, { provider: e.target.value }) }),
                h("input", { placeholder: "跟随", value: route.model, style: { ...input, width: "100%", boxSizing: "border-box" }, onChange: (e) => setStrategyRoute(entry.id, { model: e.target.value }) })
              );
            })
          ),
          catalogNote ? h("div", { style: { ...hint, marginTop: 6 } }, String(catalogNote)) : null
        ),

        h("div", { style: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--dsw-alias-border-l2, #eee)" } },
          h("button", { style: { ...btnSolid, padding: "7px 16px", background: busy ? "#9db8f0" : "#2f6fed", cursor: busy ? "wait" : "pointer" }, disabled: busy, onClick: save }, busy ? "保存中…" : dirty ? "保存 *" : "保存"),
          h("button", { style: { ...btn, padding: "7px 14px" }, disabled: busy, onClick: () => load(true) }, "刷新"),
          h("select", {
            value: testStrategy ?? "",
            style: { ...input, width: 190, padding: "6px 8px" },
            onChange: (e) => setTestStrategy(e.target.value || null),
          },
            h("option", { value: "" }, "测试默认档位"),
            ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, "测试：" + entry.label)),
            ...presetChoices.map((entry) => h("option", { key: entry.value, value: entry.value }, "测试：" + entry.label))
          ),
          h("select", {
            value: testGuidance,
            style: { ...input, width: 150, padding: "6px 8px" },
            onChange: (e) => setTestGuidance(e.target.value),
          },
            h("option", { value: "" }, "按当前指导配置"),
            h("option", { value: "builtin" }, "测内置建议"),
            h("option", { value: "external" }, "测外部建议")
          ),
          h("button", { style: { ...btnOutline, padding: "7px 14px" }, disabled: testBusy || busy, onClick: runTest }, testBusy ? "测试中…" : "一键测试"),
          dirty ? h("span", { style: { ...hint, color: "#b76e00" } }, "尚未保存") : null
        ),
        h("div", { style: { ...hint, marginTop: 6 } },
          "一键测试会用选中档位真实改写一次（约 10–30 s，按一次请求计费），按与真实链路相同的规则判定；关着插件或输入太短时它会直接说「真实链路会跳过」，不报绿。"),
        h("div", { style: { ...hint, marginTop: 4 } }, "原始 thinking 归档在 $DSH_HOME/plugin-data/iot-thinking/originals/"),
        err ? h("div", { style: { color: "#c0392b", marginTop: 10, fontSize: 12, whiteSpace: "pre-wrap" } }, String(err)) : null,
        msg ? h("div", { style: { color: "#188a4b", marginTop: 10, fontSize: 12 } }, String(msg)) : null,
        test ? h("div", { style: { marginTop: 10, fontSize: 12 } },
          h("div", { style: { color: test.ok ? "#188a4b" : "#b76e00", fontWeight: 600 } },
            test.ok
              ? "测试通过（" + (test.strategy ?? "") + "）：" + test.charsIn + " → " + test.charsOut + " 字（" + test.ms + "ms" + (test.effort ? "，档位 " + test.effort : "，未发档位") + (test.effortFallback ? "，已自动降级" : "") + "）"
              : test.error
                ? "测试失败：" + String(test.error)
                : test.reason === "disabled"
                  ? "未测试：插件当前是关闭状态，真实链路上任何块都不会被改写"
                  : test.reason === "below-min-chars"
                    ? "未测试：这段输入比最小改写字符数（" + test.minChars + "）短，真实链路上会被直接跳过"
                    : test.reason === "degenerate-input"
                      ? "未测试：这段草稿已被判为退化重复，真实链路上会跳过（不花模型调用）"
                      : test.reason === "compressor-failed"
                        ? "测试未通过：改写器没有给出结果（原文已保留，详情见最近事件）"
                        : test.reason === "empty-output"
                          ? "测试未通过：改写器返回了空内容，真实链路上这一块会保留原文"
                          : "测试未通过：改写结果没有变短（" + test.charsIn + " → " + test.charsOut + " 字），真实链路上这一块会保留原文"
          ),
          test.provider && test.model
            ? h("div", { style: { ...hint, marginTop: 4 } },
                "测试路由：" + test.provider + "/" + test.model
                  + (test.routeSource ? "（来源 " + test.routeSource + "）" : "")
                  + (test.offloaded ? " · 已卸载到该路由" : ""))
            : null,
          test.guidance
            ? h("div", { style: { ...hint, marginTop: 4 } },
                "本次建议：" + (test.guidance.enabled ? (test.guidance.mode === "builtin" ? "内置（压缩器输出 [旁观者建议]）" : "外部（并发调用强模型）") : "关闭"))
            : null,
          test.advice
            ? h("div", { style: { marginTop: 8 } },
                h("div", { style: { fontSize: 12, fontWeight: 600, color: "#2f6fed" } }, "旁观者建议输出："),
                h("div", { style: { ...monoBlock, marginTop: 4, maxHeight: 220, overflow: "auto", background: "rgba(47,111,237,0.08)", padding: 8, borderRadius: 6 } }, String(test.advice))
              )
            : null,
          test.text ? h("div", { style: { ...monoBlock, marginTop: 6, maxHeight: 220, overflow: "auto", background: "rgba(127,127,127,0.08)", padding: 8, borderRadius: 6 } }, String(test.text)) : null,
          Array.isArray(test.trace) && test.trace.length
            ? h("div", { style: { marginTop: 10 } },
                h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 } },
                  h("div", { style: { fontSize: 12, fontWeight: 600 } }, "报文日志（" + test.trace.length + " 次调用）"),
                  test.guidance
                    ? h("span", { style: hint }, "指导：" + (test.guidance.enabled ? (test.guidance.mode === "builtin" ? "内置" : "外部") : "关"))
                    : null
                ),
                ...test.trace.map((entry, index) => h("details", {
                  key: index,
                  style: { marginTop: 6, border: "1px solid var(--dsw-alias-border-l2, #e6e9ee)", borderRadius: 6, padding: "6px 8px" },
                },
                  h("summary", { style: { cursor: "pointer", fontSize: 12 } },
                    "#" + (index + 1) + " " + String(entry.stage ?? "") + " · " + String(entry.provider ?? "") + "/" + String(entry.model ?? "")
                      + (entry.finish ? " · finish=" + entry.finish : "")
                      + (entry.error ? " · 失败：" + String(entry.error) : "")
                      + (entry.usage ? " · tokens " + (entry.usage.completionTokens ?? "?") + (entry.usage.reasoningTokens !== null && entry.usage.reasoningTokens !== undefined ? "（思考 " + entry.usage.reasoningTokens + "）" : "") : "")
                  ),
                  h("div", { style: { ...hint, marginTop: 6 } }, "提示词（实际发出）："),
                  h("pre", { style: { ...monoBlock, maxHeight: 260, overflow: "auto", background: "rgba(127,127,127,0.08)", padding: 8, borderRadius: 6, whiteSpace: "pre-wrap", fontSize: 11 } }, String(entry.prompt ?? "")),
                  h("div", { style: { ...hint, marginTop: 6 } }, "原始输出："),
                  h("pre", { style: { ...monoBlock, maxHeight: 200, overflow: "auto", background: "rgba(127,127,127,0.08)", padding: 8, borderRadius: 6, whiteSpace: "pre-wrap", fontSize: 11 } }, String(entry.raw ?? "（空）"))
                ))
              )
            : null
        ) : null,
        recent.length ? h("div", { style: section },
          h("div", { style: { ...hint, marginBottom: 6 } }, "最近事件"),
          ...recent.map((event, index) => h("div", { key: index, style: { ...monoLabel, fontWeight: 400, color: "var(--dsw-alias-label-tertiary, #777)", padding: "1px 0" } },
            (event.event || "?")
              + (event.strategy ? "  " + event.strategy : "")
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

    /**
     * 会话头部的档位下拉。
     *
     * 写的是 `sessions[sessionId]`，与 `/iot <档位>` 和设置卡的「会话级覆盖」同一张表。
     * 新会话还没有 sessionId 时也照样渲染：先记住选择，等会话出现再落盘。
     */
    function IotStrategySeat({ sessionId }) {
      const { useState, useEffect, useRef } = react;
      const [state, setState] = useState(null);
      const [pending, setPending] = useState(null);
      const [busy, setBusy] = useState(false);
      const alive = useRef(true);

      useEffect(() => {
        alive.current = true;
        return () => {
          alive.current = false;
        };
      }, []);

      const load = async () => {
        // 没有 sessionId 也照样问一次：返回值里的 sessionSwitch 决定这一行要不要出现。
        const qs = sessionId ? "?sessionId=" + encodeURIComponent(sessionId) : "";
        try {
          const resp = await fetch(SESSION + qs, { headers: GUARD });
          const data = await resp.json().catch(() => null);
          if (resp.ok && data && alive.current) setState(data);
        } catch {
          /* 顶栏不报错 */
        }
      };

      useEffect(() => { load(); }, [sessionId]);

      // 一次覆盖 = 档位 + （仅 custom 档位）这份规格。写成对象，别把 prompt
      // 单独塞进另一个字段 —— 两者必须一起落盘，否则"切档成功、prompt 丢了"。
      const put = async (choice) => {
        // 会话覆盖只认两种形态：别的档位写字符串；custom 档位写
        // `{ strategy: "custom", preset: <值> }`。preset 值（builtin:/custom:名字）
        // 绝不塞进 strategy 字段 —— 那会让服务端把它当档位名解析。
        const raw = choice?.strategy || null;
        const isCustom = raw !== null && String(raw).startsWith("custom");
        const strategy = isCustom ? "custom" : raw;
        const preset = isCustom
          ? (choice?.preset || (String(raw).startsWith("custom:") ? String(raw).slice("custom:".length) : null))
          : null;
        setBusy(true);
        try {
          const resp = await fetch(SESSION, {
            method: "PUT",
            headers: { ...GUARD, "content-type": "application/json" },
            // guidance: null = 跟随全局；对象 = 本会话覆盖。不传 = 保持原值。
            body: JSON.stringify({
              sessionId,
              // 只改教师时不要把 strategy 写成 null —— 那会清掉档位覆盖。
              ...(choice?.strategy === undefined ? {} : { strategy, preset }),
              ...(choice?.guidance === undefined ? {} : { guidance: choice.guidance }),
              ...(choice?.teacher === undefined ? {} : { teacher: choice.teacher }),
            }),
          });
          const data = await resp.json().catch(() => null);
          if (resp.ok && data && alive.current) {
            setState((current) => ({
              ...(current ?? {}),
              route: data.route,
              override: data.override ?? null,
              enabled: data.enabled,
            }));
          }
        } catch {
          /* 失败不改 UI，下次打开会重新读 */
        } finally {
          if (alive.current) setBusy(false);
        }
      };

      /** 切档：有会话就立刻写；还没有会话就先记下来，等第一条消息落盘。 */
      const choose = (strategy, preset = null, guidance, extra = {}) => {
        const raw = strategy || null;
        // 把 `custom:名字` 拆成 `{ strategy: "custom", preset: "custom:名字" }`，
        // 让"档位"与"规格"两件事在落盘时就分开。
        const isCustom = raw !== null && String(raw).startsWith("custom");
        const choice = {
          // undefined = 不改档位；null = 显式清除覆盖。不能在归一化时混为一谈。
          ...(strategy === undefined ? {} : {
            strategy: isCustom ? "custom" : raw,
            preset: isCustom
              ? (preset || (String(raw).startsWith("custom:") ? String(raw).slice("custom:".length) : null))
              : null,
          }),
          // 指导选择与档位一起记住：新会话第一条消息落盘时不能丢。
          ...(guidance === undefined ? {} : { guidance }),
          ...extra,
        };
        if (!sessionId) {
          // 新会话连续选择档位/教师时合并独立字段，不丢掉之前尚未落盘的选择。
          setPending((previous) => ({ ...(previous ?? {}), ...choice }));
          return;
        }
        put(choice);
      };

      // 新会话：sessionId 一出现就把之前记下的选择写进去
      useEffect(() => {
        if (!sessionId || pending === null) return;
        const choice = pending;
        setPending(null);
        put(choice);
      }, [sessionId]);

      // 默认关：卡片里没开「会话级档位」时，顶栏不出现这一行（/iot 命令仍然可用）。
      if (!state || state.sessionSwitch !== true) return null;
      const override = state?.override
        ? typeof state.override === "object" ? String(state.override.strategy ?? "") : String(state.override)
        : "";
      const overridePreset = state?.override && typeof state.override === "object" && typeof state.override.preset === "string"
        ? state.override.preset
        : "";
      const pendingStrategy = pending && typeof pending === "object" ? String(pending.strategy ?? "") : String(pending ?? "");
      const pendingPreset = pending && typeof pending === "object" ? String(pending.preset ?? "") : "";
      const current = override || pendingStrategy || "";
      const currentPreset = overridePreset || pendingPreset || "";
      // 指导覆盖：null/undefined = 跟随全局；对象 = 本会话覆盖。
      const overrideGuidance = state?.override && typeof state.override === "object" ? state.override.guidance : undefined;
      const pendingGuidance = pending && typeof pending === "object" ? pending.guidance : undefined;
      const guidanceValue = pendingGuidance !== undefined ? pendingGuidance : overrideGuidance;
      const guidanceKey = guidanceValue === null || guidanceValue === undefined
        ? "follow"
        : guidanceValue.enabled === false
          ? "off"
          : guidanceValue.mode === "builtin"
            ? "builtin"
            : "external";
      const globalGuidance = state?.guidance?.global ?? { enabled: false, mode: "external" };
      const overrideTeacher = state?.override && typeof state.override === "object" ? state.override.teacher : undefined;
      const pendingTeacher = pending && typeof pending === "object" ? pending.teacher : undefined;
      const teacherValue = pendingTeacher !== undefined ? pendingTeacher : overrideTeacher;
      const teacherKey = teacherValue === null || teacherValue === undefined
        ? "follow"
        : teacherValue.enabled === true
          ? "on"
          : "off";
      const route = state?.route ? state.route.id : "";
      // 「跟随 preset 默认」显示的是没有本会话覆盖时的结果（presetDefault），
      // 不是 route —— route 已经是"含覆盖的生效档位"。
      const presetDefault = state?.presetDefault?.id ?? (state?.route?.source === "session" ? null : route);
      const tiers = Array.isArray(state?.choices?.tiers) && state.choices.tiers.length
        ? state.choices.tiers
        : STRATEGY_FALLBACK.map((entry) => ({ value: entry.id, label: entry.label, isCustom: entry.family === "custom" }));
      const presets = Array.isArray(state?.choices?.presets) ? state.choices.presets : [];
      // 旧 `custom` 档已迁移成「完整档 + 用户注入」：归一成 iot-full，下拉里没有 custom。
      const tierOf = (value) => {
        const raw = typeof value === "string" ? value : "";
        if (raw === "custom" || raw.startsWith("custom:") || raw.startsWith("builtin:")) return "iot-full";
        return raw;
      };
      const targetOf = (value) => (typeof value === "string" && value.startsWith("custom:") ? value : "custom");
      const selectStyle = {
        padding: "2px 4px",
        borderRadius: 6,
        border: "1px solid var(--dsw-alias-border-l2, #ddd)",
        background: "transparent",
        color: "inherit",
        fontSize: 12,
      };

      return h("label", {
        title: [
          sessionId ? "IoT 档位（本会话）：选一档就写这个会话自己的覆盖，下一个请求生效" : "IoT 档位（新会话）：选好后第一条消息就按它改写",
          "选「跟随 preset 默认」＝ 不覆盖本会话，按这个会话的 agent preset 映射走；该 preset 没配映射时用全局默认档位。",
        ].join("\n"),
        style: { display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--dsw-alias-label-secondary, #555)" },
      },
        h("span", { style: { opacity: 0.75 } }, "思考"),
        h("select", {
          value: current ? tierOf(current) : "",
          disabled: busy,
          style: selectStyle,
          onChange: (e) => {
            const tier = e.target.value;
            if (!tier) { choose(null); return; }
            // 档位下拉只写压缩强度；用户注入是另一维，不在这里改。
            choose(tier);
          },
        },
          h("option", { value: "" }, presetDefault ? "跟随 preset 默认（" + presetDefault + "）" : "跟随 preset 默认"),
          ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
        ),
        h("span", { style: { opacity: 0.75 } }, "指导"),
        h("select", {
          value: guidanceKey,
          disabled: busy,
          title: "本会话的建议开关与模式：跟随全局 / 关闭 / 内置（压缩器自己输出 [旁观者建议]）/ 外部（并发调用强模型）",
          style: selectStyle,
          onChange: (e) => {
            const key = e.target.value;
            const guidance = key === "follow"
              ? null
              : key === "off"
                ? { enabled: false }
                : { enabled: true, mode: key };
            const base = current || (state?.route?.id ?? "");
            choose(base, currentPreset || null, guidance);
          },
        },
          h("option", { value: "follow" }, "跟随全局（" + (globalGuidance.enabled ? (globalGuidance.mode === "builtin" ? "内置" : "外部") : "关") + "）"),
          h("option", { value: "off" }, "关闭"),
          h("option", { value: "builtin" }, "内置建议"),
          h("option", { value: "external" }, "外部建议")
        ),
        h("span", { style: { opacity: 0.75 } }, "教师"),
        h("select", {
          value: teacherKey,
          disabled: busy,
          title: "本会话的教师开关：跟随全局 / 开启（注入 CD 状态、允许模型调用 iot_consult_teacher）/ 关闭",
          style: selectStyle,
          onChange: (e) => {
            const key = e.target.value;
            const teacher = key === "follow" ? null : { enabled: key === "on" };
            // 只改教师：不传 strategy，档位覆盖保持原样。
            choose(undefined, undefined, undefined, { teacher });
          },
        },
          h("option", { value: "follow" }, "跟随全局（" + (state?.teacher?.global ? "开" : "关") + "）"),
          h("option", { value: "on" }, "开启"),
          h("option", { value: "off" }, "关闭")
        ),
      );
    }

    function apply(ctx) {
      const slots = ctx.get("slots");
      if (slots === undefined) return;
      ctx.effect(() => slots.inject("settings.plugins.tab", () => slots.register({
        name: "settings.plugins.tab",
        id: "iot-thinking",
        order: 90,
        label: () => "IoT 思考改写",
        inject: () => ({}),
      }, withErrorBoundary(IotThinkingCard, "IoT 设置卡"))), "iot-thinking: settings tab");

      // 顶栏下拉与设置卡是同一条链的两端：卡管默认，下拉管这一个会话。
      // 槽位不在时（非 web 组合）静默跳过。
      ctx.effect(() => {
        try {
          return slots.inject("conversation.session.header.actions", () => slots.register({
            name: "conversation.session.header.actions",
            id: "iot-strategy",
            order: -5,
            inject: () => ({}),
          }, IotStrategySeat));
        } catch {
          return undefined;
        }
      }, "iot-thinking: session strategy seat");
    }

    exports.apply = apply;
    exports.inject = inject;
    module.exports.IotThinkingCard = IotThinkingCard;
    module.exports.IotStrategySeat = IotStrategySeat;
    module.exports.changedFields = changedFields;
    module.exports.presetOptions = presetOptions;
    module.exports.composeTier = composeTier;
    return module.exports;
  },
});
