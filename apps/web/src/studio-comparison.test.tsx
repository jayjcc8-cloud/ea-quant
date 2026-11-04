import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { StudioRoutes } from './studio-real'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'

const identity = { scenario_sha256: 'scenario', data_sha256: 'data', record_count: 12 }
const job = (id: string): BacktestJob => ({
  schema: 'ea.local-web-job.v2', job_id: id, request_id: id, scenario_id: 'sample.yaml',
  input_identity: identity, status: 'succeeded', engine_run_id: `run-${id}`,
  report_sha256: `report-${id}`, summary_sha256: 'summary', error_code: null, message: null, report_ready: true,
  input_snapshot: { schema: 'ea.local-web-input.v1', scenario_id: 'sample.yaml', source_identity: identity, identity,
    scenario: { funding: { currency: 'USD', initial_cash: '1000' }, instrument: { venue: 'XNAS', symbol: 'AAPL' },
      strategy: { id: 'bounded-long-v1', parameters: { target_quantity: id === 'a' ? '1' : '2' } } } },
})
const report = (id: string): BacktestReport => ({
  schema: 'ea.backtest-report.v1', run_id: `run-${id}`,
  economics: { currency: 'USD', initial_funding: { amount: '1000', currency: 'USD' }, ending_cash: [], ending_positions: [], valuation: { price: '100', position_value: '0' },
    equity: { amount: id === 'a' ? '1000.1' : '1000.3', currency: 'USD' }, net_pnl: { amount: '0', currency: 'USD' },
    total_return: { value: '0' }, counts: { orders: 0, fills: 0 }, execution: { order: null, fill: null } },
})
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }),
    listHoldouts: async () => [], researchReferences: async () => [{ user_strategy_id: 'research', name: '成本研究',
      draft: { revision: 2 }, experiments: ['a', 'b'].map(job_id => ({ job_id, launch_intent_id: job_id, payload: { draft_revision: 1 } })) }],
    getBacktest: async id => job(id), getReport: async id => report(id), getTradeAnalytics: undefined, ...overrides }
}
function open(api: ApiAdapter, path = '/records') {
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
afterEach(cleanup)

it('selects two saved reports and compares them within v3 without dispatch', async () => {
  const launch = vi.fn(), send = vi.fn()
  open(adapter({ launchUserStrategyExperiment: launch, sendUserStrategyMessage: send }))
  await userEvent.click(await screen.findByLabelText('选择 a 进行对比'))
  expect((screen.getByRole('button', { name: '对比所选报告' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.click(screen.getByLabelText('选择 b 进行对比'))
  await userEvent.click(screen.getByRole('button', { name: '对比所选报告' }))
  const results = within(await screen.findByRole('region', { name: '结果对比' }))
  expect(results.getByText('+0.2 USD')).toBeTruthy()
  expect(screen.getByRole('link', { name: '运行 A 正式报告' }).getAttribute('href')).toBe('/report/a/overview')
  expect(screen.getByRole('link', { name: '运行 B 正式报告' }).getAttribute('href')).toBe('/report/b/overview')
  expect(screen.queryByRole('link', { name: '打开策略列表' })).toBeNull()
  expect(launch).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled()
})

it('rejects identical and mismatched run identities in the v3 route', async () => {
  const read = vi.fn(async (id: string) => job(id))
  const view = open(adapter({ getBacktest: read }), '/compare/a/a')
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('同一个运行'))
  expect(read).not.toHaveBeenCalled()
  view.unmount()
  open(adapter({ getBacktest: async () => job('different') }), '/compare/a/b')
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('identity'))
  expect(screen.queryByRole('region', { name: '结果对比' })).toBeNull()
})

it('does not select unfinished records and preserves missing metrics and different currencies', async () => {
  const view = open(adapter({ getBacktest: async id => ({ ...job(id), status: 'running', report_ready: false }) }))
  expect((await screen.findByLabelText('选择 a 进行对比') as HTMLInputElement).disabled).toBe(true)
  view.unmount()
  open(adapter({ getReport: async id => {
    const value = report(id)
    if (id === 'b') value.economics.equity.currency = 'EUR'
    return value
  } }), '/compare/a/b')
  const results = within(await screen.findByRole('region', { name: '结果对比' }))
  expect(results.getByText('Different currencies')).toBeTruthy()
  expect(results.getAllByText('No report').length).toBeGreaterThan(0)
  expect(results.queryByText('+0.2 USD')).toBeNull()
})

it.each([
  [undefined, 'Legacy latency omitted · effective 0 ms'],
  [{ policy: 'deterministic-latency-v1', latency_ms: 0 }, '0 ms execution latency'],
  [{ policy: 'deterministic-latency-v1', latency_ms: 60000 }, '60000 ms execution latency'],
] as const)('labels frozen execution latency on the actual v3 report details route: %s', async (latency, label) => {
  const frozen = job('a')
  frozen.input_snapshot!.scenario.execution = { policy: 'next-bar-open', ...(latency === undefined ? {} : { latency }) }
  const before = JSON.stringify(frozen)
  const dispatch = vi.fn()
  open(adapter({ getBacktest: async () => frozen, launchUserStrategyExperiment: dispatch }), '/report/a/inputs')
  await userEvent.click(await screen.findByText('原始指标与冻结输入'))
  expect(await screen.findByText(label)).toBeTruthy()
  expect(JSON.stringify(frozen)).toBe(before)
  expect(dispatch).not.toHaveBeenCalled()
})
