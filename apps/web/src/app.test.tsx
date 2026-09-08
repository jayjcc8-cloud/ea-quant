import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type BacktestReport, type InputSnapshot } from './app'

const identity = { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 4 }
const scenarios = [
  {
    scenario_id: 'bounded-long.yaml', name: 'bounded-long', valid: true as const, input_identity: identity,
    summary: { strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', target_quantity: '2', entry_delay_bars: 0, record_count: 4 },
    strategy_parameters: [
      { name: 'target_quantity' as const, type: 'decimal' as const, default: '2', current_value: '2', minimum: '1', maximum: null },
      { name: 'entry_delay_bars' as const, type: 'integer' as const, default: 0, current_value: 0, minimum: 0, maximum: 2 },
    ],
  },
  {
    scenario_id: 'flat.yaml', name: 'flat', valid: true as const, input_identity: identity,
    summary: { strategy_id: 'always-flat-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', target_quantity: null, entry_delay_bars: 0, record_count: 4 },
    strategy_parameters: [],
  },
  { scenario_id: 'invalid.yaml', name: 'invalid', valid: false as const, error_code: 'scenario_invalid', message: 'fingerprint conflicts' },
]
function snapshot(initialCash: string, quantity: string, entryDelayBars = 0): InputSnapshot {
  return {
    schema: 'ea.local-web-input.v1', scenario_id: 'bounded-long.yaml', source_identity: identity,
    identity: { ...identity, scenario_sha256: `${quantity.at(0) ?? 'f'}`.repeat(64) },
    scenario: {
      funding: { currency: 'USD', initial_cash: initialCash },
      strategy: { id: 'bounded-long-v1', target_quantity: quantity, entry_delay_bars: entryDelayBars },
      instrument: { venue: 'XNAS', symbol: 'AAPL' },
    },
  }
}

const job: BacktestJob = {
  schema: 'ea.local-web-job.v2', job_id: 'job-1', request_id: 'request-1', scenario_id: 'bounded-long.yaml',
  input_identity: identity, status: 'succeeded', engine_run_id: 'run-1', report_sha256: 'c'.repeat(64),
  summary_sha256: 'd'.repeat(64), error_code: null, message: null, report_ready: true,
  created_at: '2026-09-05T15:00:00.000000Z', input_snapshot: snapshot('10000', '2'),
  input_sha256: 'e'.repeat(64), attempt_id: 'run-1',
}
const report: BacktestReport = {
  schema: 'ea.backtest-report.v1', run_id: 'run-1',
  scenario: { strategy_id: 'bounded-long-v1', instrument: { venue: 'XNAS', symbol: 'AAPL' } },
  economics: {
    currency: 'USD', initial_funding: { amount: '10000', currency: 'USD' },
    ending_cash: [{ amount: '9797', currency: 'USD' }],
    ending_positions: [{ quantity: '2', venue: 'XNAS', symbol: 'AAPL' }],
    valuation: { price: '110', position_value: '220' },
    equity: { amount: '10017', currency: 'USD' }, net_pnl: { amount: '17', currency: 'USD' },
    total_return: { value: '0.0017' }, counts: { orders: 1, fills: 1 },
    execution: { order: { quantity: '2', side: 'buy' }, fill: { quantity: '2', price: '101.5', side: 'buy' } },
  },
}
const secondJob: BacktestJob = {
  ...job, job_id: 'job-2', request_id: 'request-2', engine_run_id: 'run-2', attempt_id: 'run-2',
  created_at: '2026-09-05T15:10:00.000000Z', input_snapshot: snapshot('10000', '4', 2), input_sha256: 'f'.repeat(64),
}
const secondReport: BacktestReport = {
  ...report, run_id: 'run-2',
  economics: {
    ...report.economics,
    ending_cash: [{ amount: '9594', currency: 'USD' }],
    ending_positions: [{ quantity: '4', venue: 'XNAS', symbol: 'AAPL' }],
    valuation: { price: '110', position_value: '440' },
    equity: { amount: '10034', currency: 'USD' },
    net_pnl: { amount: '34', currency: 'USD' },
    total_return: { value: '0.0034' },
    execution: { order: { quantity: '4', side: 'buy' }, fill: { quantity: '4', price: '101.5', side: 'buy' } },
  },
}
const thirdJob: BacktestJob = {
  ...secondJob, job_id: 'job-3', request_id: 'request-3', engine_run_id: 'run-3', attempt_id: 'run-3',
  created_at: '2026-09-05T15:15:00.000000Z', input_snapshot: snapshot('10000', '3', 1), input_sha256: '8'.repeat(64),
}
const thirdReport: BacktestReport = { ...secondReport, run_id: 'run-3' }
const rejectedJob: BacktestJob = {
  ...job, job_id: 'job-risk', request_id: 'request-risk', status: 'failed', engine_run_id: 'run-risk',
  attempt_id: 'run-risk', report_sha256: null, summary_sha256: null, report_ready: false,
  error_code: 'risk.rejected', message: 'scenario order was rejected by risk',
  created_at: '2026-09-05T15:20:00.000000Z', input_snapshot: snapshot('50', '2'), input_sha256: '9'.repeat(64),
}

const batch = {
  schema: 'ea.local-web-batch.v1' as const,
  batch_id: 'batch-1', created_at: '2026-09-06T03:00:00.000000Z',
  scenario_id: 'bounded-long.yaml', input_identity: identity,
  member_job_ids: ['job-1', 'job-risk'], member_count: 2, status: 'complete' as const,
  members: [
    { ...job, presentation_status: 'succeeded' },
    { ...rejectedJob, presentation_status: 'risk.rejected' },
  ],
}

function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return {
    listScenarios: async () => scenarios,
    validateScenario: async (scenarioId) => scenarios.find((item) => item.scenario_id === scenarioId)!,
    createBacktest: async () => job,
    createBatch: async () => batch,
    getBatch: async () => batch,
    listBacktests: async () => [job],
    getBacktest: async () => job,
    getReport: async () => report,
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('EA Quant local Web backtests', () => {
  it('builds and submits a bounded backend-driven batch', async () => {
    const user = userEvent.setup()
    const requests: unknown[] = []
    const batchApi = {
      ...adapter(),
      createBatch: async (request: unknown) => { requests.push(request); return batch },
      getBatch: async () => batch,
    }
    window.history.pushState({}, '', '/batches/new')
    render(<App api={batchApi} />)

    expect(await screen.findByRole('heading', { name: 'New Experiment Batch' })).toBeTruthy()
    expect(screen.getAllByRole('heading', { name: /Run [12]/ })).toHaveLength(2)
    expect(screen.getByLabelText('Run 1 entry delay bars').getAttribute('max')).toBe('2')
    await user.click(screen.getByRole('button', { name: 'Add configuration' }))
    expect(screen.getByRole('heading', { name: 'Run 3' })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Remove Run 3' }))
    expect(screen.queryByRole('heading', { name: 'Run 3' })).toBeNull()

    await user.clear(screen.getByLabelText('Run 2 quantity'))
    await user.type(screen.getByLabelText('Run 2 quantity'), '4')
    await user.clear(screen.getByLabelText('Run 2 entry delay bars'))
    await user.type(screen.getByLabelText('Run 2 entry delay bars'), '2')
    await user.click(screen.getByRole('button', { name: 'Run batch' }))

    expect(requests).toEqual([{
      scenario_id: 'bounded-long.yaml', input_identity: identity, initial_cash: '10000',
      runs: [
        { strategy_parameters: { target_quantity: '2', entry_delay_bars: 0 } },
        { strategy_parameters: { target_quantity: '4', entry_delay_bars: 2 } },
      ],
    }])
    expect(await screen.findByRole('heading', { name: 'Experiment Batch' })).toBeTruthy()
  })

  it('keeps mixed batch members readable and reuses pair comparison navigation', async () => {
    const user = userEvent.setup()
    const batchApi = {
      ...adapter({
        getBacktest: async (jobId) => jobId === 'job-1' ? job : rejectedJob,
        getReport: async () => report,
      }),
      createBatch: async () => batch,
      getBatch: async () => batch,
    }
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={batchApi} />)

    expect(await screen.findByRole('heading', { name: 'Experiment Batch' })).toBeTruthy()
    expect(screen.getByText('risk.rejected')).toBeTruthy()
    expect(screen.getByRole('row', { name: /Run 1 2 0 succeeded 10017 USD 17 USD 0.17% View/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Run 2 2 0 risk.rejected No report View/i })).toBeTruthy()
    await user.click(screen.getByLabelText('Select job-1 for comparison'))
    await user.click(screen.getByLabelText('Select job-risk for comparison'))
    await user.click(screen.getByRole('button', { name: 'Compare selected runs' }))

    expect(await screen.findByRole('heading', { name: 'Compare Backtests' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Parameter Delta' })).toBeTruthy()
    expect(screen.getAllByText('No report').length).toBeGreaterThan(0)
  })

  it('sorts report values exactly with stable ties and missing values last in both directions', async () => {
    const user = userEvent.setup()
    const analysisBatch = {
      ...batch,
      member_job_ids: ['job-1', 'job-2', 'job-3', 'job-risk'],
      member_count: 4,
      members: [
        { ...job, presentation_status: 'succeeded' },
        { ...secondJob, presentation_status: 'succeeded' },
        { ...thirdJob, presentation_status: 'succeeded' },
        { ...rejectedJob, presentation_status: 'risk.rejected' },
      ],
    }
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={adapter({
      getBatch: async () => analysisBatch,
      getReport: async (jobId) => jobId === 'job-1' ? report : jobId === 'job-2' ? secondReport : thirdReport,
    })} />)

    await screen.findByRole('row', { name: /Run 3 3 1 succeeded/i })
    await user.selectOptions(screen.getByLabelText('Sort by'), 'net_pnl')
    await user.selectOptions(screen.getByLabelText('Direction'), 'descending')
    await waitFor(() => expect(screen.getAllByRole('row').slice(1).map((row) => row.textContent)).toEqual([
      expect.stringContaining('Run 2'),
      expect.stringContaining('Run 3'),
      expect.stringContaining('Run 1'),
      expect.stringContaining('Run 4'),
    ]))

    await user.selectOptions(screen.getByLabelText('Direction'), 'ascending')
    await waitFor(() => expect(screen.getAllByRole('row').slice(1).map((row) => row.textContent)).toEqual([
      expect.stringContaining('Run 1'),
      expect.stringContaining('Run 2'),
      expect.stringContaining('Run 3'),
      expect.stringContaining('Run 4'),
    ]))
  })

  it('sorts canonical parameter values ascending and descending', async () => {
    const user = userEvent.setup()
    const analysisBatch = {
      ...batch,
      member_job_ids: ['job-2', 'job-1', 'job-3'],
      member_count: 3,
      members: [
        { ...secondJob, presentation_status: 'succeeded' },
        { ...job, presentation_status: 'succeeded' },
        { ...thirdJob, presentation_status: 'succeeded' },
      ],
    }
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={adapter({
      getBatch: async () => analysisBatch,
      getReport: async (jobId) => jobId === 'job-1' ? report : jobId === 'job-2' ? secondReport : thirdReport,
    })} />)

    await screen.findByRole('row', { name: /Run 3 3 1 succeeded/i })
    await user.selectOptions(screen.getByLabelText('Sort by'), 'target_quantity')
    await waitFor(() => expect(screen.getAllByRole('row').slice(1).map((row) => row.textContent)).toEqual([
      expect.stringContaining('Run 2'),
      expect.stringContaining('Run 3'),
      expect.stringContaining('Run 1'),
    ]))

    await user.selectOptions(screen.getByLabelText('Direction'), 'descending')
    await waitFor(() => expect(screen.getAllByRole('row').slice(1).map((row) => row.textContent)).toEqual([
      expect.stringContaining('Run 1'),
      expect.stringContaining('Run 3'),
      expect.stringContaining('Run 2'),
    ]))
  })

  it('filters every mixed presentation state and restores all members', async () => {
    const user = userEvent.setup()
    const failedJob: BacktestJob = {
      ...thirdJob, job_id: 'job-failed', request_id: 'request-failed', status: 'failed',
      report_ready: false, report_sha256: null, summary_sha256: null, error_code: 'execution.failed',
    }
    const runningJob: BacktestJob = {
      ...thirdJob, job_id: 'job-running', request_id: 'request-running', status: 'running',
      report_ready: false, report_sha256: null, summary_sha256: null,
    }
    const queuedJob: BacktestJob = {
      ...thirdJob, job_id: 'job-queued', request_id: 'request-queued', status: 'accepted',
      report_ready: false, report_sha256: null, summary_sha256: null,
    }
    const mixedBatch = {
      ...batch,
      member_job_ids: ['job-1', 'job-risk', 'job-failed', 'job-running', 'job-queued'],
      member_count: 5,
      status: 'running' as const,
      members: [
        { ...job, presentation_status: 'succeeded' },
        { ...rejectedJob, presentation_status: 'risk.rejected' },
        { ...failedJob, presentation_status: 'failed' },
        { ...runningJob, presentation_status: 'running' },
        { ...queuedJob, presentation_status: 'queued' },
      ],
    }
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={adapter({ getBatch: async () => mixedBatch })} />)

    await screen.findByLabelText('Select job-queued for comparison')
    for (const [filter, visibleJob] of [
      ['succeeded', 'job-1'],
      ['risk.rejected', 'job-risk'],
      ['failed', 'job-failed'],
      ['running', 'job-running'],
      ['queued', 'job-queued'],
    ]) {
      await user.selectOptions(screen.getByLabelText('Status filter'), filter)
      expect(screen.getAllByRole('row')).toHaveLength(2)
      expect(screen.getByLabelText(`Select ${visibleJob} for comparison`)).toBeTruthy()
      if (filter !== 'succeeded') expect(screen.getByText('No report')).toBeTruthy()
    }

    await user.selectOptions(screen.getByLabelText('Status filter'), 'all')
    expect(screen.getAllByRole('row')).toHaveLength(6)
  })

  it('shows the three existing report metrics with currencies and a derived analysis summary', async () => {
    const euroReport: BacktestReport = {
      ...secondReport,
      economics: {
        ...secondReport.economics,
        currency: 'EUR',
        equity: { amount: '10034', currency: 'EUR' },
        net_pnl: { amount: '34', currency: 'EUR' },
      },
    }
    const analysisBatch = {
      ...batch,
      member_job_ids: ['job-1', 'job-2', 'job-risk'],
      member_count: 3,
      members: [
        { ...job, presentation_status: 'succeeded' },
        { ...secondJob, presentation_status: 'succeeded' },
        { ...rejectedJob, presentation_status: 'risk.rejected' },
      ],
    }
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={adapter({
      getBatch: async () => analysisBatch,
      getReport: async (jobId) => jobId === 'job-1' ? report : euroReport,
    })} />)

    expect(await screen.findByRole('columnheader', { name: 'Final equity' })).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Net P&L' })).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Total return' })).toBeTruthy()
    expect(await screen.findByRole('row', { name: /Run 1 2 0 succeeded 10017 USD 17 USD 0.17% View/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Run 2 4 2 succeeded 10034 EUR 34 EUR 0.34% View/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Run 3 2 0 risk.rejected No report View/i })).toBeTruthy()
    const summary = within(screen.getByRole('region', { name: 'Analysis summary' }))
    expect(summary.getByText('3', { selector: 'dd[data-summary="total"]' })).toBeTruthy()
    expect(summary.getByText('2', { selector: 'dd[data-summary="succeeded"]' })).toBeTruthy()
    expect(summary.getByText('1', { selector: 'dd[data-summary="risk.rejected"]' })).toBeTruthy()
    expect(summary.getByText('2', { selector: 'dd[data-summary="reports"]' })).toBeTruthy()
  })

  it('keeps a succeeded member readable when its verified report is unavailable', async () => {
    window.history.pushState({}, '', '/batches/batch-1')
    render(<App api={adapter({
      getBatch: async () => ({
        ...batch,
        member_job_ids: ['job-1'],
        member_count: 1,
        members: [{ ...job, presentation_status: 'succeeded' }],
      }),
      getReport: async () => { throw Object.assign(new Error('verified report is unavailable'), { code: 'report_unavailable' }) },
    })} />)

    expect(await screen.findByRole('row', { name: /Run 1 2 0 succeeded No report View/i })).toBeTruthy()
    expect(screen.queryByText('verified report is unavailable')).toBeNull()
    expect(screen.getByText('0', { selector: 'dd[data-summary="reports"]' })).toBeTruthy()
  })

  it('validates selected real input, clears stale validation, and opens the accepted job', async () => {
    const user = userEvent.setup()
    window.history.pushState({}, '', '/backtests')
    render(<App api={adapter()} />)

    expect(await screen.findByRole('heading', { name: 'Offline Backtests' })).toBeTruthy()
    await user.selectOptions(screen.getByLabelText('Scenario'), 'bounded-long.yaml')
    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    expect(await screen.findByText('Validated input')).toBeTruthy()
    expect(screen.getByText(/Target quantity: 2 · Entry delay bars: 0/)).toBeTruthy()

    await user.selectOptions(screen.getByLabelText('Scenario'), 'flat.yaml')
    expect(screen.queryByText('Validated input')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    await user.click(await screen.findByRole('button', { name: 'Run new backtest' }))

    expect(await screen.findByRole('heading', { name: 'Backtest Result' })).toBeTruthy()
    expect(screen.getByText('run-1')).toBeTruthy()
  })

  it('restores immutable parameters, revalidates edits, and submits a new run', async () => {
    const user = userEvent.setup()
    const validations: { scenarioId: string; initial_cash: string; strategy_parameters: { target_quantity: string | null; entry_delay_bars: number } | null }[] = []
    const creations: { parameters: { initial_cash: string; strategy_parameters: { target_quantity: string | null; entry_delay_bars: number } | null } }[] = []
    window.history.pushState({}, '', '/backtests')
    render(<App api={adapter({
      listBacktests: async () => [job],
      validateScenario: async (scenarioId, parameters) => {
        validations.push({ scenarioId, ...parameters })
        return scenarios[0]
      },
      createBacktest: async (request) => {
        creations.push({ parameters: request.parameters })
        return secondJob
      },
      getBacktest: async () => secondJob,
      getReport: async () => secondReport,
    })} />)

    await user.click(await screen.findByRole('button', { name: 'Use parameters for job-1' }))
    expect((screen.getByLabelText('Initial cash') as HTMLInputElement).value).toBe('10000')
    expect((screen.getByLabelText('Quantity') as HTMLInputElement).value).toBe('2')
    expect((screen.getByLabelText('Entry delay bars') as HTMLInputElement).value).toBe('0')
    expect(screen.getByLabelText('Entry delay bars').getAttribute('max')).toBe('2')
    expect((screen.getByLabelText('Symbol') as HTMLInputElement).value).toBe('AAPL')
    expect((screen.getByLabelText('Symbol') as HTMLInputElement).readOnly).toBe(true)

    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    expect(await screen.findByText('Validated input')).toBeTruthy()
    await user.clear(screen.getByLabelText('Quantity'))
    await user.type(screen.getByLabelText('Quantity'), '4')
    await user.clear(screen.getByLabelText('Entry delay bars'))
    await user.type(screen.getByLabelText('Entry delay bars'), '2')
    expect(screen.queryByText('Validated input')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    await user.click(await screen.findByRole('button', { name: 'Run new backtest' }))

    expect(validations.at(-1)).toEqual({
      scenarioId: 'bounded-long.yaml', initial_cash: '10000',
      strategy_parameters: { target_quantity: '4', entry_delay_bars: 2 },
    })
    expect(creations).toEqual([{ parameters: {
      initial_cash: '10000', strategy_parameters: { target_quantity: '4', entry_delay_bars: 2 },
    } }])
    expect(await screen.findByText('run-2')).toBeTruthy()
  })

  it('compares two successful persisted runs with exact input and result deltas', async () => {
    window.history.pushState({}, '', '/backtests/compare/job-1/job-2')
    render(<App api={adapter({
      getBacktest: async (jobId) => jobId === 'job-1' ? job : secondJob,
      getReport: async (jobId) => jobId === 'job-1' ? report : secondReport,
    })} />)

    expect(await screen.findByRole('heading', { name: 'Compare Backtests' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Parameter Delta' })).toBeTruthy()
    expect(screen.getByRole('row', { name: /target_quantity 2 4 Changed/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /entry_delay_bars 0 2 Changed/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Final Equity 10017 USD 10034 USD \+17 USD/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Net P&L 17 USD 34 USD \+17 USD/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Return 0.17% 0.34% \+0.17 pp/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Orders 1 1 0/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Fills 1 1 0/i })).toBeTruthy()
  })

  it('does not subtract monetary results with different settlement currencies', async () => {
    const euroJob: BacktestJob = {
      ...secondJob,
      input_snapshot: {
        ...secondJob.input_snapshot!,
        scenario: {
          ...secondJob.input_snapshot!.scenario,
          funding: { ...secondJob.input_snapshot!.scenario.funding, currency: 'EUR' },
        },
      },
    }
    const euroReport: BacktestReport = {
      ...secondReport,
      economics: {
        ...secondReport.economics,
        currency: 'EUR',
        initial_funding: { amount: '10000', currency: 'EUR' },
        ending_cash: [{ amount: '9594', currency: 'EUR' }],
        equity: { amount: '10034', currency: 'EUR' },
        net_pnl: { amount: '34', currency: 'EUR' },
      },
    }
    window.history.pushState({}, '', '/backtests/compare/job-1/job-2')
    render(<App api={adapter({
      getBacktest: async (jobId) => jobId === 'job-1' ? job : euroJob,
      getReport: async (jobId) => jobId === 'job-1' ? report : euroReport,
    })} />)

    expect(await screen.findByRole('heading', { name: 'Compare Backtests' })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Final Equity 10017 USD 10034 EUR Different currencies/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Net P&L 17 USD 34 EUR Different currencies/i })).toBeTruthy()
  })

  it('does not subtract monetary results when settlement currencies are unavailable', async () => {
    const leftReport = structuredClone(report)
    const rightReport = structuredClone(secondReport)
    delete leftReport.economics.currency
    delete leftReport.economics.equity.currency
    delete leftReport.economics.net_pnl.currency
    delete rightReport.economics.currency
    delete rightReport.economics.equity.currency
    delete rightReport.economics.net_pnl.currency
    window.history.pushState({}, '', '/backtests/compare/job-1/job-2')
    render(<App api={adapter({
      getBacktest: async (jobId) => jobId === 'job-1' ? job : secondJob,
      getReport: async (jobId) => jobId === 'job-1' ? leftReport : rightReport,
    })} />)

    expect(await screen.findByRole('heading', { name: 'Compare Backtests' })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Final Equity 10017 10034 Currency unavailable/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /Net P&L 17 34 Currency unavailable/i })).toBeTruthy()
  })

  it('compares success with risk rejection without fabricating metrics or deltas', async () => {
    window.history.pushState({}, '', '/backtests/compare/job-1/job-risk')
    let reportRequests = 0
    render(<App api={adapter({
      getBacktest: async (jobId) => jobId === 'job-1' ? job : rejectedJob,
      getReport: async () => { reportRequests += 1; return report },
    })} />)

    expect(await screen.findByText('risk.rejected')).toBeTruthy()
    expect(screen.getAllByText('No report').length).toBeGreaterThan(0)
    expect(screen.getByRole('row', { name: /Final Equity 10017 USD No report —/i })).toBeTruthy()
    expect(reportRequests).toBe(1)
  })

  it('keeps comparison available when one persisted report is unavailable', async () => {
    window.history.pushState({}, '', '/backtests/compare/job-1/job-2')
    render(<App api={adapter({
      getBacktest: async (jobId) => jobId === 'job-1' ? job : secondJob,
      getReport: async (jobId) => {
        if (jobId === 'job-1') throw Object.assign(new Error('verified report is unavailable'), { code: 'report_unavailable' })
        return secondReport
      },
    })} />)

    expect(await screen.findByRole('heading', { name: 'Compare Backtests' })).toBeTruthy()
    expect(screen.getByRole('row', { name: /target_quantity 2 4 Changed/i })).toBeTruthy()
    expect(screen.getByRole('row', { name: /entry_delay_bars 0 2 Changed/i })).toBeTruthy()
    expect(screen.getAllByText('No report').length).toBeGreaterThan(0)
    expect(screen.getByRole('row', { name: /Final Equity No report 10034 USD —/i })).toBeTruthy()
    expect(screen.queryByText('verified report is unavailable')).toBeNull()
  })

  it('shows formal report values without browser-side economic recomputation', async () => {
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={adapter()} />)

    expect(await screen.findByText('10017 USD')).toBeTruthy()
    ;['9797 USD', '101.5', '110', '17 USD', '0.0017', 'Orders 1', 'Fills 1'].forEach((value) => {
      expect(screen.getByText(value)).toBeTruthy()
    })
    expect(screen.getAllByText('2')).toHaveLength(2)
    expect(screen.getByRole('link', { name: 'Download report.json' }).getAttribute('href')).toBe('/api/backtests/job-1/artifacts/report.json')
    expect(screen.getByRole('link', { name: 'Download summary.txt' }).getAttribute('href')).toBe('/api/backtests/job-1/artifacts/summary.txt')
    expect(screen.getByText(/may include open-position valuation/i)).toBeTruthy()
  })

  it('reports a genuinely unreachable service and does not fall back to mock data', async () => {
    window.history.pushState({}, '', '/backtests')
    render(<App api={adapter({ listScenarios: async () => { throw new Error('offline') } })} />)

    expect(await screen.findByText('Local service is unreachable')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText(/mock data/i)).toBeNull())
  })

  it('shows a real validation failure and does not enable execution', async () => {
    const user = userEvent.setup()
    window.history.pushState({}, '', '/backtests')
    render(<App api={adapter({
      validateScenario: async () => { throw new Error('data fingerprint conflicts with selected market data') },
    })} />)

    await user.selectOptions(await screen.findByLabelText('Scenario'), 'invalid.yaml')
    await user.click(screen.getByRole('button', { name: 'Validate input' }))

    expect(await screen.findByText('data fingerprint conflicts with selected market data')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Run new backtest' }).hasAttribute('disabled')).toBe(true)
  })

  it('clears a previously loaded report and surfaces a failed refresh', async () => {
    const user = userEvent.setup()
    let reportRequests = 0
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={adapter({
      getReport: async () => {
        reportRequests += 1
        if (reportRequests === 1) return report
        throw new Error('verified report is unavailable')
      },
    })} />)

    expect(await screen.findByText('10017 USD')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByText('verified report is unavailable')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('10017 USD')).toBeNull())
  })

  it('ignores a late report response after navigating to another job', async () => {
    const user = userEvent.setup()
    let resolveFirst: ((value: BacktestReport) => void) | undefined
    const firstReport = new Promise<BacktestReport>((resolve) => { resolveFirst = resolve })
    const secondJob: BacktestJob = {
      ...job, job_id: 'job-2', request_id: 'request-2', scenario_id: 'flat.yaml', engine_run_id: 'run-2',
    }
    const secondReport: BacktestReport = {
      ...report, run_id: 'run-2', economics: { ...report.economics, equity: { amount: '20008', currency: 'USD' } },
    }
    window.history.pushState({}, '', '/backtests/job-1')
    render(<App api={adapter({
      listBacktests: async () => [job, secondJob],
      getBacktest: async (jobId) => jobId === 'job-1' ? job : secondJob,
      getReport: async (jobId) => jobId === 'job-1' ? firstReport : secondReport,
    })} />)

    expect(await screen.findByText('run-1')).toBeTruthy()
    await user.click(screen.getByRole('link', { name: '← New or saved backtest' }))
    const flatJobLink = (await screen.findByText('flat.yaml')).closest('a')
    expect(flatJobLink).toBeTruthy()
    await user.click(flatJobLink!)
    expect(await screen.findByText('20008 USD')).toBeTruthy()

    resolveFirst?.(report)
    await waitFor(() => expect(screen.queryByText('10017 USD')).toBeNull())
    expect(screen.getByText('run-2')).toBeTruthy()
    expect(screen.getByText('20008 USD')).toBeTruthy()
  })
})

it('shows persisted commission assumptions and formal report fees', async () => {
  const feeJob = { ...job, input_snapshot: { ...job.input_snapshot!, scenario: {
    ...job.input_snapshot!.scenario,
    execution: { policy: 'phase1.next-bar-close.v1', commission: { policy: 'deterministic-commission-v1', commission_bps: '100' } },
  } } }
  const feeReport = { ...report, economics: { ...report.economics,
    fees: { amount: '2.03', count: 1, currency: 'USD', rule: 'deterministic-commission-v1' },
  } }
  window.history.pushState({}, '', `/backtests/${job.job_id}`)
  render(<App api={adapter({ getBacktest: async () => feeJob, getReport: async () => feeReport })} />)
  expect(await screen.findByText('deterministic-commission-v1 · 100 bps')).toBeTruthy()
  expect(await screen.findByText('2.03 USD')).toBeTruthy()
  expect(screen.queryByLabelText('Commission bps')).toBeNull()
})
