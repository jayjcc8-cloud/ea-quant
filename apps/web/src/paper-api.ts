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
}
export type PaperApi = typeof paperApi
