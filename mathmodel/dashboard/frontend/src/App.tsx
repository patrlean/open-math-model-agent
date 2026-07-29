import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { ColumnResizer } from './components/ColumnResizer'
import { Inspector, type InspectorTab } from './components/Inspector'
import { RunHeader } from './components/RunHeader'
import { RunTimeline } from './components/RunTimeline'
import { Sidebar } from './components/Sidebar'
import { SettingsDialog } from './components/SettingsDialog'
import { TaskComposer } from './components/TaskComposer'
import { useResizableColumns } from './hooks/useResizableColumns'
import type { RunDetail, RunSummary } from './types'

// Statuses from which sending a message continues the same conversation
// (via /api/continue) instead of starting a brand-new one.
const CONTINUABLE_STATUSES: RunDetail['status'][] = ['done', 'error', 'stopped', 'cancelled']

function asDataUrl(file: File): Promise<{ name: string; b64: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve({ name: file.name, b64: String(reader.result) })
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

export function App() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [creatingDraft, setCreatingDraft] = useState(false)
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('协作')
  const [error, setError] = useState<string | null>(null)
  const columns = useResizableColumns()
  // Keep a stopped run locally cancelled until the next continuation begins.
  // This also rejects a slow polling response that started before the stop and
  // arrives afterward with the stale "running" status.
  const pendingStopId = useRef<string | null>(null)

  const loadRuns = useCallback(async () => {
    try {
      const items = await api.listRuns()
      const patched = items.map((item) => {
        if (pendingStopId.current !== item.id) return item
        if (item.status === 'running' || item.status === 'waiting_input') {
          return { ...item, status: 'cancelled' as const }
        }
        return item
      })
      setRuns(patched)
      setActiveRunId((current) => current ?? patched[0]?.id ?? null)
      setError(null)
    } catch {
      setError('无法连接到 dashboard 服务。请确认 Python 服务仍在运行。')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadRun = useCallback(async (id: string) => {
    try {
      const detail = await api.run(id)
      if (pendingStopId.current === id) {
        if (detail.status === 'running' || detail.status === 'waiting_input') {
          detail.status = 'cancelled'
        }
      }
      setRun(detail)
      setError(null)
    } catch {
      setError('读取这次会话的实时记录时出现问题。')
    }
  }, [])

  useEffect(() => {
    void loadRuns()
    const timer = window.setInterval(() => void loadRuns(), 4000)
    return () => window.clearInterval(timer)
  }, [loadRuns])

  useEffect(() => {
    if (!activeRunId) {
      setRun(null)
      return
    }
    void loadRun(activeRunId)
    const timer = window.setInterval(() => void loadRun(activeRunId), 2000)
    return () => window.clearInterval(timer)
  }, [activeRunId, loadRun])

  async function continueRun(current: RunDetail, input: { task: string; files: File[] }) {
    try {
      // A new continuation is a new generation; stale polling from the stopped
      // generation no longer needs the local cancelled overlay.
      pendingStopId.current = null
      const files = await Promise.all(input.files.map(asDataUrl))
      await api.continueRun({ id: current.id, task: input.task, files })
      // The conversation stays in the same run_id/workdir -- just flip it back
      // to "running" optimistically and let polling pick up the real events.
      setRun((prev) => (prev && prev.id === current.id ? { ...prev, status: 'running', failure_reason: undefined } : prev))
      setRuns((items) => items.map((item) => (item.id === current.id ? { ...item, status: 'running', failure_reason: undefined } : item)))
      void loadRuns()
      void loadRun(current.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '继续会话失败，请重试。')
    }
  }

  async function submitTask(input: { task: string; files: File[] }) {
    if (run && CONTINUABLE_STATUSES.includes(run.status)) {
      setSubmitting(true)
      try {
        await continueRun(run, input)
      } finally {
        setSubmitting(false)
      }
      return
    }
    setSubmitting(true)
    try {
      const files = await Promise.all(input.files.map(asDataUrl))
      const created = await api.launch({ task: input.task, files, run_id: run?.status === 'draft' ? run.id : undefined })
      const createdAt = Date.now() / 1000
      const optimisticRun: RunDetail = {
        id: created.id,
        name: created.name,
        task: input.task,
        files: input.files.map((file) => file.name),
        created: createdAt,
        status: 'running',
        has_pdf: false,
        plan: '',
        plan_tasks: [],
        problem: '',
        decisions: '',
        results: {},
        figures: [],
        outputs: [],
        paper: {},
        events: [],
        run_log: '',
      }
      setActiveRunId(created.id)
      setRun(optimisticRun)
      setRuns((items) => [optimisticRun, ...items.filter((item) => item.id !== created.id)])
      void loadRuns()
      void loadRun(created.id)
    } catch {
      setError('新任务没有成功创建，请检查服务日志后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  async function retryRun() {
    if (!run) return
    setSubmitting(true)
    try {
      const created = await api.retry(run.id)
      const createdAt = Date.now() / 1000
      const retry: RunDetail = {
        id: created.id,
        name: created.name,
        task: run.task,
        files: run.files,
        created: createdAt,
        status: 'running',
        has_pdf: false,
        plan: '',
        plan_tasks: [],
        problem: '',
        decisions: '',
        results: {},
        figures: [],
        outputs: [],
        paper: {},
        events: [],
        run_log: '',
        retry_of: run.id,
      }
      setActiveRunId(created.id)
      setRun(retry)
      setRuns((items) => [retry, ...items.filter((item) => item.id !== created.id)])
      void loadRuns()
      void loadRun(created.id)
    } catch {
      setError('无法重新开始此任务，请检查本地服务后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  async function stopRun() {
    if (!run || stopping) return
    const targetId = run.id
    // Make the UI terminal at click time. The server now invalidates the old
    // generation immediately, so no artificial settling delay is required.
    pendingStopId.current = targetId
    setRun((prev) => (prev && prev.id === targetId ? { ...prev, status: 'cancelled' } : prev))
    setRuns((items) => items.map((item) => (item.id === targetId ? { ...item, status: 'cancelled' } : item)))
    setStopping(true)
    try {
      await api.stop(targetId)
      void loadRun(targetId)
    } catch (caught) {
      pendingStopId.current = null
      void loadRun(targetId)
      setError(caught instanceof Error ? caught.message : '停止会话失败，请重试。')
    } finally {
      setStopping(false)
    }
  }

  function selectRun(id: string) {
    setSettingsOpen(false)
    setActiveRunId(id)
  }

  function focusComposer() {
    window.requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus())
  }

  async function createDraft() {
    const draft = runs.find((item) => item.status === 'draft')
    if (draft || creatingDraft) return
    setCreatingDraft(true)
    try {
      const created = await api.createDraft()
      const createdAt = Date.now() / 1000
      const draft: RunDetail = {
        id: created.id,
        name: created.name,
        task: '',
        created: createdAt,
        status: 'draft',
        has_pdf: false,
        plan: '',
        plan_tasks: [],
        problem: '',
        decisions: '',
        results: {},
        figures: [],
        outputs: [],
        paper: {},
        events: [],
        run_log: '',
      }
      setActiveRunId(created.id)
      setRun(draft)
      setRuns((items) => [draft, ...items])
      focusComposer()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '新的会话没有创建成功，请重试。')
    } finally {
      setCreatingDraft(false)
    }
  }

  async function deleteRun(id: string) {
    const target = runs.find((item) => item.id === id)
    if (!target || target.status === 'running' || deletingRunId) return
    if (!window.confirm(`删除“${target.name}”吗？此操作会删除这次会话的记录和文件，无法恢复。`)) return

    setDeletingRunId(id)
    try {
      await api.deleteRun(id)
      const remaining = runs.filter((item) => item.id !== id)
      setRuns(remaining)
      if (activeRunId === id) {
        setRun(null)
        setActiveRunId(remaining[0]?.id ?? null)
      }
      void loadRuns()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除会话失败，请重试。')
    } finally {
      setDeletingRunId(null)
    }
  }

  const unsubmittedDraft = runs.find((item) => item.status === 'draft')
  const newTaskDisabled = Boolean(unsubmittedDraft) || creatingDraft
  const newTaskHint = unsubmittedDraft
    ? '请先发送当前会话的建模请求，或删除该空白会话。'
    : creatingDraft
      ? '正在创建新的会话…'
      : '创建新的会话'
  const runIsActive = run?.status === 'running' || run?.status === 'waiting_input'

  return (
    <div ref={columns.shellRef} className="dashboard-shell" style={columns.style}>
      <Sidebar
        runs={runs}
        activeRunId={activeRunId}
        onSelect={selectRun}
        onNewTask={() => void createDraft()}
        onOpenSettings={() => setSettingsOpen(true)}
        onDelete={(id) => void deleteRun(id)}
        newTaskDisabled={newTaskDisabled}
        newTaskHint={newTaskHint}
        deletingRunId={deletingRunId}
      />
      <ColumnResizer
        column="left"
        value={columns.widths.left}
        onPointerDown={columns.startResize}
        onReset={columns.resetColumn}
        onKeyboardResize={columns.resizeWithKeyboard}
      />
      <main className="workbench">
        {run ? (
          <>
            <RunHeader run={run} onRetry={retryRun} retrying={submitting} />
            <div className="run-canvas" key={run.id}><RunTimeline run={run} onOpenVerification={() => setInspectorTab('验证')} /></div>
          </>
        ) : (
          <div className="welcome-state">
            <div className="welcome-orb">M</div>
            <p className="eyebrow">MATHMODEL AGENT</p>
            <h1>{loading ? '正在读取工作区…' : '从一个问题开始'}</h1>
            <p>描述建模目标、上传题目或数据。Agent 会规划、协作并将结果沉淀为可下载的交付物。</p>
            <button onClick={() => void createDraft()}>创建新的会话 <span>→</span></button>
          </div>
        )}
        <TaskComposer
          onSubmit={submitTask}
          onStop={() => void stopRun()}
          busy={submitting}
          running={runIsActive || stopping}
          stopping={stopping}
          {...(runIsActive || stopping
            ? { placeholder: 'Agent 正在工作，可先输入后续要求…' }
            : run && CONTINUABLE_STATUSES.includes(run.status)
              ? { placeholder: '继续这次会话…', submitLabel: '继续会话', busyLabel: '发送中…' }
              : {})}
        />
      </main>
      <ColumnResizer
        column="right"
        value={columns.widths.right}
        onPointerDown={columns.startResize}
        onReset={columns.resetColumn}
        onKeyboardResize={columns.resizeWithKeyboard}
      />
      <div className="right-column">
        {run ? <Inspector run={run} tab={inspectorTab} onTabChange={setInspectorTab} /> : <aside className="empty-inspector"><span>◇</span><strong>会话检查器</strong><p>选择一次历史会话后，可在这里查看协作、计划、输入材料、关键决策、验证记录和交付物。</p></aside>}
      </div>
      {settingsOpen && <SettingsDialog
        key={run?.id ?? 'no-run'}
        run={run}
        onClose={() => setSettingsOpen(false)}
        onRunUpdate={(update) => setRun((current) => (
          current ? { ...current, ...update } : current
        ))}
      />}
      {error && <div className="toast"><span>!</span>{error}<button onClick={() => setError(null)}>×</button></div>}
    </div>
  )
}
