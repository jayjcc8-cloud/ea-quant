import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { StrictMode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StudioRoutes } from './studio-real'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'
import { UserStrategyRequestError, type UserStrategy, type UserStrategyPreview } from './user-strategies-api'

const strategy: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'real-idea', name: '明确保存的研究', intent: '检验成本',
  created_at: '2026-10-28', updated_at: '2026-10-28',
  draft: { revision: 1, scenario_id: 'sample.yaml', strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { target_quantity: 1 }, dataset_id: null, source_sha256: null, criterion: null },
  history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const preview: UserStrategyPreview = {
  ...strategy.draft as Exclude<UserStrategy['draft'], { kind: 'intent' }>, draft_revision: 1, owner: 'quine',
  record_count: 12, data: {}, input_identity: {}, input_sha256: 'input', preview_identity: 'verified-preview',
  normalized: { execution: { commission_bps: '1' } }, commission: null, slippage: null, latency: null,
  budget: { max_model_calls: 0, max_backtests: 1 },
}
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }),
    getUserStrategy: async () => structuredClone(strategy), listScenarios: async () => [], listDatasets: async () => [],
    previewUserStrategyExperiment: async () => preview,
    userStrategyDraftContract: async () => ({ draft_revision: 1, scenario_id: 'sample.yaml', source: 'registered', fields: [] }),
    ...overrides }
}
function open(api: ApiAdapter, path: string) {
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it('saves an idea through the real contract without a model or experiment call', async () => {
  const create = vi.fn(async () => strategy), launch = vi.fn(), message = vi.fn()
  open(adapter({ createUserStrategy: create, launchUserStrategyExperiment: launch, sendUserStrategyMessage: message }), '/')
  await userEvent.type(screen.getByLabelText('研究目标'), '检验成本')
  await userEvent.click(screen.getByRole('button', { name: '保存研究并继续' }))
  await screen.findByRole('heading', { name: strategy.name })
  expect(create).toHaveBeenCalledExactlyOnceWith({ name: '检验成本', intent: '检验成本' })
  expect(launch).not.toHaveBeenCalled(); expect(message).not.toHaveBeenCalled()
})

it('consumes one preview once and retains a lost receipt as query-only across remount', async () => {
  const launch = vi.fn().mockRejectedValue(new Error('receipt lost'))
  const api = adapter({ launchUserStrategyExperiment: launch })
  const view = open(api, '/research/real-idea/confirm')
  const confirm = await screen.findByRole('button', { name: '确认并运行一次' })
  fireEvent.click(confirm); fireEvent.click(confirm)
  await screen.findByText(/确认结果未知/)
  expect(launch).toHaveBeenCalledTimes(1)
  expect(launch.mock.calls[0][1]).toMatchObject({ base_revision: 1, preview_identity: 'verified-preview', approved_budget: { max_model_calls: 0, max_backtests: 1 } })
  view.unmount()
  open(api, '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '查询原请求' }))
  await screen.findByText(/尚未读到原请求/)
  expect(screen.queryByRole('button', { name: '确认并运行一次' })).toBeNull()
  expect(launch).toHaveBeenCalledTimes(1)
})

it('reads a server-owned unknown on reopen without offering a new confirmation', async () => {
  const launch = vi.fn(), readPreview = vi.fn()
  const unknown = { launch_intent_id: 'original', status: 'unknown', owner: 'quine', payload: { draft_revision: 1 }, job_id: null }
  const api = adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [unknown] } as UserStrategy), launchUserStrategyExperiment: launch, previewUserStrategyExperiment: readPreview })
  open(api, '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '查询原请求' }))
  expect(screen.getByRole('alert').textContent).toContain('原请求 original：unknown')
  expect(launch).not.toHaveBeenCalled(); expect(readPreview).not.toHaveBeenCalled()
})

it('refuses mismatched report identity instead of presenting success', async () => {
  open(adapter({ getBacktest: async () => ({ job_id: 'job', engine_run_id: 'run', status: 'succeeded', report_ready: true } as BacktestJob),
    getReport: async () => ({ run_id: 'foreign' } as BacktestReport) }), '/report/job/overview')
  await screen.findByText(/报告的身份与该运行/)
  expect(screen.queryByRole('region', { name: '正式报告关键结果' })).toBeNull()
})

it('shows unsupported routes as unavailable and never loads synthetic scenario state', async () => {
  const list = vi.fn()
  open(adapter({ listResearch: list }), '/simulations')
  await screen.findByRole('heading', { name: '此候选尚未接入' })
  await waitFor(() => expect(list).not.toHaveBeenCalled())
  expect(localStorage.length).toBe(0)
})

it('shows an unreadable real preview as a refusal with no fabricated result', async () => {
  const launch = vi.fn()
  open(adapter({ previewUserStrategyExperiment: async () => { throw new Error('dataset missing') }, launchUserStrategyExperiment: launch }), '/research/real-idea/confirm')
  await screen.findByText('dataset missing')
  expect(screen.queryByRole('button', { name: '确认并运行一次' })).toBeNull()
  expect(launch).not.toHaveBeenCalled()
})

it('uses one explicit scripted model call and applies its proposal without running', async () => {
  const proposal = { proposal_id: 'p', call_id: 'script', base_revision: 1, status: 'open' as const, created_at: '2026-10-28', applied_paths: [], stale_paths: [], items: [{ path: 'parameters.target_quantity', before: 1, after: 2, reason: 'scripted interaction', evidence: [], proposer: 'script' }] }
  const proposed = { ...strategy, proposals: [proposal] }
  const send = vi.fn(async (_id: string, input: { call_id: string }) => ({ ...proposed, calls: [{ call_id: input.call_id, status: 'observed', base_revision: 1, route: {}, usage_available: false } as UserStrategy['calls'][number]] }))
  const apply = vi.fn(async () => ({ ...proposed, draft: { ...strategy.draft, revision: 2 }, proposals: [{ ...proposal, status: 'applied' as const }] }))
  const launch = vi.fn()
  const api = adapter({ sendUserStrategyMessage: send, applyUserStrategyProposal: apply, launchUserStrategyExperiment: launch,
    settingsStatus: async () => ({ configured: true, provider: 'scripted', model: 'no-network', credential_available: false }) as Awaited<ReturnType<NonNullable<ApiAdapter['settingsStatus']>>> })
  render(<StrictMode><MemoryRouter initialEntries={['/research/real-idea/overview']}><StudioRoutes api={api} /></MemoryRouter></StrictMode>)
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await userEvent.click(await screen.findByRole('button', { name: '应用修改到草稿' }))
  await screen.findByText('草稿 r2 · XNAS:AAPL')
  expect(send).toHaveBeenCalledTimes(1)
  expect(send).toHaveBeenCalledWith('real-idea', expect.objectContaining({ approved_budget: { max_model_calls: 1, max_backtests: 0 } }))
  expect(apply).toHaveBeenCalledTimes(1)
  expect(launch).not.toHaveBeenCalled()
})


it('clears a definite pre-dispatch rejection and requires a fresh preview and click', async () => {
  const launch = vi.fn().mockRejectedValue(new UserStrategyRequestError('stale_base_revision', 'preview changed'))
  const readPreview = vi.fn(async () => preview)
  open(adapter({ launchUserStrategyExperiment: launch, previewUserStrategyExperiment: readPreview }), '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '确认并运行一次' }))
  await screen.findByText(/本次未派发/)
  await userEvent.click(screen.getByRole('button', { name: '重新读取并预览' }))
  await screen.findByRole('button', { name: '确认并运行一次' })
  expect(sessionStorage.getItem('quine.studio.confirm.real-idea')).toBeNull()
  expect(readPreview).toHaveBeenCalledTimes(2)
  expect(launch).toHaveBeenCalledTimes(1)
})

it('allows a fresh preview after the owner released an unknown slot without claiming success', async () => {
  const released = { launch_intent_id: 'released-original', status: 'unknown', owner: 'quine', slot_held: false, recovery: { status: 'released' }, payload: { draft_revision: 1 } }
  open(adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [released] } as UserStrategy) }), '/research/real-idea/confirm')
  await screen.findByRole('button', { name: '确认并运行一次' })
  expect(screen.queryByText(/原请求.*released-original/)).toBeNull()
})

const configuredModel = async () => ({ configured: true, provider: 'scripted', model: 'no-network', credential_available: false }) as Awaited<ReturnType<NonNullable<ApiAdapter['settingsStatus']>>>
it('shows a server unknown model call and identity after reopening and permits a separate question', async () => {
  let doc = structuredClone(strategy)
  const send = vi.fn(async (_id: string, input: { call_id: string }) => {
    doc = { ...doc, calls: [{ call_id: input.call_id, status: 'unknown', base_revision: 1, route: {}, message: 'receipt missing', usage_available: false } as UserStrategy['calls'][number]] }
    return doc
  })
  const api = adapter({ getUserStrategy: async () => doc, sendUserStrategyMessage: send, settingsStatus: configuredModel })
  const view = open(api, '/research/real-idea/overview')
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await screen.findByText(/结果与用量未知/)
  const identity = send.mock.calls[0][1].call_id
  view.unmount()
  open(api, '/research/real-idea/overview')
  await screen.findByText(identity)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  expect(send).toHaveBeenCalledTimes(1)
  await userEvent.type(screen.getByLabelText('消息'), '另一个独立问题')
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false)
})

it('retains transport-unknown model identity across remount and queries without resending', async () => {
  const send = vi.fn().mockRejectedValue(new Error('transport lost'))
  const api = adapter({ sendUserStrategyMessage: send, settingsStatus: configuredModel })
  const view = open(api, '/research/real-idea/overview')
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await screen.findByText(/transport lost/)
  const identity = send.mock.calls[0][1].call_id
  view.unmount()
  open(api, '/research/real-idea/overview')
  await screen.findByText(identity)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  await screen.findByText(/尚未读到此调用/)
  expect(send).toHaveBeenCalledTimes(1)
})
