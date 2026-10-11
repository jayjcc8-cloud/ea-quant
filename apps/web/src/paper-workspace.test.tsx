import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { PaperWorkspace } from './paper-workspace'
import { PaperApiError, type PaperApi, type PaperInstance, type PaperPreparation, type PaperRequest } from './paper-api'

const id = '9fdc8d75-146c-4459-8772-02d6b5c15739'
const prepared = { eligible: true, start_available: true, fingerprint: 'a'.repeat(64), blockers: [], source_job_id: id, configuration: { strategy: { id: 'bounded-long', version: 1 }, instrument: { venue: 'XNAS', symbol: 'AAPL' }, funding: { initial_cash: '10000', currency: 'USD' }, risk: {}, execution: {} } } as unknown as PaperPreparation
const intent: PaperRequest = { request_id: id, candidate_id: id, fingerprint: 'a'.repeat(64), name: '测试实例', prices: [100], interval_seconds: 1 }
const row: PaperInstance = { run_id: id, request: intent, source_job_id: id, state: 'running', terminal_confirmed: false, stop_requested: false, observed_at: '2026-01-02T00:00:00Z', created_at: '2026-01-02T00:00:00Z', overview: { available: true, run_id: id, state: 'running', cash: '9999.8', equity: '9999.8', fills: 2, position: '0', currency: 'USD', lease_held: true, health: { schema: 'ea.paper-health.v1', run_id: id, observed_at: '2026-01-02T00:00:00Z', process_alive: true, projection_stale: false, reconciliation_state: 'clean' } } }
function adapter(overrides: Partial<PaperApi> = {}): PaperApi { return { list: vi.fn().mockResolvedValue({ available: true, instances: [] }), candidates: vi.fn().mockResolvedValue({ candidates: [{ candidate_id: id, status: 'ACCEPTED', projection: { strategy: { id: 'bounded-long' } } }] }), prepare: vi.fn().mockResolvedValue(prepared), start: vi.fn().mockResolvedValue(row), get: vi.fn().mockResolvedValue(row), stop: vi.fn().mockResolvedValue(row), events: vi.fn().mockResolvedValue({ available: true, run_id: id, events: [] }), ...overrides } }
function mount(api: PaperApi) { render(<MemoryRouter initialEntries={[`/runs?candidate=${id}`]}><PaperWorkspace api={api} /></MemoryRouter>) }
afterEach(() => { cleanup(); sessionStorage.clear() })

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
  await screen.findByText('已请求停止 · 尚未确认', { selector: 'strong' })
  expect((screen.getByRole('button', { name: '配置新实例' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('均价、已实现 / 浮动盈亏与费用拆分未提供。')).toBeTruthy()
  expect(screen.getByText('clean')).toBeTruthy()
  expect(screen.getByRole('complementary', { name: 'AI 运行助手' }).textContent).toContain('运行证据解释暂未接入')
  expect(screen.queryByRole('textbox', { name: /提问/ })).toBeNull()
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
