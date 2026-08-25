import { useMemo, useState } from 'react'
import type { RunSummary } from '../types'
import { timestampLabel } from '../helpers'
import { useLanguage } from '../i18n'

interface SidebarProps {
  runs: RunSummary[]
  activeRunId: string | null
  onSelect: (runId: string) => void
  onNewTask: () => void
  onOpenSettings: () => void
  onDelete: (runId: string) => void
  newTaskDisabled: boolean
  newTaskHint: string
  deletingRunId: string | null
}

export function Sidebar({ runs, activeRunId, onSelect, onNewTask, onOpenSettings, onDelete, newTaskDisabled, newTaskHint, deletingRunId }: SidebarProps) {
  const { language } = useLanguage()
  const en = language === 'en'
  const publicDeployment = import.meta.env.VITE_PUBLIC_DEPLOYMENT === 'true'
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'all' | 'active' | 'done'>('all')
  const visibleRuns = useMemo(() => runs.filter((run) => {
    const matchesQuery = run.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
    const isActive = run.status === 'running' || run.status === 'waiting_input' || run.status === 'draft'
    return matchesQuery && (filter === 'all' || (filter === 'active' ? isActive : !isActive))
  }), [runs, query, filter])

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">M</div>
        <div>
          <strong>mathmodel</strong>
          <span>Agent workspace</span>
        </div>
      </div>

      <button className="new-task-button" onClick={onNewTask} disabled={newTaskDisabled} title={newTaskHint}>
        <span>＋</span> {en ? 'New project' : '新建项目'}
      </button>
      {newTaskDisabled && <p className="new-task-hint">{newTaskHint}</p>}

      <div className="project-browser-tools">
        <label className="project-search">
          <span>⌕</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={en ? 'Search projects' : '搜索项目'} />
        </label>
        <div className="project-filters" data-active-filter={filter} role="group" aria-label={en ? 'Project filter' : '项目筛选'}>
          <span className="project-filter-indicator" aria-hidden="true" />
          {(['all', 'active', 'done'] as const).map((value) => <button key={value} aria-pressed={filter === value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>
            {value === 'all' ? (en ? 'All' : '全部') : value === 'active' ? (en ? 'Active' : '进行中') : (en ? 'Delivered' : '已交付')}
          </button>)}
        </div>
      </div>

      <div className="sidebar-heading">
        <span>{en ? 'Projects' : '项目'}</span>
        <span>{runs.length}</span>
      </div>
      <nav className="run-list" aria-label={en ? 'Projects' : '项目'}>
        {visibleRuns.length === 0 ? (
          <p className="sidebar-empty">{runs.length ? (en ? 'No matching projects' : '没有匹配的项目') : (en ? 'No projects yet' : '还没有项目')}</p>
        ) : visibleRuns.map((run) => (
          <div
            key={run.id}
            className={`run-row-shell ${run.id === activeRunId ? 'is-active' : ''}`}
          >
            <button className="run-row" onClick={() => onSelect(run.id)} title={run.name}>
              <span className={`status-dot ${run.status}`} />
              <span className="run-copy">
                <strong>{run.name}</strong>
                <small>{timestampLabel(run.created)}</small>
              </span>
            </button>
            <button
              className="delete-run-button"
              aria-label={en ? `Delete project: ${run.name}` : `删除项目：${run.name}`}
              title={run.status === 'running' || run.status === 'waiting_input'
                ? (en ? 'This project is active and cannot be deleted yet' : '项目进行中，暂不能删除')
                : (en ? 'Delete project' : '删除项目')}
              disabled={run.status === 'running' || run.status === 'waiting_input' || deletingRunId === run.id}
              onClick={() => onDelete(run.id)}
            >
              {deletingRunId === run.id ? '…' : '×'}
            </button>
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="local-status"><span className="footer-pulse" /> {publicDeployment ? (en ? 'Service online' : '服务在线') : (en ? 'Running locally' : '本地运行中')}</span>
        <button type="button" className="settings-button" onClick={onOpenSettings} aria-label={en ? 'Open settings' : '打开设置'}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21H9.6v-.09A1.7 1.7 0 0 0 8.5 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3V9.6h.09A1.7 1.7 0 0 0 4.6 8.5a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.09A1.7 1.7 0 0 0 15.5 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.4.36.75.65 1 .3.25.68.39 1.07.4H21v4h-.09A1.7 1.7 0 0 0 19.4 15Z" />
          </svg>
          <span>{en ? 'Settings' : '设置'}</span>
        </button>
      </div>
    </aside>
  )
}
