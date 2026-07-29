import { useMemo } from 'react'
import type {
  AgentEvent,
  RunDetail,
  VerificationIssue,
} from '../types'
import { api } from '../api'
import { clock, compactNumber, eventToolPreview } from '../helpers'
import { MarkdownContent } from './MarkdownContent'

export type InspectorTab = '协作' | '计划' | '材料' | '关键决策' | '验证' | '交付物'
const tabs: InspectorTab[] = ['协作', '计划', '材料', '关键决策', '验证', '交付物']

interface Subagent {
  id: number
  task: string
  steps: AgentEvent[]
  tokens?: number
}

function collectSubagents(events: AgentEvent[]): Subagent[] {
  const byId = new Map<number, Subagent>()
  for (const event of events) {
    if (event.kind === 'subagent_start' && event.subagent != null) byId.set(event.subagent, { id: event.subagent, task: event.task ?? '', steps: [] })
    else if (event.kind === 'subagent_end' && event.subagent != null) {
      const existing = byId.get(event.subagent)
      if (existing) existing.tokens = event.tokens
    } else if (event.subagent != null) byId.get(event.subagent)?.steps.push(event)
  }
  return [...byId.values()]
}

function Collaboration({ run }: { run: RunDetail }) {
  const subagents = useMemo(() => collectSubagents(run.events), [run.events])
  if (subagents.length === 0) return <div className="inspector-empty">主 Agent 还没有调用协作 Agent。</div>
  const active = subagents.filter((item) => item.tokens == null).length
  return <div className="inspector-section">
    <div className="subagent-summary"><span><b>{active}</b> 正在运行</span><span><b>{subagents.length - active}</b> 已完成</span></div>
    {subagents.map((agent) => (
      <details className="subagent-card" key={agent.id} open={agent.tokens == null}>
        <summary><span className={agent.tokens == null ? 'tiny-pulse' : 'tiny-dot'} /><code>SUB-{agent.id}</code><strong>{agent.tokens == null ? '执行中' : '已返回'}</strong><span>⌄</span></summary>
        <p>{agent.task}</p>
        <div className="subagent-steps">
          {agent.steps.filter((event) => event.kind === 'assistant').slice(-4).map((event, index) => <div key={index}><small>第 {event.step ?? '—'} 步</small>{event.text || '正在调用工具…'}</div>)}
        </div>
        <footer>{agent.tokens == null ? '正在更新中…' : `消耗 ${agent.tokens.toLocaleString()} tokens`}</footer>
      </details>
    ))}
  </div>
}

function Plan({ run }: { run: RunDetail }) {
  if (run.plan_tasks.length === 0) return <MarkdownContent className="artifact-markdown" content={run.plan || '还没有生成执行计划。'} />
  const done = run.plan_tasks.filter((task) => task.status === 'done').length
  return <div className="inspector-section">
    <div className="plan-progress"><span>执行进度</span><strong>{done}/{run.plan_tasks.length}</strong></div>
    <div className="progress-line"><i style={{ width: `${(done / run.plan_tasks.length) * 100}%` }} /></div>
    {run.plan_tasks.map((task) => <article className={`plan-card ${task.status}`} key={task.id}><span>{task.status === 'done' ? '✓' : task.status === 'in_progress' ? '◐' : task.status === 'blocked' ? '!' : '○'}</span><div><code>{task.id}</code><strong>{task.title}</strong>{(task.result || task.note) && <p>{task.result || task.note}</p>}</div></article>)}
  </div>
}

function Materials({ run }: { run: RunDetail }) {
  return <div className="material-view"><section><h3>问题与数据说明</h3><MarkdownContent className="artifact-markdown" content={run.problem || '尚未生成 problem.md'} /></section></div>
}

function Decisions({ run }: { run: RunDetail }) {
  if (!run.decisions.trim()) return <div className="inspector-empty">Agent 还没有记录关键决策。</div>
  const count = run.decisions.split('\n').filter((line) => /^-\s+\[/.test(line.trim())).length
  return <div className="decision-view">
    <header>
      <div><span className="decision-mark">◆</span><strong>关键决策记录</strong></div>
      {count > 0 && <small>{count} 项</small>}
    </header>
    <p className="decision-description">这里集中展示 Agent 在建模过程中采用的重要假设、方法选择及其理由。</p>
    <MarkdownContent className="artifact-markdown decision-markdown" content={run.decisions} />
  </div>
}

interface VerificationAttempt {
  id: string
  attempt: number
  sourceAttempt: number
  startedAt?: number
  status: 'checking' | 'passed' | 'rejected'
  summary?: string
  issues: VerificationIssue[]
  progress: AgentEvent[]
  tokens?: number
}

interface VerificationProgressGroup {
  id: string
  title: string
  description: string
  kind: 'lead' | 'subagent'
  events: AgentEvent[]
}

function verificationStepCount(events: AgentEvent[]): number {
  const numberedSteps = new Set<string>()
  for (const event of events) {
    if (event.step == null) continue
    numberedSteps.add([
      event.role ?? 'verifier',
      event.scope_id ?? '',
      event.step,
    ].join(':'))
  }
  return numberedSteps.size
}

function collectVerificationAttempts(events: AgentEvent[]): VerificationAttempt[] {
  const attempts: VerificationAttempt[] = []
  const activeBySourceAttempt = new Map<number, VerificationAttempt>()
  const create = (sourceAttempt: number) => {
    const created: VerificationAttempt = {
      id: `verification-${attempts.length + 1}`,
      attempt: attempts.length + 1,
      sourceAttempt,
      status: 'checking',
      issues: [],
      progress: [],
    }
    attempts.push(created)
    activeBySourceAttempt.set(sourceAttempt, created)
    return created
  }
  const ensure = (sourceAttempt: number) => {
    const existing = activeBySourceAttempt.get(sourceAttempt)
    if (existing) return existing
    return create(sourceAttempt)
  }

  for (const event of events) {
    if (!['verification_start', 'verification_progress', 'verification_result', 'verification_failed'].includes(event.kind)) continue
    const sourceAttempt = event.attempt ?? Math.max(1, attempts.length)
    if (event.kind === 'verification_start') {
      // A later follow-up can legitimately start its own attempt=1 after an
      // earlier verification cycle passed. Always create a new display round
      // instead of merging it into the historical attempt with the same raw id.
      const item = create(sourceAttempt)
      item.startedAt = event.ts
      item.status = 'checking'
    } else if (event.kind === 'verification_progress') {
      const item = ensure(sourceAttempt)
      item.progress.push(event)
    } else {
      const item = ensure(sourceAttempt)
      item.status = event.verdict === 'PASS' ? 'passed' : 'rejected'
      item.summary = event.summary
      if (event.issues) item.issues = event.issues
      item.tokens = event.verification_usage?.reported_total_tokens
      activeBySourceAttempt.delete(sourceAttempt)
    }
  }
  return attempts
}

function groupVerificationProgress(events: AgentEvent[]): VerificationProgressGroup[] {
  const groups = new Map<string, VerificationProgressGroup>()
  for (const event of events) {
    const isSubagent = event.role === 'subagent' && Boolean(event.scope_id)
    const isPreflight = event.role === 'deterministic-preflight'
    const isSingleVerifier = event.role === 'verifier'
    const id = isSubagent ? `scope:${event.scope_id}` : isPreflight ? 'preflight' : 'lead'
    let group = groups.get(id)
    if (!group) {
      group = isSubagent ? {
        id,
        title: event.scope_title || event.scope_id || '专项验证',
        description: `验证 subagent${event.scope_id ? ` · ${event.scope_id}` : ''}`,
        kind: 'subagent',
        events: [],
      } : isPreflight ? {
        id,
        title: '确定性预检',
        description: '页数、结构、标签与交付物硬校验',
        kind: 'lead',
        events: [],
      } : {
        id,
        title: isSingleVerifier ? '验证 Agent' : '主验证 Agent',
        description: isSingleVerifier ? '独立通读、检查并统一裁决' : '风险拆解与最终汇总',
        kind: 'lead',
        events: [],
      }
      groups.set(id, group)
    }
    group.events.push(event)
  }
  return [...groups.values()]
}

const severityLabel: Record<VerificationIssue['severity'], string> = {
  critical: '严重',
  major: '主要',
  minor: '次要',
}

const categoryLabel: Record<string, string> = {
  'verification-protocol': '验证流程',
  input: '题目材料',
  evidence: '计算证据',
  reproducibility: '可复现性',
  coverage: '任务覆盖',
  deliverable: '交付物',
  'paper-length': '论文页数',
  'paper-layout': '论文版式',
  'paper-format': '论文格式',
  'paper-structure': '论文结构',
  'abstract-layout': '摘要版式',
  'abstract-content': '摘要内容',
  'model-formulation': '模型建立',
  'figure-language': '图表语言',
}

function progressLabel(event: AgentEvent): string {
  if (event.phase === 'preflight_blocked') {
    return `预检发现关键结构问题，已直接退回${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}`
  }
  if (event.phase === 'single_check_start') return '开始独立通读并验证完整候选结果'
  if (event.phase === 'single_check_complete') {
    const verdict = event.verdict === 'PASS' ? '通过' : '发现问题'
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return `验证完成 · ${verdict}${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}${usage}`
  }
  if (event.phase === 'triage_start') return '通读全文并识别高风险位置'
  if (event.phase === 'triage_complete') {
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return `完成验证拆解${event.scope_count ? ` · ${event.scope_count} 个专项` : ''}${usage}`
  }
  if (event.phase === 'subcheck_start') return '开始专项检查'
  if (event.phase === 'subcheck_complete') {
    const verdict = event.verdict === 'PASS' ? '通过' : event.verdict === 'REVISE' ? '发现问题' : '未形成结论'
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return `专项检查完成 · ${verdict}${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}${usage}`
  }
  if (event.phase === 'synthesis_start') return '收齐专项结果并开始统一裁决'
  if (event.phase === 'task') return '接收验证任务与验收标准'
  if (event.phase === 'context') return `整理第 ${event.step ?? '—'} 步验证上下文`
  if (event.phase === 'assistant') {
    const tools = (event.tool_calls ?? []).map(([name]) => name)
    return tools.length > 0 ? `准备执行：${tools.join('、')}` : (event.text || '分析已有证据')
  }
  if (event.phase === 'tool_result') {
    if (!event.name && !event.observation) return '完成一项工具检查'
    const result = eventToolPreview(event)
    return `${event.name ? `完成 ${event.name}` : '工具检查完成'}${event.observation ? ` · ${result}` : ''}`
  }
  if (event.phase === 'done') return '形成并提交验证结论'
  if (event.phase === 'finalization_required') return '要求提交结构化验证结论'
  if (event.phase === 'finalization_recovery_start') return '切换到独立的结论提交阶段'
  if (event.phase === 'finalization_recovery_attempt') return '使用干净上下文提交结构化结论'
  if (event.phase === 'finalization_recovery_complete') return '独立结论提交成功'
  if (event.phase === 'finalization_recovery_error') return `结论提交异常${event.observation ? ` · ${eventToolPreview(event)}` : ''}`
  if (event.phase === 'finalization_recovery_failed') return '结构化提交失败，保留已有检查记录'
  if (event.phase === 'max_steps') return '验证达到最大检查步数'
  return event.phase ? `执行 ${event.phase}` : '执行验证检查'
}

function VerificationProgress({ events, expanded }: { events: AgentEvent[]; expanded: boolean }) {
  const groups = groupVerificationProgress(events)
  const totalSteps = groups.reduce(
    (sum, group) => sum + verificationStepCount(group.events),
    0,
  )
  return <details className="verification-progress-disclosure" open={expanded}>
    <summary>
      <span>检查步骤</span>
      <b>{groups.length} 个 Agent</b>
      <small>{totalSteps} 步</small>
      <i>⌄</i>
    </summary>
    <div className="verification-agent-groups">
      {groups.map((group) => {
        const completed = [...group.events].reverse().find(
          (event) => event.phase === 'subcheck_complete' || event.phase === 'single_check_complete',
        )
        const status = completed?.verdict === 'PASS'
          ? 'passed'
          : completed?.verdict === 'REVISE'
            ? 'rejected'
            : completed?.verdict === 'INCONCLUSIVE'
              ? 'inconclusive'
              : 'checking'
        return <details className={`verification-agent-group ${group.kind} ${status}`} key={group.id}>
          <summary>
            <span>{group.kind === 'lead' ? 'V' : 'S'}</span>
            <div><strong title={group.title}>{group.title}</strong><small>{group.description}</small></div>
            <b>{verificationStepCount(group.events)} 步</b>
            <i>⌄</i>
          </summary>
          <ol className="verification-progress">
            {group.events.map((event, progressIndex) => <li key={`${event.ts ?? progressIndex}-${progressIndex}`}><span />{progressLabel(event)}</li>)}
          </ol>
          {completed?.summary && <div className={`verification-agent-conclusion ${status}`}>
            <span>结论</span>
            <MarkdownContent content={completed.summary} normalizeJoinedHeadings />
          </div>}
        </details>
      })}
    </div>
  </details>
}

function Verification({ run }: { run: RunDetail }) {
  const attempts = useMemo(() => collectVerificationAttempts(run.events), [run.events])
  const latest = attempts.at(-1)
  const issueCount = latest?.issues.length ?? 0
  const totalTokens = attempts.reduce((sum, attempt) => sum + (attempt.tokens ?? 0), 0)
  return <div className="verification-view">
    {attempts.length === 0 ? <div className="inspector-empty">最终候选结果提交后，验证 Agent 的检查过程会显示在这里。</div> : <><header className={`verification-overview ${latest?.status ?? 'checking'}`}>
      <span>{latest?.status === 'passed' ? '✓' : latest?.status === 'rejected' ? '!' : '◌'}</span>
      <div>
        <strong>{latest?.status === 'passed' ? '独立验证已通过' : latest?.status === 'rejected' ? '独立验证未通过' : '独立验证进行中'}</strong>
        <small>{attempts.length} 轮验证{totalTokens > 0 ? ` · 共 ${compactNumber(totalTokens)} tokens` : ''}{issueCount > 0 ? ` · ${issueCount} 个待解决问题` : ''}</small>
      </div>
    </header>
    <div className="verification-attempts">
      {attempts.map((attempt, index) => {
        const protocolIssues = attempt.issues.filter((issue) => issue.category === 'verification-protocol')
        const acceptanceIssues = attempt.issues.filter((issue) => issue.category !== 'verification-protocol')
        return <details className={`verification-attempt ${attempt.status}`} key={attempt.id} open={index === attempts.length - 1}>
          <summary>
            <span>{attempt.status === 'passed' ? '✓' : attempt.status === 'rejected' ? '!' : '◌'}</span>
            <strong>第 {attempt.attempt} 轮验证</strong>
            <small>{[attempt.startedAt ? clock(attempt.startedAt) : '', attempt.tokens != null ? `${compactNumber(attempt.tokens)} tokens` : ''].filter(Boolean).join(' · ')}</small>
            <b>{attempt.status === 'passed' ? '通过' : attempt.status === 'rejected' ? '未通过' : '检查中'}</b>
          </summary>
          <div className="verification-attempt-body">
            {attempt.summary && <MarkdownContent
              className="verification-summary"
              content={attempt.summary}
              normalizeJoinedHeadings
            />}
            {attempt.progress.length > 0 && <VerificationProgress
              events={attempt.progress}
              expanded={index === attempts.length - 1}
            />}
            {protocolIssues.length > 0 && <section>
              <h3>验证流程</h3>
              <div className="verification-issues">{protocolIssues.map((issue, issueIndex) => <VerificationIssueCard issue={issue} key={`${issue.category}-${issueIndex}`} />)}</div>
            </section>}
            {acceptanceIssues.length > 0 && <section>
              <h3>结果验收</h3>
              <div className="verification-issues">{acceptanceIssues.map((issue, issueIndex) => <VerificationIssueCard issue={issue} key={`${issue.category}-${issueIndex}`} />)}</div>
            </section>}
            {attempt.status === 'passed' && attempt.issues.length === 0 && <div className="verification-pass-note">所有检查项均已通过，没有待修改问题。</div>}
          </div>
        </details>
      })}
    </div></>}
  </div>
}

function VerificationIssueCard({ issue }: { issue: VerificationIssue }) {
  return <article className={`verification-issue ${issue.severity}`}>
    <header><span>{severityLabel[issue.severity]}</span><small>{categoryLabel[issue.category] ?? issue.category}</small></header>
    <MarkdownContent
      className="verification-issue-message"
      content={issue.message}
      normalizeJoinedHeadings
    />
    <dl>
      <div><dt>证据</dt><dd><MarkdownContent content={issue.evidence} normalizeJoinedHeadings /></dd></div>
      <div><dt>需要修改</dt><dd><MarkdownContent content={issue.required_fix} normalizeJoinedHeadings /></dd></div>
    </dl>
  </article>
}

function Deliverables({ run }: { run: RunDetail }) {
  const entries = Object.entries(run.results)
  const download = (path: string) => api.fileUrl(run.id, path)
  if (!run.paper.pdf && !run.paper.tex && run.outputs.length === 0 && entries.length === 0 && run.figures.length === 0) return <div className="inspector-empty">会话完成后，结果文件会集中在这里。</div>
  return <div className="deliverables">
    {(run.paper.pdf || run.paper.tex) && <section><h3>最终论文</h3><div className="download-grid">{run.paper.pdf && <a href={download(run.paper.pdf)} download={run.paper.pdf_name ?? 'paper.pdf'} title={run.paper.pdf_name}>PDF 论文 <b>↓</b></a>}{run.paper.tex && <a href={download(run.paper.tex)} download={run.paper.tex_name ?? 'paper.tex'} title={run.paper.tex_name}>LaTeX 源码 <b>↓</b></a>}</div></section>}
    {run.outputs.length > 0 && <section><h3>数据文件</h3>{run.outputs.map((name) => <a className="file-link" key={name} href={download(name)} download>▧ {name}<b>↓</b></a>)}</section>}
    {entries.length > 0 && <section><h3>计算结果</h3>{entries.map(([name, content]) => <details className="result-file" key={name}><summary>⌄ {name}</summary><pre>{content}</pre></details>)}</section>}
    {run.figures.length > 0 && <section><h3>图表</h3>{run.figures.map((name) => <img key={name} src={download(`figures/${name}`)} alt={name} />)}</section>}
  </div>
}

export function Inspector({ run, tab, onTabChange }: { run: RunDetail; tab: InspectorTab; onTabChange: (tab: InspectorTab) => void }) {
  const hasVerification = run.events.some((event) => event.kind.startsWith('verification_'))
  return <aside className="inspector">
    <div className="tab-list" role="tablist" aria-label="会话信息">
      {tabs.map((item) => <button
        key={item}
        id={`inspector-tab-${item}`}
        role="tab"
        aria-selected={tab === item}
        aria-controls="inspector-tab-panel"
        className={tab === item ? 'active' : ''}
        onClick={() => onTabChange(item)}
      >
        {item}
        {item === '关键决策' && run.decisions.trim() && <span className="tab-content-dot" aria-label="已有记录" />}
        {item === '验证' && hasVerification && <span className="tab-content-dot verification-dot" aria-label="已有验证记录" />}
      </button>)}
    </div>
    <div
      className="tab-panel"
      id="inspector-tab-panel"
      role="tabpanel"
      aria-labelledby={`inspector-tab-${tab}`}
    >
      <div className="tab-panel-content" key={tab}>
        {tab === '协作' && <Collaboration run={run} />}
        {tab === '计划' && <Plan run={run} />}
        {tab === '材料' && <Materials run={run} />}
        {tab === '关键决策' && <Decisions run={run} />}
        {tab === '验证' && <Verification run={run} />}
        {tab === '交付物' && <Deliverables run={run} />}
      </div>
    </div>
  </aside>
}
