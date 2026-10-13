import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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


function withEvidence(scope: 'current' | 'stale' | 'unknown' | 'historical', available = true) {
  const value = state(A, '原文保留：source_job_id 33333333-3333-4333-8333-333333333333，projection_stale=true。')
  const frozen = { ...value.context!, state: 'running', state_scope: scope,
    economics: { available, equity: '123.45', currency: 'USD' },
    health: { available, runtime_ready: null, trade_permitted: null, permission_scope: 'unknown' } }
  value.calls[0].evidence = frozen
  value.messages[0].evidence_scope = scope
  // A refreshed context must never rewrite the source of this retained answer.
  value.context = { ...frozen, state_scope: 'current', economics: { available: true, equity: '99999', currency: 'USD' } }
  return value
}

it('summarizes only the matching frozen host evidence, retaining stale limits and verbatim expandable records', async () => {
  const value = withEvidence('stale')
  const api = { conversation: vi.fn().mockResolvedValue(value), explain: vi.fn() }
  render(panel(A, api))
  const overview = await screen.findByRole('region', { name: '本轮运行证据' })
  expect(overview.textContent).toContain('非 AI 摘要')
  expect(overview.textContent).toContain('观测陈旧')
  expect(overview.textContent).toContain('当前就绪与许可未知')
  expect(overview.textContent).toContain('123.45 USD')
  expect(overview.textContent).toContain('不代表当前权益')
  expect(overview.textContent).toContain('不能推断盈利')
  expect(overview.textContent).not.toContain('99999')
  expect(overview.textContent).not.toContain('source_job_id')
  const answer = screen.getByText('完整 AI 回答（原文）').closest('details') as HTMLDetailsElement
  const source = screen.getByText('本轮证据来源').closest('details') as HTMLDetailsElement
  expect(answer.open).toBe(true)
  expect(source.open).toBe(false)
  expect(answer.querySelector('.paper-ai-answer-text')?.textContent).toBe(value.messages[0].text)
  await userEvent.click(screen.getByText('本轮证据来源'))
  expect(source.open).toBe(true)
  expect(source.querySelector('pre')?.textContent).toBe(JSON.stringify(value.calls[0].evidence, null, 2))
  expect(api.explain).not.toHaveBeenCalled()
})

it.each(['unknown', 'historical', 'current'] as const)('keeps %s evidence bounded without filling unknown permission or missing economics', async scope => {
  render(panel(A, { conversation: vi.fn().mockResolvedValue(withEvidence(scope, false)), explain: vi.fn() }))
  const overview = await screen.findByRole('region', { name: '本轮运行证据' })
  expect(overview.textContent).toContain('经济快照不可用')
  expect(overview.textContent).not.toContain('123.45')
  expect(overview.textContent).not.toContain('读取时未阻断')
  expect(overview.textContent).not.toContain('读取时就绪')
  expect(overview.textContent).toContain(scope === 'historical' ? '历史观测' : '未知')
})

it('never substitutes current or another instance evidence when this answer has no matching frozen record', async () => {
  const value = withEvidence('stale')
  value.calls[0].evidence!.run_id = B
  render(panel(A, { conversation: vi.fn().mockResolvedValue(value), explain: vi.fn() }))
  const overview = await screen.findByRole('region', { name: '本轮运行证据' })
  expect(within(overview).getByText(/本轮结构化证据未确认/)).toBeTruthy()
  expect(overview.textContent).not.toContain('123.45')
  expect(overview.textContent).not.toContain('99999')
  expect(overview.textContent).toContain('当前就绪与许可未知')
})


it.each([true, false])('quotes backend read-time readiness/permission %s without treating snapshot equity as current', async flag => {
  const value = withEvidence('current')
  value.calls[0].evidence!.health = { available: true, runtime_ready: flag, trade_permitted: flag, permission_scope: 'observed' }
  render(panel(A, { conversation: vi.fn().mockResolvedValue(value), explain: vi.fn() }))
  const overview = await screen.findByRole('region', { name: '本轮运行证据' })
  expect(overview.textContent).toContain(flag ? '读取时就绪，读取时未阻断' : '读取时未就绪，读取时受阻')
  expect(overview.textContent).toContain('不代表当前权益')
  if (flag) expect(overview.textContent).toContain('具体订单仍须独立检查')
})


it('defaults the latest AI answer open before auxiliary evidence and preserves manual disclosure choices through refresh and streaming', async () => {
  const value = withEvidence('stale')
  value.messages.unshift({ ...value.messages[0], message_id: 'old', call_id: 'old', text: '早前 AI 原文' })
  value.calls.unshift({ ...value.calls[0], call_id: 'old' })
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined
  const body = new ReadableStream<Uint8Array>({ start(next) { controller = next } })
  const api = { conversation: vi.fn().mockResolvedValue(value), explain: vi.fn().mockResolvedValue({ ok: true, body }) }
  render(panel(A, api))
  await screen.findByText('早前 AI 原文')
  const answers = Array.from(document.querySelectorAll<HTMLDetailsElement>('.paper-ai-original'))
  expect(answers[0].open).toBe(false)
  expect(answers[1].open).toBe(true)
  const auxiliary = screen.getAllByRole('region', { name: '本轮运行证据' }).at(-1)!
  expect(answers[1].compareDocumentPosition(auxiliary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(Array.from(document.querySelectorAll<HTMLDetailsElement>('.paper-ai-message-source')).every(node => !node.open)).toBe(true)
  await userEvent.click(answers[1].querySelector('summary')!)
  await userEvent.click(answers[0].querySelector('summary')!)
  await userEvent.click(screen.getByRole('button', { name: '刷新证据' }))
  await waitFor(() => expect(api.conversation).toHaveBeenCalledTimes(2))
  expect(answers[0].open).toBe(true)
  expect(answers[1].open).toBe(false)
  await userEvent.type(screen.getByRole('textbox'), '下一轮')
  await userEvent.click(screen.getByRole('button', { name: /发送提问/ }))
  await act(async () => { controller!.enqueue(new TextEncoder().encode('data: {"kind":"text_delta","text":"新的增量"}\n\n')) })
  expect(answers[0].open).toBe(true)
  expect(answers[1].open).toBe(false)
  const completed = { ...value, calls: [...value.calls, { ...value.calls[1], call_id: 'new' }], messages: [...value.messages, { ...value.messages[1], message_id: 'new', call_id: 'new', text: '新的完整回答' }] }
  api.conversation.mockResolvedValue(completed)
  await act(async () => { controller!.enqueue(new TextEncoder().encode('data: {"kind":"done"}\n\n')); controller!.close() })
  await screen.findByText('新的完整回答')
  expect(answers[0].open).toBe(true)
  expect(answers[1].open).toBe(false)
  expect(document.querySelector<HTMLDetailsElement>('.message-assistant:last-child .paper-ai-original')?.open).toBe(true)
})
