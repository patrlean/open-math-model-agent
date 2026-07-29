import { useEffect, useState } from 'react'
import { api } from '../api'
import type {
  AgentSettings,
  ProviderId,
  ProviderSettings,
  RunDetail,
  SubagentSettings,
  VerificationSettings,
  VerifierSettings,
} from '../types'

type SettingsUpdate = Partial<Pick<
  RunDetail,
  'agent_settings' | 'subagent_settings' | 'verification_settings' | 'verifier_settings'
>>

interface SettingsDialogProps {
  run: RunDetail | null
  onClose: () => void
  onRunUpdate: (update: SettingsUpdate) => void
}

export function SettingsDialog({
  run,
  onClose,
  onRunUpdate,
}: SettingsDialogProps) {
  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  return <div
    className="settings-backdrop"
    role="presentation"
    onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose()
    }}
  >
    <section
      className="settings-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
    >
      <header className="settings-dialog-header">
        <div>
          <span>Workspace settings</span>
          <h2 id="settings-title">设置</h2>
          <p>{run ? `全局模型与当前会话 · ${run.name}` : '全局模型设置'}</p>
        </div>
        <button type="button" aria-label="关闭设置" onClick={onClose}>×</button>
      </header>

      <div className="settings-dialog-body">
        <ProviderSetting />
        {run?.agent_settings && run.subagent_settings && run.verification_settings && run.verifier_settings
          ? <>
            <MainAgentStepSetting
              runId={run.id}
              initial={run.agent_settings}
              onUpdate={(agent_settings) => onRunUpdate({ agent_settings })}
            />
            <SubagentStepSetting
              runId={run.id}
              initial={run.subagent_settings}
              onUpdate={(subagent_settings) => onRunUpdate({ subagent_settings })}
            />
            <VerifierStepSetting
              runId={run.id}
              initial={run.verifier_settings}
              onUpdate={(verifier_settings) => onRunUpdate({ verifier_settings })}
            />
            <VerificationRoundSetting
              runId={run.id}
              initial={run.verification_settings}
              onUpdate={(verification_settings) => onRunUpdate({ verification_settings })}
            />
          </>
          : <div className="settings-empty">
            <span>◇</span>
            <strong>{run ? '正在读取会话设置…' : '还没有选中会话'}</strong>
            <p>{run ? '设置载入后即可修改主 Agent、协作 Agent、验证 Agent 步数和验证轮数。' : '请先从左侧选择一个会话。'}</p>
          </div>}
      </div>
    </section>
  </div>
}

function ProviderSetting() {
  const [current, setCurrent] = useState<ProviderSettings | null>(null)
  const [provider, setProvider] = useState<ProviderId>('deepseek')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void api.providerSettings()
      .then((settings) => {
        if (!active) return
        setCurrent(settings)
        setProvider(settings.provider)
        setModel(settings.model)
        setBaseUrl(settings.base_url)
      })
      .catch((caught) => {
        if (!active) return
        setError(caught instanceof Error ? caught.message : '模型设置读取失败。')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  const selectedPreset = current?.presets.find((preset) => preset.id === provider)
  const trimmedModel = model.trim()
  const trimmedBaseUrl = baseUrl.trim()
  const isValid = Boolean(trimmedModel && /^https?:\/\/[^/]/i.test(trimmedBaseUrl))
  const hasChanges = Boolean(
    current
    && (
      provider !== current.provider
      || trimmedModel !== current.model
      || trimmedBaseUrl.replace(/\/+$/, '') !== current.base_url.replace(/\/+$/, '')
      || apiKey.trim()
    )
  )
  const needsKey = !selectedPreset?.api_key_configured && !apiKey.trim()

  function selectProvider(nextProvider: ProviderId) {
    const preset = current?.presets.find((item) => item.id === nextProvider)
    setProvider(nextProvider)
    setModel(nextProvider === current?.provider ? current.model : (preset?.default_model ?? ''))
    setBaseUrl(nextProvider === current?.provider ? current.base_url : (preset?.default_base_url ?? ''))
    setApiKey('')
    setSaved(false)
    setError(null)
  }

  async function save() {
    if (!isValid || needsKey || !hasChanges) return
    setSaving(true)
    setSaved(false)
    setError(null)
    try {
      const next = await api.setProviderSettings({
        provider,
        model: trimmedModel,
        base_url: trimmedBaseUrl,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      })
      setCurrent(next)
      setProvider(next.provider)
      setModel(next.model)
      setBaseUrl(next.base_url)
      setApiKey('')
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '模型设置保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section provider-setting">
    <header>
      <div>
        <span className="settings-section-icon provider">⌁</span>
        <div>
          <h3>模型 API</h3>
          <p>选择会话使用的模型供应商，也可以接入其他 OpenAI 兼容接口。</p>
        </div>
      </div>
      <small>{loading ? '读取中…' : '全局设置'}</small>
    </header>

    {loading
      ? <div className="provider-loading">正在读取本机模型配置…</div>
      : current && <div className="provider-form">
        <fieldset>
          <legend>模型供应商</legend>
          <div className="provider-options">
            {current.presets.map((preset) => <button
              key={preset.id}
              type="button"
              className={provider === preset.id ? 'active' : ''}
              aria-pressed={provider === preset.id}
              disabled={saving}
              onClick={() => selectProvider(preset.id)}
            >
              <strong>{preset.label}</strong>
              <span>{preset.api_key_configured ? '密钥已配置' : '需要密钥'}</span>
            </button>)}
          </div>
        </fieldset>

        <div className="provider-field-grid">
          <label>
            <span>模型名称</span>
            <input
              value={model}
              disabled={saving}
              placeholder="例如 kimi-k2.6"
              onChange={(event) => {
                setModel(event.target.value)
                setSaved(false)
                setError(null)
              }}
            />
          </label>
          <label>
            <span>Base URL</span>
            <input
              value={baseUrl}
              disabled={saving}
              inputMode="url"
              placeholder="https://api.example.com/v1"
              onChange={(event) => {
                setBaseUrl(event.target.value)
                setSaved(false)
                setError(null)
              }}
            />
          </label>
          <label className="provider-key-field">
            <span>API Key</span>
            <input
              type="password"
              value={apiKey}
              disabled={saving}
              autoComplete="new-password"
              placeholder={selectedPreset?.api_key_configured
                ? `已配置 ${selectedPreset.api_key_hint ?? ''}，留空保持不变`
                : '输入该供应商的 API Key'}
              onChange={(event) => {
                setApiKey(event.target.value)
                setSaved(false)
                setError(null)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && isValid && !needsKey && hasChanges && !saving) {
                  void save()
                }
              }}
            />
          </label>
        </div>

        <footer>
          <p>密钥只保存在本机；完整密钥不会返回浏览器或写入会话记录。更改从下一次新建或继续会话起生效。</p>
          <div className="settings-actions">
            {saved && <span className="provider-saved">已保存</span>}
            <button
              type="button"
              disabled={saving || !isValid || needsKey || !hasChanges}
              onClick={() => void save()}
            >
              {saving ? '保存中…' : '保存模型设置'}
            </button>
          </div>
        </footer>
        {needsKey && <div className="settings-error">切换到 {selectedPreset?.label} 前，请填写对应的 API Key。</div>}
      </div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function MainAgentStepSetting({
  runId,
  initial,
  onUpdate,
}: {
  runId: string
  initial: AgentSettings
  onUpdate: (settings: AgentSettings) => void
}) {
  const [current, setCurrent] = useState(initial)
  const [draft, setDraft] = useState(String(initial.max_steps))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const parsed = Number(draft)
  const isValid = (
    Number.isInteger(parsed)
    && parsed >= current.min_steps
    && parsed <= current.max_allowed_steps
  )
  const hasChanges = isValid && parsed !== current.max_steps

  async function save(value: number | null) {
    setSaving(true)
    setError(null)
    try {
      const saved = await api.setAgentSettings(runId, value)
      setCurrent(saved)
      setDraft(String(saved.max_steps))
      onUpdate(saved)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '主 Agent 步数保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon agent">A</span>
        <div>
          <h3>主 Agent 最大步数</h3>
          <p>控制主 Agent 在一轮会话中可以执行的最大步骤数量。</p>
        </div>
      </div>
      <small>{current.is_custom ? '当前会话自定义' : '系统默认'}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>步骤上限</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label="主 Agent 最大步数"
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>步</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>恢复默认</button>
          : <span>默认 {current.default_max_steps} 步</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
    <p className="settings-help">可设置 {current.min_steps}–{current.max_allowed_steps} 步；下一次新建、重试或继续会话时生效。</p>
    {!isValid && <div className="settings-error">请输入 {current.min_steps}–{current.max_allowed_steps} 之间的整数。</div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function VerifierStepSetting({
  runId,
  initial,
  onUpdate,
}: {
  runId: string
  initial: VerifierSettings
  onUpdate: (settings: VerifierSettings) => void
}) {
  const [current, setCurrent] = useState(initial)
  const [draft, setDraft] = useState(String(initial.max_steps))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const parsed = Number(draft)
  const isValid = (
    Number.isInteger(parsed)
    && parsed >= current.min_steps
    && parsed <= current.max_allowed_steps
  )
  const hasChanges = isValid && parsed !== current.max_steps

  async function save(value: number | null) {
    setSaving(true)
    setError(null)
    try {
      const saved = await api.setVerifierSettings(runId, value)
      setCurrent(saved)
      setDraft(String(saved.max_steps))
      onUpdate(saved)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '验证 Agent 步数保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon verifier">V</span>
        <div>
          <h3>验证 Agent 最大步数</h3>
          <p>控制每一轮验证中，验证 Agent 最多可以执行多少个检查步骤。</p>
        </div>
      </div>
      <small>{current.is_custom ? '当前会话自定义' : '系统默认'}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>步骤上限</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label="验证 Agent 最大步数"
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>步</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>恢复默认</button>
          : <span>默认 {current.default_max_steps} 步</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
    <p className="settings-help">可设置 {current.min_steps}–{current.max_allowed_steps} 步；下一次开始、重试或继续会话时生效。</p>
    {!isValid && <div className="settings-error">请输入 {current.min_steps}–{current.max_allowed_steps} 之间的整数。</div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function SubagentStepSetting({
  runId,
  initial,
  onUpdate,
}: {
  runId: string
  initial: SubagentSettings
  onUpdate: (settings: SubagentSettings) => void
}) {
  const [current, setCurrent] = useState(initial)
  const [draft, setDraft] = useState(String(initial.max_steps))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const parsed = Number(draft)
  const isValid = (
    Number.isInteger(parsed)
    && parsed >= current.min_steps
    && parsed <= current.max_allowed_steps
  )
  const hasChanges = isValid && parsed !== current.max_steps

  async function save(value: number | null) {
    setSaving(true)
    setError(null)
    try {
      const saved = await api.setSubagentSettings(runId, value)
      setCurrent(saved)
      setDraft(String(saved.max_steps))
      onUpdate(saved)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '协作 Agent 步数保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon">S</span>
        <div>
          <h3>协作 Agent 最大步数</h3>
          <p>控制每个 subagent 在一项委派任务中最多可以执行多少步。</p>
        </div>
      </div>
      <small>{current.is_custom ? '当前会话自定义' : '系统默认'}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>步骤上限</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label="协作 Agent 最大步数"
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>步</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>恢复默认</button>
          : <span>默认 {current.default_max_steps} 步</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
    <p className="settings-help">可设置 {current.min_steps}–{current.max_allowed_steps} 步；下一次新建、重试或继续会话时生效。</p>
    {!isValid && <div className="settings-error">请输入 {current.min_steps}–{current.max_allowed_steps} 之间的整数。</div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function VerificationRoundSetting({
  runId,
  initial,
  onUpdate,
}: {
  runId: string
  initial: VerificationSettings
  onUpdate: (settings: VerificationSettings) => void
}) {
  const [current, setCurrent] = useState(initial)
  const [draft, setDraft] = useState(String(initial.max_attempts))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const parsed = Number(draft)
  const isValid = (
    Number.isInteger(parsed)
    && parsed >= current.min_attempts
    && parsed <= current.max_allowed_attempts
  )
  const hasChanges = isValid && parsed !== current.max_attempts

  async function save(value: number | null) {
    setSaving(true)
    setError(null)
    try {
      const saved = await api.setVerificationSettings(runId, value)
      setCurrent(saved)
      setDraft(String(saved.max_attempts))
      onUpdate(saved)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '验证轮数保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon">↻</span>
        <div>
          <h3>最大验证轮数</h3>
          <p>控制同一候选结果最多经历多少轮独立验证。</p>
        </div>
      </div>
      <small>{current.is_custom ? '当前会话自定义' : '系统默认'}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>验证上限</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_attempts}
            max={current.max_allowed_attempts}
            step={1}
            value={draft}
            disabled={saving}
            aria-label="最大验证轮数"
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>轮</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>恢复默认</button>
          : <span>默认 {current.default_max_attempts} 轮</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
    <p className="settings-help">可设置 {current.min_attempts}–{current.max_allowed_attempts} 轮；修改从下一次验证判定起生效。</p>
    {!isValid && <div className="settings-error">请输入 {current.min_attempts}–{current.max_allowed_attempts} 之间的整数。</div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}
