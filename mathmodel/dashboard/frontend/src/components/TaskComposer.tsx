import { useRef, useState } from 'react'

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

export function TaskComposer({
  onSubmit, onStop, busy,
  running = false,
  stopping = false,
  placeholder = '描述要建模、验证或求解的问题…',
  submitLabel = '开始会话',
  busyLabel = '创建中…',
}: TaskComposerProps) {
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
    <section className="composer-shell" aria-label="发起新任务">
      <div className="composer">
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void submit()
          }}
          placeholder={placeholder}
          rows={2}
          disabled={busy}
        />
        {files.length > 0 && (
          <div className="attachment-list">
            {files.map((file, index) => (
              <span className="attachment" key={`${file.name}-${index}`}>
                <span>↗</span> {file.name}
                <button aria-label={`移除 ${file.name}`} onClick={() => setFiles((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button>
              </span>
            ))}
          </div>
        )}
        <div className="composer-actions">
          <button className="attach-button" aria-label="添加材料" title="添加材料" disabled={busy} onClick={() => inputRef.current?.click()}>＋</button>
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
          <span className="shortcut">{running ? '可先输入后续要求' : '⌘ Enter'}</span>
          {running ? (
            <button
              className={`composer-stop-button${stopping ? ' is-stopping' : ''}`}
              disabled={stopping}
              onClick={onStop}
              aria-label={stopping ? '正在停止会话' : '停止会话'}
              title={stopping ? '正在停止会话' : '停止当前会话'}
            >
              <span />
            </button>
          ) : (
            <button className="send-button" disabled={busy} onClick={() => void submit()}>
              {busy ? busyLabel : submitLabel} <span>→</span>
            </button>
          )}
        </div>
      </div>
    </section>
  )
}
