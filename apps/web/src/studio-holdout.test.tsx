import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StudioRoutes } from './studio-real'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter, BacktestJob, BacktestReport, ChronologicalHoldout, ScenarioSummary } from './app'

// Scripted UI contracts only. Real chronology, frozen economics and owner
// persistence are exercised by test_web_rust_holdout.py.
const target: ScenarioSummary = {
  scenario_id: 'later.yaml', name: '后续合成数据', valid: true,
  input_identity: { scenario_sha256: 'target-scenario', data_sha256: 'target-data', record_count: 3, dataset_id: 'upload-later', source_sha256: 'target-source' },
  summary: { strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', record_count: 3,
    data: { start_utc: '2026-02-01T09:31:00Z', end_utc: '2026-02-01T09:34:00Z', fingerprint: { sha256: 'target-data', record_count: 3 } } },
}
const relation: ChronologicalHoldout = { schema: 'ea.chronological-holdout.v1', validation_id: 'relation-1', created_at: '2026-10-29T07:00:00Z', source_job_id: 'source', holdout_job_id: 'later' }
function job(id: string): BacktestJob {
  return { schema: 'ea.local-web-job.v3', job_id: id, request_id: id, scenario_id: `${id}.yaml`,
    input_identity: { scenario_sha256: `${id}-scenario`, data_sha256: `${id}-data`, record_count: 3 },
    status: 'succeeded', engine_run_id: `run-${id}`, report_ready: true, report_sha256: `report-${id}`, summary_sha256: null, error_code: null, message: null,
    parameters: { target_quantity: 2, entry_delay_bars: 0 },
    input_snapshot: { schema: 'ea.local-web-input.v1', scenario_id: `${id}.yaml`, source_identity: target.input_identity!, identity: target.input_identity!,
      scenario: { strategy: { id: 'bounded-long-v1', parameters: { target_quantity: 2, entry_delay_bars: 0 } }, funding: { currency: 'USD', initial_cash: '10000' }, instrument: { venue: 'XNAS', symbol: 'AAPL' },
        data: { start_utc: `2026-0${id === 'source' ? 1 : 2}-01T09:31:00Z`, end_utc: `2026-0${id === 'source' ? 1 : 2}-01T09:34:00Z`, fingerprint: { sha256: `${id}-data`, record_count: 3 } },
        execution: { policy: 'deterministic', commission: { policy: 'deterministic-commission-v1', commission_bps: '100' } } } },
  }
}
function report(id: string): BacktestReport {
  return { schema: 'ea.backtest-report.v1', run_id: `run-${id}`, source: { strategy: { id: 'bounded-long-v1' }, instrument: { venue: 'XNAS', symbol: 'AAPL' } },
    economics: { currency: 'USD', initial_funding: { amount: '10000' }, ending_cash: [], ending_positions: [], valuation: { price: '110', position_value: '220' }, equity: { amount: '10018' }, net_pnl: { amount: '18' }, fees: { amount: '2', currency: 'USD', count: 1, rule: 'commission' }, total_return: { value: '0.0018' }, counts: { orders: 1, fills: 1 }, execution: { order: null, fill: null } } }
}
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }), researchReferences: async () => [],
    getBacktest: async id => job(id), getReport: async id => report(id), getEquityPath: async () => { throw new Error('curve unavailable') },
    listHoldouts: async () => [], holdoutScenarios: async () => [target], getHoldout: async () => relation, createHoldout: vi.fn(async () => relation), ...overrides }
}
function open(api: ApiAdapter, path = '/holdout/new/source') {
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
async function review() {
  await screen.findByRole('option', { name: '后续合成数据' })
  await userEvent.selectOptions(screen.getByLabelText('留出输入'), 'later.yaml|upload-later')
  await userEvent.click(screen.getByRole('button', { name: '审阅冻结配置' }))
  return screen.findByRole('button', { name: '确认并运行留出' })
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it('enters from a readable frozen report and confirms the displayed identity exactly once', async () => {
  let finish!: (value: ChronologicalHoldout) => void
  const create = vi.fn(() => new Promise<ChronologicalHoldout>(resolve => { finish = resolve }))
  open(adapter({ createHoldout: create }), '/report/source/overview')
  await userEvent.click(await screen.findByRole('link', { name: '配置留出验证' }))
  const confirm = await review()
  expect(screen.getAllByText('target-scenario').length).toBeGreaterThan(0)
  expect(screen.getAllByText('target-data').length).toBeGreaterThan(0)
  expect(create).not.toHaveBeenCalled()
  fireEvent.click(confirm); fireEvent.click(confirm)
  expect(create).toHaveBeenCalledExactlyOnceWith({ source_job_id: 'source', scenario_id: 'later.yaml', input_identity: target.input_identity, dataset_id: 'upload-later', source_sha256: 'target-source' })
  finish(relation)
  expect((await screen.findByRole('link', { name: '打开来源报告' })).getAttribute('href')).toBe('/report/source/overview')
  expect((await screen.findByRole('link', { name: '打开留出报告' })).getAttribute('href')).toBe('/report/later/overview')
})

it.each(['running', 'missing', 'mismatch'])('does not prepare from a %s source report', async mode => {
  const candidates = vi.fn(async () => [target]), create = vi.fn()
  const api = adapter({ holdoutScenarios: candidates, createHoldout: create,
    getBacktest: async id => ({ ...job(id), status: mode === 'running' ? 'running' : 'succeeded', report_ready: mode !== 'missing' }),
    getReport: async id => ({ ...report(id), run_id: mode === 'mismatch' ? 'foreign' : `run-${id}` }) })
  open(api)
  await screen.findByRole('alert')
  expect(screen.queryByLabelText('留出输入')).toBeNull()
  expect(candidates).not.toHaveBeenCalled(); expect(create).not.toHaveBeenCalled()
})

it('shows only backend offers and no default target when the catalog is empty or unavailable', async () => {
  const api = adapter({ holdoutScenarios: async () => [] })
  const view = open(api)
  await screen.findByText('没有兼容且严格更晚的输入。')
  expect(screen.getByRole('button', { name: '审阅冻结配置' })).toHaveProperty('disabled', true)
  view.unmount()
  open(adapter({ holdoutScenarios: async () => { throw new Error('owner unavailable') } }))
  await screen.findByText('owner unavailable')
  expect(screen.queryByRole('button', { name: '确认并运行留出' })).toBeNull()
})

it('invalidates a rejected changed input and requires fresh selection and confirmation', async () => {
  const fresh = { ...target, input_identity: { ...target.input_identity!, scenario_sha256: 'fresh-scenario' } }
  const candidates = vi.fn().mockResolvedValueOnce([target]).mockResolvedValue([fresh])
  const create = vi.fn().mockRejectedValueOnce(Object.assign(new Error('holdout input changed'), { code: 'input_conflict' })).mockResolvedValue(relation)
  open(adapter({ holdoutScenarios: candidates, createHoldout: create }))
  await userEvent.click(await review())
  await screen.findByText(/输入已变化.*重新选择/)
  expect(screen.queryByRole('button', { name: '确认并运行留出' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '重新读取输入' }))
  await waitFor(() => expect(candidates).toHaveBeenCalledTimes(2))
  expect(screen.getByLabelText('留出输入')).toHaveProperty('value', '')
  expect(create).toHaveBeenCalledTimes(1)
  await userEvent.click(await review())
  await screen.findByRole('link', { name: '打开留出报告' })
  expect(create.mock.calls[1][0].input_identity).toEqual(fresh.input_identity)
})

it('retains an uncertain confirmation across remount and queries relationships without another create', async () => {
  const create = vi.fn().mockRejectedValue(new Error('reply lost'))
  let relations: ChronologicalHoldout[] = []
  const list = vi.fn(async () => relations)
  const api = adapter({ createHoldout: create, listHoldouts: list })
  const view = open(api)
  await userEvent.click(await review())
  await screen.findByText(/确认结果未知/)
  view.unmount(); open(api)
  await userEvent.click(await screen.findByRole('button', { name: '查询已有留出' }))
  await screen.findByText(/尚未读到关联.*不代表未执行/)
  expect(screen.queryByRole('button', { name: '确认并运行留出' })).toBeNull()
  relations = [relation]
  await userEvent.click(screen.getByRole('button', { name: '查询已有留出' }))
  await userEvent.click(await screen.findByRole('link', { name: /查看留出 relation-1/ }))
  await screen.findByRole('link', { name: '打开留出报告' })
  expect(create).toHaveBeenCalledTimes(1)
})

it('refuses dispatch when its pending confirmation cannot be retained', async () => {
  const api = adapter()
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage unavailable') })
  try {
    open(api); await userEvent.click(await review())
    await screen.findByText(/无法保留确认状态.*尚未发送/)
    expect(api.createHoldout).not.toHaveBeenCalled()
  } finally { write.mockRestore() }
})

it('reopens saved relationships from records and both reports without the legacy job list', async () => {
  const legacy = vi.fn(async () => []), create = vi.fn()
  const api = adapter({ listBacktests: legacy, createHoldout: create, listHoldouts: async () => [relation] })
  const view = open(api, '/records')
  await userEvent.click(await screen.findByRole('link', { name: /查看留出 relation-1/ }))
  await userEvent.click(await screen.findByRole('link', { name: '打开留出报告' }))
  await screen.findByRole('region', { name: '正式报告关键结果' })
  await userEvent.click(await screen.findByRole('link', { name: /查看留出 relation-1/ }))
  await userEvent.click(await screen.findByRole('link', { name: '打开来源报告' }))
  await screen.findByRole('region', { name: '正式报告关键结果' })
  view.unmount(); open(api, '/holdout/relation-1')
  await screen.findByRole('link', { name: '打开留出报告' })
  expect(legacy).not.toHaveBeenCalled(); expect(create).not.toHaveBeenCalled()
})

it('keeps an unavailable holdout report separate from a readable source', async () => {
  open(adapter({ getBacktest: async id => id === 'later' ? { ...job(id), status: 'running', report_ready: false } : job(id) }), '/holdout/relation-1')
  const source = await screen.findByRole('region', { name: '来源报告' })
  await within(source).findByRole('link', { name: '打开来源报告' })
  const later = screen.getByRole('region', { name: '留出报告' })
  await within(later).findByText(/当前的状态是 running/)
  expect(within(later).queryByRole('link', { name: '打开留出报告' })).toBeNull()
  expect(within(later).queryByRole('region', { name: '正式报告关键结果' })).toBeNull()
})
