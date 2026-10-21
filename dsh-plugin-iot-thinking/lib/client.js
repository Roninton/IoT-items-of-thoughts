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
      provider: null,
      model: null,
      reasoningEffort: "auto",
      archive: true,
    };

    // Mirrors lib/strategies.mjs. Duplicated because this bundle is plain JS
    // loaded by the host module loader; it cannot import host code. The host
    // always sends the real catalog, so this is only the pre-load placeholder
    // (and the degraded view when /status is unreachable).
    const STRATEGY_FALLBACK = [
      { id: "iot-compact", label: "决策 · 极简（3 标签）", family: "items", summary: "压得最狠，短任务用。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 5, maxNeutral: 0 } },
      { id: "iot-balanced", label: "决策 · 均衡（5 标签）", family: "items", summary: "保留计划与决定。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 6, maxNeutral: 0 } },
      { id: "iot-full", label: "决策 · 完整（8 标签）", family: "items", summary: "8 标签全量，质量优先。", defaults: { minChars: 400, maxChars: 24000, maxTokens: 2000, maxNodes: 8, maxNeutral: 2 } },
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
    ];
    const ITEM_LIMIT_FIELDS = [
      { key: "maxNodes", title: "最多节点数", desc: "超过后按优先级保留：决定 > 反对 > 权衡 > 风险 > 待定 > 计划 > 支持 > 中性。", min: 1 },
      { key: "maxNeutral", title: "最多 [中性] 节点", desc: "多余的 [中性] 会合并成一条；0 = 完全不要中性节点。", min: 0 },
    ];

    const h = react.createElement;
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
      const [reasoning, setReasoning] = useState(null);
      const [catalogNote, setCatalogNote] = useState(null);
      const [testBusy, setTestBusy] = useState(false);
      const [test, setTest] = useState(null);
      const [testStrategy, setTestStrategy] = useState(null);
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
      /** 自定义预设（第 4 项）的目录与文件列表。 */
      const [presetFiles, setPresetFiles] = useState([]);
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
          if (Array.isArray(data.strategies)) setCatalog(data.strategies);
          if (data.choices && typeof data.choices === "object" && !Array.isArray(data.choices)) setChoices(data.choices);
          if (Array.isArray(data.presetFiles)) setPresetFiles(data.presetFiles);
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
          const body = testStrategy ? JSON.stringify({ strategy: testStrategy }) : "{}";
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
        try {
          const qs = new URLSearchParams({ id: strategy });
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
        if (typeof raw === "string") return { strategy: raw };
        if (raw && typeof raw === "object") return { strategy: String(raw.strategy ?? "") };
        return { strategy: "" };
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

      const advertised = reasoning && Array.isArray(reasoning.efforts) ? reasoning.efforts : null;
      const currentEffort = cfg.reasoningEffort ?? "auto";
      const effortChoices = ["auto", ...(advertised ? advertised.map((entry) => entry.id) : [...EFFORT_IDS])];
      if (!effortChoices.includes(currentEffort)) effortChoices.push(currentEffort);

      // A route typed by hand may not be in the catalog; keep it selectable.
      const providerChoices = providers.map((entry) => entry.id);
      if (cfg.provider && !providerChoices.includes(cfg.provider)) providerChoices.push(cfg.provider);
      const modelChoices = models.map((entry) => entry.id);
      if (cfg.model && !modelChoices.includes(cfg.model)) modelChoices.push(cfg.model);

      // 两级值域：先选档位，选到"自定义预设"后才选具体用哪份文本。
      // 拿不到 host 的 choices（旧 host / 请求失败）时退回内置档位，功能不残。
      const tiers = Array.isArray(choices.tiers) && choices.tiers.length
        ? choices.tiers
        : [...catalog, ...STRATEGY_FALLBACK.filter((entry) => !catalog.some((known) => known.id === entry.id))]
            .map((entry) => ({ value: entry.id, label: entry.label, isCustom: entry.family === "custom" }));
      const presetChoices = Array.isArray(choices.presets) ? choices.presets : [];
      const tierOf = (value) => (typeof value === "string" && value.startsWith("custom") ? "custom" : value);
      const targetOf = (value) => (typeof value === "string" && value.startsWith("custom:") ? value : "custom");
      const firstCustom = () => (presetChoices[0] ? presetChoices[0].value : "custom");
      // 切到"自定义预设"时给一个真实可用值：原来的 target 不是具体文件就用第一个文件。
      const compose = (tier, target) => (tier === "custom" ? (target && target !== "custom" ? target : firstCustom()) : tier);
      const choiceLabel = (value) => {
        const tier = tiers.find((entry) => entry.value === tierOf(value));
        if (tierOf(value) !== "custom") return tier ? tier.label : value;
        const preset = presetChoices.find((entry) => entry.value === value);
        return "自定义预设 · " + (preset ? preset.label : value === "custom" ? "贴入的文本" : value);
      };

      const activeId = cfg.strategy ?? "iot-full";
      const isCustomValue = (value) => typeof value === "string" && (value === "custom" || value.startsWith("custom:"));
      const active = catalog.find((entry) => entry.id === activeId)
        ?? STRATEGY_FALLBACK.find((entry) => entry.id === activeId)
        ?? (isCustomValue(activeId) ? { id: "custom", family: "custom", label: "自定义预设", summary: "用你自己写的文本当改写指令。" } : null);
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
              "自定义预设按你的文本改写；off 原样放行。不插入「上下文已压缩」，也不产生 checkpoint。")
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

        // ── 默认档位 ────────────────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "默认档位"),
            h("span", { style: hint }, "没被 preset / 会话覆盖时用这一档")
          ),
          h("div", { style: { display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" } },
            h("select", {
              value: tierOf(activeId),
              style: { ...input, flex: "0 0 260px", padding: "6px 8px" },
              onChange: (e) => set({ strategy: compose(e.target.value, targetOf(activeId)) }),
            },
              ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
            ),
            tierOf(activeId) === "custom"
              ? h("select", {
                  value: targetOf(activeId),
                  style: { ...input, flex: "0 0 240px", padding: "6px 8px" },
                  onChange: (e) => set({ strategy: e.target.value }),
                },
                  ...(presetChoices.length
                    ? presetChoices.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                    : [h("option", { key: "custom", value: "custom" }, "贴入的文本（未建任何预设文件）")])
                )
              : null,
            h("span", { style: { ...hint, minWidth: 0, flex: "1 1 100%" } }, active ? active.summary : "")
          ),
          tierOf(activeId) === "custom" && presetChoices.length === 0
            ? h("div", { style: { ...hint, marginTop: 4, color: "#b76e00" } },
                choices.presetDirSet ? "目录里还没有预设文件 —— 在第 4 项「新建预设」建一个，或直接用贴入的文本。" : "还没有配置预设目录 —— 只能先用贴入的文本（第 4 项）。")
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
              const value = entryOf(preset).strategy;
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
                ...sessionKeys.map((sid) => h("div", { key: sid, style: { ...mappingRow, gridTemplateColumns: "minmax(0,1fr) 150px 60px" } },
                  h("span", { style: { ...monoLabel, fontWeight: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, sid),
                  h("span", { style: { ...hint, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, choiceLabel(cfg.sessions[sid])),
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

        // ── 第 4 项：自定义预设 ─────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionHead },
            h("div", { style: sectionTitle }, "自定义预设（第 4 项）"),
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

        // ── 路由与推理档位 ──────────────────────────────────────────────────
        h("div", { style: section },
          h("div", { style: sectionTitle }, "改写器路由与推理档位"),
          h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,1fr) 220px", gap: 10, alignItems: "center", marginTop: 8 } },
            h("div", { style: hint }, advertised
              ? "该路由公开的档位：" + advertised.map((entry) => (entry.name && entry.name !== entry.id ? entry.id + "（" + entry.name + "）" : entry.id)).join(" / ")
              : "自动 = 先发 off；宿主拒绝时不发这个字段，由供应商取默认值（改写器会自己思考，慢一些但一定能跑）。"),
            h("select", {
              value: currentEffort,
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
              onChange: (e) => set({ reasoningEffort: e.target.value }),
            },
              ...effortChoices.map((id) => h("option", { key: id, value: id }, EFFORT_LABELS[id] ?? id))
            )
          ),
          h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 10 } },
            h("input", { placeholder: "provider（留空跟随当前路由）", value: cfg.provider ?? "", style: { ...input, width: "100%", boxSizing: "border-box" }, onChange: (e) => set({ provider: e.target.value || null }) }),
            h("input", { placeholder: "model（留空跟随当前路由）", value: cfg.model ?? "", style: { ...input, width: "100%", boxSizing: "border-box" }, onChange: (e) => set({ model: e.target.value || null }) })
          ),
          h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 8 } },
            h("select", {
              value: cfg.provider ?? "",
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
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
              style: { ...input, width: "100%", boxSizing: "border-box", padding: "6px 8px" },
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
          test.text ? h("div", { style: { ...monoBlock, marginTop: 6, maxHeight: 220, overflow: "auto", background: "rgba(127,127,127,0.08)", padding: 8, borderRadius: 6 } }, String(test.text)) : null
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

      const put = async (value) => {
        setBusy(true);
        try {
          const resp = await fetch(SESSION, {
            method: "PUT",
            headers: { ...GUARD, "content-type": "application/json" },
            body: JSON.stringify({ sessionId, strategy: value || null }),
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
      const choose = (value) => {
        if (!sessionId) {
          setPending(value || null);
          return;
        }
        put(value);
      };

      // 新会话：sessionId 一出现就把之前记下的选择写进去
      useEffect(() => {
        if (!sessionId || pending === null) return;
        const value = pending;
        setPending(null);
        put(value);
      }, [sessionId]);

      // 默认关：卡片里没开「会话级档位」时，顶栏不出现这一行（/iot 命令仍然可用）。
      if (!state || state.sessionSwitch !== true) return null;
      const override = state?.override
        ? typeof state.override === "object" ? String(state.override.strategy ?? "") : String(state.override)
        : "";
      const current = override || pending || "";
      const route = state?.route ? state.route.id : "";
      // 「跟随 preset 默认」显示的是没有本会话覆盖时的结果（presetDefault），
      // 不是 route —— route 已经是"含覆盖的生效档位"。
      const presetDefault = state?.presetDefault?.id ?? (state?.route?.source === "session" ? null : route);
      const tiers = Array.isArray(state?.choices?.tiers) && state.choices.tiers.length
        ? state.choices.tiers
        : STRATEGY_FALLBACK.map((entry) => ({ value: entry.id, label: entry.label, isCustom: entry.family === "custom" }));
      const presets = Array.isArray(state?.choices?.presets) ? state.choices.presets : [];
      const tierOf = (value) => (typeof value === "string" && value.startsWith("custom") ? "custom" : value ?? "");
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
            choose(tier === "custom" ? (current ? targetOf(current) : presets[0]?.value ?? "custom") : tier);
          },
        },
          h("option", { value: "" }, presetDefault ? "跟随 preset 默认（" + presetDefault + "）" : "跟随 preset 默认"),
          ...tiers.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
        ),
        current && tierOf(current) === "custom"
          ? h("select", {
              value: targetOf(current),
              disabled: busy,
              style: { ...selectStyle, maxWidth: 160 },
              onChange: (e) => choose(e.target.value),
            },
              ...(presets.length
                ? presets.map((entry) => h("option", { key: entry.value, value: entry.value }, entry.label))
                : [h("option", { key: "custom", value: "custom" }, "贴入的文本")])
            )
          : null
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
      }, IotThinkingCard)), "iot-thinking: settings tab");

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
    return module.exports;
  },
});
