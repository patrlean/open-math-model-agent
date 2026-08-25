import type {
  AccountInfo,
  AgentSettings,
  CredentialMode,
  ProviderSettings,
  ProviderId,
  ProjectBudgetSettings,
  ReasoningEffort,
  RunDetail,
  RunSummary,
  SubagentSettings,
  VerificationSettings,
  VerifierSettings,
} from './types'
import { storedLanguage } from './i18n'

const configuredApiBase = (import.meta.env.VITE_API_BASE_PATH || '').replace(/\/$/, '')

function apiUrl(url: string): string {
  return configuredApiBase ? url.replace(/^\/api/, configuredApiBase) : url
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  const token = window.localStorage.getItem('access_token') || window.localStorage.getItem('token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(apiUrl(url), { ...init, headers })
  if (!response.ok) {
    const error = await response.json().catch(() => null) as { error?: string } | null
    throw new Error(error?.error || (
      storedLanguage() === 'en'
        ? `Request failed (${response.status})`
        : `请求失败（${response.status}）`
    ))
  }
  return response.json() as Promise<T>
}

async function siteRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  const token = window.localStorage.getItem('access_token') || window.localStorage.getItem('token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(url, { ...init, headers })
  if (!response.ok) throw new Error(storedLanguage() === 'en' ? 'Could not load account information.' : '账户信息读取失败。')
  return response.json() as Promise<T>
}

export const api = {
  providerSettings: () => request<ProviderSettings>('/api/provider-settings'),
  setProviderSettings: (body: {
    credential_mode: CredentialMode
    provider?: ProviderId
    model?: string
    base_url?: string
    reasoning_effort?: ReasoningEffort
    api_key?: string
  }) => request<ProviderSettings>('/api/provider-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  account: async (): Promise<AccountInfo> => {
    const [profile, balance] = await Promise.all([
      siteRequest<{ data?: Omit<AccountInfo, 'balance'> & { account_balance?: number } }>('/api/user/profile'),
      siteRequest<{ data?: { balance: number; user_type: 'basic' | 'pro' } }>('/api/user/balance'),
    ])
    if (!profile.data || !balance.data) throw new Error(storedLanguage() === 'en' ? 'Account information is incomplete.' : '账户信息不完整。')
    return { ...profile.data, user_type: balance.data.user_type, balance: balance.data.balance }
  },
  logout: async () => {
    try {
      await siteRequest('/api/auth/logout', { method: 'POST' })
    } finally {
      window.localStorage.removeItem('access_token')
      window.localStorage.removeItem('refresh_token')
      window.localStorage.removeItem('token')
      window.localStorage.removeItem('user')
    }
  },
  listRuns: () => request<RunSummary[]>('/api/runs'),
  run: (id: string) => request<RunDetail>(`/api/run?id=${encodeURIComponent(id)}`),
  createDraft: () => request<{ id: string; name: string }>('/api/drafts', { method: 'POST' }),
  launch: (body: { task: string; files: { name: string; b64: string }[]; run_id?: string }) =>
    request<{ id: string; name: string }>('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  retry: (id: string) => request<{ id: string; name: string }>('/api/retry', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  }),
  continueRun: (body: { id: string; task: string; files: { name: string; b64: string }[] }) =>
    request<{ id: string; name: string }>('/api/continue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  continueAfterMaxSteps: (id: string) =>
    request<{
      id: string
      name: string
      added_steps: number
      agent_settings: AgentSettings
    }>('/api/continue-after-max-steps', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    }),
  stop: (id: string) => request<{ ok: true }>('/api/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  }),
  answer: (
    id: string,
    response: { answer?: string; option_id?: string },
  ) => request<{ ok: true }>('/api/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, ...response }),
  }),
  setVerificationSettings: (id: string, maxAttempts: number | null) =>
    request<VerificationSettings>('/api/verification-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(maxAttempts == null
        ? { id, reset: true }
        : { id, max_attempts: maxAttempts }),
    }),
  setAgentSettings: (id: string, maxSteps: number | null) =>
    request<AgentSettings>('/api/agent-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(maxSteps == null
        ? { id, reset: true }
        : { id, max_steps: maxSteps }),
    }),
  setVerifierSettings: (id: string, maxSteps: number | null) =>
    request<VerifierSettings>('/api/verifier-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(maxSteps == null
        ? { id, reset: true }
        : { id, max_steps: maxSteps }),
    }),
  setSubagentSettings: (id: string, maxSteps: number | null) =>
    request<SubagentSettings>('/api/subagent-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(maxSteps == null
        ? { id, reset: true }
        : { id, max_steps: maxSteps }),
    }),
  setProjectBudgetSettings: (id: string, revisionBudgetLimitCny: number) =>
    request<ProjectBudgetSettings>('/api/project-budget-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, revision_budget_limit_cny: revisionBudgetLimitCny }),
    }),
  deleteRun: (id: string) => request<{ ok: true }>(`/api/run?id=${encodeURIComponent(id)}`, { method: 'DELETE' }),
  fileUrl: (id: string, path: string) =>
    apiUrl(`/api/file?id=${encodeURIComponent(id)}&path=${encodeURIComponent(path)}`),
}
