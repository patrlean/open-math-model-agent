import {
  memo,
  startTransition,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { LanguageSwitcher, useLanguage } from '../src/i18n'
import {
  fetchCase,
  fetchAgentContexts,
  fetchContextRequest,
  fetchExperiment,
  fetchExperiments,
} from './api'
import type {
  AgentContextGroup,
  AgentEvent,
  Artifact,
  CaseDetail,
  CaseStatus,
  CaseSummary,
  ContextRequestDetail,
  ExperimentDetail,
  ExperimentStatus,
  ExperimentSummary,
} from './types'

type InspectorTab = 'overview' | 'plan' | 'decisions' | 'artifacts' | 'context' | 'logs'

const statusText: Record<ExperimentStatus | CaseStatus, { en: string; zh: string }> = {
  prepared: { en: 'Prepared', zh: '已准备' },
  queued: { en: 'Queued', zh: '排队中' },
  running: { en: 'Running', zh: '运行中' },
  orphaned: { en: 'Orphaned', zh: '孤儿运行' },
  killed: { en: 'Killed', zh: '已杀死' },
  completed: { en: 'Completed', zh: '已完成' },
  completed_with_errors: { en: 'Completed with errors', zh: '完成但有错误' },
  failed: { en: 'Failed', zh: '失败' },
  stopped: { en: 'Stopped', zh: '已停止' },
  unknown: { en: 'Unknown', zh: '未知' },
}

function statusLabel(status: ExperimentStatus | CaseStatus | undefined, language: 'en' | 'zh') {
  return statusText[status ?? 'unknown']?.[language] ?? statusText.unknown[language]
}

function caseLabel(item: CaseSummary, language: 'en' | 'zh') {
  if (!item.repetition || !item.repetitions) return item.name
  return language === 'en'
    ? `${item.name} · Run ${item.repetition}/${item.repetitions}`
    : `${item.name} · 第 ${item.repetition}/${item.repetitions} 次`
}

function formatClock(timestamp?: number | null) {
  if (!timestamp) return '—'
  return new Intl.DateTimeFormat(undefined, {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(timestamp * 1000)
}

function duration(seconds?: number | null) {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const rounded = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const rest = rounded % 60
  return [hours, minutes, rest].map((value) => String(value).padStart(2, '0')).join(':')
}

function elapsed(start?: number | null, end?: number | null) {
  if (!start) return undefined
  return Math.max(0, (end ?? Date.now() / 1000) - start)
}

function compactNumber(value?: number | null) {
  if (!value) return '0'
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function byteSize(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 ** 2).toFixed(1)} MB`
}

function shortHash(value?: string | null, length = 9) {
  return value ? value.slice(0, length) : '—'
}

function stringify(value: unknown) {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function InspectorMark() {
  return <svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" /><path d="m9 11 5 5-5 5M17 21h6" /></svg>
}

function RefreshIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16 7a6.5 6.5 0 1 0 .2 5.4M16 3v4h-4" /></svg>
}

function ArtifactIcon({ kind }: { kind: Artifact['kind'] }) {
  return <span className={`artifact-icon ${kind}`} aria-hidden="true">
    {kind === 'pdf' ? 'PDF' : kind === 'image' ? 'IMG' : kind === 'data' ? 'XLS' : kind === 'text' ? 'TXT' : 'FILE'}
  </span>
}

const ExperimentRail = memo(function ExperimentRail({
  experiments,
  selectedId,
  loading,
  onSelect,
}: {
  experiments: ExperimentSummary[]
  selectedId: string | null
  loading: boolean
  onSelect: (id: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  return <aside className="experiment-rail" aria-label={en ? 'Experiments' : '实验列表'}>
    <div className="rail-title"><strong>{en ? 'EXPERIMENTS' : '实验'}</strong><span>{experiments.length}</span></div>
    <div className="rail-scroll">
      {loading && experiments.length === 0 ? <div className="empty-rail">{en ? 'Loading…' : '正在加载…'}</div> : null}
      {!loading && experiments.length === 0 ? <div className="empty-rail">{en ? 'No experiments yet.' : '还没有实验。'}</div> : null}
      {experiments.map((experiment) => <button
        type="button"
        key={experiment.id}
        className={`experiment-row ${selectedId === experiment.id ? 'selected' : ''}`}
        onClick={() => onSelect(experiment.id)}
      >
        <span className="row-heading"><i className={`status-dot ${experiment.status}`} /><strong>{experiment.label}</strong></span>
        <small>{experiment.settings.model ?? '—'} · {experiment.settings.reasoning_effort ?? '—'}</small>
        <time>{formatClock(experiment.submitted_at)}</time>
        <code>{shortHash(experiment.source_sha256)}</code>
      </button>)}
    </div>
    <footer><span className="live-dot" />{en ? 'Local, read-only data' : '本机只读数据'}</footer>
  </aside>
})

const CaseRail = memo(function CaseRail({
  cases,
  selectedSlug,
  onSelect,
}: {
  cases: CaseSummary[]
  selectedSlug: string | null
  onSelect: (slug: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  return <aside className="case-rail" aria-label={en ? 'Benchmark cases' : 'Benchmark 题目'}>
    <div className="rail-title"><strong>{en ? 'CASES' : '题目'}</strong><span>{cases.length}</span></div>
    <div className="case-list">
      {cases.map((item) => <button
        type="button"
        key={item.slug}
        className={`case-row ${selectedSlug === item.slug ? 'selected' : ''}`}
        onClick={() => onSelect(item.slug)}
      >
        <span className="row-heading"><i className={`status-dot ${item.status}`} /><strong>{caseLabel(item, language)}</strong></span>
        <span>{statusLabel(item.status, language)}</span>
        <small>{item.status === 'running'
          ? `${en ? 'Started' : '开始于'} ${formatClock(item.started_at)}`
          : item.duration_seconds != null
            ? `${en ? 'Duration' : '耗时'} ${duration(item.duration_seconds)}`
            : `${item.request_count ?? 0} ${en ? 'requests' : '次请求'}`}</small>
      </button>)}
    </div>
  </aside>
})

function eventMeta(event: AgentEvent, en: boolean) {
  if (event.kind === 'task') return { tone: 'start', title: en ? 'Experiment task' : '实验任务', body: event.task ?? '' }
  if (event.kind === 'assistant') {
    const calls = event.tool_calls ?? []
    return {
      tone: calls.length ? 'tool' : 'assistant',
      title: calls.length ? (en ? `Agent step ${event.step ?? ''}` : `Agent 第 ${event.step ?? ''} 步`) : (en ? 'Assistant message' : 'Agent 消息'),
      body: event.text || event.reasoning_text || (calls.length ? calls.map(([name]) => name).join(', ') : ''),
    }
  }
  if (event.kind === 'tool_result') return { tone: 'result', title: en ? 'Tool result' : '工具返回', body: event.observation ?? '', code: event.name }
  if (event.kind === 'subagent_start') return { tone: 'subagent', title: en ? 'Sub-agent started' : 'Sub-agent 已启动', body: event.task ?? '', code: `SUB-${event.subagent}` }
  if (event.kind === 'subagent_end') return { tone: 'subagent', title: en ? 'Sub-agent returned' : 'Sub-agent 已返回', body: `${compactNumber(event.tokens)} tokens`, code: `SUB-${event.subagent}` }
  if (event.kind === 'tool_prune_start') return {
    tone: 'warning',
    title: en ? 'Tool Result pruning started' : '开始清理 Tool Result',
    body: `${event.prune_level ?? 'moderate'} · ${Number(event.pruned_tool_results ?? 0).toLocaleString()} ${en ? 'results selected' : '个 Result 待处理'}`,
    code: event.strategy,
  }
  if (event.kind === 'tool_prune_done') return {
    tone: 'result',
    title: en ? 'Tool Results pruned' : 'Tool Result 清理完成',
    body: `${Number(event.pruned_tool_results ?? 0).toLocaleString()} ${en ? 'results' : '个 Result'} · ${Number(event.tool_result_tokens_saved_estimate ?? 0).toLocaleString()} ${en ? 'estimated tokens saved' : '预计 Token 已节省'}`,
    code: event.prune_level,
  }
  if (event.kind === 'compact_start') return {
    tone: 'warning',
    title: en ? 'Context compaction started' : '开始压缩 Context',
    body: event.strategy === 'externalized_tool_results_v1'
      ? `${Number(event.context_chars_before ?? 0).toLocaleString()} chars · ≥${Number(event.externalize_threshold_tokens ?? 0).toLocaleString()} tokens`
      : `${Number(event.context_chars_before ?? 0).toLocaleString()} chars · ${event.summarizing ?? 0} ${en ? 'messages selected' : '条消息待处理'}`,
    code: event.strategy,
  }
  if (event.kind === 'compact_done') {
    const reduction = Math.max(0, (1 - Number(event.compression_ratio ?? 1)) * 100)
    return {
      tone: 'result',
      title: en ? 'Context compacted' : 'Context 压缩完成',
      body: `${Number(event.context_chars_before ?? 0).toLocaleString()} → ${Number(event.context_chars_after ?? 0).toLocaleString()} chars · ${reduction.toFixed(1)}% ${en ? 'reduction' : '缩减'}`,
      code: event.strategy,
    }
  }
  if (event.kind.startsWith('verification')) return { tone: 'verification', title: en ? 'Verification event' : '验证事件', body: stringify(event.summary ?? event.kind) }
  if (event.kind === 'done') return { tone: 'result', title: en ? 'Experiment completed' : '实验已完成', body: event.text ?? '' }
  if (event.kind === 'max_steps') return { tone: 'warning', title: en ? 'Maximum steps reached' : '达到最大步数', body: '' }
  return { tone: 'neutral', title: event.kind.replaceAll('_', ' '), body: stringify(event.text ?? event.observation ?? '') }
}

const EventRow = memo(function EventRow({ event, selected, onSelect }: {
  event: AgentEvent
  selected: boolean
  onSelect: () => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const meta = eventMeta(event, en)
  const expandable = Boolean(meta.body || event.reasoning_text || event.tool_calls?.length)
  return <article className={`event-row ${selected ? 'selected' : ''}`}>
    <button type="button" className="event-summary" onClick={onSelect}>
      <span className="event-time"><strong>{duration(event.t)}</strong><small>{event.ts ? new Date(event.ts * 1000).toLocaleTimeString() : '—'}</small></span>
      <span className={`event-marker ${meta.tone}`}><i /></span>
      <span className="event-copy">
        <strong>{meta.title}</strong>
        <small>{meta.code ? <code>{meta.code}</code> : null}{meta.body ? meta.body.split('\n')[0].slice(0, 110) : (en ? 'Recorded event' : '已记录事件')}</small>
      </span>
      <span className="event-tokens">{event.total_tokens ? <>{compactNumber(event.total_tokens)}<small>tokens</small></> : null}</span>
      <span className="event-chevron">{expandable ? '⌄' : '›'}</span>
    </button>
    {selected && expandable ? <div className="event-detail">
      {event.tool_calls?.map(([name, args], index) => <div className="event-tool" key={`${name}-${index}`}><code>{name}</code><pre>{stringify(args)}</pre></div>)}
      {event.reasoning_text ? <><label>{en ? 'Reasoning' : '推理'}</label><pre>{event.reasoning_text}</pre></> : null}
      {meta.body ? <><label>{event.kind === 'tool_result' ? (en ? 'Result' : '返回') : (en ? 'Content' : '内容')}</label><pre>{meta.body}</pre></> : null}
    </div> : null}
  </article>
})

const OMITTED_EVENT_KINDS = new Set(['context', 'provider_heartbeat', 'tool_heartbeat', 'heartbeat'])

function Timeline({ events, status }: { events: AgentEvent[]; status?: CaseStatus }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [autoScroll, setAutoScroll] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const deferredQuery = useDeferredValue(query.trim().toLowerCase())
  const visible = useMemo(() => events.filter((event) => {
    if (OMITTED_EVENT_KINDS.has(event.kind)) return false
    if (!deferredQuery) return true
    return stringify(event).toLowerCase().includes(deferredQuery)
  }), [deferredQuery, events])
  useEffect(() => {
    if (!autoScroll || !scrollRef.current) return
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [autoScroll, events.length])
  return <main className="timeline-panel">
    <div className="panel-heading">
      <div><strong>{en ? 'TIMELINE' : '时间线'}</strong><span>{visible.length} {en ? 'events' : '条事件'}</span></div>
      <div className="timeline-controls">
        <label className="auto-scroll"><span>{en ? 'Auto-scroll' : '自动滚动'}</span><input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} /><i /></label>
        <label className="timeline-search"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5" /><path d="m12.5 12.5 4 4" /></svg><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={en ? 'Filter events' : '筛选事件'} /></label>
      </div>
    </div>
    <div className="timeline-scroll" ref={scrollRef}>
      {visible.length === 0 ? <div className="timeline-empty"><InspectorMark /><strong>{status === 'queued' ? (en ? 'Waiting for worker' : '等待 Worker 启动') : (en ? 'No matching events' : '没有匹配事件')}</strong><p>{en ? 'New Agent steps and tool results appear here automatically.' : '新的 Agent 步骤和工具结果会自动出现在这里。'}</p></div> : null}
      {visible.map((event, index) => <EventRow key={`${event.ts ?? event.t}-${event.kind}-${index}`} event={event} selected={selectedIndex === index} onSelect={() => setSelectedIndex(selectedIndex === index ? null : index)} />)}
      {status === 'running' ? <div className="live-tail"><span /><span /><span />{en ? 'Waiting for the next event' : '等待下一条事件'}</div> : null}
    </div>
  </main>
}

function DefinitionList({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return <dl className="definition-list">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}

function Overview({ experiment, selectedCase, detail, events }: {
  experiment: ExperimentDetail
  selectedCase?: CaseSummary
  detail: CaseDetail | null
  events: AgentEvent[]
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const settings = experiment.settings
  const usage = detail?.usage ?? {}
  const runDuration = selectedCase?.duration_seconds ?? elapsed(selectedCase?.started_at, selectedCase?.finished_at)
  const compaction = useMemo(() => {
    let count = 0
    let latest: AgentEvent | undefined
    let summaryTokens = 0
    let externalizedResults = 0
    let toolResultTokensSaved = 0
    let pruningCount = 0
    let prunedResults = 0
    const prunedByTool = new Map<string, number>()
    for (const event of events) {
      if (event.kind === 'compact_done') {
        count += 1
        latest = event
        summaryTokens += Number(event.summary_usage?.total_tokens ?? 0)
        externalizedResults += Number(event.externalized_tool_results ?? 0)
        toolResultTokensSaved += Number(event.tool_result_tokens_saved_estimate ?? 0)
      }
      if (event.kind === 'tool_prune_done') {
        pruningCount += 1
        prunedResults += Number(event.pruned_tool_results ?? 0)
        toolResultTokensSaved += Number(event.tool_result_tokens_saved_estimate ?? 0)
        for (const [tool, metrics] of Object.entries(event.tool_result_pruning_by_tool ?? {})) {
          prunedByTool.set(tool, (prunedByTool.get(tool) ?? 0) + Number(metrics.count ?? 0))
        }
      }
    }
    return {
      count,
      latest,
      summaryTokens,
      externalizedResults,
      toolResultTokensSaved,
      pruningCount,
      prunedResults,
      prunedByTool: [...prunedByTool.entries()]
        .sort((left, right) => right[1] - left[1])
        .map(([tool, toolCount]) => `${tool} (${toolCount})`)
        .join(', '),
    }
  }, [events])
  const latestCompressionReduction = compaction.latest
    ? Math.max(0, (1 - Number(compaction.latest.compression_ratio ?? 1)) * 100)
    : null
  return <div className="inspector-section overview-section">
    <SectionTitle>{en ? 'EXPERIMENT OVERVIEW' : '实验概览'}</SectionTitle>
    <DefinitionList rows={[
      [en ? 'Source hash' : '源码哈希', <code>{experiment.source_sha256 ?? '—'}</code>],
      [en ? 'Git commit' : 'Git 提交', <code>{experiment.git.commit ?? '—'}</code>],
      [en ? 'Working tree' : '工作区', <span className={experiment.git.dirty ? 'warning-text' : 'success-text'}>{experiment.git.dirty ? (en ? 'Dirty snapshot' : '包含未提交改动') : (en ? 'Clean' : '干净')}</span>],
      [en ? 'Case' : '题目', selectedCase ? caseLabel(selectedCase, language) : '—'],
      [en ? 'Status' : '状态', <StatusValue status={selectedCase?.status} />],
      [en ? 'Elapsed time' : '已运行', duration(runDuration)],
      [en ? 'Provider' : '供应商', settings.provider ?? '—'],
      [en ? 'Model' : '模型', <code>{settings.model ?? '—'}</code>],
      [en ? 'Reasoning' : '推理强度', settings.reasoning_effort ?? '—'],
      [en ? 'Verification' : '内置验证', settings.verification_enabled ? (en ? 'Enabled' : '启用') : (en ? 'External scoring' : '外部评分')],
      [en ? 'Sandbox' : '沙箱', settings.sandbox ?? '—'],
    ]} />
    {settings.compaction_strategy || compaction.count ? <>
      <SectionTitle>{en ? 'CONTEXT COMPACTION' : 'CONTEXT 压缩'}</SectionTitle>
      <DefinitionList rows={[
        [en ? 'Experiment profile' : '实验组', settings.context_profile ?? (en ? 'Custom' : '自定义')],
        [en ? 'Strategy' : '压缩策略', <code>{settings.compaction_strategy ?? '—'}</code>],
        [en ? 'Trigger' : '触发阈值', settings.compact_threshold_tokens ? `${Number(settings.compact_threshold_tokens).toLocaleString()} tokens` : '—'],
        [en ? 'Raw tail' : '原样保留', settings.keep_tail_messages ? `${settings.keep_tail_messages}+ ${en ? 'messages' : '条消息'}` : '—'],
        [en ? 'Tool prune trigger' : 'Tool 清理阈值', settings.tool_prune_threshold_tokens ? `${Number(settings.tool_prune_threshold_tokens).toLocaleString()} tokens` : '—'],
        [en ? 'Aggressive trigger' : '激进清理阈值', settings.tool_prune_aggressive_threshold_tokens ? `${Number(settings.tool_prune_aggressive_threshold_tokens).toLocaleString()} tokens` : '—'],
        [en ? 'Protected recent results' : '保留最新 Result', settings.tool_prune_recent_results != null ? `${settings.tool_prune_recent_results} ${en ? 'results' : '个'}` : '—'],
        [en ? 'Result file threshold' : 'Result 外置阈值', settings.tool_result_externalize_threshold_tokens ? `${Number(settings.tool_result_externalize_threshold_tokens).toLocaleString()} estimated tokens` : '—'],
        [en ? 'Compactions' : '压缩次数', compaction.count.toLocaleString()],
        [en ? 'Tool pruning passes' : 'Tool 清理次数', compaction.pruningCount.toLocaleString()],
        [en ? 'Pruned results' : '已清理 Result', compaction.prunedResults.toLocaleString()],
        [en ? 'Pruned by tool' : '按 Tool 分布', compaction.prunedByTool || '—'],
        [en ? 'Externalized results' : '已外置 Result', compaction.externalizedResults.toLocaleString()],
        [en ? 'Tool tokens saved' : 'Result 预计节省 Token', compaction.toolResultTokensSaved.toLocaleString()],
        [en ? 'Latest characters' : '最近字符数', compaction.latest ? `${Number(compaction.latest.context_chars_before ?? 0).toLocaleString()} → ${Number(compaction.latest.context_chars_after ?? 0).toLocaleString()}` : '—'],
        [en ? 'Latest reduction' : '最近缩减', latestCompressionReduction == null ? '—' : `${latestCompressionReduction.toFixed(1)}%`],
        [en ? 'Summary API tokens' : 'Summary API Token', compaction.summaryTokens.toLocaleString()],
      ]} />
    </> : null}
    <SectionTitle>{en ? 'TOKEN USAGE' : 'TOKEN 用量'}</SectionTitle>
    <DefinitionList rows={[
      [en ? 'Total so far' : '当前总量', Number(usage.total_tokens ?? 0).toLocaleString()],
      [en ? 'Prompt' : '输入', Number(usage.prompt_tokens ?? 0).toLocaleString()],
      [en ? 'Completion' : '输出', Number(usage.completion_tokens ?? 0).toLocaleString()],
      [en ? 'Cached input' : '缓存输入', Number(usage.cached_input_tokens ?? 0).toLocaleString()],
      [en ? 'Estimated cost' : '预估费用', usage.estimated_cost_cny != null ? `¥${Number(usage.estimated_cost_cny).toFixed(4)}` : '—'],
    ]} />
    <SectionTitle>{en ? 'CURRENT OUTPUT' : '当前产物'}</SectionTitle>
    <div className="overview-counts">
      <div><strong>{detail?.artifacts.length ?? 0}</strong><span>{en ? 'Artifacts' : '产物'}</span></div>
      <div><strong>{detail?.context.request_count ?? 0}</strong><span>{en ? 'Context requests' : 'Context 请求'}</span></div>
      <div><strong>{events.length}</strong><span>{en ? 'Loaded events' : '已载入事件'}</span></div>
    </div>
    {detail?.artifacts.find((item) => item.kind === 'pdf') ? <ArtifactRow artifact={detail.artifacts.find((item) => item.kind === 'pdf')!} /> : null}
  </div>
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="section-title">{children}</h3>
}

function StatusValue({ status }: { status?: CaseStatus }) {
  const { language } = useLanguage()
  return <span className="status-value"><i className={`status-dot ${status ?? 'unknown'}`} />{statusLabel(status, language)}</span>
}

function TextPane({ text, empty }: { text: string; empty: string }) {
  return text.trim() ? <pre className="document-pane">{text}</pre> : <div className="tab-empty"><span>—</span>{empty}</div>
}

function ArtifactRow({ artifact }: { artifact: Artifact }) {
  return <a className="artifact-row" href={artifact.url} target="_blank" rel="noreferrer">
    <ArtifactIcon kind={artifact.kind} />
    <span><strong>{artifact.name}</strong><small>{artifact.path} · {byteSize(artifact.bytes)}</small></span>
    <b>↗</b>
  </a>
}

function ArtifactsPane({ artifacts }: { artifacts: Artifact[] }) {
  const { language } = useLanguage()
  const en = language === 'en'
  return <div className="inspector-section"><SectionTitle>{en ? `ARTIFACTS (${artifacts.length})` : `产物 (${artifacts.length})`}</SectionTitle><div className="artifact-list">{artifacts.length ? artifacts.map((artifact) => <ArtifactRow artifact={artifact} key={artifact.path} />) : <div className="tab-empty"><span>—</span>{en ? 'No artifacts yet' : '尚无产物'}</div>}</div></div>
}

const ContextPane = memo(function ContextPane({
  agents,
  selected,
  detail,
  loading,
  onSelect,
}: {
  agents: AgentContextGroup[]
  selected: string | null
  detail: ContextRequestDetail | null
  loading: boolean
  onSelect: (requestId: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [chosenAgent, setChosenAgent] = useState<string | null>(null)
  const selectedAgent = useMemo(() => agents.find((agent) => agent.requests.some((request) => request.request_id === selected)), [agents, selected])
  const activeAgent = agents.find((agent) => agent.key === chosenAgent) ?? selectedAgent ?? agents[0]
  const requests = activeAgent?.requests ?? []

  const chooseAgent = (agent: AgentContextGroup) => {
    setChosenAgent(agent.key)
    if (!agent.requests.some((request) => request.request_id === selected)) {
      const newest = agent.requests[0]
      if (newest) onSelect(newest.request_id)
    }
  }

  return <div className="context-pane">
    <div className="context-agent-strip" aria-label={en ? 'Agent contexts' : 'Agent Context 列表'}>
      {loading && agents.length === 0 ? <div className="context-agent-loading">{en ? 'Loading Agent contexts…' : '正在整理 Agent Context…'}</div> : null}
      {!loading && agents.length === 0 ? <div className="context-agent-loading">{en ? 'No Agent contexts recorded yet' : '尚未记录 Agent Context'}</div> : null}
      {agents.map((agent) => <button
        type="button"
        key={agent.key}
        className={activeAgent?.key === agent.key ? 'selected' : ''}
        onClick={() => chooseAgent(agent)}
        aria-label={`${agent.agent_role} Context`}
      >
        <i>{agent.agent_role === 'Main Agent' ? 'M' : agent.agent_role.replace('Subagent ', 'S')}</i>
        <span><strong>{agent.agent_role}</strong><small>{agent.agent_scope || (en ? 'root scope' : '主作用域')}</small></span>
        <span><b>{agent.request_count}</b><small>{en ? 'requests' : '次请求'}</small></span>
        <span><b>{compactNumber(agent.total_tokens || agent.estimated_input_tokens)}</b><small>tokens</small></span>
      </button>)}
    </div>
    <div className="context-workspace">
      <div className="context-request-list">
        {activeAgent ? <header><strong>{activeAgent.agent_role}</strong><span>{activeAgent.request_count} {en ? 'requests' : '次请求'} · {en ? 'latest step' : '最近步骤'} {activeAgent.latest_step ?? '—'}</span></header> : null}
        {requests.map((request) => <button type="button" key={request.request_id} className={selected === request.request_id ? 'selected' : ''} onClick={() => onSelect(request.request_id)}>
          <span><strong>#{request.sequence}</strong><small>{request.phase} · step {request.step ?? '—'}</small></span>
          <span><time>{request.ts ? new Date(request.ts * 1000).toLocaleTimeString() : '—'}</time><small>{compactNumber(request.usage?.total_tokens ?? request.estimated_input_tokens)} tok</small></span>
        </button>)}
      </div>
      <div className="context-detail">
        {detail ? <div className="context-detail-heading"><span><strong>{detail.agent_role}</strong><small>{detail.agent_scope || (en ? 'root scope' : '主作用域')} · #{detail.sequence}</small></span><span><b>{detail.message_count ?? detail.items.length}</b><small>{en ? 'context sections' : '段 Context'}</small></span></div> : null}
        {!detail ? <div className="tab-empty"><span>⌁</span>{en ? 'Select a model request' : '选择一条模型请求'}</div> : detail.items.map((item, index) => <details key={`${item.label}-${index}`} open={index < 2}><summary><span className={`context-kind ${item.category}`} /><strong>{item.label}</strong><small>{compactNumber(item.estimated_tokens)} tok</small></summary><div>{item.source ? <label>{item.source}</label> : null}<pre>{stringify(item.content)}</pre></div></details>)}
      </div>
    </div>
  </div>
})

function ContextStage({
  agents,
  selected,
  detail,
  loading,
  onSelect,
}: {
  agents: AgentContextGroup[]
  selected: string | null
  detail: ContextRequestDetail | null
  loading: boolean
  onSelect: (requestId: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const requestCount = agents.reduce((total, agent) => total + agent.request_count, 0)
  return <main className="timeline-panel context-stage">
    <div className="panel-heading">
      <div><strong>{en ? 'AGENT CONTEXT' : 'AGENT CONTEXT'}</strong><span>{agents.length} {en ? 'Agents' : '个 Agent'} · {requestCount} {en ? 'requests' : '次请求'}</span></div>
      <span className="context-stage-hint">{en ? 'Grouped by Agent' : '按 Agent 分组'}</span>
    </div>
    <ContextPane agents={agents} selected={selected} detail={detail} loading={loading} onSelect={onSelect} />
  </main>
}

function ContextRequestOverview({ detail }: { detail: ContextRequestDetail | null }) {
  const { language } = useLanguage()
  const en = language === 'en'
  if (!detail) return <div className="tab-empty"><span>⌁</span>{en ? 'Select a Context request in the center panel' : '请在中间栏选择一条 Context 请求'}</div>
  return <div className="inspector-section context-overview">
    <SectionTitle>{en ? 'CONTEXT REQUEST' : 'CONTEXT 请求摘要'}</SectionTitle>
    <DefinitionList rows={[
      [en ? 'Agent' : 'Agent', detail.agent_role],
      [en ? 'Scope' : '作用域', <code>{detail.agent_scope || 'root'}</code>],
      [en ? 'Request' : '请求', <code>#{detail.sequence}</code>],
      [en ? 'Phase' : '阶段', detail.phase],
      [en ? 'Step' : '步骤', detail.step ?? '—'],
      [en ? 'Status' : '状态', detail.status],
      [en ? 'Provider' : '供应商', detail.provider],
      [en ? 'Model' : '模型', <code>{detail.model}</code>],
      [en ? 'Duration' : '耗时', detail.duration_seconds != null ? `${detail.duration_seconds.toFixed(2)}s` : '—'],
      [en ? 'Messages' : '消息数', detail.message_count ?? '—'],
      [en ? 'Tool definitions' : '工具定义', detail.tool_definition_count ?? '—'],
      [en ? 'Estimated input' : '估算输入', `${Number(detail.estimated_input_tokens ?? 0).toLocaleString()} tokens`],
      [en ? 'Actual total' : '实际总量', `${Number(detail.usage?.total_tokens ?? 0).toLocaleString()} tokens`],
    ]} />
    <SectionTitle>{en ? 'CONTEXT SECTIONS' : 'CONTEXT 组成'}</SectionTitle>
    <div className="context-section-counts">
      {Array.from(new Set(detail.items.map((item) => item.category))).map((category) => <span key={category}><i className={`context-kind ${category}`} />{category.replaceAll('_', ' ')}</span>)}
    </div>
  </div>
}

function InspectorPanel({
  tab,
  onTab,
  experiment,
  selectedCase,
  detail,
  contextDetail,
  events,
}: {
  tab: InspectorTab
  onTab: (tab: InspectorTab) => void
  experiment: ExperimentDetail
  selectedCase?: CaseSummary
  detail: CaseDetail | null
  contextDetail: ContextRequestDetail | null
  events: AgentEvent[]
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const tabs: Array<[InspectorTab, string]> = [
    ['overview', en ? 'Overview' : '概览'],
    ['plan', en ? 'Plan' : '计划'],
    ['decisions', en ? 'Decisions' : '决策'],
    ['artifacts', en ? 'Artifacts' : '产物'],
    ['context', 'Context'],
    ['logs', en ? 'Logs' : '日志'],
  ]
  return <aside className={`inspector-panel tab-${tab}`}>
    <nav>{tabs.map(([value, label]) => <button type="button" className={tab === value ? 'selected' : ''} key={value} onClick={() => onTab(value)}>{label}</button>)}</nav>
    <div className="inspector-scroll">
      {tab === 'overview' ? <Overview experiment={experiment} selectedCase={selectedCase} detail={detail} events={events} /> : null}
      {tab === 'plan' ? <TextPane text={detail?.plan ?? ''} empty={en ? 'No plan recorded yet' : '尚未记录计划'} /> : null}
      {tab === 'decisions' ? <TextPane text={detail?.decisions ?? ''} empty={en ? 'No decisions recorded yet' : '尚未记录决策'} /> : null}
      {tab === 'artifacts' ? <ArtifactsPane artifacts={detail?.artifacts ?? []} /> : null}
      {tab === 'context' ? <ContextRequestOverview detail={contextDetail} /> : null}
      {tab === 'logs' ? <TextPane text={detail?.console_log ?? ''} empty={en ? 'No console output yet' : '尚无控制台输出'} /> : null}
    </div>
  </aside>
}

function App() {
  const { language } = useLanguage()
  const en = language === 'en'
  const initial = useMemo(() => new URLSearchParams(window.location.search), [])
  const [experiments, setExperiments] = useState<ExperimentSummary[]>([])
  const [selectedExperimentId, setSelectedExperimentId] = useState<string | null>(() => initial.get('experiment'))
  const [selectedCaseSlug, setSelectedCaseSlug] = useState<string | null>(() => initial.get('case'))
  const [experiment, setExperiment] = useState<ExperimentDetail | null>(null)
  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null)
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [tab, setTab] = useState<InspectorTab>('overview')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefresh, setLastRefresh] = useState<number | null>(null)
  const [contextAgents, setContextAgents] = useState<AgentContextGroup[]>([])
  const [selectedRequest, setSelectedRequest] = useState<string | null>(null)
  const [contextDetail, setContextDetail] = useState<ContextRequestDetail | null>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const cursorRef = useRef(0)

  const selectExperiment = useCallback((id: string) => {
    startTransition(() => {
      setSelectedExperimentId(id)
      setSelectedCaseSlug(null)
      setExperiment(null)
      setCaseDetail(null)
      setEvents([])
      setContextAgents([])
      setSelectedRequest(null)
      setContextDetail(null)
    })
    cursorRef.current = 0
  }, [])

  const selectCase = useCallback((slug: string) => {
    startTransition(() => {
      setSelectedCaseSlug(slug)
      setCaseDetail(null)
      setEvents([])
      setContextAgents([])
      setSelectedRequest(null)
      setContextDetail(null)
    })
    cursorRef.current = 0
  }, [])

  const loadExperiments = useCallback(async (signal?: AbortSignal) => {
    const next = await fetchExperiments(signal)
    setExperiments(next)
    setSelectedExperimentId((current) => current && next.some((item) => item.id === current) ? current : (next[0]?.id ?? null))
    setLastRefresh(Date.now())
    setLoading(false)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void loadExperiments(controller.signal).catch((caught) => {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : String(caught))
        setLoading(false)
      }
    })
    const timer = window.setInterval(() => void loadExperiments().catch(() => undefined), 3000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [loadExperiments])

  useEffect(() => {
    if (!selectedExperimentId) return
    const controller = new AbortController()
    let stopped = false
    const load = async () => {
      const requestedAfter = cursorRef.current
      try {
        const detail = await fetchExperiment(selectedExperimentId, controller.signal)
        if (stopped) return
        setExperiment(detail)
        const slug = selectedCaseSlug && detail.cases.some((item) => item.slug === selectedCaseSlug)
          ? selectedCaseSlug
          : detail.cases[0]?.slug
        if (!slug) return
        if (slug !== selectedCaseSlug) setSelectedCaseSlug(slug)
        const nextCase = await fetchCase(selectedExperimentId, slug, requestedAfter, controller.signal)
        if (stopped) return
        cursorRef.current = nextCase.events_cursor
        setEvents((current) => requestedAfter === 0 ? nextCase.events : [...current, ...nextCase.events])
        setCaseDetail({ ...nextCase, events: [] })
        setError(null)
        setLastRefresh(Date.now())
      } catch (caught) {
        if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught))
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 2200)
    return () => { stopped = true; controller.abort(); window.clearInterval(timer) }
  }, [selectedCaseSlug, selectedExperimentId])

  useEffect(() => {
    const params = new URLSearchParams()
    if (selectedExperimentId) params.set('experiment', selectedExperimentId)
    if (selectedCaseSlug) params.set('case', selectedCaseSlug)
    window.history.replaceState(null, '', `${window.location.pathname}${params.size ? `?${params}` : ''}`)
  }, [selectedCaseSlug, selectedExperimentId])

  useEffect(() => {
    if (tab !== 'context' || !selectedExperimentId || !selectedCaseSlug) return
    const controller = new AbortController()
    setContextLoading(true)
    void fetchAgentContexts(selectedExperimentId, selectedCaseSlug, controller.signal)
      .then((agents) => {
        setContextAgents(agents)
        setSelectedRequest((current) => current && agents.some((agent) => agent.requests.some((item) => item.request_id === current))
          ? current
          : (agents[0]?.requests[0]?.request_id ?? null))
      })
      .catch((caught) => { if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught)) })
      .finally(() => { if (!controller.signal.aborted) setContextLoading(false) })
    return () => controller.abort()
  }, [caseDetail?.context.request_count, selectedCaseSlug, selectedExperimentId, tab])

  useEffect(() => {
    if (tab !== 'context' || !selectedRequest || !selectedExperimentId || !selectedCaseSlug) return
    const controller = new AbortController()
    void fetchContextRequest(selectedExperimentId, selectedCaseSlug, selectedRequest, controller.signal)
      .then(setContextDetail)
      .catch((caught) => { if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught)) })
    return () => controller.abort()
  }, [selectedCaseSlug, selectedExperimentId, selectedRequest, tab])

  const selectedCase = experiment?.cases.find((item) => item.slug === selectedCaseSlug)
  const selectContextRequest = useCallback((id: string) => {
    setSelectedRequest(id)
    setContextDetail(null)
  }, [])

  return <div className="experimental-shell">
    <header className="app-header">
      <div className="brand"><InspectorMark /><strong>Experimental Inspector</strong></div>
      <div className="connection"><span className="live-dot" /><strong>{en ? 'Live' : '实时'}</strong><span>{en ? 'Reading local experiment logs' : '正在读取本机实验日志'}</span></div>
      <div className="header-context">
        <span><small>{en ? 'Experiment' : '实验'}</small><strong>{experiment?.label ?? '—'}</strong></span>
        <span><small>{en ? 'Case' : '题目'}</small><strong>{selectedCase ? caseLabel(selectedCase, language) : '—'}</strong></span>
      </div>
      <div className="header-actions">
        <button type="button" className="refresh-button" onClick={() => void loadExperiments()}><RefreshIcon />{en ? 'Refresh' : '刷新'}</button>
        <small>{lastRefresh ? `${en ? 'Updated' : '更新于'} ${new Date(lastRefresh).toLocaleTimeString()}` : '—'}</small>
        <LanguageSwitcher />
        <span className="read-only"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="8" width="12" height="9" rx="2" /><path d="M7 8V6a3 3 0 0 1 6 0v2" /></svg>{en ? 'Read-only' : '只读'}</span>
      </div>
    </header>
    {error ? <div className="error-banner"><strong>{en ? 'Inspector read error' : 'Inspector 读取错误'}</strong><span>{error}</span><button type="button" onClick={() => setError(null)}>×</button></div> : null}
    <div className="mobile-selection">
      <select value={selectedExperimentId ?? ''} onChange={(event) => selectExperiment(event.target.value)}>{experiments.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}</select>
      <select value={selectedCaseSlug ?? ''} onChange={(event) => selectCase(event.target.value)}>{experiment?.cases.map((item) => <option value={item.slug} key={item.slug}>{caseLabel(item, language)} · {statusLabel(item.status, language)}</option>)}</select>
    </div>
    <div className="app-grid">
      <ExperimentRail experiments={experiments} selectedId={selectedExperimentId} loading={loading} onSelect={selectExperiment} />
      <CaseRail cases={experiment?.cases ?? []} selectedSlug={selectedCaseSlug} onSelect={selectCase} />
      {tab === 'context'
        ? <ContextStage agents={contextAgents} selected={selectedRequest} detail={contextDetail} loading={contextLoading} onSelect={selectContextRequest} />
        : <Timeline events={events} status={selectedCase?.status} />}
      {experiment ? <InspectorPanel
        tab={tab}
        onTab={setTab}
        experiment={experiment}
        selectedCase={selectedCase}
        detail={caseDetail}
        contextDetail={contextDetail}
        events={events}
      /> : <aside className="inspector-panel"><div className="timeline-empty"><InspectorMark /><strong>{loading ? (en ? 'Loading inspector' : '正在加载 Inspector') : (en ? 'Select an experiment' : '请选择实验')}</strong></div></aside>}
    </div>
  </div>
}

export default App
