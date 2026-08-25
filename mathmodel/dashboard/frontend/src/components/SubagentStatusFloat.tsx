import { useId, useMemo, useState } from 'react'
import type { AgentEvent, RunDetail, RunStatus } from '../types'
import { useLanguage } from '../i18n'

type SubagentState = 'running' | 'completed' | 'failed' | 'cancelled' | 'incomplete'

interface SubagentStatus {
  id: number
  task: string
  activity: string
  state: SubagentState
}

function compactText(value: string | undefined, maxLength = 110): string {
  const normalized = (value ?? '').replace(/\s+/g, ' ').trim()
  if (normalized.length <= maxLength) return normalized
  return `${normalized.slice(0, maxLength - 1).trimEnd()}…`
}

function eventActivity(event: AgentEvent, en: boolean): string | null {
  if (event.kind === 'assistant') {
    const response = compactText(event.text)
    if (response) return response
    const tools = (event.tool_calls ?? []).map(([name]) => name)
    if (tools.length > 0) return en
      ? `Using ${tools.join(', ')}`
      : `正在使用 ${tools.join('、')}`
    if (event.reasoning_text?.trim()) return en ? 'Analyzing the task' : '正在分析任务'
  }
  if (event.kind === 'tool_heartbeat') return en
    ? `Running ${event.name ?? 'a tool'}`
    : `正在执行 ${event.name ?? '工具'}`
  if (event.kind === 'provider_heartbeat') return en ? 'Thinking' : '正在思考'
  if (event.kind === 'tool_result') return en
    ? `${event.name ?? 'Tool'} completed`
    : `${event.name ?? '工具'} 已完成`
  return null
}

export function collectSubagentStatuses(events: AgentEvent[], en: boolean, runStatus: RunStatus): SubagentStatus[] {
  const byId = new Map<number, SubagentStatus>()
  for (const event of events) {
    if (event.subagent == null) continue
    if (event.kind === 'subagent_start') {
      const task = compactText(event.task, 150)
      byId.set(event.subagent, {
        id: event.subagent,
        task,
        activity: en ? 'Starting delegated task' : '正在开始委派任务',
        state: 'running',
      })
      continue
    }
    const agent = byId.get(event.subagent)
    if (!agent) continue
    if (event.kind === 'subagent_end') {
      const terminal = event.status === 'failed'
        ? 'failed'
        : event.status === 'cancelled'
          ? 'cancelled'
          : 'completed'
      agent.state = terminal
      agent.activity = terminal === 'completed'
        ? (en ? 'Task completed' : '任务已完成')
        : terminal === 'failed'
          ? (en ? 'Task failed' : '任务失败')
          : (en ? 'Task cancelled' : '任务已取消')
      continue
    }
    const activity = eventActivity(event, en)
    if (activity) agent.activity = activity
  }
  const runIsActive = runStatus === 'running' || runStatus === 'waiting_input'
  return [...byId.values()].map((agent) => (
    !runIsActive && agent.state === 'running'
      ? {
        ...agent,
        state: 'incomplete' as const,
        activity: en ? 'Did not return before the run ended' : '会话结束前未返回',
      }
      : agent
  ))
}

export function SubagentStatusFloat({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [collapsed, setCollapsed] = useState(false)
  const listId = useId()
  const agents = useMemo(
    () => collectSubagentStatuses(run.events, en, run.status),
    [run.events, run.status, en],
  )
  if (agents.length === 0) return null
  const completed = agents.filter((agent) => agent.state === 'completed').length

  const stateLabel: Record<SubagentState, string> = {
    running: en ? 'Running' : '执行中',
    completed: en ? 'Done' : '已完成',
    failed: en ? 'Failed' : '失败',
    cancelled: en ? 'Cancelled' : '已取消',
    incomplete: en ? 'Incomplete' : '未完成',
  }

  return (
    <aside className={`subagent-status-float ${collapsed ? 'is-collapsed' : ''}`} aria-live="polite" aria-label={en ? 'Sub-agent status' : '协作 Agent 状态'}>
      <header className="subagent-status-header">
        <button
          type="button"
          aria-controls={listId}
          aria-expanded={!collapsed}
          aria-label={collapsed
            ? (en ? 'Expand Sub-agent status' : '展开协作 Agent 状态')
            : (en ? 'Collapse Sub-agent status' : '折叠协作 Agent 状态')}
          onClick={() => setCollapsed((current) => !current)}
        >
          <div><strong>{en ? 'Sub-agents' : '协作 Agent'}</strong><b>{agents.length}</b></div>
          <span>{en ? `${completed}/${agents.length} done` : `${completed}/${agents.length} 已完成`}</span>
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M2.5 4.25 6 7.5l3.5-3.25" />
          </svg>
        </button>
      </header>
      <div className="subagent-status-list" id={listId} aria-hidden={collapsed}>
        {agents.map((agent) => (
          <article className={agent.state} key={agent.id}>
            <span className="subagent-status-mark" aria-hidden="true">
              {agent.state === 'running' ? '' : agent.state === 'completed' ? '✓' : '!'}
            </span>
            <div>
              <header><code>SUB-{agent.id}</code><small>{stateLabel[agent.state]}</small></header>
              <p title={agent.task}>{agent.task || (en ? 'Delegated task' : '委派任务')}</p>
              <span title={agent.activity}>{agent.activity}</span>
            </div>
          </article>
        ))}
      </div>
    </aside>
  )
}
