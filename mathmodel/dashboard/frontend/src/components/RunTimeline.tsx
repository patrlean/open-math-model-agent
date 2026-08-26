import { useState } from 'react'
import type { AgentEvent, PendingQuestion, RunDetail, VerificationIssue } from '../types'
import { clock, eventToolPreview, prettyJson } from '../helpers'
import agentPetClosedUrl from '../assets/agent-pet-closed-v3.png'
import agentPetOpenUrl from '../assets/agent-pet-open-v3.png'
import loadingRingUrl from '../assets/sequential-loading-ring.svg'
import { MarkdownContent } from './MarkdownContent'
import { api } from '../api'
import { type Language, useLanguage } from '../i18n'

interface ToolCall {
  name: string
  args: string
  result?: string
}

interface Message {
  kind: 'assistant' | 'compaction' | 'verification' | 'done' | 'stopped' | 'user' | 'question'
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
  rolling?: boolean
  question?: PendingQuestion
  questionResolved?: boolean
  selectedOptionId?: string | null
}

function SequentialLoadingRing({ className = '' }: { className?: string }) {
  return <img className={`sequential-loading-ring ${className}`} src={loadingRingUrl} alt="" aria-hidden="true" />
}

function AgentPet({ isThinking = false }: { isThinking?: boolean }) {
  const { language } = useLanguage()
  return (
    <span
      className={`agent-pet ${isThinking ? 'is-thinking' : ''}`}
      role="img"
      aria-label={isThinking
        ? (language === 'en' ? 'Agent is thinking' : 'Agent 正在思考')
        : 'Agent'}
    >
      <img className="agent-pet-frame agent-pet-open" src={agentPetOpenUrl} alt="" aria-hidden="true" />
      <img className="agent-pet-frame agent-pet-closed" src={agentPetClosedUrl} alt="" aria-hidden="true" />
    </span>
  )
}

function ActivityEllipsis() {
  const { language } = useLanguage()
  return (
    <span
      className="activity-ellipsis"
      role="status"
      aria-label={language === 'en' ? 'Updating' : '正在更新'}
    >
      <i aria-hidden="true" />
      <i aria-hidden="true" />
      <i aria-hidden="true" />
    </span>
  )
}

function CopyMessageButton({ text, idleLabel }: { text: string; idleLabel?: string }) {
  const { language } = useLanguage()
  const en = language === 'en'
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

  const label = state === 'copied'
    ? (en ? 'Copied' : '已复制')
    : state === 'error'
      ? (en ? 'Copy failed' : '复制失败')
      : (idleLabel ?? (en ? 'Copy message' : '复制消息'))
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
  const { language } = useLanguage()
  const [darkHalfTurns, setDarkHalfTurns] = useState(0)
  const [lightHalfTurns, setLightHalfTurns] = useState(0)

  return (
    <svg
      className="sequential-loading-ring is-draft-state"
      viewBox="0 0 100 100"
      role="img"
      aria-label={language === 'en' ? 'Double-ring conversation icon' : '双环会话图标'}
      onMouseEnter={() => setDarkHalfTurns((turns) => turns + 1)}
      onMouseLeave={() => setLightHalfTurns((turns) => turns + 1)}
    >
      <g className="draft-ring-breath draft-ring-breath-dark">
        <g className="draft-ring-dark" style={{ transform: `rotate(${darkHalfTurns * 180}deg)` }} fill="#252629">
          <path d="M 10.1 33.9 A 43 43 0 1 1 41.1 92 Q 41.8 92.2 41.4 90.3 C 42 88.3 45.3 86.7 50 86 A 36 36 0 1 0 20.2 29.9 C 17 30.9 13.5 32.5 11.7 34.6 Q 10.4 35.1 10.1 33.9 Z" />
        </g>
      </g>
      <g className="draft-ring-breath draft-ring-breath-light">
        <g className="draft-ring-light" style={{ transform: `rotate(${lightHalfTurns * 180}deg)` }} fill="#a4a6aa">
          <path d="M 81.1 28.2 A 38 38 0 1 1 24.1 22.2 Q 24.6 22 25.3 23.5 C 24.8 26.3 23.5 29.4 22.6 30.8 A 33.5 33.5 0 1 0 80.8 36.9 C 80.9 34.1 79.9 31.2 79.7 29.2 Q 80.1 28.2 81.1 28.2 Z" />
        </g>
      </g>
    </svg>
  )
}

function questionFromEvent(event: AgentEvent): PendingQuestion | null {
  if (!['question', 'change_confirmation'].includes(event.kind)) return null
  if (!event.id || !event.question) return null
  return {
    id: event.id,
    kind: event.kind === 'change_confirmation' ? 'change_confirmation' : 'question',
    title: event.title,
    summary: event.summary,
    question: event.question,
    impacts: event.impacts,
    budget: event.budget,
    options: event.options ?? [],
    allow_custom: event.allow_custom ?? event.kind === 'question',
    asked_at: event.asked_at ?? event.ts ?? 0,
    change_request_id: event.change_request_id,
  }
}

function buildMessages(events: AgentEvent[], language: Language): Message[] {
  const en = language === 'en'
  const messages: Message[] = []
  let activeAssistant: Message | undefined
  let rollingAssistant: Message | undefined
  let sawFirstTask = false

  function closeRollingWindow() {
    if (rollingAssistant) rollingAssistant.rolling = false
    rollingAssistant = undefined
  }

  for (const event of events) {
    if (event.kind === 'task' && event.subagent == null) {
      // The very first task is already shown in the "你的任务" card above the
      // timeline; only a continued conversation's follow-up messages appear
      // here, as their own bubbles.
      if (sawFirstTask) {
        closeRollingWindow()
        messages.push({ kind: 'user', text: event.task, ts: event.ts })
      }
      sawFirstTask = true
      continue
    }
    if (event.subagent != null) continue
    const recordedQuestion = questionFromEvent(event)
    if (recordedQuestion) {
      closeRollingWindow()
      activeAssistant = undefined
      messages.push({
        kind: 'question',
        ts: recordedQuestion.asked_at,
        question: recordedQuestion,
        questionResolved: false,
      })
      continue
    }
    if (event.kind === 'ask_resolved' && event.id) {
      const questionMessage = [...messages].reverse().find(
        (message) => message.kind === 'question' && message.question?.id === event.id,
      )
      if (questionMessage) {
        questionMessage.questionResolved = event.answered === true
        questionMessage.selectedOptionId = event.selected_option_id
      }
      continue
    }
    if (event.kind === 'assistant') {
      const assistant: Message = {
        kind: 'assistant',
        text: event.text,
        reasoning: event.reasoning_text,
        ts: event.ts,
        step: event.step,
        context: event.context_tokens,
        calls: (event.tool_calls ?? [])
          .filter(([name]) => name !== 'spawn_subagent' && name !== 'ask_user')
          .map(([name, args]) => ({ name, args })),
      }
      const hasResponse = Boolean(assistant.text?.trim())
      const hasActivity = Boolean(
        assistant.reasoning?.trim() || assistant.calls?.length,
      )
      if (!hasResponse && !hasActivity) {
        activeAssistant = undefined
        continue
      }
      if (hasResponse) {
        // A visible response closes the current rolling activity window. Keep
        // the most recent pre-response reasoning/tool group in history, then
        // render the response as its own fixed message.
        closeRollingWindow()
      } else if (rollingAssistant) {
        // Until a response exists, only the newest reasoning/tool group is
        // useful to the user. Replace the previous rolling entry in place in
        // the projected timeline instead of accumulating every model step.
        const rollingIndex = messages.indexOf(rollingAssistant)
        if (rollingIndex >= 0) messages.splice(rollingIndex, 1)
      }
      messages.push(assistant)
      activeAssistant = assistant
      if (!hasResponse) {
        assistant.rolling = true
        rollingAssistant = assistant
      }
    } else if (event.kind === 'tool_result' && !['spawn_subagent', 'ask_user'].includes(event.name ?? '')) {
      const call = activeAssistant?.calls?.find((item) => item.result == null)
      if (call) call.result = event.observation
      else activeAssistant?.calls?.push({ name: event.name ?? 'tool', args: '', result: event.observation })
    } else if (event.kind === 'compact_start') {
      messages.push({
        kind: 'compaction',
        context: event.context_tokens,
        text: en
          ? `Summarized ${event.summarizing ?? 0} records and kept ${event.keeping ?? 0} key context items`
          : `摘要 ${event.summarizing ?? 0} 条记录，保留 ${event.keeping ?? 0} 条关键上下文`,
      })
    } else if (event.kind === 'verification_start') {
      closeRollingWindow()
      messages.push({
        kind: 'verification',
        verdict: 'checking',
        text: en
          ? `Verification Agent is checking candidate ${event.attempt ?? 1}`
          : `验证 Agent 正在检查第 ${event.attempt ?? 1} 版候选结果`,
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
      closeRollingWindow()
      messages.push({ kind: 'done', text: event.text, ts: event.ts })
    } else if (event.kind === 'max_steps' && event.subagent == null) {
      closeRollingWindow()
      messages.push({ kind: 'stopped' })
    }
  }
  return messages
}

function ToolCard({ call }: { call: ToolCall }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const hasResult = call.result != null
  return (
    <details className="tool-card">
      <summary>
        <span className={`tool-state ${hasResult ? 'done' : ''}`} />
        <code>{call.name}</code>
        <span>{hasResult
          ? eventToolPreview({ kind: 'tool_result', observation: call.result }, language)
          : (en ? 'Preparing call' : '准备调用')}</span>
        <b>⌄</b>
      </summary>
      <div className="tool-detail">
        <label>{en ? 'Arguments' : '参数'}</label>
        <pre>{prettyJson(call.args)}</pre>
        {hasResult && <><label>{en ? 'Result' : '返回'}</label><pre>{call.result}</pre></>}
      </div>
    </details>
  )
}

function ToolDisclosure({
  calls,
  runStatus,
  isRolling,
}: {
  calls: ToolCall[]
  runStatus: RunDetail['status']
  isRolling: boolean
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const completed = calls.filter((call) => call.result != null).length
  const label = completed === calls.length
    ? (en ? 'View tool activity' : '查看本轮工具记录')
    : runStatus === 'running'
      ? (en ? 'Processing tool activity' : '正在处理工具记录')
      : (en ? 'Tool activity interrupted' : '本轮工具调用中断')
  return (
    <details className="tool-disclosure">
      <summary>
        <span className="disclosure-chevron">⌄</span>
        <span>{label}</span>
        {isRolling && <ActivityEllipsis />}
        <small>{en ? `${calls.length} calls` : `${calls.length} 次调用`}</small>
      </summary>
      <div className="tool-stack">{calls.map((call, index) => <ToolCard key={`${call.name}-${index}`} call={call} />)}</div>
    </details>
  )
}

function ReasoningDisclosure({ reasoning, isRolling }: { reasoning: string; isRolling: boolean }) {
  const { language } = useLanguage()
  return (
    <details className="reasoning-disclosure">
      <summary>
        <span className="disclosure-chevron">⌄</span>
        <span>{language === 'en' ? 'View reasoning' : '查看本轮推理过程'}</span>
        {isRolling && <ActivityEllipsis />}
      </summary>
      <MarkdownContent className="reasoning-markdown" content={reasoning} />
    </details>
  )
}

function AssistantMessage({ message, runStatus }: { message: Message; runStatus: RunDetail['status'] }) {
  const { language } = useLanguage()
  const isRolling = message.rolling === true && runStatus === 'running'
  return (
    <article className="timeline-message assistant-message copyable-message agent-copyable">
      {message.text && <MarkdownContent content={message.text} />}
      {message.reasoning?.trim() && <ReasoningDisclosure reasoning={message.reasoning} isRolling={isRolling} />}
      {message.calls && message.calls.length > 0 && (
        <ToolDisclosure calls={message.calls} runStatus={runStatus} isRolling={isRolling} />
      )}
      {message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} idleLabel={language === 'en' ? 'Copy response' : '复制回复'} /></div>}
    </article>
  )
}

function PendingQuestionCard({
  runId,
  question,
  active,
  resolved = false,
  selectedOptionId = null,
}: {
  runId: string
  question: PendingQuestion
  active: boolean
  resolved?: boolean
  selectedOptionId?: string | null
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [customText, setCustomText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [optimisticOptionId, setOptimisticOptionId] = useState<string | null>(null)
  const [optimisticAnswer, setOptimisticAnswer] = useState('')
  const effectiveOptionId = selectedOptionId ?? optimisticOptionId
  const answered = resolved || effectiveOptionId != null || Boolean(optimisticAnswer)
  const canAnswer = active && !answered
  const selectedOption = question.options.find((option) => option.id === effectiveOptionId)

  async function submit(response: { answer?: string; option_id?: string }) {
    if ((!response.answer?.trim() && !response.option_id) || submitting || !canAnswer) return
    setSubmitting(true)
    setError(null)
    try {
      const normalizedAnswer = response.answer?.trim()
      await api.answer(runId, {
        ...response,
        ...(normalizedAnswer ? { answer: normalizedAnswer } : {}),
      })
      if (response.option_id) setOptimisticOptionId(response.option_id)
      if (normalizedAnswer) setOptimisticAnswer(normalizedAnswer)
    } catch (caught) {
      setError(caught instanceof Error
        ? caught.message
        : (en ? 'Could not submit your answer. Please try again.' : '提交回答失败，请重试。'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <article className={`pending-question-card ${question.kind === 'change_confirmation' ? 'change-confirmation' : ''}${answered ? ' is-resolved' : ''}${!active && !answered ? ' is-closed' : ''}`}>
      <div className="pending-question-heading">
        <span className="branch-icon">{question.kind === 'change_confirmation' ? '∆' : '?'}</span>
        <strong>{question.kind === 'change_confirmation'
          ? (en ? 'Research amendment' : '研究修订提案')
          : (en ? 'Agent needs your input' : 'Agent 想确认一下')}</strong>
        <small>{answered
          ? (en ? 'Answered' : '已选择')
          : !active
            ? (en ? 'Closed' : '已结束')
            : question.kind === 'change_confirmation'
              ? (en ? 'No changes have been applied' : '尚未改动当前版本')
              : (en ? 'Waiting for your answer' : '等待你的回答')}</small>
      </div>
      {question.title && <h2 className="pending-question-title">{question.title}</h2>}
      {question.summary && <p className="pending-question-summary">{question.summary}</p>}
      {question.impacts && question.impacts.length > 0 && (
        <div className="change-impact-list">
          <span className="change-section-label">{en ? 'Affected evidence' : '影响范围'}</span>
          {question.impacts.map((impact, index) => <div key={`${impact.target}-${index}`}>
            <strong>{impact.target}</strong>
            <p>{impact.change}</p>
            {impact.reason && <small>{impact.reason}</small>}
          </div>)}
        </div>
      )}
      {question.budget && (
        <div className="change-budget">
          <span>{en ? 'Additional computation budget' : '追加计算预算'}</span>
          <strong>{question.budget.max_additional_cost != null
            ? `${question.budget.currency === 'USD' ? '$' : '¥'}${question.budget.max_additional_cost}`
            : (en ? 'Agent estimate' : '按实际调用结算')}</strong>
          {question.budget.note && <small>{question.budget.note}</small>}
        </div>
      )}
      <p className="pending-question-text">{question.question}</p>
      {question.options.length > 0 && (
        <div className="pending-question-options">
          {question.options.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`pending-question-option option-${option.id}${effectiveOptionId === option.id ? ' is-selected' : ''}`}
              disabled={submitting || !canAnswer}
              aria-pressed={effectiveOptionId === option.id}
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
            placeholder={en ? 'Or enter your own answer…' : '或者，输入你自己的回答…'}
            disabled={submitting || !canAnswer}
          />
          <button type="submit" disabled={submitting || !canAnswer || !customText.trim()}>{en ? 'Send' : '发送'}</button>
        </form>
      )}
      {answered && (
        <div className="pending-question-answer" role="status">
          <span aria-hidden="true">✓</span>
          <small>{en ? 'Your choice' : '你的选择'}</small>
          <strong>{selectedOption?.label || optimisticAnswer || (en ? 'Answer submitted' : '回答已提交')}</strong>
        </div>
      )}
      {error && <p className="pending-question-error">{error}</p>}
    </article>
  )
}

function VerificationNotice({ message, onOpenVerification }: { message: Message; onOpenVerification: () => void }) {
  const { language } = useLanguage()
  const en = language === 'en'
  if (message.verdict === 'checking') return <div className="timeline-notice verification checking">◌ {message.text}</div>
  const issues = message.issues ?? []
  const critical = issues.filter((issue) => issue.severity === 'critical').length
  const major = issues.filter((issue) => issue.severity === 'major').length
  const issueText = issues.length > 0
    ? (en
      ? `${issues.length} issues${critical > 0 ? ` · ${critical} critical` : ''}${major > 0 ? ` · ${major} major` : ''}`
      : `${issues.length} 个问题${critical > 0 ? ` · ${critical} 严重` : ''}${major > 0 ? ` · ${major} 主要` : ''}`)
    : (en ? 'No unresolved issues' : '未发现待解决问题')
  return <article className={`verification-notice ${message.verdict}`}>
    <span>{message.verdict === 'passed' ? '✓' : '!'}</span>
    <div>
      <strong>{message.verdict === 'passed'
        ? (en ? 'Independent verification passed' : '独立验证已通过')
        : (en ? 'Independent verification failed and was returned for revision' : '独立验证未通过，已退回主 Agent 修订')}</strong>
      <small>{issueText}{message.attempt ? (en ? ` · Attempt ${message.attempt}` : ` · 第 ${message.attempt} 轮`) : ''}</small>
      {message.summary && <MarkdownContent
        className="verification-notice-markdown"
        content={message.summary}
        normalizeJoinedHeadings
      />}
    </div>
    <button type="button" onClick={onOpenVerification}>{en ? 'View verification' : '查看验证详情'} <b>→</b></button>
  </article>
}

function stoppedByMainAgentMaxSteps(run: RunDetail): boolean {
  if (run.status !== 'stopped') return false
  if (run.stop_reason) return run.stop_reason === 'max_steps'
  for (let index = run.events.length - 1; index >= 0; index -= 1) {
    const event = run.events[index]
    if (event.subagent != null) continue
    if (event.kind === 'max_steps') return true
    if (event.kind === 'task' || event.kind === 'done' || event.kind === 'cancelled') return false
  }
  return false
}

export function RunTimeline({
  run,
  onOpenVerification,
  onContinueAfterMaxSteps,
  continuingAfterMaxSteps,
}: {
  run: RunDetail
  onOpenVerification: () => void
  onContinueAfterMaxSteps: () => void
  continuingAfterMaxSteps: boolean
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const messages = buildMessages(run.events, language)
  const initialTaskCandidate = (
    run.task
    || run.events.find((event) => event.kind === 'task' && event.subagent == null)?.task
    || ''
  ).trim()
  const initialTask = initialTaskCandidate === 'Solve the modeling problem.'
    ? ''
    : initialTaskCandidate
  const initialFiles = run.files ?? []
  const initialCopyText = [
    initialTask,
    initialFiles.length > 0
      ? `${en ? 'Materials' : '材料'}: ${initialFiles.join(en ? ', ' : '、')}`
      : '',
  ].filter(Boolean).join('\n')
  const maxStepsStopped = stoppedByMainAgentMaxSteps(run)
  const canAddSteps = !run.agent_settings
    || run.agent_settings.max_steps + 40 <= run.agent_settings.max_allowed_steps
  const pendingQuestionIsInTimeline = run.pending_question != null && messages.some(
    (message) => message.kind === 'question' && message.question?.id === run.pending_question?.id,
  )
  let latestStoppedMessage = -1
  messages.forEach((message, index) => {
    if (message.kind === 'stopped') latestStoppedMessage = index
  })
  if (messages.length === 0 && !initialTask && initialFiles.length === 0) {
    const emptyIcon = run.status === 'draft'
      ? <DraftLoadingRing />
      : run.status === 'running'
        ? <SequentialLoadingRing className="is-empty-state" />
        : <span>◌</span>
    return <div className="timeline-empty">
      {emptyIcon}
      <strong>{run.status === 'draft'
        ? (en ? 'New conversation created' : '新的会话已创建')
        : run.status === 'running'
          ? (en ? 'Agent is preparing' : 'Agent 正在准备')
          : (en ? 'No conversation activity yet' : '尚未记录会话过程')}</strong>
      <p>{run.status === 'draft'
        ? (en ? 'Describe the problem or add materials below, then start the conversation.' : '在底部描述问题或添加材料，然后开始会话。')
        : (en ? 'New steps, tool calls, and results will appear here.' : '新的步骤、工具调用与结果会在这里持续出现。')}</p>
    </div>
  }
  return (
    <section className="timeline">
      {(initialTask || initialFiles.length > 0) && (
        <article className="task-brief copyable-message user-copyable">
          <span>{en ? 'Your task' : '你的任务'}</span>
          {initialTask && <p>{initialTask}</p>}
          {initialFiles.length > 0 && (
            <div className="task-files" aria-label={en ? 'Initial materials' : '首轮材料'}>
              {initialFiles.map((file) => <small key={file}>{file}</small>)}
            </div>
          )}
          <div className="message-actions">
            <CopyMessageButton
              text={initialCopyText}
              idleLabel={initialTask
                ? (en ? 'Copy task' : '复制任务')
                : (en ? 'Copy material list' : '复制材料列表')}
            />
          </div>
        </article>
      )}
      {(run.status === 'error' || run.status === 'stopped' || run.status === 'cancelled') && !maxStepsStopped && (
        <article className="run-recovery-notice">
          <strong>
            {run.status === 'error'
              ? (en ? 'This conversation stopped with an error' : '本轮会话已异常停止')
              : run.status === 'cancelled'
                ? (en ? 'This conversation was interrupted' : '本轮会话已被中断')
                : (en ? 'This conversation has stopped' : '本轮会话已停止')}
          </strong>
          <p>
            {run.status === 'cancelled'
              ? (en
                ? 'You stopped this conversation. Existing plans, results, and materials are preserved, and you can restart with a clean run.'
                : '你手动停止了这次会话。已生成的计划、结果和素材都还保留着，可以重新开始一轮干净的会话。')
              : (run.failure_reason || (en
                ? 'This task stopped producing new activity. You can restart it with a clean run.'
                : '当前任务没有继续产生新的运行记录。你可以重新开始一轮干净的会话。'))}
          </p>
        </article>
      )}
      {messages.map((message, index) => {
        if (message.kind === 'question' && message.question) {
          const active = run.status === 'waiting_input'
            && run.pending_question?.id === message.question.id
          return (
            <PendingQuestionCard
              key={`question-${message.question.id}`}
              runId={run.id}
              question={message.question}
              active={active}
              resolved={message.questionResolved}
              selectedOptionId={message.selectedOptionId}
            />
          )
        }
        if (message.kind === 'assistant') return <AssistantMessage key={index} message={message} runStatus={run.status} />
        if (message.kind === 'compaction') return <div key={index} className="timeline-notice">✦ {en ? 'Context compacted' : '上下文已整理'} · {message.text}</div>
        if (message.kind === 'verification') return <VerificationNotice key={index} message={message} onOpenVerification={onOpenVerification} />
        if (message.kind === 'stopped') {
          const actionable = maxStepsStopped && index === latestStoppedMessage
          return <div key={index} className={`timeline-notice warning${actionable ? ' max-steps-notice' : ''}`}>
            <span>{en ? 'This conversation paused after reaching the maximum step count.' : '此轮会话达到最大步数，任务已暂停。'}</span>
            {actionable && (
              <button
                type="button"
                disabled={continuingAfterMaxSteps || !canAddSteps}
                onClick={onContinueAfterMaxSteps}
                title={!canAddSteps
                  ? (en ? 'The configured maximum has been reached.' : '已达到允许设置的最大步数。')
                  : undefined}
              >
                <span>{continuingAfterMaxSteps
                  ? (en ? 'Continuing…' : '正在继续…')
                  : canAddSteps
                    ? (en ? 'Continue now' : '直接继续')
                    : (en ? 'Limit reached' : '已达上限')}</span>
                {canAddSteps && <small>+40 {en ? 'steps' : '步'}</small>}
              </button>
            )}
          </div>
        }
        if (message.kind === 'user') return (
          <article key={index} className="user-followup copyable-message user-copyable">
            <p>{message.text}</p>
            <time>{clock(message.ts, language)}</time>
            {message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} /></div>}
          </article>
        )
        return <article key={index} className="final-answer copyable-message agent-copyable">
          <div className="final-answer-meta">
            <AgentPet />
            <strong>{en ? 'Agent completed' : 'Agent 已完成'}</strong>
            <time>{clock(message.ts, language)}</time>
          </div>
          {message.text && <MarkdownContent content={message.text} />}
          {message.text?.trim() && <div className="message-actions"><CopyMessageButton text={message.text} idleLabel={en ? 'Copy response' : '复制回复'} /></div>}
        </article>
      })}
      {run.status === 'waiting_input' && run.pending_question && !pendingQuestionIsInTimeline && (
        <PendingQuestionCard
          runId={run.id}
          question={run.pending_question}
          active
        />
      )}
      {run.status === 'running' && <div className="thinking-indicator"><AgentPet isThinking /><span>{en ? 'Agent is thinking' : 'Agent 正在思考'}</span></div>}
    </section>
  )
}
