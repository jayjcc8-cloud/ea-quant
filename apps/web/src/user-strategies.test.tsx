/**
 * User-strategy workspace UI contract.
 *
 * The adapter below is a scripted double: every strategy document, preview fact
 * and job id in these tests is hand-written fixture data, not a model output and
 * not an engine result. The scripted "AI" answers only prove the product rules —
 * creating a strategy is a save and never a model call, every model call needs an
 * explicit approval click, a confirmation names the very preview it was made
 * from, and only an explicit click applies a change, undoes one or starts an
 * experiment.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type ScenarioSummary } from './app'
import {
  UserStrategyRequestError,
  type UserStrategy,
  type UserStrategyChange,
  type UserStrategyPreview,
  type UserStrategyProposal,
} from './user-strategies-api'

const scenario: ScenarioSummary = {
  scenario_id: 'bounded-long.yaml',
  name: 'Bounded long',
  valid: true,
  summary: {
    strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000',
    parameters: { target_quantity: '2', entry_delay_bars: 0 }, record_count: 4,
  },
}

/** The criterion exactly as the host froze it, in the report's own vocabulary. */
const CRITERION = {
  schema: 'radian.report-criterion.v1',
  field: 'economics.net_pnl.amount',
  expectation: 'positive',
} as const

/** The identity a confirmation must carry back: the admitted input, not a revision. */
const PREVIEW_IDENTITY = `sha256:${'a'.repeat(64)}`

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
      // A local scenario file has no upload behind it: the source binding is
      // absent rather than a fingerprint dressed up as where the bytes came from.
      dataset_id: null,
      source_sha256: null,
      initial_cash: '10000',
      parameters: { target_quantity: '2', entry_delay_bars: 0 },
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

function proposal(overrides: Partial<UserStrategyProposal> = {}): UserStrategyProposal {
  return {
    proposal_id: 'prop-1',
    call_id: 'call-1',
    base_revision: 1,
    status: 'open',
    created_at: '2026-10-06T10:01:00.000000Z',
    items: [
      { path: 'parameters.target_quantity', before: '2', after: '3', reason: 'A wider sample size.', evidence: ['idea: more size'], proposer: 'model' },
      { path: 'parameters.entry_delay_bars', before: 0, after: 1, reason: 'Skip the first bar.', evidence: [], proposer: 'model' },
    ],
    applied_paths: [],
    stale_paths: [],
    ...overrides,
  }
}

/** One applied change, with the provenance of every field it wrote. */
const appliedChange: UserStrategyChange = {
  operation_id: 'op-apply-1',
  kind: 'apply',
  proposal_id: 'prop-1',
  target_operation_id: null,
  from_revision: 1,
  to_revision: 2,
  applied_paths: ['parameters.target_quantity'],
  stale_paths: [],
  items: [{
    path: 'parameters.target_quantity', before: '2', after: '3',
    reason: 'A wider sample size.', evidence: ['idea: more size'], proposer: 'model',
  }],
  evidence: ['idea: more size'],
  proposer: 'model',
  at: '2026-10-06T10:03:00.000000Z',
}

const preview: UserStrategyPreview = {
  draft_revision: 1,
  strategy_id: 'bounded-long-v1',
  scenario_id: 'bounded-long.yaml',
  venue: 'XNAS',
  symbol: 'AAPL',
  initial_cash: '10000',
  parameters: { target_quantity: '2', entry_delay_bars: 0 },
  record_count: 4,
  data: {
    path: 'prices.csv', start_utc: '2024-01-02T09:31:00.000000Z', end_utc: '2024-01-05T09:34:00.000000Z',
    fingerprint: { sha256: 'a'.repeat(64), record_count: 4 },
  },
  input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
  input_sha256: 'c'.repeat(64),
  preview_identity: PREVIEW_IDENTITY,
  normalized: {
    strategy: { target_quantity: '2', entry_delay_bars: 0 },
    execution: {
      policy: 'phase1.next-bar-close.v1',
      commission: { policy: 'deterministic-commission-v1', commission_bps: '100' },
      slippage: { policy: 'deterministic-slippage-v1', slippage_bps: '100' },
      latency: { policy: 'deterministic-latency-v1', latency_ms: 60000 },
    },
    risk: { max_order_quantity: '2' },
    funding: { currency: 'USD' },
    instrument: { venue: 'XNAS', symbol: 'AAPL' },
    data: {
      path: 'prices.csv', start_utc: '2024-01-02T09:31:00.000000Z', end_utc: '2024-01-05T09:34:00.000000Z',
      fingerprint: { sha256: 'a'.repeat(64), record_count: 4 },
    },
  },
  // The scenario's own cost assumptions, exactly as admitted: policies and their
  // declared values, never a price this page invented.
  commission: { policy: 'deterministic-commission-v1', commission_bps: '100' },
  slippage: { policy: 'deterministic-slippage-v1', slippage_bps: '100' },
  latency: { policy: 'deterministic-latency-v1', latency_ms: 60000 },
  criterion: CRITERION,
  budget: { max_model_calls: 10, max_backtests: 5 },
}

const DECLARED_COSTS =
  '手续费 policy deterministic-commission-v1, commission_bps 100 · ' +
  '滑点 policy deterministic-slippage-v1, slippage_bps 100 · ' +
  '延迟 policy deterministic-latency-v1, latency_ms 60000'

/** A recorded call the workspace could not make: honest, uncharged, no answer. */
const unconfigured: UserStrategy['calls'][number] = {
  call_id: 'call-1', base_revision: 1, status: 'unconfigured',
  route: { provider: 'none', model: 'none', endpoint: null },
  approved_budget: { max_model_calls: 1, max_backtests: 0 },
  reserved_at: '2026-10-06T10:01:00.000000Z',
  message: 'No model provider is configured in this workspace.',
  // Nothing was measured, so the tokens are absent rather than zero.
  model: null, input_tokens: null, output_tokens: null, usage_available: false,
}

/** A recorded call that may have reached a provider: never re-sent, never refunded. */
const unresolved: UserStrategy['calls'][number] = {
  call_id: 'call-2', base_revision: 1, status: 'unknown',
  route: { provider: 'fixture', model: 'fixture-model', endpoint: 'https://fixture.invalid/v1/messages' },
  approved_budget: { max_model_calls: 1, max_backtests: 0 },
  reserved_at: '2026-10-06T10:04:00.000000Z',
  message: 'The provider was reached but no outcome was observed.',
  model: null, input_tokens: null, output_tokens: null, usage_available: false,
}

const asked: { callId: string; baseRevision: number; approved: boolean }[] = []
const applied: { proposalId: string; operationId: string; paths: string[] | null }[] = []
const revised: { callId: string; approved: number }[] = []
const undone: { operationId: string; headRevision: number; target: string }[] = []
const launched: {
  intent: string
  baseRevision: number
  previewIdentity: string
  approved: { max_model_calls: number; max_backtests: number }
}[] = []

type Script = {
  strategies?: UserStrategy[]
  current?: () => UserStrategy
  read?: (id: string) => Promise<UserStrategy>
  ask?: (id: string) => Promise<UserStrategy>
  apply?: (id: string) => Promise<UserStrategy>
  preview?: (id: string) => Promise<UserStrategyPreview>
  launch?: (id: string, intent: string) => Promise<UserStrategy>
}

/** One read that the test resolves by hand, to move the route while it is pending. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function scripted(script: Script = {}) {
  const calls = { list: 0, create: 0, get: 0, ask: 0, apply: 0, reject: 0, revise: 0, undo: 0, preview: 0, launch: 0 }
  const ids: string[] = []
  let persisted = strategy(script.strategies?.[0])
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    listScenarios: async () => [scenario],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { throw new Error('unused') },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => { throw new Error('unused') },
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    listUserStrategies: async () => { calls.list += 1; return script.strategies ?? [] },
    createUserStrategy: async () => { calls.create += 1; return persisted },
    getUserStrategy: async (id) => {
      calls.get += 1
      ids.push(id)
      if (script.read) return script.read(id)
      const listed = script.strategies?.find(item => item.user_strategy_id === id)
      return script.current ? script.current() : (listed ?? persisted)
    },
    sendUserStrategyMessage: async (id, input) => {
      calls.ask += 1
      ids.push(id)
      asked.push({ callId: input.call_id, baseRevision: input.base_revision, approved: (input.approved_budget?.max_model_calls ?? 0) > 0 })
      if (!script.ask) throw new Error('unused')
      persisted = await script.ask(id)
      return persisted
    },
    applyUserStrategyProposal: async (id, proposalId, input) => {
      calls.apply += 1
      ids.push(id)
      applied.push({ proposalId, operationId: input.operation_id, paths: input.paths ?? null })
      if (!script.apply) return persisted
      persisted = await script.apply(id)
      return persisted
    },
    rejectUserStrategyProposal: async (id) => {
      calls.reject += 1
      ids.push(id)
      persisted = { ...persisted, proposals: persisted.proposals.map((item) => item.proposal_id === 'prop-1' ? { ...item, status: 'rejected' as const } : item) }
      return persisted
    },
    reviseUserStrategyProposal: async (id, _proposalId, input) => {
      calls.revise += 1
      ids.push(id)
      revised.push({ callId: input.call_id, approved: input.approved_budget.max_model_calls })
      return persisted
    },
    undoUserStrategyChange: async (id, input) => {
      calls.undo += 1
      ids.push(id)
      undone.push({ operationId: input.operation_id, headRevision: input.head_revision, target: input.target_operation_id })
      return persisted
    },
    previewUserStrategyExperiment: async (id) => { calls.preview += 1; ids.push(id); return script.preview ? script.preview(id) : preview },
    launchUserStrategyExperiment: async (id, input) => {
      calls.launch += 1
      ids.push(id)
      launched.push({ intent: input.launch_intent_id, baseRevision: input.base_revision, previewIdentity: input.preview_identity, approved: input.approved_budget })
      persisted = {
        ...persisted,
        experiments: [{
          launch_intent_id: input.launch_intent_id,
          request_id: 'request-1',
          payload_sha256: 'f'.repeat(64),
          // The frozen request, retained on the record: the revision, the input
          // identity and the budget this confirmation was made with.
          payload: {
            draft_revision: input.base_revision,
            scenario_id: 'bounded-long.yaml',
            parameters: persisted.draft.parameters,
            preview_identity: input.preview_identity,
            input_sha256: preview.input_sha256,
            input_identity: preview.input_identity,
            approved_budget: input.approved_budget,
          },
          task_id: 'task-1',
          job_id: 'job-1',
          status: 'created',
          created_at: '2026-10-06T10:02:00.000000Z',
        }],
      }
      return script.launch ? script.launch(id, input.launch_intent_id) : persisted
    },
  }
  return { api, calls, ids }
}

function detail(api: ApiAdapter, id = 'us-1') {
  window.history.pushState({}, '', `/strategies/${id}`)
  render(<App api={api} />)
}

/** The detail sections live behind the workspace's own tabs. */
async function openSection(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('tab', { name }))
  return screen.findByRole('region', { name })
}

afterEach(() => {
  cleanup()
  asked.length = 0
  applied.length = 0
  revised.length = 0
  undone.length = 0
  launched.length = 0
  window.history.pushState({}, '', '/backtests')
})

describe('user strategy workspace', () => {
  it('saves a draft on create and never asks the model', async () => {
    const user = userEvent.setup()
    const { api, calls, ids } = scripted()
    window.history.pushState({}, '', '/strategies')
    render(<App api={api} />)

    expect(await screen.findByRole('heading', { name: '策略' })).toBeTruthy()
    expect(await screen.findByText('还没有策略。')).toBeTruthy()
    // The AI column is present before anything is selected, and it says what it
    // will hold rather than pretending to hold a conversation.
    const emptyChat = screen.getByRole('region', { name: 'AI 协作' })
    expect(within(emptyChat).getByText(/先创建或选择一条策略/)).toBeTruthy()
    expect(screen.getByText('创建策略只保存草稿：不调用模型，也不启动回测。')).toBeTruthy()

    const form = screen.getByRole('region', { name: '新建策略' })
    await user.type(within(form).getByLabelText('策略名称'), 'Bold entry')
    await user.type(within(form).getByLabelText('意图'), 'Test one registered entry')
    // The scenario list is the real registry view, so it arrives asynchronously.
    await within(form).findByRole('option', { name: 'bounded-long-v1 · XNAS:AAPL' })
    await user.selectOptions(within(form).getByLabelText('场景'), 'bounded-long.yaml')
    expect(calls.create).toBe(0)

    await user.click(within(form).getByRole('button', { name: '创建策略' }))

    expect(calls.create).toBe(1)
    // Only the save happened: no model call, no experiment, no extra navigation read.
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
    expect(asked).toEqual([])
    // One create selects the strategy it saved: its own detail and its own AI
    // column open immediately, with no second creation step.
    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    const draft = await screen.findByRole('region', { name: '草稿' })
    expect(within(draft).getByText('草稿修订 1')).toBeTruthy()
    expect(within(draft).getByText('10000 USD')).toBeTruthy()
    // The form gives way to the strategy that is now open, with one obvious way back.
    await waitFor(() => expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull())
    expect(screen.getByRole('link', { name: '新建策略' }).getAttribute('href')).toBe('/strategies')
    expect(ids).toEqual(['us-1'])

    // One click on that entry point opens the create form here, with every required
    // field, and creates nothing: saving is still an explicit submit of its own.
    await user.click(screen.getByRole('link', { name: '新建策略' }))
    const reopened = await screen.findByRole('region', { name: '新建策略' })
    expect(within(reopened).getByLabelText('策略名称')).toBeTruthy()
    expect(within(reopened).getByLabelText('场景')).toBeTruthy()
    expect(within(reopened).getByLabelText('意图')).toBeTruthy()
    expect(within(reopened).getByLabelText('准则字段')).toBeTruthy()
    expect(within(reopened).getByRole('button', { name: '创建策略' })).toBeTruthy()
    expect(calls.create).toBe(1)
    expect(calls.ask).toBe(0)
  })

  it('asks the model only after an explicit approval and labels the answer as inference', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      ask: async () => strategy({
        calls: [{ call_id: 'call-1', base_revision: 1, status: 'observed', route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' }, approved_budget: { max_model_calls: 1, max_backtests: 0 }, reserved_at: '2026-10-06T10:01:00.000000Z', message: null, model: 'scripted-fixture-v1', input_tokens: 11, output_tokens: 7, usage_available: true }],
        messages: [{ message_id: 'msg-1', role: 'user', kind: 'message', text: 'What does my draft do?', base_revision: 1, at: '2026-10-06T10:01:00.000000Z', call_id: 'call-1', proposal_id: null }, { message_id: 'msg-2', role: 'model', kind: 'message', text: 'Your draft enters once with a two-unit target.', base_revision: 1, at: '2026-10-06T10:01:01.000000Z', call_id: 'call-1', proposal_id: null }],
      }),
    })
    detail(api)
    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()

    const chat = screen.getByRole('region', { name: 'AI 协作' })
    await user.type(within(chat).getByLabelText('消息'), 'What does my draft do?')
    await user.click(within(chat).getByRole('button', { name: '询问模型' }))

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '在询问模型之前，需要先批准一次模型调用。')
    expect(calls.ask).toBe(0)
    expect(asked).toEqual([])

    await user.click(within(chat).getByLabelText('批准一次模型调用'))
    await user.click(within(chat).getByRole('button', { name: '询问模型' }))

    expect(calls.ask).toBe(1)
    expect(asked).toEqual([{ callId: 'call-1', baseRevision: 1, approved: true }])
    expect(await within(chat).findByText('Your draft enters once with a two-unit target.')).toBeTruthy()
    // Prose from the model is inference, never formal evidence, and the usage is measured.
    expect(within(chat).getByText('模型推断 — 不是正式证据。')).toBeTruthy()
    expect(within(chat).getByText('输入 11 tokens · 输出 7 tokens')).toBeTruthy()
    // Approval is one call, not a price: nothing here is written as money.
    expect(within(chat).getByText(/一次批准只允许针对这个草稿修订的一次调用/)).toBeTruthy()
    expect(chat.textContent).not.toMatch(/\$\s?\d/)
    expect(screen.queryByRole('region', { name: '建议修改' })).toBeNull()
  })

  it('applies only the checked paths, surfaces a refusal, and rejects only on an explicit click', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      current: () => strategy({ proposals: [proposal()] }),
      ask: async () => strategy({ proposals: [proposal()], calls: [unconfigured] }),
      apply: async () => {
        // The second attempt races a newer revision: the backend refuses it.
        if (calls.apply > 1) throw new UserStrategyRequestError('stale_base_revision', 'the draft revision moved on')
        return strategy({ proposals: [proposal()] })
      },
    })
    detail(api)
    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()

    // The open strategy owns the page; the create form is offered as a link instead.
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()
    expect(screen.getByRole('link', { name: '新建策略' }).getAttribute('href')).toBe('/strategies')

    const panel = await screen.findByRole('region', { name: '建议修改' })
    // The reviewed change names its base revision, its proposal and its proposer.
    expect(within(panel).getByText(/针对草稿修订 1 · 提案 prop-1/)).toBeTruthy()
    // Both proposed fields are shown, each with its own before → after, its own
    // reason and its own proposer.
    const items = within(panel).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items.map(item => item.textContent)).toEqual([
      expect.stringContaining('parameters.target_quantity'),
      expect.stringContaining('parameters.entry_delay_bars'),
    ])
    expect(within(panel).getByText('2 → 3')).toBeTruthy()
    expect(within(panel).getByText('0 → 1')).toBeTruthy()
    expect(within(panel).getByText('A wider sample size.')).toBeTruthy()
    expect(within(panel).getByText('Skip the first bar.')).toBeTruthy()
    expect(within(panel).getAllByText('提议者：model')).toHaveLength(2)
    expect(calls.apply).toBe(0)

    // Asking the model is not applying, and a recorded non-answer stays honest.
    const chat = screen.getByRole('region', { name: 'AI 协作' })
    await user.type(within(chat).getByLabelText('消息'), 'Why this change?')
    await user.click(within(chat).getByLabelText('批准一次模型调用'))
    await user.click(within(chat).getByRole('button', { name: '询问模型' }))
    await waitFor(() => expect(calls.ask).toBe(1))
    expect(calls.apply).toBe(0)
    expect(await within(chat).findByText('未配置')).toBeTruthy()
    expect(within(chat).getByText('No model provider is configured in this workspace.')).toBeTruthy()
    // A workspace with no provider says how to get one, and says that asking again
    // is a new call with a new approval rather than a silent retry.
    expect(within(chat).getByRole('link', { name: '设置' }).getAttribute('href')).toBe('/settings')
    expect(chat.textContent).toMatch(/那是一次新的调用，需要新的批准/)
    // An unmeasured call is never reported as a zero-cost one.
    expect(within(chat).getByText('这次调用没有上报 token 用量，因此不会按零计费')).toBeTruthy()

    await user.click(within(panel).getByLabelText('parameters.entry_delay_bars'))
    await user.click(within(panel).getByRole('button', { name: '应用所选' }))

    expect(calls.apply).toBe(1)
    // One click, one intent: the selection and the operation id this click made.
    expect(applied).toEqual([{ proposalId: 'prop-1', operationId: 'op-prop-1-1-0', paths: ['parameters.target_quantity'] }])

    // A refused apply is reported with the host's own message, never shown as applied.
    await user.click(within(panel).getByRole('button', { name: '应用所选' }))
    const refusal = await screen.findByRole('alert')
    expect(refusal.textContent).toContain('草稿修订已前进，这次修改没有被应用。')
    expect(refusal.textContent).toContain('the draft revision moved on')
    expect(within(screen.getByRole('region', { name: '草稿' })).getByText('草稿修订 1')).toBeTruthy()
    expect(within(panel).getByText('待处理')).toBeTruthy()

    await user.click(within(panel).getByRole('button', { name: '拒绝建议' }))
    expect(calls.reject).toBe(1)
    // The decision remounts the panel, so the current region is re-read rather
    // than held: the rejected proposal is still this draft revision's own.
    await waitFor(() => {
      const settled = screen.getByRole('region', { name: '建议修改' })
      expect(within(settled).getByText('已拒绝')).toBeTruthy()
    })
    expect(within(screen.getByRole('region', { name: '草稿' })).getByText('草稿修订 1')).toBeTruthy()
  })

  it('asks for a different change only with its own fresh approval', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ current: () => strategy({ proposals: [proposal()] }) })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    await user.click(within(panel).getByRole('button', { name: '修改这条建议' }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '在修改这条建议之前，需要先批准一次模型调用。')
    expect(calls.revise).toBe(0)

    await user.click(within(panel).getByLabelText('为这个修订批准一次模型调用'))
    await user.click(within(panel).getByRole('button', { name: '修改这条建议' }))

    expect(calls.revise).toBe(1)
    // Revising is one more approved call against this revision — nothing else runs.
    expect(revised).toEqual([{ callId: 'revise-prop-1-1', approved: 1 }])
    expect(calls.apply).toBe(0)
    expect(calls.reject).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('undoes the named change of the revision it is on', async () => {
    const user = userEvent.setup()
    const moved = strategy({
      draft: { ...strategy().draft, revision: 2, parameters: { target_quantity: '3', entry_delay_bars: 0 } },
      proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [appliedChange],
    })
    const { api, calls } = scripted({ current: () => moved })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })
    const button = within(panel).getByRole('button', { name: '撤销最近一次修改' })
    expect((button as HTMLButtonElement).disabled).toBe(false)

    await user.click(button)

    expect(calls.undo).toBe(1)
    // The click names the revision on screen and the one change it means, so a
    // stale page cannot reverse whatever happens to be newest.
    expect(undone).toEqual([{ operationId: 'undo-op-apply-1-2', headRevision: 2, target: 'op-apply-1' }])
    expect(calls.apply).toBe(0)
  })

  it('offers no undo for a revision that has no applied change', async () => {
    const ahead = strategy({
      draft: { ...strategy().draft, revision: 3, parameters: { target_quantity: '3', entry_delay_bars: 1 } },
      proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [appliedChange],
    })
    const { api, calls } = scripted({ current: () => ahead })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    expect((within(panel).getByRole('button', { name: '撤销最近一次修改' }) as HTMLButtonElement).disabled).toBe(true)
    expect(calls.undo).toBe(0)
    expect(within(screen.getByRole('region', { name: '草稿' })).getByText('草稿修订 3')).toBeTruthy()
  })

  it('keeps every request on the strategy that is open', async () => {
    const user = userEvent.setup()
    const second = strategy({
      user_strategy_id: 'us-2', name: 'Quiet entry',
      draft: { ...strategy().draft, revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
    })
    const { api, calls, ids } = scripted({ strategies: [strategy(), second] })
    detail(api, 'us-2')

    expect(await screen.findByRole('heading', { name: 'Quiet entry' })).toBeTruthy()
    const draft = screen.getByRole('region', { name: '草稿' })
    expect(within(draft).getByText('草稿修订 3')).toBeTruthy()
    expect(within(draft).getByText('bounded-long-v1 · XNAS:AAPL')).toBeTruthy()

    const chat = screen.getByRole('region', { name: 'AI 协作' })
    await user.type(within(chat).getByLabelText('消息'), 'Explain this draft')
    await user.click(within(chat).getByLabelText('批准一次模型调用'))
    await user.click(within(chat).getByRole('button', { name: '询问模型' }))

    await waitFor(() => expect(calls.ask).toBe(1))
    expect([...new Set(ids)]).toEqual(['us-2'])
    expect(asked).toEqual([{ callId: 'call-1', baseRevision: 3, approved: true }])
    // The AI column names the strategy it belongs to and can only switch to a real
    // strategy of this workspace.
    const context = within(chat).getByLabelText('当前策略') as HTMLSelectElement
    expect(context.value).toBe('us-2')
    expect([...context.options].map(option => option.value)).toEqual(['us-1', 'us-2'])
  })

  it('gives every strategy its own checkout of the panels it is open in', async () => {
    const user = userEvent.setup()
    const second = strategy({
      user_strategy_id: 'us-2', name: 'Quiet entry',
      draft: { ...strategy().draft, revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
    })
    const { api, calls } = scripted({ strategies: [strategy(), second] })
    detail(api, 'us-1')

    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()
    const chat = screen.getByRole('region', { name: 'AI 协作' })
    await user.type(within(chat).getByLabelText('消息'), 'Explain the draft of us-1')
    const experiment = await openSection(user, '实验')
    await user.click(within(experiment).getByRole('button', { name: '预览实验' }))
    expect(await screen.findByText('4 条记录 · aaaaaaaa')).toBeTruthy()
    expect(calls.preview).toBe(1)

    await user.click(screen.getByRole('link', { name: 'Quiet entry' }))

    expect(await screen.findByRole('heading', { name: 'Quiet entry' })).toBeTruthy()
    const draft = screen.getByRole('region', { name: '草稿' })
    await waitFor(() => expect(within(draft).getByText('草稿修订 3')).toBeTruthy())
    // Neither the typed question nor the other strategy's preview is carried over.
    const nextChat = screen.getByRole('region', { name: 'AI 协作' })
    expect((within(nextChat).getByLabelText('消息') as HTMLTextAreaElement).value).toBe('')
    const nextExperiment = await openSection(user, '实验')
    expect(within(nextExperiment).queryByText('4 条记录 · aaaaaaaa')).toBeNull()
    expect(calls.launch).toBe(0)
  })

  it('never puts a strategy that is no longer open under the open route', async () => {
    const user = userEvent.setup()
    const late = deferred<UserStrategy>()
    const second = strategy({
      user_strategy_id: 'us-2', name: 'Quiet entry',
      draft: { ...strategy().draft, revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
    })
    const { api } = scripted({
      strategies: [strategy(), second],
      read: id => id === 'us-1' ? late.promise : Promise.resolve(second),
    })
    detail(api, 'us-1')

    // The first strategy's own read has not answered yet; navigation does not wait.
    await user.click(await screen.findByRole('link', { name: 'Quiet entry' }))
    expect(await screen.findByRole('heading', { name: 'Quiet entry' })).toBeTruthy()

    // Its slow answer arrives after the route moved on, and it is not rendered as
    // if it were the strategy that is open.
    late.resolve(strategy())
    await waitFor(() => expect(within(screen.getByRole('region', { name: '草稿' })).getByText('草稿修订 3')).toBeTruthy())
    expect(screen.getByRole('heading', { name: 'Quiet entry' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Bold entry' })).toBeNull()
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()
  })

  it('treats an unresolved call as unresolved and points at Settings, not a re-send', async () => {
    const { api, calls } = scripted({ current: () => strategy({ calls: [unresolved] }) })
    detail(api)
    // The AI column is on screen before anything is open; the conversation of the
    // open strategy is the one that arrives with that strategy's own read.
    await screen.findByRole('heading', { name: 'Bold entry' })
    const chat = screen.getByRole('region', { name: 'AI 协作' })
    await waitFor(() => expect(within(chat).getByText('结果未知')).toBeTruthy())
    expect(chat.textContent).toMatch(/不会被重发，也不会被退还/)
    expect(within(chat).getByRole('link', { name: '设置' }).getAttribute('href')).toBe('/settings')
    expect(within(chat).getByText('这次调用没有上报 token 用量，因此不会按零计费')).toBeTruthy()
    // Nothing here re-sends it: an answer would need a new, explicitly approved call.
    expect(calls.ask).toBe(0)
    expect(within(chat).getByRole('button', { name: '询问模型' })).toBeTruthy()
  })

  it('previews the admitted input without starting a run and links the run only after a confirmation', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted()
    detail(api)
    const panel = await openSection(user, '实验')

    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)
    expect(within(panel).queryByRole('link', { name: '打开运行以查看它的状态和报告' })).toBeNull()

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))

    expect(calls.preview).toBe(1)
    await waitFor(() => expect(within(panel).getByText('草稿修订 1')).toBeTruthy())
    expect(within(panel).getByText('bounded-long-v1 · XNAS:AAPL')).toBeTruthy()
    expect(within(panel).getByText('初始资金 10000 USD')).toBeTruthy()
    expect(within(panel).getByText('4 条记录 · aaaaaaaa')).toBeTruthy()
    // The scenario's own cost assumptions, read as they are declared.
    expect(within(panel).getByText(DECLARED_COSTS)).toBeTruthy()
    expect(within(panel).getByText('radian.report-criterion.v1 economics.net_pnl.amount positive')).toBeTruthy()
    expect(within(panel).getByText('10 次模型调用 · 5 次回测')).toBeTruthy()
    // The identity a confirmation has to carry back is on screen before the click.
    expect(within(panel).getByText('sha256:aaaaaaaaaaaa')).toBeTruthy()
    expect(calls.launch).toBe(0)

    await user.click(within(panel).getByRole('button', { name: '开始实验' }))

    expect(calls.launch).toBe(1)
    // The confirmation names the previewed input and one backtest, no model call.
    expect(launched).toEqual([{
      intent: 'li-1',
      baseRevision: 1,
      previewIdentity: PREVIEW_IDENTITY,
      approved: { max_model_calls: 0, max_backtests: 1 },
    }])
    const entry = within(panel).getByText('已从修订 1 启动实验 li-1').closest('li')!
    expect(within(entry).getByRole('link', { name: '打开运行以查看它的状态和报告' }).getAttribute('href')).toBe('/backtests/job-1')
    // Nothing here claims an outcome: the run and its report live on the backtest page.
    expect(entry.textContent).not.toMatch(/profit|net pnl|guarantee/i)
    // One started run is not a finished piece of research, and this page says where
    // the rest of it belongs.
    expect(panel.textContent).toMatch(/一次运行只是一个冻结的结果，不是已经完成的研究/)
    expect(within(panel).getByRole('link', { name: '留出验证' }).getAttribute('href')).toBe('/holdouts')
    expect(within(panel).getByRole('link', { name: '研究' }).getAttribute('href')).toBe('/research')
  })

  it('reports the cost assumptions the scenario declares and never invents one', async () => {
    const user = userEvent.setup()
    const bare: UserStrategyPreview = {
      ...preview,
      commission: null,
      slippage: null,
      latency: null,
      normalized: { ...preview.normalized, execution: { policy: 'phase1.next-bar-close.v1' } },
    }
    const { api } = scripted({ preview: async () => bare })
    detail(api)
    const panel = await openSection(user, '实验')

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))

    expect(await within(panel).findByText(
      '手续费 该场景未声明 · 滑点 该场景未声明 · 延迟 该场景未声明',
    )).toBeTruthy()
  })

  it('keeps a frozen run on the revision it ran and never claims its outcome', async () => {
    const user = userEvent.setup()
    const frozen = strategy({
      draft: { ...strategy().draft, revision: 3, parameters: { target_quantity: '2', entry_delay_bars: 1 } },
      experiments: [
        {
          launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
          payload: { draft_revision: 1, scenario_id: 'bounded-long.yaml', parameters: { target_quantity: '2', entry_delay_bars: 0 } },
          task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00.000000Z',
        },
        {
          launch_intent_id: 'li-2', request_id: 'request-2', payload_sha256: 'e'.repeat(64),
          payload: {
            draft_revision: 2, scenario_id: 'bounded-long.yaml',
            parameters: { target_quantity: '3', entry_delay_bars: 0 }, preview_identity: PREVIEW_IDENTITY,
          },
          task_id: null, job_id: null, status: 'unknown',
          message: 'The provider was reached but no outcome was observed.',
          created_at: '2026-10-06T10:03:00.000000Z',
        },
      ],
    })
    const { api } = scripted({ strategies: [frozen], current: () => frozen })
    detail(api)
    const panel = await openSection(user, '实验')

    // The draft has moved to revision 3; each frozen run stays on its own revision.
    expect(within(screen.getByRole('region', { name: '草稿' })).getByText('草稿修订 3')).toBeTruthy()
    expect(within(panel).getByText('已从修订 1 启动实验 li-1')).toBeTruthy()
    expect(within(panel).getByRole('link', { name: '打开运行以查看它的状态和报告' }).getAttribute('href')).toBe('/backtests/job-1')
    expect(within(panel).getByText('实验 li-2 来自修订 2 — 结果未知')).toBeTruthy()
    expect(panel.textContent).toMatch(/中断之前可能已经派发了一次运行/)
    expect(panel.textContent).toMatch(/The provider was reached but no outcome was observed./)
    // An outcome that was never observed is never written up as a result.
    expect(panel.textContent).not.toMatch(/profit|net pnl|guarantee/i)
    // The table carries the same frozen fact on the strategy's own row: the
    // revision its latest run froze, and the state of that launch — not a claim
    // that a started run succeeded.
    const table = within(screen.getByRole('navigation', { name: '策略列表' }))
    expect(table.getByText('冻结 v2')).toBeTruthy()
    expect(table.getByText('结果未知')).toBeTruthy()
    expect(table.queryByText(/成功/)).toBeNull()
  })

  it('shows a frozen run its own real evidence and refuses a report of another run', async () => {
    const frozen = strategy({
      experiments: [{
        launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
        payload: { draft_revision: 1, scenario_id: 'bounded-long.yaml', parameters: { target_quantity: '2', entry_delay_bars: 0 } },
        task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00.000000Z',
      }],
    })
    const { api } = scripted({ strategies: [frozen], current: () => frozen })
    const job = {
      schema: 'ea.local-web-job.v5', job_id: 'job-1', request_id: 'request-1', scenario_id: 'bounded-long.yaml',
      input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
      status: 'succeeded' as const, engine_run_id: 'run-1', report_sha256: 'd'.repeat(64), summary_sha256: null,
      error_code: null, message: null, report_ready: true,
    }
    api.getBacktest = async () => job
    api.getReport = async () => ({
      // A report of a different run: it must not be shown as this run's result.
      schema: 'ea.backtest-report.v1' as const, run_id: 'run-2',
      economics: {
        ending_cash: [{ amount: '100', currency: 'USD' }], ending_positions: [],
        valuation: { price: '1', position_value: '0' }, equity: { amount: '100', currency: 'USD' },
        net_pnl: { amount: '0', currency: 'USD' }, total_return: { value: '0' },
        counts: { orders: 1, fills: 1 }, execution: { order: null, fill: null },
      },
    })
    detail(api)
    // The evidence section is the default view of a strategy that has a frozen run.
    const panel = await screen.findByRole('region', { name: '证据' })

    expect(await within(panel).findByText(/报告的身份与该运行的 engine run id 不一致/)).toBeTruthy()
    expect(panel.textContent).not.toMatch(/期末权益/)
    expect(panel.textContent).not.toMatch(/100 USD/)
  })
})
