import { useMemo } from 'react'
import type {
  AgentEvent,
  RevisionDeliverables,
  RunDetail,
  VerificationIssue,
} from '../types'
import { api } from '../api'
import { clock, compactNumber, eventToolPreview } from '../helpers'
import { type Language, useLanguage } from '../i18n'
import { MarkdownContent } from './MarkdownContent'

export type InspectorTab = '计划' | '材料' | '关键决策' | '验证' | '交付物'
const tabs: InspectorTab[] = ['计划', '材料', '关键决策', '验证', '交付物']
const tabLabels: Record<Language, Record<InspectorTab, string>> = {
  en: {
    计划: 'Plan',
    材料: 'Materials',
    关键决策: 'Decisions',
    验证: 'Verification',
    交付物: 'Outputs',
  },
  zh: {
    计划: '计划',
    材料: '材料',
    关键决策: '关键决策',
    验证: '验证',
    交付物: '交付物',
  },
}

function Plan({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  if (run.plan_tasks.length === 0) return <MarkdownContent className="artifact-markdown" content={run.plan || (en ? 'No execution plan yet.' : '还没有生成执行计划。')} />
  const done = run.plan_tasks.filter((task) => task.status === 'done').length
  return <div className="inspector-section">
    <div className="plan-progress"><span>{en ? 'Progress' : '执行进度'}</span><strong>{done}/{run.plan_tasks.length}</strong></div>
    <div className="progress-line"><i style={{ width: `${(done / run.plan_tasks.length) * 100}%` }} /></div>
    {run.plan_tasks.map((task) => <article className={`plan-card ${task.status}`} key={task.id}><span>{task.status === 'done' ? '✓' : task.status === 'in_progress' ? '◐' : task.status === 'blocked' ? '!' : '○'}</span><div><code>{task.id}</code><strong>{task.title}</strong>{(task.result || task.note) && <p>{task.result || task.note}</p>}</div></article>)}
  </div>
}

function Materials({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  return <div className="material-view"><section><h3>{en ? 'Problem and data' : '问题与数据说明'}</h3><MarkdownContent className="artifact-markdown" content={run.problem || (en ? 'problem.md has not been generated yet.' : '尚未生成 problem.md')} /></section></div>
}

function Decisions({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  if (!run.decisions.trim()) return <div className="inspector-empty">{en ? 'The Agent has not recorded any key decisions yet.' : 'Agent 还没有记录关键决策。'}</div>
  const count = run.decisions.split('\n').filter((line) => /^-\s+\[/.test(line.trim())).length
  return <div className="decision-view">
    <header>
      <div><span className="decision-mark">◆</span><strong>{en ? 'Key decisions' : '关键决策记录'}</strong></div>
      {count > 0 && <small>{en ? `${count} items` : `${count} 项`}</small>}
    </header>
    <p className="decision-description">{en
      ? 'Important assumptions, method choices, and their rationale are collected here.'
      : '这里集中展示 Agent 在建模过程中采用的重要假设、方法选择及其理由。'}</p>
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

function groupVerificationProgress(events: AgentEvent[], language: Language): VerificationProgressGroup[] {
  const en = language === 'en'
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
        title: event.scope_title || event.scope_id || (en ? 'Focused verification' : '专项验证'),
        description: `${en ? 'Verification sub-agent' : '验证 subagent'}${event.scope_id ? ` · ${event.scope_id}` : ''}`,
        kind: 'subagent',
        events: [],
      } : isPreflight ? {
        id,
        title: en ? 'Deterministic preflight' : '确定性预检',
        description: en ? 'Hard checks for pages, structure, labels, and deliverables' : '页数、结构、标签与交付物硬校验',
        kind: 'lead',
        events: [],
      } : {
        id,
        title: isSingleVerifier
          ? (en ? 'Verification Agent' : '验证 Agent')
          : (en ? 'Lead Verification Agent' : '主验证 Agent'),
        description: isSingleVerifier
          ? (en ? 'Independent full review and final verdict' : '独立通读、检查并统一裁决')
          : (en ? 'Risk triage and final synthesis' : '风险拆解与最终汇总'),
        kind: 'lead',
        events: [],
      }
      groups.set(id, group)
    }
    group.events.push(event)
  }
  return [...groups.values()]
}

const severityLabel: Record<Language, Record<VerificationIssue['severity'], string>> = {
  en: { critical: 'Critical', major: 'Major', minor: 'Minor' },
  zh: { critical: '严重', major: '主要', minor: '次要' },
}

const categoryLabel: Record<Language, Record<string, string>> = {
  en: {
    'verification-protocol': 'Verification protocol',
    input: 'Problem materials',
    evidence: 'Computational evidence',
    reproducibility: 'Reproducibility',
    coverage: 'Task coverage',
    deliverable: 'Deliverables',
    'paper-length': 'Paper length',
    'paper-layout': 'Paper layout',
    'paper-format': 'Paper format',
    'paper-structure': 'Paper structure',
    'abstract-layout': 'Abstract layout',
    'abstract-content': 'Abstract content',
    'model-formulation': 'Model formulation',
    'figure-language': 'Figure language',
  },
  zh: {
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
  },
}

function progressLabel(event: AgentEvent, language: Language): string {
  const en = language === 'en'
  if (event.phase === 'preflight_blocked') {
    return en
      ? `Preflight found critical structural issues and returned the candidate${event.issue_count ? ` · ${event.issue_count} issues` : ''}`
      : `预检发现关键结构问题，已直接退回${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}`
  }
  if (event.phase === 'single_check_start') return en ? 'Started an independent full review of the candidate' : '开始独立通读并验证完整候选结果'
  if (event.phase === 'single_check_complete') {
    const verdict = event.verdict === 'PASS' ? (en ? 'Passed' : '通过') : (en ? 'Issues found' : '发现问题')
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return en
      ? `Verification complete · ${verdict}${event.issue_count ? ` · ${event.issue_count} issues` : ''}${usage}`
      : `验证完成 · ${verdict}${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}${usage}`
  }
  if (event.phase === 'triage_start') return en ? 'Reviewing the full candidate for high-risk areas' : '通读全文并识别高风险位置'
  if (event.phase === 'triage_complete') {
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return en
      ? `Verification triage complete${event.scope_count ? ` · ${event.scope_count} scopes` : ''}${usage}`
      : `完成验证拆解${event.scope_count ? ` · ${event.scope_count} 个专项` : ''}${usage}`
  }
  if (event.phase === 'subcheck_start') return en ? 'Started focused check' : '开始专项检查'
  if (event.phase === 'subcheck_complete') {
    const verdict = event.verdict === 'PASS'
      ? (en ? 'Passed' : '通过')
      : event.verdict === 'REVISE'
        ? (en ? 'Issues found' : '发现问题')
        : (en ? 'No verdict' : '未形成结论')
    const usage = event.total_tokens != null ? ` · ${compactNumber(event.total_tokens)} tokens` : ''
    return en
      ? `Focused check complete · ${verdict}${event.issue_count ? ` · ${event.issue_count} issues` : ''}${usage}`
      : `专项检查完成 · ${verdict}${event.issue_count ? ` · ${event.issue_count} 个问题` : ''}${usage}`
  }
  if (event.phase === 'synthesis_start') return en ? 'Synthesizing focused results into a unified verdict' : '收齐专项结果并开始统一裁决'
  if (event.phase === 'task') return en ? 'Received verification task and acceptance criteria' : '接收验证任务与验收标准'
  if (event.phase === 'context') return en ? `Prepared verification context for step ${event.step ?? '—'}` : `整理第 ${event.step ?? '—'} 步验证上下文`
  if (event.phase === 'assistant') {
    const tools = (event.tool_calls ?? []).map(([name]) => name)
    return tools.length > 0
      ? (en ? `Preparing: ${tools.join(', ')}` : `准备执行：${tools.join('、')}`)
      : (event.text || (en ? 'Analyzing available evidence' : '分析已有证据'))
  }
  if (event.phase === 'tool_result') {
    if (!event.name && !event.observation) return en ? 'Completed a tool check' : '完成一项工具检查'
    const result = eventToolPreview(event, language)
    return `${event.name
      ? (en ? `Completed ${event.name}` : `完成 ${event.name}`)
      : (en ? 'Tool check complete' : '工具检查完成')}${event.observation ? ` · ${result}` : ''}`
  }
  if (event.phase === 'done') return en ? 'Created and submitted verification verdict' : '形成并提交验证结论'
  if (event.phase === 'finalization_required') return en ? 'Structured verification verdict required' : '要求提交结构化验证结论'
  if (event.phase === 'finalization_recovery_start') return en ? 'Switched to isolated verdict submission' : '切换到独立的结论提交阶段'
  if (event.phase === 'finalization_recovery_attempt') return en ? 'Submitting structured verdict with clean context' : '使用干净上下文提交结构化结论'
  if (event.phase === 'finalization_recovery_complete') return en ? 'Isolated verdict submission succeeded' : '独立结论提交成功'
  if (event.phase === 'finalization_recovery_error') return `${en ? 'Verdict submission error' : '结论提交异常'}${event.observation ? ` · ${eventToolPreview(event, language)}` : ''}`
  if (event.phase === 'finalization_recovery_failed') return en ? 'Structured submission failed; existing checks were preserved' : '结构化提交失败，保留已有检查记录'
  if (event.phase === 'max_steps') return en ? 'Verification reached the maximum step count' : '验证达到最大检查步数'
  return event.phase ? (en ? `Running ${event.phase}` : `执行 ${event.phase}`) : (en ? 'Running verification checks' : '执行验证检查')
}

function VerificationProgress({ events, expanded }: { events: AgentEvent[]; expanded: boolean }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const groups = groupVerificationProgress(events, language)
  const totalSteps = groups.reduce(
    (sum, group) => sum + verificationStepCount(group.events),
    0,
  )
  return <details className="verification-progress-disclosure" open={expanded}>
    <summary>
      <span>{en ? 'Check steps' : '检查步骤'}</span>
      <b>{en ? `${groups.length} agents` : `${groups.length} 个 Agent`}</b>
      <small>{en ? `${totalSteps} steps` : `${totalSteps} 步`}</small>
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
            <b>{en ? `${verificationStepCount(group.events)} steps` : `${verificationStepCount(group.events)} 步`}</b>
            <i>⌄</i>
          </summary>
          <ol className="verification-progress">
            {group.events.map((event, progressIndex) => <li key={`${event.ts ?? progressIndex}-${progressIndex}`}><span />{progressLabel(event, language)}</li>)}
          </ol>
          {completed?.summary && <div className={`verification-agent-conclusion ${status}`}>
            <span>{en ? 'Conclusion' : '结论'}</span>
            <MarkdownContent content={completed.summary} normalizeJoinedHeadings />
          </div>}
        </details>
      })}
    </div>
  </details>
}

function Verification({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const attempts = useMemo(() => collectVerificationAttempts(run.events), [run.events])
  const latest = attempts.at(-1)
  const issueCount = latest?.issues.length ?? 0
  const totalTokens = attempts.reduce((sum, attempt) => sum + (attempt.tokens ?? 0), 0)
  return <div className="verification-view">
    {attempts.length === 0 ? <div className="inspector-empty">{en ? 'Verification activity will appear here after the final candidate is submitted.' : '最终候选结果提交后，验证 Agent 的检查过程会显示在这里。'}</div> : <><header className={`verification-overview ${latest?.status ?? 'checking'}`}>
      <span>{latest?.status === 'passed' ? '✓' : latest?.status === 'rejected' ? '!' : '◌'}</span>
      <div>
        <strong>{latest?.status === 'passed'
          ? (en ? 'Independent verification passed' : '独立验证已通过')
          : latest?.status === 'rejected'
            ? (en ? 'Independent verification failed' : '独立验证未通过')
            : (en ? 'Independent verification in progress' : '独立验证进行中')}</strong>
        <small>{en ? `${attempts.length} attempts` : `${attempts.length} 轮验证`}{totalTokens > 0 ? (en ? ` · ${compactNumber(totalTokens)} tokens total` : ` · 共 ${compactNumber(totalTokens)} tokens`) : ''}{issueCount > 0 ? (en ? ` · ${issueCount} unresolved issues` : ` · ${issueCount} 个待解决问题`) : ''}</small>
      </div>
    </header>
    <div className="verification-attempts">
      {attempts.map((attempt, index) => {
        const protocolIssues = attempt.issues.filter((issue) => issue.category === 'verification-protocol')
        const acceptanceIssues = attempt.issues.filter((issue) => issue.category !== 'verification-protocol')
        return <details className={`verification-attempt ${attempt.status}`} key={attempt.id} open={index === attempts.length - 1}>
          <summary>
            <span>{attempt.status === 'passed' ? '✓' : attempt.status === 'rejected' ? '!' : '◌'}</span>
            <strong>{en ? `Verification attempt ${attempt.attempt}` : `第 ${attempt.attempt} 轮验证`}</strong>
            <small>{[attempt.startedAt ? clock(attempt.startedAt, language) : '', attempt.tokens != null ? `${compactNumber(attempt.tokens)} tokens` : ''].filter(Boolean).join(' · ')}</small>
            <b>{attempt.status === 'passed'
              ? (en ? 'Passed' : '通过')
              : attempt.status === 'rejected'
                ? (en ? 'Failed' : '未通过')
                : (en ? 'Checking' : '检查中')}</b>
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
              <h3>{en ? 'Verification process' : '验证流程'}</h3>
              <div className="verification-issues">{protocolIssues.map((issue, issueIndex) => <VerificationIssueCard issue={issue} key={`${issue.category}-${issueIndex}`} />)}</div>
            </section>}
            {acceptanceIssues.length > 0 && <section>
              <h3>{en ? 'Result acceptance' : '结果验收'}</h3>
              <div className="verification-issues">{acceptanceIssues.map((issue, issueIndex) => <VerificationIssueCard issue={issue} key={`${issue.category}-${issueIndex}`} />)}</div>
            </section>}
            {attempt.status === 'passed' && attempt.issues.length === 0 && <div className="verification-pass-note">{en ? 'All checks passed. There are no unresolved changes.' : '所有检查项均已通过，没有待修改问题。'}</div>}
          </div>
        </details>
      })}
    </div></>}
  </div>
}

function VerificationIssueCard({ issue }: { issue: VerificationIssue }) {
  const { language } = useLanguage()
  const en = language === 'en'
  return <article className={`verification-issue ${issue.severity}`}>
    <header><span>{severityLabel[language][issue.severity]}</span><small>{categoryLabel[language][issue.category] ?? issue.category}</small></header>
    <MarkdownContent
      className="verification-issue-message"
      content={issue.message}
      normalizeJoinedHeadings
    />
    <dl>
      <div><dt>{en ? 'Evidence' : '证据'}</dt><dd><MarkdownContent content={issue.evidence} normalizeJoinedHeadings /></dd></div>
      <div><dt>{en ? 'Required change' : '需要修改'}</dt><dd><MarkdownContent content={issue.required_fix} normalizeJoinedHeadings /></dd></div>
    </dl>
  </article>
}

function revisionStatusLabel(status: string, en: boolean): string {
  const labels: Record<string, [string, string]> = {
    draft: ['草稿', 'Draft'],
    running: ['计算中', 'Computing'],
    waiting_input: ['等待确认', 'Waiting'],
    verified: ['已验证', 'Verified'],
    completed: ['已完成', 'Completed'],
    failed: ['失败', 'Failed'],
    cancelled: ['已取消', 'Cancelled'],
    stopped: ['已暂停', 'Stopped'],
  }
  const label = labels[status]
  return label ? label[en ? 1 : 0] : status
}

function revisionRoleLabel(revision: RevisionDeliverables, en: boolean): string {
  if (revision.is_current && revision.is_active) return en ? 'Current' : '当前版本'
  if (revision.is_current) return en ? 'Current stable' : '当前稳定版'
  if (revision.is_active) return en ? 'Working' : '修订中'
  return en ? 'History' : '历史版本'
}

function RevisionDeliveryCard({
  revision,
  en,
  download,
}: {
  revision: RevisionDeliverables
  en: boolean
  download: (path: string) => string
}) {
  const hasPaper = Boolean(revision.paper.pdf || revision.paper.tex)
  const hasCode = revision.source_files.length > 0
  return <article className={`delivery-revision-card ${revision.is_current ? 'current' : ''} ${revision.is_active && !revision.is_current ? 'active' : ''}`}>
    <header>
      <span className="delivery-round"><b>V{revision.number}</b>{en ? `Round ${revision.number}` : `第 ${revision.number} 轮`}</span>
      <div>
        <strong>{revision.title}</strong>
        {revision.summary && <small>{revision.summary}</small>}
      </div>
      <span className="delivery-revision-state">{revisionRoleLabel(revision, en)} · {revisionStatusLabel(revision.status, en)}</span>
    </header>
    {hasPaper && <section>
      <h4>{en ? 'Paper' : '论文'}</h4>
      <div className="download-grid">
        {revision.paper.pdf && <a href={download(revision.paper.pdf)} download={revision.paper.pdf_name ?? `paper-v${revision.number}.pdf`} title={revision.paper.pdf_name}>{en ? `Round ${revision.number} PDF` : `第 ${revision.number} 轮 PDF 论文`} <b>↓</b></a>}
        {revision.paper.tex && <a href={download(revision.paper.tex)} download={revision.paper.tex_name ?? `paper-v${revision.number}.tex`} title={revision.paper.tex_name}>{en ? `Round ${revision.number} LaTeX` : `第 ${revision.number} 轮 LaTeX 源码`} <b>↓</b></a>}
      </div>
    </section>}
    {hasCode && <section className="source-deliverables">
      <h4>{en ? 'Code' : '代码'}</h4>
      {revision.source_files.map((file) => <a className="file-link source-file-link" key={file.path} href={download(file.path)} download title={file.path}><span><i>{file.name.split('.').pop()?.toUpperCase()}</i>{file.name}</span><small>{file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`}</small><b>↓</b></a>)}
    </section>}
    {!hasPaper && !hasCode && <p className="delivery-revision-empty">{en ? 'No paper or code has been produced for this round yet.' : '这一轮还没有生成论文或代码。'}</p>}
  </article>
}

function Deliverables({ run }: { run: RunDetail }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const entries = Object.entries(run.results)
  const currentRevision = run.project.current_revision
    ?? run.project.revisions.find((revision) => revision.id === run.project.current_revision_id)
  const revisionDeliveries = run.project.deliverable_revisions ?? [{
    revision_id: currentRevision?.id ?? 'rev_0001',
    number: currentRevision?.number ?? 1,
    title: currentRevision?.title ?? (en ? 'Current delivery' : '当前交付'),
    summary: currentRevision?.summary ?? '',
    status: currentRevision?.status ?? run.status,
    is_current: true,
    is_active: run.project.current_revision_id === run.project.active_revision_id,
    paper: run.paper,
    source_files: run.source_files ?? [],
  }]
  const hasRevisionFiles = revisionDeliveries.some((revision) => (
    revision.paper.pdf || revision.paper.tex || revision.source_files.length > 0
  ))
  const download = (path: string) => api.fileUrl(run.id, path)
  if (!hasRevisionFiles && run.outputs.length === 0 && entries.length === 0 && run.figures.length === 0) return <div className="inspector-empty">{en ? 'Result files will appear here when the conversation is complete.' : '会话完成后，结果文件会集中在这里。'}</div>
  return <div className="deliverables">
    {hasRevisionFiles && <section className="revision-deliverables">
      <div className="deliverable-section-heading"><h3>{en ? 'Paper and code by round' : '各轮论文与代码'}</h3><span>{revisionDeliveries.length}</span></div>
      <p className="deliverable-section-description">{en ? 'Each round keeps its own downloadable paper and source files.' : '每一轮的论文与代码分别保存，可直接下载对应版本。'}</p>
      <div className="delivery-revision-list">{revisionDeliveries.map((revision) => <RevisionDeliveryCard key={revision.revision_id} revision={revision} en={en} download={download} />)}</div>
    </section>}
    {run.outputs.length > 0 && <section><h3>{en ? 'Data files' : '数据文件'}</h3>{run.outputs.map((name) => <a className="file-link" key={name} href={download(name)} download>▧ {name}<b>↓</b></a>)}</section>}
    {entries.length > 0 && <section><h3>{en ? 'Results' : '计算结果'}</h3>{entries.map(([name, content]) => <details className="result-file" key={name}><summary>⌄ {name}</summary><pre>{content}</pre></details>)}</section>}
    {run.figures.length > 0 && <section><h3>{en ? 'Figures' : '图表'}</h3>{run.figures.map((name) => <img key={name} src={download(`figures/${name}`)} alt={name} />)}</section>}
  </div>
}

export function Inspector({ run, tab, onTabChange }: { run: RunDetail; tab: InspectorTab; onTabChange: (tab: InspectorTab) => void }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const hasVerification = run.events.some((event) => event.kind.startsWith('verification_'))
  return <aside className="inspector">
    <div className="tab-list" role="tablist" aria-label={en ? 'Project information' : '项目信息'}>
      {tabs.map((item) => <button
        key={item}
        id={`inspector-tab-${item}`}
        role="tab"
        aria-selected={tab === item}
        aria-controls="inspector-tab-panel"
        className={tab === item ? 'active' : ''}
        onClick={() => onTabChange(item)}
      >
        {tabLabels[language][item]}
        {item === '关键决策' && run.decisions.trim() && <span className="tab-content-dot" aria-label={en ? 'Content available' : '已有记录'} />}
        {item === '验证' && hasVerification && <span className="tab-content-dot verification-dot" aria-label={en ? 'Verification records available' : '已有验证记录'} />}
      </button>)}
    </div>
    <div
      className="tab-panel"
      id="inspector-tab-panel"
      role="tabpanel"
      aria-labelledby={`inspector-tab-${tab}`}
    >
      <div className="tab-panel-content" key={tab}>
        {tab === '计划' && <Plan run={run} />}
        {tab === '材料' && <Materials run={run} />}
        {tab === '关键决策' && <Decisions run={run} />}
        {tab === '验证' && <Verification run={run} />}
        {tab === '交付物' && <Deliverables run={run} />}
      </div>
    </div>
  </aside>
}
