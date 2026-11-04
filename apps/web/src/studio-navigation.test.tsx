import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter } from './app'
import type { UserStrategy } from './user-strategies-api'
import { StudioRoutes } from './studio-real'

const initial: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'research-edit', name: '成本研究', intent: '检查费用',
  created_at: '2026-11-04', updated_at: '2026-11-04',
  draft: { revision: 1, scenario_id: 'sample.yaml', strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { target_quantity: 1 }, dataset_id: null, source_sha256: null, criterion: null },
  history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
function Location() { return <output aria-label="当前位置">{useLocation().pathname}</output> }
function setup() {
  let stored = structuredClone(initial)
  const save = vi.fn<NonNullable<ApiAdapter['saveUserStrategyDraft']>>(async (_id, request) => { stored = { ...stored, changes: [{ kind: 'manual', operation_id: request.operation_id }] as UserStrategy['changes'], draft: { ...stored.draft, revision: 2, parameters: { target_quantity: 3 } } as UserStrategy['draft'] }; return stored })
  const list = vi.fn(async () => ({ items: [{ user_strategy_id: stored.user_strategy_id, name: stored.name, intent: stored.intent, exchange_id: null, updated_at: stored.updated_at, instrument: { venue: 'XNAS', symbol: 'AAPL' }, draft: { kind: 'configured' as const, revision: stored.draft.revision, pending_proposals: 0 }, experiment_count: 0, latest_experiment: null, next_step: 'experiment' as const }], total: 1, matched: 1 }))
  const api: ApiAdapter = { ...scriptedAdapter(), listResearch: list, getUserStrategy: async () => stored,
    listScenarios: async () => [], listDatasets: async () => [], saveUserStrategyDraft: save,
    previewUserStrategyExperiment: async () => ({ ...stored.draft, owner: 'quine', draft_revision: stored.draft.revision, data: {}, input_identity: {}, input_sha256: 'input', preview_identity: 'preview', budget: { max_model_calls: 0, max_backtests: 1 }, record_count: 8, normalized: {}, commission: null, slippage: null, latency: null }) as Awaited<ReturnType<NonNullable<ApiAdapter['previewUserStrategyExperiment']>>>,
    userStrategyDraftContract: async () => ({ draft_revision: stored.draft.revision, scenario_id: 'sample.yaml', source: 'registered', fields: [{ path: 'parameters.target_quantity', kind: 'integer', json_type: 'integer', current: stored.draft.revision === 1 ? 1 : 3, minimum: 0, maximum: 100, editable: true }] }),
  }
  render(<MemoryRouter initialEntries={['/research/research-edit/draft']}><StudioRoutes api={api} /><Location /></MemoryRouter>)
  return { save, list }
}
beforeEach(() => {
  sessionStorage.clear()
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('refreshes the sidebar after saving on the same route, without repeating the write', async () => {
  const { save, list } = setup()
  const nav = within(screen.getByRole('complementary', { name: '工作区导航' }))
  expect((await nav.findByRole('link', { name: /成本研究/ })).textContent).toContain('草稿 r1')
  fireEvent.change(await screen.findByLabelText('目标持仓数量'), { target: { value: '3' } })
  await userEvent.click(screen.getByRole('button', { name: '保存草稿' }))
  await waitFor(() => expect(nav.getByRole('link', { name: /成本研究/ }).textContent).toContain('草稿 r2'))
  expect(save).toHaveBeenCalledTimes(1)
  expect(list).toHaveBeenCalledTimes(2)
  expect(screen.getByLabelText('当前位置').textContent).toBe('/research/research-edit/draft')
})

it('moves from search input to its result with the keyboard and opens that research', async () => {
  setup()
  await userEvent.click(screen.getByRole('button', { name: '搜索研究' }))
  const dialog = screen.getByRole('dialog', { name: '搜索研究' })
  const input = within(dialog).getByRole('searchbox', { name: '搜索研究' })
  await userEvent.type(input, '成本')
  const result = await within(dialog).findByRole('link', { name: /成本研究/ })
  fireEvent.keyDown(input, { key: 'ArrowDown', isComposing: true })
  expect(document.activeElement).toBe(input)
  await userEvent.keyboard('{ArrowDown}')
  expect(document.activeElement).toBe(result)
  await userEvent.keyboard('{ArrowUp}')
  expect(document.activeElement).toBe(input)
  await userEvent.keyboard('{ArrowDown}{Enter}')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByLabelText('当前位置').textContent).toBe('/research/research-edit/overview')
})
