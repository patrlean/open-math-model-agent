import type { AgentEvent, RunStatus } from './types'

export const statusLabel: Record<RunStatus, string> = {
  draft: '等待描述',
  running: '进行中',
  waiting_input: '等待你的回答',
  done: '已完成',
  error: '执行异常',
  stopped: '已停止',
  cancelled: '已中断',
  unknown: '状态未知',
}

export function relativeTime(timestamp?: number): string {
  if (!timestamp) return '—'
  const seconds = Math.max(0, Date.now() / 1000 - timestamp)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

export function timestampLabel(timestamp?: number): string {
  if (!timestamp) return '—'
  const date = new Date(timestamp * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function clock(timestamp?: number): string {
  if (!timestamp) return ''
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(timestamp * 1000)
}

export function compactNumber(value?: number): string {
  if (!value) return '0'
  const absolute = Math.abs(value)
  const format = (scaled: number) => new Intl.NumberFormat('en-US', {
    maximumFractionDigits: 1,
    useGrouping: false,
  }).format(scaled)
  if (absolute >= 1_000_000) return `${format(value / 1_000_000)}m`
  if (absolute >= 1_000) return `${format(value / 1_000)}k`
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value)
}

export function eventToolPreview(event: AgentEvent): string {
  if (!event.observation) return '等待返回…'
  const line = event.observation.split('\n').find((item) => item.trim()) ?? '已完成'
  return line.length > 92 ? `${line.slice(0, 92)}…` : line
}

export function prettyJson(raw?: string): string {
  if (!raw) return '{}'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}
