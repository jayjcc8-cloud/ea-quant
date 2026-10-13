import type { Candidate, SimulationPreparation } from './candidates'
import type { PaperEvents, PaperOverview } from './radian-api'

export type PaperRequest = {
  request_id: string; candidate_id: string; fingerprint: string; name: string
  prices: number[]; interval_seconds: number
}
export type PaperInstance = {
  strategy?: { id: string; version: number }; run_id: string; request: PaperRequest; source_job_id: string; state: string
  terminal_confirmed: boolean; stop_requested: boolean; observed_at: string
  created_at: string; detail?: string; overview: PaperOverview
}
export type PaperPreparation = SimulationPreparation & {
  source_job_id: string
  configuration: {
    strategy: { id: string; version?: number; parameters?: Record<string, string | number> }
    instrument: { venue: string; symbol: string }
    funding: { initial_cash: string; currency: string }
    risk: Record<string, string>; execution: Record<string, unknown>
  }
}
export type PaperExplanationEvidence = {
  schema: string; run_id: string; instance_name: string; candidate_id: string; source_job_id: string
  strategy: { id: string; version: number }; configuration_sha256: string
  market: { kind: string; interval_seconds: number; bounded_prices: number }
  observed_at: string; state: string
  state_scope: 'current' | 'historical' | 'stale' | 'unknown'
  historical: boolean; terminal_durable: boolean
  economics: { available: boolean; reason?: string | null } & Record<string, unknown>
  health: { available: boolean; reason?: string | null } & Record<string, unknown>
  limits: string[]
}
export type PaperExplanationMessage = {
  message_id: string; role: 'user' | 'assistant'; text: string; call_id: string; created_at: string
  evidence_observed_at?: string | null; evidence_scope?: string | null
}
export type PaperExplanationCall = {
  call_id: string; status: string; created_at: string; evidence?: PaperExplanationEvidence | null
}
export type PaperExplanationConversation = {
  run_id: string; context: PaperExplanationEvidence | null; context_error?: string; messages: PaperExplanationMessage[]
  calls: PaperExplanationCall[]; budget: { max_model_calls: number; max_backtests: number }
  route: { provider: string; model: string; endpoint?: string } | null
}
/** The one call an explanation turn may spend: no backtest, and by default one model call. */
export const paperExplanationBudget = { max_model_calls: 1, max_backtests: 0 }
export const paperExplanationDraftKey = (runId: string) => `quine.paper-ai.draft.${runId}`
export const paperExplanationPendingKey = (runId: string) => `quine.paper-ai.pending.${runId}`
export class PaperApiError extends Error {
  constructor(readonly status: number, message: string) { super(message) }
}
async function request<T>(url: string, body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 12000)
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify(body) }),
    })
    const value = await response.json()
    if (!response.ok) throw new PaperApiError(response.status, value.error?.message ?? '运行请求未确认')
    return value as T
  } finally { window.clearTimeout(timer) }
}
export const paperApi = {
  list: () => request<{ available: boolean; instances: PaperInstance[] }>('/api/paper/instances'),
  get: (id: string) => request<PaperInstance>(`/api/paper/instances/${encodeURIComponent(id)}`),
  start: (body: PaperRequest) => request<PaperInstance>('/api/paper/instances', body),
  stop: (id: string) => request<PaperInstance>(`/api/paper/instances/${encodeURIComponent(id)}/stop`, {}),
  candidates: () => request<{ candidates: Candidate[] }>('/api/candidates'),
  prepare: (id: string) => request<PaperPreparation>(`/api/candidates/${encodeURIComponent(id)}/simulation-preparation`),
  events: (id: string) => request<PaperEvents>(`/api/paper/events?run_id=${encodeURIComponent(id)}&limit=40`),
  conversation: (id: string) => request<PaperExplanationConversation>(`/api/paper/instances/${encodeURIComponent(id)}/conversation`),
  explain: (id: string, body: { call_id: string; text: string; approved_budget: typeof paperExplanationBudget }, signal?: AbortSignal) =>
    fetch(`/api/paper/instances/${encodeURIComponent(id)}/messages/stream`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' },
      body: JSON.stringify(body),
    }),
}
export type PaperApi = typeof paperApi
