import type { ContextCategory, RunStatus } from './types'
import type { Language } from '../src/i18n'

export const categoryMeta: Record<
  Language,
  Record<ContextCategory, { short: string; label: string }>
> = {
  en: {
    system_prompt: { short: 'SYS', label: 'System Prompt' },
    working_memory: { short: 'MEM', label: 'Working Memory' },
    system_instruction: { short: 'INST', label: 'System Instruction' },
    user_input: { short: 'USER', label: 'User Input' },
    assistant_response: { short: 'ASST', label: 'Assistant Response' },
    reasoning: { short: 'RSN', label: 'Reasoning' },
    tool_call: { short: 'CALL', label: 'Tool Call' },
    tool_result: { short: 'RESULT', label: 'Tool Result' },
    tool_definition: { short: 'TOOLS', label: 'Tool Definitions' },
    metadata: { short: 'META', label: 'Metadata' },
  },
  zh: {
    system_prompt: { short: '系统', label: 'System Prompt' },
    working_memory: { short: '记忆', label: 'Working Memory' },
    system_instruction: { short: '指令', label: 'System Instruction' },
    user_input: { short: '用户', label: '用户输入' },
    assistant_response: { short: '助手', label: 'Assistant Response' },
    reasoning: { short: '推理', label: 'Reasoning' },
    tool_call: { short: '工具', label: 'Tool Call' },
    tool_result: { short: '结果', label: 'Tool Result' },
    tool_definition: { short: '定义', label: 'Tool Definitions' },
    metadata: { short: '参数', label: 'Metadata' },
  },
}

export const filterCategories: ContextCategory[] = [
  'system_prompt',
  'working_memory',
  'tool_definition',
  'system_instruction',
  'user_input',
  'assistant_response',
  'reasoning',
  'tool_call',
  'tool_result',
  'metadata',
]

export function formatClock(value: number | null | undefined, language: Language = 'en') {
  if (!value) return '—'
  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(value * 1000)
}

export function formatDate(value: number | null | undefined, language: Language = 'en') {
  if (!value) return '—'
  return new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(value * 1000)
}

export function formatTokens(value?: number | null) {
  if (!value) return '0'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}m`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return new Intl.NumberFormat('en-US').format(value)
}

export function statusLabel(status: RunStatus, language: Language) {
  return language === 'en'
    ? {
      running: 'Running',
      waiting_input: 'Waiting for input',
      done: 'Completed',
      error: 'Error',
      stopped: 'Stopped',
      cancelled: 'Cancelled',
      draft: 'Draft',
      unknown: 'Unknown',
    }[status]
    : {
      running: '运行中',
      waiting_input: '等待输入',
      done: '已完成',
      error: '异常',
      stopped: '已停止',
      cancelled: '已取消',
      draft: '草稿',
      unknown: '未知',
    }[status]
}

export function stringifyContent(value: unknown) {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}
