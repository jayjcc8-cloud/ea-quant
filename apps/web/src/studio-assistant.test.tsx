import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ApiAdapter } from './app'
import { Assistant } from './studio-assistant'
import type { UserStrategy, UserStrategyCall, UserStrategyConversation, UserStrategyConversationEvent } from './user-strategies-api'

const original: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'research-one', name: '成本研究', intent: '比较成本', created_at: '2026-11-04', updated_at: '2026-11-04',
  draft: { kind: 'intent', revision: 4, scenario_id: null }, history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const conversation: UserStrategyConversation = { strategy_id: original.user_strategy_id, revision: 4, pending: null, preview: null, open_proposals: [], task: null, budget: { max_model_calls_per_turn: 4, reserved_model_calls: 0, max_backtests: 0 } }
const call = (id: string, status: UserStrategyCall['status'] = 'observed'): UserStrategyCall => ({ call_id: id, base_revision: 4, status, route: { provider: 'script', model: 'offline', endpoint: null }, approved_budget: { max_model_calls: 1, max_backtests: 0 }, reserved_at: '2026-11-04', message: null, model: 'offline', input_tokens: null, output_tokens: null, usage_available: false })
function queue() {
  const events: UserStrategyConversationEvent[] = []
  let wake: (() => void) | undefined
  let ended = false
  return {
    push(event: UserStrategyConversationEvent) { events.push(event); wake?.() },
    end() { ended = true; wake?.() },
    async *stream() { while (true) { if (events.length) yield events.shift()!; else if (ended) return; else await new Promise<void>(resolve => { wake = resolve }) } },
  }
}
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { settingsStatus: async () => ({ configured: true, provider: 'script', model: 'offline', credential_available: false }), getUserStrategy: async () => structuredClone(original), userStrategyConversation: async () => conversation, resumeUserStrategyConversation: vi.fn(async () => (async function* () {})()), ...overrides } as ApiAdapter
}
function open(api: ApiAdapter, initial = original, jobId?: string) {
  function Harness() { const [strategy, changed] = useState(initial); return <Assistant api={api} strategy={strategy} changed={changed} jobId={jobId} /> }
  return render(<MemoryRouter><Harness /></MemoryRouter>)
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it('fills and focuses an empty question without replacing an unsent draft or sending it', async () => {
  const send = vi.fn()
  open(adapter({ sendUserStrategyMessage: send }))
  const input = await screen.findByLabelText('消息') as HTMLTextAreaElement
  await userEvent.click(screen.getByRole('button', { name: '检查当前证据' }))
  expect(input.value).toBe('检查当前草稿的参数与数据')
  expect(document.activeElement).toBe(input)
  fireEvent.change(input, { target: { value: '我还没有写完的问题' } })
  await userEvent.click(screen.getByRole('button', { name: '检查下一步' }))
  expect(input.value).toBe('我还没有写完的问题')
  expect(sessionStorage.getItem('quine.studio.unsent.research-one.draft')).toBe('我还没有写完的问题')
  expect(send).not.toHaveBeenCalled()
})

it.each(['ctrlKey', 'metaKey'] as const)('sends once with %s+Enter while preserving newlines and composition', async modifier => {
  const q = queue(), send = vi.fn(async () => q.stream())
  open(adapter({ streamUserStrategyMessage: send }))
  const input = await screen.findByLabelText('消息') as HTMLTextAreaElement
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true })
  expect(send).not.toHaveBeenCalled()
  await userEvent.type(input, '第一行{enter}第二行{shift>}{enter}{/shift}第三行')
  expect(input.value).toBe('第一行\n第二行\n第三行')
  expect(send).not.toHaveBeenCalled()
  fireEvent.compositionStart(input)
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true })
  fireEvent.compositionEnd(input)
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true, isComposing: true })
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true, keyCode: 229 })
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true, repeat: true })
  expect(send).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true })
  fireEvent.keyDown(input, { key: 'Enter', [modifier]: true })
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  const request = (send.mock.calls[0] as unknown as [string, { call_id: string }])[1]
  expect(request).toMatchObject({ text: '第一行\n第二行\n第三行', task_budget: 1, approved_budget: { max_model_calls: 1, max_backtests: 0 } })
  await act(async () => { q.push({ kind: 'done', strategy: { ...original, calls: [call(request.call_id)] } }); q.end() })
  expect(input.value).toBe('')
})

it('shows the submitted question before the stream opens and replaces it with its recorded message without duplication', async () => {
  const q = queue()
  let start!: (events: ReturnType<typeof q.stream>) => void
  const send = vi.fn(async () => new Promise<ReturnType<typeof q.stream>>(resolve => { start = resolve }))
  open(adapter({ streamUserStrategyMessage: send }), original, 'selected-report')
  const input = await screen.findByLabelText('消息') as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '这份报告\n有什么限制？' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  const currentQuestion = await screen.findByRole('article', { name: '本次提问' })
  expect(within(currentQuestion).getByText('这份报告 有什么限制？').textContent).toBe('这份报告\n有什么限制？')
  expect(within(currentQuestion).getByRole('link', { name: /selected-report/ }).getAttribute('href')).toBe('/report/selected-report/overview')
  expect(input.value).toBe('这份报告\n有什么限制？')
  fireEvent.change(input, { target: { value: '准备中的下一问' } })
  expect(within(currentQuestion).queryByText('准备中的下一问')).toBeNull()
  const request = (send.mock.calls[0] as unknown as [string, { call_id: string }])[1]
  const userMessage: UserStrategy['messages'][number] = { message_id: 'saved-question', call_id: request.call_id, role: 'user', kind: 'message', text: '这份报告\n有什么限制？', base_revision: 4, at: '2026-11-04', proposal_id: null }
  const stored = { ...original, calls: [call(request.call_id, 'dispatched')], messages: [userMessage] }
  await act(async () => { start(q.stream()); q.push({ kind: 'state', strategy: stored }) })
  expect(screen.queryByRole('article', { name: '本次提问' })).toBeNull()
  expect(screen.getAllByText('这份报告 有什么限制？')).toHaveLength(1)
  await act(async () => { q.push({ kind: 'done', strategy: { ...stored, calls: [call(request.call_id)] } }); q.end() })
  expect(input.value).toBe('准备中的下一问')
  expect(screen.getAllByText('这份报告 有什么限制？')).toHaveLength(1)
})

it('offers model setup directly while keeping the question and keyboard send disabled until a model is configured', async () => {
  const send = vi.fn(), launch = vi.fn()
  open(adapter({ settingsStatus: async () => ({ schema: 'radian.settings-status.v1', settings_file: '/fixture/settings.json', configured: false, provider: null, model: null, base_url: null, api_key_env: null, credential_available: false }), sendUserStrategyMessage: send, launchUserStrategyExperiment: launch }))
  const setup = await screen.findByRole('link', { name: '配置模型连接' })
  expect(setup.getAttribute('href')).toBe('/resources/models')
  expect(setup.closest('details')).toBeNull()
  const input = screen.getByLabelText('消息') as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '先保留这个问题' } })
  fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true })
  fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByRole('status').textContent).toBe('选择模型后可发送')
  expect(input.value).toBe('先保留这个问题')
  expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('refreshes saved model settings without replacing the composer, aborting its stream or sending another question', async () => {
  const q = queue(), send = vi.fn(async () => q.stream()), configure = vi.fn()
  let modelName = 'original-model'
  const settings = vi.fn(async () => ({ schema: 'radian.settings-status.v1' as const, settings_file: '/fixture/settings.json', configured: true, provider: 'script', model: modelName, base_url: null, api_key_env: null }))
  const api = adapter({ settingsStatus: settings, streamUserStrategyMessage: send })
  const page = (version: number) => <MemoryRouter><Assistant api={api} strategy={original} changed={() => {}} modelVersion={version} onConfigureModel={configure} /></MemoryRouter>
  const view = render(page(0))
  await screen.findByText('original-model')
  const input = screen.getByLabelText('消息') as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '正在处理的问题' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  const args = send.mock.calls[0] as unknown as [string, { call_id: string }, AbortSignal]
  fireEvent.change(input, { target: { value: '保留下一问' } })
  modelName = 'newly-saved-model'
  view.rerender(page(1))
  await screen.findByText('newly-saved-model')
  expect(settings).toHaveBeenCalledTimes(2)
  expect(screen.getByLabelText('消息')).toBe(input)
  expect(input.value).toBe('保留下一问')
  expect(args[2].aborted).toBe(false)
  await act(async () => q.push({ kind: 'text_delta', call_id: args[1].call_id, text: '原请求继续回答' }))
  expect(await screen.findByText('原请求继续回答')).toBeTruthy()
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  expect(send).toHaveBeenCalledTimes(1); expect(configure).not.toHaveBeenCalled()
  await act(async () => { q.push({ kind: 'done', strategy: { ...original, calls: [call(args[1].call_id)] } }); q.end() })
})

it('streams one explicitly bounded call and real tool activity, preserving newer unsent text', async () => {
  const q = queue(), send = vi.fn(async () => q.stream()), fallback = vi.fn(), launch = vi.fn()
  const api = adapter({ streamUserStrategyMessage: send, sendUserStrategyMessage: fallback, launchUserStrategyExperiment: launch })
  open(api)
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '检查这次成本' } })
  await userEvent.click(await screen.findByRole('button', { name: '发送一次模型请求' }))
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  const request = send.mock.calls[0] as unknown as [string, { call_id: string }]
  expect(request[0]).toBe('research-one')
  expect(request[1]).toMatchObject({ base_revision: 4, text: '检查这次成本', task_budget: 1, approved_budget: { max_model_calls: 1, max_backtests: 0 } })
  await act(async () => { q.push({ kind: 'text_delta', call_id: request[1].call_id, text: '已读到费用' }); q.push({ kind: 'tool_start', tool: 'read_experiments', tool_id: 'read-1' }) })
  expect(await screen.findByText('已读到费用')).toBeTruthy()
  expect(screen.getByText(/读取正式报告.*执行中/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '尚未发送的新问题' } })
  const updated: UserStrategy = { ...original, calls: [call(request[1].call_id)], messages: [{ message_id: 'answer', call_id: request[1].call_id, role: 'model', kind: 'message', text: '冻结费用已核对', base_revision: 4, at: '2026-11-04', proposal_id: null }] }
  await act(async () => { q.push({ kind: 'tool_end', tool: 'read_experiments', tool_id: 'read-1', text: '读取成功' }); q.push({ kind: 'done', strategy: updated }); q.end() })
  expect(await screen.findByText('冻结费用已核对')).toBeTruthy()
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('尚未发送的新问题')
  expect(fallback).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('cancels the identified turn while its provider stream is still pending without claiming a settled response', async () => {
  const q = queue(), send = vi.fn(async () => q.stream())
  const cancel = vi.fn<NonNullable<ApiAdapter['resumeUserStrategyConversation']>>(async (_id, input) => (async function* () {
    yield { kind: 'done', strategy: { ...original, calls: [{ ...call(input.turn_id, 'dispatched'), task: { limit: 1, status: 'stopped', stop_reason: 'cancelled' } }] } } as UserStrategyConversationEvent
  })())
  open(adapter({ streamUserStrategyMessage: send, resumeUserStrategyConversation: cancel }))
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '检查风险' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  const id = (send.mock.calls[0] as unknown as [string, { call_id: string }])[1].call_id
  await act(async () => { q.push({ kind: 'text_delta', call_id: id, text: '部分回答' }) })
  await userEvent.click(await screen.findByRole('button', { name: '停止生成' }))
  await waitFor(() => expect(cancel).toHaveBeenCalledExactlyOnceWith('research-one', { turn_id: id, action: 'cancel' }))
  expect(screen.getByText(/停止已请求.*响应/)).toBeTruthy()
  expect(screen.getByText('部分回答')).toBeTruthy()
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => { q.push({ kind: 'done', strategy: { ...original, calls: [{ ...call(id, 'unknown'), task: { limit: 1, status: 'stopped', stop_reason: 'cancelled' } }] } }); q.end() })
  expect(await screen.findByText(/结果与用量未知/)).toBeTruthy()
  expect(send).toHaveBeenCalledTimes(1)
})

it('retains an interrupted stream identity through unmount and only reads it on reopen', async () => {
  const q = queue(), send = vi.fn(async () => q.stream()), cancel = vi.fn(async () => (async function* () {})())
  let stored = original
  const read = vi.fn(async () => stored)
  const api = adapter({ streamUserStrategyMessage: send, resumeUserStrategyConversation: cancel, getUserStrategy: read })
  const first = open(api)
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '保留原请求' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
  const args = send.mock.calls[0] as unknown as [string, { call_id: string }, AbortSignal]
  await act(async () => { q.push({ kind: 'state', strategy: { ...original, calls: [call(args[1].call_id, 'dispatched')] } }) })
  first.unmount()
  expect(args[2].aborted).toBe(true)
  expect(sessionStorage.getItem('quine.studio.calls.research-one')).toContain(args[1].call_id)
  stored = { ...original, calls: [call(args[1].call_id, 'unknown')] }
  open(api, stored)
  await screen.findByText(args[1].call_id)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
  expect(send).toHaveBeenCalledTimes(1); expect(cancel).not.toHaveBeenCalled()
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('保留原请求')
  await act(async () => q.end())
})

it('identifies every historical message from its recorded evidence and never assigns unknown history to the active report', async () => {
  const recorded = { ...call('old-call'), base_revision: 2, evidence: [{ job_id: 'report-old', draft_revision: 1 }] } as UserStrategyCall
  const message = { message_id: 'known', call_id: 'old-call', role: 'model', kind: 'message', text: '旧证据的解释', base_revision: 2, at: '2026-11-03', proposal_id: null } as UserStrategy['messages'][number]
  open(adapter(), { ...original, calls: [recorded], messages: [message, { ...message, message_id: 'orphan', call_id: 'missing', text: '来源未知的旧解释' }] }, 'report-current')
  const known = (await screen.findByText('旧证据的解释')).closest('article')!
  expect(within(known).getByRole('link', { name: /report-old.*r1/ }).getAttribute('href')).toBe('/report/report-old/overview')
  const orphan = screen.getByText('来源未知的旧解释').closest('article')!
  expect(within(orphan).getByText(/上下文未记录/)).toBeTruthy()
  expect(within(orphan).queryByText(/report-current/)).toBeNull()
})

it('treats stream exhaustion without a terminal event as unknown and blocks replay of its text', async () => {
  const stream = vi.fn(async () => (async function* () { yield { kind: 'text_delta', text: '尚未确认的输出' } as UserStrategyConversationEvent })())
  const read = vi.fn(async () => original)
  open(adapter({ streamUserStrategyMessage: stream, getUserStrategy: read }))
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '不要重发' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  expect((await screen.findByRole('alert')).textContent).toContain('响应未确认')
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.keyDown(screen.getByLabelText('消息'), { key: 'Enter', ctrlKey: true })
  fireEvent.keyDown(screen.getByLabelText('消息'), { key: 'Enter', metaKey: true })
  expect(stream).toHaveBeenCalledTimes(1)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  expect(read).toHaveBeenCalledTimes(1); expect(stream).toHaveBeenCalledTimes(1)
})

it.each(['awaiting_proposal', 'awaiting_budget'] as const)('reopens %s without resuming and explicitly ends it before another one-call question', async phase => {
  const savedCall = { ...call('original-turn'), task: { limit: 1, status: phase === 'awaiting_budget' ? 'awaiting_budget' : 'awaiting_decision', stop_reason: null } }
  let stored: UserStrategy = { ...original, calls: [savedCall], proposals: phase === 'awaiting_proposal' ? [{ proposal_id: 'already-applied', call_id: 'original-turn', base_revision: 3, status: 'applied', created_at: '2026-11-04', applied_paths: ['parameters.fast_window'], stale_paths: [], items: [] }] : [] }
  let state: UserStrategyConversation = { ...conversation, pending: { phase, turn_id: 'original-turn', proposal_id: phase === 'awaiting_proposal' ? 'already-applied' : null, base_revision: 3, launch_intent_id: null, preview_identity: null, job_id: null, at: '2026-11-04' }, task: { turn_id: 'original-turn', limit: 1, used: 1, remaining: 0, status: savedCall.task.status, stop_reason: phase === 'awaiting_budget' ? 'budget_exhausted' : null } }
  const stream = vi.fn(async () => (async function* () {})())
  const resume = vi.fn<NonNullable<ApiAdapter['resumeUserStrategyConversation']>>(async (_id, input) => (async function* () {
    expect(input).toEqual({ turn_id: 'original-turn', action: 'cancel' })
    stored = { ...stored, calls: [{ ...savedCall, task: { limit: 1, status: 'stopped', stop_reason: 'cancelled' } }] }
    state = { ...conversation, task: { turn_id: 'original-turn', limit: 1, used: 1, remaining: 0, status: 'stopped', stop_reason: 'cancelled' } }
    yield { kind: 'done', strategy: stored } as UserStrategyConversationEvent
  })())
  const api = adapter({ streamUserStrategyMessage: stream, resumeUserStrategyConversation: resume, userStrategyConversation: async () => state, getUserStrategy: async () => stored })
  open(api, stored)
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '一个新的明确问题' } })
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  expect(stream).not.toHaveBeenCalled(); expect(resume).not.toHaveBeenCalled()
  if (phase === 'awaiting_proposal') await screen.findAllByText('提案已应用到草稿；结束本轮后可提出新问题。')
  await userEvent.click(await screen.findByRole('button', { name: '结束本轮' }))
  await waitFor(() => expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false))
  expect(resume).toHaveBeenCalledTimes(1); expect(stream).not.toHaveBeenCalled()
})

it('sends selected frozen evidence without changing its id when the current draft has advanced', async () => {
  const stream = vi.fn<NonNullable<ApiAdapter['streamUserStrategyMessage']>>(async (_id, input) => (async function* () {
    yield { kind: 'done', strategy: { ...original, calls: [call(input.call_id)] } } as UserStrategyConversationEvent
  })())
  open(adapter({ streamUserStrategyMessage: stream }), original, 'frozen-report-v1')
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '解释所选报告' } })
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await waitFor(() => expect(stream).toHaveBeenCalledTimes(1))
  expect(stream.mock.calls[0][1]).toMatchObject({ evidence_job_ids: ['frozen-report-v1'], base_revision: 4, approved_budget: { max_model_calls: 1, max_backtests: 0 }, task_budget: 1 })
})

it('reconciles a retained identity against a settled canonical read on reopen without dispatching', async () => {
  sessionStorage.setItem('quine.studio.calls.research-one', JSON.stringify([{ call_id: 'known-outcome', base_revision: 4, text: '已有问题', stream: true }]))
  sessionStorage.setItem('quine.studio.unsent.research-one.draft', '已有问题')
  const stream = vi.fn(async () => (async function* () {})()), resume = vi.fn(async () => (async function* () {})())
  open(adapter({ streamUserStrategyMessage: stream, resumeUserStrategyConversation: resume }), { ...original, calls: [call('known-outcome', 'unknown')] })
  await waitFor(() => expect(sessionStorage.getItem('quine.studio.calls.research-one')).toBe('[]'))
  expect(screen.getByText(/结果与用量未知/)).toBeTruthy()
  expect(stream).not.toHaveBeenCalled(); expect(resume).not.toHaveBeenCalled()
})

it('offers a read-only retry when conversation recovery fails instead of remaining in a loading state', async () => {
  const read = vi.fn().mockRejectedValueOnce(new Error('conversation unavailable')).mockResolvedValue(conversation)
  const stream = vi.fn(async () => (async function* () {})())
  open(adapter({ userStrategyConversation: read, streamUserStrategyMessage: stream }))
  expect(await screen.findByText('会话暂不可读，请重新读取。')).toBeTruthy()
  expect(screen.queryByText('正在读取会话状态…')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '重新读取会话' }))
  expect(await screen.findByText('等待输入')).toBeTruthy()
  expect(stream).not.toHaveBeenCalled()
})

it('does not call an unchanged task a confirmed stop', async () => {
  const running = { ...original, calls: [{ ...call('running-turn', 'dispatched'), task: { limit: 1, status: 'running', stop_reason: null } }] }
  const resume = vi.fn(async () => (async function* () { yield { kind: 'done', strategy: running } as UserStrategyConversationEvent })())
  open(adapter({ streamUserStrategyMessage: vi.fn(async () => (async function* () {})()), resumeUserStrategyConversation: resume }), running)
  await userEvent.click(await screen.findByRole('button', { name: '结束本轮' }))
  expect((await screen.findByRole('alert')).textContent).toContain('停止回执尚未确认')
  expect(screen.queryByText(/停止已请求/)).toBeNull()
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
})

it.each(['awaiting_confirmation', 'awaiting_proposal', 'awaiting_observation'] as const)('handles legacy non-task %s without issuing an unsupported task cancel or dispatching', async phase => {
  const old = { ...original, calls: [call('legacy-turn')], proposals: [{ proposal_id: 'rejected-proposal', call_id: 'legacy-turn', base_revision: 3, status: 'rejected', created_at: '2026-11-03', applied_paths: [], stale_paths: [], items: [] }] } as UserStrategy
  let state: UserStrategyConversation = { ...conversation, pending: { phase, turn_id: 'legacy-turn', proposal_id: 'rejected-proposal', base_revision: 3, launch_intent_id: 'original-launch', preview_identity: 'original-preview', job_id: phase === 'awaiting_observation' ? 'original-run' : null, at: '2026-11-03' } }
  const stream = vi.fn(async () => (async function* () {})()), launch = vi.fn()
  const resume = vi.fn<NonNullable<ApiAdapter['resumeUserStrategyConversation']>>(async (_id, input) => (async function* () {
    expect(input).toMatchObject({ turn_id: 'legacy-turn', action: phase === 'awaiting_proposal' ? 'rejected' : 'declined' })
    state = conversation
    yield { kind: 'done', strategy: old } as UserStrategyConversationEvent
  })())
  open(adapter({ streamUserStrategyMessage: stream, resumeUserStrategyConversation: resume, launchUserStrategyExperiment: launch, userStrategyConversation: async () => state }), old)
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '下一次独立提问' } })
  expect(screen.queryByRole('button', { name: '结束本轮' })).toBeNull()
  if (phase === 'awaiting_observation') {
    await screen.findAllByText(/历史会话没有可停止的任务记录/)
    expect(screen.getByRole('link', { name: '查看原实验' }).getAttribute('href')).toBe('/research/research-one/experiments')
    expect(resume).not.toHaveBeenCalled()
  } else {
    await userEvent.click(await screen.findByRole('button', { name: phase === 'awaiting_proposal' ? '结束已拒绝的旧会话' : '取消原实验确认' }))
    await waitFor(() => expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false))
    expect(resume).toHaveBeenCalledTimes(1)
  }
  expect(stream).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('requires a separate explicit decline after reconciling an applied legacy proposal', async () => {
  const old = { ...original, calls: [call('legacy-applied')], proposals: [{ proposal_id: 'applied', call_id: 'legacy-applied', base_revision: 3, status: 'applied', created_at: '2026-11-03', applied_paths: ['parameters.fast_window'], stale_paths: [], items: [] }] } as UserStrategy
  let state: UserStrategyConversation = { ...conversation, pending: { phase: 'awaiting_proposal', turn_id: 'legacy-applied', proposal_id: 'applied', base_revision: 3, launch_intent_id: null, preview_identity: null, job_id: null, at: '2026-11-03' } }
  const stream = vi.fn(async () => (async function* () {})()), launch = vi.fn()
  const resume = vi.fn<NonNullable<ApiAdapter['resumeUserStrategyConversation']>>(async (_id, input) => (async function* () {
    if (input.action === 'applied') state = { ...state, pending: { ...state.pending!, phase: 'awaiting_confirmation', launch_intent_id: 'old-intent', preview_identity: 'frozen-preview' } }
    else { expect(input.action).toBe('declined'); state = conversation }
    yield { kind: 'done', strategy: old } as UserStrategyConversationEvent
  })())
  open(adapter({ streamUserStrategyMessage: stream, resumeUserStrategyConversation: resume, launchUserStrategyExperiment: launch, userStrategyConversation: async () => state }), old)
  await userEvent.click(await screen.findByRole('button', { name: '核对已应用提案' }))
  await screen.findByRole('button', { name: '取消原实验确认' })
  expect(resume).toHaveBeenCalledExactlyOnceWith('research-one', { turn_id: 'legacy-applied', action: 'applied', proposal_id: 'applied' })
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.click(screen.getByRole('button', { name: '取消原实验确认' }))
  await waitFor(() => expect(resume).toHaveBeenCalledTimes(2))
  expect(resume.mock.calls[1][1]).toEqual({ turn_id: 'legacy-applied', action: 'declined' })
  expect(stream).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})
