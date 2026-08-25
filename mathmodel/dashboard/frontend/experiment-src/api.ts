import type {
  AgentContextGroup,
  CaseDetail,
  ContextRequestDetail,
  ContextRequestSummary,
  ExperimentDetail,
  ExperimentSummary,
} from './types'

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', signal })
  if (!response.ok) {
    const payload = await response.text()
    throw new Error(payload || `Request failed with ${response.status}`)
  }
  return response.json() as Promise<T>
}

const query = (params: Record<string, string | number>) => new URLSearchParams(
  Object.entries(params).map(([key, value]) => [key, String(value)]),
).toString()

export function fetchExperiments(signal?: AbortSignal) {
  return requestJson<ExperimentSummary[]>('/api/experiments', signal)
}

export function fetchExperiment(experimentId: string, signal?: AbortSignal) {
  return requestJson<ExperimentDetail>(
    `/api/experiment?${query({ experiment_id: experimentId })}`,
    signal,
  )
}

export function fetchCase(
  experimentId: string,
  caseSlug: string,
  eventsAfter: number,
  signal?: AbortSignal,
) {
  return requestJson<CaseDetail>(
    `/api/case?${query({ experiment_id: experimentId, case: caseSlug, events_after: eventsAfter })}`,
    signal,
  )
}

export function fetchContextRequests(
  experimentId: string,
  caseSlug: string,
  signal?: AbortSignal,
) {
  return requestJson<ContextRequestSummary[]>(
    `/api/context/requests?${query({ experiment_id: experimentId, case: caseSlug })}`,
    signal,
  )
}

export function fetchAgentContexts(
  experimentId: string,
  caseSlug: string,
  signal?: AbortSignal,
) {
  return requestJson<AgentContextGroup[]>(
    `/api/context/agents?${query({ experiment_id: experimentId, case: caseSlug })}`,
    signal,
  )
}

export function fetchContextRequest(
  experimentId: string,
  caseSlug: string,
  requestId: string,
  signal?: AbortSignal,
) {
  return requestJson<ContextRequestDetail>(
    `/api/context/request?${query({ experiment_id: experimentId, case: caseSlug, request_id: requestId })}`,
    signal,
  )
}
