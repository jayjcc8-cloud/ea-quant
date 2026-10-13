import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { PaperWorkspace } from './paper-workspace'
import { PaperApiError, type PaperApi, type PaperInstance, type PaperPreparation, type PaperRequest } from './paper-api'
import { observationTime } from './paper-observation'

const id = '9fdc8d75-146c-4459-8772-02d6b5c15739'
const prepared = { eligible: true, start_available: true, fingerprint: 'a'.repeat(64), blockers: [], source_job_id: id, configuration: { strategy: { id: 'bounded-long', version: 1 }, instrument: { venue: 'XNAS', symbol: 'AAPL' }, funding: { initial_cash: '10000', currency: 'USD' }, risk: {}, execution: {} } } as unknown as PaperPreparation
const intent: PaperRequest = { request_id: id, candidate_id: id, fingerprint: 'a'.repeat(64), name: '测试实例', prices: [100], interval_seconds: 1 }
const row: PaperInstance = { run_id: id, request: intent, source_job_id: id, state: 'running', terminal_confirmed: false, stop_requested: false, observed_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z', overview: { available: true, run_id: id, state: 'running', cash: '9999.8', equity: '9999.8', fills: 2, position: '0', currency: 'USD', lease_held: true, health: { schema: 'ea.paper-health.v1', run_id: id, observed_at: '2026-01-02T00:00:00Z', process_alive: true, projection_stale: false, reconciliation_state: 'clean' } } }
const explanationContext = { schema: 'ea.paper-explanation-evidence.v1', run_id: id, instance_name: '测试实例', candidate_id: id, source_job_id: id, strategy: { id: 'bounded-long', version: 1 }, configuration_sha256: 'a'.repeat(64), market: { kind: 'local_synthetic_feed', interval_seconds: 1, bounded_prices: 1 }, observed_at: '2026-01-02T00:00:00Z', state: 'running', state_scope: 'live' as const, historical: false, terminal_durable: false, economics: { available: true, reason: null }, health: { available: true, reason: null }, limits: [] }
function adapter(overrides: Partial<PaperApi> = {}): PaperApi { return { list: vi.fn().mockResolvedValue({ available: true, instances: [] }), candidates: vi.fn().mockResolvedValue({ candidates: [{ candidate_id: id, status: 'ACCEPTED', projection: { strategy: { id: 'bounded-long' } } }] }), prepare: vi.fn().mockResolvedValue(prepared), start: vi.fn().mockResolvedValue(row), get: vi.fn().mockResolvedValue(row), stop: vi.fn().mockResolvedValue(row), events: vi.fn().mockResolvedValue({ available: true, run_id: id, events: [] }), conversation: vi.fn().mockResolvedValue({ run_id: id, context: explanationContext, messages: [], calls: [], budget: { max_model_calls: 1, max_backtests: 0 }, route: null }), explain: vi.fn(), ...overrides } }
function mount(api: PaperApi) { render(<MemoryRouter initialEntries={[`/runs?candidate=${id}`]}><PaperWorkspace api={api} /></MemoryRouter>) }
afterEach(() => { cleanup(); sessionStorage.clear() })

/*
 * Fixtures for the observation regressions below. Each row is a hand-built
 * PaperInstance served by an injected PaperApi — no backend, clock or network is
 * involved, and every assertion reads rendered text rather than a recomputed fact.
 */
const baseHealth = { schema: 'ea.paper-health.v1', observed_at: '2026-01-02T00:00:00Z', process_alive: true, projection_stale: false, runtime_ready: true, trade_permitted: true, reconciliation_state: 'clean' }
function fixtureRow(overrides: { run_id?: string; name?: string; state?: string; terminal_confirmed?: boolean; health?: Record<string, unknown> | null; overview?: Record<string, unknown> } = {}): PaperInstance {
  const run_id = overrides.run_id ?? id
  const overview: Record<string, unknown> = { available: true, run_id, state: overrides.state ?? 'running', cash: '9999.8', equity: '9999.8', fills: 2, position: '0', currency: 'USD', valuation_time: '2026-01-02T00:00:00Z', lease_held: true, updated_at: '2026-01-02T00:00:00Z', ...overrides.overview }
  if (overrides.health !== null) overview.health = { ...baseHealth, run_id, ...overrides.health }
  return { run_id, request: { ...intent, request_id: run_id, name: overrides.name ?? '测试实例' }, source_job_id: id, state: overrides.state ?? 'running', terminal_confirmed: overrides.terminal_confirmed ?? false, stop_requested: false, observed_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z', overview } as unknown as PaperInstance
}
const served = (...instances: PaperInstance[]) => ({ available: true, instances })
const region = (selector: string) => (document.querySelector(selector) as HTMLElement).textContent ?? ''

it('requires explicit confirmation and retains a lost startup response for query only', async () => {
  const user = userEvent.setup()
  const api = adapter({ start: vi.fn().mockRejectedValue(new Error('response lost')) })
  mount(api)
  const start = await screen.findByRole('button', { name: '确认启动本地模拟' })
  expect((start as HTMLButtonElement).disabled).toBe(true)
  await user.click(screen.getByRole('checkbox'))
  await user.dblClick(start)
  expect(api.start).toHaveBeenCalledTimes(1)
  await screen.findByText('启动结果尚未确认')
  expect(screen.queryByRole('button', { name: '确认启动本地模拟' })).toBeNull()
  await user.click(screen.getByRole('button', { name: '查询原启动请求' }))
  expect(api.get).toHaveBeenCalledWith((vi.mocked(api.start).mock.calls[0][0]).request_id)
  expect(api.start).toHaveBeenCalledTimes(1)
})

it('keeps an unconfirmed stop and missing economic metrics explicit', async () => {
  const user = userEvent.setup()
  const api = adapter({ list: vi.fn().mockResolvedValue({ available: true, instances: [row] }), stop: vi.fn().mockResolvedValue({ ...row, state: 'stop_requested', stop_requested: true }) })
  mount(api)
  await user.click(await screen.findByRole('button', { name: '停止运行' }))
  await screen.findByText('已请求停止 · 尚未确认', { selector: '.paper-runtime-value' })
  expect((screen.getByRole('button', { name: '配置新实例' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('均价、已实现 / 浮动盈亏与费用拆分未提供。')).toBeTruthy()
  expect(screen.getByText('clean', { selector: '.paper-metrics strong' })).toBeTruthy()
  expect(screen.getByRole('complementary', { name: 'AI 运行解释' }).textContent).toContain('只读解释')
  expect(screen.getByRole('textbox', { name: /提问/ })).toBeTruthy()
})

it('shows a disconnected observation as unknown and never retries start on mount', async () => {
  sessionStorage.setItem('quine.local-paper.pending.v1', JSON.stringify(intent))
  const api = adapter({ list: vi.fn().mockRejectedValue(new Error('offline')), get: vi.fn().mockRejectedValue(new PaperApiError(503, 'offline')) })
  mount(api)
  await screen.findByText(/连接中断/)
  expect((screen.getByRole('button', { name: '配置新实例' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.start).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '查询原启动请求' }))
  await waitFor(() => expect(screen.getByText(/仍无法确认原请求/)).toBeTruthy())
  expect(api.start).not.toHaveBeenCalled()
})

it('【fixture】keeps readiness, process and permission as three separate readings and quotes the backend reason', async () => {
  const user = userEvent.setup()
  const health = { runtime_ready: true, trade_permitted: false, trade_blocking_guard: 'kill_switch', trade_reason: '操作员已暂停交易，许可被阻断', trade_permission_basis: 'zero_effect_probe', trade_permission_unexercised_guards: ['price_deviation'], reason_codes: ['trade_permission.kill_switch'] }
  mount(adapter({ list: vi.fn().mockResolvedValue(served(fixtureRow({ health }))) }))
  const observation = await screen.findByRole('region', { name: '运行观测' })
  expect(observation.textContent).toContain('存活 · 租约占用')
  expect(screen.getByText('读取时就绪')).toBeTruthy()
  expect(screen.getByText('读取时受阻')).toBeTruthy()
  expect(screen.getByText('trade_permission.kill_switch', { selector: '.paper-runtime-reasons li' })).toBeTruthy()
  expect(screen.getByText('首个许可阻断：kill_switch · 操作员已暂停交易，许可被阻断')).toBeTruthy()
  // The caveat is visible without opening anything, and it promises no particular order.
  expect(screen.getByText('仅反映操作性门槛，不保证具体订单获准。')).toBeTruthy()
  const details = screen.getByText('观测详情').closest('details') as HTMLDetailsElement
  expect(details.open).toBe(false)
  await user.click(screen.getByText('观测详情'))
  expect(details.open).toBe(true)
  expect(screen.getByText('首个阻断项不代表全部检查；具体订单仍须独立检查。')).toBeTruthy()
  expect(screen.getByText('zero_effect_probe')).toBeTruthy()
  expect(screen.getByText('price_deviation')).toBeTruthy()
  // The probe's own basis is published once, inside the disclosure only.
  expect(screen.getAllByText('zero_effect_probe')).toHaveLength(1)
})

it('【fixture】reports a readiness failure on its own while permission stays unblocked', async () => {
  mount(adapter({ list: vi.fn().mockResolvedValue(served(fixtureRow({ health: { runtime_ready: false, trade_permitted: true, reason_codes: ['readiness.storage'] } }))) }))
  await screen.findByText('读取时未就绪')
  expect(screen.getByText('存储未就绪（readiness.storage）')).toBeTruthy()
  expect(screen.getByText('读取时未阻断')).toBeTruthy()
  expect(screen.queryByText('读取时受阻')).toBeNull()
})

it('【fixture】a stale projection shows no current reading at all', async () => {
  mount(adapter({ list: vi.fn().mockResolvedValue(served(fixtureRow({ health: { projection_stale: true } }))) }))
  await screen.findByText('观测陈旧 · 当前就绪与许可未知，请刷新确认。')
  expect(screen.getAllByText('未知')).toHaveLength(2)
  expect(screen.queryByText('读取时就绪')).toBeNull()
  expect(screen.queryByText('读取时未阻断')).toBeNull()
  expect(screen.queryByText('读取时受阻')).toBeNull()
})

it('【fixture】missing projection fields and omitted economics read 未提供, never 0', async () => {
  const sparse = fixtureRow({ health: { runtime_ready: undefined, trade_permitted: undefined, process_alive: undefined, projection_stale: undefined, reconciliation_state: undefined, observed_at: null }, overview: { cash: undefined, equity: undefined, fills: undefined, position: undefined, currency: undefined, valuation_time: undefined, updated_at: undefined } })
  mount(adapter({ list: vi.fn().mockResolvedValue(served(sparse)) }))
  await screen.findByText('观测未确认 · 缺少有效投影，当前就绪与许可未知。')
  expect(screen.queryByText('读取时就绪')).toBeNull()
  expect(screen.queryByText('读取时未阻断')).toBeNull()
  expect(screen.queryByText('读取时受阻')).toBeNull()
  expect(region('.paper-equity')).toContain('未提供')
  expect(region('.paper-metrics')).toContain('未提供')
  expect(region('.paper-metrics')).not.toContain('0')
  expect(screen.getByText(`状态发布（经济快照）：${observationTime(undefined)}`)).toBeTruthy()
  expect(screen.getByText(`估值时点：${observationTime(undefined)}`)).toBeTruthy()
})

it('【fixture】a dropped poll keeps the economic snapshot and demotes permission until a good poll returns', async () => {
  const user = userEvent.setup()
  const good = served(fixtureRow({}))
  let recover: (value: { available: boolean; instances: PaperInstance[] }) => void = () => {}
  const later = new Promise<{ available: boolean; instances: PaperInstance[] }>(resolve => { recover = resolve })
  const list = vi.fn().mockResolvedValueOnce(good).mockRejectedValueOnce(new Error('offline')).mockReturnValue(later)
  mount(adapter({ list }))
  await screen.findByText('读取时未阻断')
  expect(screen.getByText('读取时经济快照')).toBeTruthy()
  await user.click(screen.getByRole('button', { name: '刷新状态' }))
  await screen.findByText('连接中断：保留上次观测，当前进程与停止状态未知。')
  expect(region('.paper-equity')).toContain('9999.8')
  expect(screen.queryByText('读取时未阻断')).toBeNull()
  expect(screen.getByText('历史经济快照')).toBeTruthy()
  await user.click(screen.getByRole('button', { name: '刷新状态' }))
  recover(good)
  await waitFor(() => expect(screen.queryByText('连接中断：保留上次观测，当前进程与停止状态未知。')).toBeNull())
  expect(screen.getByText('读取时未阻断')).toBeTruthy()
  expect(screen.getByText('读取时经济快照')).toBeTruthy()
})

it('【fixture】a confirmed terminal run reads as history rather than a current fault', async () => {
  const stopped = fixtureRow({ state: 'stopped', terminal_confirmed: true, overview: { lease_held: false }, health: { process_alive: false, projection_stale: true, runtime_ready: false, trade_permitted: false, reason_codes: ['readiness.storage'] } })
  mount(adapter({ list: vi.fn().mockResolvedValue(served(stopped)) }))
  await screen.findByText('终态已确认 · 以下保留原实例历史，不作为当前就绪或许可。')
  expect(screen.getByText('已停止')).toBeTruthy()
  expect(screen.getAllByText('历史观测')).toHaveLength(2)
  expect(screen.queryByText('读取时受阻原因')).toBeNull()
  expect(screen.queryByText('读取时未就绪')).toBeNull()
  expect(screen.queryByText('读取时受阻')).toBeNull()
  expect(screen.queryByText('观测陈旧 · 当前就绪与许可未知，请刷新确认。')).toBeNull()
  // The code the authority published is still readable — inside the disclosure only.
  expect(screen.getByText('readiness.storage')).toBeTruthy()
  expect(screen.getByText(`本次读取：${observationTime('2026-01-02T00:00:00Z')}`)).toBeTruthy()
  expect(region('.paper-equity')).toContain('9999.8')
  expect(screen.getByText('历史经济快照')).toBeTruthy()
})

it('【fixture】a discarded older list response cannot replace the selected run or its reasons', async () => {
  const user = userEvent.setup()
  const runA = '11111111-1111-4111-8111-111111111111'
  const runB = '22222222-2222-4222-8222-222222222222'
  const runC = '33333333-3333-4333-8333-333333333333'
  const a = fixtureRow({ run_id: runA, name: '实例 A', health: { runtime_ready: false, reason_codes: ['readiness.storage'] } })
  const b = fixtureRow({ run_id: runB, name: '实例 B', health: { runtime_ready: false, reason_codes: ['readiness.market_freshness'] } })
  // C's projection belongs to another run: it must be quarantined, not quoted.
  const c = fixtureRow({ run_id: runC, name: '实例 C', health: { run_id: runA, runtime_ready: false, reason_codes: ['readiness.recovery'] } })
  let release: (value: { available: boolean; instances: PaperInstance[] }) => void = () => {}
  const older = new Promise<{ available: boolean; instances: PaperInstance[] }>(resolve => { release = resolve })
  const list = vi.fn().mockReturnValueOnce(older).mockResolvedValue(served(a, b, c))
  mount(adapter({ list }))
  await user.click(await screen.findByRole('button', { name: '刷新状态' }))
  const select = await screen.findByLabelText('运行实例') as HTMLSelectElement
  await user.selectOptions(select, runB)
  expect(screen.getByText('行情未就绪（readiness.market_freshness）')).toBeTruthy()
  release(served(a))
  await waitFor(() => expect(select.value).toBe(runB))
  expect(screen.getByText('行情未就绪（readiness.market_freshness）')).toBeTruthy()
  expect(screen.queryByText('存储未就绪（readiness.storage）')).toBeNull()
  await user.selectOptions(select, runC)
  expect(screen.queryByText('读取时受阻原因')).toBeNull()
  expect(screen.queryByText('恢复依赖未就绪（readiness.recovery）')).toBeNull()
  expect(screen.getByText('健康投影身份不匹配或格式不支持，已隔离')).toBeTruthy()
})

it('distinguishes unavailable economic evidence from a historical snapshot without invented placeholder balances', async () => {
  mount(adapter({ list: vi.fn().mockResolvedValue(served(fixtureRow({ health: null, overview: { available: false } }))) }))
  await screen.findByText('经济快照不可用')
  expect(region('.paper-account')).toContain('尚无可读取的经济快照')
  expect(region('.paper-account')).not.toContain('未提供')
  expect(screen.queryByText('历史经济快照')).toBeNull()
  expect(document.querySelector('.paper-equity')).toBeNull()
  expect(screen.queryByRole('heading', { name: '当前持仓' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '持仓与成交' }))
  expect(region('.paper-facts')).toContain('尚无可读取的经济快照')
  expect(document.querySelector('.paper-facts table')).toBeNull()
})


it('offers keyboard-focusable AI and overview navigation and foregrounds stale scope before readings and equity', async () => {
  const user = userEvent.setup()
  mount(adapter({ list: vi.fn().mockResolvedValue(served(fixtureRow({ health: { projection_stale: true } }))) }))
  const jump = await screen.findByRole('button', { name: '查看 AI 解释' })
  await user.click(jump)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'AI 运行解释' }))
  await user.click(screen.getByRole('button', { name: '返回概览' }))
  expect(document.activeElement).toBe(screen.getByRole('heading', { level: 1 }))
  const status = screen.getByRole('region', { name: '运行观测' })
  expect(status.firstElementChild?.textContent).toContain('观测陈旧')
  const account = document.querySelector('.paper-account')!
  expect(account.textContent?.indexOf('不代表当前权益')).toBeLessThan(account.textContent!.indexOf('9999.8'))
})
