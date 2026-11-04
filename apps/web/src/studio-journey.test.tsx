import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ApiAdapter } from './app'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import { StudioRoutes } from './studio-real'
import type { ResearchSummary } from './research-workbench-api'
import type { UserStrategy, UserStrategyExperiment } from './user-strategies-api'

const strategy: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'journey', name: '研究条目', intent: '验证成本假设',
  created_at: '2026-11-04', updated_at: '2026-11-04',
  draft: { revision: 4, scenario_id: 'sample.yaml', strategy_id: 'moving-average-entry-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { fast_window: 25, slow_window: 60 }, dataset_id: null, source_sha256: null, criterion: null },
  history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const summary: ResearchSummary = {
  user_strategy_id: strategy.user_strategy_id, name: strategy.name, intent: strategy.intent,
  exchange_id: null, updated_at: strategy.updated_at, instrument: { venue: 'XNAS', symbol: 'AAPL' },
  draft: { kind: 'configured', revision: 4, pending_proposals: 0 }, experiment_count: 0,
  latest_experiment: null, next_step: 'experiment',
}
const latest: NonNullable<ResearchSummary['latest_experiment']> = {
  launch_intent_id: 'original', job_id: 'frozen-report', draft_revision: 3,
  launch_status: 'created', job_status: 'succeeded', report_ready: true, read_error: null,
}
function open(doc: UserStrategy, path: string, overrides: Partial<ApiAdapter> = {}) {
  const api: ApiAdapter = { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }),
    getUserStrategy: async () => structuredClone(doc), listScenarios: async () => [],
    ...overrides }
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it.each([
  ['unbound', { draft: { kind: 'intent' as const, revision: 1, pending_proposals: 0 }, next_step: 'draft' as const }, '绑定策略与数据', '/research/journey/draft'],
  ['configured', {}, '准备一次实验', '/research/journey/confirm'],
  ['proposal', { draft: { ...summary.draft, pending_proposals: 2 }, next_step: 'review' as const }, '审阅待处理提案', '/research/journey/proposal'],
  ['report', { latest_experiment: latest, next_step: 'evidence' as const }, '阅读冻结报告', '/report/frozen-report/overview'],
  ['running', { latest_experiment: { ...latest, launch_status: 'running', job_status: 'running', report_ready: false }, next_step: 'inspect' as const }, '查看运行进度', '/research/journey/experiments'],
  ['unknown despite stale ready flag', { latest_experiment: { ...latest, launch_status: 'unknown' }, next_step: 'evidence' as const }, '核对原实验', '/research/journey/experiments'],
  ['unreadable report', { latest_experiment: { ...latest, read_error: '报告不可读' }, next_step: 'evidence' as const }, '核对原实验', '/research/journey/experiments'],
])('offers the saved %s research next step without sending work', async (_name, patch, label, href) => {
  const launch = vi.fn(), send = vi.fn()
  open(strategy, '/', { listResearch: async () => ({ items: [{ ...summary, ...patch }], total: 1, matched: 1 }), launchUserStrategyExperiment: launch, sendUserStrategyMessage: send })
  const home = screen.getByRole('heading', { name: '今天，验证什么想法？' }).parentElement!
  const row = await within(home).findByRole('link', { name: new RegExp(`研究条目.*${label}`) })
  expect(row.getAttribute('href')).toBe(href)
  if (_name === 'unknown despite stale ready flag') {
    expect(row.textContent).toContain('结果未知')
    expect(row.textContent).not.toContain('运行完成')
  }
  expect(launch).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled()
})

it('guides an unbound idea to binding and gives a direct route back from confirmation', async () => {
  const doc: UserStrategy = { ...strategy, draft: { kind: 'intent', revision: 1, scenario_id: null } }
  const preview = vi.fn(), launch = vi.fn()
  const view = open(doc, '/research/journey/overview', { previewUserStrategyExperiment: preview, launchUserStrategyExperiment: launch })
  await screen.findByRole('heading', { name: '研究条目' })
  expect(screen.queryByRole('link', { name: '准备一次实验' })).toBeNull()
  const next = screen.getByRole('region', { name: '研究下一步' })
  expect(within(next).getByRole('link', { name: '绑定策略与数据' }).getAttribute('href')).toBe('/research/journey/draft')
  expect(within(next).getByText('尚未绑定策略与数据')).toBeTruthy()
  view.unmount()
  open(doc, '/research/journey/confirm', { previewUserStrategyExperiment: preview, launchUserStrategyExperiment: launch })
  expect((await screen.findByRole('link', { name: '前往绑定策略与数据' })).getAttribute('href')).toBe('/research/journey/draft')
  expect(preview).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('opens a pending proposal from the next step and explains stale baseline, proposal and current values', async () => {
  const proposal: UserStrategy['proposals'][number] = { proposal_id: 'stale', call_id: 'call', base_revision: 3, status: 'open', created_at: '2026-11-03', applied_paths: [], stale_paths: ['parameters.fast_window'],
    items: [{ path: 'parameters.fast_window', before: 20, after: 30, reason: '模型建议', evidence: [], proposer: 'model' }] }
  const apply = vi.fn(), launch = vi.fn()
  open({ ...strategy, proposals: [proposal] }, '/research/journey/proposal', { applyUserStrategyProposal: apply, launchUserStrategyExperiment: launch })
  await screen.findByRole('heading', { name: '审阅修改提案' })
  const row = screen.getByRole('row', { name: /parameters.fast_window/ })
  expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['parameters.fast_window模型建议', '20', '30', '25'])
  expect(screen.getByRole('columnheader', { name: '提案基线 r3' })).toBeTruthy()
  expect(screen.getByRole('columnheader', { name: '当前草稿 r4' })).toBeTruthy()
  expect(screen.getByText(/草稿已从 r3 更新至 r4/)).toBeTruthy()
  const button = screen.getByRole('button', { name: '应用修改到草稿' })
  expect((button as HTMLButtonElement).disabled).toBe(true)
  await userEvent.click(button)
  expect(apply).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('labels a completed old report separately from the current draft and offers an explicit next experiment', async () => {
  const record = { launch_intent_id: 'original', status: 'created', owner: 'quine', payload: { draft_revision: 3 }, job_id: 'old-report', report_ready: true } as UserStrategyExperiment
  open({ ...strategy, experiments: [record] }, '/research/journey/overview')
  const next = await screen.findByRole('region', { name: '研究下一步' })
  expect(within(next).getByRole('link', { name: '阅读冻结报告' }).getAttribute('href')).toBe('/report/old-report/overview')
  expect(within(next).getByText(/报告对应冻结草稿 r3；当前草稿为 r4/)).toBeTruthy()
  expect(within(next).getByRole('link', { name: '用当前草稿准备新实验' }).getAttribute('href')).toBe('/research/journey/confirm')
})

it('keeps unknown experiments inspectable instead of suggesting another launch', async () => {
  const record = { launch_intent_id: 'original', status: 'unknown', owner: 'quine', payload: { draft_revision: 4 }, job_id: null, message: '所有者尚未确认' } as UserStrategyExperiment
  const launch = vi.fn()
  open({ ...strategy, experiments: [record] }, '/research/journey/overview', { launchUserStrategyExperiment: launch })
  const next = await screen.findByRole('region', { name: '研究下一步' })
  expect(within(next).getByRole('link', { name: '核对原实验' }).getAttribute('href')).toBe('/research/journey/experiments')
  expect(within(next).getByText(/结果未知/)).toBeTruthy()
  expect(screen.queryByRole('link', { name: '准备一次实验' })).toBeNull()
  expect(launch).not.toHaveBeenCalled()
})

it('presents known cost units in confirmation while retaining the raw frozen inputs', async () => {
  const preview = {
    draft_revision: 4, owner: 'quine', scenario_id: 'sample.yaml', strategy_id: 'moving-average-entry-v1',
    venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { fast_window: 25, slow_window: 60 },
    record_count: 10, data: {}, input_identity: {}, input_sha256: 'input', preview_identity: 'preview',
    normalized: { execution: { commission: { policy: 'fixed-bps-v1', commission_bps: '10' }, slippage: { policy: 'fixed-bps-v1', slippage_bps: '5' } } },
    commission: { policy: 'fixed-bps-v1', commission_bps: '10' }, slippage: { policy: 'fixed-bps-v1', slippage_bps: '5' },
    latency: null, criterion: null, budget: { max_model_calls: 0, max_backtests: 1 },
  } as const
  open(strategy, '/research/journey/confirm', { previewUserStrategyExperiment: async () => preview })
  expect(await screen.findByText('fixed-bps-v1 · 10 bps')).toBeTruthy()
  expect(screen.getByText('5 bps adverse slippage')).toBeTruthy()
  expect(screen.getByText('Legacy latency omitted · effective 0 ms')).toBeTruthy()
  expect(screen.getByText(/每 1 bps 等于 0.01%/)).toBeTruthy()
  const details = screen.getByText('核对原始冻结输入与身份').closest('details')!
  expect(details.textContent).toContain('commission_bps')
  expect(details.textContent).toContain('执行所有者：quine')
})
