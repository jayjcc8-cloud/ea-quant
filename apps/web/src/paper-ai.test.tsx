import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { PaperAi, type PaperExplanationApi } from './paper-ai'
import { paperExplanationDraftKey, paperExplanationPendingKey, type PaperExplanationConversation } from './paper-api'

const A = '11111111-1111-4111-8111-111111111111'
const B = '22222222-2222-4222-8222-222222222222'
function state(id: string, answer = ''): PaperExplanationConversation {
  return { run_id: id, context: { schema: 'ea.paper-explanation-evidence.v1', run_id: id, instance_name: id, candidate_id: 'candidate', source_job_id: 'source', strategy: { id: 'bounded', version: 1 }, configuration_sha256: 'a'.repeat(64), market: { kind: 'local_synthetic_feed', interval_seconds: 1, bounded_prices: 1 }, observed_at: '2026-10-13T01:00:00Z', state: 'unknown', state_scope: 'unknown', historical: false, terminal_durable: false, economics: { available: false }, health: { available: false }, limits: [] }, messages: answer ? [{ message_id: 'm', role: 'assistant', call_id: 'saved', text: answer, created_at: '2026-10-13T01:01:00Z', evidence_observed_at: '2026-10-13T01:00:00Z', evidence_scope: 'unknown' }] : [], calls: answer ? [{ call_id: 'saved', status: 'observed', created_at: '2026-10-13T01:00:00Z' }] : [], budget: { max_model_calls: 1, max_backtests: 0 }, route: { provider: 'fixture', model: 'fixture' } }
}
function response(events: object[]): Response {
  return { ok: true, body: new ReadableStream<Uint8Array>({ start(controller) { for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)); controller.close() } }) } as Response
}
function panel(id: string, api: PaperExplanationApi) {
  return <MemoryRouter><PaperAi key={id} runId={id} instanceName={`实例 ${id === A ? 'A' : 'B'}`} sourceJobId="source" api={api} /></MemoryRouter>
}
afterEach(() => { cleanup(); sessionStorage.clear() })

it('switches A/B without leaking late replies or unsent input, and never sends on selection', async () => {
  const api = { conversation: vi.fn(async (id: string) => state(id)), explain: vi.fn() }
  const mounted = render(panel(A, api))
  await screen.findByText(/证据观测时间/)
  await userEvent.type(screen.getByRole('textbox'), 'A 的草稿')
  mounted.rerender(panel(B, api))
  await screen.findByText('只读解释：实例 B')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  await userEvent.type(screen.getByRole('textbox'), 'B 的草稿')
  mounted.rerender(panel(A, api))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('A 的草稿')
  expect(api.explain).not.toHaveBeenCalled()
  let release: (value: Response) => void = () => {}
  api.explain.mockReturnValue(new Promise<Response>(resolve => { release = resolve }))
  await userEvent.click(await screen.findByRole('button', { name: /发送提问/ }))
  mounted.rerender(panel(B, api))
  await act(async () => { release(response([{ kind: 'text_delta', text: 'A 迟到回答' }, { kind: 'done' }])) })
  expect(screen.queryByText('A 迟到回答')).toBeNull()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('B 的草稿')
  expect(api.explain).toHaveBeenCalledTimes(1)
})

it('queries a restored unknown identity once using the newly returned record, without resend', async () => {
  sessionStorage.setItem(paperExplanationPendingKey(A), 'saved')
  sessionStorage.setItem(paperExplanationDraftKey(A), '保留原问题')
  const pending = { ...state(A), calls: [{ call_id: 'saved', status: 'dispatched', created_at: '2026-10-13T01:00:00Z' }] }
  const api = { conversation: vi.fn().mockResolvedValueOnce(pending).mockResolvedValue(state(A, '原请求回答')), explain: vi.fn() }
  render(panel(A, api))
  await screen.findByText(/已发送 · 结果未确认/)
  await userEvent.click(screen.getByRole('button', { name: '查询原请求结果' }))
  await screen.findByText('原请求回答')
  expect(sessionStorage.getItem(paperExplanationPendingKey(A))).toBeNull()
  expect(api.explain).not.toHaveBeenCalled()
  expect(screen.getByText(/依据证据观测于/)).toBeTruthy()
  expect(screen.getByRole('link', { name: /来源研究/ }).getAttribute('href')).toBe('/backtests/source')
})

it('shows real deltas, persists completed output, and reload is only a read', async () => {
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  const api = { conversation: vi.fn().mockResolvedValueOnce(state(A)).mockResolvedValue(state(A, '完整回答')), explain: vi.fn().mockResolvedValue({ ok: true, body }) }
  const mounted = render(panel(A, api))
  await screen.findByText(/证据观测时间/)
  await userEvent.type(screen.getByRole('textbox'), '当前状态？')
  await userEvent.click(screen.getByRole('button', { name: /发送提问/ }))
  await act(async () => { controller!.enqueue(new TextEncoder().encode('data: {"kind":"text_delta","text":"真正增量"}\n\n')) })
  await screen.findByText('真正增量')
  await act(async () => { controller!.enqueue(new TextEncoder().encode('data: {"kind":"done"}\n\n')); controller!.close() })
  await screen.findByText('完整回答')
  mounted.unmount()
  render(panel(A, api))
  await screen.findByText('完整回答')
  expect(api.explain).toHaveBeenCalledTimes(1)
})

it('a failed stream can be queried and a new explicit turn is not permanently disabled', async () => {
  const unknown = { ...state(A), calls: [{ call_id: 'unused', status: 'unknown', created_at: '2026-10-13T01:00:00Z' }] }
  const api = { conversation: vi.fn().mockResolvedValue(state(A)), explain: vi.fn().mockResolvedValue(response([{ kind: 'error', error: '结果未知' }])) }
  render(panel(A, api))
  await screen.findByText(/证据观测时间/)
  await userEvent.type(screen.getByRole('textbox'), '失败问题')
  await userEvent.click(screen.getByRole('button', { name: /发送提问/ }))
  await screen.findByText(/这次提问的结果未确认/)
  const id = sessionStorage.getItem(paperExplanationPendingKey(A))!
  unknown.calls[0].call_id = id
  api.conversation.mockResolvedValue(unknown)
  await userEvent.click(screen.getByRole('button', { name: '查询原请求结果' }))
  await waitFor(() => expect(sessionStorage.getItem(paperExplanationPendingKey(A))).toBeNull())
  expect((screen.getByRole('button', { name: /发送提问/ }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.explain).toHaveBeenCalledTimes(1)
})
