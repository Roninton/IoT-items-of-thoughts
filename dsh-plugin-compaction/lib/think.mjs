// lib/think.mjs — IoT 的压缩输入选取与条目渲染
//
// IoT（Items of Thoughts）的规格：**只压 thinking 块**，正文与工具往返原样保留，
// 用户原话逐字回流。所以这里要做的不是"摘要整段对话"，而是：
//   1) 把这段历史里的 reasoning 块挑出来（其余内容只作为上下文标签）
//   2) 压成"正节点 / 负节点 / 待定 / 风险"的条目链
//
// 这与 dsh-compaction-basic 默认的"散文摘要"是两种东西：摘要丢的是细节，
// 提纯要保的是"为什么这么定 / 否掉了什么"。

const REASONING_TYPES = new Set(["reasoning"]);

function blocksOf(message) {
  if (!message) return [];
  if (Array.isArray(message.content)) return message.content;
  if (typeof message.content === "string") {
    return [{ type: "text", text: message.content }];
  }
  return [];
}

function textOf(blocks) {
  let out = "";
  for (const block of blocks) {
    if (block && block.type === "text" && typeof block.text === "string") out += block.text;
  }
  return out;
}

/**
 * 从待压缩的 surface 里抽出全部 thinking 原文。
 *
 * 返回 `{ reasoning, visible, toolCalls }`：
 *   reasoning = 所有 reasoning 块的文本（**这是提纯的唯一原料**）
 *   visible   = 正文文本（只用来给条目定位"这轮在干什么"，不要求逐字保留）
 *   toolCalls = 工具名列表（条目里"做了什么"的物证索引）
 */
export function extractThinking(messages) {
  const reasoning = [];
  const visible = [];
  const toolCalls = new Set();
  for (const message of messages || []) {
    for (const block of blocksOf(message)) {
      if (!block || typeof block !== "object") continue;
      if (REASONING_TYPES.has(block.type) && typeof block.text === "string") {
        if (block.text.trim()) reasoning.push(block.text);
      } else if (block.type === "text" && typeof block.text === "string") {
        if (block.text.trim()) visible.push(block.text);
      } else if (block.type === "tool-call" && typeof block.name === "string") {
        toolCalls.add(block.name);
      }
    }
  }
  return {
    reasoning: reasoning.join("\n\n"),
    visible: visible.join("\n").slice(0, 4000),
    toolCalls: [...toolCalls],
  };
}

/** 整段历史是否真的含 thinking —— 不含就没有提纯的必要，直接跳过压缩。 */
export function hasThinking(messages) {
  for (const message of messages || []) {
    for (const block of blocksOf(message)) {
      if (block && REASONING_TYPES.has(block.type) && typeof block.text === "string" && block.text.trim()) {
        return true;
      }
    }
  }
  return false;
}

export const IOT_INSTRUCTION = `你是工作记忆的**推理编排器**，不是摘要器。

输入是上文的 thinking（思考草稿）。把它压成**条目**，丢掉填充，保留推理骨架。

输出格式（纯文本，每行一条，不要 JSON、不要代码块、不要前言）：

  · 正节点：写"定了什么 + 为什么成立"
  · 否掉：写"否掉了什么 + 为什么被否"（这类最贵：它拦住下一轮重走一遍）
  · 待定：写"还没定的是什么 + 卡在哪"
  · 风险：写"决定已定 + 什么条件下会出问题"

硬规则：

1. **只压不补。** 每个具体物（文件名、符号名、数字、输入值、异常类型）都必须在原文出现过。
   原文没提到的，一个字都不许写。
2. **禁止完成态断言。** 不许写"已实现 / 已落盘 / 测试通过 / 验证通过"——
   完成状态只能由工具回执给出，不是从思考里推出来的。
3. **否掉的路线必须单独成条。** 不许合并成"考虑过其他方案"。
4. **没定的事不许写成定了的事。** 还在权衡的，用"待定："开头。
5. 一条一行，不换行、不编号、不加标题。原文很干就少写几行，空也是合格答案。
6. 正文用中文。`;
