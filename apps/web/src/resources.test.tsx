/**
 * QUINE 资源中心 (resources) component tests.
 *
 * Four things this page must not blur, each pinned below by its own test:
 * a host-admissible capability is not a configured model, an empty dataset
 * directory is not a scenario without data, a failed read is not an empty read,
 * and a composition verdict is not a connection, a request validation or a
 * frozen run binding.
 *
 * Every fixture is a document shape the installed engine really serves; the
 * adapter stub records what the page asked the host for, so "the page calls
 * nothing by itself" is checked by counting calls rather than by inspection.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import type { ApiAdapter, BacktestJob, ScenarioSummary } from './app'
import type {
  ModelConfigurationStatus,
  PluginBinding,
  PluginCatalog,
  PluginSelection,
  RecipeResolution,
  ResearchTask,
} from './research-api'
import type { UserStrategy } from './user-strategies-api'
import { ResourcesPage, deriveKnownReferences } from './resources'

const hex = (character: string) => character.repeat(64)
const SOURCE_SHA = hex('a')
const DIGEST_MODEL = `sha256:${hex('3')}`
const DIGEST_DATA = `sha256:${hex('7')}`
const DIGEST_STRATEGY = `sha256:${hex('9')}`
const DIGEST_VALIDATOR = `sha256:${hex('5')}`
const BINDING_SHA = `sha256:${hex('1')}`

const MODEL_PLUGIN = 'ea.protocol.messages.v1'
const STRATEGY_PLUGIN = 'ea.local.strategy.ma'
const REJECTED_PLUGIN = 'ea.plugin.remote.validator'
const MODEL_OPTION = `${MODEL_PLUGIN}@1.0.0`

/** One model configuration an operator wrote: present, but with no credential
 * and a protocol that does not declare every capability research requires. */
const modelStatus: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1',
  settings_file: '/Users/local/.ea/settings.json',
  configured: false,
  provider: 'custom-messages-endpoint',
  model: 'local-model-name',
  base_url: null,
  api_key_env: null,
  configuration_present: true,
  credential_available: false,
  request_validation: 'not_verified',
  endpoint: null,
  protocol: 'messages',
  capabilities: { tool_use: true, structured_output: false, usage: false },
  required_capabilities: ['tool_use', 'structured_output'],
  missing_capabilities: ['structured_output'],
  configuration_error: 'the configured protocol does not declare structured_output',
}

const catalog: PluginCatalog = {
  schema: 'ea.plugin-catalog.v1',
  supported_kinds: ['model', 'data', 'strategy', 'validator', 'backtest-method'],
  plugins: [
    {
      plugin_id: MODEL_PLUGIN,
      kind: 'model',
      package_version: '1.0.0',
      artifact_digest: DIGEST_MODEL,
      entrypoint: 'ea.host.adapter.protocol-model',
      effects: { declared: [], granted: [], host_granted: [] },
      identity: { wraps: 'one host model transport', protocol: 'messages' },
      usable: true,
      reason: null,
    },
    {
      plugin_id: STRATEGY_PLUGIN,
      kind: 'strategy',
      package_version: '1.0.0',
      artifact_digest: DIGEST_STRATEGY,
      entrypoint: 'ea.host.adapter.local-strategy',
      effects: { declared: ['local_compute'], granted: ['local_compute'], host_granted: ['local_compute'] },
      identity: { wraps: 'moving-average-entry', action_contract: 'V2' },
      usable: true,
      reason: null,
    },
    {
      plugin_id: REJECTED_PLUGIN,
      kind: 'validator',
      package_version: '0.9.0',
      artifact_digest: DIGEST_VALIDATOR,
      entrypoint: 'ea.host.adapter.validator',
      effects: { declared: ['remote_transport'], granted: [], host_granted: [] },
      identity: { wraps: 'a remote validator' },
      usable: false,
      reason: { code: 'declaration_missing', message: 'no installed declaration grants remote_transport' },
    },
  ],
}

/** The host's own projection of a dataset entry; the page reads the version
 * evidence fields, so the adapter's return type has to carry them. */
type DatasetProjection = Awaited<ReturnType<NonNullable<ApiAdapter['listDatasets']>>>[number]

const uploadedDataset: DatasetProjection = {
  dataset_id: 'ds-upload-1',
  valid: true,
  origin: 'upload',
  source_sha256: SOURCE_SHA,
  capture_sha256: 'b1'.repeat(32),
  data_sha256: 'c1'.repeat(32),
  record_count: 12,
  venue: 'XNAS',
  symbol: 'AAPL',
  replay_start_utc: '2026-01-02T14:30:00.000000Z',
  replay_end_utc: '2026-01-02T14:35:00.000000Z',
  version_evidence_digest: `sha256:${'d1'.repeat(32)}`,
  availability_policy: {
    id: 'ea.availability.provided-in-capture.v1',
    version: 1,
    latency_seconds: null,
    pit_verified: false,
    evidence: 'the uploader asserted this capture was available when recorded',
  },
}

const registeredDataset: DatasetProjection = {
  dataset_id: 'ds-registered',
  valid: true,
  origin: 'registered',
  source_sha256: SOURCE_SHA,
  record_count: 4,
  venue: 'XNAS',
  symbol: 'AAPL',
}

const scenarios: ScenarioSummary[] = [
  {
    scenario_id: 'ma-cross.yaml',
    name: 'ma-cross',
    valid: true,
    input_identity: { dataset_id: 'ds-registered', source_sha256: SOURCE_SHA, scenario_sha256: hex('b'), data_sha256: hex('c'), record_count: 4 },
    summary: { strategy_id: 'ma-cross-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', record_count: 4 },
  },
]

const frozenBinding: PluginBinding = {
  schema: 'ea.plugin-binding.v1',
  binding_sha256: BINDING_SHA,
  components: [
    {
      layer: 'data',
      plugin_id: 'ea.dataset.uploaded.version',
      kind: 'data',
      package_version: '2.0.0',
      artifact_digest: DIGEST_DATA,
      entrypoint: 'ea.host.adapter.uploaded-version',
      effects: [],
      identity: { wraps: 'one published dataset version' },
    },
    {
      layer: 'strategy',
      plugin_id: STRATEGY_PLUGIN,
      kind: 'strategy',
      package_version: '1.0.0',
      artifact_digest: DIGEST_STRATEGY,
      entrypoint: 'ea.host.adapter.local-strategy',
      effects: ['local_compute'],
      identity: { wraps: 'moving-average-entry' },
    },
  ],
}

const uploadedVersion = {
  schema: 'ea.local-upload-version.v1',
  dataset_id: 'ds-upload-1',
  kind: 'ohlcv',
  version: 2,
  capture_sha256: 'b1'.repeat(32),
  data_sha256: 'c1'.repeat(32),
  record_count: 12,
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  availability_policy: uploadedDataset.availability_policy,
}

/** One frozen run whose exact capability identity and input version the host
 * recorded alongside the accepted input. */
const frozenJob: BacktestJob = {
  schema: 'ea.local-web-job.v3',
  job_id: 'job-frozen',
  request_id: 'request-frozen',
  scenario_id: 'ma-cross.yaml',
  input_identity: { dataset_id: 'ds-upload-1', source_sha256: SOURCE_SHA, scenario_sha256: hex('b'), data_sha256: hex('c'), record_count: 12 },
  status: 'succeeded',
  engine_run_id: 'run-frozen',
  report_sha256: hex('d'),
  summary_sha256: hex('e'),
  error_code: null,
  message: null,
  report_ready: true,
  equity_path_sha256: hex('f'),
  created_at: '2026-09-25T03:05:00.000000Z',
  input_sha256: hex('0'),
  attempt_id: 'run-frozen',
  input_snapshot: {
    schema: 'ea.local-web-input.v2',
    research_input: uploadedDataset,
    scenario_id: 'ma-cross.yaml',
    source_identity: { dataset_id: 'ds-upload-1', source_sha256: SOURCE_SHA, scenario_sha256: hex('b'), data_sha256: hex('c'), record_count: 12 },
    identity: { dataset_id: 'ds-upload-1', source_sha256: SOURCE_SHA, scenario_sha256: hex('b'), data_sha256: hex('c'), record_count: 12 },
    scenario: {
      funding: { currency: 'USD', initial_cash: '10000' },
      strategy: { id: 'ma-cross-v1', version: 2, parameters: { window: 5 } },
      instrument: { venue: 'XNAS', symbol: 'AAPL' },
    },
  },
  plugin_binding: frozenBinding,
  dataset_version: uploadedVersion,
}

/** A run written before capability binding and input snapshots existed: its
 * scenario id is known and nothing else about its identity is. */
const legacyJob: BacktestJob = {
  schema: 'ea.local-web-job.v1',
  job_id: 'job-legacy',
  request_id: 'request-legacy',
  scenario_id: 'legacy.yaml',
  input_identity: { scenario_sha256: hex('1'), data_sha256: hex('2'), record_count: 4 },
  status: 'succeeded',
  engine_run_id: 'run-legacy',
  report_sha256: hex('3'),
  summary_sha256: hex('4'),
  error_code: null,
  message: null,
  report_ready: true,
}

const userStrategy: UserStrategy = {
  schema: 'ea.user-strategy.v1',
  user_strategy_id: 'us-1',
  created_at: '2026-09-25T01:00:00.000000Z',
  updated_at: '2026-09-25T03:05:00.000000Z',
  name: 'Trend follow',
  intent: 'Does the moving average cross pay after fees?',
  draft: {
    revision: 2,
    strategy_id: 'ma-cross-v1',
    venue: 'XNAS',
    symbol: 'AAPL',
    scenario_id: 'ma-cross.yaml',
    dataset_id: 'ds-upload-1',
    source_sha256: SOURCE_SHA,
    initial_cash: '10000',
    parameters: { window: 5 },
    criterion: null,
  },
  history: [],
  messages: [],
  calls: [],
  proposals: [],
  changes: [],
  experiments: [
    {
      launch_intent_id: 'li-1',
      request_id: 'request-1',
      payload_sha256: hex('5'),
      payload: {
        draft_revision: 2,
        scenario_id: 'ma-cross.yaml',
        parameters: { window: 5 },
        preview_identity: `sha256:${hex('6')}`,
        input_sha256: hex('7'),
        input_identity: { dataset_id: 'ds-upload-1', source_sha256: SOURCE_SHA },
        approved_budget: { max_model_calls: 0, max_backtests: 1 },
      },
      task_id: 'task-1',
      job_id: 'job-frozen',
      status: 'created',
      created_at: '2026-09-25T03:00:00.000000Z',
    },
  ],
}

const researchTask: ResearchTask = {
  schema: 'radian.research-task.v1',
  task_id: 'task-1',
  created_at: '2026-09-25T02:50:00.000000Z',
  updated_at: '2026-09-25T03:10:00.000000Z',
  status: 'succeeded',
  raw_idea: 'Does the moving average cross pay after fees?',
  gap: null,
  budget: { max_model_calls: 2, max_backtests: 1 },
  usage: { model_calls: 1, input_tokens: 120, output_tokens: 40, backtests: 1 },
  spec_draft: null,
  spec_confirmed: [
    {
      version: 1,
      confirmed_at: '2026-09-25T03:00:00.000000Z',
      spec: {
        strategy_id: 'ma-cross-v1',
        instrument: { venue: 'XNAS', symbol: 'AAPL' },
        initial_cash: '10000',
        rationale: 'frozen before the run',
        scenario_id: 'ma-cross.yaml',
        dataset_id: 'ds-upload-1',
        source_sha256: SOURCE_SHA,
        parameters: { window: 5 },
        assumptions: [],
      },
    },
  ],
  jobs: ['job-frozen'],
  request_id: 'request-1',
  error: null,
  cancel_note: null,
  last_model: null,
  reservation: null,
  backtest_reservation: null,
  spent_calls: [1],
  operation_generation: 1,
  operation_completion_generation: 1,
  operation_kind: null,
  runtime_diagnostic: null,
  pinned_binding: frozenBinding,
}

const usableResolution: RecipeResolution = {
  schema: 'ea.plugin-recipe.v1',
  usable: true,
  code: null,
  message: null,
  selection: { model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } },
  binding: frozenBinding,
}

const refusedResolution: RecipeResolution = {
  schema: 'ea.plugin-recipe.v1',
  usable: false,
  code: 'plugin_not_usable',
  message: 'the selected capability is not usable in this catalog',
  selection: { model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } },
  binding: null,
}

type Script = {
  catalog: () => Promise<PluginCatalog>
  settings: () => Promise<ModelConfigurationStatus>
  datasets: () => Promise<DatasetProjection[]>
  scenarios: () => Promise<ScenarioSummary[]>
  strategies: () => Promise<UserStrategy[]>
  tasks: () => Promise<ResearchTask[]>
  jobs: () => Promise<BacktestJob[]>
  resolve: (selection: PluginSelection) => Promise<RecipeResolution>
}

type Calls = {
  catalog: number
  settings: number
  datasets: number
  scenarios: number
  strategies: number
  tasks: number
  jobs: number
  resolve: PluginSelection[]
}

function stubApi(script: Partial<Script> = {}): { api: ApiAdapter; calls: Calls } {
  const calls: Calls = { catalog: 0, settings: 0, datasets: 0, scenarios: 0, strategies: 0, tasks: 0, jobs: 0, resolve: [] }
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    validateScenario: async (scenarioId) => scenarios.find((item) => item.scenario_id === scenarioId)!,
    createBacktest: async () => frozenJob,
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: () => { calls.jobs += 1; return script.jobs ? script.jobs() : Promise.resolve([frozenJob, legacyJob]) },
    getBacktest: async () => frozenJob,
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    listScenarios: () => { calls.scenarios += 1; return script.scenarios ? script.scenarios() : Promise.resolve(scenarios) },
    listDatasets: () => { calls.datasets += 1; return script.datasets ? script.datasets() : Promise.resolve([uploadedDataset, registeredDataset]) },
    settingsStatus: () => { calls.settings += 1; return script.settings ? script.settings() : Promise.resolve(modelStatus) },
    pluginCatalog: () => { calls.catalog += 1; return script.catalog ? script.catalog() : Promise.resolve(catalog) },
    resolveRecipe: (selection) => { calls.resolve.push(selection); return script.resolve ? script.resolve(selection) : Promise.resolve(usableResolution) },
    listUserStrategies: () => { calls.strategies += 1; return script.strategies ? script.strategies() : Promise.resolve([userStrategy]) },
    listResearchTasks: () => { calls.tasks += 1; return script.tasks ? script.tasks() : Promise.resolve([researchTask]) },
  }
  return { api, calls }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

const fact = (name: string) => document.querySelector(`[data-resource-fact="${name}"]`)?.textContent ?? null
const entries = (kind: string) =>
  [...document.querySelectorAll(`[data-resource-entry="${kind}"]`)].map((node) => node.getAttribute('data-id'))
const references = () =>
  [...document.querySelectorAll('[data-resource="reference"]')].map((node) => ({
    key: node.getAttribute('data-key') ?? '',
    kind: node.getAttribute('data-kind') ?? '',
    node: node as HTMLElement,
  }))
const referenced = (root: HTMLElement, name: string) => root.querySelector(`[data-ref="${name}"]`)?.textContent ?? null

const show = (api: ApiAdapter) => render(<MemoryRouter><ResourcesPage api={api} /></MemoryRouter>)
const findRegion = (name: string) => screen.findByRole('region', { name })
/** The host's catalog is what offers a selectable version, so a test that selects
 * waits for the catalog's own answer instead of for the section to appear. */
const catalogReady = () => waitFor(() => expect(entries('model')).toEqual([MODEL_PLUGIN]))

afterEach(() => { cleanup() })

describe('QUINE 资源中心 directory', () => {
  it('keeps a host-admissible model wrapper explicitly unconfigured and unverified', async () => {
    const user = userEvent.setup()
    const { api, calls } = stubApi()
    show(api)

    const modelRegion = await findRegion('模型与协议封装')
    await waitFor(() => expect(fact('host-admissible')).toBe('是'))
    // Five separate facts: what this host can load, what an operator configured,
    // what credential it has, and the two things nothing here has verified.
    expect(fact('configured')).toBe('否')
    expect(fact('credential')).toBe('否')
    expect(fact('connection')).toBe('未验证')
    expect(fact('request-validation')).toBe('未验证')

    const entry = modelRegion.querySelector('[data-resource-entry="model"]') as HTMLElement
    expect(entry.textContent).toContain(MODEL_PLUGIN)
    expect(entry.textContent).toContain('1.0.0')
    expect(entry.textContent).toContain(DIGEST_MODEL)
    expect(entry.textContent).toContain('one host model transport')
    // The catalog's usable flag is host admissibility only.
    expect(entry.textContent).toContain('未配置')
    // There is no credential form and no connection control on this page.
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0)
    // No sentence anywhere states a connection or a verified request as a fact;
    // this page has observed neither.
    expect(document.body.textContent).not.toContain('已连接')
    expect(document.body.textContent).not.toContain('请求能力已验证')

    // Reading the page asked the host for nothing but reads, and a choice of a
    // model the host would refuse is still possible so it can refuse explicitly.
    expect(calls.resolve).toEqual([])
    await user.selectOptions(screen.getByLabelText('模型层'), MODEL_OPTION)
    expect(calls.resolve).toEqual([])

    await user.click(screen.getByRole('button', { name: '校验组合' }))
    expect(calls.resolve).toEqual([{ model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } }])
  })

  it('lists each catalog entry with its effects and the host reason a refused one is refused', async () => {
    const { api } = stubApi()
    show(api)
    await catalogReady()

    const entriesOf = (kind: string) => [...document.querySelectorAll(`[data-resource-entry="${kind}"]`)] as HTMLElement[]
    // One declaration is one directory entry whatever its kind; the kind it
    // declares is carried inside the entry, not in the entry's own attribute.
    const strategy = entriesOf('plugin').find((node) => node.getAttribute('data-id') === STRATEGY_PLUGIN)!
    expect(strategy.textContent).toContain('strategy')
    expect(strategy.textContent).toContain('local_compute')
    expect(strategy.textContent).toContain('moving-average-entry')

    // A refused entry carries the host's own code and sentence, not a generic
    // "unavailable": the reader can see which declaration is missing.
    const refused = entriesOf('plugin').find((node) => node.getAttribute('data-id') === REJECTED_PLUGIN)!
    expect(refused.textContent).toContain('declaration_missing')
    expect(refused.textContent).toContain('no installed declaration grants remote_transport')
    expect(refused.textContent).toContain('remote_transport')
  })

  it('names a refused composition by the host code and an accepted one as an authorization check only', async () => {
    const user = userEvent.setup()
    const first = stubApi({ resolve: async () => refusedResolution })
    show(first.api)
    await catalogReady()
    await user.selectOptions(screen.getByLabelText('策略层'), `${STRATEGY_PLUGIN}@1.0.0`)
    await user.click(screen.getByRole('button', { name: '校验组合' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('plugin_not_usable')
    expect(alert.textContent).toContain(refusedResolution.message!)
    expect(screen.queryByRole('region', { name: '组合校验结果' })).toBeNull()

    cleanup()

    const second = stubApi({ resolve: async () => usableResolution })
    show(second.api)
    await catalogReady()
    await user.selectOptions(screen.getByLabelText('模型层'), MODEL_OPTION)
    await user.click(screen.getByRole('button', { name: '校验组合' }))
    const verdict = await screen.findByRole('region', { name: '组合校验结果' })
    // The host verified a composition. It verified no connection, no request and
    // no frozen run binding.
    expect(verdict.textContent).toContain('组合授权检查')
    expect(verdict.textContent).toContain(BINDING_SHA)
    expect(verdict.textContent).toContain('dataset: ea.dataset.uploaded.version 2.0.0')
    expect(verdict.textContent).not.toContain('已连接')
    expect(verdict.textContent).not.toContain('请求能力已验证')
    expect(second.calls.resolve).toHaveLength(1)
  })

  it('asks the host only from the explicit button and clears the verdict on the next choice', async () => {
    const user = userEvent.setup()
    const { api, calls } = stubApi()
    show(api)
    await catalogReady()

    const model = screen.getByLabelText('模型层')
    await user.selectOptions(model, MODEL_OPTION)
    await user.click(screen.getByRole('button', { name: '校验组合' }))
    expect(await screen.findByRole('region', { name: '组合校验结果' })).toBeTruthy()
    expect(calls.resolve).toHaveLength(1)

    // A new choice retires the previous verdict immediately, and choosing never
    // asks the host on its own.
    await user.selectOptions(model, '')
    expect(screen.queryByRole('region', { name: '组合校验结果' })).toBeNull()
    expect(calls.resolve).toHaveLength(1)
  })

  it('clears a verdict on a catalog refresh and ignores a resolution that arrives late', async () => {
    const user = userEvent.setup()
    const pending = deferred<RecipeResolution>()
    const { api, calls } = stubApi({ resolve: () => pending.promise })
    show(api)
    await catalogReady()

    await user.selectOptions(screen.getByLabelText('模型层'), MODEL_OPTION)
    await user.click(screen.getByRole('button', { name: '校验组合' }))
    expect(calls.resolve).toHaveLength(1)

    // The catalog is re-read: it names versions this verdict was not produced
    // against, so the verdict is retired and the in-flight reply is dropped.
    await user.click(screen.getByRole('button', { name: '刷新能力目录' }))
    await waitFor(() => expect(calls.catalog).toBe(2))
    await act(async () => { pending.resolve(usableResolution); await pending.promise })

    expect(screen.queryByRole('region', { name: '组合校验结果' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(calls.resolve).toHaveLength(1)
  })

  it('filters the real model, data, scenario and plugin directory with one query', async () => {
    const user = userEvent.setup()
    // Nothing in the reference sources, so the query can only be read as filtering
    // the directory itself.
    const { api } = stubApi({
      strategies: async () => [],
      tasks: async () => [],
      jobs: async () => [],
    })
    show(api)
    await waitFor(() => expect(entries('scenario')).toEqual(['ma-cross.yaml']))
    await catalogReady()
    await waitFor(() => expect(entries('dataset')).toEqual(['ds-upload-1', 'ds-registered']))
    expect(entries('model')).toEqual([MODEL_PLUGIN])
    expect(entries('plugin')).toEqual([STRATEGY_PLUGIN, REJECTED_PLUGIN])

    const search = screen.getByLabelText('搜索资源')
    await user.type(search, 'ds-upload-1')
    expect(entries('dataset')).toEqual(['ds-upload-1'])
    expect(entries('scenario')).toEqual([])
    expect(entries('model')).toEqual([])
    expect(entries('plugin')).toEqual([])
    // A filtered-out list is not an empty directory, and it says which it is.
    const scenarioRegion = screen.getByRole('region', { name: '场景' })
    expect(within(scenarioRegion).getByText(/没有匹配当前搜索的资源/)).toBeTruthy()
    expect(within(scenarioRegion).queryByText('本工作区没有已注册场景。')).toBeNull()

    await user.clear(search)
    expect(entries('dataset')).toEqual(['ds-upload-1', 'ds-registered'])
    expect(entries('scenario')).toEqual(['ma-cross.yaml'])
  })

  it('separates an empty dataset directory from the registered scenarios that still hold data', async () => {
    const { api } = stubApi({ datasets: async () => [] })
    show(api)

    const datasetRegion = await findRegion('数据集')
    await waitFor(() => expect(datasetRegion.textContent).toContain('本工作区没有独立数据集'))
    // An empty directory of standalone datasets is stated as exactly that, and
    // never as scenarios without data.
    expect(datasetRegion.textContent).toContain('数据集为空不代表场景没有数据')
    expect(within(datasetRegion).getByRole('link', { name: '导入与声明' }).getAttribute('href')).toBe('/research')

    const scenarioRegion = await findRegion('场景')
    await waitFor(() => expect(entries('scenario')).toEqual(['ma-cross.yaml']))
    // Scenarios keep their own registered data; only the standalone dataset
    // directory is empty, and it is the only section that says so.
    expect(within(scenarioRegion).queryByText('本工作区没有已注册场景。')).toBeNull()
  })

  it('reports a failing source as an error rather than an empty catalog, and keeps the others readable', async () => {
    const { api } = stubApi({
      scenarios: async () => { throw new Error('scenario_catalog_unavailable') },
      jobs: async () => { throw new Error('job_index_unavailable') },
    })
    show(api)

    const scenarioRegion = await findRegion('场景')
    await waitFor(() => expect(within(scenarioRegion).getByRole('alert').textContent).toContain('scenario_catalog_unavailable'))
    expect(within(scenarioRegion).queryByText('本工作区没有已注册场景。')).toBeNull()
    expect(entries('scenario')).toEqual([])

    // The dataset directory answered, so it is still listed in full.
    await waitFor(() => expect(entries('dataset')).toEqual(['ds-upload-1', 'ds-registered']))

    // A source that failed is unknown, never zero: the run association is named
    // as unread rather than counted as unused.
    const referenceRegion = await findRegion('可观察到的已知引用')
    await waitFor(() => expect(referenced(referenceRegion, 'source-jobs')).toContain('不可用'))
    expect(referenced(referenceRegion, 'source-jobs')).toContain('job_index_unavailable')
    expect(referenced(referenceRegion, 'source-strategies')).toContain('已读取')
    expect(within(referenceRegion).queryByText('已读取的来源中没有已知引用。')).toBeNull()
  })

  it('states the unsupported account, database and multi-profile capabilities without offering them', async () => {
    const { api, calls } = stubApi()
    show(api)

    const region = await findRegion('当前未支持')
    await waitFor(() => expect(region.textContent).toContain('多账户'))
    expect(region.textContent).toContain('数据库')
    expect(region.textContent).toContain('多套执行配置')
    // No fake control and no fake persistence: nothing here is button-shaped.
    expect(within(region).queryAllByRole('button')).toHaveLength(0)
    expect(within(region).queryAllByRole('textbox')).toHaveLength(0)
    expect(within(region).queryAllByRole('checkbox')).toHaveLength(0)
    expect(calls.resolve).toEqual([])
  })

  it('links the AI context entry into the existing workspaces without a model call', async () => {
    const user = userEvent.setup()
    const { api, calls } = stubApi()
    show(api)

    const region = await findRegion('组合选择')
    await waitFor(() => expect(within(region).getByRole('link', { name: '策略工作区' })).toBeTruthy())
    expect(within(region).getByRole('link', { name: '策略工作区' }).getAttribute('href')).toBe('/strategies')
    expect(within(region).getByRole('link', { name: '研究工作区' }).getAttribute('href')).toBe('/research')
    // Following the entry is navigation; nothing on this page answers as a model.
    await user.click(within(region).getByRole('link', { name: '研究工作区' }))
    expect(calls.resolve).toEqual([])
    expect(calls.settings).toBe(1)
  })

  it('renders every observed reference with its exact identity and its unknown parts named', async () => {
    const { api } = stubApi()
    show(api)

    const region = await findRegion('可观察到的已知引用')
    await waitFor(() => expect(references().length).toBeGreaterThan(0))
    expect(region.textContent).toContain('不是完整的运行时依赖')

    const frozen = references().find((item) => item.key === 'job:job-frozen')!
    expect(frozen.kind).toBe('frozen')
    expect(frozen.node.textContent).toContain('历史冻结')
    expect(referenced(frozen.node, 'scenario')).toBe('ma-cross.yaml')
    expect(referenced(frozen.node, 'dataset')).toBe('ds-upload-1')
    expect(referenced(frozen.node, 'source_sha256')).toBe(SOURCE_SHA)
    expect(frozen.node.querySelectorAll('[data-ref="layer"]')).toHaveLength(2)
    expect(frozen.node.textContent).toContain(DIGEST_DATA)
    expect(frozen.node.textContent).toContain(DIGEST_STRATEGY)
    expect(frozen.node.querySelector('[data-ref="unknown"]')).toBeNull()
    expect(frozen.node.querySelector('a[href="/backtests/job-frozen"]')).toBeTruthy()

    const draft = references().find((item) => item.key === 'draft:us-1:2')!
    expect(draft.kind).toBe('draft')
    expect(draft.node.textContent).toContain('草稿选择')
    // A draft selection names no exact capability and is not a frozen digest.
    expect(draft.node.textContent).toContain('摘要未冻结')
    expect(referenced(draft.node, 'unknown')).toContain('插件标识')
    expect(draft.node.querySelector('a[href="/strategies/us-1"]')).toBeTruthy()

    const legacy = references().find((item) => item.key === 'job:job-legacy')!
    expect(referenced(legacy.node, 'scenario')).toBe('legacy.yaml')
    expect(referenced(legacy.node, 'dataset')).toBe('未知')
    expect(referenced(legacy.node, 'source_sha256')).toBe('未知')
    expect(referenced(legacy.node, 'unknown')).toContain('插件标识')
  })
  it('attributes one configured provider to exactly the adapters whose identity declares it', async () => {
    const user = userEvent.setup()
    const template = catalog.plugins[0]
    const adapters: PluginCatalog = {
      ...catalog,
      plugins: [
        { ...template, plugin_id: 'ea.protocol.deepseek.v1', identity: { wraps: 'the configured transport', provider: 'deepseek', protocol: 'messages' } },
        { ...template, plugin_id: 'ea.protocol.anthropic.v1', identity: { wraps: 'another transport', provider: 'anthropic', protocol: 'messages' } },
        { ...template, plugin_id: 'ea.protocol.openai.v1', identity: { wraps: 'another transport', provider: 'openai', protocol: 'messages' } },
        { ...template, plugin_id: 'ea.protocol.unstated.v1', identity: { wraps: 'a transport that names no provider' } },
      ],
    }
    const deepseek: ModelConfigurationStatus = { ...modelStatus, configured: true, provider: 'deepseek', protocol: 'messages', credential_available: true }
    const { api, calls } = stubApi({ catalog: async () => adapters, settings: async () => deepseek })
    show(api)
    await waitFor(() => expect(entries('model')).toHaveLength(4))
    const entryOf = (id: string) =>
      [...document.querySelectorAll('[data-resource-entry="model"]')].find((node) => node.getAttribute('data-id') === id) as HTMLElement

    // Only the adapter whose declared identity is the configured provider is the
    // configured one. A workspace configured for deepseek says nothing about the
    // anthropic or openai wrappers, and an entry that names no provider at all
    // cannot be attributed to any of them.
    expect(entryOf('ea.protocol.deepseek.v1').textContent).toContain('已配置')
    expect(entryOf('ea.protocol.anthropic.v1').textContent).toContain('未配置')
    expect(entryOf('ea.protocol.anthropic.v1').textContent).not.toContain('已配置')
    expect(entryOf('ea.protocol.openai.v1').textContent).not.toContain('已配置')
    expect(entryOf('ea.protocol.unstated.v1').textContent).toContain('配置未知')
    expect(fact('configured')).toBe('是')
    expect(fact('credential')).toBe('是')

    // The long evidence is one native disclosure down, closed until a reader asks
    // for it, and still in the document when they do not.
    const deepseekEntry = entryOf('ea.protocol.deepseek.v1')
    const details = deepseekEntry.querySelector('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')!.textContent).toContain('摘要、加载点、效果与身份')
    expect(deepseekEntry.textContent).toContain(DIGEST_MODEL)
    expect(deepseekEntry.textContent).toContain('provider: deepseek')

    // The configuration status is re-read when the reader asks, and asking for it
    // reaches no resolve endpoint and starts nothing.
    expect(calls.settings).toBe(1)
    await user.click(screen.getByRole('button', { name: '刷新配置状态' }))
    expect(calls.settings).toBe(2)
    expect(calls.resolve).toEqual([])

    // A workspace that reports no credential has not refused one: it is unknown.
    cleanup()
    const unstated = stubApi({ catalog: async () => adapters, settings: async () => ({ ...deepseek, credential_available: undefined }) })
    show(unstated.api)
    await waitFor(() => expect(fact('configured')).toBe('是'))
    expect(fact('credential')).toBe('未知')
  })

  it('shows a cleared draft as the host default route instead of the abandoned selection', async () => {
    const abandoned: PluginSelection = { model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } }
    const cleared: ResearchTask = {
      ...researchTask,
      task_id: 'task-cleared',
      jobs: [],
      spec_confirmed: [],
      pinned_binding: null,
      selection: abandoned,
      // The user cleared the selection, so the host persisted the spec without it.
      spec_draft: { ...researchTask.spec_confirmed[0].spec },
    }
    const { api } = stubApi({ tasks: async () => [cleared] })
    show(api)

    await waitFor(() => expect(references().length).toBeGreaterThan(0))
    const reference = references().find((item) => item.key === 'selection:task-cleared')!
    expect(reference.kind).toBe('draft')
    expect(reference.node.textContent).toContain('草稿选择')
    expect(reference.node.textContent).toContain('主机默认路径')
    expect(referenced(reference.node, 'default-route')).toContain('主机默认路径')
    expect(referenced(reference.node, 'scenario')).toBe('ma-cross.yaml')
    // Nothing chosen means nothing is missing, and the abandoned selection is not
    // put back anywhere in the reference.
    expect(referenced(reference.node, 'unknown')).toBeNull()
    expect(reference.node.querySelectorAll('[data-ref="layer"]')).toHaveLength(0)
    expect(reference.node.textContent).not.toContain(MODEL_PLUGIN)
    expect(reference.node.querySelector('a[href="/research/task-cleared"]')).toBeTruthy()
  })

  it('shows a pinned task without a run as a frozen binding, never as a draft', async () => {
    const pinned: ResearchTask = { ...researchTask, task_id: 'task-pinned', jobs: [], selection: null }
    const { api } = stubApi({ tasks: async () => [pinned] })
    show(api)

    await waitFor(() => expect(references().length).toBeGreaterThan(0))
    const reference = references().find((item) => item.key === 'task:task-pinned')!
    expect(reference.kind).toBe('frozen')
    expect(reference.node.textContent).toContain('历史冻结')
    expect(reference.node.textContent).toContain('没有对应的运行记录')
    expect(reference.node.textContent).not.toContain('草稿选择')
    // The binding is the immutable identity; no run is invented to carry it.
    expect(reference.node.querySelector('a[href="/backtests/job-frozen"]')).toBeNull()
    expect(reference.node.querySelector('a[href="/research/task-pinned"]')).toBeTruthy()
    expect(referenced(reference.node, 'scenario')).toBe('ma-cross.yaml')
    expect(reference.node.querySelectorAll('[data-ref="layer"]')).toHaveLength(2)
    expect(reference.node.textContent).toContain(DIGEST_DATA)
    expect(reference.node.textContent).toContain(DIGEST_STRATEGY)
    expect(referenced(reference.node, 'unknown')).toBeNull()
  })

  it('keeps a chosen version the refreshed catalog no longer offers, and sends exactly it', async () => {
    const user = userEvent.setup()
    const version = (package_version: string, artifact_digest: string) => ({ ...catalog.plugins[0], package_version, artifact_digest })
    const first: PluginCatalog = { ...catalog, plugins: [version('1.0.0', DIGEST_MODEL), ...catalog.plugins.slice(1)] }
    const second: PluginCatalog = { ...catalog, plugins: [version('2.0.0', `sha256:${hex('8')}`), ...catalog.plugins.slice(1)] }
    const { api, calls } = stubApi({ catalog: async () => (calls.catalog === 1 ? first : second) })
    show(api)
    await catalogReady()

    const select = screen.getByLabelText('模型层') as HTMLSelectElement
    const chosen = `${MODEL_PLUGIN}@1.0.0`
    await user.selectOptions(select, chosen)
    expect(select.value).toBe(chosen)

    await user.click(screen.getByRole('button', { name: '刷新能力目录' }))
    await waitFor(() => expect(calls.catalog).toBe(2))
    // The version is gone from the catalog and the verdict produced against the
    // earlier one is retired. The choice itself stays as the exact value that will
    // be sent, clearly marked, so the host can refuse that version explicitly
    // rather than the control showing one value and the request carrying another.
    await waitFor(() => expect(select.value).toBe(chosen))
    expect([...select.options].find((option) => option.value === chosen)?.textContent).toContain('当前目录中不存在')
    expect(screen.queryByRole('region', { name: '组合校验结果' })).toBeNull()

    await user.click(screen.getByRole('button', { name: '校验组合' }))
    expect(calls.resolve).toEqual([{ model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } }])
  })

  it('drops a resolution that arrives after the selection it was made for has changed', async () => {
    const user = userEvent.setup()
    const pending = deferred<RecipeResolution>()
    const { api, calls } = stubApi({ resolve: () => pending.promise })
    show(api)
    await catalogReady()

    const model = screen.getByLabelText('模型层') as HTMLSelectElement
    await user.selectOptions(model, MODEL_OPTION)
    await user.click(screen.getByRole('button', { name: '校验组合' }))
    expect(calls.resolve).toHaveLength(1)

    // The reader changes the composition while that answer is still in flight.
    // The answer belongs to a selection that is no longer on screen, so it may
    // not be displayed over the new one.
    await user.selectOptions(model, '')
    await act(async () => { pending.resolve(usableResolution); await pending.promise })

    expect(screen.queryByRole('region', { name: '组合校验结果' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(model.value).toBe('')
    // The retired attempt is no longer in flight, so the action is available again.
    expect((screen.getByRole('button', { name: '校验组合' }) as HTMLButtonElement).disabled).toBe(false)
    expect(calls.resolve).toHaveLength(1)
  })
})

describe('known reference derivation', () => {
  it('derives exact frozen identities, deduplicates one run, and keeps drafts distinct', () => {
    const derived = deriveKnownReferences({
      strategies: [userStrategy],
      tasks: [researchTask],
      jobs: [frozenJob, legacyJob],
    })

    // The strategy experiment, the confirmed task spec and the job index all name
    // one run: that is one reference with three associations, not three runs.
    const frozen = derived.filter((item) => item.job_id === 'job-frozen')
    expect(frozen).toHaveLength(1)
    expect(frozen[0].key).toBe('job:job-frozen')
    expect(frozen[0].kind).toBe('frozen')
    expect(frozen[0].scenario_id).toBe('ma-cross.yaml')
    expect(frozen[0].dataset_id).toBe('ds-upload-1')
    expect(frozen[0].source_sha256).toBe(SOURCE_SHA)
    expect(frozen[0].layers).toEqual([
      { layer: 'data', plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0', artifact_digest: DIGEST_DATA },
      { layer: 'strategy', plugin_id: STRATEGY_PLUGIN, package_version: '1.0.0', artifact_digest: DIGEST_STRATEGY },
    ])
    expect(frozen[0].links.map((link) => link.to)).toEqual([
      '/backtests/job-frozen',
      '/research/task-1',
      '/strategies/us-1',
    ])
    expect(frozen[0].scope).toEqual(expect.arrayContaining([
      '作业记录', '作业输入快照', '作业插件绑定', '作业数据集版本',
      '任务冻结规范', '任务冻结绑定', '任务作业', '策略实验冻结载荷',
    ]))
    expect(new Set(frozen[0].scope).size).toBe(frozen[0].scope.length)
    expect(frozen[0].unknown).toEqual([])

    const draft = derived.find((item) => item.key === 'draft:us-1:2')!
    expect(draft.kind).toBe('draft')
    expect(draft.job_id).toBeNull()
    expect(draft.scenario_id).toBe('ma-cross.yaml')
    expect(draft.dataset_id).toBe('ds-upload-1')
    expect(draft.source_sha256).toBe(SOURCE_SHA)
    expect(draft.layers).toEqual([])
    expect(draft.scope).toEqual(['策略草稿'])
    expect(draft.links).toEqual([{ to: '/strategies/us-1', label: '策略' }])
    expect(draft.unknown).toEqual(['插件标识'])

    // A run written before snapshots and bindings existed keeps the identity it
    // does have and names each identity it does not.
    const legacy = derived.find((item) => item.key === 'job:job-legacy')!
    expect(legacy.kind).toBe('frozen')
    expect(legacy.scenario_id).toBe('legacy.yaml')
    expect(legacy.dataset_id).toBeNull()
    expect(legacy.source_sha256).toBeNull()
    expect(legacy.layers).toEqual([])
    expect(legacy.unknown).toEqual(['数据集', '来源摘要', '插件标识'])
  })

  it('keeps a task selection unfrozen and a task association without its job unknown', () => {
    // The host attaches the user's selection to the spec it proposes, and only an
    // edit changes it, so the current spec is where a task's composition lives.
    const specSelection: PluginSelection = { data: { plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0' } }
    const withSelection: ResearchTask = {
      ...researchTask,
      task_id: 'task-2',
      jobs: [],
      spec_confirmed: [],
      pinned_binding: null,
      selection: specSelection,
      spec_draft: { ...researchTask.spec_confirmed[0].spec, scenario_id: 'ma-cross.yaml', selection: specSelection },
    }
    const orphaned: ResearchTask = { ...researchTask, task_id: 'task-3', jobs: ['job-gone'], spec_confirmed: [], pinned_binding: null }

    const derived = deriveKnownReferences({ strategies: [], tasks: [withSelection, orphaned], jobs: [] })
    expect(derived).toHaveLength(2)

    const selection = derived.find((item) => item.key === 'selection:task-2')!
    expect(selection.kind).toBe('draft')
    expect(selection.scenario_id).toBe('ma-cross.yaml')
    expect(selection.dataset_id).toBe('ds-upload-1')
    expect(selection.source_sha256).toBe(SOURCE_SHA)
    expect(selection.layers).toEqual([
      { layer: 'data', plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0', artifact_digest: null },
    ])
    // The version is named; the digest that would freeze it is not.
    expect(selection.unknown).toEqual(['插件摘要'])

    // A task that names a run this read could not see keeps the association and
    // states every identity it cannot observe.
    const orphan = derived.find((item) => item.key === 'job:job-gone')!
    expect(orphan.kind).toBe('frozen')
    expect(orphan.job_id).toBe('job-gone')
    expect(orphan.scenario_id).toBeNull()
    expect(orphan.dataset_id).toBeNull()
    expect(orphan.source_sha256).toBeNull()
    expect(orphan.unknown).toEqual(['场景', '数据集', '来源摘要', '插件标识'])
    expect(orphan.links).toEqual([
      { to: '/backtests/job-gone', label: '运行' },
      { to: '/research/task-3', label: '研究' },
    ])
    expect(derived.filter((item) => item.key === 'job:job-gone')).toHaveLength(1)
  })

  it('derives no draft reference from a saved intent, which has no scenario to name', () => {
    // A saved intent holds no scenario, no dataset and no source at all. It is not
    // a draft this page can name, so it contributes no reference rather than one
    // whose every identity would have to be reported as unknown.
    const savedIntent: UserStrategy = {
      ...userStrategy,
      user_strategy_id: 'us-intent',
      draft: { kind: 'intent', revision: 1, scenario_id: null },
      experiments: [],
    }
    expect(deriveKnownReferences({ strategies: [savedIntent] })).toEqual([])
    // The bound strategy beside it still contributes its own draft and frozen run.
    expect(deriveKnownReferences({ strategies: [savedIntent, userStrategy] }).map((item) => item.key))
      .toEqual(['draft:us-1:2', 'job:job-frozen'])
  })

  it('encodes each linked identifier and derives nothing from a source that was never read', () => {
    // The identifier is the host's; this pins the encoding rule, not one id shape.
    const derived = deriveKnownReferences({ jobs: [{ ...frozenJob, job_id: 'job/1 x' }] })
    expect(derived).toHaveLength(1)
    expect(derived[0].key).toBe('job:job/1 x')
    expect(derived[0].links).toEqual([{ to: `/backtests/${encodeURIComponent('job/1 x')}`, label: '运行' }])
    expect(derived[0].links[0].to).toBe('/backtests/job%2F1%20x')

    // A source that was never read contributes no reference and is never
    // reported as an empty one.
    expect(deriveKnownReferences({})).toEqual([])
    expect(deriveKnownReferences({ jobs: [frozenJob] })).toHaveLength(1)
    expect(deriveKnownReferences({ jobs: [frozenJob] })[0].links).toEqual([{ to: '/backtests/job-frozen', label: '运行' }])
  })

  it('reads the composition the current spec states, never the selection the task was created with', () => {
    const created: PluginSelection = { model: { plugin_id: MODEL_PLUGIN, package_version: '1.0.0' } }
    const chosen: PluginSelection = { data: { plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0' } }
    const base = researchTask.spec_confirmed[0].spec
    // The user edited the spec's selection; the host keeps that edit in the spec.
    const edited: ResearchTask = {
      ...researchTask,
      task_id: 'task-edited',
      jobs: [],
      spec_confirmed: [],
      pinned_binding: null,
      selection: created,
      spec_draft: { ...base, selection: chosen },
    }
    // The user cleared it, and the host persists a cleared selection as absence.
    const cleared: ResearchTask = {
      ...researchTask,
      task_id: 'task-cleared',
      jobs: [],
      spec_confirmed: [],
      pinned_binding: null,
      selection: created,
      spec_draft: { ...base },
    }
    const derived = deriveKnownReferences({ strategies: [], tasks: [edited, cleared], jobs: [] })

    const edit = derived.find((item) => item.key === 'selection:task-edited')!
    expect(edit.kind).toBe('draft')
    expect(edit.layers).toEqual([
      { layer: 'data', plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0', artifact_digest: null },
    ])
    expect(edit.unknown).toEqual(['插件摘要'])
    expect(JSON.stringify(edit)).not.toContain(MODEL_PLUGIN)

    const empty = derived.find((item) => item.key === 'selection:task-cleared')!
    expect(empty.kind).toBe('draft')
    expect(empty.layers).toEqual([])
    expect(empty.default_route).toBe(true)
    expect(empty.scenario_id).toBe('ma-cross.yaml')
    // Nothing was chosen, so nothing is missing, and the abandoned creation-time
    // selection is not resurrected anywhere.
    expect(empty.unknown).toEqual([])
    expect(JSON.stringify(empty)).not.toContain(MODEL_PLUGIN)
  })

  it('keeps a confirmed and pinned task as a frozen reference with no run invented for it', () => {
    const pinned: ResearchTask = { ...researchTask, task_id: 'task-pinned', jobs: [], selection: null }
    const derived = deriveKnownReferences({ strategies: [], tasks: [pinned], jobs: [] })
    expect(derived).toHaveLength(1)

    const frozen = derived[0]
    expect(frozen.key).toBe('task:task-pinned')
    expect(frozen.kind).toBe('frozen')
    expect(frozen.job_id).toBeNull()
    expect(frozen.scenario_id).toBe('ma-cross.yaml')
    expect(frozen.dataset_id).toBe('ds-upload-1')
    expect(frozen.source_sha256).toBe(SOURCE_SHA)
    expect(frozen.layers).toEqual([
      { layer: 'data', plugin_id: 'ea.dataset.uploaded.version', package_version: '2.0.0', artifact_digest: DIGEST_DATA },
      { layer: 'strategy', plugin_id: STRATEGY_PLUGIN, package_version: '1.0.0', artifact_digest: DIGEST_STRATEGY },
    ])
    expect(frozen.default_route).toBe(false)
    expect(frozen.unknown).toEqual([])
    // The binding is the reference; no run is invented to carry it.
    expect(frozen.links).toEqual([{ to: '/research/task-pinned', label: '研究' }])

    // A task that recorded nothing at all — no spec, no binding, no jobs, no
    // selection — contributes no reference, and none of its identities is guessed.
    const bare: ResearchTask = {
      ...researchTask,
      task_id: 'task-bare',
      jobs: [],
      spec_confirmed: [],
      spec_draft: null,
      selection: null,
      pinned_binding: null,
    }
    expect(deriveKnownReferences({ strategies: [], tasks: [bare], jobs: [] })).toEqual([])
  })
})
