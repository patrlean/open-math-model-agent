import type { RunSummary } from '../types'
import { statusLabel, timestampLabel } from '../helpers'

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
        <span>＋</span> 发起新会话
      </button>
      {newTaskDisabled && <p className="new-task-hint">{newTaskHint}</p>}

      <div className="sidebar-heading">
        <span>历史会话</span>
        <span>{runs.length}</span>
      </div>
      <nav className="run-list" aria-label="历史会话">
        {runs.length === 0 ? (
          <p className="sidebar-empty">还没有运行记录</p>
        ) : runs.map((run) => (
          <div
            key={run.id}
            className={`run-row-shell ${run.id === activeRunId ? 'is-active' : ''}`}
          >
            <button className="run-row" onClick={() => onSelect(run.id)}>
              <span className={`status-dot ${run.status}`} />
              <span className="run-copy">
                <strong>{run.name}</strong>
                <small>{timestampLabel(run.created)}</small>
              </span>
              <time>{statusLabel[run.status]}</time>
            </button>
            <button
              className="delete-run-button"
              aria-label={`删除会话：${run.name}`}
              title={run.status === 'running' || run.status === 'waiting_input' ? '会话进行中，暂不能删除' : '删除会话'}
              disabled={run.status === 'running' || run.status === 'waiting_input' || deletingRunId === run.id}
              onClick={() => onDelete(run.id)}
            >
              {deletingRunId === run.id ? '…' : '×'}
            </button>
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="local-status"><span className="footer-pulse" /> 本地运行中</span>
        <button type="button" className="settings-button" onClick={onOpenSettings} aria-label="打开设置">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21H9.6v-.09A1.7 1.7 0 0 0 8.5 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3V9.6h.09A1.7 1.7 0 0 0 4.6 8.5a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.09A1.7 1.7 0 0 0 15.5 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.14.4.36.75.65 1 .3.25.68.39 1.07.4H21v4h-.09A1.7 1.7 0 0 0 19.4 15Z" />
          </svg>
          <span>设置</span>
        </button>
      </div>
    </aside>
  )
}
