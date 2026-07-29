import type {
  AgentSettings,
  ProviderSettings,
  ProviderId,
  RunDetail,
  RunSummary,
  SubagentSettings,
  VerificationSettings,
  VerifierSettings,
} from './types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    const error = await response.json().catch(() => null) as { error?: string } | null
    throw new Error(error?.error || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export const api = {
  providerSettings: () => request<ProviderSettings>('/api/provider-settings'),
  setProviderSettings: (body: {
    provider: ProviderId
    model: string
    base_url: string
    api_key?: string
  }) => request<ProviderSettings>('/api/provider-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
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
  deleteRun: (id: string) => request<{ ok: true }>(`/api/run?id=${encodeURIComponent(id)}`, { method: 'DELETE' }),
  fileUrl: (id: string, path: string) =>
    `/api/file?id=${encodeURIComponent(id)}&path=${encodeURIComponent(path)}`,
}
