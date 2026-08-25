import { useEffect, useState } from 'react'
import { api } from '../api'
import type {
  AccountInfo,
  AgentSettings,
  CredentialMode,
  ProviderId,
  ProviderSettings,
  ProjectBudgetSettings,
  RunDetail,
  SubagentSettings,
  VerificationSettings,
  VerifierSettings,
} from '../types'
import { useLanguage } from '../i18n'

type SettingsUpdate = Partial<Pick<
  RunDetail,
  'agent_settings' | 'subagent_settings' | 'verification_settings' | 'verifier_settings' | 'project_budget_settings'
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
  const { language } = useLanguage()
  const en = language === 'en'
  const publicDeployment = import.meta.env.VITE_PUBLIC_DEPLOYMENT === 'true'
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
          <h2 id="settings-title">{en ? 'Settings' : '设置'}</h2>
          <p>{run
            ? (en ? `Account, model API, and current conversation · ${run.name}` : `账户、模型 API 与当前会话 · ${run.name}`)
            : (en ? 'Account and model API' : '账户与模型 API')}</p>
        </div>
        <button type="button" aria-label={en ? 'Close settings' : '关闭设置'} onClick={onClose}>×</button>
      </header>

      <div className="settings-dialog-body">
        {publicDeployment && <AccountSetting />}
        <ProviderSetting publicDeployment={publicDeployment} />
        {run?.project_budget_settings && <ProjectBudgetSetting
          runId={run.id}
          initial={run.project_budget_settings}
          onUpdate={(project_budget_settings) => onRunUpdate({ project_budget_settings })}
        />}
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
            <strong>{run
              ? (en ? 'Loading conversation settings…' : '正在读取会话设置…')
              : (en ? 'No conversation selected' : '还没有选中会话')}</strong>
            <p>{run
              ? (en
                ? 'Once loaded, you can change step limits for the main, collaboration, and verification Agents, plus verification attempts.'
                : '设置载入后即可修改主 Agent、协作 Agent、验证 Agent 步数和验证轮数。')
              : (en ? 'Select a conversation from the left sidebar first.' : '请先从左侧选择一个会话。')}</p>
          </div>}
      </div>
    </section>
  </div>
}

function ProjectBudgetSetting({
  runId,
  initial,
  onUpdate,
}: {
  runId: string
  initial: ProjectBudgetSettings
  onUpdate: (settings: ProjectBudgetSettings) => void
}) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [current, setCurrent] = useState(initial)
  const [draft, setDraft] = useState(String(initial.revision_budget_limit_cny))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const parsed = Number(draft)
  const valid = Number.isFinite(parsed)
    && parsed >= current.min_revision_budget_limit_cny
    && parsed <= current.max_revision_budget_limit_cny
  const changed = valid && parsed !== current.revision_budget_limit_cny

  async function save(value: number) {
    setSaving(true)
    setSaved(false)
    setError(null)
    try {
      const next = await api.setProjectBudgetSettings(runId, value)
      setCurrent(next)
      setDraft(String(next.revision_budget_limit_cny))
      setSaved(true)
      onUpdate(next)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save the budget cap.' : '追加费用上限保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section project-budget-setting">
    <header>
      <div>
        <span className="settings-section-icon budget">¥</span>
        <div>
          <h3>{en ? 'Revision cost cap' : '单次修改追加费用上限'}</h3>
          <p>{en
            ? 'The backend pauses a confirmed revision when its additional model cost reaches this project limit.'
            : '修改请求确认后，当前 Revision 的新增模型费用达到此项目上限时，后端会自动暂停。'}</p>
        </div>
      </div>
      <small>{en ? 'Project hard limit' : '项目硬上限'}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>{en ? 'Additional cost cap' : '追加费用上限'}</span>
        <span className="settings-number-input budget-input">
          <b>¥</b>
          <input
            type="number"
            min={current.min_revision_budget_limit_cny}
            max={current.max_revision_budget_limit_cny}
            step="0.5"
            value={draft}
            disabled={saving}
            onChange={(event) => { setDraft(event.target.value); setSaved(false); setError(null) }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && changed && !saving) void save(parsed)
            }}
          />
          <em>CNY</em>
        </span>
      </label>
      <div className="settings-actions">
        {saved && <span className="provider-saved">{en ? 'Saved' : '已保存'}</span>}
        <button type="button" className="settings-reset" disabled={saving || current.revision_budget_limit_cny === current.default_revision_budget_limit_cny} onClick={() => void save(current.default_revision_budget_limit_cny)}>
          {en ? 'Restore default' : '恢复默认'}
        </button>
        <button type="button" disabled={saving || !changed} onClick={() => void save(parsed)}>
          {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save cap' : '保存上限')}
        </button>
      </div>
    </div>
    <p className="budget-policy-note">{en
      ? 'Agent estimates are advisory. This value is authoritative and cannot be raised by a change proposal.'
      : 'Agent 给出的费用只是估算；修改卡片不能把这一上限调高，最终以此处设置为准。'}</p>
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function AccountSetting() {
  const { language } = useLanguage()
  const en = language === 'en'
  const [account, setAccount] = useState<AccountInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loggingOut, setLoggingOut] = useState(false)

  useEffect(() => {
    let active = true
    void api.account()
      .then((value) => { if (active) setAccount(value) })
      .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : (en ? 'Could not load the account.' : '账户信息读取失败。')) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [en])

  async function logout() {
    setLoggingOut(true)
    try { await api.logout() } catch { /* Local credentials are cleared by api.logout even if the request fails. */ }
    window.location.assign('/')
  }

  return <section className="settings-section account-setting">
    <header>
      <div>
        <span className="settings-section-icon account">{account?.avatar_url
          ? <img src={account.avatar_url} alt="" referrerPolicy="no-referrer" />
          : (account?.full_name || account?.username || account?.email || 'U').slice(0, 1).toUpperCase()}</span>
        <div>
          <h3>{en ? 'Signed-in account' : '当前登录账户'}</h3>
          <p>{loading
            ? (en ? 'Loading account information…' : '正在读取账户信息…')
            : (account?.full_name || account?.username || account?.email || '—')}</p>
        </div>
      </div>
      <small>{account?.user_type === 'pro' ? 'PRO' : (en ? 'BASIC' : '基础账户')}</small>
    </header>
    {account && <div className="account-setting-body">
      <div>
        <span>{en ? 'Email' : '登录邮箱'}</span>
        <strong>{account.email}</strong>
      </div>
      <div className="account-balance">
        <span>{en ? 'Account balance' : '账户余额'}</span>
        <strong>¥{Number(account.balance || 0).toFixed(2)}</strong>
      </div>
      <button type="button" disabled={loggingOut} onClick={() => void logout()}>{loggingOut ? (en ? 'Signing out…' : '正在退出…') : (en ? 'Sign out' : '退出登录')}</button>
    </div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}

function ProviderSetting({ publicDeployment }: { publicDeployment: boolean }) {
  const { language } = useLanguage()
  const en = language === 'en'
  const [current, setCurrent] = useState<ProviderSettings | null>(null)
  const [credentialMode, setCredentialMode] = useState<CredentialMode>(publicDeployment ? 'server' : 'user')
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
        setCredentialMode(publicDeployment ? (settings.credential_mode || 'server') : 'user')
        setProvider(settings.provider)
        setModel(settings.model)
        setBaseUrl(settings.base_url)
      })
      .catch((caught) => {
        if (!active) return
        setError(caught instanceof Error ? caught.message : (en ? 'Could not load model settings.' : '模型设置读取失败。'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [en])

  const selectedPreset = current?.presets.find((preset) => preset.id === provider)
  const hasComposerModelSelector = Boolean(selectedPreset?.model_options.length)
  const trimmedModel = model.trim()
  const trimmedBaseUrl = baseUrl.trim()
  const isValid = credentialMode === 'server' || Boolean(trimmedModel && /^https?:\/\/[^/]/i.test(trimmedBaseUrl))
  const hasChanges = Boolean(
    current
    && (
      credentialMode !== (current.credential_mode || (publicDeployment ? 'server' : 'user'))
      || (credentialMode === 'user' && (
        provider !== current.provider
        || trimmedModel !== current.model
        || trimmedBaseUrl.replace(/\/+$/, '') !== current.base_url.replace(/\/+$/, '')
        || Boolean(apiKey.trim())
      ))
    )
  )
  const needsKey = credentialMode === 'user' && !selectedPreset?.api_key_configured && !apiKey.trim()

  function selectCredentialMode(nextMode: CredentialMode) {
    setCredentialMode(nextMode)
    setSaved(false)
    setError(null)
    setApiKey('')
    if (!current) return
    if (nextMode === 'server') {
      setProvider(current.server_provider || current.provider)
      setModel(current.server_model || current.model)
      const preset = current.presets.find((item) => item.id === (current.server_provider || current.provider))
      setBaseUrl(preset?.default_base_url || current.base_url)
      return
    }
    const nextProvider = current.user_provider || 'deepseek'
    const preset = current.presets.find((item) => item.id === nextProvider)
    setProvider(nextProvider)
    setModel(current.user_model || preset?.default_model || '')
    setBaseUrl(current.user_base_url || preset?.default_base_url || '')
  }

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
        credential_mode: credentialMode,
        ...(credentialMode === 'user' ? {
          provider,
          model: trimmedModel,
          base_url: trimmedBaseUrl,
          reasoning_effort: current?.user_reasoning_effort ?? current?.reasoning_effort ?? 'high',
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        } : {}),
      })
      setCurrent(next)
      setCredentialMode(publicDeployment ? next.credential_mode : 'user')
      setProvider(next.provider)
      setModel(next.model)
      setBaseUrl(next.base_url)
      setApiKey('')
      setSaved(true)
      window.dispatchEvent(new CustomEvent('mathmodel:provider-settings', { detail: next }))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save model settings.' : '模型设置保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section provider-setting">
    <header>
      <div>
        <span className="settings-section-icon provider">⌁</span>
        <div>
          <h3>{en ? 'Model API' : '模型 API'}</h3>
          <p>{en ? 'Use the platform model service or connect your own provider credential.' : '可以使用平台 API，也可以为当前账户接入自己的模型 API。'}</p>
        </div>
      </div>
      <small>{loading ? (en ? 'Loading…' : '读取中…') : credentialMode === 'server' ? (en ? 'Platform API' : '平台 API') : (en ? 'My API' : '我的 API')}</small>
    </header>

    {loading
      ? <div className="provider-loading">{en ? 'Loading local model configuration…' : '正在读取本机模型配置…'}</div>
      : current && <div className="provider-form">
        {publicDeployment && <fieldset className="credential-mode-fieldset">
          <legend>{en ? 'API source' : 'API 使用方式'}</legend>
          <div className="credential-mode-options">
            <button type="button" className={credentialMode === 'server' ? 'active' : ''} aria-pressed={credentialMode === 'server'} onClick={() => selectCredentialMode('server')}>
              <strong>{en ? 'Platform API' : '平台 API'}</strong>
              <span>{en ? 'Use the service credential' : '使用网站提供的额度'}</span>
            </button>
            <button type="button" className={credentialMode === 'user' ? 'active' : ''} aria-pressed={credentialMode === 'user'} onClick={() => selectCredentialMode('user')}>
              <strong>{en ? 'My API' : '我的 API'}</strong>
              <span>{en ? 'Use my own credential' : '使用自己的 API Key'}</span>
            </button>
          </div>
        </fieldset>}

        {credentialMode === 'server'
          ? <div className="platform-provider-summary">
            <span>{en ? 'Current platform model' : '当前平台模型'}</span>
            <strong>{current.server_provider} · {current.server_model}</strong>
            <p>{en ? 'Usage is billed from your website account balance.' : '任务费用从上方显示的网站账户余额中扣除。'}</p>
          </div>
          : <>
        <fieldset>
          <legend>{en ? 'Model provider' : '模型供应商'}</legend>
          <div className="provider-options">
            {current.presets.map((preset) => <button
              key={preset.id}
              type="button"
              className={provider === preset.id ? 'active' : ''}
              aria-pressed={provider === preset.id}
              disabled={saving}
              onClick={() => selectProvider(preset.id)}
            >
              <strong>{en && preset.id === 'openai_compatible' ? 'Other compatible API' : preset.label}</strong>
              <span>{preset.api_key_configured
                ? (en ? 'Key configured' : '密钥已配置')
                : (en ? 'Key required' : '需要密钥')}</span>
            </button>)}
          </div>
        </fieldset>

        <div className="provider-field-grid">
          {!hasComposerModelSelector && <label>
              <span>{en ? 'Model name' : '模型名称'}</span>
              <input
                value={model}
                disabled={saving}
                placeholder={en ? 'For example, kimi-k2.6' : '例如 kimi-k2.6'}
                onChange={(event) => {
                  setModel(event.target.value)
                  setSaved(false)
                  setError(null)
                }}
              />
            </label>}
          <label className={hasComposerModelSelector ? 'provider-base-url-field' : undefined}>
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
                ? (en
                  ? `${selectedPreset.api_key_hint ?? 'Key'} configured; leave blank to keep it`
                  : `已配置 ${selectedPreset.api_key_hint ?? ''}，留空保持不变`)
                : (en ? 'Enter this provider’s API Key' : '输入该供应商的 API Key')}
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
          <p>{en
            ? 'Your key is encrypted at rest and scoped to this account. It is never returned to the browser or written to conversation logs.'
            : 'API Key 会加密保存并只绑定当前账户；完整密钥不会返回浏览器，也不会写入会话记录。'}</p>
          <div className="settings-actions">
            {saved && <span className="provider-saved">{en ? 'Saved' : '已保存'}</span>}
            <button
              type="button"
              disabled={saving || !isValid || needsKey || !hasChanges}
              onClick={() => void save()}
            >
              {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save my API' : '保存我的 API')}
            </button>
          </div>
        </footer>
        {needsKey && <div className="settings-error">{en
          ? `Enter an API Key before switching to ${selectedPreset?.id === 'openai_compatible' ? 'the compatible API' : selectedPreset?.label}.`
          : `切换到 ${selectedPreset?.label} 前，请填写对应的 API Key。`}</div>}
        </>}
        {credentialMode === 'server' && hasChanges && <div className="settings-actions provider-mode-save">
          {saved && <span className="provider-saved">{en ? 'Saved' : '已保存'}</span>}
          <button type="button" disabled={saving} onClick={() => void save()}>{saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Use platform API' : '使用平台 API')}</button>
        </div>}
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
  const { language } = useLanguage()
  const en = language === 'en'
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
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save the main Agent step limit.' : '主 Agent 步数保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon agent">A</span>
        <div>
          <h3>{en ? 'Main Agent maximum steps' : '主 Agent 最大步数'}</h3>
          <p>{en ? 'Maximum number of steps the main Agent can execute in one conversation.' : '控制主 Agent 在一轮会话中可以执行的最大步骤数量。'}</p>
        </div>
      </div>
      <small>{current.is_custom ? (en ? 'Custom for this conversation' : '当前会话自定义') : (en ? 'System default' : '系统默认')}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>{en ? 'Step limit' : '步骤上限'}</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label={en ? 'Main Agent maximum steps' : '主 Agent 最大步数'}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>{en ? 'steps' : '步'}</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>{en ? 'Restore default' : '恢复默认'}</button>
          : <span>{en ? `Default: ${current.default_max_steps} steps` : `默认 ${current.default_max_steps} 步`}</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save' : '保存')}
        </button>
      </div>
    </div>
    <p className="settings-help">{en
      ? `Choose ${current.min_steps}–${current.max_allowed_steps} steps. Applies to the next new, retried, or continued conversation.`
      : `可设置 ${current.min_steps}–${current.max_allowed_steps} 步；下一次新建、重试或继续会话时生效。`}</p>
    {!isValid && <div className="settings-error">{en
      ? `Enter an integer from ${current.min_steps} to ${current.max_allowed_steps}.`
      : `请输入 ${current.min_steps}–${current.max_allowed_steps} 之间的整数。`}</div>}
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
  const { language } = useLanguage()
  const en = language === 'en'
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
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save the Verification Agent step limit.' : '验证 Agent 步数保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon verifier">V</span>
        <div>
          <h3>{en ? 'Verification Agent maximum steps' : '验证 Agent 最大步数'}</h3>
          <p>{en ? 'Maximum number of checks the Verification Agent can execute in each attempt.' : '控制每一轮验证中，验证 Agent 最多可以执行多少个检查步骤。'}</p>
        </div>
      </div>
      <small>{current.is_custom ? (en ? 'Custom for this conversation' : '当前会话自定义') : (en ? 'System default' : '系统默认')}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>{en ? 'Step limit' : '步骤上限'}</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label={en ? 'Verification Agent maximum steps' : '验证 Agent 最大步数'}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>{en ? 'steps' : '步'}</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>{en ? 'Restore default' : '恢复默认'}</button>
          : <span>{en ? `Default: ${current.default_max_steps} steps` : `默认 ${current.default_max_steps} 步`}</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save' : '保存')}
        </button>
      </div>
    </div>
    <p className="settings-help">{en
      ? `Choose ${current.min_steps}–${current.max_allowed_steps} steps. Applies to the next start, retry, or continuation.`
      : `可设置 ${current.min_steps}–${current.max_allowed_steps} 步；下一次开始、重试或继续会话时生效。`}</p>
    {!isValid && <div className="settings-error">{en
      ? `Enter an integer from ${current.min_steps} to ${current.max_allowed_steps}.`
      : `请输入 ${current.min_steps}–${current.max_allowed_steps} 之间的整数。`}</div>}
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
  const { language } = useLanguage()
  const en = language === 'en'
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
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save the collaboration Agent step limit.' : '协作 Agent 步数保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon">S</span>
        <div>
          <h3>{en ? 'Collaboration Agent maximum steps' : '协作 Agent 最大步数'}</h3>
          <p>{en ? 'Maximum number of steps each sub-agent can execute for a delegated task.' : '控制每个 subagent 在一项委派任务中最多可以执行多少步。'}</p>
        </div>
      </div>
      <small>{current.is_custom ? (en ? 'Custom for this conversation' : '当前会话自定义') : (en ? 'System default' : '系统默认')}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>{en ? 'Step limit' : '步骤上限'}</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_steps}
            max={current.max_allowed_steps}
            step={1}
            value={draft}
            disabled={saving}
            aria-label={en ? 'Collaboration Agent maximum steps' : '协作 Agent 最大步数'}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>{en ? 'steps' : '步'}</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>{en ? 'Restore default' : '恢复默认'}</button>
          : <span>{en ? `Default: ${current.default_max_steps} steps` : `默认 ${current.default_max_steps} 步`}</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save' : '保存')}
        </button>
      </div>
    </div>
    <p className="settings-help">{en
      ? `Choose ${current.min_steps}–${current.max_allowed_steps} steps. Applies to the next new, retried, or continued conversation.`
      : `可设置 ${current.min_steps}–${current.max_allowed_steps} 步；下一次新建、重试或继续会话时生效。`}</p>
    {!isValid && <div className="settings-error">{en
      ? `Enter an integer from ${current.min_steps} to ${current.max_allowed_steps}.`
      : `请输入 ${current.min_steps}–${current.max_allowed_steps} 之间的整数。`}</div>}
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
  const { language } = useLanguage()
  const en = language === 'en'
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
      setError(caught instanceof Error ? caught.message : (en ? 'Could not save the verification attempt limit.' : '验证轮数保存失败。'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="settings-section">
    <header>
      <div>
        <span className="settings-section-icon">↻</span>
        <div>
          <h3>{en ? 'Maximum verification attempts' : '最大验证轮数'}</h3>
          <p>{en ? 'Maximum number of independent verification attempts for the same candidate.' : '控制同一候选结果最多经历多少轮独立验证。'}</p>
        </div>
      </div>
      <small>{current.is_custom ? (en ? 'Custom for this conversation' : '当前会话自定义') : (en ? 'System default' : '系统默认')}</small>
    </header>
    <div className="settings-round-row">
      <label>
        <span>{en ? 'Attempt limit' : '验证上限'}</span>
        <span className="settings-number-input">
          <input
            type="number"
            min={current.min_attempts}
            max={current.max_allowed_attempts}
            step={1}
            value={draft}
            disabled={saving}
            aria-label={en ? 'Maximum verification attempts' : '最大验证轮数'}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && hasChanges && !saving) void save(parsed)
            }}
          />
          <b>{en ? 'attempts' : '轮'}</b>
        </span>
      </label>
      <div className="settings-actions">
        {current.is_custom
          ? <button type="button" className="secondary" disabled={saving} onClick={() => void save(null)}>{en ? 'Restore default' : '恢复默认'}</button>
          : <span>{en ? `Default: ${current.default_max_attempts} attempts` : `默认 ${current.default_max_attempts} 轮`}</span>}
        <button type="button" disabled={saving || !hasChanges} onClick={() => void save(parsed)}>
          {saving ? (en ? 'Saving…' : '保存中…') : (en ? 'Save' : '保存')}
        </button>
      </div>
    </div>
    <p className="settings-help">{en
      ? `Choose ${current.min_attempts}–${current.max_allowed_attempts} attempts. Changes apply from the next verification decision.`
      : `可设置 ${current.min_attempts}–${current.max_allowed_attempts} 轮；修改从下一次验证判定起生效。`}</p>
    {!isValid && <div className="settings-error">{en
      ? `Enter an integer from ${current.min_attempts} to ${current.max_allowed_attempts}.`
      : `请输入 ${current.min_attempts}–${current.max_allowed_attempts} 之间的整数。`}</div>}
    {error && <div className="settings-error">{error}</div>}
  </section>
}
