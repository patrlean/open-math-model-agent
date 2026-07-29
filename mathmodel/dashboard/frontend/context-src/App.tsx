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
} from './types'

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

const RunSidebar = memo(function RunSidebar({
  runs,
  selectedId,
  onSelect,
}: {
  runs: ContextRun[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <aside className="run-sidebar" aria-label="建模会话">
      <header className="rail-heading">
        <strong>建模会话 / 运行</strong>
        <span>{runs.length}</span>
      </header>
      <div className="run-scroll">
        {runs.length === 0 ? (
          <div className="rail-empty">还没有会话记录。</div>
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
              {statusLabel(run.status)}
              <time>{formatClock(run.latest_request_ts || run.created)}</time>
            </span>
            <span className="run-meta">
              <span>{run.request_count} 次请求</span>
              <span>{run.latest_model || '尚无模型请求'}</span>
            </span>
          </button>
        ))}
      </div>
      <footer className="local-note">
        <span className="privacy-dot" />
        仅保存在本机 workspace
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

const RequestTimeline = memo(function RequestTimeline({
  requests,
  selectedId,
  onSelect,
}: {
  requests: ContextRequestSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <aside className="request-rail" aria-label="模型请求时间线">
      <header className="rail-heading request-heading">
        <strong>请求时间线</strong>
        <span>{requests.length} 个请求</span>
      </header>
      <div className="request-columns" aria-hidden="true">
        <span>#</span><span>Agent</span><span>模型</span><span>Tokens</span>
      </div>
      <div className="request-scroll">
        {requests.length === 0 ? (
          <div className="rail-empty request-empty">
            这个会话还没有发送新的模型请求。记录功能只捕获启用后的请求。
          </div>
        ) : requests.map((request) => {
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
  const meta = categoryMeta[category]
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
        <span className="category-tag">{categoryMeta[item.category].short}</span>
        <span className="row-title">
          <strong>{item.label}</strong>
          {item.source ? <small>{item.source}</small> : null}
        </span>
        <span className="token-estimate">≈ {formatTokens(item.estimated_tokens)}</span>
        <Chevron open={open} />
      </button>
      <div className={open ? 'row-content open' : 'row-content'}>
        <pre className={structured ? 'structured' : ''}>{rendered || '（空内容）'}</pre>
        <button className="copy-button" onClick={copy} title="复制这段 Context" type="button">
          <CopyIcon />
          <span>{copied ? '已复制' : '复制'}</span>
        </button>
      </div>
    </article>
  )
})

function EmptyDetail() {
  return (
    <main className="detail-empty">
      <InspectorMark />
      <h2>等待第一条 Context 记录</h2>
      <p>在数学建模页面发送消息后，这里会显示实际提交给模型 API 的完整上下文。</p>
    </main>
  )
}

function RequestDetail({
  detail,
  categories,
  search,
  raw,
  onRawChange,
}: {
  detail: ContextRequestDetail
  categories: Set<ContextCategory>
  search: string
  raw: boolean
  onRawChange: (value: boolean) => void
}) {
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
          <span className="request-kicker">请求 #{detail.sequence}</span>
          <h1>{detail.agent_role}</h1>
          <p>
            {detail.phase}
            {detail.step ? ` · 第 ${detail.step} 步` : ''}
            {' · '}
            {formatDate(detail.ts)}
          </p>
        </div>
        <dl className="request-facts">
          <div><dt>模型</dt><dd>{detail.model}</dd></div>
          <div><dt>输入 Tokens</dt><dd>{isEstimated ? '≈ ' : ''}{formatTokens(inputTokens)}</dd></div>
          <div><dt>输出 Tokens</dt><dd>{formatTokens(usage.completion_tokens)}</dd></div>
          <div><dt>耗时</dt><dd>{detail.duration_seconds ? `${detail.duration_seconds.toFixed(2)}s` : '进行中'}</dd></div>
        </dl>
        <div className="header-actions">
          <button
            aria-pressed={raw}
            className={raw ? 'raw-button active' : 'raw-button'}
            onClick={() => onRawChange(!raw)}
            type="button"
          >
            {'{ }'} 原始 JSON
          </button>
        </div>
      </header>

      {detail.status === 'error' && detail.error ? (
        <div className="request-error">{detail.error}</div>
      ) : null}

      <section className="context-scroll" aria-label="请求 Context 内容">
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
          <div className="filter-empty">当前筛选条件下没有 Context 内容。</div>
        )}
      </section>
      <footer className="request-footer">
        <span>输入 {formatTokens(usage.prompt_tokens || detail.estimated_input_tokens)}{isEstimated ? '（估算）' : ''}</span>
        <span>输出 {formatTokens(usage.completion_tokens)}</span>
        <span>消息 {detail.message_count}</span>
        <span>工具定义 {detail.tool_definition_count}</span>
        <code>{detail.request_id}</code>
      </footer>
    </main>
  )
}

export default function App() {
  const [runs, setRuns] = useState<ContextRun[]>([])
  const [requests, setRequests] = useState<ContextRequestSummary[]>([])
  const [detail, setDetail] = useState<ContextRequestDetail | null>(null)
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

  const loadRuns = useCallback(async (signal?: AbortSignal) => {
    const next = await fetchRuns(signal)
    startTransition(() => {
      setRuns(next)
      setSelectedRun((current) => {
        if (current && next.some((run) => run.id === current)) return current
        return next.find((run) => run.request_count > 0)?.id || next[0]?.id || null
      })
      setLastUpdated(Date.now() / 1000)
    })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    loadRuns(controller.signal).catch((reason: Error) => setError(reason.message))
    return () => controller.abort()
  }, [loadRuns])

  useEffect(() => {
    if (!autoRefresh) return
    const timer = window.setInterval(() => {
      loadRuns().catch((reason: Error) => setError(reason.message))
    }, 2500)
    return () => window.clearInterval(timer)
  }, [autoRefresh, loadRuns])

  useEffect(() => {
    if (!selectedRun) {
      setRequests([])
      setSelectedRequest(null)
      return
    }
    const controller = new AbortController()
    fetchRequests(selectedRun, controller.signal)
      .then((next) => {
        startTransition(() => {
          setRequests(next)
          setSelectedRequest((current) => (
            current && next.some((request) => request.request_id === current)
              ? current
              : next[0]?.request_id || null
          ))
        })
      })
      .catch((reason: Error) => {
        if (reason.name !== 'AbortError') setError(reason.message)
      })
    return () => controller.abort()
  }, [selectedRun, runs])

  useEffect(() => {
    if (!selectedRun || !selectedRequest) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    fetchRequestDetail(selectedRun, selectedRequest, controller.signal)
      .then((next) => startTransition(() => {
        setDetail(next)
        setError(null)
      }))
      .catch((reason: Error) => {
        if (reason.name !== 'AbortError') setError(reason.message)
      })
    return () => controller.abort()
  }, [selectedRequest, selectedRun, requests])

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
          <span>已连接</span>
          <code>localhost:8766</code>
          <span>最后更新 {formatClock(lastUpdated)}</span>
        </div>
        <label className="refresh-switch">
          <span>自动刷新</span>
          <input
            checked={autoRefresh}
            onChange={(event) => setAutoRefresh(event.target.checked)}
            type="checkbox"
          />
          <i />
        </label>
        <div className="category-filters">
          <span>类别筛选</span>
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
            全部
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
            placeholder="搜索消息、工具名或内容…"
            value={search}
          />
        </label>
        {detail && selectedRun ? (
          <a
            className="download-button"
            href={exportUrl(selectedRun, detail.request_id)}
            title="导出原始请求 JSON"
          >
            <DownloadIcon />
            导出
          </a>
        ) : null}
      </header>

      {error ? (
        <button className="global-error" onClick={() => setError(null)} type="button">
          {error}
        </button>
      ) : null}

      <div className="workspace-grid">
        <RunSidebar
          onSelect={(id) => {
            setSelectedRun(id)
            setSelectedRequest(null)
          }}
          runs={runs}
          selectedId={selectedRun}
        />
        <RequestTimeline
          onSelect={setSelectedRequest}
          requests={requests}
          selectedId={selectedRequest}
        />
        {detail ? (
          <RequestDetail
            categories={categories}
            detail={detail}
            onRawChange={setRaw}
            raw={raw}
            search={search}
          />
        ) : <EmptyDetail />}
      </div>
    </div>
  )
}
