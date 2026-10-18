// iot-compaction 插件入口 —— 形态同 host 的 compaction-basic：
// default export 是引擎类（Service 子类），挂载即 provide `ctx.compaction`。
//
// 挂载行（**必须挂进 compaction 组，不能挂 host 平面**）：
//   - insert:
//       - id: iot-compaction
//         name: ./plugins/iot-compaction/index.mjs
//         config: { maxTokens: 1200, retainRatio: 0.12, everyStep: true }
//
// 为什么不能挂 host：preset 会话的 compaction 是 entry-local realm
// （`isolate: {compaction: true}`），host 层的后端对它**永不触发**。
// 见 harness-evolve/knowledge/harness-debug.md 的「上下文压缩(一)」。
import IotCompactionEngine from "./lib/engine.mjs";

export default IotCompactionEngine;
export { IotCompactionEngine };
