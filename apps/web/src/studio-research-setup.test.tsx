import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import type { ApiAdapter, ScenarioSummary } from './app'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import { BindScenario } from './user-strategies'
import { importTarget } from './research-upload-binding'
import type { UserStrategy } from './user-strategies-api'

const intent: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'own-data', name: '检验我的数据', intent: '检验我的数据',
  created_at: '2026-11-04', updated_at: '2026-11-04', draft: { kind: 'intent', revision: 1, scenario_id: null },
  history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const long: ScenarioSummary = { scenario_id: 'template:bounded-long.yaml', name: 'bounded-long.yaml', valid: true,
  summary: { strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', record_count: 4 },
  template_assumptions: { instrument: { venue: 'XNAS', symbol: 'AAPL' }, risk: { max_order_quantity: '5' }, execution: { policy: 'phase1.next-bar-close.v1' } } }
const moving: ScenarioSummary = { ...long, scenario_id: 'template:moving-average-entry.yaml', name: 'moving-average-entry.yaml', summary: { ...long.summary!, strategy_id: 'moving-average-entry-v1' } }
const registered: ScenarioSummary = { scenario_id: 'historical-research.yaml', name: '已登记历史研究', valid: true,
  summary: { ...long.summary!, record_count: 240 } }
const bound: UserStrategy = { ...intent, draft: { revision: 2, scenario_id: long.scenario_id, strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: {}, dataset_id: 'my-published.csv', source_sha256: 'source', criterion: null } }
const ownDataset = { dataset_id: 'my-published.csv', valid: true, origin: 'upload' as const, venue: 'XNAS', symbol: 'AAPL', record_count: 120, replay_start_utc: '2025-01-01T00:00:00Z', replay_end_utc: '2025-06-30T00:00:00Z' }
function open(api: Partial<ApiAdapter> = {}, path = '/research/own-data/draft') {
  const changed = vi.fn(), failed = vi.fn()
  const adapter = { ...scriptedAdapter(), listDatasets: async () => [ownDataset], ...api }
  const view = render(<MemoryRouter initialEntries={[path]}><BindScenario presentation="studio" api={adapter} strategy={intent}
    scenarios={[registered, long, moving]} catalogReady catalogError={null} reloadCatalog={vi.fn()} changed={changed} failed={failed} /></MemoryRouter>)
  return { ...view, changed, failed }
}
afterEach(cleanup)

it('starts with own data and binds the explicit strategy, published CSV and optional criterion once', async () => {
  const bind = vi.fn(async () => bound), send = vi.fn(), launch = vi.fn()
  const { changed } = open({ bindUserStrategyScenario: bind, sendUserStrategyMessage: send, launchUserStrategyExperiment: launch,
    listDatasets: async () => [ownDataset,
      { ...ownDataset, dataset_id: 'registered.csv', origin: 'registered' },
      { ...ownDataset, dataset_id: 'other-symbol.csv', symbol: 'MSFT' }] })
  expect(screen.getByRole('button', { name: '使用自己的 CSV' }).getAttribute('aria-pressed')).toBe('true')
  expect(screen.queryByRole('checkbox')).toBeNull()
  expect(screen.queryByLabelText('已登记输入')).toBeNull()
  await userEvent.click(screen.getByRole('radio', { name: /延迟后买入一次/ }))
  const data = await screen.findByLabelText('已发布的 CSV 数据')
  expect(within(data).queryByRole('option', { name: /registered.csv|other-symbol.csv/ })).toBeNull()
  expect((screen.getByRole('button', { name: '使用此策略与数据' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.selectOptions(data, 'my-published.csv')
  const optional = screen.getByText('如何判断结果（可选）').closest('details')!
  expect(optional.open).toBe(false)
  await userEvent.click(screen.getByText('如何判断结果（可选）'))
  await userEvent.selectOptions(screen.getByLabelText('准则字段'), 'economics.net_pnl.amount')
  await userEvent.selectOptions(screen.getByLabelText('预期方向'), 'positive')
  expect(bind).not.toHaveBeenCalled()
  expect(screen.getByText(/更换策略需新建研究；参数和数据之后可调整/)).toBeTruthy()
  const button = screen.getByRole('button', { name: '使用此策略与数据' })
  fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(changed).toHaveBeenCalledWith(bound))
  expect(bind).toHaveBeenCalledExactlyOnceWith('own-data', {
    operation_id: 'bind-own-data-1-0', base_revision: 1, scenario_id: long.scenario_id,
    dataset_id: 'my-published.csv', criterion: { field: 'economics.net_pnl.amount', expectation: 'positive' },
  })
  expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('takes an empty own-data list to CSV import and restores the strategy through the existing return context', async () => {
  const bind = vi.fn(async () => bound)
  const first = open({ bindUserStrategyScenario: bind, listDatasets: async () => [] })
  await userEvent.click(screen.getByRole('radio', { name: /快均线高于慢均线时买入一次/ }))
  expect(await screen.findByText(/没有可用于 XNAS:AAPL 的已发布 CSV/)).toBeTruthy()
  const href = screen.getByRole('link', { name: '导入 CSV' }).getAttribute('href')!
  const url = new URL(href, 'http://localhost')
  expect(url.pathname).toBe('/resources/data'); expect(url.hash).toBe('#data-upload')
  const target = importTarget(url.search)!
  expect(target).toEqual({ id: 'own-data', revision: 1, returnTo: `/strategies/own-data?tab=strategy&setup_scenario=${encodeURIComponent(moving.scenario_id)}` })
  expect(bind).not.toHaveBeenCalled()
  first.unmount()
  // The existing studio legacy redirect carries this return query to the draft route.
  open({ bindUserStrategyScenario: bind }, target.returnTo.replace('/strategies/own-data', '/research/own-data/draft'))
  expect((screen.getByRole('radio', { name: /快均线高于慢均线时买入一次/ }) as HTMLInputElement).checked).toBe(true)
  await userEvent.selectOptions(await screen.findByLabelText('已发布的 CSV 数据'), ownDataset.dataset_id)
  expect(bind).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '使用此策略与数据' }))
  expect(bind).toHaveBeenCalledExactlyOnceWith('own-data', expect.objectContaining({ scenario_id: moving.scenario_id, dataset_id: ownDataset.dataset_id }))
})

it('keeps registered inputs secondary and describes their catalog identity without claiming they are synthetic', async () => {
  const bind = vi.fn(async () => bound)
  open({ bindUserStrategyScenario: bind })
  await userEvent.click(screen.getByRole('button', { name: '已登记输入' }))
  await userEvent.selectOptions(screen.getByLabelText('已登记输入'), registered.scenario_id)
  const summary = screen.getByRole('region', { name: '选择摘要' })
  expect(summary.textContent).toContain('已登记历史研究')
  expect(summary.textContent).toContain('240')
  expect(summary.textContent).not.toMatch(/合成|演示数据/)
  expect(screen.queryByRole('checkbox')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '使用此策略与数据' }))
  expect(bind).toHaveBeenCalledExactlyOnceWith('own-data', { operation_id: 'bind-own-data-1-0', base_revision: 1, scenario_id: registered.scenario_id })
})
