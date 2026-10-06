/**
 * SCRIPTED FIXTURES for the guided-upload first-use path — bounded, synthetic,
 * in memory only.
 *
 * Two things live here and nothing else:
 *
 * - A handful of hand-written CSV sources (three data rows each), used both by
 *   the acceptance tests and by the manual preview page so a reviewer sees the
 *   real, unchanged panel over a known input.
 * - A small finite set of *scripted host answers*: one accepted preview, five
 *   refusals, one published version. Each answer is a fixed document copied from
 *   the shape the host publishes (`ea.local-csv-mapping-preview.v1`,
 *   `ea.local-dataset-version.v2`), with its code and sentence written by hand.
 *
 * This module deliberately contains no CSV reader, no mapper, no validator and no
 * derivation of any kind: nothing here parses a source, checks a declaration or
 * decides whether one is acceptable. The real host does all of that, and the
 * unchanged integration tests exercise it. An answer below is selected by name —
 * never computed from the input — so a test that asserts a refusal proves what the
 * panel *renders* and what it *sends*, not that this file can judge a declaration.
 *
 * `pit_verified` is false everywhere, because no upload is ever verified
 * point-in-time data, and nothing here is a quality or licensing claim.
 */
import type { ApiAdapter } from '../src/app'
import {
  AVAILABILITY_PROVIDED,
  type DatasetEntry,
  type LocalStrategySlot,
  type MappedPreview,
  type ModelConfigurationStatus,
  type PluginCatalog,
  type UploadPublishRequest,
  type UploadVersion,
} from '../src/research-api'

/** One synthetic vendor-shaped export: its own column names, CRLF, a quoted
 * comma, an escaped quote, and one column no declaration needs. */
export const SYNTHETIC_CSV =
  [
    'bar_start,bar_end,o,h,l,c,v,seq,mic,"tick,er","say ""hi"""',
    '2026-01-05T22:30:00,2026-01-05T23:30:00,100.0,101.0,99.0,100.5,10.0,0,XNAS,AAPL,2026-01-05T23:30:45',
    '2026-01-05T23:30:00,2026-01-06T00:30:00,100.5,102.0,100.0,101.5,12.0,1,XNAS,AAPL,2026-01-06T00:30:45',
    '2026-01-06T00:30:00,2026-01-06T01:30:00,101.5,102.5,101.0,102.0,9.0,2,XNAS,AAPL,2026-01-06T01:30:45',
  ].join('\r\n') + '\r\n'

/** The same export after a vendor rename: the availability column is now `seen`. */
export const SYNTHETIC_CSV_ALTERNATE =
  [
    'bar_start,bar_end,o,h,l,c,v,seq,mic,"tick,er",seen',
    '2026-01-05T22:30:00,2026-01-05T23:30:00,100.0,101.0,99.0,100.5,10.0,0,XNAS,AAPL,yes',
    '2026-01-05T23:30:00,2026-01-06T00:30:00,100.5,102.0,100.0,101.5,12.0,1,XNAS,AAPL,yes',
    '2026-01-06T00:30:00,2026-01-06T01:30:00,101.5,102.5,101.0,102.0,9.0,2,XNAS,AAPL,yes',
  ].join('\r\n') + '\r\n'

export const SYNTHETIC_CSV_DUPLICATE_HEADER = 'bar_start,c,c\r\n1,2,3\r\n'
export const SYNTHETIC_CSV_EMPTY_HEADER_NAME = 'bar_start,,c\r\n1,2,3\r\n'
/** A quote inside a name: neither `close` nor one whole quoted name. */
export const SYNTHETIC_CSV_QUOTE_IN_NAME = 'bar_start,clo"se",c\r\n1,2,3\r\n'
/** A quoted name with more characters right after the closing quote. */
export const SYNTHETIC_CSV_AFTER_QUOTE = 'bar_start,"close"x,c\r\n1,2,3\r\n'
export const SYNTHETIC_CSV_BYTE_ORDER_MARK = '﻿' + SYNTHETIC_CSV

/** The name a selected-file case gives the synthetic export above. */
export const SYNTHETIC_FILE_NAME = 'vendor-export.csv'

/** One fixed digest string; no digest is computed anywhere in this file. */
export const SYNTHETIC_DIGEST = 'e3b0c442' + '9'.repeat(56)

/** The declaration the `guided` state's controls describe, as a scripted answer. */
const SCRIPTED_MAPPING = {
  columns: {
    venue: 'mic',
    symbol: 'tick,er',
    interval_start: 'bar_start',
    interval_end: 'bar_end',
    open: 'o',
    high: 'h',
    low: 'l',
    close: 'c',
    volume: 'v',
    source_sequence: 'seq',
    available_at: 'say "hi"',
  },
  constants: { schema_version: '1', adjustment: 'raw', source: 'vendor.a', revision: '0' },
}

const SCRIPTED_TIME = {
  timezone: 'Asia/Shanghai',
  formats: {
    interval_start: 'naive-seconds',
    interval_end: 'naive-seconds',
    available_at: 'naive-seconds',
  },
}

const LIMITATIONS = [
  'Scripted preview fixture: no engine, no store and no network are involved.',
  'Nothing here is verified point-in-time data and no version is ever published by this file.',
]

/** The one accepted answer: a whole source that normalized and re-decoded. */
export const MAPPED_PREVIEW_OK: MappedPreview = {
  schema: 'ea.local-csv-mapping-preview.v1',
  mode: 'explicit-mapping',
  valid: true,
  first_error: null,
  byte_count: SYNTHETIC_CSV.length,
  profile: 'ea-phase1-ohlcv-csv-v1',
  source: { byte_count: SYNTHETIC_CSV.length, sha256: SYNTHETIC_DIGEST, record_count: 3, stored_here: false },
  capture: { byte_count: SYNTHETIC_CSV.length, sha256: SYNTHETIC_DIGEST, data_sha256: SYNTHETIC_DIGEST, record_count: 3 },
  mapping: SCRIPTED_MAPPING,
  time: { ...SCRIPTED_TIME, target_format: '%Y-%m-%dT%H:%M:%S.%fZ' },
  quality: {
    bar_count: 3,
    revision_count: 0,
    bar_durations_seconds: ['3600'],
    observed_gap_count: 0,
    largest_gap_seconds: null,
    bar_start_utc: '2026-01-05T22:30:00.000000Z',
    bar_end_utc: '2026-01-06T01:30:00.000000Z',
  },
  preview: { rows: [], row_count_sampled: 0, truncated: false, valid: true },
  mapping_evidence: null,
  mapping_evidence_sha256: SYNTHETIC_DIGEST,
  limitations: LIMITATIONS,
}

/** The refusals this preview can be put in, each chosen by name, none computed. */
export type RefusalKind =
  | 'invalid_header'
  | 'invalid_encoding'
  | 'mapping_column_missing'
  | 'mapping_declaration'
  | 'upload_policy'
  | 'empty_source'

const refusal = (
  code: RefusalKind,
  message: string,
  extra: { recordNumber?: number; fieldName?: string } = {},
): MappedPreview => ({
  schema: 'ea.local-csv-mapping-preview.v1',
  mode: 'explicit-mapping',
  valid: false,
  first_error: {
    code,
    message,
    record_number: extra.recordNumber ?? null,
    field_name: extra.fieldName ?? null,
  },
  byte_count: SYNTHETIC_CSV.length,
  // No `profile`, `source`, `capture`, `mapping`, `time` or `quality`: a refused
  // preview has nothing to report about a source the host did not accept.
  mapping_evidence: null,
  limitations: LIMITATIONS,
})

export const REFUSALS: Record<RefusalKind, MappedPreview> = {
  invalid_header: refusal('invalid_header', 'the header repeats a name or names none of its columns', {
    recordNumber: 1,
  }),
  invalid_encoding: refusal('invalid_encoding', 'the source CSV starts with a byte-order mark', {
    recordNumber: 1,
  }),
  mapping_column_missing: refusal('mapping_column_missing', 'the source has no column with that name', {
    recordNumber: 1,
    fieldName: 'available_at',
  }),
  mapping_declaration: refusal('mapping_declaration', 'every strict field must be declared exactly once', {
    fieldName: 'schema_version',
  }),
  upload_policy: refusal('upload_policy', 'the source states how it became available', {}),
  empty_source: refusal('empty_source', 'the source is empty', {}),
}

/**
 * One scripted published version. No upload is ever verified point-in-time data,
 * so `pit_verified` is false and `evidence` says the uploader asserted it.
 */
export const SYNTHETIC_VERSION: UploadVersion = {
  schema: 'ea.local-dataset-version.v2',
  dataset_id: 'preview-synthetic-version',
  kind: 'ohlcv',
  profile: 'ea-phase1-ohlcv-csv-v1',
  version: 1,
  published_at: '2026-01-06T00:00:00.000000Z',
  source: {
    declared_by: 'preview',
    asserted: true,
    reference: 'scripted preview fixture',
    file_name: null,
    license_reference: null,
  },
  availability_policy: {
    id: AVAILABILITY_PROVIDED,
    version: 1,
    latency_seconds: null,
    pit_verified: false,
    evidence: 'asserted-by-uploader: scripted fixture, never verified point-in-time',
  },
  source_bytes_sha256: SYNTHETIC_DIGEST,
  source_byte_count: SYNTHETIC_CSV.length,
  raw_retained: true,
  capture_sha256: SYNTHETIC_DIGEST,
  capture_byte_count: SYNTHETIC_CSV.length,
  data_sha256: SYNTHETIC_DIGEST,
  record_count: 3,
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  quality: {
    bar_count: 3,
    revision_count: 0,
    bar_durations_seconds: ['3600'],
    observed_gap_count: 0,
    largest_gap_seconds: null,
    bar_start_utc: '2026-01-05T22:30:00.000000Z',
    bar_end_utc: '2026-01-06T01:30:00.000000Z',
  },
  limitations: ['Scripted preview fixture: nothing was published and nothing was verified.'],
  manifest_sha256: SYNTHETIC_DIGEST,
}

const DATASETS: DatasetEntry[] = [
  {
    dataset_id: 'preview-synthetic-version',
    valid: true,
    origin: 'upload',
    source_sha256: SYNTHETIC_DIGEST,
    capture_sha256: SYNTHETIC_DIGEST,
    data_sha256: SYNTHETIC_DIGEST,
    record_count: 3,
    venue: 'XNAS',
    symbol: 'AAPL',
    replay_start_utc: '2026-01-05T22:30:00.000000Z',
    replay_end_utc: '2026-01-06T01:30:00.000000Z',
    availability_policy: SYNTHETIC_VERSION.availability_policy,
  },
]

const CATALOG: PluginCatalog = {
  schema: 'ea.plugin-catalog.v1',
  supported_kinds: ['model', 'data', 'strategy', 'validator', 'backtest-method'],
  plugins: [],
}

const SLOT: LocalStrategySlot = {
  schema: 'ea.local-strategy-slot.v1',
  registered: false,
  trusted: false,
  revoked: false,
  scenario_id: 'preview-local-package.yaml',
  capability: null,
  grant: null,
  warning: 'The scripted preview runs no package.',
  limitations: ['Scripted preview: no package can be inspected or trusted here.'],
}

const STATUS: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1',
  settings_file: '/scripted/preview/settings.json',
  configured: false,
  configuration_present: false,
  credential_available: false,
  request_validation: 'not_verified',
  provider: null,
  model: null,
  base_url: null,
  api_key_env: null,
  endpoint: null,
  protocol: null,
  capabilities: null,
  required_capabilities: [],
  missing_capabilities: [],
  configuration_error: 'the scripted preview configures no model provider',
  research_budget: { max_model_calls: 10, max_backtests: 5 },
}

const unsupported = (what: string) => async (): Promise<never> => {
  throw new Error(`the scripted preview ${what}`)
}

/** One file's bytes as standard base64, exactly as a browser would have read them. */
export function decodeBase64(contentBase64: string): string {
  const binary = atob(contentBase64)
  const bytes = Uint8Array.from(binary, character => character.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

/**
 * One in-memory adapter over the fixtures above.
 *
 * Every member either answers from a constant in this file or throws, so a route
 * this preview does not cover fails loudly instead of reading a made-up document.
 * `preview` is the scripted answer this adapter gives for an upload: the accepted
 * document by default, or one of the refusals, chosen by the caller by name.
 */
export function scriptedAdapter(options: { preview?: MappedPreview } = {}): ApiAdapter {
  const answer = options.preview ?? MAPPED_PREVIEW_OK
  return {
    listDatasets: async () => DATASETS,
    listScenarios: async () => [],
    listBacktests: async () => [],
    listHoldouts: async () => [],
    holdoutScenarios: async () => [],
    listResearchTasks: async () => [],
    getResearchTask: unsupported('opens no research task'),
    createResearchTask: unsupported('creates no research task'),
    editResearchSpec: unsupported('edits no draft'),
    confirmResearchTask: unsupported('confirms no task'),
    retryResearchTask: unsupported('retries no task'),
    cancelResearchTask: unsupported('cancels no task'),
    interpretResearchTask: unsupported('requests no conclusion'),
    pluginCatalog: async () => CATALOG,
    resolveRecipe: unsupported('resolves no recipe'),
    listDeclarations: async () => ({
      schema: 'ea.plugin-declaration-catalog.v1',
      supported_kinds: [],
      validator_constraints: [],
      declaration_root_configured: false,
      declarations: [],
    }),
    validateDeclaration: unsupported('validates no declaration'),
    registerDeclaration: unsupported('registers no declaration'),
    settingsStatus: async () => STATUS,
    localStrategySlot: async () => SLOT,
    inspectLocalStrategy: unsupported('inspects no package'),
    registerLocalStrategy: unsupported('trusts no package'),
    revokeLocalStrategy: unsupported('revokes no trust'),
    // The answer is the caller's choice, never a reading of the request: neither
    // member looks at what it was sent. An empty payload is the one scripted
    // refusal they can tell apart, and that is the host's own `empty_source`.
    previewUpload: async () => answer,
    previewUploadFile: async (contentBase64: string) =>
      contentBase64 ? answer : REFUSALS.empty_source,
    publishUpload: async (request: UploadPublishRequest) => ({
      ...SYNTHETIC_VERSION,
      availability_policy: {
        ...SYNTHETIC_VERSION.availability_policy,
        id: request.availability_policy,
        latency_seconds: request.latency_seconds ?? null,
        evidence: `scripted preview: an uploader assertion, policy ${request.availability_policy}`,
      },
      datasets: DATASETS,
    }),
    validateScenario: unsupported('validates no scenario'),
    createBacktest: unsupported('submits no backtest'),
    createBatch: unsupported('submits no batch'),
    getBatch: unsupported('holds no batch'),
    getBacktest: unsupported('holds no backtest'),
    getReport: unsupported('holds no report'),
    getEquityPath: unsupported('holds no equity path'),
    createHoldout: unsupported('creates no holdout'),
    getHoldout: unsupported('holds no holdout'),
    artifactUrl: (jobId: string, name: string) => `/scripted-preview/${jobId}/${name}`,
  }
}

/** One situation this preview page can be put in, by hand or on camera. */
export type FirstUseState = {
  id: string
  title: string
  note: string
  csv: string
  preview: MappedPreview
}

export const FIRST_USE_STATES: readonly FirstUseState[] = [
  {
    id: 'guided',
    title: 'Guided mapping · the source is accepted',
    note: 'Choose each destination from the file’s own header, state the zone and the exact forms, preview, publish.',
    csv: SYNTHETIC_CSV,
    preview: MAPPED_PREVIEW_OK,
  },
  {
    id: 'renamed-header',
    title: 'Guided mapping after a vendor renamed a column',
    note: 'A destination chosen for the old name stays as it was chosen and is marked as not in this file; the host refuses it.',
    csv: SYNTHETIC_CSV_ALTERNATE,
    preview: REFUSALS.mapping_column_missing,
  },
  {
    id: 'duplicate-header',
    title: 'Header that names a column twice',
    note: 'No column is offered, because a repeated name would not say which column is meant.',
    csv: SYNTHETIC_CSV_DUPLICATE_HEADER,
    preview: REFUSALS.invalid_header,
  },
  {
    id: 'empty-header-name',
    title: 'Header with an unnamed column',
    note: 'No column is offered, and no name is invented for the unnamed one.',
    csv: SYNTHETIC_CSV_EMPTY_HEADER_NAME,
    preview: REFUSALS.invalid_header,
  },
  {
    id: 'quote-in-header-name',
    title: 'Header with a quote inside a name',
    note: 'A quote inside a name is not a name this reader can end, so no column is offered from it: the header is refused rather than read as `close`.',
    csv: SYNTHETIC_CSV_QUOTE_IN_NAME,
    preview: REFUSALS.invalid_header,
  },
  {
    id: 'text-after-quoted-name',
    title: 'Header with text after a closing quote',
    note: 'A quoted name is the whole name, so `"close"x` is refused instead of being read as some name the file does not have.',
    csv: SYNTHETIC_CSV_AFTER_QUOTE,
    preview: REFUSALS.invalid_header,
  },
  {
    id: 'byte-order-mark',
    title: 'Source that starts with a byte-order mark',
    note: 'The mark never becomes part of the first column name, and the bytes sent are still the file’s own, so the host refuses the source.',
    csv: SYNTHETIC_CSV_BYTE_ORDER_MARK,
    preview: REFUSALS.invalid_encoding,
  },
  {
    id: 'incomplete-declaration',
    title: 'Declaration that leaves destinations undeclared',
    note: 'The panel sends exactly what the controls describe; the host is what refuses an incomplete declaration.',
    csv: SYNTHETIC_CSV,
    preview: REFUSALS.mapping_declaration,
  },
  {
    id: 'policy-refused',
    title: 'Availability the host will not accept',
    note: 'A policy the host refuses is shown as its refusal, and the preview never authorizes a publication.',
    csv: SYNTHETIC_CSV,
    preview: REFUSALS.upload_policy,
  },
]

export function firstUseStateFor(id: string | null): FirstUseState {
  return FIRST_USE_STATES.find(state => state.id === id) ?? FIRST_USE_STATES[0]
}
