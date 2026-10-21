import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { FrozenReportFacts, FrozenReportMetrics, FrozenReportDetails } from './frozen-report'
import type { BacktestJob, BacktestReport } from './app'
import type { EquityPath } from './equity-path'

// Display fixture copied from a local synthetic run, not evidence of market performance.
const report = { schema: 'ea.backtest-report.v3', economics: { currency: 'USD', initial_funding: { amount: '10000', currency: 'USD' }, equity: { amount: '10010.73' }, net_pnl: { amount: '10.73' }, total_return: { value: '0.001073' }, fees: { amount: '1.27' }, counts: { orders: 6, fills: 6 }, completed_round_trips: 3 } } as BacktestReport
const path = { currency: 'USD', max_drawdown: { amount: '0.22', ratio: '0.000021984237301855' } } as EquityPath
const job = { input_identity: { record_count: 12 }, parameters: {}, input_snapshot: { scenario: { instrument: { venue: 'XNAS', symbol: 'AAPL' }, data: { start_utc: '2026-01-01T00:00:00Z', end_utc: '2026-01-01T00:11:00Z' }, funding: { initial_cash: '10000', currency: 'USD' }, strategy: { parameters: {} } } } } as BacktestJob
afterEach(cleanup)
it('shows frozen results and rounded drawdown with exact values in the disclosure', () => {
  const original = JSON.stringify({ report, path, job })
  render(<><FrozenReportFacts job={job} report={report} revision={1} /><FrozenReportMetrics report={report} path={path} /><FrozenReportDetails job={job} report={report} path={path} /></>)
  const metrics = screen.getByRole('region', { name: '正式报告关键结果' })
  expect(within(metrics).getByText('10010.73 USD')).toBeTruthy()
  expect(within(metrics).getByText('10.73 USD')).toBeTruthy()
  expect(within(metrics).getByText('0.1073%')).toBeTruthy()
  expect(within(metrics).getByText('0.0022%')).toBeTruthy()
  expect(within(metrics).getByText('0.22 USD')).toBeTruthy()
  expect(screen.getByText(/12 条 · 性质未声明/)).toBeTruthy()
  fireEvent.click(screen.getByText('原始指标与冻结输入'))
  expect(screen.getByText(/0.0021984237301855%/)).toBeTruthy()
  expect(screen.getByText(/0.000021984237301855/)).toBeTruthy()
  expect(within(metrics).getByText('6 / 6')).toBeTruthy()
  expect(JSON.stringify({ report, path, job })).toBe(original)
})
it('distinguishes missing path and fees from recorded zero', () => {
  const legacy = { ...report, economics: { ...report.economics, fees: undefined, net_pnl: { amount: '0' } } } as BacktestReport
  render(<FrozenReportMetrics report={legacy} path={null} />)
  expect(screen.getAllByText('未提供')).toHaveLength(2)
  expect(screen.getByText('0.00 USD')).toBeTruthy()
})
it('does not round a nonzero tiny result to a displayed zero or lose large decimal precision', () => {
  const tiny = { ...report, economics: { ...report.economics, equity: { amount: '9007199254740993.12' }, net_pnl: { amount: '-0.000001' }, total_return: { value: '0.000000000001' } } } as BacktestReport
  render(<FrozenReportMetrics report={tiny} path={null} />)
  expect(screen.getByText('9007199254740993.12 USD')).toBeTruthy()
  expect(screen.getByText('−<0.01 USD')).toBeTruthy()
  expect(screen.getByText('<0.0001%')).toBeTruthy()
})
