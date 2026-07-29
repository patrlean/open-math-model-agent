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
}

export interface QuestionOption {
  id: string
  label: string
  description?: string
}

export interface PendingQuestion {
  id: string
  question: string
  options: QuestionOption[]
  allow_custom: boolean
  asked_at: number
}

export interface PaperDelivery {
  pdf?: string
  pdf_name?: string
  tex?: string
  tex_name?: string
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
}

export type ProviderId = 'deepseek' | 'kimi' | 'minimax' | 'openai_compatible'

export interface ProviderPreset {
  id: ProviderId
  label: string
  default_model: string
  default_base_url: string
  api_key_configured: boolean
  api_key_hint?: string | null
}

export interface ProviderSettings {
  provider: ProviderId
  model: string
  base_url: string
  api_key_configured: boolean
  api_key_hint?: string | null
  presets: ProviderPreset[]
}

export interface RunDetail extends RunSummary {
  files?: string[]
  plan: string
  plan_tasks: PlanTask[]
  problem: string
  decisions: string
  results: Record<string, string>
  figures: string[]
  outputs: string[]
  paper: PaperDelivery
  events: AgentEvent[]
  run_log: string
  retry_of?: string
  pending_question?: PendingQuestion | null
  verification_settings?: VerificationSettings
  agent_settings?: AgentSettings
  subagent_settings?: SubagentSettings
  verifier_settings?: VerifierSettings
  usage?: ConversationUsage
}
