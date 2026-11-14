// Dynamic user-role runtime context; tool schema owns the detailed parameter contract.
import { normalizeTeacherLimits, teacherStatus, sessionIdOf } from './teacher.mjs';
export const TEACHER_CONTEXT_NAME = 'iot-thinking.teacher';
export const TEACHER_CONTEXT_MAX_CHARS = 180;
export function teacherContextText({ enabled, sessionId, limits }) {
  if (!enabled || !sessionId || sessionId === 'unknown') return '';
  const normalized = normalizeTeacherLimits(limits);
  const status = teacherStatus(sessionId, normalized);
  const state = status.available ? '可用' : `冷却中，还需 ${status.remainingBlocks} 个思考块`;
  // Put state first; fixed vocabulary + normalized numbers, no truncation of vital facts.
  return `【外援咨询】iot_consult_teacher：${state}。用于方案审阅或卡点求助；主动调用，不必每轮使用。传 ask、depth（${normalized.minDepth}-${normalized.maxDepth} 轮正文）、goal、progress，参数详见工具声明。建议仅供参考，由你决定是否采纳。`;
}
export function registerTeacherContext(ctx, getConfig, onError = () => {}) {
  return ctx.inject(['systemPrompt'], ready => {
    let reported = false;
    return ready.systemPrompt.context({
      name: TEACHER_CONTEXT_NAME,
      order: 620,
      interpolate: false,
      text(context) {
        try {
          const sessionId = sessionIdOf(context);
          if (sessionId === 'unknown') return '';
          // No history traversal, prompt rewriting, model calls or logging on the success path.
          const { enabled, teacher } = getConfig(context);
          return teacherContextText({ enabled, sessionId, limits: teacher });
        } catch (error) {
          if (!reported) {
            reported = true;
            try { onError(error); } catch { /* diagnostics must not prevent admission */ }
          }
          return '';
        }
      },
    });
  });
}
