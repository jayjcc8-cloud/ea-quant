import { researchCollection } from '../preview/research-fixtures'
/**
 * The conversation panel's own contract, over a scripted stream.
 *
 * Every event here is hand-written fixture data, not a model output and not an
 * engine result. What these tests pin is the panel's behaviour: real deltas are
 * shown as they arrive and never invented, the canonical record is what remains
 * after a turn, applying a proposal resumes the turn to a *separate* confirmation
 * that starts nothing, the confirmation carries the frozen identity unchanged, and
 * a stream that belongs to a strategy the reader has left can never write into the
 * strategy they are looking at now.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { App, type ApiAdapter, type BacktestJob, type BacktestReport } from './app'
import { UserStrategyRequestError } from './user-strategies-api'
import type {
  UserStrategy,
  UserStrategyConversation,
  UserStrategyConversationEvent,
  UserStrategyConversationPending,
  UserStrategyExperiment,
  UserStrategyPreview,
  UserStrategyResumeRequest,
} from './user-strategies-api'

const CRITERION = {
  schema: 'radian.report-criterion.v1',
  field: 'economics.net_pnl.amount',
  expectation: 'positive',
} as const

const PREVIEW_IDENTITY = `sha256:${'a'.repeat(64)}`
const INTENT = 'int-1'

const preview: UserStrategyPreview = {
  draft_revision: 2,
  strategy_id: 'bounded-long-v1',
  scenario_id: 'bounded-long.yaml',
  venue: 'XNAS',
  symbol: 'AAPL',
  initial_cash: '10000',
  parameters: { target_quantity: '3' },
  record_count: 4,
  data: {
    path: 'prices.csv',
    start_utc: '2024-01-02T09:31:00.000000Z',
    end_utc: '2024-01-05T09:34:00.000000Z',
    fingerprint: { sha256: 'a'.repeat(64), record_count: 4 },
  },
  input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
  input_sha256: 'c'.repeat(64),
  preview_identity: PREVIEW_IDENTITY,
  normalized: {
    strategy: { target_quantity: '3' },
    execution: {
      policy: 'phase1.next-bar-close.v1',
      commission: { policy: 'deterministic-commission-v1', commission_bps: '100' },
      slippage: { policy: 'deterministic-slippage-v1', slippage_bps: '100' },
      latency: { policy: 'deterministic-latency-v1', latency_ms: 60000 },
    },
    risk: { max_order_quantity: '3' },
    funding: { currency: 'USD' },
    instrument: { venue: 'XNAS', symbol: 'AAPL' },
    data: { path: 'prices.csv', start_utc: '2024-01-02T09:31:00.000000Z', end_utc: '2024-01-05T09:34:00.000000Z' },
  },
  commission: { policy: 'deterministic-commission-v1', commission_bps: '100' },
  slippage: { policy: 'deterministic-slippage-v1', slippage_bps: '100' },
  latency: { policy: 'deterministic-latency-v1', latency_ms: 60000 },
  criterion: CRITERION,
  budget: { max_model_calls: 10, max_backtests: 5 },
}

function strategy(overrides: Partial<UserStrategy> = {}): UserStrategy {
  return {
    schema: 'ea.user-strategy.v1',
    user_strategy_id: 'us-1',
    created_at: '2026-10-06T10:00:00.000000Z',
    updated_at: '2026-10-06T10:00:00.000000Z',
    name: 'Bold entry',
    intent: 'Test one registered entry on the fixture data',
    draft: {
      revision: 1,
      strategy_id: 'bounded-long-v1',
      venue: 'XNAS',
      symbol: 'AAPL',
      scenario_id: 'bounded-long.yaml',
      dataset_id: null,
      source_sha256: null,
      initial_cash: '10000',
      parameters: { target_quantity: '2' },
      criterion: CRITERION,
    },
    history: [],
    messages: [],
    calls: [],
    proposals: [],
    changes: [],
    experiments: [],
    ...overrides,
  }
}

const conversation = (overrides: Partial<UserStrategyConversation> = {}): UserStrategyConversation => ({
  strategy_id: 'us-1',
  revision: 1,
  pending: null,
  preview: null,
  open_proposals: [],
  budget: { max_model_calls_per_turn: 4, reserved_model_calls: 1, max_backtests: 5 },
  ...overrides,
})

/** One scripted stream the test drives by hand, so ordering is the test's own. */
function queue() {
  const events: UserStrategyConversationEvent[] = []
  let wake: (() => void) | null = null
  let closed = false
  return {
    push(event: UserStrategyConversationEvent) {
      events.push(event)
      wake?.()
      wake = null
    },
    end() {
      closed = true
      wake?.()
      wake = null
    },
    async *source(): AsyncGenerator<UserStrategyConversationEvent> {
      for (;;) {
        const next = events.shift()
        if (next) {
          yield next
          continue
        }
        if (closed) return
        await new Promise<void>(resolve => { wake = resolve })
      }
    },
  }
}

const asked: UserStrategyResumeRequest[] = []
const sent: { call_id: string; text: string; approved: { max_model_calls: number; max_backtests: number } | undefined }[] = []
const launched: string[] = []
const signals: AbortSignal[] = []

function scripted(input: {
  strategies: UserStrategy[]
  conversation: () => UserStrategyConversation
  send?: () => AsyncGenerator<UserStrategyConversationEvent>
  resume?: () => AsyncGenerator<UserStrategyConversationEvent>
}): ApiAdapter {
  let persisted = input.strategies[0]
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    settingsStatus: async () => ({
      schema: 'radian.settings-status.v1',
      settings_file: '/fixture/settings.json',
      configured: true,
      provider: 'fixture',
      model: 'fixture-model',
      base_url: null,
      api_key_env: null,
    }),
    listScenarios: async () => [],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { throw new Error('unused') },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => { throw new Error('unused') },
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    listResearch: async query => researchCollection(input.strategies, query),
    researchReferences: async () => input.strategies,
    getUserStrategy: async id => input.strategies.find(item => item.user_strategy_id === id) ?? persisted,
    userStrategyConversation: async id => ({
      ...input.conversation(),
      strategy_id: id,
    }),
    streamUserStrategyMessage: async (id, body, signal) => {
      if (signal) signals.push(signal)
      sent.push({ call_id: body.call_id, text: body.text, approved: body.approved_budget })
      persisted = { ...persisted, user_strategy_id: id }
      return input.send!()
    },
    resumeUserStrategyConversation: async (_id, body, signal) => {
      if (signal) signals.push(signal)
      asked.push(body)
      if (body.turn_id === 'turn-1' && body.action === 'applied') {
        // The one scenario where the second scripted stream is needed: the caller
        // switches `resume` before this resolves.
      }
      return input.resume!()
    },
    applyUserStrategyProposal: async () => { throw new Error('unused') },
    rejectUserStrategyProposal: async () => { throw new Error('unused') },
    previewUserStrategyExperiment: async () => preview,
    launchUserStrategyExperiment: async (_id, body) => {
      launched.push(body.launch_intent_id)
      return persisted
    },
  }
  return api
}

function mount(api: ApiAdapter, id = 'us-1') {
  window.history.pushState({}, '', `/strategies/${id}`)
  return render(<App api={api} />)
}

async function chat(): Promise<HTMLElement> {
  // The empty-state panel carries the same accessible name, so the composer is what
  // tells the open strategy's own conversation from the placeholder.
  const field = await screen.findByLabelText('消息')
  return field.closest('section') as HTMLElement
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  const panel = await chat()
  await user.type(within(panel).getByLabelText('消息'), text)
  await user.click(within(panel).getByRole('button', { name: '发送消息' }))
  return panel
}

beforeEach(() => {
  asked.length = 0
  sent.length = 0
  launched.length = 0
  signals.length = 0
})

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  window.history.pushState({}, '', '/backtests')
})

describe('the streamed conversation panel', () => {
  it('shows each delta as it arrives, then the canonical answer the turn recorded', async () => {
    const user = userEvent.setup()
    const stream = queue()
    const answered = strategy({
      calls: [{
        call_id: 'turn-1',
        turn_id: 'turn-1',
        base_revision: 1,
        status: 'observed',
        route: { provider: 'fixture', model: 'fixture-model', endpoint: 'https://fixture.invalid' },
        approved_budget: { max_model_calls: 2, max_backtests: 0 },
        reserved_at: '2026-10-06T10:01:00.000000Z',
        message: null,
        model: 'fixture-model',
        input_tokens: 11,
        output_tokens: 7,
        usage_available: true,
      }],
      messages: [{
        message_id: 'm-1',
        role: 'user',
        kind: 'message',
        text: '请解释这次实验',
        base_revision: 1,
        at: '2026-10-06T10:01:00.000000Z',
        call_id: 'turn-1',
        proposal_id: null,
      }, {
        message_id: 'm-turn-1',
        role: 'model',
        kind: 'message',
        text: '这是最终回答。',
        base_revision: 1,
        at: '2026-10-06T10:01:30.000000Z',
        call_id: 'turn-1',
        proposal_id: null,
      }],
    })
    const api = scripted({
      strategies: [strategy()],
      conversation: () => conversation(),
      send: () => stream.source(),
    })
    mount(api)
    await ask(user, '请解释这次实验')

    // The first deltas are shown while the turn is still open — no terminal event
    // has arrived, and nothing was invented to fill the answer in.
    stream.push({ kind: 'text_delta', text: '这是' })
    expect(await screen.findByText('这是')).toBeTruthy()
    stream.push({ kind: 'tool_start', tool: 'describe_data', tool_id: 'read-1' })
    expect(await screen.findByText(/读取数据说明/)).toBeTruthy()
    stream.push({ kind: 'tool_end', tool: 'describe_data', tool_id: 'read-1', text: '数据说明' })
    await waitFor(() => expect(screen.getByText(/已完成/)).toBeTruthy())

    stream.push({ kind: 'text_delta', text: '最终回答。' })
    expect(await screen.findByText('这是最终回答。')).toBeTruthy()
    // The streamed text is not yet the record: the turn has not ended.
    expect(screen.queryByText('这是最终回答。', { selector: '.answer-scroll p' })).toBeTruthy()

    stream.push({ kind: 'done', strategy: answered })
    stream.end()
    // The canonical record is what remains, and the partial live view is gone.
    await waitFor(() => expect(screen.getByText('这是最终回答。', { selector: '.turn-answer p' })).toBeTruthy())
    await waitFor(() => expect(screen.queryByText(/正在生成，尚未完成/)).toBeNull())
    expect(sent[0].approved).toBeUndefined()
    expect(sent[0].call_id.startsWith('turn-')).toBe(true)
  })

  it('keeps a disconnected new turn unknown when the previous turn completed and reread fails', async () => {
    const user = userEvent.setup()
    let disconnected = false
    const api = scripted({
      strategies: [strategy()],
      conversation: () => {
        if (disconnected) throw new UserStrategyRequestError('request_failed', '服务已关闭')
        return conversation({ task: { turn_id: 'previous', limit: 1, used: 1, remaining: 0,
          status: 'stopped', stop_reason: 'completed' } })
      },
      send: async function* () {
        yield { kind: 'text_delta', text: '已经收到的部分回答' }
        disconnected = true
        throw new UserStrategyRequestError('request_failed', '连接已中断')
      },
    })
    mount(api)
    const panel = await ask(user, '请检查这次输入')
    await waitFor(() => expect(within(panel).getByText(/草稿 v1 · 响应未确认/)).toBeTruthy())
    expect(within(panel).getByText('已经收到的部分回答')).toBeTruthy()
    expect(within(panel).queryByText(/草稿 v1 · 已完成/)).toBeNull()
    expect(within(panel).queryByText('生成中')).toBeNull()
    expect(within(panel).queryByText(/什么都没有改变/)).toBeNull()
    expect(within(panel).getByRole('button', { name: '发送消息' }).hasAttribute('disabled')).toBe(true)
    expect(sent).toHaveLength(1)
  })

  it('resumes an applied proposal to a separate confirmation and starts nothing', async () => {
    const stream = queue()
    const applied = strategy({
      draft: { ...(strategy().draft as Exclude<UserStrategy['draft'], { kind: 'intent' }>), revision: 2 },
      proposals: [{
        proposal_id: 'prop-1',
        call_id: 'turn-1',
        base_revision: 1,
        status: 'applied',
        created_at: '2026-10-06T10:01:00.000000Z',
        items: [{
          path: 'parameters.target_quantity', before: '2', after: '3',
          reason: 'A wider size.', evidence: [], proposer: 'model',
        }],
        applied_paths: ['parameters.target_quantity'],
        stale_paths: [],
      }],
    })
    let waiting: UserStrategyConversation = conversation({
      revision: 2,
      pending: {
        phase: 'awaiting_proposal',
        turn_id: 'turn-1',
        proposal_id: 'prop-1',
        base_revision: 1,
        launch_intent_id: null,
        preview_identity: null,
        job_id: null,
        at: '2026-10-06T10:01:00.000000Z',
      },
    })
    const api = scripted({
      strategies: [applied],
      conversation: () => waiting,
      resume: () => stream.source(),
    })
    mount(api)

    // The panel sees the durable records — the proposal is applied and the turn is
    // still waiting for that decision — and carries the decision to it, once.
    await waitFor(() => expect(asked).toHaveLength(1))
    expect(asked[0]).toEqual({ turn_id: 'turn-1', action: 'applied', proposal_id: 'prop-1' })

    // The turn answers with its own separate confirmation, and no run exists.
    waiting = conversation({
      revision: 2,
      pending: {
        phase: 'awaiting_confirmation',
        turn_id: 'turn-1',
        proposal_id: 'prop-1',
        base_revision: 2,
        launch_intent_id: INTENT,
        preview_identity: PREVIEW_IDENTITY,
        job_id: null,
        at: '2026-10-06T10:02:00.000000Z',
      },
      preview,
    })
    stream.push({ kind: 'interrupt', waiting: { kind: 'confirmation', turn_id: 'turn-1', launch_intent_id: INTENT, base_revision: 2, preview_identity: PREVIEW_IDENTITY, preview }, strategy: applied })
    stream.end()

    const card = await screen.findByRole('region', { name: '实验确认' })
    expect(within(card).getByText('确认并运行一次回测')).toBeTruthy()
    expect(within(card).getByText(/冻结修订/)).toBeTruthy()
    expect(within(card).getByText(/本次确认消耗 1 次回测/)).toBeTruthy()
    expect(launched).toEqual([])
    // One decision per turn: the effect does not repeat itself.
    expect(asked).toHaveLength(1)
  })

  it('carries the frozen intent, revision and preview unchanged into the confirmation', async () => {
    const user = userEvent.setup()
    const stream = queue()
    const pending = {
      phase: 'awaiting_confirmation' as const,
      turn_id: 'turn-1',
      proposal_id: 'prop-1',
      base_revision: 2,
      launch_intent_id: INTENT,
      preview_identity: PREVIEW_IDENTITY,
      job_id: null,
      at: '2026-10-06T10:02:00.000000Z',
    }
    const api = scripted({
      strategies: [strategy()],
      conversation: () => conversation({ revision: 2, pending, preview }),
      resume: () => stream.source(),
    })
    mount(api)

    const card = await screen.findByRole('region', { name: '实验确认' })
    await user.click(within(card).getByRole('button', { name: '确认并运行一次回测' }))

    await waitFor(() => expect(asked).toHaveLength(1))
    expect(asked[0]).toEqual({
      turn_id: 'turn-1',
      action: 'confirm',
      launch_intent_id: INTENT,
      base_revision: 2,
      preview_identity: PREVIEW_IDENTITY,
    })
    stream.push({ kind: 'done', strategy: strategy({ draft: { ...(strategy().draft as Exclude<UserStrategy['draft'], { kind: 'intent' }>), revision: 2 } }) })
    stream.end()
    await waitFor(() => expect(screen.queryByRole('region', { name: '实验确认' })).toBeNull())
  })

  it('shows earlier turns again and groups a multi-call turn by its own identity', async () => {
    const stream = queue()
    const multi = strategy({
      calls: [{
        call_id: 'turn-1',
        turn_id: 'turn-1',
        base_revision: 1,
        status: 'observed',
        route: { provider: 'fixture', model: 'fixture-model', endpoint: null },
        approved_budget: { max_model_calls: 4, max_backtests: 0 },
        reserved_at: '2026-10-06T10:03:00.000000Z',
        message: null,
        model: 'fixture-model',
        input_tokens: null,
        output_tokens: null,
        usage_available: false,
      }, {
        call_id: 'turn-1-s1-abcd',
        turn_id: 'turn-1',
        base_revision: 1,
        status: 'observed',
        route: { provider: 'fixture', model: 'fixture-model', endpoint: null },
        approved_budget: { max_model_calls: 4, max_backtests: 0 },
        reserved_at: '2026-10-06T10:03:10.000000Z',
        message: null,
        model: 'fixture-model',
        input_tokens: null,
        output_tokens: null,
        usage_available: false,
      }],
      messages: [
        {
          message_id: 'old-user', role: 'user', kind: 'message', text: '更早的问题',
          base_revision: 1, at: '2026-10-06T10:00:00.000000Z', call_id: 'turn-0', proposal_id: null,
        },
        {
          message_id: 'old-model', role: 'model', kind: 'message', text: '更早的回答',
          base_revision: 1, at: '2026-10-06T10:00:30.000000Z', call_id: 'turn-0', proposal_id: null,
        },
        {
          message_id: 'now-user', role: 'user', kind: 'message', text: '本轮的问题',
          base_revision: 1, at: '2026-10-06T10:03:00.000000Z', call_id: 'turn-1', proposal_id: null,
        },
        {
          message_id: 'read-model', role: 'model', kind: 'message', text: '本轮读取数据后的说明',
          base_revision: 1, at: '2026-10-06T10:03:05.000000Z', call_id: 'turn-1', proposal_id: null,
        },
        {
          message_id: 'now-model', role: 'model', kind: 'message', text: '本轮的回答',
          base_revision: 1, at: '2026-10-06T10:03:20.000000Z', call_id: 'turn-1-s1-abcd', proposal_id: null,
        },
      ],
    })
    multi.calls[0].tool_results = [{tool: 'discover_resources', tool_id: 'read-catalog', status: 'done', text: '{"ok":true,"datasets":[]}' }]
    const api = scripted({
      strategies: [multi],
      conversation: () => conversation(),
      send: () => stream.source(),
    })
    mount(api)

    const panel = await chat()
    // The turn that made two calls shows both, and the answer of its second call.
    expect(within(panel).getByText('已观测')).toBeTruthy()
    expect(within(panel).getByText('本轮的回答')).toBeTruthy()
    expect(within(panel).getByText('本轮读取数据后的说明')).toBeTruthy()
    expect(within(panel).getByText('✓ 发现策略与数据 · 已完成')).toBeTruthy()
    expect(within(panel).getByText('{"ok":true,"datasets":[]}')).toBeTruthy()
    // A reload after a later provider failure must show that failure, even though
    // the root call of the same turn succeeded.
    cleanup()
    multi.calls[1] = { ...multi.calls[1], status: 'unknown', message: '最后一次模型调用的结果未确认' }
    multi.messages = multi.messages.filter(item => item.message_id !== 'now-model')
    mount(scripted({ strategies: [multi], conversation: () => conversation() }))
    const reopened = await chat()
    expect(within(reopened).getByText('最后一次模型调用的结果未确认')).toBeTruthy()
    expect(within(reopened).getByText('本轮读取数据后的说明')).toBeTruthy()
    // The earlier conversation is still there, one disclosure away.
    expect(within(reopened).getByText('更早的问题')).toBeTruthy()
    expect(within(reopened).getByText('更早的回答')).toBeTruthy()
  })

  it('allows a new bounded turn after more than 200 recorded calls', async () => {
    const user = userEvent.setup()
    const stream = queue()
    const api = scripted({
      strategies: [strategy()],
      conversation: () => conversation({
        budget: { max_model_calls_per_turn: 4, reserved_model_calls: 220, max_backtests: 5 },
      }),
      send: () => stream.source(),
    })
    mount(api)
    const panel = await chat()
    await user.type(within(panel).getByLabelText('消息'), '继续研究')
    expect(within(panel).queryByText('工作区累计预留 220 次模型调用')).toBeNull()
    const send = within(panel).getByRole('button', { name: '发送消息' }) as HTMLButtonElement
    expect(send.disabled).toBe(false)
    await user.click(send)
    expect(sent).toHaveLength(1)
    expect(sent[0].approved).toBeUndefined()
    stream.push({ kind: 'done', strategy: strategy() })
    stream.end()
  })

  it('aborts a stream when the reader leaves, so another strategy is never written to', async () => {
    const stream = queue()
    const other = strategy({ user_strategy_id: 'us-2', name: 'Second idea' })
    const api = scripted({
      strategies: [strategy(), other],
      conversation: () => conversation(),
      send: () => stream.source(),
    })
    const user = userEvent.setup()
    mount(api)
    await ask(user, '请解释这次实验')
    stream.push({ kind: 'text_delta', text: '这段回答属于第一条策略' })
    expect(await screen.findByText('这段回答属于第一条策略')).toBeTruthy()
    expect(signals[0].aborted).toBe(false)

    // The reader switches strategies. The turn's own stream is cancelled, and the
    // events that were already on their way cannot reach the new strategy.
    await chat()
    await act(async()=>{window.history.pushState({},'', '/strategies/us-2');window.dispatchEvent(new PopStateEvent('popstate'))})

    await waitFor(() => expect(signals[0].aborted).toBe(true))
    stream.push({ kind: 'text_delta', text: '这段回答不应该出现在第二条策略里' })
    stream.push({ kind: 'done', strategy: strategy() })
    stream.end()
    await waitFor(() => expect(screen.queryByText('这段回答不应该出现在第二条策略里')).toBeNull())
    expect(screen.queryByText('这段回答属于第一条策略')).toBeNull()
  })

  it('keeps watching its own confirmed run by asking about the same intent', { timeout: 15000 }, async () => {
    const stream = queue()
    const pending = {
      phase: 'awaiting_observation' as const,
      turn_id: 'turn-1',
      proposal_id: 'prop-1',
      base_revision: 2,
      launch_intent_id: INTENT,
      preview_identity: PREVIEW_IDENTITY,
      job_id: 'job-1',
      at: '2026-10-06T10:03:00.000000Z',
    }
    const api = scripted({
      strategies: [strategy()],
      conversation: () => conversation({ revision: 2, pending }),
      resume: () => stream.source(),
    })
    mount(api)

    const card = await screen.findByRole('region', { name: '实验观察' })
    expect(within(card).getByText(/job-1/)).toBeTruthy()
    expect((screen.getByRole('button',{name:'发送消息'}) as HTMLButtonElement).disabled).toBe(true)
    // The polite reader asks again, one query at a time, and never a second launch.
    await waitFor(() => expect(asked.length).toBeGreaterThanOrEqual(1), { timeout: 9000 })
    expect(asked.every(item => item.action === 'observe' && item.turn_id === 'turn-1')).toBe(true)
    expect(launched).toEqual([])
    stream.push({ kind: 'state', job_id: 'job-1', run_status: 'running' })
    stream.end()
  })
})


describe('completed conversation experiment evidence', () => {
  it.each(['succeeded', 'failed'] as const)('rereads only its completed job without reload (%s)', async status => {
    const user = userEvent.setup()
    const stream = queue()
    const experiment: UserStrategyExperiment = {
      launch_intent_id: INTENT, request_id: 'request-1', payload_sha256: 'a'.repeat(64),
      payload: { draft_revision: 1, scenario_id: 'bounded-long.yaml', parameters: { target_quantity: '2' } },
      task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00Z',
    }
    const value = strategy({ experiments: [experiment] })
    let pending: UserStrategyConversationPending | null = {
      phase: 'awaiting_confirmation', turn_id: 'turn-1', proposal_id: 'prop-1', base_revision: 1,
      launch_intent_id: INTENT, preview_identity: PREVIEW_IDENTITY, job_id: null, at: '2026-10-06T10:02:00Z',
    }
    let terminal = false
    const reads: string[] = []
    const reports: string[] = []
    const api = scripted({ strategies: [value], conversation: () => conversation({ pending, preview }), resume: () => stream.source() })
    api.getBacktest = async id => {
      reads.push(id)
      return { job_id: id, status: terminal ? status : 'running', report_ready: terminal && status === 'succeeded', engine_run_id: `run-${id}` } as BacktestJob
    }
    api.getReport = async id => { reports.push(id); return { run_id: `run-${id}` } as BacktestReport }
    mount(api)
    const panel = await chat()
    await within(panel).findByText(/所选证据尚未就绪/)
    // Existing runs open the experiment tab. The frozen status strip belongs to
    // the strategy overview, so reach it through the same visible tab as a user.
    expect(screen.getByRole('tab', { name: '实验' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('region', { name: '下一步' })).toBeNull()
    await user.click(screen.getByRole('tab', { name: '策略' }))
    const strip = screen.getByRole('region', { name: '下一步' })
    await within(strip).findByRole('link', { name: '运行中' })
    const frozen = JSON.stringify(experiment)
    await user.click(within(panel).getByRole('button', { name: '确认并运行一次回测' }))
    const before = reads.length
    terminal = true
    // Neither an unrelated job event nor an interrupt finishes this frozen intent.
    await act(async () => { stream.push({ kind: 'state', job_id: 'foreign-job', verified: true }) })
    expect(reads).toHaveLength(before)
    pending = null
    await act(async () => {
      stream.push({ kind: 'done', strategy: value })
      stream.end()
    })
    await waitFor(() => expect(reads.length).toBeGreaterThan(before))
    expect(reads.every(id => id === 'job-1')).toBe(true)
    await within(strip).findByRole('link', { name: status === 'succeeded' ? '成功 · 报告可读' : '失败' })
    expect(strip.textContent).toContain('v1')
    expect(strip.querySelector('time')?.getAttribute('datetime')).toBe(experiment.created_at)
    expect(JSON.stringify(experiment)).toBe(frozen)
    if (status === 'succeeded') {
      await waitFor(() => expect(within(panel).queryByText(/所选证据尚未就绪/)).toBeNull())
      expect(reports).toEqual(['job-1'])
      await user.type(within(panel).getByLabelText('消息'), '解读已完成实验')
      expect((within(panel).getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
    } else {
      expect(within(panel).getByText(/所选证据尚未就绪/)).toBeTruthy()
      expect(reports).toEqual([])
    }
    expect(sent).toEqual([])
    expect(launched).toEqual([])
  })
})


it('keeps the saved turn and applied proposal mounted during a resumed explanation', async () => {
  const user = userEvent.setup()
  const stream = queue()
  const saved = strategy({
    calls: [{ call_id: 'turn-1', turn_id: 'turn-1', base_revision: 1, status: 'observed',
      route: { provider: 'fixture', model: 'fixture', endpoint: null }, approved_budget: { max_model_calls: 4, max_backtests: 0 },
      reserved_at: '2026-10-06T10:01:00Z', message: null, model: 'fixture', input_tokens: null, output_tokens: null, usage_available: false }],
    messages: [
      { message_id: 'u1', role: 'user', kind: 'message', text: '原始问题', base_revision: 1, at: '2026-10-06T10:01:00Z', call_id: 'turn-1', proposal_id: null },
      { message_id: 'm1', role: 'model', kind: 'message', text: '已保存的提案解释', base_revision: 1, at: '2026-10-06T10:01:01Z', call_id: 'turn-1', proposal_id: null },
    ],
    proposals: [{ proposal_id: 'prop-1', call_id: 'turn-1', base_revision: 1, status: 'applied', created_at: '2026-10-06T10:01:01Z',
      items: [{ path: 'parameters.target_quantity', before: '2', after: '3', reason: 'fixture', evidence: [], proposer: 'model' }],
      applied_paths: ['parameters.target_quantity'], stale_paths: [] }],
  })
  const pending: UserStrategyConversationPending = { phase: 'awaiting_confirmation', turn_id: 'turn-1', proposal_id: 'prop-1', base_revision: 1,
    launch_intent_id: INTENT, preview_identity: PREVIEW_IDENTITY, job_id: null, at: '2026-10-06T10:02:00Z' }
  const api = scripted({ strategies: [saved], conversation: () => conversation({ pending, preview }), resume: () => stream.source() })
  mount(api)
  const answer = await screen.findByText('已保存的提案解释')
  const proposal = screen.getByRole('region', { name: '建议修改' })
  await user.click(await screen.findByRole('button', { name: '确认并运行一次回测' }))
  await act(async () => { stream.push({ kind: 'text_delta', text: '这是实验后的续写' }) })
  expect(await screen.findByText('这是实验后的续写')).toBeTruthy()
  expect(screen.getByText('已保存的提案解释')).toBe(answer)
  expect(screen.getByRole('region', { name: '建议修改' })).toBe(proposal)
  expect(screen.getAllByText('原始问题')).toHaveLength(1)
  stream.end()
})


it('keeps a departed research late terminal job read out of the current frozen strip', async () => {
  const user = userEvent.setup()
  const stream = queue()
  const first: UserStrategyExperiment = {
    launch_intent_id: INTENT, request_id: 'request-1', payload_sha256: 'a'.repeat(64),
    payload: { draft_revision: 1, scenario_id: 'bounded-long.yaml', parameters: { target_quantity: '2' } },
    task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00Z',
  }
  const second: UserStrategyExperiment = { ...first, launch_intent_id: 'int-2', job_id: 'job-2', payload: { ...first.payload, draft_revision: 7 } }
  const value = strategy({ experiments: [first] })
  const other = strategy({ user_strategy_id: 'us-2', name: 'Second idea', experiments: [second] })
  let pending: UserStrategyConversationPending | null = {
    phase: 'awaiting_confirmation', turn_id: 'turn-1', proposal_id: 'prop-1', base_revision: 1,
    launch_intent_id: INTENT, preview_identity: PREVIEW_IDENTITY, job_id: null, at: '2026-10-06T10:02:00Z',
  }
  const late: ((value: BacktestJob) => void)[] = []
  let terminal = false
  const api = scripted({ strategies: [value, other], conversation: () => conversation({ pending, preview }), resume: () => stream.source() })
  api.getBacktest = async id => {
    if (id === 'job-1' && terminal) return new Promise<BacktestJob>(resolve => { late.push(resolve) })
    return { job_id: id, status: id === 'job-2' ? 'failed' : 'running', report_ready: false } as BacktestJob
  }
  mount(api)
  const panel = await chat()
  await user.click(screen.getByRole('tab', { name: '策略' }))
  await within(screen.getByRole('region', { name: '下一步' })).findByRole('link', { name: '运行中' })
  await user.click(within(panel).getByRole('button', { name: '确认并运行一次回测' }))
  terminal = true
  pending = null
  await act(async () => { stream.push({ kind: 'done', strategy: value }); stream.end() })
  await waitFor(() => expect(late).toHaveLength(2)) // AI report precondition and the frozen strip.
  await act(async()=>{window.history.pushState({},'', '/strategies/us-2');window.dispatchEvent(new PopStateEvent('popstate'))})
  await user.click(await screen.findByRole('tab', { name: '策略' }))
  const strip = await screen.findByRole('region', { name: '下一步' })
  const ownLink = await within(strip).findByRole('link', { name: '失败' })
  await act(async () => {
    for (const resolve of late) resolve({ job_id: 'job-1', status: 'succeeded', report_ready: true, engine_run_id: 'run-1' } as BacktestJob)
  })
  expect(within(strip).queryByRole('link', { name: /成功/ })).toBeNull()
  expect(ownLink.getAttribute('href')).toContain('/strategies/us-2?tab=experiments&evidence_a=job-2')
  expect(strip.textContent).toContain('v7')
  expect(sent).toEqual([])
  expect(launched).toEqual([])
})

it.each(['done', 'failed'] as const)('keeps a tool-only %s receipt visible on completion and reload', async status => {
  const common: UserStrategy['calls'][number] = {
    call_id: 'tool-only', turn_id: 'tool-only', base_revision: 1, status: 'observed',
    route: {provider: 'fixture', model: 'fixture', endpoint: null},
    approved_budget: {max_model_calls: 4, max_backtests: 0}, reserved_at: '2026-10-13T13:00:00Z',
    message: null, model: 'fixture', input_tokens: null, output_tokens: null, usage_available: false,
  }
  const receipt = {tool: 'discover_resources', tool_id: 'catalog-1', status,
    text: status === 'done' ? '{"ok":true,"datasets":[]}' : '{"ok":false,"error":"catalog unavailable"}'}
  const saved = strategy({
    calls: [{...common, tool_results: [receipt]}, {...common, call_id: 'after-tool'}],
    messages: [
      {message_id: 'question', role: 'user', kind: 'message', text: 'Find the actual datasets.',
        base_revision: 1, at: common.reserved_at, call_id: common.call_id, proposal_id: null},
      {message_id: 'answer', role: 'model', kind: 'message', text: 'Catalog read complete.',
        base_revision: 1, at: common.reserved_at, call_id: 'after-tool', proposal_id: null},
    ],
  })
  const api = scripted({strategies: [saved], conversation: () => conversation()})
  for (let reload = 0; reload < 2; reload++) {
    mount(api)
    const panel = await chat()
    const label = status === 'done' ? '✓ 发现策略与数据 · 已完成' : '! 发现策略与数据 · 失败'
    const step = within(panel).getByText(label)
    const answer = within(panel).getByText('Catalog read complete.')
    expect(within(panel).getAllByText(label)).toHaveLength(1)
    expect(within(panel).getByText(receipt.text)).toBeTruthy()
    expect(step.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    cleanup()
  }
})

it('continues a persisted legacy pause without a numeric authorization form', async () => {
  const user = userEvent.setup()
  const stream = queue()
  const state = conversation({task:{turn_id:'task-1',limit:6,used:6,remaining:0,status:'awaiting_budget',stop_reason:'budget_exhausted'},pending:{phase:'awaiting_budget',turn_id:'task-1',proposal_id:null,base_revision:1,launch_intent_id:null,preview_identity:null,job_id:null,at:'2026-10-13T13:00:00Z'}})
  const api = scripted({strategies:[strategy()],conversation:()=>state,resume:()=>stream.source()})
  mount(api)
  const panel = await chat()
  expect(within(panel).queryByLabelText('任务总请求上限')).toBeNull()
  expect(within(panel).queryByLabelText('追加后的总请求上限')).toBeNull()
  await user.click(await within(panel).findByRole('button',{name:'授权继续（最多再 24 次请求）'}))
  expect(within(panel).getByText('发送新消息即授权最多 24 次模型请求；费用取决于模型用量。')).toBeTruthy()
  expect(within(panel).getByText('当前任务累计授权 6 次，已用 6 次，剩余 0 次。')).toBeTruthy()
  expect(asked[0]).toMatchObject({turn_id:'task-1',action:'continue'})
  expect(asked[0].grant_id).toBeTruthy()
  expect(asked[0].total_model_calls).toBeUndefined()
  stream.push({kind:'done',strategy:strategy()});stream.end()
})

it('keeps cancellation available during automatic observation after reload', async () => {
  const stream = queue()
  const pending: UserStrategyConversationPending = {phase:'awaiting_observation', turn_id:'turn-1',proposal_id:'prop-1',base_revision:2,launch_intent_id:INTENT,preview_identity:PREVIEW_IDENTITY,job_id:'job-1',at:'2026-10-13T13:00:00Z'}
  const api = scripted({strategies:[strategy()], conversation:()=>conversation({pending, task:{turn_id:'turn-1',limit:8,used:1,remaining:7,status:'awaiting_decision',stop_reason:'decision_required'}}),resume:()=>stream.source()})
  mount(api)
  const panel = await chat()
  await waitFor(()=>expect(asked.length).toBeGreaterThanOrEqual(1),{timeout:9000})
  expect(within(panel).getByRole('button',{name:'停止生成'})).toBeTruthy()
  stream.push({kind:'interrupt',waiting:{kind:'observation',turn_id:'turn-1',job_id:'job-1',status:'running'},strategy:strategy()})
  stream.end()
})

it('sends with Enter without a numeric form and keeps Shift+Enter as a newline', async () => {
  const user = userEvent.setup()
  const stream = queue()
  const api = scripted({strategies:[strategy()],conversation:()=>conversation(),send:()=>stream.source()})
  mount(api)
  const panel = await chat()
  expect(within(panel).queryByLabelText('任务总请求上限')).toBeNull()
  expect(within(panel).queryByRole('button',{name:'询问模型'})).toBeNull()
  const input = within(panel).getByLabelText('消息')
  await user.type(input,'检查输入{Shift>}{Enter}{/Shift}再解释')
  expect(sent).toHaveLength(0)
  await user.keyboard('{Enter}')
  await waitFor(()=>expect(sent).toHaveLength(1))
  expect(sent[0].text).toBe('检查输入\n再解释')
  expect(sent[0].approved).toBeUndefined()
  expect(within(panel).getByRole('button',{name:'停止生成'})).toBeTruthy()
  stream.end()
})
