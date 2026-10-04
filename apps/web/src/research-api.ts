/**
 * RADIAN research task contract.
 *
 * These types mirror the persisted documents served by the installed engine
 * (src/ea/web/research.py). The task record is the only authority for the idea,
 * the draft and confirmed specs, the frozen budget, measured usage, the bound
 * backtest jobs and the failure classification; this client never derives or
 * invents a spec, a result, or an outcome.
 *
 * The same file carries the three host-verified surfaces this workspace offers
 * next to a task: the declared capability catalog and one recipe verification,
 * the controlled upload preview/publish pair, and declaration verification with
 * explicit registration. Each of them is a host verdict this client only reads
 * and displays. Registering a declaration and publishing a version are separate,
 * explicit user writes; nothing here selects a capability, installs a
 * declaration or dispatches a run on its own.
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
  /** Present only when the user selected capabilities explicitly. */
  selection?: PluginSelection
}

export type ResearchError = { step: string; code: string; message: string }

/**
 * The five layers this batch binds, in the host's own order. A layer the user
 * does not select keeps the host default route; a layer the user does select
 * names one exact version, never a range and never a "latest".
 */
export type LayerName = 'model' | 'data' | 'strategy' | 'validator' | 'backtest'

export const LAYER_ORDER: readonly LayerName[] = ['model', 'data', 'strategy', 'validator', 'backtest']

/** One layer's selection: exactly the two fields the host decodes. */
export type LayerSelection = { plugin_id: string; package_version: string }

export type PluginSelection = Partial<Record<LayerName, LayerSelection>>

/** One declared capability as the host read it, usable or refused with a reason. */
export type PluginCatalogEntry = {
  plugin_id: string
  kind: string
  package_version: string
  artifact_digest: string
  entrypoint: string
  effects: { declared: string[]; granted: string[]; host_granted: string[] }
  identity: Record<string, unknown>
  usable: boolean
  reason: { code: string; message: string } | null
}

export type PluginCatalog = {
  schema: 'ea.plugin-catalog.v1'
  supported_kinds: string[]
  plugins: PluginCatalogEntry[]
}

export type BoundCapability = {
  layer: LayerName
  plugin_id: string
  kind: string
  package_version: string
  artifact_digest: string
  entrypoint: string
  effects: string[]
  identity: Record<string, unknown>
}

/** The exact capability identity frozen for one accepted run. */
export type PluginBinding = {
  schema: 'ea.plugin-binding.v1'
  binding_sha256: string
  components: BoundCapability[]
}

/** One resolved selection: a verification verdict, never a dispatch. */
export type RecipeResolution = {
  schema: 'ea.plugin-recipe.v1'
  usable: boolean
  code: string | null
  message: string | null
  selection: PluginSelection | null
  binding: PluginBinding | null
}

/**
 * One declaration body. The host fixes the schema, the package version and the
 * declared-by value, and computes the integrity digest itself when the body does
 * not carry one; a body whose digest disagrees is refused, not repaired.
 */
export type DeclarationBody = {
  schema?: string
  declaration_id?: string
  kind?: string
  package_version?: string
  title?: string
  declared_by?: string
  rule?: string | null
  rule_history?: number | null
  constraints?: Record<string, unknown> | null
  artifact_digest?: string
}

/** The host's verdict for one declaration; `registered` is only true after storage. */
export type DeclarationVerdict = {
  usable: boolean
  code: string | null
  message: string | null
  declaration: DeclarationBody | null
  capability: PluginCatalogEntry | null
  registered: boolean
}

export type DeclaredDeclaration = DeclarationBody & {
  usable: boolean
  capability?: PluginCatalogEntry
  reason?: { code: string; message: string }
}

export type DeclarationCatalog = {
  schema: 'ea.plugin-declaration-catalog.v1'
  supported_kinds: string[]
  validator_constraints: string[]
  declaration_root_configured: boolean
  declarations: DeclaredDeclaration[]
}

/** The coverage facts the strict decoder derives from a bound input. */
export type DatasetQuality = {
  bar_count: number
  revision_count: number
  bar_durations_seconds: string[]
  observed_gap_count: number
  largest_gap_seconds: string | null
  bar_start_utc: string
  bar_end_utc: string
}

export type AvailabilityPolicy = {
  id: string
  version: number
  latency_seconds: number | null
  /** This host never records verified point-in-time availability for an upload. */
  pit_verified: false
  evidence: string
}

export const AVAILABILITY_PROVIDED = 'ea.availability.provided-in-capture.v1'
export const AVAILABILITY_SIMULATED = 'ea.availability.simulated-bar-end-plus-latency.v1'

/** The upload ceiling the host enforces; a larger body is refused before parsing. */
export const MAX_UPLOAD_CHARACTERS = 4 * 1024 * 1024

/** A bounded sample plus what the host could honestly conclude from it. */
export type UploadPreview = {
  schema: 'ea.local-upload-preview.v1'
  profile: string
  byte_count: number
  expected_header: string[]
  header: string[]
  header_ok: boolean
  rows: Record<string, string | number>[]
  row_count_sampled: number
  truncated: boolean
  full_validation: 'not_attempted' | 'passed' | 'failed'
  valid: boolean
  quality: DatasetQuality | null
  first_error: { code: string; message: string; record_number: number | null; field_name: string | null } | null
  limitations: string[]
}

/** One published, immutable dataset version, exactly as it was sealed. */
export type UploadVersion = {
  schema: 'ea.local-dataset-version.v1'
  dataset_id: string
  kind: string
  profile: string
  version: number
  published_at: string
  source: {
    declared_by: string
    asserted: boolean
    reference: string | null
    file_name: string | null
    license_reference: string | null
  }
  availability_policy: AvailabilityPolicy
  source_bytes_sha256: string
  source_byte_count: number
  raw_retained: boolean
  capture_sha256: string
  capture_byte_count: number
  data_sha256: string
  record_count: number
  instrument: { venue: string; symbol: string }
  quality: DatasetQuality
  limitations: string[]
  manifest_sha256: string
}

export type UploadPublishRequest = {
  content: string
  file_name?: string
  source_reference?: string
  license_reference?: string
  availability_policy: string
  latency_seconds?: number
}

/** One catalog entry; an upload carries its version evidence digest and policy. */
export type DatasetEntry = {
  dataset_id: string
  valid: boolean
  origin?: 'registered' | 'upload'
  source_sha256?: string
  capture_sha256?: string
  data_sha256?: string
  record_count?: number
  venue?: string
  symbol?: string
  replay_start_utc?: string
  replay_end_utc?: string
  availability_policy?: AvailabilityPolicy
  version_evidence_digest?: string
  error_code?: string
  message?: string
}

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
  /** The user's own selection for this task; absent means the host default route. */
  selection?: PluginSelection | null
  /** The exact capability identity frozen at confirmation; re-verified before dispatch. */
  pinned_binding?: PluginBinding | null
  /** One host-verified declaration the model drafted. Never installed by the model. */
  declaration_draft?: DeclarationVerdict | null
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
    // A write boundary reports its refusal either in the shared error envelope
    // or, for one verified-but-refused declaration, as its own verdict document.
    const detail = payload?.error ?? payload
    throw new ResearchRequestError(
      detail?.code ?? 'request_failed',
      detail?.message ?? 'Research request failed',
    )
  }
  return payload as T
}

const writeHeaders = { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }

const write = <T,>(path: string, body: unknown, method = 'POST') =>
  apiRequest<T>(path, { method, headers: writeHeaders, body: JSON.stringify(body) })

export const researchApi = {
  async listTasks(): Promise<ResearchTask[]> {
    return (await apiRequest<{ tasks: ResearchTask[] }>('/api/research/tasks')).tasks
  },
  createTask: (idea: string, selection?: PluginSelection) =>
    write<ResearchTask>(
      '/api/research/tasks',
      selection && Object.keys(selection).length ? { idea, selection } : { idea },
    ),
  getTask: (taskId: string) =>
    apiRequest<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}`),
  editSpec: (taskId: string, fields: Record<string, unknown>) =>
    write<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/spec`, fields, 'PATCH'),
  confirmTask: (taskId: string, autoRun = true) =>
    write<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/confirm`, {
      auto_run: autoRun,
    }),
  retryTask: (taskId: string) =>
    write<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/retry`, {}),
  cancelTask: (taskId: string) =>
    write<ResearchTask>(`/api/research/tasks/${encodeURIComponent(taskId)}/cancel`, {}),
  pluginCatalog: () => apiRequest<PluginCatalog>('/api/plugins/catalog'),
  resolveRecipe: (selection: PluginSelection) =>
    write<RecipeResolution>('/api/plugins/recipe/resolve', { selection }),
  listDeclarations: () => apiRequest<DeclarationCatalog>('/api/plugins/declarations'),
  validateDeclaration: (declaration: DeclarationBody) =>
    write<DeclarationVerdict>('/api/plugins/declarations/validate', { declaration }),
  registerDeclaration: (declaration: DeclarationBody) =>
    write<DeclarationVerdict>('/api/plugins/declarations', { declaration }),
  async listDatasets(): Promise<DatasetEntry[]> {
    return (await apiRequest<{ datasets: DatasetEntry[] }>('/api/datasets')).datasets
  },
  previewUpload: (content: string, fileName?: string) =>
    write<UploadPreview>(
      '/api/datasets/upload/preview',
      fileName ? { content, file_name: fileName } : { content },
    ),
  publishUpload: (request: UploadPublishRequest) =>
    write<UploadVersion & { datasets: DatasetEntry[] }>('/api/datasets/upload', request),
}
