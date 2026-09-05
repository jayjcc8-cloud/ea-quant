import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type BacktestReport } from './app'

const identity = { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 4 }
const scenarios = [
  {
    scenario_id: 'bounded-long.yaml', name: 'bounded-long', valid: true as const, input_identity: identity,
    summary: { strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', target_quantity: '2', record_count: 4 },
  },
  {
    scenario_id: 'flat.yaml', name: 'flat', valid: true as const, input_identity: identity,
    summary: { strategy_id: 'always-flat-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', target_quantity: null, record_count: 4 },
  },
  { scenario_id: 'invalid.yaml', name: 'invalid', valid: false as const, error_code: 'scenario_invalid', message: 'fingerprint conflicts' },
]
const job: BacktestJob = {
  schema: 'ea.local-web-job.v1', job_id: 'job-1', request_id: 'request-1', scenario_id: 'bounded-long.yaml',
  input_identity: identity, status: 'succeeded', engine_run_id: 'run-1', report_sha256: 'c'.repeat(64),
  error_code: null, message: null, report_ready: true,
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

function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return {
    listScenarios: async () => scenarios,
    validateScenario: async (scenarioId) => scenarios.find((item) => item.scenario_id === scenarioId)!,
    createBacktest: async () => job,
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
  it('validates selected real input, clears stale validation, and opens the accepted job', async () => {
    const user = userEvent.setup()
    window.history.pushState({}, '', '/backtests')
    render(<App api={adapter()} />)

    expect(await screen.findByRole('heading', { name: 'Offline Backtests' })).toBeTruthy()
    await user.selectOptions(screen.getByLabelText('Scenario'), 'bounded-long.yaml')
    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    expect(await screen.findByText('Validated input')).toBeTruthy()
    expect(screen.getByText('Target quantity: 2')).toBeTruthy()

    await user.selectOptions(screen.getByLabelText('Scenario'), 'flat.yaml')
    expect(screen.queryByText('Validated input')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Validate input' }))
    await user.click(await screen.findByRole('button', { name: 'Run new backtest' }))

    expect(await screen.findByRole('heading', { name: 'Backtest Result' })).toBeTruthy()
    expect(screen.getByText('run-1')).toBeTruthy()
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
