// lib/config-check.mjs — 宿主配置项的本地校验（纯逻辑，零依赖，可独立单测）
//
// 为什么要有这一层：宿主 `BasicCompactionEngine` 构造函数内部就会
// `resolveConfig` → `assertRatio`，非法值在那里抛出来的错误只说
// "must be a number in (0, 1]"，看不出是哪个插件、哪个配置项；
// 而构造期抛错会让 **整个 plugin tree 加载失败**，dsh web 直接起不来。
//
// 真机事故（2026-10-17）：thresholdRatio 被写成 0（注释还写着"0=不考虑阈值"），
// 宿主拒绝 0，dsh web 启动即退出。DSH 里**没有**"用 0 关闭阈值"这种写法。

/** thresholdRatio 的默认值：合法极小正数，语义 ≈ "每步都值得检查一次"。 */
export const MIN_THRESHOLD_RATIO = 1e-6;

/**
 * 归一化并校验 thresholdRatio。
 *
 * @param raw - 来自插件行 config 的原值；`undefined`/`null` 取默认。
 * @returns (0, 1] 内的有限数字。
 * @throws 当值不在 (0, 1] 内、不是有限数字时，抛带配置项名的明确错误。
 */
export function normalizeThresholdRatio(raw) {
  if (raw === undefined || raw === null) return MIN_THRESHOLD_RATIO;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0 || value > 1) {
    throw new Error(
      `iot-compaction: thresholdRatio 必须是 (0, 1] 区间内的数字，收到 ${JSON.stringify(raw)}。` +
        `要表达"不按压力阈值、由 thinking 闸门决定"，请**省略**它（默认 ${MIN_THRESHOLD_RATIO}），` +
        `不要传 0 —— 0 会被宿主 assertRatio 拒绝，并让 dsh web 启动失败。`
    );
  }
  return value;
}

/**
 * 宿主还有第二条不变量：**retainRatio 必须小于 thresholdRatio**
 * （`validateRatioRetention`，源码注释写着 "Reject a capacity-independent retention
 * conflict at plugin load"）。
 *
 * 这条约束等于宣布了一件事：**"极小阈值 = 每步都压" 与 "保留尾部 12%" 不可共存**。
 * 真机第二次崩溃就是这么来的：
 *     BasicCompactionConfig: retainRatio (0.12) must be less than the resolved thresholdRatio (1e-6)
 * 语义上也说得通：保留区若比压力阈值还大，就意味着"还没到该压的点，就已经豁免了一大块"。
 *
 * 所以：要么调小阈值**同时**调小 retainRatio，要么就别动阈值、交给宿主默认。
 *
 * @param retainRatio - 解析后的保留比例（未设置时传 undefined）。
 * @param thresholdRatio - 解析后的压力阈值；undefined 表示交给宿主默认。
 * @throws 当两者都显式给出且 retainRatio >= thresholdRatio。
 */
export function assertRetentionBelowThreshold(retainRatio, thresholdRatio) {
  if (thresholdRatio === undefined) return;
  if (retainRatio !== undefined && retainRatio >= thresholdRatio) {
    throw new Error(
      `iot-compaction: retainRatio (${retainRatio}) 必须小于 thresholdRatio (${thresholdRatio})。` +
        `宿主的语义是"保留区必须落在压力阈值之内"；想更频繁压缩请**同时**调小两者。`
    );
  }
}
