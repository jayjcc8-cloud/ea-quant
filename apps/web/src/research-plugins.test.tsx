/**
 * Capability selection, controlled upload and declaration registration.
 *
 * Every catalog entry, preview, published version and declaration verdict below
 * is hand-written fixture data shaped exactly like the host documents. These
 * tests only prove that the workspace shows what the host verified, that a
 * selection is carried on the request the user submits, and that nothing is
 * published, registered or installed without one explicit click.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter } from './app'
import {
  AVAILABILITY_SIMULATED,
  type DeclarationCatalog,
  type DeclarationVerdict,
  type PluginCatalog,
  type PluginCatalogEntry,
  type PluginSelection,
  type RecipeResolution,
  type ResearchSpec,
  type ResearchTask,
  type UploadPreview,
  type UploadVersion,
} from './research-api'

const entry = (plugin_id: string, kind: string, usable = true): PluginCatalogEntry => ({
  plugin_id,
  kind,
  package_version: '1.0.0',
  artifact_digest: 'a'.repeat(64),
  entrypoint: `ea.plugin.builtins:${kind}`,
  effects: { declared: ['local-compute'], granted: ['local-compute'], host_granted: ['local-compute'] },
  identity: { wraps: `host ${kind} primitive` },
  usable,
  reason: usable ? null : { code: 'plugin_unavailable', message: 'no host adapter is granted for this kind' },
})

const catalog: PluginCatalog = {
  schema: 'ea.plugin-catalog.v1',
  supported_kinds: ['model', 'data', 'strategy', 'validator', 'backtest-method'],
  plugins: [
    entry('ea.model.scripted.v1', 'model'),
    entry('ea.data.registered-root.v1', 'data'),
    entry('ea.strategy.bounded-long.v1', 'strategy'),
    entry('ea.validator.chronological-holdout.v1', 'validator'),
    entry('ea.backtest.installed-engine.v1', 'backtest-method'),
    entry('ea.model.legacy.v0', 'model', false),
  ],
}

const MODEL = { plugin_id: 'ea.model.scripted.v1', package_version: '1.0.0' }

const resolution: RecipeResolution = {
  schema: 'ea.plugin-recipe.v1',
  usable: true,
  code: null,
  message: null,
  selection: { model: MODEL },
  binding: {
    schema: 'ea.plugin-binding.v1',
    binding_sha256: 'b'.repeat(64),
    components: [
      { layer: 'model', kind: 'model', artifact_digest: 'a'.repeat(64), entrypoint: 'ea.plugin.builtins:model', effects: ['local-compute'], identity: {}, ...MODEL },
      { layer: 'data', plugin_id: 'ea.data.registered-root.v1', kind: 'data', package_version: '1.0.0', artifact_digest: 'a'.repeat(64), entrypoint: 'ea.plugin.builtins:data', effects: ['local-compute'], identity: {} },
    ],
  },
}

const quality = {
  bar_count: 3,
  revision_count: 0,
  bar_durations_seconds: ['60'],
  observed_gap_count: 0,
  largest_gap_seconds: null,
  bar_start_utc: '2026-01-02T00:00:00Z',
  bar_end_utc: '2026-01-02T00:02:00Z',
}

const preview: UploadPreview = {
  schema: 'ea.local-upload-preview.v1',
  profile: 'ea-phase1-ohlcv-csv-v1',
  byte_count: 42,
  expected_header: ['venue', 'symbol', 'interval_start_utc'],
  header: ['venue', 'symbol'],
  header_ok: false,
  rows: [],
  row_count_sampled: 0,
  truncated: false,
  full_validation: 'not_attempted',
  valid: false,
  quality: null,
  first_error: {
    code: 'header_mismatch',
    message: 'the header does not match the strict Phase 1 profile',
    record_number: null,
    field_name: null,
  },
  limitations: ['A preview reads a bounded sample only.'],
}

const published: UploadVersion = {
  schema: 'ea.local-dataset-version.v1',
  dataset_id: 'upload-1',
  kind: 'ohlcv',
  profile: 'ea-phase1-ohlcv-csv-v1',
  version: 1,
  published_at: '2026-10-04T09:00:00.000000Z',
  source: { declared_by: 'user', asserted: true, reference: 'internal export', file_name: 'bars.csv', license_reference: null },
  availability_policy: {
    id: AVAILABILITY_SIMULATED,
    version: 1,
    latency_seconds: 2,
    pit_verified: false,
    evidence: 'host-simulated: interval_end + 2s',
  },
  source_bytes_sha256: 'c'.repeat(64),
  source_byte_count: 42,
  raw_retained: true,
  capture_sha256: 'd'.repeat(64),
  capture_byte_count: 40,
  data_sha256: 'e'.repeat(64),
  record_count: 3,
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  quality,
  limitations: ['Availability is simulated, not observed.'],
  manifest_sha256: 'f'.repeat(64),
}

const declarations: DeclarationCatalog = {
  schema: 'ea.plugin-declaration-catalog.v1',
  supported_kinds: ['strategy', 'validator'],
  validator_constraints: ['min_record_count', 'max_observed_gap_count', 'require_declared_availability'],
  declaration_root_configured: true,
  declarations: [],
}

const body = {
  declaration_id: 'strict-gap-check.v1',
  kind: 'validator',
  package_version: '1.0.0',
  title: 'Strict gap check',
  declared_by: 'user',
  rule: null,
  rule_history: null,
  constraints: { min_record_count: 3 },
}

const verified: DeclarationVerdict = {
  usable: true,
  code: null,
  message: null,
  declaration: { ...body, schema: 'ea.plugin-declaration.v1', artifact_digest: '1'.repeat(64) },
  capability: { ...entry('ea.declaration.strict-gap-check.v1', 'validator'), identity: { wraps: 'host validator primitive', validator_constraints: { min_record_count: 3 } } },
  registered: false,
}

const spec: ResearchSpec = {
  strategy_id: 'bounded-long-v1',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'One registered entry with a two-unit target.',
  scenario_id: null,
  dataset_id: null,
  source_sha256: null,
  parameters: { target_quantity: '2' },
  assumptions: ['The registered scenario window is representative.'],
}

function task(overrides: Partial<ResearchTask> = {}): ResearchTask {
  return {
    schema: 'radian.research-task.v1',
    task_id: 'task-1',
    created_at: '2026-10-04T09:00:00.000000Z',
    updated_at: '2026-10-04T09:00:00.000000Z',
    status: 'awaiting_confirmation',
    raw_idea: 'Test a two-unit entry on AAPL',
    gap: null,
    budget: { max_model_calls: 10, max_backtests: 5 },
    usage: { model_calls: 1, input_tokens: 812, output_tokens: 143, backtests: 0 },
    spec_draft: null,
    spec_confirmed: [],
    jobs: [],
    request_id: null,
    error: null,
    cancel_note: null,
    last_model: null,
    reservation: null,
    backtest_reservation: null,
    spent_calls: [],
    operation_generation: 1,
    operation_completion_generation: 1,
    operation_kind: null,
    runtime_diagnostic: null,
    ...overrides,
  }
}

function adapter(overrides: Partial<ApiAdapter> = {}) {
  const calls = {
    create: 0, publish: 0, validate: 0, register: 0, resolve: 0, edit: 0,
    selection: undefined as PluginSelection | undefined,
    publishRequest: undefined as Record<string, unknown> | undefined,
    editFields: undefined as Record<string, unknown> | undefined,
  }
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    listResearchTasks: async () => [],
    getResearchTask: async () => task(),
    pluginCatalog: async () => catalog,
    resolveRecipe: async (selection: PluginSelection) => { calls.resolve += 1; calls.selection = selection; return resolution },
    listDeclarations: async () => declarations,
    validateDeclaration: async () => { calls.validate += 1; return verified },
    registerDeclaration: async () => { calls.register += 1; return { ...verified, registered: true } },
    previewUpload: async () => preview,
    publishUpload: async (request) => { calls.publish += 1; calls.publishRequest = request as unknown as Record<string, unknown>; return { ...published, datasets: [] } },
    createResearchTask: async (_idea: string, selection?: PluginSelection) => { calls.create += 1; calls.selection = selection; return task() },
    editResearchSpec: async (_taskId: string, fields: Record<string, unknown>) => { calls.edit += 1; calls.editFields = fields; return task() },
    // The remaining required adapter members belong to the backtest, batch and
    // holdout pages, which no test here renders. A list answers with an empty
    // result; every other one throws, so a test that unexpectedly reaches one
    // fails loudly rather than reading a made-up document.
    listScenarios: async () => [],
    listBacktests: async () => [],
    listHoldouts: async () => [],
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    createBacktest: async () => { throw new Error('this fixture submits no backtest') },
    createBatch: async () => { throw new Error('this fixture submits no batch') },
    getBatch: async () => { throw new Error('this fixture holds no batch') },
    getBacktest: async () => { throw new Error('this fixture holds no backtest') },
    getReport: async () => { throw new Error('this fixture holds no report') },
    getEquityPath: async () => { throw new Error('this fixture holds no equity path') },
    validateScenario: async () => { throw new Error('this fixture validates no scenario') },
    createHoldout: async () => { throw new Error('this fixture creates no holdout') },
    getHoldout: async () => { throw new Error('this fixture holds no holdout') },
    ...overrides,
  }
  return { api, calls }
}

function workspace(api: ApiAdapter) {
  window.history.pushState({}, '', '/research/new')
  render(<App api={api} />)
}

function detail(api: ApiAdapter) {
  window.history.pushState({}, '', '/research/task-1')
  render(<App api={api} />)
}

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('capability selection', () => {
  it('verifies one exact version per layer and carries the selection onto the created task', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter({ listResearchTasks: async () => [task()] })
    workspace(api)

    expect(await screen.findByRole('heading', { name: 'New research idea' })).toBeTruthy()
    // The selector lives inside a collapsed disclosure, so these controls are
    // driven directly: the assertions below are about what the host offers, not
    // about how a test DOM hides a closed disclosure. The host's catalog is read
    // asynchronously, and until it answers there is no select and no option at
    // all, so this waits for the control the host's answer creates.
    const model = await screen.findByLabelText('Model capability') as HTMLSelectElement
    expect(model.value).toBe('')
    const offered = Array.from(model.options, option => option.value)
    expect(offered).toContain('ea.model.scripted.v1@1.0.0')
    // A capability the host refuses is never offered as a selectable option.
    expect(offered).not.toContain('ea.model.legacy.v0@1.0.0')

    fireEvent.change(model, { target: { value: 'ea.model.scripted.v1@1.0.0' } })
    // If the host had not offered this version, the change would have selected
    // nothing; asserting the control took the value keeps that failure local
    // instead of letting it surface as an empty selection below.
    expect(model.value).toBe('ea.model.scripted.v1@1.0.0')
    fireEvent.click(screen.getByText('Verify selection'))

    expect(await screen.findByText('Selection verified by the host')).toBeTruthy()
    expect(calls.resolve).toBe(1)
    expect(calls.selection).toEqual({ model: MODEL })
    expect(calls.create).toBe(0)

    fireEvent.change(screen.getByLabelText('Research idea'), { target: { value: 'Test a two-unit entry' } })
    await user.click(screen.getByRole('button', { name: 'Create draft' }))

    await waitFor(() => expect(calls.create).toBe(1))
    expect(calls.selection).toEqual({ model: MODEL })
  })

  it('applies a user selection to the unconfirmed draft through the host', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter({ getResearchTask: async () => task({ spec_draft: spec }) })
    detail(api)

    expect(await screen.findByRole('heading', { name: 'Draft specification' })).toBeTruthy()
    // The draft's own selector reads the host catalog on mount, so the option has
    // to be offered before it can be chosen: an empty selection here would mean
    // the host offered no such version, not that the user picked nothing.
    const validator = await screen.findByLabelText('Validator capability') as HTMLSelectElement
    await waitFor(() => expect(Array.from(validator.options, option => option.value))
      .toContain('ea.validator.chronological-holdout.v1@1.0.0'))
    fireEvent.change(validator, { target: { value: 'ea.validator.chronological-holdout.v1@1.0.0' } })
    expect(validator.value).toBe('ea.validator.chronological-holdout.v1@1.0.0')
    await user.click(screen.getByRole('button', { name: 'Apply selection to draft' }))

    await waitFor(() => expect(calls.edit).toBe(1))
    expect(calls.editFields).toEqual({
      selection: { validator: { plugin_id: 'ea.validator.chronological-holdout.v1', package_version: '1.0.0' } },
    })
    expect(await screen.findByText(/The persisted draft now names this selection/)).toBeTruthy()
  })

  it('sends no selection at all when the user keeps the host default route', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter({ createResearchTask: async (_idea, selection) => { calls.create += 1; calls.selection = selection; return task() } })
    workspace(api)

    fireEvent.change(await screen.findByLabelText('Research idea'), { target: { value: 'Keep the default route' } })
    await user.click(screen.getByRole('button', { name: 'Create draft' }))

    await waitFor(() => expect(calls.create).toBe(1))
    expect(calls.selection).toBeUndefined()
  })
})

describe('controlled upload', () => {
  it('previews a bounded sample and publishes only after an explicit availability policy', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter()
    workspace(api)

    const content = await screen.findByLabelText('CSV 内容')
    fireEvent.change(content, { target: { value: 'venue,symbol\nXNAS,AAPL' } })
    await user.click(screen.getByRole('button', { name: '检查数据' }))

    expect(await screen.findByText(/header_mismatch: the header does not match the strict Phase 1 profile/)).toBeTruthy()
    expect(calls.publish).toBe(0)

    const publish = screen.getByRole('button', { name: '发布数据版本' })
    // No policy is assumed: the host refuses an upload that does not state one.
    expect((publish as HTMLButtonElement).disabled).toBe(true)

    await user.selectOptions(screen.getByLabelText('可用时间策略'), AVAILABILITY_SIMULATED)
    fireEvent.change(screen.getByLabelText('模拟延迟（秒）'), { target: { value: '2' } })
    await user.click(screen.getByRole('button', { name: '发布数据版本' }))

    await waitFor(() => expect(calls.publish).toBe(1))
    expect(calls.publishRequest).toMatchObject({ availability_policy: AVAILABILITY_SIMULATED, latency_seconds: 2 })
    expect(await screen.findByText(/point-in-time verified: false/)).toBeTruthy()
    expect(screen.getByText(/retained as provenance only/)).toBeTruthy()
    expect(screen.getByText('Availability is simulated, not observed.')).toBeTruthy()
  })
})

describe('declarations', () => {
  it('verifies a pasted declaration and registers it only on the explicit second click', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter()
    workspace(api)

    const textarea = await screen.findByLabelText('Declaration body (JSON)')
    fireEvent.change(textarea, { target: { value: JSON.stringify(body) } })

    expect(screen.queryByRole('button', { name: 'Register this declaration' })).toBeNull()
    expect(calls.register).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Verify declaration' }))

    expect(await screen.findByText('Verified by the host · not installed')).toBeTruthy()
    expect(calls.validate).toBe(1)
    expect(calls.register).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Register this declaration' }))

    await waitFor(() => expect(calls.register).toBe(1))
    expect(await screen.findByText('Registered by the host')).toBeTruthy()
  })

  it('shows a model-drafted declaration as verified evidence and never installs it', async () => {
    const { api, calls } = adapter({ getResearchTask: async () => task({ declaration_draft: verified }) })
    detail(api)

    expect(await screen.findByRole('heading', { name: 'Declaration proposal' })).toBeTruthy()
    expect(screen.getByText('Verified by the host · not installed')).toBeTruthy()
    expect(screen.getByText(/nothing was installed, selected, replaced or run/)).toBeTruthy()
    expect(screen.getByLabelText('Declaration body (JSON)')).toHaveProperty('value', JSON.stringify(verified.declaration, null, 2))
    expect(calls.register).toBe(0)
    // The model's draft is offered for registration, never performed.
    expect(screen.getByRole('button', { name: 'Register this declaration' })).toBeTruthy()
  })

  it('shows a declaration the host refused with its own code instead of an installable draft', async () => {
    const { api } = adapter({
      getResearchTask: async () => task({
        declaration_draft: { usable: false, code: 'declaration_rule_unknown', message: 'the declared rule is not a host primitive', declaration: null, capability: null, registered: false },
      }),
    })
    detail(api)

    expect(await screen.findByText(/declaration_rule_unknown: the declared rule is not a host primitive/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Register this declaration' })).toBeNull()
  })
})
