// lib/engine.mjs — IotCompactionEngine：把"压缩历史"换成 IoT 的 thinking 提纯
//
// 与 dsh-compaction-basic 的关系（这是本插件的全部价值，不是包装）：
//   · 触发时机：宿主的 auto 接线是 **between-step**（step 边界 = 一个 thinking block
//     刚结束、下一个 block 还没开始）——正好是 IoT 规格要求的那个点。
//   · `summarize()` 把"散文摘要"换成"条目提纯"。
//   · `everyStep: true` 时**不再等压力阈值**：本类自己选一个工具配对平衡、
//     覆盖到当前尾部的段，直接调宿主的 `compactRegion()`。阈值仍是宿主默认值，
//     只用于 `everyStep: false` 的按压力触发路径。
//   · 闸门：选段里**没有 thinking 就不压**（否则每步都白调一次模型）。
//     0.2.x 的 surface 是 seq 数组，消息由 lib/surface.mjs 投影。
//
// ⚠ 挂载层是决定性的（harness-evolve/knowledge/harness-debug.md「上下文压缩(一)」）：
//   preset 会话的 compaction 是 entry-local realm，**host 层挂的后端对它永不触发**。
//   给 preset 会话换后端 ⇒ 必须挂进该 preset 自己的 compaction 组（见 deploy/）。

import { importSeam } from "./seams.mjs";
import { IOT_INSTRUCTION, extractThinking, hasThinking } from "./think.mjs";
import { messagesForSeqs, selectEveryStepRange, surfaceMessages } from "./surface.mjs";
import { assertRetentionBelowThreshold, normalizeThresholdRatio } from "./config-check.mjs";
import { appendFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// 加载账本：dsh 的启动日志不保证打出插件自己的 logger.info，
// "配置里有 entry" 也不等于 "引擎真的被实例化"。写一份事实账本便于排障。
const MARK_DIR = process.env.IOT_COMPACTION_MARK_DIR || join(tmpdir(), "iot-compaction");
function mark(event, payload = {}) {
  try {
    mkdirSync(MARK_DIR, { recursive: true });
    appendFileSync(
      join(MARK_DIR, "events.jsonl"),
      `${JSON.stringify({ event, t: new Date().toISOString(), pid: process.pid, ...payload })}\n`
    );
  } catch {
    /* 观测失败不致命 */
  }
}

const Basic = await importSeam("@deepseek-ai/dsh-compaction-basic");
const Compaction = await importSeam("@deepseek-ai/dsh-compaction");
const Llm = await importSeam("@deepseek-ai/dsh-llm");

const BasicCompactionEngine = Basic.BasicCompactionEngine ?? Basic.default;
const { BlockAssembler, createUserMessage } = Llm;
const { toolPairingBalancedAfter, toolPairingBalancedBefore } = Compaction;

/** 只把 text 块当作可提交的摘要（避免把 reasoning 又塞回上下文）。 */
function textBlocks(blocks) {
  return (blocks || []).filter((b) => b && b.type === "text" && typeof b.text === "string");
}

/** 给提纯指令附一点"这轮在干什么"的定位信息（不含 reasoning 原文之外的判断）。 */
function contextNote(visible, toolCalls) {
  const parts = [];
  if (toolCalls.length) parts.push(`本轮调用过的工具：${toolCalls.join(", ")}`);
  if (visible.trim()) parts.push(`本轮正文（仅供定位，不要复述）：${visible.trim().slice(0, 1200)}`);
  return parts.length ? `\n补充定位信息：\n${parts.join("\n")}` : "";
}

// thresholdRatio 的合法区间由**宿主**决定：(0, 1]；而且还有第二条不变量
// retainRatio < thresholdRatio。两条规则与真机事故记录都在 config-check.mjs。
// 校验必须发生在 `super()` 之前：宿主构造函数内部就会 resolveConfig 并抛错，
// 而构造期抛错 = 整个 plugin tree 加载失败 = dsh web 起不来。
export class IotCompactionEngine extends BasicCompactionEngine {
  constructor(ctx, config = {}) {
    const everyStep = config.everyStep !== false;
    // thresholdRatio 只在 `everyStep: false` 的宿主路径里生效；默认**不传**，
    // 交给宿主自己的默认压力阈值。
    //
    // 不能靠"极小阈值"表达"每步都压"——宿主两条不变量会拦下（真机崩过两次）：
    //   0    → thresholdRatio must be a number in (0, 1]
    //   1e-6 → retainRatio (0.12) must be less than the resolved thresholdRatio (1e-6)
    // 所以 everyStep 走的是"自己选段 + compactRegion"，不是调阈值。
    const thresholdRatio =
      config.thresholdRatio === undefined
        ? undefined
        : normalizeThresholdRatio(config.thresholdRatio);
    const retainRatio = config.retainRatio ?? 0.12;
    assertRetentionBelowThreshold(retainRatio, thresholdRatio);

    super(ctx, {
      // auto:true = 用宿主的事件接线（between-step pressure / request-error overflow），
      // compactIfNeeded 是动态分派，所以下面的覆盖在事件时点生效。
      auto: true,
      ...(thresholdRatio === undefined ? {} : { thresholdRatio }),
      // retainRatio 只用于 everyStep:false / context-overflow 的宿主路径。
      retainRatio,
      ...(config.maxTokens === undefined ? {} : { maxTokens: config.maxTokens }),
      ...(config.modelPolicies === undefined ? {} : { modelPolicies: config.modelPolicies }),
    });
    this.iot = {
      maxTokens: config.maxTokens ?? 1200,
      // true = 自己选段并直接 compactRegion（不等压力阈值）；
      // false = 完全交给宿主按压力阈值触发。
      everyStep,
      // false 时即使没有可提纯的 thinking 也走宿主摘要（不该开，留作对照）
      requireThinking: config.requireThinking !== false,
    };
    mark("constructed", {
      everyStep: this.iot.everyStep,
      thresholdRatio: thresholdRatio ?? "(host default)",
      retainRatio,
    });
  }

  /** 从 agent 解析这次辅助调用该用哪个 provider/model（与宿主同序：latest → options）。 */
  targetOf(agent) {
    const latest = agent?.session?.requestHeader?.()?.config;
    if (latest?.provider && latest?.model) {
      return { provider: latest.provider, model: latest.model };
    }
    const opts = agent?.options;
    if (opts?.provider && opts?.model) {
      return { provider: opts.provider, model: opts.model };
    }
    return undefined;
  }

  /**
   * 闸门：选段里没有 thinking 就不值得为它花一次模型调用。
   * `seqs` 缺省时看整个 surface；拿不到消息时**不拦**（宁可压一次，也不要静默失效）。
   */
  hasThinkingToCompact(agent, seqs) {
    if (!this.iot.requireThinking) return true;
    const messages = seqs ? messagesForSeqs(agent?.session, seqs) : surfaceMessages(agent?.session);
    if (messages.length === 0) return true;
    return hasThinking(messages);
  }

  async compactIfNeeded(agent, trigger, signal) {
    signal?.throwIfAborted();
    // 只接管 between-step 的压力触发。context-overflow 是紧急恢复，
    // 交给宿主（它会强制压到可压为止），且仍然走本类的 summarize()。
    if (!this.iot.everyStep || trigger !== "pressure") {
      try {
        return await super.compactIfNeeded(agent, trigger, signal);
      } catch (error) {
        this.ctx.logger.warn(`iot-compaction: compaction failed: ${String(error?.message ?? error)}`);
        return null;
      }
    }

    try {
      if (this.targetOf(agent) === undefined) return null;
      const meter = this.ctx.tokenMeter;
      if (!meter) return null;
      const measurement = meter.measure(agent.session);
      const range = selectEveryStepRange(agent.session, measurement, {
        balancedBefore: toolPairingBalancedBefore,
        balancedAfter: toolPairingBalancedAfter,
      });
      if (range === null) return null;
      if (!this.hasThinkingToCompact(agent, range.seqs)) return null;

      const result = await this.compactRegion(range.start, range.end, agent, signal);
      mark("every-step", {
        start: range.start,
        end: range.end,
        nodes: range.seqs.length,
      });
      return result;
    } catch (error) {
      this.ctx.logger.warn(
        `iot-compaction: every-step compaction failed: ${String(error?.message ?? error)}`
      );
      return null;
    }
  }

  /**
   * **唯一的行为覆盖**：把宿主的"散文摘要"换成 IoT 的"条目提纯"。
   *
   * 输入复用宿主重放的会话前缀（system + tools + 历史），所以这次辅助调用仍然
   * 吃得到提供方的前缀缓存；只有末尾那条提纯指令是新增输入。
   * 段内没有 thinking 时就退回宿主摘要（保证插件在纯工具会话里也不失效）。
   */
  async summarize(input, agent, signal) {
    const { reasoning, visible, toolCalls } = extractThinking(input.messages);
    if (!reasoning.trim()) {
      return super.summarize(input, agent, signal);
    }

    const target = this.targetOf(agent);
    if (!target) {
      throw new Error(
        "iot-compaction: 没有可用的 provider/model（先路由一次请求，或设 AgentOptions.provider/model）"
      );
    }

    const assembler = new BlockAssembler();
    const options = {
      provider: target.provider,
      model: target.model,
      messages: [
        ...input.messages,
        createUserMessage({
          content: [{ type: "text", text: IOT_INSTRUCTION + contextNote(visible, toolCalls) }],
          source: { kind: "plugin", plugin: "iot-compaction" },
        }),
      ],
      ...(input.tools === undefined ? {} : { tools: [...input.tools] }),
      maxTokens: this.iot.maxTokens,
      sessionId: agent?.session?.id,
      purpose: "compaction",
      ...(signal === undefined ? {} : { signal }),
    };

    for await (const chunk of this.ctx.llm.stream(options)) assembler.push(chunk);

    const finish = assembler.finish;
    if (finish && finish.kind === "error") {
      throw new Error(
        `iot-compaction: 提纯调用的模型请求失败: ${finish.failure?.message ?? "unknown"}`
      );
    }

    const rawOutput = assembler.blocks();
    const summary = textBlocks(rawOutput);
    if (!summary.some((block) => block.text.trim().length > 0)) {
      throw new Error("iot-compaction: 提纯没有产出任何文本条目");
    }

    return {
      summary,
      rawOutput,
      llmStreamCall: true,
      provider: options.provider,
      model: options.model,
      maxTokens: this.iot.maxTokens,
      ...(assembler.usage === undefined ? {} : { usage: assembler.usage }),
    };
  }
}

export default IotCompactionEngine;
