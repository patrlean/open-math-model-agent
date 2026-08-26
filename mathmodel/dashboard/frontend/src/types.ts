export type RunStatus = 'draft' | 'running' | 'waiting_input' | 'done' | 'error' | 'stopped' | 'cancelled' | 'unknown'

export interface RunSummary {
  id: string
  name: string
  task: string
  created: number
  status: RunStatus
  has_pdf: boolean
  failure_reason?: string
  last_activity?: number
}

export interface PlanTask {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'done' | 'blocked'
  note?: string
  result?: string
}

export interface VerificationIssue {
  severity: 'critical' | 'major' | 'minor'
  category: string
  message: string
  evidence: string
  required_fix: string
}

export interface VerificationUsage {
  triage_tokens?: number
  scope_tokens?: number
  synthesis_tokens?: number
  reported_total_tokens?: number
  model_verification_skipped?: boolean
}

export interface AgentEvent {
  kind: string
  id?: string
  ts?: number
  t?: number
  subagent?: number
  step?: number
  total_tokens?: number
  context_tokens?: number
  text?: string
  reasoning_text?: string
  name?: string
  observation?: string
  timed_out?: boolean
  elapsed_seconds?: number
  task?: string
  tokens?: number
  status?: string
  tool_calls?: [string, string][]
  summarizing?: number
  keeping?: number
  attempt?: number
  phase?: string
  role?: 'lead-triage' | 'lead-synthesis' | 'subagent' | string
  scope_id?: string
  scope_title?: string
  scope_count?: number
  issue_count?: number
  verdict?: 'PASS' | 'REVISE' | 'INCONCLUSIVE'
  summary?: string
  issues?: VerificationIssue[]
  verification_usage?: VerificationUsage
  question_kind?: 'question' | 'change_confirmation'
  title?: string
  question?: string
  impacts?: PendingQuestion['impacts']
  budget?: PendingQuestion['budget']
  options?: QuestionOption[]
  allow_custom?: boolean
  asked_at?: number
  change_request_id?: string | null
  answered?: boolean
  selected_option_id?: string | null
  change_action?: string | null
}

export interface QuestionOption {
  id: string
  label: string
  description?: string
}

export interface PendingQuestion {
  id: string
  kind?: 'question' | 'change_confirmation'
  title?: string
  summary?: string
  question: string
  impacts?: Array<{
    target: string
    change: string
    reason?: string
  }>
  budget?: {
    currency?: string
    estimated_additional_cost?: number
    max_additional_cost?: number
    note?: string
  } | null
  options: QuestionOption[]
  allow_custom: boolean
  asked_at: number
  change_request_id?: string | null
}

export interface ProjectRevision {
  id: string
  number: number
  parent_revision_id?: string | null
  trigger_type: 'initial' | 'change' | 'material' | string
  title: string
  summary: string
  status: string
  created_at: number
  updated_at: number
  completed_at?: number | null
  change_request_id?: string | null
  budget?: PendingQuestion['budget']
  usage_baseline_cny?: number
}

export interface ChangeRequest {
  id: string
  base_revision_id: string
  revision_id?: string | null
  status: 'pending' | 'confirmed' | 'adjusted' | 'cancelled' | string
  title: string
  summary: string
  impacts: NonNullable<PendingQuestion['impacts']>
  budget?: PendingQuestion['budget']
  created_at: number
  resolved_at?: number | null
}

export interface ProjectState {
  id: string
  title: string
  current_revision_id: string
  active_revision_id: string
  current_revision?: ProjectRevision | null
  active_revision?: ProjectRevision | null
  revisions: ProjectRevision[]
  deliverable_revisions?: RevisionDeliverables[]
  change_requests: ChangeRequest[]
  settings: {
    revision_budget_limit_cny: number
  }
}

export interface ProjectBudgetSettings {
  revision_budget_limit_cny: number
  default_revision_budget_limit_cny: number
  min_revision_budget_limit_cny: number
  max_revision_budget_limit_cny: number
  currency: 'CNY'
}

export interface PaperDelivery {
  pdf?: string
  pdf_name?: string
  tex?: string
  tex_name?: string
}

export interface SourceDelivery {
  path: string
  name: string
  size: number
}

export interface RevisionDeliverables {
  revision_id: string
  number: number
  title: string
  summary: string
  status: string
  is_current: boolean
  is_active: boolean
  paper: PaperDelivery
  source_files: SourceDelivery[]
}

export interface VerificationSettings {
  max_attempts: number
  default_max_attempts: number
  min_attempts: number
  max_allowed_attempts: number
  is_custom: boolean
}

export interface AgentSettings {
  max_steps: number
  default_max_steps: number
  min_steps: number
  max_allowed_steps: number
  is_custom: boolean
}

export interface VerifierSettings {
  max_steps: number
  default_max_steps: number
  min_steps: number
  max_allowed_steps: number
  is_custom: boolean
}

export interface SubagentSettings {
  max_steps: number
  default_max_steps: number
  min_steps: number
  max_allowed_steps: number
  is_custom: boolean
}

export interface ConversationUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_input_tokens: number
  uncached_input_tokens: number
  unclassified_input_tokens: number
  estimated_cost_cny: number
  priced_tokens: number
  unpriced_tokens: number
  cache_breakdown_complete: boolean
  pricing_complete: boolean
  currency: 'CNY'
  rates_per_million?: {
    cached_input?: number
    uncached_input?: number
    output?: number
  }
  pricing_policy?: {
    rates_by_model: Record<string, {
      cached_input: number
      uncached_input: number
      output: number
    }>
    peak: {
      enabled: boolean
      timezone: string
      multiplier: number
      windows: [string, string][]
    }
  }
  external_model_usage?: Array<{
    tool: string
    provider: string
    model: string
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    unclassified_input_tokens: number
    estimated_cost_cny: number
    priced_tokens: number
    unpriced_tokens: number
    cache_breakdown_complete: boolean
    pricing_complete: boolean
  }>
}

export type ProviderId = 'deepseek' | 'kimi' | 'minimax' | 'openai_compatible'
export type ReasoningEffort = 'low' | 'high' | 'max'
export type CredentialMode = 'server' | 'user'

export interface ProviderModelOption {
  id: string
  label: string
  description: string
  is_default: boolean
}

export interface ProviderPreset {
  id: ProviderId
  label: string
  default_model: string
  default_base_url: string
  api_key_configured: boolean
  api_key_hint?: string | null
  model_options: ProviderModelOption[]
}

export interface ProviderSettings {
  credential_mode: CredentialMode
  provider: ProviderId
  model: string
  base_url: string
  reasoning_effort: ReasoningEffort
  api_key_configured: boolean
  api_key_hint?: string | null
  server_provider: ProviderId
  server_model: string
  user_provider?: ProviderId | null
  user_model?: string | null
  user_base_url?: string | null
  user_reasoning_effort?: ReasoningEffort | null
  presets: ProviderPreset[]
}

export interface AccountInfo {
  id: number
  email: string
  username?: string
  full_name?: string
  avatar_url?: string
  user_type: 'basic' | 'pro'
  balance: number
}

export interface RunDetail extends RunSummary {
  files?: string[]
  plan: string
  plan_tasks: PlanTask[]
  problem: string
  decisions: string
  results: Record<string, string>
  figures: string[]
  source_files: SourceDelivery[]
  outputs: string[]
  paper: PaperDelivery
  events: AgentEvent[]
  run_log: string
  retry_of?: string
  stop_reason?: 'max_steps' | 'cancelled' | 'done' | 'verification_failed' | string | null
  pending_question?: PendingQuestion | null
  verification_settings?: VerificationSettings
  agent_settings?: AgentSettings
  subagent_settings?: SubagentSettings
  verifier_settings?: VerifierSettings
  usage?: ConversationUsage
  project: ProjectState
  project_budget_settings?: ProjectBudgetSettings
}
