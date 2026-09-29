/**
 * RADIAN workspace read contract.
 *
 * These types mirror the read-only backend documents served by the installed
 * engine (src/ea/web/radian.py). Every document marks absence explicitly:
 * `available: false` with a reason, never zero, empty success, or sample data.
 * The frontend must render those states honestly and must not recompute any
 * financial fact from the numbers shown here.
 */

export type WorkItemKind = 'backtest' | 'batch' | 'holdout' | 'candidate'

export type WorkItem = {
  id: string
  kind: WorkItemKind
  status: string | null
  created_at: string | null
  scenario_id?: string | null
  name?: string | null
  strategy_id?: string | null
  instrument?: { venue: string; symbol: string } | null
  member_count?: number
  source_job_id?: string | null
  holdout_job_id?: string | null
  decision?: string | null
  link: string
}

export type AttentionItem = {
  id: string
  kind: 'paper' | 'job' | 'alert'
  severity: string
  message: string
  observation?: string
  observed_at?: string | null
  run_id?: string
  job_id?: string
  status?: string
  state?: string
  operator_action?: string
}

/** The observed ea.paper-health.v1 projection, as re-observed by the reader. */
export type PaperHealth = {
  schema: string
  run_id?: string
  observed_at: string | null
  process_alive: boolean
  runtime_state?: string
  runtime_ready?: boolean
  storage_state?: string
  broker_state?: string
  market_state?: string
  strategy_heartbeat_state?: string
  kill_switch_state?: string
  reconciliation_state?: string
  recovery_state?: string
  projection_stale?: boolean
  operator_halt?: boolean
  trade_permitted?: boolean
  trade_reason?: string | null
  trade_permission_basis?: string | null
  trade_blocking_guard?: string | null
  reason_codes?: string[]
  candidate_id?: string
}

/** Explicit unavailable marker published when no health projection exists. */
export type HealthUnavailable = { schema: string; available: false; detail: string }

export type PaperOverview = {
  available: boolean
  /** Absence reason, or the run's own terminal reason when available. */
  reason?: string | null
  run_id?: string
  state?: string
  detail?: string
  candidate_id?: string
  account_id?: string
  symbol?: string
  currency?: string
  cash?: string
  equity?: string
  position?: string
  valuation_price?: string | null
  valuation_time?: string | null
  orders?: number
  fills?: number
  market_events?: number
  fact_events?: number
  ledger_sequence?: number
  reconciliation?: string
  risk_halted?: boolean
  kill_switch_halted?: boolean
  started_at?: string
  ended_at?: string
  updated_at?: string
  terminal_durable?: boolean
  incomplete?: boolean
  lease_held?: boolean
  health?: PaperHealth | HealthUnavailable | null
}

export type ModelStatus = {
  configured: boolean
  provider: string | null
  model: string | null
}

export type WorkspaceOverview = {
  schema: 'radian.workspace-overview.v1'
  recent_work: WorkItem[]
  attention: AttentionItem[]
  paper: PaperOverview
  model: ModelStatus
}

export type PaperEvent = {
  event?: string
  operation?: string
  outcome?: string
  run_id?: string
  account_id?: string
  candidate_id?: string
  [key: string]: unknown
}

export type PaperEvents = {
  available: boolean
  reason?: string
  run_id?: string
  events: PaperEvent[]
  count?: number
  total_lines?: number
  truncated?: boolean
}

export type SearchResult = {
  kind: 'scenario' | 'backtest' | 'batch' | 'holdout' | 'candidate'
  id: string
  label: string
  status: string | null
  link: string
}

export type SearchResponse = {
  schema: 'radian.search.v1'
  query: string
  results: SearchResult[]
}

export type SettingsStatus = {
  schema: 'radian.settings-status.v1'
  settings_file: string
  configured: boolean
  provider: string | null
  model: string | null
  base_url: string | null
  api_key_env: string | null
}

class ApiFailure extends Error {
  constructor(readonly code: string, message: string) {
    super(message)
  }
}

async function apiRequest<T>(path: string): Promise<T> {
  const response = await fetch(path)
  const payload = await response.json()
  if (!response.ok) {
    const detail = payload?.error
    throw new ApiFailure(detail?.code ?? 'request_failed', detail?.message ?? 'Request failed')
  }
  return payload as T
}

export const radianApi = {
  overview: () => apiRequest<WorkspaceOverview>('/api/workspace/overview'),
  paperOverview: (runId?: string) =>
    apiRequest<PaperOverview>(`/api/paper/overview${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`),
  paperEvents: (runId?: string, limit = 100) =>
    apiRequest<PaperEvents>(`/api/paper/events?limit=${limit}${runId ? `&run_id=${encodeURIComponent(runId)}` : ''}`),
  search: (query: string) => apiRequest<SearchResponse>(`/api/search?q=${encodeURIComponent(query)}`),
  settingsStatus: () => apiRequest<SettingsStatus>('/api/settings/status'),
}
