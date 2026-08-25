export type ExperimentStatus =
  | 'prepared'
  | 'queued'
  | 'running'
  | 'orphaned'
  | 'killed'
  | 'completed'
  | 'completed_with_errors'
  | 'failed'
  | 'unknown'

export type CaseStatus =
  | 'queued'
  | 'running'
  | 'killed'
  | 'completed'
  | 'stopped'
  | 'failed'
  | 'unknown'

export interface GitInfo {
  commit?: string | null
  branch?: string | null
  dirty?: boolean
}

export interface ExperimentSettings {
  max_steps?: number
  sub_max_steps?: number
  max_workers?: number
  repetitions?: number
  verification_enabled?: boolean
  sandbox?: string
  provider?: string
  model?: string
  reasoning_effort?: string
  context_profile?: string | null
  compact_threshold_tokens?: number
  keep_tail_messages?: number
  compaction_strategy?: 'legacy_monolithic' | 'split_user_agent_v1' | string
  tool_result_externalize_threshold_tokens?: number
  tool_result_preview_chars?: number
  tool_prune_threshold_tokens?: number
  tool_prune_aggressive_threshold_tokens?: number
  tool_prune_recent_results?: number
}

export interface CaseSummary {
  name: string
  slug: string
  benchmark_case?: string
  repetition?: number | null
  repetitions?: number | null
  status: CaseStatus
  started_at?: number | null
  finished_at?: number | null
  duration_seconds?: number | null
  stop_reason?: string | null
  error?: string | null
  artifact_count: number
  last_activity?: number | null
  request_count: number
  latest_request_ts?: number | null
  latest_model?: string | null
}

export interface ExperimentSummary {
  id: string
  label: string
  status: ExperimentStatus
  submitted_at: number
  started_at?: number | null
  finished_at?: number | null
  source_sha256?: string | null
  git: GitInfo
  settings: ExperimentSettings
  cases: CaseSummary[]
  last_activity?: number | null
}

export interface ExperimentDetail extends ExperimentSummary {
  benchmark_source?: string | null
  config_source?: string | null
  supervisor_pid?: number | null
}

export interface AgentEvent {
  kind: string
  t?: number
  ts?: number
  step?: number
  subagent?: number
  name?: string
  text?: string
  reasoning_text?: string
  observation?: string
  task?: string
  total_tokens?: number
  context_tokens?: number
  elapsed_seconds?: number
  tool_calls?: [string, string][]
  tokens?: number
  strategy?: string
  compaction_index?: number
  summarizing?: number
  keeping?: number
  context_chars_before?: number
  context_chars_after?: number
  segment_chars_before?: number
  summary_chars_after?: number
  user_merged_chars?: number
  agent_summary_chars?: number
  compression_ratio?: number
  segment_compression_ratio?: number
  summary_calls?: number
  summary_latency_seconds?: number
  summary_usage?: Usage
  prior_summary_chars?: number
  delta_summary_chars?: number
  externalized_tool_results?: number
  prune_level?: 'moderate' | 'aggressive'
  pruned_tool_results?: number
  reference_only_tool_results?: number
  archived_tool_results?: number
  tool_result_pruning_by_tool?: Record<string, {
    count?: number
    tokens_before?: number
    tokens_after?: number
    tokens_saved?: number
  }>
  tool_result_tokens_before_estimate?: number
  tool_result_tokens_after_estimate?: number
  tool_result_tokens_saved_estimate?: number
  tool_result_log_files?: string[]
  [key: string]: unknown
}

export interface Artifact {
  path: string
  name: string
  kind: 'pdf' | 'image' | 'data' | 'text' | 'file'
  bytes: number
  modified_at: number
  url: string
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

export interface CaseDetail {
  experiment_id: string
  slug: string
  status: Record<string, unknown> & { status?: CaseStatus; started_at?: string; error?: string }
  workspace_ready: boolean
  events: AgentEvent[]
  events_cursor: number
  task: string
  plan: string
  decisions: string
  problem: string
  results: Record<string, string>
  artifacts: Artifact[]
  console_log: string
  usage: Usage
  context: {
    request_count?: number
    latest_request_ts?: number | null
    latest_model?: string | null
  }
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
  agent_scope?: string
  phase: string
  step?: number | null
  usage?: Usage
  message_count?: number
  tool_definition_count?: number
  estimated_input_tokens: number
}

export interface AgentContextGroup {
  key: string
  agent_role: string
  agent_scope: string
  request_count: number
  completed_count: number
  total_tokens: number
  estimated_input_tokens: number
  first_ts?: number | null
  latest_ts?: number | null
  latest_step?: number | null
  phases: string[]
  requests: ContextRequestSummary[]
}

export interface ContextItem {
  category: string
  label: string
  content: unknown
  source?: string | null
  estimated_tokens: number
}

export interface ContextRequestDetail extends ContextRequestSummary {
  context: Record<string, unknown>
  items: ContextItem[]
  raw_request: Record<string, unknown>
}
