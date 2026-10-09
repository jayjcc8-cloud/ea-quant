/**
 * Stopping a run from the existing screens.
 *
 * The client asks the controller to stop a live run and then keeps reading the
 * run's own record until that record is terminal. What is asserted here is what
 * the screens render, what they send, and what they never read: no report is
 * asked for a run that stopped, a run that committed is untouched, and the
 * lifecycle boundary is stated as a lifecycle boundary.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type BacktestReport } from './app'
import type { ResearchTask } from './research-api'

afterEach(cleanup)

const IDENTITY = { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 4 }

function job(overrides: Partial<BacktestJob> = {}): BacktestJob {
  return {
    schema: 'ea.local-web-job.v2',
    job_id: 'job-1',
    request_id: 'request-1',
    scenario_id: 'bounded-long.yaml',
    input_identity: IDENTITY,
    status: 'running',
    engine_run_id: null,
    report_sha256: null,
    summary_sha256: null,
    error_code: null,
    message: null,
    report_ready: false,
    ...overrides,
  }
}

const RUNNING = job()
const STOPPING = job({ status: 'stopping', message: 'the run is stopping; no result of it will be published' })

const SUCCEEDED = job({
  status: 'succeeded',
  engine_run_id: 'run-1',
  report_sha256: 'c'.repeat(64),
  summary_sha256: 'd'.repeat(64),
  report_ready: true,
})

const REPORT: BacktestReport = {
  schema: 'ea.backtest-report.v1',
  run_id: 'run-1',
  scenario: { strategy_id: 'bounded-long-v1', instrument: { venue: 'XNAS', symbol: 'AAPL' } },
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

type Calls = { stops: number; reads: number; reports: number }

function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter & { calls: Calls } {
  const calls: Calls = { stops: 0, reads: 0, reports: 0 }
  const base: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    listScenarios: async () => [],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { throw new Error('unused') },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => { calls.reads += 1; return RUNNING },
    getReport: async () => { calls.reports += 1; return REPORT },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
  }
  return Object.assign({ ...base, ...overrides }, { calls })
}

const pageText = () => document.body.textContent ?? ''

describe('stopping a run from the run page', () => {
  it('asks once, shows the accepted stop, and follows it to the confirmed one', async () => {
    let stops = 0
    const api = adapter({
      getBacktest: async () => (stops === 0 ? RUNNING : job({ status: 'stopped', message: 'the run was stopped before it published a result' })),
      stopBacktest: async () => { stops += 1; return STOPPING },
    })
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={api} />)

    const stop = await screen.findByRole('button', { name: '停止运行' })
    await userEvent.click(stop)

    // One request, and the accepted state is what the page reads next.
    expect(stops).toBe(1)
    expect(api.calls.reports).toBe(0)
    await waitFor(() => expect(pageText()).toContain('stopped'))
    expect(pageText()).toContain('No success report is available.')
    // A stopped run has no official report to read, and no stop control left.
    expect(api.calls.reports).toBe(0)
    expect(screen.queryByRole('button', { name: '停止运行' })).toBeNull()
  })

  it('reads the official report of a committed run and offers no stop', async () => {
    const api = adapter({ getBacktest: async () => SUCCEEDED })
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={api} />)

    await waitFor(() => expect(pageText()).toContain('10017'))
    expect(api.calls.reports).toBe(1)
    expect(api.calls.stops).toBe(0)
    expect(screen.queryByRole('button', { name: '停止运行' })).toBeNull()
  })

  it("shows the run's own stop state as terminal with nothing to read", async () => {
    const api = adapter({ getBacktest: async () => job({ status: 'stopped', engine_run_id: 'run-1' }) })
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={api} />)

    await waitFor(() => expect(pageText()).toContain('No success report is available.'))
    // A stopped run never becomes a report, whatever staged files it left.
    expect(api.calls.reports).toBe(0)
    expect(screen.queryByRole('button', { name: '停止运行' })).toBeNull()
  })
})

const BOUND_TASK: ResearchTask = {
  schema: 'radian.research-task.v1',
  task_id: 'task-1',
  created_at: '2026-10-03T10:00:00.000000Z',
  updated_at: '2026-10-03T10:05:00.000000Z',
  status: 'cancelled',
  raw_idea: 'Test a two-unit entry on AAPL',
  gap: null,
  budget: { max_model_calls: 10, max_backtests: 5 },
  usage: { model_calls: 3, input_tokens: 812, output_tokens: 143, backtests: 1 },
  spec_draft: null,
  spec_confirmed: [],
  jobs: ['job-1'],
  request_id: 'request-1',
  error: null,
  cancel_note: 'Every run this task started is being stopped, and a run that already committed keeps its own result. Cancelling is not a rollback.',
  last_model: null,
  reservation: null,
  backtest_reservation: null,
  spent_calls: [],
  operation_generation: 3,
  operation_completion_generation: 3,
  operation_kind: null,
  runtime_diagnostic: null,
}

describe('the research page while a bound run stops', () => {
  it("shows the run's own stop state instead of a finished result", async () => {
    const api = adapter({
      getResearchTask: async () => BOUND_TASK,
      listResearchTasks: async () => [BOUND_TASK],
      getBacktest: async () => STOPPING,
    })
    window.history.pushState({}, '', '/research/task-1')
    render(<App api={api} />)

    await waitFor(() => expect(pageText()).toContain('stopping · stop accepted, not yet confirmed'))
    // A run that has not published is not offered as evidence.
    expect(pageText()).not.toContain('Download report.json')
    expect(api.calls.reports).toBe(0)
  })
})

describe('polling a run that is stopping', () => {
  /** One bound run whose status comes from a scripted sequence of reads. */
  function stoppingHost(statuses: BacktestJob['status'][], onRead?: () => void) {
    const reads: string[] = []
    const api = adapter({
      getResearchTask: async () => BOUND_TASK,
      listResearchTasks: async () => [BOUND_TASK],
      getBacktest: async () => {
        onRead?.()
        const status = statuses[Math.min(reads.length, statuses.length - 1)]
        reads.push(status)
        return job({ status, engine_run_id: status === 'stopped' ? 'run-1' : null })
      },
    })
    return { api, reads }
  }

  it("keeps reading a stopping run until the controller reports a terminal state", async () => {
    // Two identical `stopping` answers still schedule the next read: an unchanged
    // status is not an outcome, and the run is only settled when it is terminal.
    const { api, reads } = stoppingHost(['stopping', 'stopping', 'stopped'])
    window.history.pushState({}, '', '/research/task-1')
    render(<App api={api} />)

    await waitFor(
      () => expect(pageText()).toContain('stopped · no result was published'),
      { timeout: 5000 },
    )
    expect(reads.length).toBeGreaterThanOrEqual(3)
    expect(api.calls.reports).toBe(0)
  })

  it('never leaves a second read in flight while one is still unanswered', async () => {
    let inFlight = 0
    let peak = 0
    let calls = 0
    const pending: Array<() => void> = []
    const api = adapter({
      getResearchTask: async () => BOUND_TASK,
      listResearchTasks: async () => [BOUND_TASK],
      getBacktest: () => {
        calls += 1
        inFlight += 1
        peak = Math.max(peak, inFlight)
        return new Promise(resolve => {
          pending.push(() => {
            inFlight -= 1
            resolve(job({ status: 'stopping' }))
          })
        })
      },
    })
    window.history.pushState({}, '', '/research/task-1')
    const view = render(<App api={api} />)

    await waitFor(() => expect(calls).toBe(1))
    // Hold the answer past the poll interval: nothing may start a second read.
    await new Promise(resolve => setTimeout(resolve, 600))
    expect(calls).toBe(1)
    pending[0]?.()
    await waitFor(() => expect(calls).toBe(2), { timeout: 5000 })
    expect(peak).toBe(1)
    view.unmount()
  })

  it('stops reading once the page is unmounted', async () => {
    const { api, reads } = stoppingHost(['stopping'])
    window.history.pushState({}, '', '/research/task-1')
    const view = render(<App api={api} />)

    await waitFor(() => expect(reads.length).toBeGreaterThanOrEqual(1))
    view.unmount()
    const settled = reads.length
    await new Promise(resolve => setTimeout(resolve, 900))
    expect(reads.length).toBe(settled)
  })
})
