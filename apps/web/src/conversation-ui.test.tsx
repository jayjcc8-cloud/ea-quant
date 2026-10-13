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
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { App, type ApiAdapter } from './app'
import type {
  UserStrategy,
  UserStrategyConversation,
  UserStrategyConversationEvent,
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
const sent: { call_id: string; text: string; approved: { max_model_calls: number; max_backtests: number } }[] = []
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
    listUserStrategies: async () => input.strategies,
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
  await user.click(within(panel).getByLabelText(/批准本轮最多/))
  await user.click(within(panel).getByRole('button', { name: '询问模型' }))
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
    await waitFor(() => expect(screen.getByText('这是最终回答。', { selector: '.answer-scroll p' })).toBeTruthy())
    await waitFor(() => expect(screen.queryByText(/正在生成，尚未完成/)).toBeNull())
    expect(sent[0].approved).toEqual({ max_model_calls: 4, max_backtests: 0 })
    expect(sent[0].call_id.startsWith('turn-')).toBe(true)
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
    const api = scripted({
      strategies: [multi],
      conversation: () => conversation(),
      send: () => stream.source(),
    })
    mount(api)

    const panel = await chat()
    // The turn that made two calls shows both, and the answer of its second call.
    expect(within(panel).getByText(/本轮共 2 次模型调用/)).toBeTruthy()
    expect(within(panel).getByText('本轮的回答')).toBeTruthy()
    expect(within(panel).getByText('本轮读取数据后的说明')).toBeTruthy()
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
    expect(within(reopened).getByText('更早的对话（2）')).toBeTruthy()
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
    await user.click(within(panel).getByLabelText(/批准本轮最多 4 次模型调用/))
    expect(within(panel).getByText('工作区累计预留 220 次模型调用')).toBeTruthy()
    const send = within(panel).getByRole('button', { name: '询问模型' }) as HTMLButtonElement
    expect(send.disabled).toBe(false)
    await user.click(send)
    expect(sent).toHaveLength(1)
    expect(sent[0].approved).toEqual({ max_model_calls: 4, max_backtests: 0 })
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
    const panel = await chat()
    await user.selectOptions(within(panel).getByLabelText('当前策略'), 'us-2')

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
    expect(screen.getByText(/运行完成后可在这里继续追问/)).toBeTruthy()
    // The polite reader asks again, one query at a time, and never a second launch.
    await waitFor(() => expect(asked.length).toBeGreaterThanOrEqual(1), { timeout: 9000 })
    expect(asked.every(item => item.action === 'observe' && item.turn_id === 'turn-1')).toBe(true)
    expect(launched).toEqual([])
    stream.push({ kind: 'state', job_id: 'job-1', run_status: 'running' })
    stream.end()
  })
})
