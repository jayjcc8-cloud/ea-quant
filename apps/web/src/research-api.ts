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
  schema: 'ea.local-dataset-version.v1' | 'ea.local-dataset-version.v2'
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
  /** Present only on a version an explicit mapping produced. */
  mapping_evidence?: MappingEvidence
  mapping_evidence_sha256?: string
}

/**
 * One explicit mapping declaration, in the mapper's own vocabulary: every strict
 * destination field is either a source column name or, for the metadata fields
 * only, the constant to write, and every timestamp field in use names one exact
 * allowlisted form. Nothing here is inferred from the data.
 */
export type MappingDeclaration = {
  columns: Record<string, string>
  constants: Record<string, string>
}

export type TimeDeclaration = {
  timezone: string
  formats: Record<string, string>
}

/** The three exact timestamp forms this host accepts for a mapped source. */
export const TIME_FORMAT_IDS = [
  'utc-microseconds',
  'utc-seconds',
  'offset-microseconds',
  'offset-seconds',
  'naive-microseconds',
  'naive-seconds',
] as const

/**
 * The whole explicit mapped-mode decision: the declaration, and how the mapped
 * capture became available. It is one object so that the exact thing a preview
 * verified is the exact thing a publication sends.
 */
export type UploadDeclaration = {
  mode: 'explicit-mapping'
  mapping: MappingDeclaration
  time_declaration: TimeDeclaration
  availability_policy: string
  latency_seconds?: number
}

/** The mapper's own sealed evidence for one mapped version. */
export type MappingEvidence = {
  schema: 'ea.local-csv-mapping-evidence.v1'
  version: number
  mapper: string
  profile: string
  declaration: { mapping: MappingDeclaration; time_declaration: TimeDeclaration }
  result: Record<string, unknown>
}

/** The bounded part of one mapped version's evidence that a catalog entry names. */
export type MappingSummary = {
  schema: 'ea.local-csv-mapping-evidence.v1'
  mapper: string
  evidence_sha256: string
  mapping: MappingDeclaration
  time: { timezone: string; formats: Record<string, string>; target_format: string }
  capture: { byte_count: number; sha256: string; data_sha256: string; record_count: number }
  source: { byte_count: number; sha256: string; record_count: number }
}

/**
 * What the host verified about one mapped source before anything was published.
 *
 * The verdict is the mapper's own: it normalizes the whole source and the strict
 * decoder re-reads the whole capture, so ``valid`` says the entire file maps.
 * The sample is bounded exactly as the strict preview's is, and the evidence
 * digest names the exact declaration a publication would seal.
 */
export type MappedPreview = {
  schema: 'ea.local-csv-mapping-preview.v1'
  mode: 'explicit-mapping'
  valid: boolean
  first_error: { code: string; message: string; record_number?: number | null; field_name?: string | null } | null
  byte_count: number
  profile?: string
  source?: { byte_count: number; sha256: string; record_count: number; stored_here: boolean }
  capture?: { byte_count: number; sha256: string; data_sha256: string; record_count: number }
  mapping?: MappingDeclaration
  time?: { timezone: string; formats: Record<string, string>; target_format: string }
  quality?: DatasetQuality
  preview?: { rows: Record<string, string | number>[]; row_count_sampled: number; truncated: boolean; valid: boolean }
  mapping_evidence?: MappingEvidence | null
  mapping_evidence_sha256?: string | null
  limitations: string[]
}

export type UploadPreviewResponse = UploadPreview | MappedPreview

/**
 * The host's own built-in fixture, served so that offline preview, publication
 * and catalog selection can be exercised with no provider and no network.
 *
 * It is the only body the host will ever mark synthetic, and loading it is the
 * only thing that marks an import synthetic: a pasted response is always the
 * user's own, never re-labelled by a request.
 */
export type OfflineHistoryExample = {
  schema: 'ea.offline-history-example.v1'
  example_id: string
  synthetic: true
  label: string
  note: string
  adapter: { provider: Record<string, unknown>; normalizer: Record<string, unknown> }
  media_type: string
  record_field_count: number
  documented_fields: string[]
  venue: string
  symbol: string
  interval: string
  start_ms: number
  end_ms: number
  latency_seconds: number
  record_count: number
  response_body: string
  response_sha256: string
  limitations: string[]
}

/**
 * Exactly the fixed assumptions and assertions one offline import binds.
 *
 * The bytes on screen plus these fields are the whole preview identity, so a
 * publication that changes either is publishing something nobody previewed. A
 * user paste states its source, rights and intended use; the built-in example
 * states none of them and refuses one.
 */
export type OfflineHistoryDeclaration = {
  venue: string
  symbol: string
  interval: string
  start_ms: number
  end_ms: number
  latency_seconds: number
  synthetic: boolean
  declared_source?: string
  declared_rights?: string
  intended_use?: string
}

/**
 * What the host read offline from one pasted response.
 *
 * ``valid`` is the adapter's own verdict over the whole response, and
 * ``preview_sha256`` names the exact identity a publication must return. The
 * host clock stamps are informational: they are outside that identity.
 */
export type OfflineHistoryPreview = {
  schema: 'ea.offline-history-preview.v1'
  mode: 'offline-history'
  valid: boolean
  first_error: { code: string; message: string; record_number?: number | null; field_name?: string | null } | null
  synthetic: boolean | null
  provenance?: string
  provider?: Record<string, unknown>
  normalizer?: Record<string, unknown>
  byte_count: number
  response_sha256?: string
  envelope_sha256?: string
  capture?: { sha256: string; byte_count: number; data_sha256: string; record_count: number }
  instrument?: { venue: string; symbol: string }
  query?: Record<string, unknown>
  observed_now_utc?: string
  ingested_at_utc?: string
  availability_policy?: AvailabilityPolicy
  quality?: DatasetQuality
  preview?: {
    rows: Record<string, string | number>[]
    row_count_sampled: number
    truncated: boolean
    valid: boolean
  }
  declarations?: { source: string | null; rights: string | null; intended_use: string | null }
  preview_sha256: string | null
  limitations: string[]
}

/** One published offline version: the pasted response, sealed and re-derivable. */
export type OfflineHistoryVersion = Omit<UploadVersion, 'schema' | 'source'> & {
  schema: 'ea.local-dataset-version.v4'
  source: UploadVersion['source'] & { intended_use?: string | null; provenance?: string }
  provider_evidence?: Record<string, unknown>
  provider_evidence_sha256?: string
}

/**
 * One statically inspected local strategy package. Inspection establishes no
 * trust and runs nothing: it reports the identity a captured container actually
 * is, so an operator can decide about exactly that identity.
 */
export type LocalStrategyCapability = {
  schema: 'ea.plugin-local-strategy.v1'
  warning: string
  static_only: true
  trust_established: false
  plugin_id: string
  package_version: string
  manifest_digest: string
  entrypoint: string
  effects: string[]
  identity: Record<string, unknown>
  package: {
    package_id: string
    schema_version: number
    artifact_sha256: string
    source: { kind: 'local-package'; package_id: string; artifact_sha256: string }
  }
  descriptor: {
    strategy_id?: string
    strategy_version?: number
    display_name?: string
    parameters?: { name: string; type?: string; default?: unknown }[]
  }
  manifest: Record<string, unknown>
}

/** What one workspace's single local package slot currently is. */
export type LocalStrategySlot = {
  schema: 'ea.local-strategy-slot.v1'
  registered: boolean
  trusted: boolean
  revoked: boolean
  scenario_id: string
  base_scenario_id?: string | null
  registered_at?: string | null
  capability: LocalStrategyCapability | null
  grant: {
    schema: 'ea.plugin-local-strategy-grant.v1'
    trusted: boolean
    plugin_id: string
    package_version: string
    manifest_digest: string
    implementation_digest: string
    effects: string[]
  } | null
  warning: string
  limitations: string[]
}

/** One explicit trust decision over exactly the identity that was inspected. */
export type LocalStrategyRegisterRequest = {
  content_base64: string
  manifest: Record<string, unknown>
  trusted: true
  effects: string[]
  base_scenario_id: string
  expected_plugin_id: string
  expected_package_version: string
  expected_manifest_digest: string
  expected_implementation_digest: string
}
/** The one discriminator between the two preview documents the host returns. */
export function isMappedPreview(preview: UploadPreviewResponse): preview is MappedPreview {
  return preview.schema === 'ea.local-csv-mapping-preview.v1'
}

export type UploadPublishRequest = {
  /** Exactly one payload field: strict UTF-8 text, or a file's own bytes in base64. */
  content?: string
  content_base64?: string
  file_name?: string
  mode?: 'strict' | 'explicit-mapping'
  mapping?: MappingDeclaration
  time_declaration?: TimeDeclaration
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
  /** Present only for a version an explicit mapping produced. */
  mapping?: MappingSummary
  /** Present only for a version a provider transport or an offline paste produced. */
  provider?: Record<string, unknown>
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
  previewUpload: (content: string, fileName?: string, declaration?: UploadDeclaration) =>
    write<UploadPreviewResponse>('/api/datasets/upload/preview', {
      content,
      ...(fileName ? { file_name: fileName } : {}),
      ...(declaration ?? {}),
    }),
  /**
   * A preview of a file's own bytes, carried as base64 so nothing is re-encoded
   * on the way to the host: a BOM, a CRLF ending or a non-ASCII column arrives
   * exactly as the browser read it.
   */
  previewUploadFile: (
    contentBase64: string,
    declaration: UploadDeclaration,
    fileName?: string,
  ) =>
    write<UploadPreviewResponse>('/api/datasets/upload/preview', {
      content_base64: contentBase64,
      file_name: fileName,
      ...declaration,
    }),
  publishUpload: (request: UploadPublishRequest) =>
    write<UploadVersion & { datasets: DatasetEntry[] }>('/api/datasets/upload', request),
  /**
   * The host's labelled synthetic example. Loading it is what marks an import
   * synthetic; nothing a caller sends can mark a pasted response synthetic.
   */
  offlineHistoryExample: () =>
    apiRequest<OfflineHistoryExample>('/api/datasets/offline-history/example'),
  previewOfflineHistory: (content: string, declaration: OfflineHistoryDeclaration) =>
    write<OfflineHistoryPreview>('/api/datasets/offline-history/preview', {
      content,
      ...declaration,
    }),
  /**
   * Publish exactly the preview this host verified. ``previewSha256`` is the
   * identity the preview named; the host recomputes it from these bytes and
   * declarations and refuses a publication that names anything else.
   */
  publishOfflineHistory: (
    content: string,
    declaration: OfflineHistoryDeclaration,
    previewSha256: string,
  ) =>
    write<OfflineHistoryVersion & { datasets: DatasetEntry[] }>('/api/datasets/offline-history', {
      content,
      ...declaration,
      preview_sha256: previewSha256,
    }),
  localStrategySlot: () => apiRequest<LocalStrategySlot>('/api/plugins/local-strategy'),
  inspectLocalStrategy: (contentBase64: string, manifest: Record<string, unknown>) =>
    write<LocalStrategyCapability>('/api/plugins/local-strategy/inspect', {
      content_base64: contentBase64,
      manifest,
    }),
  registerLocalStrategy: (request: LocalStrategyRegisterRequest) =>
    write<LocalStrategySlot>('/api/plugins/local-strategy/register', request),
  revokeLocalStrategy: () =>
    write<LocalStrategySlot>('/api/plugins/local-strategy/revoke', {}),
}
