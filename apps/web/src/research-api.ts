/**
 * RADIAN research task contract.
 *
 * These types mirror the persisted documents served by the installed engine
 * (src/ea/web/research.py). The task record is the only authority for the idea,
 * the draft and confirmed specs, the frozen budget, measured usage, the bound
 * backtest jobs and the failure classification; this client never derives or
 * invents a spec, a result, or an outcome.
 */

import type { SettingsStatus } from './radian-api'

/**
 * The model configuration projection this workspace presents.
 *
 * It extends the workspace settings status with the neutral protocol facts the
 * backend reports: which protocol the configured provider value selects, the
 * capabilities that protocol declares, the capabilities research requires, and
 * the reason a configuration is not usable. The UI never names a provider
 * constant of its own and never treats a custom endpoint as a different
 * protocol.
 */
export type ModelConfigurationStatus = SettingsStatus & {
  configuration_present?: boolean
  credential_available?: boolean
  request_validation?: 'not_verified'
  endpoint?: string | null
  protocol?: string | null
  capabilities?: { tool_use: boolean; structured_output: boolean; usage: boolean } | null
  required_capabilities?: string[]
  missing_capabilities?: string[]
  configuration_error?: string | null
  research_budget?: { max_model_calls: number; max_backtests: number }
}

export type ResearchTaskStatus =
  | 'drafting'
  | 'awaiting_confirmation'
  | 'confirmed'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'unsupported'

export type ResearchSpec = {
  strategy_id: string
  instrument: { venue: string; symbol: string }
  initial_cash: string
  rationale: string
  scenario_id: string | null
  dataset_id: string | null
  source_sha256: string | null
  parameters: Record<string, string | number>
  assumptions: string[]
}

export type ResearchError = { step: string; code: string; message: string }

export type ResearchReservation = {
  kind: string
  seq: number
  status: 'pending' | 'dispatched' | 'unknown'
  at: string
}

export type ResearchTask = {
  schema: 'radian.research-task.v1'
  task_id: string
  created_at: string
  updated_at: string
  status: ResearchTaskStatus
  raw_idea: string
  gap: string | null
  budget: { max_model_calls: number; max_backtests: number }
  usage: { model_calls: number; input_tokens: number; output_tokens: number; backtests: number; unavailable_model_calls?: number }
  spec_draft: ResearchSpec | null
  spec_confirmed: { version: number; confirmed_at: string; spec: ResearchSpec }[]
  jobs: string[]
  request_id: string | null
  error: ResearchError | null
  cancel_note: string | null
  last_model: { model: string; at: string; outcome: string; usage_available?: boolean; stop_reason?: string } | null
  reservation: ResearchReservation | null
  backtest_reservation: ResearchReservation | null
  spent_calls: number[]
  operation_generation: number
  operation_completion_generation: number
  operation_kind: string | null
  runtime_diagnostic: string | null
}

/** A local research request failure with the engine's own error code. */
export class ResearchRequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const payload = await response.json()
  if (!response.ok) {
    const detail = payload?.error
    throw new ResearchRequestError(
      detail?.code ?? 'request_failed',
      detail?.message ?? 'Research request failed',
    )
  }
  return payload as T
}

const writeHeaders = { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }

export const researchApi = {
  async listTasks(): Promise<ResearchTask[]> {
    return (await apiRequest<{ tasks: ResearchTask[] }>('/api/research/tasks')).tasks
  },
  createTask: (idea: string) =>
    apiRequest<ResearchTask>('/api/research/tasks', {
      method: 'POST',
      headers: writeHeaders,
      body: JSON.stringify({ idea }),
    }),
  getTask: (taskId: string) =>
    apiRequest<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}`),
  confirmTask: (taskId: string, autoRun = true) =>
    apiRequest<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/confirm`, {
      method: 'POST',
      headers: writeHeaders,
      body: JSON.stringify({ auto_run: autoRun }),
    }),
  retryTask: (taskId: string) =>
    apiRequest<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: 'POST',
      headers: writeHeaders,
      body: JSON.stringify({}),
    }),
  cancelTask: (taskId: string) =>
    apiRequest<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      headers: writeHeaders,
      body: JSON.stringify({}),
    }),
}
