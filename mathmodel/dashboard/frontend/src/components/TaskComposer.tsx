import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useLanguage } from '../i18n'
import type { ProviderSettings, ReasoningEffort } from '../types'

interface TaskComposerProps {
  onSubmit: (input: { task: string; files: File[] }) => Promise<void>
  onStop: () => void
  busy: boolean
  running?: boolean
  stopping?: boolean
  placeholder?: string
  submitLabel?: string
  busyLabel?: string
}

const EFFORTS: ReasoningEffort[] = ['low', 'high', 'max']

function modelShortName(model: string) {
  return model.toLowerCase().includes('flash') ? 'Flash' : 'Pro'
}

function ModelControl() {
  const { language } = useLanguage()
  const en = language === 'en'
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [backendReady, setBackendReady] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const detailsRef = useRef<HTMLDetailsElement>(null)

  useEffect(() => {
    let active = true
    function apply(next: ProviderSettings) {
      if (active) {
        setBackendReady(Boolean(next.reasoning_effort))
        setSettings({
          ...next,
          reasoning_effort: next.reasoning_effort ?? 'high',
        })
      }
    }
    function refresh() {
      void api.providerSettings().then(apply).catch(() => undefined)
    }
    function handleUpdate(event: Event) {
      const next = (event as CustomEvent<ProviderSettings>).detail
      if (next) apply(next)
      else refresh()
    }
    refresh()
    window.addEventListener('focus', refresh)
    window.addEventListener('mathmodel:provider-settings', handleUpdate)
    return () => {
      active = false
      window.removeEventListener('focus', refresh)
      window.removeEventListener('mathmodel:provider-settings', handleUpdate)
    }
  }, [])

  if (!settings || settings.provider !== 'deepseek') return null
  const activeSettings = settings
  const preset = settings.presets.find((item) => item.id === 'deepseek')
  const options = preset?.model_options ?? []

  async function update(model: string, reasoningEffort: ReasoningEffort) {
    if (
      saving
      || (model === activeSettings.model
        && reasoningEffort === activeSettings.reasoning_effort)
    ) return
    setSaving(true)
    setError(null)
    try {
      const response = await api.setProviderSettings({
        credential_mode: activeSettings.credential_mode || 'user',
        provider: 'deepseek',
        model,
        base_url: activeSettings.base_url,
        reasoning_effort: reasoningEffort,
      })
      const next = {
        ...response,
        reasoning_effort: response.reasoning_effort ?? reasoningEffort,
      }
      setSettings(next)
      window.dispatchEvent(new CustomEvent('mathmodel:provider-settings', { detail: next }))
      detailsRef.current?.removeAttribute('open')
    } catch (caught) {
      setError(caught instanceof Error
        ? caught.message
        : (en ? 'Could not update model.' : '模型设置更新失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <details className="composer-model-control" ref={detailsRef}>
    <summary aria-label={en ? 'Choose model and reasoning effort' : '选择模型和思考强度'}>
      <strong>{modelShortName(settings.model)}</strong>
      <span>{settings.reasoning_effort}</span>
      <i>⌄</i>
    </summary>
    <div className="composer-model-menu">
      <section>
        <header>{en ? 'Model' : '模型'}</header>
        {options.map((option) => <button
          type="button"
          key={option.id}
          className={option.id === settings.model ? 'active' : ''}
          disabled={saving || !backendReady}
          onClick={() => void update(option.id, settings.reasoning_effort)}
        >
          <span><strong>{option.label}</strong><small>{option.id}</small></span>
          <i>{option.id === settings.model ? '✓' : ''}</i>
        </button>)}
      </section>
      <section>
        <header>{en ? 'Thinking effort' : '思考强度'}</header>
        <div className="composer-effort-options">
          {EFFORTS.map((effort) => <button
            type="button"
            key={effort}
            className={effort === settings.reasoning_effort ? 'active' : ''}
            disabled={saving || !backendReady}
            onClick={() => void update(settings.model, effort)}
          >{effort}</button>)}
        </div>
      </section>
      {!backendReady && <p>{en
        ? 'Available after the current task finishes and the service restarts.'
        : '当前任务结束并重启服务后即可切换。'}</p>}
      {error && <p>{error}</p>}
    </div>
  </details>
}

export function TaskComposer({
  onSubmit, onStop, busy,
  running = false,
  stopping = false,
  placeholder,
  submitLabel,
  busyLabel,
}: TaskComposerProps) {
  const { language } = useLanguage()
  const en = language === 'en'
  const publicDeployment = import.meta.env.VITE_PUBLIC_DEPLOYMENT === 'true'
  const [task, setTask] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const inputRef = useRef<HTMLInputElement>(null)

  async function submit() {
    if ((!task.trim() && files.length === 0) || busy || running) return
    await onSubmit({ task: task.trim(), files })
    setTask('')
    setFiles([])
  }

  return (
    <section className="composer-shell" aria-label={en ? 'Start a new task' : '发起新任务'}>
      <div className="composer">
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key !== 'Enter'
              || event.nativeEvent.isComposing
              || event.metaKey
              || event.ctrlKey
              || event.shiftKey
              || event.altKey
            ) return
            event.preventDefault()
            void submit()
          }}
          placeholder={placeholder ?? (en ? 'Describe the problem to model, verify, or solve…' : '描述要建模、验证或求解的问题…')}
          rows={2}
          disabled={busy}
        />
        {files.length > 0 && (
          <div className="attachment-list">
            {files.map((file, index) => (
              <span className="attachment" key={`${file.name}-${index}`}>
                <span>↗</span> {file.name}
                <button
                  aria-label={en ? `Remove ${file.name}` : `移除 ${file.name}`}
                  onClick={() => setFiles((items) => items.filter((_, itemIndex) => itemIndex !== index))}
                >×</button>
              </span>
            ))}
          </div>
        )}
        <div className="composer-actions">
          <button
            className="attach-button"
            aria-label={en ? 'Add materials' : '添加材料'}
            title={en ? 'Add materials' : '添加材料'}
            disabled={busy}
            onClick={() => inputRef.current?.click()}
          >＋</button>
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            disabled={busy}
            onChange={(event) => {
              setFiles((items) => [...items, ...Array.from(event.target.files ?? [])])
              event.currentTarget.value = ''
            }}
          />
          <span className="composer-action-spacer" />
          {!publicDeployment && <ModelControl />}
          {running ? (
            <button
              className={`composer-stop-button${stopping ? ' is-stopping' : ''}`}
              disabled={stopping}
              onClick={onStop}
              aria-label={stopping ? (en ? 'Stopping conversation' : '正在停止会话') : (en ? 'Stop conversation' : '停止会话')}
              title={stopping ? (en ? 'Stopping conversation' : '正在停止会话') : (en ? 'Stop current conversation' : '停止当前会话')}
            >
              <span />
            </button>
          ) : (
            <button className="send-button" disabled={busy} onClick={() => void submit()}>
              {busy
                ? (busyLabel ?? (en ? 'Creating…' : '创建中…'))
                : (submitLabel ?? (en ? 'Start conversation' : '开始会话'))} <span>→</span>
            </button>
          )}
        </div>
      </div>
    </section>
  )
}
