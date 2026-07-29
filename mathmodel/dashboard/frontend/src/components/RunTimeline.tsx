import { useState } from 'react'
import type { AgentEvent, RunDetail, VerificationIssue } from '../types'
import { clock, eventToolPreview, prettyJson } from '../helpers'
import agentPetClosedUrl from '../assets/agent-pet-closed-v3.png'
import agentPetOpenUrl from '../assets/agent-pet-open-v3.png'
import loadingRingUrl from '../assets/sequential-loading-ring.svg'
import { MarkdownContent } from './MarkdownContent'
import { api } from '../api'

interface ToolCall {
  name: string
  args: string
  result?: string
}

interface Message {
  kind: 'assistant' | 'delegation' | 'compaction' | 'verification' | 'done' | 'stopped' | 'user'
  text?: string
  reasoning?: string
  ts?: number
  step?: number
  context?: number
  verdict?: 'checking' | 'passed' | 'rejected'
  attempt?: number
  summary?: string
  issues?: VerificationIssue[]
  calls?: ToolCall[]
  agents?: { id: number; task: string; tokens?: number }[]
}

function SequentialLoadingRing({ className = '' }: { className?: string }) {
  return <img className={`sequential-loading-ring ${className}`} src={loadingRingUrl} alt="" aria-hidden="true" />
}

function AgentPet({ isThinking = false }: { isThinking?: boolean }) {
  return (
    <span
      className={`agent-pet ${isThinking ? 'is-thinking' : ''}`}
      role="img"
      aria-label={isThinking ? 'Agent 正在思考' : 'Agent'}
    >
      <img className="agent-pet-frame agent-pet-open" src={agentPetOpenUrl} alt="" aria-hidden="true" />
      <img className="agent-pet-frame agent-pet-closed" src={agentPetClosedUrl} alt="" aria-hidden="true" />
    </span>
  )
}

function CopyMessageButton({ text, idleLabel = '复制消息' }: { text: string; idleLabel?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'error'>('idle')

  async function copy() {
    try {
      let copied = false
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(text)
          copied = true
        } catch {
          // Some embedded browsers expose Clipboard API but deny its permission.
        }
      }
      if (!copied) {
        const textarea = document.createElement('textarea')
        textarea.value = text
        textarea.style.position = 'fixed'
        textarea.style.left = '-9999px'
        textarea.style.opacity = '0'
        document.body.appendChild(textarea)
        textarea.focus()
        textarea.select()
        copied = document.execCommand('copy')
        textarea.remove()
      }
      if (!copied) throw new Error('copy command failed')
      setState('copied')
    } catch {
      setState('error')
    }
    window.setTimeout(() => setState('idle'), 1600)
  }

  const label = state === 'copied' ? '已复制' : state === 'error' ? '复制失败' : idleLabel
  return (
    <button
      type="button"
      className={`message-copy-button ${state}`}
      aria-label={label}
      title={label}
      onClick={() => void copy()}
    >
      {state === 'copied' ? (
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="m3.4 8.2 2.7 2.7 6.4-6.4" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <rect x="5.2" y="4.8" width="7.2" height="8" rx="1.5" />
          <path d="M10.7 4.8V3.6A1.6 1.6 0 0 0 9.1 2H3.6A1.6 1.6 0 0 0 2 3.6v6A1.6 1.6 0 0 0 3.6 11h1.6" />
        </svg>
      )}
      <span>{label}</span>
    </button>
  )
}

function DraftLoadingRing() {
  const [darkHalfTurns, setDarkHalfTurns] = useState(0)
  const [lightHalfTurns, setLightHalfTurns] = useState(0)

  return (
    <svg
      className="sequential-loading-ring is-draft-state"
      viewBox="0 0 100 100"
      role="img"
      aria-label="双环会话图标"
      onMouseEnter={() => setDarkHalfTurns((turns) => turns + 1)}
      onMouseLeave={() => setLightHalfTurns((turns) => turns + 1)}
    >
      <g className="draft-ring-dark" style={{ transform: `rotate(${darkHalfTurns * 180}deg)` }} fill="#252629">
        <path d="M 10.1 33.9 A 43 43 0 1 1 41.1 92 Q 41.8 92.2 41.4 90.3 C 42 88.3 45.3 86.7 50 86 A 36 36 0 1 0 20.2 29.9 C 17 30.9 13.5 32.5 11.7 34.6 Q 10.4 35.1 10.1 33.9 Z" />
      </g>
      <g className="draft-ring-light" style={{ transform: `rotate(${lightHalfTurns * 180}deg)` }} fill="#a4a6aa">
        <path d="M 81.1 28.2 A 38 38 0 1 1 24.1 22.2 Q 24.6 22 25.3 23.5 C 24.8 26.3 23.5 29.4 22.6 30.8 A 33.5 33.5 0 1 0 80.8 36.9 C 80.9 34.1 79.9 31.2 79.7 29.2 Q 80.1 28.2 81.1 28.2 Z" />
      </g>
    </svg>
  )
}

function buildMessages(events: AgentEvent[]): Message[] {
  const messages: Message[] = []
  let activeAssistant: Message | undefined
  let sawFirstTask = false

  for (const event of events) {
    if (event.kind === 'task' && event.subagent == null) {
      // The very first task is already shown in the "你的任务" card above the
      // timeline; only a continued conversation's follow-up messages appear
      // here, as their own bubbles.
      if (sawFirstTask) messages.push({ kind: 'user', text: event.task, ts: event.ts })
      sawFirstTask = true
      continue
    }
    if (event.kind === 'subagent_start') {
      const last = messages.at(-1)
      const agent = { id: event.subagent ?? 0, task: event.task ?? '' }
      if (last?.kind === 'delegation') last.agents?.push(agent)
      else messages.push({ kind: 'delegation', agents: [agent] })
      continue
    }
    if (event.kind === 'subagent_end') {
      for (const message of [...messages].reverse()) {
        const agent = message.agents?.find((item) => item.id === event.subagent)
        if (agent) {
          agent.tokens = event.tokens
          break
        }
      }
      continue
    }
    if (event.subagent != null) continue
    if (event.kind === 'assistant') {
      activeAssistant = {
        kind: 'assistant',
        text: event.text,
        reasoning: event.reasoning_text,
        ts: event.ts,
        step: event.step,
        context: event.context_tokens,
        calls: (event.tool_calls ?? [])
          .filter(([name]) => name !== 'spawn_subagent')
          .map(([name, args]) => ({ name, args })),
      }
      messages.push(activeAssistant)
    } else if (event.kind === 'tool_result' && event.name !== 'spawn_subagent') {
      const call = activeAssistant?.calls?.find((item) => item.result == null)
      if (call) call.result = event.observation
      else activeAssistant?.calls?.push({ name: event.name ?? 'tool', args: '', result: event.observation })
    } else if (event.kind === 'compact_start') {
      messages.push({ kind: 'compaction', context: event.context_tokens, text: `摘要 ${event.summarizing ?? 0} 条记录，保留 ${event.keeping ?? 0} 条关键上下文` })
    } else if (event.kind === 'verification_start') {
      messages.push({
        kind: 'verification',
        verdict: 'checking',
        text: `验证 Agent 正在检查第 ${event.attempt ?? 1} 版候选结果`,
      })
    } else if (event.kind === 'verification_result') {
      const pendingVerification = [...messages].reverse().find(
        (message) => message.kind === 'verification' && message.verdict === 'checking',
      )
      const result = {
        verdict: event.verdict === 'PASS' ? 'passed' as const : 'rejected' as const,
        attempt: event.attempt,
        summary: event.summary,
        issues: event.issues ?? [],
      }
      if (pendingVerification) Object.assign(pendingVerification, result)
      else messages.push({ kind: 'verification', ...result })
    } else if (event.kind === 'verification_failed') {
      messages.push({
        kind: 'verification',
        verdict: 'rejected',
        attempt: event.attempt,
        summary: event.summary,
        issues: event.issues ?? [],
      })
    } else if (event.kind === 'done') {
      messages.push({ kind: 'done', text: event.text, ts: event.ts })
    } else if (event.kind === 'max_steps') {
      messages.push({ kind: 'stopped' })
    }
  }
  return messages
}

function ToolCard({ call }: { call: ToolCall }) {
  const hasResult = call.result != null
  return (
    <details className="tool-card">
      <summary>
        <span className={`tool-state ${hasResult ? 'done' : ''}`} />
        <code>{call.name}</code>
        <span>{hasResult ? eventToolPreview({ kind: 'tool_result', observation: call.result }) : '准备调用'}</span>
        <b>⌄</b>
      </summary>
      <div className="tool-detail">
        <label>参数</label>
        <pre>{prettyJson(call.args)}</pre>
        {hasResult && <><label>返回</label><pre>{call.result}</pre></>}
      </div>
    </details>
  )
}

function ToolDisclosure({ calls, runStatus }: { calls: ToolCall[]; runStatus: RunDetail['status'] }) {
  const completed = calls.filter((call) => call.result != null).length
  const label = completed === calls.length
    ? '查看本轮工具记录'
    : runStatus === 'running' ? '正在处理工具记录' : '本轮工具调用中断'
  return (
    <details className="tool-disclosure">
      <summary>
        <span className="disclosure-chevron">⌄</span>
        <span>{label}</span>
        <small>{calls.length} 次调用</small>
      </summary>
      <div className="tool-stack">{calls.map((call, index) => <ToolCard key={`${call.name}-${index}`} call={call} />)}</div>
    </details>
  )
}

function ReasoningDisclosure({ reasoning }: { reasoning: string }) {
  return (
    <details className="reasoning-disclosure">
      <summary>
        <span className="disclosure-chevron">⌄</span>
        <span>查看本轮推理过程</span>
      </summary>
      <MarkdownContent className="reasoning-markdown" content={reasoning} />
    </details>
  )
}

function AssistantMessage({ message, runStatus }: { message: Message; runStatus: RunDetail['status'] }) {
  return (
    <article className="timeline-message assistant-message copyable-message agent-copyable">
      {message.text && <MarkdownContent content={message.text} />}
      {message.reasoning?.trim() && <ReasoningDisclosure reasoning={message.reasoning} />}
      {message.calls && message.calls.length > 0 && <ToolDisclosure calls={message.calls} runStatus={runStatus} />}
      {message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} idleLabel="复制回复" /></div>}
    </article>
  )
}

function Delegation({ message }: { message: Message }) {
  const agents = message.agents ?? []
  const running = agents.filter((agent) => agent.tokens == null).length
  return (
    <article className="delegation-card">
      <div className="delegation-heading">
        <span className="branch-icon">⌘</span>
        <strong>{agents.length > 1 ? `并行委派了 ${agents.length} 个 Sub-Agent` : '已委派给 Sub-Agent'}</strong>
        <span className={running ? 'live-label' : 'complete-label'}>{running ? `${running} 个正在执行` : '全部返回'}</span>
      </div>
      <div className="delegation-list">
        {agents.map((agent) => <div key={agent.id}>
          <span className={agent.tokens == null ? 'tiny-pulse' : 'tiny-dot'} />
          <code>SUB-{agent.id}</code>
          <span>{agent.task}</span>
          <small>{agent.tokens == null ? '运行中' : `${agent.tokens.toLocaleString()} tok`}</small>
        </div>)}
      </div>
    </article>
  )
}

function PendingQuestionCard({ runId, question }: { runId: string; question: NonNullable<RunDetail['pending_question']> }) {
  const [customText, setCustomText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(response: { answer?: string; option_id?: string }) {
    if ((!response.answer?.trim() && !response.option_id) || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await api.answer(runId, {
        ...response,
        ...(response.answer ? { answer: response.answer.trim() } : {}),
      })
      setCustomText('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '提交回答失败，请重试。')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <article className="pending-question-card">
      <div className="pending-question-heading">
        <span className="branch-icon">?</span>
        <strong>Agent 想确认一下</strong>
      </div>
      <p className="pending-question-text">{question.question}</p>
      {question.options.length > 0 && (
        <div className="pending-question-options">
          {question.options.map((option) => (
            <button
              key={option.id}
              className="pending-question-option"
              disabled={submitting}
              onClick={() => void submit({ option_id: option.id })}
            >
              <strong>{option.label}</strong>
              {option.description && <span>{option.description}</span>}
            </button>
          ))}
        </div>
      )}
      {question.allow_custom && (
        <form
          className="pending-question-custom"
          onSubmit={(event) => {
            event.preventDefault()
            void submit({ answer: customText })
          }}
        >
          <input
            type="text"
            value={customText}
            onChange={(event) => setCustomText(event.target.value)}
            placeholder="或者，输入你自己的回答…"
            disabled={submitting}
          />
          <button type="submit" disabled={submitting || !customText.trim()}>发送</button>
        </form>
      )}
      {error && <p className="pending-question-error">{error}</p>}
    </article>
  )
}

function VerificationNotice({ message, onOpenVerification }: { message: Message; onOpenVerification: () => void }) {
  if (message.verdict === 'checking') return <div className="timeline-notice verification checking">◌ {message.text}</div>
  const issues = message.issues ?? []
  const critical = issues.filter((issue) => issue.severity === 'critical').length
  const major = issues.filter((issue) => issue.severity === 'major').length
  const issueText = issues.length > 0
    ? `${issues.length} 个问题${critical > 0 ? ` · ${critical} 严重` : ''}${major > 0 ? ` · ${major} 主要` : ''}`
    : '未发现待解决问题'
  return <article className={`verification-notice ${message.verdict}`}>
    <span>{message.verdict === 'passed' ? '✓' : '!'}</span>
    <div>
      <strong>{message.verdict === 'passed' ? '独立验证已通过' : '独立验证未通过，已退回主 Agent 修订'}</strong>
      <small>{issueText}{message.attempt ? ` · 第 ${message.attempt} 轮` : ''}</small>
      {message.summary && <MarkdownContent
        className="verification-notice-markdown"
        content={message.summary}
        normalizeJoinedHeadings
      />}
    </div>
    <button type="button" onClick={onOpenVerification}>查看验证详情 <b>→</b></button>
  </article>
}

export function RunTimeline({ run, onOpenVerification }: { run: RunDetail; onOpenVerification: () => void }) {
  const messages = buildMessages(run.events)
  if (messages.length === 0) {
    const emptyIcon = run.status === 'draft'
      ? <DraftLoadingRing />
      : run.status === 'running'
        ? <SequentialLoadingRing className="is-empty-state" />
        : <span>◌</span>
    return <div className="timeline-empty">{emptyIcon}<strong>{run.status === 'draft' ? '新的会话已创建' : run.status === 'running' ? 'Agent 正在准备' : '尚未记录会话过程'}</strong><p>{run.status === 'draft' ? '在底部描述问题或添加材料，然后开始会话。' : '新的步骤、工具调用与结果会在这里持续出现。'}</p></div>
  }
  return (
    <section className="timeline">
      {run.task && <article className="task-brief copyable-message user-copyable"><span>你的任务</span><p>{run.task}</p><div className="message-actions"><CopyMessageButton text={run.task} idleLabel="复制任务" /></div></article>}
      {(run.status === 'error' || run.status === 'stopped' || run.status === 'cancelled') && (
        <article className="run-recovery-notice">
          <strong>
            {run.status === 'error' ? '本轮会话已异常停止' : run.status === 'cancelled' ? '本轮会话已被中断' : '本轮会话已停止'}
          </strong>
          <p>
            {run.status === 'cancelled'
              ? '你手动停止了这次会话。已生成的计划、结果和素材都还保留着，可以重新开始一轮干净的会话。'
              : (run.failure_reason || '当前任务没有继续产生新的运行记录。你可以重新开始一轮干净的会话。')}
          </p>
        </article>
      )}
      {messages.map((message, index) => {
        if (message.kind === 'assistant') return <AssistantMessage key={index} message={message} runStatus={run.status} />
        if (message.kind === 'delegation') return <Delegation key={index} message={message} />
        if (message.kind === 'compaction') return <div key={index} className="timeline-notice">✦ 上下文已整理 · {message.text}</div>
        if (message.kind === 'verification') return <VerificationNotice key={index} message={message} onOpenVerification={onOpenVerification} />
        if (message.kind === 'stopped') return <div key={index} className="timeline-notice warning">此轮会话达到最大步数而停止。</div>
        if (message.kind === 'user') return (
          <article key={index} className="user-followup copyable-message user-copyable">
            <p>{message.text}</p>
            <time>{clock(message.ts)}</time>
            {message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} /></div>}
          </article>
        )
        return <article key={index} className="final-answer copyable-message agent-copyable"><div className="final-answer-meta"><AgentPet /><strong>Agent 已完成</strong><time>{clock(message.ts)}</time></div>{message.text && <MarkdownContent content={message.text} />}{message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} idleLabel="复制回复" /></div>}</article>
      })}
      {run.status === 'waiting_input' && run.pending_question && (
        <PendingQuestionCard runId={run.id} question={run.pending_question} />
      )}
      {run.status === 'running' && <div className="thinking-indicator"><AgentPet isThinking /><span>Agent 正在思考</span></div>}
    </section>
  )
}
