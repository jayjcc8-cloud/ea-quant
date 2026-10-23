import { researchCollection } from '../preview/research-fixtures'
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EquityCurve, type EquityPath } from './equity-path'
import { ResearchUploadBinding } from './research-upload-binding'
import { BrowserRouter } from 'react-router-dom'
import { App } from './app'
import { scriptedAdapter as baseAdapter } from '../preview/first-use-fixtures'
import type { UserStrategy } from './user-strategies-api'
const record: UserStrategy = { schema: 'ea.user-strategy.v1', user_strategy_id: 'us-1', name: '均线研究', intent: '合成样本研究', created_at: '2026-10-14', updated_at: '2026-10-14', draft: { kind: 'intent', revision: 1, scenario_id: null }, history: [], messages: [], calls: [], proposals: [], changes: [], experiments: [] }
const scriptedAdapter = () => ({ ...baseAdapter(), listResearch: async (query?: import('./research-workbench-api').ResearchQuery) => researchCollection([record], query), getUserStrategy: async () => record, saveUserStrategyDraft: vi.fn() })
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)
it('clears the exchange selection without changing research or dispatching work', async () => {
  window.history.replaceState({}, '', '/strategies')
  const user = userEvent.setup()
  const api = scriptedAdapter()
  render(<App api={api} />)
  await screen.findByRole('link', { name: '均线研究' })
  await user.click(screen.getByRole('button', { name: 'Binance 未接入' }))
  await user.click(screen.getByRole('button', { name: '全部交易所' }))
  expect(sessionStorage.getItem('quine.platform-filter')).toBeNull()
  expect(screen.getByRole('button', { name: '全部交易所' }).getAttribute('aria-pressed')).toBe('true')
  expect(screen.getByRole('link', { name: '均线研究' })).toBeTruthy()
  expect(api.saveUserStrategyDraft).not.toHaveBeenCalled()
})

it('opens the real creation form from the assistant without creating or calling a model', async () => {
  window.history.replaceState({}, '', '/strategies')
  const user = userEvent.setup()
  const create = vi.fn()
  render(<App api={{ ...scriptedAdapter(), listResearch: async () => researchCollection([]), createUserStrategy: create }} />)
  await screen.findByText('还没有策略。')
  await user.click(within(screen.getByRole('region', { name: 'AI 协作' })).getByRole('link', { name: /梳理研究思路/ }))
  expect(await screen.findByRole('textbox', { name: '策略名称' })).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByRole('textbox', { name: '意图' }))
  expect(create).not.toHaveBeenCalled()
})

it('keeps management independent and returns to the actual report', async () => {
  window.history.replaceState({}, '', '/strategies/us-1?tab=experiments&report=trades')
  const user = userEvent.setup()
  render(<App api={scriptedAdapter()} />)
  await screen.findByRole('heading', { name: '均线研究' })
  await user.click(screen.getByRole('link', { name: '资源' }))
  expect(screen.queryByRole('navigation', { name: '主导航' })).toBeNull()
  expect(screen.getByRole('link', { name: /返回/ }).getAttribute('href')).toContain('/strategies/us-1?tab=experiments&report=trades')
  await user.click(screen.getByRole('link', { name: '设置' }))
  await user.click(screen.getByRole('link', { name: '管理模型连接' }))
  expect(screen.getByRole('link', { name: '返回设置' })).toBeTruthy()
})
it('separates exchange scope from legacy ownership and preserves list inputs', async () => {
  window.history.replaceState({}, '', '/strategies')
  const user = userEvent.setup()
  const api = scriptedAdapter()
  const write = vi.fn(api.saveUserStrategyDraft)
  render(<App api={{ ...api, saveUserStrategyDraft: write }} />)
  await screen.findByRole('link', { name: '均线研究' })
  await user.type(screen.getByRole('searchbox', { name: '搜索研究' }), '均线')
  await user.click(screen.getByRole('button', { name: 'Binance 未接入' }))
  await user.click(screen.getByRole('button', { name: '当前交易所' }))
  expect(screen.queryByRole('link', { name: '均线研究' })).toBeNull()
  await user.click(screen.getByRole('button', { name: 'OKX 未接入' }))
  await user.click(screen.getByRole('button', { name: '全部研究' }))
  await user.click(screen.getByRole('link', { name: '均线研究' }))
  expect(await screen.findByText('未指定交易所')).toBeTruthy()
  await user.click(screen.getByRole('link', { name: '返回研究列表' }))
  expect((screen.getByRole('searchbox', { name: '搜索研究' }) as HTMLInputElement).value).toBe('均线')
  expect(write).not.toHaveBeenCalled()
})
it('separates appearance and workspace forms', async () => {
  window.history.replaceState({}, '', '/settings')
  const user = userEvent.setup()
  render(<App api={scriptedAdapter()} />)
  expect(await screen.findByRole('heading', { name: '设置' })).toBeTruthy()
  expect(screen.queryByLabelText('工作区绝对路径')).toBeNull()
  await user.click(screen.getByRole('link', { name: '工作区' }))
  expect(screen.queryByLabelText('主题')).toBeNull()
})
it('keeps live preview local and closing does not confirm a stop', async () => {
  window.history.replaceState({}, '', '/live')
  const user = userEvent.setup()
  render(<App api={scriptedAdapter()} />)
  await user.click(screen.getByRole('link', { name: '查看演示详情' }))
  await user.click(screen.getByRole('button', { name: '预览停止流程' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('实盘预览 · 未接入 · 全部为演示数据')).toBeTruthy()
  await user.click(within(dialog).getByRole('button', { name: '结果未知' }))
  await user.click(within(dialog).getByRole('button', { name: '返回详情' }))
  expect(screen.getByText('结果未知 · 等待原请求查询')).toBeTruthy()
  await user.click(screen.getByRole('button', { name: '查询原请求（预览）' }))
  expect(screen.getByText(/原请求 DEMO-042/)).toBeTruthy()
  expect(screen.getByRole('heading', { name: '当前持仓（演示）' })).toBeTruthy()
})

it('shows exact recorded curve points while zoom leaves formal drawdown unchanged', async () => {
  const points = [0, 1, 2, 3].map(index => ({ index, time: `2026-01-02T09:${30 + index}:00Z`, equity: ['10000', '10002.5', '9999.9', '10001'][index] }))
  const analysis = { display_points: points, point_count: 4, currency: 'USD', max_drawdown: { amount: '2.6', ratio: '0.00026', peak_time: points[1].time, trough_time: points[2].time, peak_equity: '10002.5', trough_equity: '9999.9' } } as EquityPath
  render(<EquityCurve analysis={analysis} />)
  fireEvent.change(screen.getByRole('slider', { name: '曲线观测点' }), { target: { value: '2' } })
  expect(screen.getByRole('status').textContent).toContain('9999.9 USD')
  fireEvent.change(screen.getByLabelText('曲线显示区间'), { target: { value: 'recent' } })
  expect(screen.getByRole('status').textContent).toContain('9999.9 USD')
  expect(screen.getByText('0.026% / 0.00026')).toBeTruthy()
})
it('offers a real research target and prevents a known incompatible symbol binding', async () => {
  const user = userEvent.setup()
  const target = { ...record, draft: { revision: 1, scenario_id: 'bounded-long.yaml', strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', dataset_id: null, source_sha256: null, initial_cash: '10000', parameters: {}, criterion: null } } as UserStrategy
  const select = vi.fn()
  const api = { ...scriptedAdapter(), getUserStrategy: async () => target, listResearch: async () => researchCollection([target]), listDatasets: async () => [{ dataset_id: 'btc', venue: 'TEST', symbol: 'BTC' }], selectUserStrategyDataset: select } as unknown as Parameters<typeof ResearchUploadBinding>[0]['api']
  render(<BrowserRouter><ResearchUploadBinding api={api} target={null} datasetId="btc" /></BrowserRouter>)
  await screen.findByRole('option', { name: /均线研究/ })
  await user.selectOptions(screen.getByLabelText('目标研究'), 'us-1')
  expect(await screen.findByText(/标的不兼容/)).toBeTruthy()
  expect((screen.getByRole('button', { name: '用于当前研究' }) as HTMLButtonElement).disabled).toBe(true)
  expect(select).not.toHaveBeenCalled()
})

it('restores the initial all-research entry when browser history removes the filter query', async () => {
  window.history.replaceState({}, '', '/strategies')
  const user = userEvent.setup()
  render(<App api={scriptedAdapter()} />)
  await screen.findByRole('link', { name: '均线研究' })
  await user.click(screen.getByRole('button', { name: 'Binance 未接入' }))
  await user.click(screen.getByRole('button', { name: '当前交易所' }))
  await waitFor(() => expect(screen.queryByRole('link', { name: '均线研究' })).toBeNull())
  await act(async () => { window.history.back() })
  expect(await screen.findByRole('link', { name: '均线研究' })).toBeTruthy()
  expect(window.location.search).toBe('')
})

it('restores filters, selected row and the same AI object after returning from detail', async () => {
  window.history.replaceState({}, '', '/strategies?scope=all&search=均线&sort=name')
  const user = userEvent.setup()
  render(<App api={scriptedAdapter()} />)
  await user.click(await screen.findByRole('link', { name: '均线研究' }))
  await screen.findByRole('heading', { name: '均线研究' })
  await user.click(screen.getByRole('link', { name: '返回研究列表' }))
  expect((screen.getByRole('searchbox', { name: '搜索研究' }) as HTMLInputElement).value).toBe('均线')
  expect((await screen.findByRole('link', { name: '均线研究' })).getAttribute('aria-current')).toBe('page')
  expect(within(screen.getByRole('region', { name: 'AI 协作' })).getByText('均线研究', { exact: true })).toBeTruthy()
})

it('keeps one creation entry and reveals the real search from empty AI', async () => {
  window.history.replaceState({}, '', '/strategies')
  const api = { ...scriptedAdapter(), listResearch: async () => researchCollection([]), createUserStrategy: vi.fn(), askUserStrategy: vi.fn(), launchUserStrategyExperiment: vi.fn() }
  render(<App api={api} />)
  const ai = await screen.findByRole('region', { name: 'AI 协作' })
  expect(document.querySelector('[data-ai-dock="split"]')).toBeTruthy()
  await userEvent.click(within(ai).getByRole('button', { name: '选择研究' }))
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('searchbox', { name: '搜索研究' })))
  expect(screen.getAllByRole('button', { name: '新建研究' })).toHaveLength(1)
  expect(api.createUserStrategy).not.toHaveBeenCalled()
  expect(api.askUserStrategy).not.toHaveBeenCalled()
  expect(api.launchUserStrategyExperiment).not.toHaveBeenCalled()
})

it('selects a research beside the list without navigation or any model or experiment write', async () => {
  window.history.replaceState({}, '', '/strategies')
  const other = { ...record, user_strategy_id: 'us-2', name: '第二项研究', updated_at: '2026-10-15' }
  const api = { ...scriptedAdapter(), listResearch: async () => researchCollection([record, other]), getUserStrategy: async (id: string) => id === other.user_strategy_id ? other : record, askUserStrategy: vi.fn(), launchUserStrategyExperiment: vi.fn() }
  render(<App api={api} />)
  await screen.findByRole('link', { name: '第二项研究' })
  expect(within(screen.getByRole('region', { name: 'AI 协作' })).getByText('研究工作区')).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: '选择研究 第二项研究' }))
  await waitFor(() => expect(within(screen.getByRole('region', { name: 'AI 协作' })).getByText('第二项研究', { exact: true })).toBeTruthy())
  await userEvent.click(screen.getByRole('button', { name: '选择研究 均线研究' }))
  await waitFor(() => expect(within(screen.getByRole('region', { name: 'AI 协作' })).getByText('均线研究', { exact: true })).toBeTruthy())
  expect(screen.getByRole('button', { name: '选择研究 均线研究' }).getAttribute('aria-pressed')).toBe('true')
  expect(window.location.pathname).toBe('/strategies')
  expect(api.askUserStrategy).not.toHaveBeenCalled()
  expect(api.launchUserStrategyExperiment).not.toHaveBeenCalled()
  expect(api.saveUserStrategyDraft).not.toHaveBeenCalled()
})
