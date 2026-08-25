import type {
  ContextRequestDetail,
  ContextRequestSummary,
  ContextRun,
  ToolMetrics,
} from './types'

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `Request failed with ${response.status}`)
  }
  return response.json() as Promise<T>
}

export function fetchRuns(signal?: AbortSignal) {
  return requestJson<ContextRun[]>('/api/runs', signal)
}

export function fetchRequests(runId: string, signal?: AbortSignal) {
  return requestJson<ContextRequestSummary[]>(
    `/api/requests?run_id=${encodeURIComponent(runId)}`,
    signal,
  )
}

export function fetchRequestDetail(
  runId: string,
  requestId: string,
  signal?: AbortSignal,
) {
  return requestJson<ContextRequestDetail>(
    `/api/request?run_id=${encodeURIComponent(runId)}&request_id=${encodeURIComponent(requestId)}`,
    signal,
  )
}

export function fetchToolMetrics(runId: string, signal?: AbortSignal) {
  return requestJson<ToolMetrics>(
    `/api/tool-metrics?run_id=${encodeURIComponent(runId)}`,
    signal,
  )
}

export function exportUrl(runId: string, requestId: string) {
  return `/api/export?run_id=${encodeURIComponent(runId)}&request_id=${encodeURIComponent(requestId)}`
}
