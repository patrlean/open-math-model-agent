import {
  memo,
  startTransition,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useState,
} from 'react'
import {
  exportUrl,
  fetchRequestDetail,
  fetchRequests,
  fetchRuns,
  fetchToolMetrics,
} from './api'
import {
  categoryMeta,
  filterCategories,
  formatClock,
  formatDate,
  formatTokens,
  statusLabel,
  stringifyContent,
} from './format'
import type {
  ContextCategory,
  ContextItem,
  ContextRequestDetail,
  ContextRequestSummary,
  ContextRun,
  ToolMetricAgent,
  ToolMetricCounters,
  ToolMetrics,
} from './types'
import { LanguageSwitcher, useLanguage } from '../src/i18n'
import loadingRingUrl from '../src/assets/sequential-loading-ring.svg'

function InspectorMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 32 32">
      <rect width="32" height="32" rx="8" fill="currentColor" />
      <path d="m9 11 5 5-5 5M17 21h6" fill="none" stroke="#fff" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.2" />
    </svg>
  )
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg aria-hidden="true" className={open ? 'chevron open' : 'chevron'} viewBox="0 0 20 20">
      <path d="m6 8 4 4 4-4" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
    </svg>
  )
}

function CopyIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <rect x="7" y="6" width="8" height="9" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5 12H4.5A1.5 1.5 0 0 1 3 10.5v-6A1.5 1.5 0 0 1 4.5 3h6A1.5 1.5 0 0 1 12 4.5V5" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <path d="M10 3v9m0 0 3-3m-3 3L7 9M4 15h12" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" />
    </svg>
  )
}

interface ToolCallCount {
  count: number
  name: string
}

function getToolCallCounts(items: ContextItem[]): ToolCallCount[] {
  const counts = new Map<string, number>()
  for (const item of items) {
    if (item.category !== 'tool_call') continue
    const content = item.content
    const rawName = (
      content
      && typeof content === 'object'
      && !Array.isArray(content)
    ) ? (content as { name?: unknown }).name : null
    const name = typeof rawName === 'string' && rawName.trim()
      ? rawName.trim()
      : 'unknown'
    counts.set(name, (counts.get(name) || 0) + 1)
  }
  return Array.from(counts, ([name, count]) => ({ count, name }))
    .sort((left, right) => (
      right.count - left.count || left.name.localeCompare(right.name)
    ))
}

const ToolCallStats = memo(function ToolCallStats({
  items,
}: {
  items: ContextItem[]
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const tools = useMemo(() => getToolCallCounts(items), [items])
  const total = useMemo(
    () => tools.reduce((sum, tool) => sum + tool.count, 0),
    [tools],
  )

  return (
    <details className="tool-call-stats">
      <summary title={en ? 'View tool call counts in this context' : '查看当前 Context 中的工具调用次数'}>
        <span>{en ? 'Tool calls' : '工具调用'}</span>
        <strong>{total}</strong>
        <i aria-hidden="true">⌄</i>
      </summary>
      <div className="tool-call-stats-menu">
        <header>
          <div>
            <strong>{en ? 'Tool calls in this context' : '当前 Context 的工具调用'}</strong>
            <small>{en ? `${tools.length} tools · ${total} calls` : `${tools.length} 个工具 · ${total} 次调用`}</small>
          </div>
        </header>
        {tools.length ? (
          <ol>
            {tools.map((tool) => (
              <li key={tool.name}>
                <code>{tool.name}</code>
                <strong>{tool.count}</strong>
              </li>
            ))}
          </ol>
        ) : (
          <p>{en ? 'No tool has been called yet.' : '当前还没有工具调用。'}</p>
        )}
      </div>
    </details>
  )
})

function metricRate(successes: number, evaluated: number) {
  if (!evaluated) return '—'
  return `${Math.round((successes / evaluated) * 100)}%`
}

function metricFraction(successes: number, evaluated: number) {
  return evaluated ? `${successes}/${evaluated}` : '—'
}

const ToolMetricCard = memo(function ToolMetricCard({
  label,
  value,
  detail,
  tone = '',
}: {
  label: string
  value: string
  detail: string
  tone?: string
}) {
  return (
    <div className={`tool-metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  )
})

function specializedMetrics(summary: ToolMetricCounters, en: boolean) {
  return [
    summary.compile_attempts ? {
      key: 'compile',
      label: en ? 'LaTeX compiled' : 'LaTeX 编译',
      successes: summary.compile_successes,
      attempts: summary.compile_attempts,
    } : null,
    summary.acceptance_attempts ? {
      key: 'acceptance',
      label: en ? 'Paper accepted' : '论文验收',
      successes: summary.acceptance_successes,
      attempts: summary.acceptance_attempts,
    } : null,
    summary.verdict_attempts ? {
      key: 'verdict',
      label: en ? 'Valid verdicts' : '有效验证结论',
      successes: summary.verdict_successes,
      attempts: summary.verdict_attempts,
    } : null,
    summary.retry_attempts ? {
      key: 'recovery',
      label: en ? 'Retry recovery' : '重试恢复',
      successes: summary.recovered_retries,
      attempts: summary.retry_attempts,
    } : null,
  ].filter((item): item is {
    key: string
    label: string
    successes: number
    attempts: number
  } => item !== null)
}

const ToolMetricsPanel = memo(function ToolMetricsPanel({
  runId,
  runName,
}: {
  runId: string
  runName: string
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [metrics, setMetrics] = useState<ToolMetrics | null>(null)
  const [agent, setAgent] = useState<ToolMetricAgent>('all')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setMetrics(await fetchToolMetrics(runId))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [runId])

  const group = metrics?.groups[agent]
  const summary = group?.summary
  const agentLabels: Record<ToolMetricAgent, string> = en
    ? {
      all: 'All agents',
      main: 'Main Agent',
      verifier: 'Verifier',
      subagent: 'Sub-agent',
    }
    : {
      all: '全部 Agent',
      main: '主 Agent',
      verifier: '验证 Agent',
      subagent: 'Sub-agent',
    }

  return (
    <details
      className="tool-metrics"
      onToggle={(event) => {
        if (event.currentTarget.open && !metrics && !loading) void load()
      }}
    >
      <summary title={en ? 'View conversation-level tool reliability' : '查看会话级工具可靠性'}>
        <span>{en ? 'Tool metrics' : '工具指标'}</span>
        <i aria-hidden="true">⌄</i>
      </summary>
      <section className="tool-metrics-panel">
        <header>
          <div>
            <strong>{en ? 'Tool reliability' : '工具可靠性'}</strong>
            <small title={runName}>{runName}</small>
          </div>
          <div className="tool-metrics-controls">
            <select
              aria-label={en ? 'Filter tool metrics by agent type' : '按 Agent 类型筛选工具指标'}
              onChange={(event) => setAgent(event.target.value as ToolMetricAgent)}
              value={agent}
            >
              {(Object.keys(agentLabels) as ToolMetricAgent[]).map((key) => (
                key === 'all' || (metrics?.groups[key].summary.total_calls || 0) > 0
                  ? <option key={key} value={key}>{agentLabels[key]}</option>
                  : null
              ))}
            </select>
            <button disabled={loading} onClick={() => void load()} type="button">
              {loading ? '…' : (en ? 'Refresh' : '刷新')}
            </button>
          </div>
        </header>

        {error ? <p className="tool-metrics-error">{error}</p> : null}
        {!metrics && loading ? (
          <LoadingState label={en ? 'Calculating unique tool calls…' : '正在统计唯一工具调用…'} />
        ) : summary && group ? (
          <>
            <div className="tool-metric-cards">
              <ToolMetricCard
                detail={en ? `${summary.completed_calls}/${summary.total_calls} calls returned` : `${summary.completed_calls}/${summary.total_calls} 次调用已返回`}
                label={en ? 'Completion' : '返回完成率'}
                value={metricRate(summary.completed_calls, summary.total_calls)}
              />
              <ToolMetricCard
                detail={en ? `${summary.protocol_successes}/${summary.protocol_evaluated} valid contracts` : `${summary.protocol_successes}/${summary.protocol_evaluated} 次协议有效`}
                label={en ? 'Valid invocation' : '调用有效率'}
                tone="protocol"
                value={metricRate(summary.protocol_successes, summary.protocol_evaluated)}
              />
              <ToolMetricCard
                detail={en ? `${summary.objective_successes}/${summary.objective_evaluated} objectives met` : `${summary.objective_successes}/${summary.objective_evaluated} 次达到工具目标`}
                label={en ? 'Objective success' : '目标成功率'}
                tone="objective"
                value={metricRate(summary.objective_successes, summary.objective_evaluated)}
              />
              <ToolMetricCard
                detail={en ? `${summary.timed_out_calls} timed out · ${summary.interrupted_calls} interrupted` : `${summary.timed_out_calls} 次超时 · ${summary.interrupted_calls} 次中断`}
                label={en ? 'Failed outcomes' : '目标失败'}
                tone={summary.failed_calls ? 'failure' : ''}
                value={String(summary.failed_calls)}
              />
            </div>

            {specializedMetrics(summary, en).length ? (
              <div className="specialized-metrics">
                {specializedMetrics(summary, en).map((item) => (
                  <div key={item.key}>
                    <span>{item.label}</span>
                    <strong>{metricRate(item.successes, item.attempts)}</strong>
                    <small>{metricFraction(item.successes, item.attempts)}</small>
                  </div>
                ))}
              </div>
            ) : null}

            {group.tools.length ? (
              <div className="tool-metrics-table-wrap">
                <table className="tool-metrics-table">
                  <thead>
                    <tr>
                      <th>{en ? 'Tool' : '工具'}</th>
                      <th>{en ? 'Calls' : '调用'}</th>
                      <th>{en ? 'Returned' : '返回'}</th>
                      <th>{en ? 'Valid' : '有效'}</th>
                      <th>{en ? 'Objective' : '目标成功'}</th>
                      <th>{en ? 'Timeout' : '超时'}</th>
                      <th>{en ? 'Retry' : '重试'}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.tools.map((tool) => (
                      <tr key={tool.name}>
                        <td><code>{tool.name}</code></td>
                        <td>{tool.total_calls}</td>
                        <td>{metricRate(tool.completed_calls, tool.total_calls)}</td>
                        <td>{metricRate(tool.protocol_successes, tool.protocol_evaluated)}</td>
                        <td>{metricRate(tool.objective_successes, tool.objective_evaluated)}</td>
                        <td>{tool.timed_out_calls}</td>
                        <td>{tool.retry_attempts}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="tool-metrics-empty">{en ? 'No tool calls for this Agent type.' : '这个 Agent 类型还没有工具调用。'}</p>
            )}

            <footer>
              {en
                ? 'Returned means a durable tool_result exists. Valid excludes malformed arguments and contract errors. Objective applies tool-specific checks: run_code must exit 0; write_paper/edit_paragraph must compile; verification submission must record a verdict. User interruption is not counted as an objective failure.'
                : '“已返回”表示存在持久化 tool_result；“有效”排除参数和协议错误；“目标成功”使用工具专属判定：run_code 必须 exit 0，write_paper/edit_paragraph 必须成功编译，验证提交必须记录有效 verdict。用户中断不计入目标失败。'}
            </footer>
          </>
        ) : null}
      </section>
    </details>
  )
})

function LoadingState({
  className = '',
  label,
}: {
  className?: string
  label: string
}) {
  return (
    <div
      aria-live="polite"
      className={`loading-state ${className}`}
      role="status"
    >
      <img aria-hidden="true" className="context-loading-ring" src={loadingRingUrl} />
      <span>{label}</span>
    </div>
  )
}

const RunSidebar = memo(function RunSidebar({
  loading,
  runs,
  selectedId,
  onSelect,
}: {
  loading: boolean
  runs: ContextRun[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  return (
    <aside className="run-sidebar" aria-label={en ? 'Modeling conversations' : '建模会话'}>
      <header className="rail-heading">
        <strong>{en ? 'CONVERSATIONS / RUNS' : '建模会话 / 运行'}</strong>
        <span>{loading ? (en ? 'Loading…' : '加载中…') : runs.length}</span>
      </header>
      <div className="run-scroll">
        {loading ? (
          <LoadingState
            className="run-loading"
            label={en ? 'Loading conversations…' : '正在加载会话…'}
          />
        ) : runs.length === 0 ? (
          <div className="rail-empty">{en ? 'No conversations yet.' : '还没有会话记录。'}</div>
        ) : runs.map((run) => (
          <button
            className={run.id === selectedId ? 'run-entry selected' : 'run-entry'}
            key={run.id}
            onClick={() => onSelect(run.id)}
            type="button"
          >
            <span className="run-name">{run.name}</span>
            <span className="run-status-line">
              <i className={`status-dot ${run.status}`} />
              {statusLabel(run.status, language)}
              <time>{formatClock(run.latest_request_ts || run.created, language)}</time>
            </span>
            <span className="run-meta">
              <span>{en ? `${run.request_count} requests` : `${run.request_count} 次请求`}</span>
              <span>{run.latest_model || (en ? 'No model requests yet' : '尚无模型请求')}</span>
            </span>
          </button>
        ))}
      </div>
      <footer className="local-note">
        <span className="privacy-dot" />
        {en ? 'Stored only in the local workspace' : '仅保存在本机 workspace'}
      </footer>
    </aside>
  )
})

function roleTone(role: string) {
  const lowered = role.toLowerCase()
  if (lowered.includes('verifier')) return 'verifier'
  if (lowered.includes('subagent')) return 'subagent'
  if (lowered.includes('router')) return 'router'
  if (lowered.includes('chat')) return 'chat'
  return 'main'
}

type AgentFilter = 'all' | 'main' | 'verifier' | 'subagent' | 'other'

function agentFilterForRole(role: string): Exclude<AgentFilter, 'all'> {
  const lowered = role.toLowerCase()
  if (lowered.includes('verifier') || lowered.includes('verification')) {
    return 'verifier'
  }
  if (lowered.includes('subagent') || lowered.includes('sub-agent')) {
    return 'subagent'
  }
  if (lowered.includes('main agent')) return 'main'
  return 'other'
}

const RequestTimeline = memo(function RequestTimeline({
  loading,
  requests,
  selectedId,
  onSelect,
}: {
  loading: boolean
  requests: ContextRequestSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [agentFilter, setAgentFilter] = useState<AgentFilter>('all')
  const agentCounts = useMemo(() => {
    const counts: Record<Exclude<AgentFilter, 'all'>, number> = {
      main: 0,
      verifier: 0,
      subagent: 0,
      other: 0,
    }
    for (const request of requests) {
      counts[agentFilterForRole(request.agent_role)] += 1
    }
    return counts
  }, [requests])
  const filteredRequests = useMemo(
    () => agentFilter === 'all'
      ? requests
      : requests.filter(
        (request) => agentFilterForRole(request.agent_role) === agentFilter,
      ),
    [agentFilter, requests],
  )
  const changeAgentFilter = useCallback((value: AgentFilter) => {
    setAgentFilter(value)
    const nextRequests = value === 'all'
      ? requests
      : requests.filter(
        (request) => agentFilterForRole(request.agent_role) === value,
      )
    if (
      nextRequests.length
      && !nextRequests.some((request) => request.request_id === selectedId)
    ) {
      onSelect(nextRequests[0].request_id)
    }
  }, [onSelect, requests, selectedId])
  const filterLabels: Record<AgentFilter, string> = en
    ? {
      all: 'All agents',
      main: 'Main Agent',
      verifier: 'Verifier',
      subagent: 'Sub-agent',
      other: 'Other agents',
    }
    : {
      all: '全部 Agent',
      main: '主 Agent',
      verifier: '验证 Agent',
      subagent: 'Sub-agent',
      other: '其他 Agent',
    }
  return (
    <aside className="request-rail" aria-label={en ? 'Model request timeline' : '模型请求时间线'}>
      <header className="rail-heading request-heading">
        <strong>{en ? 'REQUEST TIMELINE' : '请求时间线'}</strong>
        <div className="request-heading-controls">
          <select
            aria-label={en ? 'Filter requests by agent type' : '按 Agent 类型筛选请求'}
            onChange={(event) => changeAgentFilter(event.target.value as AgentFilter)}
            value={agentFilter}
          >
            <option value="all">{filterLabels.all}</option>
            {agentCounts.main ? <option value="main">{filterLabels.main}</option> : null}
            {agentCounts.verifier ? <option value="verifier">{filterLabels.verifier}</option> : null}
            {agentCounts.subagent ? <option value="subagent">{filterLabels.subagent}</option> : null}
            {agentCounts.other ? <option value="other">{filterLabels.other}</option> : null}
          </select>
          <span>{loading
            ? (en ? 'Loading…' : '加载中…')
            : agentFilter === 'all'
              ? requests.length
              : `${filteredRequests.length}/${requests.length}`}</span>
        </div>
      </header>
      <div className="request-columns" aria-hidden="true">
        <span>#</span><span>Agent</span><span>{en ? 'Model' : '模型'}</span><span>Tokens</span>
      </div>
      <div className="request-scroll">
        {loading ? (
          <LoadingState
            className="request-loading"
            label={en ? 'Loading request timeline…' : '正在加载请求时间线…'}
          />
        ) : requests.length === 0 ? (
          <div className="rail-empty request-empty">
            {en
              ? 'This conversation has not sent any model requests. Logging only captures requests made after it was enabled.'
              : '这个会话还没有发送新的模型请求。记录功能只捕获启用后的请求。'}
          </div>
        ) : filteredRequests.length === 0 ? (
          <div className="rail-empty request-empty">
            {en ? 'No requests match this agent type.' : '没有符合当前 Agent 类型的请求。'}
          </div>
        ) : filteredRequests.map((request) => {
          const actualTokens = request.usage.prompt_tokens
          return (
            <button
              className={request.request_id === selectedId ? 'request-entry selected' : 'request-entry'}
              key={request.request_id}
              onClick={() => onSelect(request.request_id)}
              type="button"
            >
              <span className="request-sequence">{request.sequence}</span>
              <span className="request-role">
                <i className={`role-dot ${roleTone(request.agent_role)}`} />
                <span>{request.agent_role}</span>
              </span>
              <span className="request-model" title={request.model}>{request.model}</span>
              <span className="request-tokens">
                {formatTokens(actualTokens || request.estimated_input_tokens)}
                {!actualTokens ? <small>≈</small> : null}
              </span>
              <span className={`request-state ${request.status}`} />
            </button>
          )
        })}
      </div>
    </aside>
  )
})

function CategoryFilter({
  active,
  category,
  onToggle,
}: {
  active: boolean
  category: ContextCategory
  onToggle: (category: ContextCategory) => void
}) {
  const { language } = useLanguage()
  const meta = categoryMeta[language][category]
  return (
    <button
      aria-pressed={active}
      className={`filter-button ${category} ${active ? 'active' : ''}`}
      onClick={() => onToggle(category)}
      type="button"
    >
      {meta.short}
    </button>
  )
}

const ContextRow = memo(function ContextRow({
  item,
  index,
  open,
  onToggle,
}: {
  item: ContextItem
  index: number
  open: boolean
  onToggle: () => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [copied, setCopied] = useState(false)
  const rendered = stringifyContent(item.content)
  const structured = typeof item.content !== 'string'
  const copy = useCallback(async () => {
    await navigator.clipboard.writeText(rendered)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }, [rendered])

  return (
    <article className={`context-row ${item.category} ${open ? 'expanded' : ''}`}>
      <button
        aria-expanded={open}
        className="row-toggle"
        onClick={onToggle}
        type="button"
      >
        <span className="row-number">{index + 1}</span>
        <span className="category-tag">{categoryMeta[language][item.category].short}</span>
        <span className="row-title">
          <strong>{item.label}</strong>
          {item.source ? <small>{item.source}</small> : null}
        </span>
        <span className="token-estimate">≈ {formatTokens(item.estimated_tokens)}</span>
        <Chevron open={open} />
      </button>
      <div className={open ? 'row-content open' : 'row-content'}>
        <pre className={structured ? 'structured' : ''}>{rendered || (en ? '(empty)' : '（空内容）')}</pre>
        <button className="copy-button" onClick={copy} title={en ? 'Copy this context block' : '复制这段 Context'} type="button">
          <CopyIcon />
          <span>{copied ? (en ? 'Copied' : '已复制') : (en ? 'Copy' : '复制')}</span>
        </button>
      </div>
    </article>
  )
})

function EmptyDetail() {
  const { language } = useLanguage()
  const en = language === 'en'
  return (
    <main className="detail-empty">
      <InspectorMark />
      <h2>{en ? 'Waiting for the first context record' : '等待第一条 Context 记录'}</h2>
      <p>{en
        ? 'After a message is sent from the modeling workspace, the complete context submitted to the model API will appear here.'
        : '在数学建模页面发送消息后，这里会显示实际提交给模型 API 的完整上下文。'}</p>
    </main>
  )
}

function DetailLoading({ label }: { label: string }) {
  return (
    <main className="detail-loading">
      <LoadingState className="context-loading" label={label} />
    </main>
  )
}

function RequestDetail({
  detail,
  runId,
  runName,
  categories,
  search,
  raw,
  onRawChange,
}: {
  detail: ContextRequestDetail
  runId: string
  runName: string
  categories: Set<ContextCategory>
  search: string
  raw: boolean
  onRawChange: (value: boolean) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const deferredSearch = useDeferredValue(search.trim().toLowerCase())
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set([0]))
  const filteredItems = useMemo(() => detail.items.filter((item) => {
    if (!categories.has(item.category)) return false
    if (!deferredSearch) return true
    return (
      item.label.toLowerCase().includes(deferredSearch)
      || String(item.source || '').toLowerCase().includes(deferredSearch)
      || stringifyContent(item.content).toLowerCase().includes(deferredSearch)
    )
  }), [categories, deferredSearch, detail.items])

  useEffect(() => {
    setExpanded(new Set([0]))
  }, [detail.request_id])

  const usage = detail.usage
  const inputTokens = usage.prompt_tokens || detail.estimated_input_tokens
  const isEstimated = !usage.prompt_tokens

  return (
    <main className="detail-canvas">
      <header className="request-header">
        <div>
          <span className="request-kicker">{en ? `Request #${detail.sequence}` : `请求 #${detail.sequence}`}</span>
          <h1>{detail.agent_role}</h1>
          <p>
            {detail.phase}
            {detail.step ? (en ? ` · Step ${detail.step}` : ` · 第 ${detail.step} 步`) : ''}
            {' · '}
            {formatDate(detail.ts, language)}
          </p>
        </div>
        <dl className="request-facts">
          <div><dt>{en ? 'Model' : '模型'}</dt><dd>{detail.model}</dd></div>
          <div><dt>{en ? 'Input tokens' : '输入 Tokens'}</dt><dd>{isEstimated ? '≈ ' : ''}{formatTokens(inputTokens)}</dd></div>
          <div><dt>{en ? 'Output tokens' : '输出 Tokens'}</dt><dd>{formatTokens(usage.completion_tokens)}</dd></div>
          <div><dt>{en ? 'Duration' : '耗时'}</dt><dd>{detail.duration_seconds ? `${detail.duration_seconds.toFixed(2)}s` : (en ? 'In progress' : '进行中')}</dd></div>
        </dl>
        <div className="header-actions">
          <ToolMetricsPanel key={runId} runId={runId} runName={runName} />
          <ToolCallStats items={detail.items} />
          <button
            aria-pressed={raw}
            className={raw ? 'raw-button active' : 'raw-button'}
            onClick={() => onRawChange(!raw)}
            type="button"
          >
            {'{ }'} {en ? 'Raw JSON' : '原始 JSON'}
          </button>
        </div>
      </header>

      {detail.status === 'error' && detail.error ? (
        <div className="request-error">{detail.error}</div>
      ) : null}

      <section className="context-scroll" aria-label={en ? 'Request context content' : '请求 Context 内容'}>
        {raw ? (
          <article className="raw-json">
            <pre>{JSON.stringify(detail.raw_request, null, 2)}</pre>
          </article>
        ) : filteredItems.length ? (
          filteredItems.map((item) => {
            const sourceIndex = detail.items.indexOf(item)
            return (
              <ContextRow
                index={sourceIndex}
                item={item}
                key={`${sourceIndex}-${item.category}-${item.label}`}
                onToggle={() => setExpanded((current) => {
                  const next = new Set(current)
                  if (next.has(sourceIndex)) next.delete(sourceIndex)
                  else next.add(sourceIndex)
                  return next
                })}
                open={expanded.has(sourceIndex)}
              />
            )
          })
        ) : (
          <div className="filter-empty">{en ? 'No context matches the current filters.' : '当前筛选条件下没有 Context 内容。'}</div>
        )}
      </section>
      <footer className="request-footer">
        <span>{en ? 'Input' : '输入'} {formatTokens(usage.prompt_tokens || detail.estimated_input_tokens)}{isEstimated ? (en ? ' (estimated)' : '（估算）') : ''}</span>
        <span>{en ? 'Output' : '输出'} {formatTokens(usage.completion_tokens)}</span>
        <span>{en ? 'Messages' : '消息'} {detail.message_count}</span>
        <span>{en ? 'Tool definitions' : '工具定义'} {detail.tool_definition_count}</span>
        <code>{detail.request_id}</code>
      </footer>
    </main>
  )
}

export default function App() {
  const { language } = useLanguage()
  const en = language === 'en'
  const [runs, setRuns] = useState<ContextRun[]>([])
  const [requests, setRequests] = useState<ContextRequestSummary[]>([])
  const [detail, setDetail] = useState<ContextRequestDetail | null>(null)
  const [runsLoading, setRunsLoading] = useState(true)
  const [requestsLoading, setRequestsLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [selectedRun, setSelectedRun] = useState<string | null>(null)
  const [selectedRequest, setSelectedRequest] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [search, setSearch] = useState('')
  const [raw, setRaw] = useState(false)
  const [categories, setCategories] = useState<Set<ContextCategory>>(
    () => new Set(filterCategories),
  )
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const selectedRunLatestRequestTs = runs.find(
    (run) => run.id === selectedRun,
  )?.latest_request_ts ?? null
  const selectedRequestResponseTs = requests.find(
    (request) => request.request_id === selectedRequest,
  )?.response_ts ?? null

  const loadRuns = useCallback(async (signal?: AbortSignal) => {
    const next = await fetchRuns(signal)
    setRuns(next)
    setSelectedRun((current) => {
      if (current && next.some((run) => run.id === current)) return current
      return next.find((run) => run.request_count > 0)?.id || next[0]?.id || null
    })
    setLastUpdated(Date.now() / 1000)
  }, [])

  useEffect(() => {
    let cancelled = false
    let controller: AbortController | null = null
    let timer: number | null = null

    const refresh = async () => {
      controller = new AbortController()
      try {
        await loadRuns(controller.signal)
        if (!cancelled) setRunsLoading(false)
      } catch (reason) {
        const refreshError = reason as Error
        if (!cancelled && refreshError.name !== 'AbortError') {
          setRunsLoading(false)
          setError(refreshError.message)
        }
      } finally {
        if (!cancelled && autoRefresh) {
          timer = window.setTimeout(refresh, 2500)
        }
      }
    }

    void refresh()
    return () => {
      cancelled = true
      controller?.abort()
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [autoRefresh, loadRuns])

  useEffect(() => {
    if (!selectedRun) {
      setRequests([])
      setSelectedRequest(null)
      setRequestsLoading(false)
      return
    }
    const controller = new AbortController()
    setRequestsLoading(true)
    fetchRequests(selectedRun, controller.signal)
      .then((next) => {
        startTransition(() => {
          setRequests(next)
          setSelectedRequest((current) => (
            current && next.some((request) => request.request_id === current)
              ? current
              : next[0]?.request_id || null
          ))
          setRequestsLoading(false)
        })
      })
      .catch((reason: Error) => {
        if (reason.name !== 'AbortError') {
          setRequestsLoading(false)
          setError(reason.message)
        }
      })
    return () => controller.abort()
  }, [selectedRun, selectedRunLatestRequestTs])

  useEffect(() => {
    if (!selectedRun || !selectedRequest) {
      setDetail(null)
      setDetailLoading(false)
      return
    }
    const controller = new AbortController()
    setDetailLoading(true)
    fetchRequestDetail(selectedRun, selectedRequest, controller.signal)
      .then((next) => startTransition(() => {
        setDetail(next)
        setDetailLoading(false)
        setError(null)
      }))
      .catch((reason: Error) => {
        if (reason.name !== 'AbortError') {
          setDetailLoading(false)
          setError(reason.message)
        }
      })
    return () => controller.abort()
  }, [selectedRequest, selectedRequestResponseTs, selectedRun])

  const selectRun = useCallback((id: string) => {
    if (id === selectedRun) return
    setSelectedRun(id)
    setRequests([])
    setSelectedRequest(null)
    setDetail(null)
    setRequestsLoading(true)
    setDetailLoading(false)
    setError(null)
  }, [selectedRun])

  const selectRequest = useCallback((id: string) => {
    if (id === selectedRequest) return
    setSelectedRequest(id)
    setDetail(null)
    setDetailLoading(true)
    setError(null)
  }, [selectedRequest])

  const toggleCategory = useCallback((category: ContextCategory) => {
    setCategories((current) => {
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }, [])

  return (
    <div className="inspector-shell">
      <header className="app-header">
        <div className="brand">
          <InspectorMark />
          <strong>Context Inspector</strong>
        </div>
        <div className="connection-state">
          <span className="live-dot" />
          <span>{en ? 'Connected' : '已连接'}</span>
          <code>localhost:8766</code>
          <span>{en ? 'Updated' : '最后更新'} {formatClock(lastUpdated, language)}</span>
        </div>
        <label className="refresh-switch">
          <span>{en ? 'Auto refresh' : '自动刷新'}</span>
          <input
            checked={autoRefresh}
            onChange={(event) => setAutoRefresh(event.target.checked)}
            type="checkbox"
          />
          <i />
        </label>
        <div className="category-filters">
          <span>{en ? 'Categories' : '类别筛选'}</span>
          <button
            aria-pressed={categories.size === filterCategories.length}
            className={categories.size === filterCategories.length ? 'filter-button all active' : 'filter-button all'}
            onClick={() => setCategories(
              categories.size === filterCategories.length
                ? new Set()
                : new Set(filterCategories),
            )}
            type="button"
          >
            {en ? 'All' : '全部'}
          </button>
          {filterCategories.map((category) => (
            <CategoryFilter
              active={categories.has(category)}
              category={category}
              key={category}
              onToggle={toggleCategory}
            />
          ))}
        </div>
        <label className="context-search">
          <svg aria-hidden="true" viewBox="0 0 20 20">
            <circle cx="8.5" cy="8.5" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="m12 12 4 4" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" />
          </svg>
          <input
            onChange={(event) => setSearch(event.target.value)}
            placeholder={en ? 'Search messages, tools, or content…' : '搜索消息、工具名或内容…'}
            value={search}
          />
        </label>
        {detail && selectedRun ? (
          <a
            className="download-button"
            href={exportUrl(selectedRun, detail.request_id)}
            title={en ? 'Export raw request JSON' : '导出原始请求 JSON'}
          >
            <DownloadIcon />
            {en ? 'Export' : '导出'}
          </a>
        ) : null}
        <LanguageSwitcher className="context-language-switcher" />
      </header>

      {error ? (
        <button className="global-error" onClick={() => setError(null)} type="button">
          {error}
        </button>
      ) : null}

      <div className="workspace-grid">
        <RunSidebar
          loading={runsLoading}
          onSelect={selectRun}
          runs={runs}
          selectedId={selectedRun}
        />
        <RequestTimeline
          key={selectedRun || 'no-run'}
          loading={requestsLoading}
          onSelect={selectRequest}
          requests={requests}
          selectedId={selectedRequest}
        />
        {detailLoading ? (
          <DetailLoading label={en ? 'Loading context…' : '正在加载 Context…'} />
        ) : detail ? (
          <RequestDetail
            categories={categories}
            detail={detail}
            onRawChange={setRaw}
            raw={raw}
            runId={selectedRun || ''}
            runName={runs.find((run) => run.id === selectedRun)?.name || selectedRun || ''}
            search={search}
          />
        ) : requestsLoading ? (
          <DetailLoading label={en ? 'Loading conversation requests…' : '正在加载会话请求…'} />
        ) : <EmptyDetail />}
      </div>
    </div>
  )
}
