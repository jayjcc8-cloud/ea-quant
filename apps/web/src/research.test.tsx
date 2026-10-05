/**
 * Research workspace UI contract.
 *
 * The adapter below is a scripted double: every task document, job and holdout
 * in these tests is hand-written fixture data, not a model output and not an
 * engine result. The scripted "proposal" only proves that the UI shows what the
 * backend persisted and never drafts, confirms, retries or submits on its own.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type BacktestReport, type ChronologicalHoldout } from './app'
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

/** A verified report handwritten to the published v1 schema for run-1. */
const report: BacktestReport = {
  schema: 'ea.backtest-report.v1',
  run_id: 'run-1',
  economics: {
    currency: 'USD',
    initial_funding: { amount: '10000', currency: 'USD' },
    ending_cash: [{ amount: '9797', currency: 'USD' }],
    ending_positions: [{ quantity: '2', venue: 'XNAS', symbol: 'AAPL' }],
    valuation: { price: '110', position_value: '220' },
    equity: { amount: '10017', currency: 'USD' },
    net_pnl: { amount: '17', currency: 'USD' },
    total_return: { value: '0.0017' },
    counts: { orders: 1, fills: 1 },
    execution: { order: { quantity: '2', side: 'buy' }, fill: { quantity: '2', price: '101.5', side: 'buy' } },
  },
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
  report?: () => Promise<BacktestReport>
}

function scripted(script: Script = {}) {
  const calls = { list: 0, create: 0, get: 0, confirm: 0, retry: 0, cancel: 0 }
  let persisted = task(script.task)
  const persist = async (operation?: () => Promise<ResearchTask>) => {
    const next = operation ? await operation() : persisted
    persisted = next
    return next
  }
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
    getReport: async () => {
      if (!script.report) throw new Error('unused')
      return script.report()
    },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    settingsStatus: async () => {
      if (script.settingsError) throw script.settingsError
      return script.settings ?? configured
    },
    listResearchTasks: async () => { calls.list += 1; return script.tasks ?? [persisted] },
    createResearchTask: async () => { calls.create += 1; return persist(script.create) },
    getResearchTask: async () => { calls.get += 1; return persisted },
    confirmResearchTask: async () => { calls.confirm += 1; return persist(script.confirm) },
    retryResearchTask: async () => { calls.retry += 1; return persist(script.retry) },
    cancelResearchTask: async () => { calls.cancel += 1; return persist(script.cancel) },
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

  it('ignores a delayed pre-retry read even when the mutation keeps the failed status', async () => {
    const user = userEvent.setup()
    const before = task({
      status: 'failed', last_model: null,
      error: { step: 'propose', code: 'model_not_configured', message: 'Before retry' },
    })
    const after = task({
      ...before,
      error: { step: 'propose', code: 'budget_exceeded', message: 'New persisted retry failure' },
      operation_generation: 3, operation_completion_generation: 3,
    })
    const { api, calls } = scripted({ task: before, retry: async () => after })
    const readPersisted = api.getResearchTask!
    let delayNextRead = false
    let releaseRead!: (value: ResearchTask) => void
    api.getResearchTask = async id => {
      if (!delayNextRead) return readPersisted(id)
      delayNextRead = false
      calls.get += 1
      return new Promise<ResearchTask>(resolve => { releaseRead = resolve })
    }
    detail(api)
    await screen.findByRole('button', { name: 'Retry failed step' })
    delayNextRead = true
    await user.click(screen.getByRole('button', { name: /^Refresh$/ }))
    await waitFor(() => expect(releaseRead).toBeTypeOf('function'))
    await user.click(screen.getByRole('button', { name: 'Retry failed step' }))
    await screen.findByText('New persisted retry failure')
    await act(async () => { releaseRead(before) })
    expect(screen.getByText('New persisted retry failure')).toBeTruthy()
    expect(screen.queryByText('Before retry')).toBeNull()
    expect(calls.retry).toBe(1)
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
    const readsBeforeRetry = calls.get
    await user.click(retry)

    expect(calls.retry).toBe(1)
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'an operation is already in flight')
    expect(calls.get).toBe(readsBeforeRetry + 1)
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

/**
 * The detail page leads with the state the task is in.
 *
 * Whatever a task has already settled is what a reader meets first, and only the
 * actions that state can actually accept are offered. Every fixture here is a
 * persisted document; the report is a handwritten v1 document for run-1.
 */
it('leads with the state the task is in and offers only the actions it allows', async () => {
  const confirmed = [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }]
  const runningJob: BacktestJob = { ...job, status: 'running', report_ready: false }
  const cases = [
    { status: 'drafting', overrides: { status: 'drafting' as const, spec_draft: null, spec_confirmed: [] }, job,
      first: 'Task state', stateHeading: 'Drafting a specification',
      allows: ['Cancel task'], denies: ['Confirm spec and start real backtest', 'Retry failed step'] },
    { status: 'awaiting_confirmation', overrides: {}, job,
      first: 'Task state', stateHeading: 'Waiting for your confirmation',
      allows: ['Confirm spec and start real backtest', 'Cancel task'], denies: ['Retry failed step'] },
    { status: 'running', overrides: { status: 'running' as const, spec_draft: null, spec_confirmed: confirmed, jobs: ['job-1'] }, job: runningJob,
      first: 'Task state', stateHeading: 'One backtest running',
      allows: ['Cancel task'], denies: ['Confirm spec and start real backtest'] },
    { status: 'failed', overrides: { status: 'failed' as const, error: { step: 'propose', code: 'model_not_configured', message: 'no provider' } }, job,
      first: 'Failure explanation', stateHeading: 'This task failed',
      allows: ['Retry failed step', 'Cancel task'], denies: [] },
    // A succeeded run settles its own state, so it carries no state panel: the
    // block a reader meets first is the report summary itself.
    { status: 'succeeded', overrides: { status: 'succeeded' as const, spec_draft: null, spec_confirmed: confirmed, jobs: ['job-1'] }, job,
      first: 'Verified report summary', stateHeading: null,
      allows: [], denies: ['Confirm spec and start real backtest', 'Cancel task'] },
  ]
  for (const item of cases) {
    cleanup()
    const { api } = scripted({ task: task(item.overrides), report: async () => report, getBacktest: async () => item.job })
    detail(api)

    // The block a reader meets first is awaited first: for a succeeded run that
    // is the report summary, which exists only once this run's job has loaded.
    const leading = await screen.findByRole('region', { name: item.first })
    expect(document.querySelector('.research-artifact > section')?.getAttribute('aria-label')).toBe(item.first)
    if (item.stateHeading) {
      const state = await screen.findByRole('region', { name: 'Task state' })
      await within(state).findByRole('heading', { name: item.stateHeading })
      expect(state.textContent).toContain('Model calls 1/10 · backtests 0/5')
    } else {
      expect(screen.queryByRole('region', { name: 'Task state' })).toBeNull()
      await within(leading).findByRole('heading', { name: 'What this run measured' })
      // The report this block was read from is reachable from the block itself,
      // not only from the evidence list further down the page.
      expect(within(leading).getByRole('link', { name: 'Open the run and its verified report' })).toBeTruthy()
    }
    for (const name of item.allows) expect(screen.getByRole('button', { name })).toBeTruthy()
    for (const name of item.denies) expect(screen.queryByRole('button', { name })).toBeNull()
  }
})

it('shows a succeeded run from its own verified report and never guesses its unit', async () => {
  const succeeded = { status: 'succeeded' as const, spec_draft: null, spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }], jobs: ['job-1'] }
  const { api } = scripted({ task: task(succeeded), report: async () => report })
  detail(api)

  const summary = () => screen.getByRole('region', { name: 'Verified report summary' }).textContent ?? ''
  await waitFor(() => expect(summary()).toContain('10017 USD'))
  expect(summary()).toContain('17 USD')
  expect(summary()).toContain('0.17%')
  expect(summary()).toContain('2 units')
  expect(summary()).toContain('4 records')
  // report v1 records no realized/unrealized split: none is invented for it, and
  // the net figure is not presented as realized profit.
  expect(summary()).toContain('records no realized/unrealized split')
  expect(summary()).not.toContain('Realized P&L')
  // Nothing on this page draws a conclusion or submits anything by itself.
  expect(screen.queryByRole('button', { name: 'Interpret this run' })).toBeNull()
})

it('shows no figure at all when the report is not this run\'s own', async () => {
  const { api } = scripted({
    task: task({ status: 'succeeded', spec_draft: null, spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }], jobs: ['job-1'] }),
    report: async () => ({ ...report, run_id: 'run-somewhere-else' }),
  })
  detail(api)

  await waitFor(() => expect(
    screen.getByRole('region', { name: 'Verified report summary' }).textContent,
  ).toContain('does not match this run'))
  const summary = () => screen.getByRole('region', { name: 'Verified report summary' }).textContent ?? ''
  expect(summary()).not.toContain('10017')
  expect(summary()).not.toContain('USD')
  // The run itself stays reachable; only the inferred figures are withheld.
  expect(screen.getByRole('link', { name: 'Open the run and its verified report' })).toBeTruthy()
})

it('names a currency the report did not record instead of assuming USD', async () => {
  const bare: BacktestReport = {
    schema: 'ea.backtest-report.v1', run_id: 'run-1',
    economics: {
      ending_cash: [{ amount: '9797' }],
      ending_positions: [{ quantity: '2', venue: 'XNAS', symbol: 'AAPL' }],
      valuation: { price: '110', position_value: '220' },
      equity: { amount: '10017' }, net_pnl: { amount: '17' },
      total_return: { value: '0.0017' }, counts: { orders: 1, fills: 1 },
      execution: { order: { quantity: '2', side: 'buy' }, fill: { quantity: '2', price: '101.5', side: 'buy' } },
    },
  }
  const { api } = scripted({
    task: task({ status: 'succeeded', spec_draft: null, spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }], jobs: ['job-1'] }),
    report: async () => bare,
  })
  detail(api)

  const summary = () => screen.getByRole('region', { name: 'Verified report summary' }).textContent ?? ''
  await waitFor(() => expect(summary()).toContain('17 · currency not recorded by the report'))
  expect(summary()).not.toContain('USD')
  expect(summary()).toContain('records no currency')
})

/**
 * An explicit refresh revalidates the run this task is bound to, not only the task.
 *
 * A run can lose or replace its report while the task keeps the same status and
 * the same job ids, and the poll below the page only re-reads the task itself, so
 * a refresh that stopped there would leave the last-read figures on screen as if
 * they still described the run. The refresh stays a read: it confirms, retries,
 * cancels, interprets and dispatches nothing.
 */
it('withdraws a verified summary when an explicit refresh finds the run without its report', async () => {
  const user = userEvent.setup()
  let ready = true
  const { api, calls } = scripted({
    task: task({
      status: 'succeeded', spec_draft: null,
      spec_confirmed: [{ version: 1, confirmed_at: '2026-10-03T10:02:00.000000Z', spec }],
      jobs: ['job-1'],
    }),
    // The same run, re-read after its report was withdrawn: same job, same run
    // id, same status, no ready report and no digest.
    getBacktest: async () => ready ? job : { ...job, report_ready: false, report_sha256: null },
    report: async () => report,
  })
  detail(api)

  const summary = () => screen.getByRole('region', { name: 'Verified report summary' }).textContent ?? ''
  await waitFor(() => expect(summary()).toContain('10017 USD'))

  ready = false
  await user.click(screen.getByRole('button', { name: 'Refresh' }))

  await waitFor(() => expect(summary()).toContain('has not recorded a ready verified report'))
  expect(summary()).not.toContain('10017')
  expect(summary()).not.toContain('USD')
  // A refresh re-reads; every write stays where it was.
  expect(calls.confirm).toBe(0)
  expect(calls.retry).toBe(0)
  expect(calls.cancel).toBe(0)
  expect(calls.create).toBe(0)
  expect(screen.queryByRole('button', { name: 'Interpret this run' })).toBeNull()
})
