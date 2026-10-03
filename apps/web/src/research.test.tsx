/**
 * Research workspace UI contract.
 *
 * The adapter below is a scripted double: every task document, job and holdout
 * in these tests is hand-written fixture data, not a model output and not an
 * engine result. The scripted "proposal" only proves that the UI shows what the
 * backend persisted and never drafts, confirms, retries or submits on its own.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type ChronologicalHoldout } from './app'
import { ResearchRequestError, type ModelConfigurationStatus, type ResearchTask } from './research-api'

const spec = {
  strategy_id: 'bounded-long-v1',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'One registered entry with a two-unit target.',
  scenario_id: null,
  dataset_id: null,
  source_sha256: null,
  parameters: { target_quantity: '2', entry_delay_bars: 0 },
  assumptions: ['The registered scenario window is representative.'],
}

function task(overrides: Partial<ResearchTask> = {}): ResearchTask {
  return {
    schema: 'radian.research-task.v1',
    task_id: 'task-1',
    created_at: '2026-10-03T10:00:00.000000Z',
    updated_at: '2026-10-03T10:00:00.000000Z',
    status: 'awaiting_confirmation',
    raw_idea: 'Test a two-unit entry on AAPL',
    gap: null,
    budget: { max_model_calls: 10, max_backtests: 5 },
    usage: { model_calls: 1, input_tokens: 812, output_tokens: 143, backtests: 0 },
    spec_draft: spec,
    spec_confirmed: [],
    jobs: [],
    request_id: null,
    error: null,
    cancel_note: null,
    last_model: { model: 'scripted-proposal-fixture', at: '2026-10-03T10:00:00.000000Z', outcome: 'tool_call' },
    reservation: null,
    backtest_reservation: null,
    spent_calls: [1],
    operation_generation: 2,
    operation_completion_generation: 2,
    operation_kind: null,
    runtime_diagnostic: null,
    ...overrides,
  }
}

const job: BacktestJob = {
  schema: 'ea.local-web-job.v2',
  job_id: 'job-1',
  request_id: 'request-1',
  scenario_id: 'bounded-long.yaml',
  input_identity: { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 4 },
  status: 'succeeded',
  engine_run_id: 'run-1',
  report_sha256: 'c'.repeat(64),
  summary_sha256: 'd'.repeat(64),
  error_code: null,
  message: null,
  report_ready: true,
  strategy_descriptor: { strategy_id: 'bounded-long-v1', strategy_version: 1, research_visible: true, parameters: [] },
}

const holdout: ChronologicalHoldout = {
  schema: 'ea.chronological-holdout.v1',
  validation_id: 'holdout-1',
  created_at: '2026-10-03T10:05:00.000000Z',
  source_job_id: 'job-1',
  holdout_job_id: 'job-2',
}

const configured: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1',
  settings_file: '/tmp/radian/settings.json',
  configured: true,
  provider: 'example-provider',
  model: 'example-model-v1',
  base_url: 'http://127.0.0.1:9999',
  api_key_env: null,
  endpoint: 'http://127.0.0.1:9999',
  protocol: 'example-protocol',
  capabilities: { tool_use: true, structured_output: false, usage: true },
  required_capabilities: ['tool_use'],
  missing_capabilities: [],
  configuration_error: null,
}

type Script = {
  task?: ResearchTask
  tasks?: ResearchTask[]
  settings?: ModelConfigurationStatus
  settingsError?: Error
  getBacktest?: () => Promise<BacktestJob>
  holdouts?: ChronologicalHoldout[]
  create?: () => Promise<ResearchTask>
  confirm?: () => Promise<ResearchTask>
  retry?: () => Promise<ResearchTask>
  cancel?: () => Promise<ResearchTask>
}

function scripted(script: Script = {}) {
  const calls = { list: 0, create: 0, get: 0, confirm: 0, retry: 0, cancel: 0 }
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => script.holdouts ?? [],
    listScenarios: async () => [],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => job,
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => (script.getBacktest ? script.getBacktest() : job),
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    settingsStatus: async () => {
      if (script.settingsError) throw script.settingsError
      return script.settings ?? configured
    },
    listResearchTasks: async () => { calls.list += 1; return script.tasks ?? [task(script.task)] },
    createResearchTask: async () => { calls.create += 1; return script.create ? script.create() : task(script.task) },
    getResearchTask: async () => { calls.get += 1; return task(script.task) },
    confirmResearchTask: async () => { calls.confirm += 1; return script.confirm ? script.confirm() : task(script.task) },
    retryResearchTask: async () => { calls.retry += 1; return script.retry ? script.retry() : task(script.task) },
    cancelResearchTask: async () => { calls.cancel += 1; return script.cancel ? script.cancel() : task(script.task) },
  }
  return { api, calls }
}

function detail(api: ApiAdapter) {
  window.history.pushState({}, '', '/research/task-1')
  render(<App api={api} />)
}

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('research workspace', () => {
  it('lists persisted tasks and drafts only on an explicit submit', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ tasks: [task()] })
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    expect(await screen.findByRole('heading', { name: 'Research Workspace' })).toBeTruthy()
    expect(await screen.findByText('Test a two-unit entry on AAPL')).toBeTruthy()
    expect(screen.getByText(/bounded-long-v1 · XNAS:AAPL/)).toBeTruthy()
    expect(calls.create).toBe(0)

    await user.type(screen.getByLabelText('Research idea'), 'Test a delayed entry')
    await user.click(screen.getByRole('button', { name: 'Create draft' }))

    expect(calls.create).toBe(1)
    expect(await screen.findByRole('heading', { name: 'Draft specification' })).toBeTruthy()
  })

  it('refuses a duplicate submit while the first draft is still in flight', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ create: () => new Promise<ResearchTask>(() => {}) })
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    const idea = await screen.findByLabelText('Research idea')
    await user.type(idea, 'Test a delayed entry')
    const submit = screen.getByRole('button', { name: 'Create draft' })
    await user.click(submit)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Creating draft…' })).toBeTruthy())
    await user.click(screen.getByRole('button', { name: 'Creating draft…' }))
    expect(calls.create).toBe(1)
  })

  it('reports an existing active task for the same idea and reloads the list', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      create: () => { throw new ResearchRequestError('research_task_exists', 'an active research task already exists for this idea') },
    })
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    await user.type(await screen.findByLabelText('Research idea'), 'Test a delayed entry')
    await user.click(screen.getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'An active research task already exists for this idea. Open it below instead of submitting it again.',
    )
    await waitFor(() => expect(calls.list).toBe(2))
  })

  it('confirms only on an explicit click, then links the real job and holdout evidence', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      task: task(),
      holdouts: [holdout],
      confirm: async () => task({
        status: 'succeeded',
        spec_draft: null,
        spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }],
        usage: { model_calls: 1, input_tokens: 812, output_tokens: 143, backtests: 1 },
        jobs: ['job-1'],
      }),
    })
    detail(api)

    expect(await screen.findByRole('heading', { name: 'Draft specification' })).toBeTruthy()
    expect(screen.getByText('Unconfirmed draft')).toBeTruthy()
    expect(screen.getByText('One registered entry with a two-unit target.')).toBeTruthy()
    expect(screen.getByText('The registered scenario window is representative.')).toBeTruthy()
    expect(screen.getByText('1 of 10')).toBeTruthy()
    expect(calls.confirm).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Confirm spec and start real backtest' }))

    expect(calls.confirm).toBe(1)
    expect(await screen.findByRole('heading', { name: 'Confirmed specification' })).toBeTruthy()
    const jobCard = (await screen.findByText('job-1')).closest('li')!
    expect(within(jobCard).getByRole('link', { name: 'Open job and report' }).getAttribute('href')).toBe('/backtests/job-1')
    expect(within(jobCard).getByRole('link', { name: 'Evaluate chronological holdout' }).getAttribute('href')).toBe('/holdouts/new/job-1')
    expect(within(jobCard).getByRole('link', { name: 'Open holdout holdout-1' }).getAttribute('href')).toBe('/holdouts/holdout-1')
    expect(within(jobCard).getByRole('link', { name: 'Create evaluated candidate' }).getAttribute('href')).toBe('/backtests/job-1')
    expect(within(jobCard).getByRole('link', { name: 'Download report.json' }).getAttribute('href')).toBe('/api/backtests/job-1/artifacts/report.json')
    expect(screen.getByText(/Candidate acceptance records a research judgment only/)).toBeTruthy()
  })

  it('restarts polling after confirmation and refreshes the completed job projection', async () => {
    const user = userEvent.setup()
    let current = task()
    let completed = false
    const { api, calls } = scripted({ confirm: async () => {
      current = task({ status: 'running', spec_draft: null, jobs: ['job-1'] })
      return current
    } })
    api.getResearchTask = async () => {
      calls.get += 1
      if (current.status === 'running') {
        completed = true
        current = task({ status: 'succeeded', spec_draft: null, jobs: ['job-1'] })
      }
      return current
    }
    api.getBacktest = async () => ({ ...job, status: completed ? 'succeeded' : 'running' })
    detail(api)
    await screen.findByRole('heading', { name: 'Draft specification' })
    await user.click(screen.getByRole('button', { name: 'Confirm spec and start real backtest' }))
    await screen.findByText('backtest succeeded', { selector: '.page-title .status' })
    const jobCard = (await screen.findByText('job-1')).closest('li')!
    await waitFor(() => expect(within(jobCard).getByText('succeeded')).toBeTruthy())
    expect(calls.confirm).toBe(1)
  })

  it('never retries an outcome that left the process and explains why', async () => {
    const { api } = scripted({
      task: task({
        status: 'failed',
        spec_draft: null,
        spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }],
        error: { step: 'start_backtest', code: 'job_outcome_unknown', message: 'the submission was never confirmed' },
        last_model: { model: 'scripted-proposal-fixture', at: '2026-10-03T10:02:00.000000Z', outcome: 'unknown_after_dispatch' },
        backtest_reservation: { kind: 'backtest', seq: 1, status: 'unknown', at: '2026-10-03T10:02:00.000000Z' },
        usage: { model_calls: 1, input_tokens: 812, output_tokens: 143, backtests: 1 },
      }),
    })
    detail(api)

    expect(await screen.findByText(/A dispatched backtest submission was never confirmed/)).toBeTruthy()
    expect(screen.getByText(/Cancel this task and create a new one for another attempt/)).toBeTruthy()
    expect(screen.getByText(/stays charged and is never re-submitted automatically/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Retry failed step' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel task' })).toBeTruthy()
  })

  it('offers retry only for a failure that provably spent nothing and reloads a failed mutation', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const { api, calls } = scripted({
      task: task({
        status: 'failed',
        error: { step: 'propose', code: 'model_not_configured', message: 'model provider is not configured' },
        last_model: null,
      }),
      retry: async () => {
        attempts += 1
        if (attempts === 1) throw new ResearchRequestError('task_in_flight', 'an operation is already in flight')
        return task({ status: 'awaiting_confirmation' })
      },
    })
    detail(api)

    const retry = await screen.findByRole('button', { name: 'Retry failed step' })
    expect(screen.getByText(/Retry resumes the failed step in place/)).toBeTruthy()
    await user.click(retry)

    expect(calls.retry).toBe(1)
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'an operation is already in flight')
    expect(calls.get).toBe(2)
    expect(calls.confirm).toBe(0)
  })

  it('treats cancel as final, never as a rollback, and keeps started runs visible', async () => {
    const user = userEvent.setup()
    const running = task({
      status: 'running',
      spec_draft: null,
      spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }],
      jobs: ['job-1'],
      usage: { model_calls: 1, input_tokens: 812, output_tokens: 143, backtests: 1 },
    })
    const { api, calls } = scripted({
      task: running,
      cancel: async () => task({
        ...running,
        status: 'cancelled',
        cancel_note: 'Already started backtests keep their own terminal states; cancelling is not a rollback.',
      }),
    })
    detail(api)

    expect(await screen.findByText(/Cancelling is final/)).toBeTruthy()
    expect(screen.getByText(/never claims a rollback or kills a backtest that already started/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Cancel task' }))

    expect(calls.cancel).toBe(1)
    expect(await screen.findByText(/Already started backtests keep their own terminal states/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel task' })).toBeNull()
    expect(screen.getByText('job-1')).toBeTruthy()
  })

  it('polls a task that can still advance and stops once it is terminal', async () => {
    let current = task({ status: 'running', spec_draft: null, spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }] })
    const calls = { get: 0 }
    const api = { ...scripted().api, getResearchTask: async () => { calls.get += 1; return current } }
    detail(api)

    await screen.findByText(/Cancelling is final/)
    await waitFor(() => expect(calls.get).toBeGreaterThan(1), { timeout: 3000 })
    const settled = calls.get
    current = task({ status: 'cancelled', spec_draft: null, cancel_note: 'Cancelled before any backtest started.' })
    await new Promise(resolve => setTimeout(resolve, 1400))
    expect(calls.get).toBeGreaterThanOrEqual(settled)
    const afterCancel = calls.get
    await new Promise(resolve => setTimeout(resolve, 1400))
    expect(calls.get).toBe(afterCancel)
  })

  it('shows the reported protocol and capabilities instead of assuming a provider', async () => {
    const { api } = scripted({ settings: configured })
    detail(api)

    expect(await screen.findByText('example-provider')).toBeTruthy()
    expect(screen.getByText('example-protocol')).toBeTruthy()
    expect(screen.getByText('example-model-v1')).toBeTruthy()
    expect(screen.getByText('http://127.0.0.1:9999')).toBeTruthy()
    expect(screen.getByText('tool_use · declared')).toBeTruthy()
    expect(screen.getByText('Configured')).toBeTruthy()
    expect(screen.getByText(/A custom endpoint must implement the reported protocol/)).toBeTruthy()
  })

  it('fails closed when the reported protocol does not declare a required capability', async () => {
    const { api } = scripted({
      settings: {
        ...configured,
        configured: false,
        missing_capabilities: ['tool_use'],
        capabilities: { tool_use: false, structured_output: true, usage: true },
        configuration_error: 'the configured model protocol does not declare: tool_use',
      },
    })
    detail(api)

    expect(await screen.findByText('Not usable')).toBeTruthy()
    expect(screen.getByRole('alert')).toHaveProperty(
      'textContent',
      'The configured protocol does not declare: tool_use. Research fails closed and dispatches nothing.',
    )
    expect(screen.getByText(/Research cannot draft a spec: the configured model protocol does not declare: tool_use/)).toBeTruthy()
    expect(screen.getByText('Missing')).toBeTruthy()
  })

  it('reports an unreadable model configuration instead of a false ready state', async () => {
    const { api } = scripted({ settingsError: new Error('Model configuration status is unavailable.') })
    detail(api)

    expect(await screen.findByText('Status unavailable')).toBeTruthy()
    expect(screen.getByText('Model configuration status is unavailable.')).toBeTruthy()
  })
})


it('shows unknown or partial token usage without refunding model calls', async () => {
  const missing = scripted({ task: task({
    usage: { model_calls: 2, input_tokens: 0, output_tokens: 0, backtests: 0, unavailable_model_calls: 2 },
  }) })
  detail(missing.api)
  expect(await screen.findByText(/Token usage unavailable for 2 model calls/)).toBeTruthy()
  let panel = screen.getByRole('region', { name: 'Frozen budget and measured usage' })
  expect(within(panel).getAllByText('Unknown (no usage reported)')).toHaveLength(2)
  expect(within(panel).getByText('2 of 10')).toBeTruthy()
  expect(within(panel).queryByText('0')).toBeNull()
  cleanup()
  const partial = scripted({ task: task({
    usage: { model_calls: 3, input_tokens: 812, output_tokens: 143, backtests: 0, unavailable_model_calls: 2 },
  }) })
  detail(partial.api)
  expect(await screen.findByText(/Reported totals are incomplete; these calls remain charged/)).toBeTruthy()
  panel = screen.getByRole('region', { name: 'Frozen budget and measured usage' })
  expect(within(panel).getByText('812 reported (partial)')).toBeTruthy()
  expect(within(panel).getByText('143 reported (partial)')).toBeTruthy()
  expect(within(panel).getByText('3 of 10')).toBeTruthy()
})
