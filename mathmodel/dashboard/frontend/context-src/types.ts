export type RunStatus =
  | 'running'
  | 'waiting_input'
  | 'done'
  | 'error'
  | 'stopped'
  | 'cancelled'
  | 'draft'
  | 'unknown'

export interface ContextRun {
  id: string
  name: string
  status: RunStatus
  created: number
  last_activity?: number | null
  request_count: number
  latest_request_ts?: number | null
  latest_model?: string | null
}

export interface Usage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  cached_input_tokens?: number
  uncached_input_tokens?: number
  unclassified_input_tokens?: number
  estimated_cost_cny?: number
}

export interface ContextRequestSummary {
  request_id: string
  sequence: number
  ts: number
  response_ts?: number | null
  duration_seconds?: number | null
  status: 'pending' | 'completed' | 'error'
  provider: string
  model: string
  agent_role: string
  agent_scope: string
  phase: string
  step?: number | null
  transport_attempt: number
  usage: Usage
  message_count: number
  tool_definition_count: number
  estimated_input_tokens: number
  error?: string | null
}

export type ContextCategory =
  | 'system_prompt'
  | 'working_memory'
  | 'system_instruction'
  | 'user_input'
  | 'assistant_response'
  | 'reasoning'
  | 'tool_call'
  | 'tool_result'
  | 'tool_definition'
  | 'metadata'

export interface ContextItem {
  category: ContextCategory
  label: string
  content: unknown
  message_index?: number | null
  source?: string | null
  metadata: Record<string, unknown>
  estimated_tokens: number
}

export interface ContextRequestDetail extends ContextRequestSummary {
  context: {
    agent_role?: string
    agent_scope?: string
    phase?: string
    step?: number
    system_prompt_source?: string
  }
  items: ContextItem[]
  raw_request: Record<string, unknown>
}

export type ToolMetricAgent = 'all' | 'main' | 'verifier' | 'subagent'

export interface ToolMetricCounters {
  total_calls: number
  completed_calls: number
  pending_calls: number
  interrupted_calls: number
  protocol_evaluated: number
  protocol_successes: number
  objective_evaluated: number
  objective_successes: number
  failed_calls: number
  timed_out_calls: number
  cancelled_calls: number
  compile_attempts: number
  compile_successes: number
  acceptance_attempts: number
  acceptance_successes: number
  verdict_attempts: number
  verdict_successes: number
  retry_attempts: number
  recovered_retries: number
}

export interface ToolMetricRow extends ToolMetricCounters {
  name: string
}

export interface ToolMetricGroup {
  summary: ToolMetricCounters
  tools: ToolMetricRow[]
}

export interface ToolMetrics {
  run_status: RunStatus
  groups: Record<ToolMetricAgent, ToolMetricGroup>
  definitions: {
    completed: string
    protocol: string
    objective: string
    retry: string
  }
}
