// lib/surface.mjs — 从 0.2.x 的 surface 里取消息、选一个可压的平衡段
//
// 0.2.x 的 `session.surface.nodes` 是 **seq 数字数组**（不是消息对象）。
// 消息要用 `session.deriveEventMessage(session.eventAt(seq))` 投影出来。
// 0.1.x 的旧形状（节点自带 message/content）仍做兼容，避免升级期两套代码。
//
// `selectEveryStepRange` 复刻 dsh-compaction-basic 的 selectCompactableRange，
// 但保留尾部为 0：everyStep 要压的正是**刚结束的那一步**。
// 工具配对平衡函数由调用方注入（宿主从 @deepseek-ai/dsh-compaction 导出），
// 这样本模块保持零依赖、可被 selftest 直接覆盖。

/** surface 的 seq 列表；结构不认识时返回空数组。 */
export function surfaceSeqs(session) {
  const nodes = session?.surface?.nodes;
  return Array.isArray(nodes) ? nodes : [];
}

/** 取一个 surface seq 对应的消息；取不到返回 undefined。 */
export function eventMessage(session, seq) {
  if (!session) return undefined;
  let event;
  try {
    event = session.eventAt?.(seq);
  } catch {
    return undefined;
  }
  if (!event) return undefined;
  try {
    if (typeof session.deriveEventMessage === "function") {
      const message = session.deriveEventMessage(event);
      if (message) return message;
    }
  } catch {
    /* 投影失败则退回事件自带 message */
  }
  return event.message && typeof event.message === "object" ? event.message : undefined;
}

/** 按 seq 列表投影消息（保持顺序）。 */
export function messagesForSeqs(session, seqs) {
  if (!Array.isArray(seqs)) return [];
  const out = [];
  for (const seq of seqs) {
    if (typeof seq !== "number") continue;
    const message = eventMessage(session, seq);
    if (message) out.push(message);
  }
  return out;
}

/** 当前 surface 的全部消息（兼容 0.1.x 的对象节点）。 */
export function surfaceMessages(session) {
  const nodes = session?.surface?.nodes;
  if (!Array.isArray(nodes)) return [];
  const out = [];
  for (const node of nodes) {
    if (typeof node === "number") {
      const message = eventMessage(session, node);
      if (message) out.push(message);
      continue;
    }
    if (!node || typeof node !== "object") continue;
    if (node.message && typeof node.message === "object") out.push(node.message);
    else if (Array.isArray(node.content)) out.push(node);
  }
  return out;
}

/**
 * 选一个工具配对平衡、且覆盖到当前尾部的最老段。
 *
 * @param session - 提供 surface 与事件日志的会话
 * @param measurement - `ctx.tokenMeter.measure(session)` 的结果
 * @param pairing - `{ balancedBefore, balancedAfter }`（宿主导出）
 * @returns `{ startIdx, endIdx, start, end, seqs }` 或 null
 */
export function selectEveryStepRange(session, measurement, pairing) {
  const priced = measurement?.nodes;
  const seqs = surfaceSeqs(session);
  if (!Array.isArray(priced) || priced.length === 0) return null;
  if (seqs.length !== priced.length) return null;
  for (let index = 0; index < seqs.length; index += 1) {
    if (seqs[index] !== priced[index]?.seq) return null;
  }

  const head = session?.eventAt?.(seqs[0]);
  let startIdx = head?.type === "system/message" ? 1 : 0;
  if (startIdx >= seqs.length) return null;

  let endIdx = seqs.length - 1;
  while (endIdx >= startIdx) {
    try {
      if (pairing.balancedAfter(session, seqs[endIdx])) break;
    } catch {
      return null;
    }
    endIdx -= 1;
  }

  while (startIdx <= endIdx) {
    try {
      if (pairing.balancedBefore(session, seqs[startIdx])) break;
    } catch {
      return null;
    }
    startIdx += 1;
  }

  if (startIdx > endIdx) return null;
  return {
    startIdx,
    endIdx,
    start: seqs[startIdx],
    end: seqs[endIdx],
    seqs: seqs.slice(startIdx, endIdx + 1),
  };
}
