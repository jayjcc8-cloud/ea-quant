import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
afterEach(() => { cleanup(); sessionStorage.clear(); vi.restoreAllMocks() })

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


function scrollGeometry() {
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(1500)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(300)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const top = this.classList.contains('message') ? 900 : 100
    return { top, bottom: top + 30, left: 0, right: 300, width: 300, height: 30, x: 0, y: top, toJSON() {} }
  })
}

it('opens the latest retained turn without scrolling the page, respects history reading, and resets per instance', async () => {
  scrollGeometry()
  const history = (id: string) => {
    const value = state(id, `${id} 最新回答`)
    value.messages.unshift({ ...value.messages[0], message_id: 'old', call_id: 'old', text: '早前回答' })
    return value
  }
  const api = { conversation: vi.fn(async (id: string) => history(id)), explain: vi.fn() }
  const mounted = render(panel(A, api))
  await screen.findByText(`${A} 最新回答`)
  let scroller = screen.getByRole('region', { name: '运行解释会话' })
  expect(scroller.scrollTop).toBe(800)
  scroller.scrollTop = 160
  fireEvent.scroll(scroller)
  await userEvent.click(screen.getByRole('button', { name: '刷新证据' }))
  await waitFor(() => expect(api.conversation).toHaveBeenCalledTimes(2))
  expect(scroller.scrollTop).toBe(160)
  mounted.rerender(panel(B, api))
  await screen.findByText(`${B} 最新回答`)
  scroller = screen.getByRole('region', { name: '运行解释会话' })
  expect(scroller.scrollTop).toBe(800)
  expect(screen.queryByText(`${A} 最新回答`)).toBeNull()
  mounted.rerender(panel(A, api))
  await screen.findByText(`${A} 最新回答`)
  expect(screen.getByRole('region', { name: '运行解释会话' }).scrollTop).toBe(800)
  expect(api.explain).not.toHaveBeenCalled()
})

it('follows streamed text only while at the end and keeps manual history position through completion', async () => {
  scrollGeometry()
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  const api = { conversation: vi.fn().mockResolvedValueOnce(state(A, '旧回答')).mockResolvedValue(state(A, '完整新回答')), explain: vi.fn().mockResolvedValue({ ok: true, body }) }
  render(panel(A, api))
  await screen.findByText('旧回答')
  const scroller = screen.getByRole('region', { name: '运行解释会话' })
  await userEvent.type(screen.getByRole('textbox'), '新问题')
  await userEvent.click(screen.getByRole('button', { name: /发送提问/ }))
  const delta = async (text: string) => act(async () => { controller!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ kind: 'text_delta', text })}\n\n`)) })
  await delta('第一段')
  expect(scroller.scrollTop).toBe(1200)
  scroller.scrollTop = 120
  fireEvent.scroll(scroller)
  await delta('第二段')
  expect(scroller.scrollTop).toBe(120)
  await act(async () => { controller!.enqueue(new TextEncoder().encode('data: {"kind":"done"}\n\n')); controller!.close() })
  await screen.findByText('完整新回答')
  expect(scroller.scrollTop).toBe(120)
  await userEvent.click(screen.getByRole('button', { name: '回到最新' }))
  expect(scroller.scrollTop).toBe(1200)
})

it('preserves literal model evidence and exposes full source time through a disclosure', async () => {
  const answer = '结论\n<script>alert(1)</script> javascript:alert(1)\nsource_job_id 33333333-3333-4333-8333-333333333333'
  render(panel(A, { conversation: vi.fn().mockResolvedValue(state(A, answer)), explain: vi.fn() }))
  await screen.findByText(/source_job_id/)
  expect(document.querySelector('.paper-ai-answer-text')?.textContent).toBe(answer)
  expect(document.querySelector('.paper-ai-answer-text script')).toBeNull()
  const details = screen.getByText('本轮证据来源').closest('details') as HTMLDetailsElement
  expect(details.open).toBe(false)
  await userEvent.click(screen.getByText('本轮证据来源'))
  expect(details.open).toBe(true)
  expect(details.textContent).toContain('2026-10-13T01:00:00Z')
})
