/**
 * User-strategy workspace UI contract.
 *
 * The adapter below is a scripted double: every strategy document, preview fact
 * and job id in these tests is hand-written fixture data, not a model output and
 * not an engine result. The scripted "AI" answers only prove the product rules —
 * creating a strategy is a save and never a model call, every model call needs an
 * explicit approval click, a confirmation names the very preview it was made
 * from, and only an explicit click applies a change, undoes one or starts an
 * experiment. One confirmation spends exactly the preview it was made from, and a
 * preview the user gave up on is never read as consent.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { EvidenceSection } from './user-strategies'
import { StrategyEvidenceResearch } from './user-strategy-research'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type ScenarioSummary } from './app'
import {
  UserStrategyRequestError,
  isIntentDraft,
  type UserStrategy,
  type UserStrategyChange,
  type UserStrategyDraft,
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

/**
 * The executable draft of a bound fixture. An intent fixture has none, so reaching
 * for a draft field through this helper is a test that has said which one it means.
 */
function executable(value: UserStrategy): UserStrategyDraft {
  const draft = value.draft
  if (isIntentDraft(draft)) throw new Error('this fixture strategy is not bound to a scenario')
  return draft
}

/** One saved idea: revision 1 of the same durable strategy, with nothing executable. */
function intent(overrides: Partial<UserStrategy> = {}): UserStrategy {
  return strategy({
    draft: { kind: 'intent', revision: 1, scenario_id: null },
    binding: null,
    ...overrides,
  })
}

/** One immutable binding record, exactly as the host keeps it on the record. */
const BINDING = {
  operation_id: 'bind-us-1-1-0',
  request: { action: 'bind' as const, base_revision: 1, scenario_id: 'bounded-long.yaml', criterion: null },
  request_sha256: 'a'.repeat(64),
  scenario_id: 'bounded-long.yaml',
  from_revision: 1,
  to_revision: 2,
  bound_at: '2026-10-06T10:02:00.000000Z',
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

/** One web job, as the workspace's own read of a run returns it. */
const runJob: BacktestJob = {
  schema: 'ea.local-web-job.v5', job_id: 'job-1', request_id: 'request-1', scenario_id: 'bounded-long.yaml',
  input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
  status: 'running', engine_run_id: 'run-1', report_sha256: null, summary_sha256: null,
  error_code: null, message: null, report_ready: false,
}

/** One created experiment record: a dispatch exists, nothing about its outcome. */
function createdExperiment(overrides: Partial<UserStrategy['experiments'][number]> = {}) {
  return {
    launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
    payload: {
      draft_revision: 1, scenario_id: 'bounded-long.yaml',
      parameters: { target_quantity: '2', entry_delay_bars: 0 },
      preview_identity: PREVIEW_IDENTITY, input_sha256: preview.input_sha256,
      approved_budget: { max_model_calls: 0, max_backtests: 1 },
    },
    task_id: 'task-1', job_id: 'job-1', status: 'created' as const,
    created_at: '2026-10-06T10:02:00.000000Z',
    ...overrides,
  }
}

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
const created: { name: string; intent: string; scenario_id?: string | null }[] = []
const boundCalls: {
  operationId: string
  baseRevision: number
  scenarioId: string
  criterion: { field: string; expectation: string } | null
}[] = []
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
  /** The list read, when a test needs to hold it open rather than answer at once. */
  list?: () => Promise<UserStrategy[]>
  current?: () => UserStrategy
  read?: (id: string) => Promise<UserStrategy>
  create?: (input: { name: string; intent: string }) => UserStrategy
  bind?: (id: string) => Promise<UserStrategy>
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
  const calls = { list: 0, create: 0, get: 0, bind: 0, ask: 0, apply: 0, reject: 0, revise: 0, undo: 0, preview: 0, launch: 0 }
  const ids: string[] = []
  let persisted = strategy(script.strategies?.[0])
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    // Chat interaction fixtures represent an explicitly configured local model.
    // Missing/error/pending configuration is covered by first-use-readiness.
    settingsStatus: async () => ({
      schema: 'radian.settings-status.v1', settings_file: '/fixture/settings.json',
      configured: true, provider: 'fixture', model: 'fixture-model', base_url: null, api_key_env: null,
    }),
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
    listUserStrategies: async () => {
      calls.list += 1
      return script.list ? script.list() : script.strategies ?? []
    },
    createUserStrategy: async (input) => {
      calls.create += 1
      created.push({ name: input.name, intent: input.intent, scenario_id: input.scenario_id })
      if (!script.create) return persisted
      persisted = script.create({ name: input.name, intent: input.intent })
      return persisted
    },
    getUserStrategy: async (id) => {
      calls.get += 1
      ids.push(id)
      if (script.read) return script.read(id)
      const listed = script.strategies?.find(item => item.user_strategy_id === id)
      return script.current ? script.current() : (listed ?? persisted)
    },
    bindUserStrategyScenario: async (id, input) => {
      calls.bind += 1
      ids.push(id)
      boundCalls.push({
        operationId: input.operation_id,
        baseRevision: input.base_revision,
        scenarioId: input.scenario_id,
        criterion: input.criterion ?? null,
      })
      if (!script.bind) throw new Error('unused')
      persisted = await script.bind(id)
      return persisted
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
            parameters: executable(persisted).parameters,
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
  await user.click(await screen.findByRole('tab', { name: name === '实验' ? '实验' : '策略' }))
  if (name === '修订') await user.click(screen.getByText('修订记录'))
  if (name === '实验') {
    const disclosure = screen.getByText('新实验与运行记录').closest('details')!
    if (!disclosure.open) await user.click(screen.getByText('新实验与运行记录'))
  }
  return screen.findByRole('region', { name })
}

/**
 * Whether one node really comes before another in document order. Reading order is
 * the DOM's own, so the maturity tests assert it with compareDocumentPosition rather
 * than trusting a class name or a visual mirror of the layout.
 */
function precedes(first: Node, second: Node): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
}

/** The nearest collapsed disclosure a node would be hidden inside, if there is one. */
function collapsedAncestor(node: Element): HTMLDetailsElement | null {
  let current: Element | null = node.parentElement
  while (current) {
    if (current instanceof HTMLDetailsElement && !current.open) return current
    current = current.parentElement
  }
  return null
}

afterEach(() => {
  cleanup()
  asked.length = 0
  created.length = 0
  boundCalls.length = 0
  applied.length = 0
  revised.length = 0
  undone.length = 0
  launched.length = 0
  window.history.pushState({}, '', '/backtests')
})

describe('user strategy workspace', () => {
  it('saves an idea without a scenario and never asks the model', async () => {
    const user = userEvent.setup()
    // The save the host really receives is what comes back: the name the form fell
    // back to and the user's own words, with no scenario and nothing executable.
    const { api, calls, ids } = scripted({
      create: input => intent({ name: input.name, intent: input.intent }),
    })
    window.history.pushState({}, '', '/strategies')
    render(<App api={api} />)

    expect(await screen.findByRole('heading', { name: '研究' })).toBeTruthy()
    expect(await screen.findByText('还没有策略。')).toBeTruthy()
    // The AI column is present before anything is selected, and it says what it
    // will hold rather than pretending to hold a conversation.
    const emptyChat = screen.getByRole('region', { name: 'AI 协作' })
    expect(within(emptyChat).getByText(/先创建或选择一条策略/)).toBeTruthy()

    const form = screen.getByRole('region', { name: '新建策略' })
    // The entry point asks for the idea alone. No scenario is chosen here, so a
    // workspace with an empty catalog can still save, and no criterion is stated:
    // both belong to the one explicit binding, on the strategy itself.
    expect(within(form).queryByLabelText('场景')).toBeNull()
    expect(within(form).queryByLabelText('准则字段')).toBeNull()
    expect(within(form).queryByLabelText('预期方向')).toBeNull()
    expect(within(form).getByText(/保存只写下这个想法/)).toBeTruthy()
    await user.type(within(form).getByLabelText('意图'), 'Test one registered entry')
    expect(calls.create).toBe(0)

    await user.click(within(form).getByRole('button', { name: '保存想法' }))

    expect(calls.create).toBe(1)
    // The save named no scenario and no criterion, and the name it fell back to is
    // the user's own words rather than a scenario or a default draft field.
    expect(created).toEqual([{
      name: 'Test one registered entry',
      intent: 'Test one registered entry',
      scenario_id: undefined,
    }])
    // Only the save happened: no model call, no experiment, no extra navigation read.
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
    expect(calls.bind).toBe(0)
    expect(asked).toEqual([])
    // One create selects the strategy it saved: its own detail and its own AI
    // column open immediately, with no second creation step.
    expect(await screen.findByRole('heading', { name: 'Test one registered entry' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    const draft = await screen.findByRole('region', { name: '草稿' })
    // What is on screen is an intent: revision 1, and none of the executable facts
    // a scenario would give it is invented to fill the space.
    expect(within(draft).getByText('意图修订 1')).toBeTruthy()
    expect(within(draft).getByText('尚未绑定：没有策略、标的、资金、数据来源或注册参数')).toBeTruthy()
    expect(draft.textContent).not.toMatch(/USD|XNAS|bounded-long/)
    // The form gives way to the strategy that is now open, with one obvious way back.
    await waitFor(() => expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull())
    expect(screen.getByRole('link', { name: '新建策略' }).getAttribute('href')).toBe('/strategies')
    expect(ids).toEqual(['us-1'])

    // One click on that entry point opens the create form here, with the fields a
    // save needs, and creates nothing: saving is still an explicit submit of its own.
    await user.click(screen.getByRole('link', { name: '新建策略' }))
    const reopened = await screen.findByRole('region', { name: '新建策略' })
    expect(within(reopened).getByLabelText('策略名称')).toBeTruthy()
    expect(within(reopened).getByLabelText('意图')).toBeTruthy()
    expect(within(reopened).getByRole('button', { name: '保存想法' })).toBeTruthy()
    // Leaving the name empty is allowed, and the form shows exactly what it would
    // save instead — never a scenario, never a strategy.
    expect(within(reopened).getByText(/留空时用你自己的话作为临时名称：未命名想法/)).toBeTruthy()
    expect(calls.create).toBe(1)
    expect(calls.ask).toBe(0)
  })

  it('binds the one confirmed scenario to the saved idea, and nothing before that click', async () => {
    const user = userEvent.setup()
    // The clarification is plain text from an unbound strategy: an answer about
    // the idea, with no typed proposal attached to it.
    const clarified = intent({
      calls: [{
        call_id: 'call-1', base_revision: 1, status: 'observed',
        route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' },
        approved_budget: { max_model_calls: 1, max_backtests: 0 },
        reserved_at: '2026-10-06T10:01:00.000000Z', message: null, model: 'scripted-fixture-v1',
        input_tokens: 5, output_tokens: 3, usage_available: true,
      }],
      messages: [
        { message_id: 'msg-1', role: 'user', kind: 'message', text: 'What is missing from this idea?', base_revision: 1, at: '2026-10-06T10:01:00.000000Z', call_id: 'call-1', proposal_id: null },
        { message_id: 'msg-2', role: 'model', kind: 'message', text: 'Which costs would this idea have to beat?', base_revision: 1, at: '2026-10-06T10:01:01.000000Z', call_id: 'call-1', proposal_id: null },
      ],
    })
    const bound = strategy({
      draft: { ...executable(strategy()), revision: 2 },
      binding: BINDING,
      changes: [{
        operation_id: 'bind-us-1-1-0',
        kind: 'bind',
        proposal_id: null,
        target_operation_id: null,
        scenario_id: 'bounded-long.yaml',
        criterion: null,
        from_revision: 1,
        to_revision: 2,
        applied_paths: [],
        stale_paths: [],
        items: [],
        evidence: [],
        proposer: 'user',
        at: '2026-10-06T10:02:00.000000Z',
      }],
    })
    const { api, calls, ids } = scripted({
      strategies: [intent()], current: () => intent(),
      ask: async () => clarified, bind: async () => bound,
    })
    detail(api)

    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()
    let panel = await screen.findByRole('region', { name: '绑定场景' })

    // An unbound strategy offers no experiment at all, and says why: there is no
    // draft it could preview or run.
    const experiment = await openSection(user, '实验')
    expect(within(experiment).queryByRole('button', { name: '预览实验' })).toBeNull()
    expect(within(experiment).queryByRole('button', { name: '确认并运行一次' })).toBeNull()
    expect(experiment.textContent).toMatch(/没有可以预览或运行的草稿/)

    // The conversation is available, and it is honest about what it can do.
    const chat = screen.getByRole('region', { name: 'AI 协作' })
    expect(chat.textContent).toMatch(/这次对话只能澄清这个想法/)
    expect(within(chat).getByText(/澄清当前想法/)).toBeTruthy()
    await user.type(within(chat).getByLabelText('消息'), 'What is missing from this idea?')
    await user.click(within(chat).getByLabelText('批准一次模型调用'))
    await user.click(within(chat).getByRole('button', { name: '询问模型' }))

    await waitFor(() => expect(calls.ask).toBe(1))
    // One approved call against the intent's own revision, and it proposes nothing.
    expect(asked).toEqual([{ callId: 'call-1', baseRevision: 1, approved: true }])
    expect(await within(chat).findByText('Which costs would this idea have to beat?')).toBeTruthy()
    expect(within(chat).getByText('模型推断 — 不是正式证据。')).toBeTruthy()
    expect(screen.queryByRole('region', { name: '建议修改' })).toBeNull()
    expect(calls.apply).toBe(0)

    await user.click(screen.getByRole('tab', { name: '策略' }))
    panel = screen.getByRole('region', { name: '绑定场景' })
    // Nothing is preselected, and the registered scenario is named by its exact id
    // beside that scenario's own summary so two similar choices stay told apart.
    const select = within(panel).getByLabelText('场景') as HTMLSelectElement
    expect(select.value).toBe('')
    expect(await within(panel).findByRole('option', { name: 'bounded-long.yaml · bounded-long-v1 · XNAS:AAPL' })).toBeTruthy()
    const button = () => within(panel).getByRole('button', { name: '绑定场景' }) as HTMLButtonElement
    expect(button().disabled).toBe(true)

    await user.selectOptions(select, 'bounded-long.yaml')
    // Choosing is not binding: without the explicit confirmation nothing is sent.
    expect(button().disabled).toBe(true)
    expect(calls.bind).toBe(0)

    await user.selectOptions(within(panel).getByLabelText('准则字段'), 'economics.net_pnl.amount')
    await user.selectOptions(within(panel).getByLabelText('预期方向'), 'positive')
    expect(calls.bind).toBe(0)
    await user.click(within(panel).getByLabelText('我确认把选中的这个场景绑定到这条策略'))
    await user.click(button())

    expect(calls.bind).toBe(1)
    // One explicit choice, one operation id: the revision on screen, the exact
    // scenario id and the criterion that belongs to this binding.
    expect(boundCalls).toEqual([{
      operationId: 'bind-us-1-1-0',
      baseRevision: 1,
      scenarioId: 'bounded-long.yaml',
      criterion: { field: 'economics.net_pnl.amount', expectation: 'positive' },
    }])
    expect([...new Set(ids)]).toEqual(['us-1'])
    // Binding is one choice of one scenario: it asks no model and starts nothing.
    expect(calls.launch).toBe(0)
    expect(calls.preview).toBe(0)

    // The offer is gone and the executable revision the host formed is on screen.
    await waitFor(() => expect(screen.queryByRole('region', { name: '绑定场景' })).toBeNull())
    const draft = screen.getByRole('region', { name: '草稿' })
    expect(within(draft).getByText('草稿修订 2')).toBeTruthy()
    expect(within(draft).getByText('bounded-long-v1 · XNAS:AAPL')).toBeTruthy()
    // The binding is named as its own kind of revision, with the scenario it bound.
    const revisions = await openSection(user, '修订')
    expect(within(revisions).getByText('绑定场景')).toBeTruthy()
    expect(within(revisions).getByText('绑定的场景：bounded-long.yaml')).toBeTruthy()
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

    expect((within(chat).getByRole('button', { name: '询问模型' }) as HTMLButtonElement).disabled).toBe(true)
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
    expect(within(chat).getByText(/一次模型调用 · 不应用修改，不启动实验/)).toBeTruthy()
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
    expect(within(panel).getByText('之前 2')).toBeTruthy()
    expect(within(panel).getByText('之后 3')).toBeTruthy()
    expect(within(panel).getByText('之前 0')).toBeTruthy()
    expect(within(panel).getByText('之后 1')).toBeTruthy()
    expect(within(panel).getByText('A wider sample size.')).toBeTruthy()
    expect(within(panel).getByText('Skip the first bar.')).toBeTruthy()
    expect(within(panel).getAllByText(/提议者：model/)).toHaveLength(2)
    // Applying is a draft change, and the panel says what the next step after it is.
    expect(panel.textContent).toMatch(/应用只改变草稿/)
    expect(panel.textContent).toMatch(/在「实验」里预览这次草稿并确认一次运行/)
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
    expect(screen.getByText('v1 草稿')).toBeTruthy()
    expect(within(panel).getByText('待处理')).toBeTruthy()

    await user.click(within(panel).getByRole('button', { name: '拒绝建议' }))
    expect(calls.reject).toBe(1)
    // The decision remounts the panel, so the current region is re-read rather
    // than held: the rejected proposal is still this draft revision's own.
    await waitFor(() => {
      const settled = screen.getByRole('region', { name: '建议修改' })
      expect(within(settled).getByText('已拒绝')).toBeTruthy()
    })
    expect(screen.getByText('v1 草稿')).toBeTruthy()
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
      draft: { ...executable(strategy()), revision: 2, parameters: { target_quantity: '3', entry_delay_bars: 0 } },
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
      draft: { ...executable(strategy()), revision: 3, parameters: { target_quantity: '3', entry_delay_bars: 1 } },
      proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [appliedChange],
    })
    const { api, calls } = scripted({ current: () => ahead })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    // The record is one collapsed disclosure, and the Undo it carries cannot act on
    // this revision, so it stays inside with the rest of the history.
    const disclosure = within(panel).getByText(/的修改与来源/).closest('details') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)
    expect(collapsedAncestor(within(panel).getByText('撤销最近一次修改'))).toBe(disclosure)

    fireEvent.click(within(panel).getByText(/的修改与来源/))
    expect((within(panel).getByRole('button', { name: '撤销最近一次修改' }) as HTMLButtonElement).disabled).toBe(true)
    expect(calls.undo).toBe(0)
    expect(screen.getByText('v3 草稿')).toBeTruthy()
  })

  it('shows an applied-and-then-undone change as undone, without rewriting the proposal', async () => {
    const reversed = strategy({
      draft: { ...executable(strategy()), revision: 6, parameters: { target_quantity: '2', entry_delay_bars: 1 } },
      proposals: [proposal({ base_revision: 4, status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [
        { ...appliedChange, from_revision: 4, to_revision: 5 },
        {
          operation_id: 'op-undo-1', kind: 'undo', proposal_id: null, target_operation_id: 'op-apply-1',
          from_revision: 5, to_revision: 6, applied_paths: [], stale_paths: [], items: [],
          evidence: [], proposer: 'user', at: '2026-10-06T10:05:00.000000Z',
        },
      ],
    })
    const { api, calls } = scripted({ current: () => reversed })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    // An undo names the application it reversed, and the collapsed record already
    // says what it amounts to — the effective state alone. The recorded status stays
    // inside with the rest of the provenance, and the current draft is not presented
    // as the result this proposal stands on.
    const summary = within(panel).getByText(/已撤销的修改与来源/)
    expect(summary.textContent).not.toMatch(/已应用/)
    const disclosure = summary.closest('details') as HTMLDetailsElement
    expect(disclosure.open).toBe(false)
    expect(panel.textContent).toMatch(/由模型提出，已应用/)
    expect(panel.textContent).toMatch(/这次应用已在修订 5 → 6 上被撤销/)

    // The whole record is that one disclosure, decisions included: there is nothing
    // left to undo here, and nothing on the first screen to press.
    fireEvent.click(summary)
    expect((within(panel).getByRole('button', { name: '撤销最近一次修改' }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(panel).getByRole('button', { name: '应用所选' }) as HTMLButtonElement).disabled).toBe(true)
    expect(calls.undo).toBe(0)
  })

  it('keeps every request on the strategy that is open', async () => {
    const user = userEvent.setup()
    const second = strategy({
      user_strategy_id: 'us-2', name: 'Quiet entry',
      draft: { ...executable(strategy()), revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
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
      draft: { ...executable(strategy()), revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
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

    await user.selectOptions(screen.getByLabelText('当前策略'), 'us-2')

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
      draft: { ...executable(strategy()), revision: 3, parameters: { target_quantity: '1', entry_delay_bars: 0 } },
    })
    const { api } = scripted({
      strategies: [strategy(), second],
      read: id => id === 'us-1' ? late.promise : Promise.resolve(second),
    })
    detail(api, 'us-1')

    // The first strategy's own read has not answered yet; navigation does not wait.
    await user.click(screen.getByRole('link', { name: '所有研究' }))
    await user.click(await screen.findByRole('link', { name: 'Quiet entry' }))
    expect(await screen.findByRole('heading', { name: 'Quiet entry' })).toBeTruthy()

    // Its slow answer arrives after the route moved on, and it is not rendered as
    // if it were the strategy that is open.
    late.resolve(strategy())
    await waitFor(() => expect(screen.getByText('v3 草稿')).toBeTruthy())
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
    expect(within(panel).queryByRole('link', { name: '查看这次实验报告' })).toBeNull()
    // The confirmation is the one control that spends a run, and it is inert until
    // there is an admitted preview to confirm.
    const confirm = () => within(panel).getByRole('button', { name: '确认并运行一次' }) as HTMLButtonElement
    expect(confirm().disabled).toBe(true)

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))

    expect(calls.preview).toBe(1)
    await waitFor(() => expect(within(panel).getByText('草稿修订 1')).toBeTruthy())
    expect(within(panel).getByText('bounded-long-v1 · XNAS:AAPL')).toBeTruthy()
    expect(within(panel).getByText('初始资金 10000 USD')).toBeTruthy()
    expect(within(panel).getByText('4 条记录 · aaaaaaaa')).toBeTruthy()
    // The scenario's own cost assumptions, read as they are declared.
    expect(within(panel).getByText(DECLARED_COSTS)).toBeTruthy()
    expect(within(panel).getByText('radian.report-criterion.v1 economics.net_pnl.amount positive')).toBeTruthy()
    expect(within(panel).getByText(/10 次模型调用 · 5 次回测/)).toBeTruthy()
    // The identity a confirmation has to carry back is on screen before the click.
    expect(within(panel).getByText('sha256:aaaaaaaaaaaa')).toBeTruthy()
    // One admitted preview is what makes the confirmation live.
    expect(confirm().disabled).toBe(false)
    expect(calls.launch).toBe(0)

    await user.click(confirm())

    expect(calls.launch).toBe(1)
    // The confirmation names the previewed input and one backtest, no model call.
    expect(launched).toEqual([{
      intent: 'li-1',
      baseRevision: 1,
      previewIdentity: PREVIEW_IDENTITY,
      approved: { max_model_calls: 0, max_backtests: 1 },
    }])
    const entry = within(panel).getByText('已从修订 1 启动实验 li-1').closest('li')!
    expect(within(entry).getByRole('link', { name: '查看这次实验报告' }).getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
    // Nothing here claims an outcome: the run and its report live on the backtest page.
    expect(entry.textContent).not.toMatch(/profit|net pnl|guarantee/i)
    // One started run is not a finished piece of research, and this page says where
    // the rest of it belongs.
    expect(panel.textContent).toMatch(/一次运行只是一个冻结的结果，不是已经完成的研究/)
    expect(within(panel).queryByRole('link', { name: '留出验证' })).toBeNull()
    expect(within(panel).queryByRole('link', { name: '研究' })).toBeNull()
  })

  it('reports the cost assumptions and the data window the scenario declares, and invents neither', async () => {
    const user = userEvent.setup()
    const bare: UserStrategyPreview = {
      ...preview,
      // A data block that declares no window at all: nothing here may be filled in.
      data: { path: 'prices.csv', fingerprint: { sha256: 'a'.repeat(64), record_count: 4 } },
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
    // A window the data does not declare is stated as missing, and no instant is
    // invented to stand in for it.
    expect(panel.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/)
    expect(within(panel).getByText(/未记录/)).toBeTruthy()
  })

  it('shows every admitted preview fact, including a parameter it has never heard of and a numeric zero', async () => {
    const user = userEvent.setup()
    // A registered scenario may carry a parameter name this page has no label for,
    // and a parameter may legitimately be zero. Both are facts of the admitted
    // input: the preview prints them exactly as they were admitted.
    const { api } = scripted({
      preview: async () => ({
        ...preview,
        parameters: { target_quantity: '2', entry_delay_bars: 0, half_spread_ticks: 0 },
      }),
    })
    detail(api)
    const panel = await openSection(user, '实验')

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))

    // The scenario the run would execute is named by its own id.
    expect(await within(panel).findByText(/bounded-long\.yaml/)).toBeTruthy()
    // Every registered parameter is shown: a name this page has never heard of is
    // printed rather than dropped, and a zero is a value rather than a missing one.
    expect(within(panel).getByText(/target_quantity: 2/)).toBeTruthy()
    expect(within(panel).getByText(/entry_delay_bars: 0/)).toBeTruthy()
    expect(within(panel).getByText(/half_spread_ticks: 0/)).toBeTruthy()
    // The admitted data window is read from the data block itself, and it is UTC.
    const dataWindow = within(panel).getByText(/2024-01-02T09:31:00/)
    expect(dataWindow.textContent).toContain('2024-01-05T09:34:00')
    expect(dataWindow.textContent).toContain('UTC')
    // The workspace's ceiling and what one confirmation allows are two different
    // facts, and the preview states both: confirming spends one backtest and no
    // model call.
    expect(within(panel).getByText(/10 次模型调用 · 5 次回测/)).toBeTruthy()
    expect(within(panel).getByText(/0 次模型调用 · 1 次回测/)).toBeTruthy()
    // The confirmation says what its click does.
    expect(within(panel).getByRole('button', { name: '确认并运行一次' })).toBeTruthy()
  })

  it('gives up an in-flight preview without writing anything, and can preview again afterwards', async () => {
    const user = userEvent.setup()
    // Two reads the test resolves by hand: the one given up on while it is still in
    // flight, and the fresh one that follows it.
    const abandoned = deferred<UserStrategyPreview>()
    const admitted = deferred<UserStrategyPreview>()
    const reads = [abandoned, admitted]
    const { api, calls } = scripted({ preview: () => reads.shift()!.promise })
    detail(api)
    const panel = await openSection(user, '实验')
    const confirm = () => within(panel).getByRole('button', { name: '确认并运行一次' }) as HTMLButtonElement
    const previewButton = within(panel).getByRole('button', { name: '预览实验' })

    await user.click(previewButton)
    expect(calls.preview).toBe(1)
    // While the read is in flight there is nothing to confirm, and giving up is
    // possible rather than having to wait for it.
    expect(confirm().disabled).toBe(true)
    await user.click(within(panel).getByRole('button', { name: '取消预览' }))

    // Giving up is local: it writes nothing and starts nothing.
    expect(calls.launch).toBe(0)
    expect(launched).toEqual([])

    // The read that was given up on lands afterwards, and it is not read as consent.
    await act(async () => { abandoned.resolve({ ...preview, initial_cash: '999999' }) })
    expect(confirm().disabled).toBe(true)
    expect(within(panel).queryByText('初始资金 999999 USD')).toBeNull()

    // Previewing again is a new read of its own, and it works normally.
    await user.click(previewButton)
    expect(calls.preview).toBe(2)
    await act(async () => { admitted.resolve(preview) })
    await waitFor(() => expect(confirm().disabled).toBe(false))
    expect(within(panel).getByText('初始资金 10000 USD')).toBeTruthy()
    expect(within(panel).queryByText('初始资金 999999 USD')).toBeNull()
    expect(calls.launch).toBe(0)
  })

  it('consumes the preview with one confirmation instead of running the same preview twice', async () => {
    const user = userEvent.setup()
    const pending = deferred<UserStrategy>()
    const frozen = strategy({
      experiments: [{
        launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
        payload: {
          draft_revision: 1, scenario_id: 'bounded-long.yaml',
          parameters: { target_quantity: '2', entry_delay_bars: 0 },
          preview_identity: PREVIEW_IDENTITY,
          approved_budget: { max_model_calls: 0, max_backtests: 1 },
        },
        task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00.000000Z',
      }],
    })
    const { api, calls } = scripted({ launch: () => pending.promise })
    detail(api)
    const panel = await openSection(user, '实验')
    const confirm = () => within(panel).getByRole('button', { name: '确认并运行一次' }) as HTMLButtonElement

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))
    await within(panel).findByText(/bounded-long\.yaml/)
    expect(confirm().disabled).toBe(false)

    await user.click(confirm())

    expect(calls.launch).toBe(1)
    // One confirmation is one run: while it is in flight it cannot be pressed again.
    expect(confirm().disabled).toBe(true)
    expect(launched).toEqual([{
      intent: 'li-1',
      baseRevision: 1,
      previewIdentity: PREVIEW_IDENTITY,
      approved: { max_model_calls: 0, max_backtests: 1 },
    }])
    // One more attempt at the very same rendered control, while the first launch is
    // still pending, dispatches nothing. The attempt is a click event on the button
    // itself rather than a pointer gesture, so what is under test is the control
    // refusing a second confirmation and not the helper declining to try.
    fireEvent.click(confirm())
    expect(calls.launch).toBe(1)

    await act(async () => { pending.resolve(frozen) })

    expect(await within(panel).findByText('已从修订 1 启动实验 li-1')).toBeTruthy()
    // That confirmation consumed the preview: the same preview is not a licence for
    // a second run, and there is nothing left here to confirm.
    expect(confirm().disabled).toBe(true)
    expect(within(panel).queryByText(/0 次模型调用 · 1 次回测/)).toBeNull()
    fireEvent.click(confirm())
    expect(calls.launch).toBe(1)
    expect(launched).toHaveLength(1)
  })

  it('requires a new preview after a refused launch instead of keeping the stale consent', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      launch: () => Promise.reject(
        new UserStrategyRequestError('user_strategy_invalid', 'the preview no longer describes what would run'),
      ),
    })
    detail(api)
    const panel = await openSection(user, '实验')
    const confirm = () => within(panel).getByRole('button', { name: '确认并运行一次' }) as HTMLButtonElement

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))
    await within(panel).findByText(/bounded-long\.yaml/)
    await user.click(confirm())

    expect(calls.launch).toBe(1)
    // The refusal is on screen in the user's words and in the host's own.
    const refusal = await screen.findByRole('alert')
    expect(refusal.textContent).toContain('这个操作对这条策略的当前状态无效，因此什么都没有改变。')
    expect(refusal.textContent).toContain('the preview no longer describes what would run')
    // A refused confirmation leaves nothing to confirm: it is not kept as consent.
    expect(confirm().disabled).toBe(true)
    expect(within(panel).queryByText(/0 次模型调用 · 1 次回测/)).toBeNull()
    expect(calls.launch).toBe(1)

    // Recovering means previewing again, and that fresh preview can be confirmed.
    await user.click(within(panel).getByRole('button', { name: '预览实验' }))
    expect(calls.preview).toBe(2)
    await waitFor(() => expect(confirm().disabled).toBe(false))
  })

  it('does not let a launch of an older revision overwrite the revision shown now', async () => {
    const user = userEvent.setup()
    const pending = deferred<UserStrategy>()
    const revised = strategy({
      draft: { ...executable(strategy()), revision: 2, parameters: { target_quantity: '3', entry_delay_bars: 0 } },
      proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [appliedChange],
    })
    const { api, calls } = scripted({
      current: () => strategy({ proposals: [proposal()] }),
      apply: async () => revised,
      launch: () => pending.promise,
    })
    detail(api)
    const panel = await openSection(user, '实验')

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))
    await within(panel).findByText(/bounded-long\.yaml/)
    await user.click(within(panel).getByRole('button', { name: '确认并运行一次' }))
    expect(calls.launch).toBe(1)
    // The confirmation is sent, and this page says that it is waiting rather than
    // pretending the request can be taken back.
    expect(within(panel).getByText(/正在等待宿主回答/)).toBeTruthy()

    // A parallel action on the same strategy moves the draft on while the launch is
    // still in flight. The next revision is its own panel, not the old consent.
    const proposals = await screen.findByRole('region', { name: '建议修改' })
    await user.click(within(proposals).getByRole('button', { name: '应用所选' }))
    expect(calls.apply).toBe(1)
    expect(await screen.findByText('v2 草稿')).toBeTruthy()
    const advanced = screen.getByRole('region', { name: '实验' })
    expect(within(advanced).queryByText(/正在等待宿主回答/)).toBeNull()
    expect((within(advanced).getByRole('button', { name: '预览实验' }) as HTMLButtonElement).disabled).toBe(false)

    // The launch answers afterwards with the revision it dispatched from. It is not
    // written over the revision that is shown now, and it reports no error there.
    await act(async () => {
      pending.resolve(strategy({
        experiments: [{
          launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
          payload: {
            draft_revision: 1, scenario_id: 'bounded-long.yaml',
            parameters: { target_quantity: '2', entry_delay_bars: 0 },
          },
          task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00.000000Z',
        }],
      }))
    })

    expect(screen.getByText('v2 草稿')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '实验' })).queryByText('已从修订 1 启动实验 li-1')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(calls.launch).toBe(1)
  })

  it('does not let a launch that outlives the page write into the strategy it left', async () => {
    const user = userEvent.setup()
    const pending = deferred<UserStrategy>()
    const { api, calls } = scripted({ strategies: [strategy()], launch: () => pending.promise })
    detail(api)
    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()

    const panel = await openSection(user, '实验')
    await user.click(within(panel).getByRole('button', { name: '预览实验' }))
    await within(panel).findByText(/bounded-long\.yaml/)
    await user.click(within(panel).getByRole('button', { name: '确认并运行一次' }))
    expect(calls.launch).toBe(1)

    // The user leaves while the launch is still in flight, and comes back before the
    // answer arrives: that answer belongs to the panel that was left.
    await user.click(screen.getByRole('link', { name: '返回研究列表' }))
    await user.click(await screen.findByRole('link', { name: 'Bold entry' }))
    expect(await screen.findByRole('region', { name: '草稿' })).toBeTruthy()

    await act(async () => {
      pending.resolve(strategy({
        experiments: [{
          launch_intent_id: 'li-1', request_id: 'request-1', payload_sha256: 'f'.repeat(64),
          payload: {
            draft_revision: 1, scenario_id: 'bounded-long.yaml',
            parameters: { target_quantity: '2', entry_delay_bars: 0 },
          },
          task_id: 'task-1', job_id: 'job-1', status: 'created', created_at: '2026-10-06T10:02:00.000000Z',
        }],
      }))
    })

    // What is shown is the strategy as it was read, not the run the left panel froze.
    const intent = screen.getByRole('region', { name: '意图' })
    expect(within(intent).getByText(/还没有确认过的实验/)).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(calls.launch).toBe(1)
  })

  it('keeps a frozen run on the revision it ran and never claims its outcome', async () => {
    const user = userEvent.setup()
    const frozen = strategy({
      draft: { ...executable(strategy()), revision: 3, parameters: { target_quantity: '2', entry_delay_bars: 1 } },
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
    expect(screen.getByText('v3 草稿')).toBeTruthy()
    expect(within(panel).getByText('已从修订 1 启动实验 li-1')).toBeTruthy()
    expect(within(panel).getByRole('link', { name: '查看这次实验报告' }).getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
    expect(within(panel).getByText('实验 li-2 来自修订 2 — 结果未知')).toBeTruthy()
    expect(panel.textContent).toMatch(/中断之前可能已经派发了一次运行/)
    expect(panel.textContent).toMatch(/The provider was reached but no outcome was observed./)
    // An outcome that was never observed is never written up as a result.
    expect(panel.textContent).not.toMatch(/profit|net pnl|guarantee/i)
    // The table carries the same frozen fact on the strategy's own row: the
    // revision its latest run froze, and the state of that launch — not a claim
    // that a started run succeeded.
    await user.click(screen.getByRole('link', { name: '返回研究列表' }))
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

  it('creates only on the collection route, and keeps the idea that was typed', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ strategies: [strategy()] })
    window.history.pushState({}, '', '/strategies')
    render(<App api={api} />)

    // A workspace with something to select opens no form.
    expect(await screen.findByRole('link', { name: 'Bold entry' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()

    await user.click(screen.getByRole('button', { name: '新建策略' }))
    const form = await screen.findByRole('region', { name: '新建策略' })
    await user.type(within(form).getByLabelText('意图'), 'A quieter entry')

    // Opening a strategy is not a reason to keep the creation form over it.
    await user.click(screen.getByRole('link', { name: 'Bold entry' }))
    expect(await screen.findByRole('heading', { name: 'Bold entry' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()
    expect(calls.create).toBe(0)

    // Coming back to the list offers the same half-written idea again.
    await user.click(screen.getByRole('link', { name: '返回研究列表' }))
    const back = await screen.findByRole('region', { name: '新建策略' })
    expect((within(back).getByLabelText('意图') as HTMLTextAreaElement).value).toBe('A quieter entry')
    expect(calls.create).toBe(0)
  })

  it('reads before it can claim an empty workspace or an unselected strategy', async () => {
    const user = userEvent.setup()
    const listed = deferred<UserStrategy[]>()
    const read = deferred<UserStrategy>()
    const { api } = scripted({ list: () => listed.promise, read: () => read.promise })
    window.history.pushState({}, '', '/strategies')
    render(<App api={api} />)

    // Nothing has been read, so there is no count, no empty claim and no form.
    expect((await screen.findByText('正在读取策略…')).getAttribute('role')).toBe('status')
    expect(screen.queryByText('还没有策略。')).toBeNull()
    expect(screen.queryByText('0 个策略')).toBeNull()
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()

    listed.resolve([strategy()])
    expect(await screen.findByRole('link', { name: 'Bold entry' })).toBeTruthy()
    expect(screen.getByText('1 个策略')).toBeTruthy()

    await user.click(screen.getByRole('link', { name: 'Bold entry' }))
    // The selected strategy has not answered yet: the AI column says that rather
    // than asking for a strategy that is already selected.
    expect(await screen.findByText('正在读取这条策略的对话…')).toBeTruthy()
    expect(screen.queryByText(/先创建或选择一条策略/)).toBeNull()

    read.resolve(strategy())
    expect(await screen.findByRole('region', { name: '草稿' })).toBeTruthy()
    expect(within(screen.getByRole('region', { name: 'AI 协作' })).getByLabelText('消息')).toBeTruthy()
  })

  it('keeps a failed read a failure rather than an empty or unselected page', async () => {
    const user = userEvent.setup()
    const refused = scripted({
      list: () => Promise.reject(new UserStrategyRequestError('request_failed', 'the service refused the list')),
    })
    window.history.pushState({}, '', '/strategies')
    render(<App api={refused.api} />)

    const listFailure = await screen.findByRole('alert')
    expect(listFailure.textContent).toContain('the service refused the list')
    expect(screen.queryByText('还没有策略。')).toBeNull()
    expect(screen.queryByRole('region', { name: '新建策略' })).toBeNull()

    cleanup()
    const missing = scripted({
      list: () => Promise.resolve([]),
      read: () => Promise.reject(new UserStrategyRequestError('user_strategy_not_found', 'no such strategy here')),
    })
    detail(missing.api, 'us-9')

    const detailFailure = await screen.findByRole('alert')
    expect(detailFailure.textContent).toContain('no such strategy here')
    expect(await screen.findByText('这条策略没有被读到，因此这里没有对话。')).toBeTruthy()
    expect(screen.queryByText(/先创建或选择一条策略/)).toBeNull()

    // A selected route whose strategy could not be read is not a dead end: its
    // header offers creation as a way to the collection route, where the form is,
    // and following it really opens that form.
    expect(screen.getByRole('link', { name: '新建策略' }).getAttribute('href')).toBe('/strategies')
    await user.click(screen.getByRole('link', { name: '新建策略' }))
    expect(await screen.findByRole('region', { name: '新建策略' })).toBeTruthy()
  })

  it('scrolls the history on its own and keeps the composer, approval and last call outside it', async () => {
    const { api } = scripted({
      current: () => strategy({
        // An earlier call that was observed: its messages are the earlier
        // conversation, kept out of the current turn.
        calls: [
          {
            call_id: 'call-0', base_revision: 1, status: 'observed',
            route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' },
            approved_budget: { max_model_calls: 1, max_backtests: 0 },
            reserved_at: '2026-10-06T10:00:00.000000Z', message: null, model: 'scripted-fixture-v1',
            input_tokens: 5, output_tokens: 3, usage_available: true,
          },
          // The latest call: never sent, and a cost the workspace could not price
          // stays an unavailable cost, said out loud rather than quietly read as zero.
          { ...unconfigured, call_id: 'call-1', cost: { amount: null, available: false, reason: '这个工作区没有价格来源' } },
        ],
        messages: [
          { message_id: 'msg-1', role: 'user', kind: 'message', text: 'What does my draft do?', base_revision: 1, at: '2026-10-06T10:01:00.000000Z', call_id: 'call-0', proposal_id: null },
          { message_id: 'msg-2', role: 'model', kind: 'message', text: 'A first answer that ran long. '.repeat(20), base_revision: 1, at: '2026-10-06T10:02:00.000000Z', call_id: 'call-0', proposal_id: null },
          { message_id: 'msg-3', role: 'user', kind: 'message', text: 'And the fees?', base_revision: 1, at: '2026-10-06T10:03:00.000000Z', call_id: 'call-0', proposal_id: null },
          { message_id: 'msg-4', role: 'model', kind: 'message', text: 'The scenario declares its own commission.', base_revision: 1, at: '2026-10-06T10:04:00.000000Z', call_id: 'call-0', proposal_id: null },
        ],
      }),
    })
    detail(api)
    // The real conversation, not the placeholder the AI column shows while the
    // strategy is still being read: the column is ready when its composer is.
    await screen.findByLabelText('消息')
    const chat = screen.getByRole('region', { name: 'AI 协作' })

    // Every earlier message is still here, inside one named, keyboard-scrollable
    // region that the current turn's own status, usage and cost stay out of.
    const history = within(chat).getByRole('group', { name: '对话记录' })
    expect(within(history).getAllByRole('listitem')).toHaveLength(4)
    expect(history.getAttribute('tabindex')).toBe('0')

    const outside = [
      within(chat).getByLabelText('消息'),
      within(chat).getByLabelText('批准一次模型调用'),
      within(chat).getByRole('button', { name: '询问模型' }),
      within(chat).getByText('未配置'),
      within(chat).getByText('这次调用没有上报 token 用量，因此不会按零计费'),
      within(chat).getByText('成本不可用：这个工作区没有价格来源。'),
    ]
    for (const control of outside) expect(history.contains(control)).toBe(false)

    // The recorded instant stays readable, and its exact value stays available.
    const stamp = within(history).getByText('10:01 UTC')
    expect(stamp.tagName).toBe('TIME')
    expect(stamp.getAttribute('title')).toBe('2026-10-06T10:01:00.000000Z')
    expect(stamp.getAttribute('datetime')).toBe('2026-10-06T10:01:00.000000Z')
  })

  it('asks for a decision only while a proposal is still open', async () => {
    const open = scripted({ current: () => strategy({ proposals: [proposal()] }) })
    detail(open.api)
    const openPanel = await screen.findByRole('region', { name: '建议修改' })
    expect(openPanel.textContent).toMatch(/针对草稿修订 1 · 提案 prop-1 · 由模型提出，等待你决定/)

    cleanup()
    const applied = scripted({
      current: () => strategy({
        proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      }),
    })
    detail(applied.api)
    const closedPanel = await screen.findByRole('region', { name: '建议修改' })

    // The status, the provenance, the fields and the decisions stay; only the
    // waiting for a decision that is already made is gone.
    expect(closedPanel.textContent).toMatch(/由模型提出，已应用/)
    expect(closedPanel.textContent).not.toMatch(/等待你决定/)
    expect(within(closedPanel).getByLabelText('parameters.target_quantity')).toBeTruthy()
    expect(closedPanel.textContent).toMatch(/应用会用整份已校验的草稿写出一个新修订/)
    // The whole record is one collapsed disclosure, so its controls are read after
    // opening it — and they are still there, inert.
    fireEvent.click(within(closedPanel).getByText(/的修改与来源/))
    expect((within(closedPanel).getByRole('button', { name: '应用所选' }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(closedPanel).getByRole('button', { name: '拒绝建议' }) as HTMLButtonElement).disabled).toBe(true)

    cleanup()
    const rejected = scripted({ current: () => strategy({ proposals: [proposal({ status: 'rejected' })] }) })
    detail(rejected.api)
    const rejectedPanel = await screen.findByRole('region', { name: '建议修改' })
    expect(rejectedPanel.textContent).toMatch(/由模型提出，已拒绝/)
    expect(rejectedPanel.textContent).not.toMatch(/等待你决定/)
  })

  it('names a created experiment as created, not as a run that is still starting', async () => {
    const user = userEvent.setup()
    const frozen = strategy({ experiments: [createdExperiment()] })
    const { api } = scripted({ strategies: [frozen], current: () => frozen })
    // The run column reads the run's own job; a created dispatch is not a result.
    api.getBacktest = async () => runJob
    detail(api)

    if (window.location.pathname !== '/strategies') await userEvent.setup().click(await screen.findByRole('link', { name: '返回研究列表' }))
    const table = within(await screen.findByRole('navigation', { name: '策略列表' }))
    // The row carries the job's own state, and not the record it was created from.
    const runLink = await table.findByRole('link', { name: '运行中' })
    expect(runLink.getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
    expect(table.queryByText('运行已创建')).toBeNull()
    expect(table.queryByText('运行已启动')).toBeNull()
    expect(table.queryByText(/成功/)).toBeNull()

    await user.click(table.getByRole('link', { name: 'Bold entry' }))
    const intent = await openSection(user, '意图')
    expect(within(intent).getByText('修订 1 · 运行已创建')).toBeTruthy()
  })

  it('returns to the list, and offers a keyboard path between the strategy and its conversation', async () => {
    const { api } = scripted({ strategies: [strategy()] })
    detail(api)
    await screen.findByRole('heading', { name: 'Bold entry' })

    // The detail no longer links to the page it is already on.
    expect(screen.queryByRole('link', { name: '打开策略' })).toBeNull()
    expect(screen.getByRole('link', { name: '返回研究列表' }).getAttribute('href')).toBe('/strategies')

    // Both jumps are real anchors to real, focusable targets. They are read as
    // text rather than by role because the stacked layout, not this test, decides
    // whether they are on screen.
    const toAi = screen.getByText('讨论这个研究')
    expect(toAi.tagName).toBe('A')
    expect(toAi.getAttribute('href')).toBe('#strategy-message')
    const column = document.getElementById('strategy-ai-column')!
    expect(column.getAttribute('tabindex')).toBe('-1')
    expect(column.contains(screen.getByRole('region', { name: 'AI 协作' }))).toBe(true)

    const toDetails = screen.getByText('返回策略详情')
    expect(toDetails.tagName).toBe('A')
    expect(toDetails.getAttribute('href')).toBe('#strategy-detail')
    const strategyDetail = document.getElementById('strategy-detail')!
    expect(strategyDetail.getAttribute('tabindex')).toBe('-1')
    expect(strategyDetail.contains(screen.getByRole('heading', { name: 'Bold entry' }))).toBe(true)
  })

  it('answers the latest call alone and keeps an earlier answer out of its turn', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      current: () => strategy({
        calls: [
          {
            call_id: 'call-1', base_revision: 1, status: 'observed',
            route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' },
            approved_budget: { max_model_calls: 1, max_backtests: 0 },
            reserved_at: '2026-10-06T10:01:00.000000Z', message: null, model: 'scripted-fixture-v1',
            input_tokens: 5, output_tokens: 3, usage_available: true,
          },
          {
            call_id: 'call-2', base_revision: 1, status: 'refused',
            route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' },
            approved_budget: { max_model_calls: 1, max_backtests: 0 },
            reserved_at: '2026-10-06T10:05:00.000000Z', message: 'The host refused this call.',
            model: null, input_tokens: null, output_tokens: null, usage_available: false,
          },
        ],
        messages: [
          { message_id: 'msg-1', role: 'user', kind: 'message', text: 'What does my draft do?', base_revision: 1, at: '2026-10-06T10:01:00.000000Z', call_id: 'call-1', proposal_id: null },
          { message_id: 'msg-2', role: 'model', kind: 'message', text: 'An older answer about the draft.', base_revision: 1, at: '2026-10-06T10:01:01.000000Z', call_id: 'call-1', proposal_id: null },
          { message_id: 'msg-3', role: 'user', kind: 'message', text: 'And now?', base_revision: 1, at: '2026-10-06T10:05:00.000000Z', call_id: 'call-2', proposal_id: null },
        ],
      }),
    })
    detail(api)
    // The AI column is a placeholder until this strategy's own read answers, so the
    // turn itself is what is waited for — not a timeout and not the column, which
    // exists either way.
    const turn = await screen.findByRole('region', { name: '当前一轮' })
    const chat = screen.getByRole('region', { name: 'AI 协作' })

    // The turn is the latest call's: its own status, its own reason and its own prompt.
    expect(within(turn).getByText('被拒绝')).toBeTruthy()
    expect(within(turn).getByText('The host refused this call.')).toBeTruthy()
    expect(within(turn).getByText('And now?')).toBeTruthy()
    // A refused call answers nothing, so the earlier answer is not shown as its own.
    expect(within(turn).queryByText('An older answer about the draft.')).toBeNull()
    expect(within(turn).getByText(/没有被观测到的模型回答/)).toBeTruthy()

    // It is still readable as history: the reader opens that disclosure, and the
    // region inside it is the one bounded, keyboard-scrollable surface.
    await user.click(within(chat).getByText(/更早的对话/))
    const history = within(chat).getByRole('group', { name: '对话记录' })
    expect(within(history).getByText('An older answer about the draft.')).toBeTruthy()
    expect(within(history).getByText('What does my draft do?')).toBeTruthy()
    expect(history.getAttribute('tabindex')).toBe('0')
    // Reading the conversation calls no model and starts nothing.
    expect(calls.ask).toBe(0)
  })

  it('warns that a preview repeats a frozen input without cancelling or disabling it', async () => {
    const user = userEvent.setup()
    const frozen = strategy({ experiments: [createdExperiment()] })
    const { api, calls } = scripted({ strategies: [frozen], current: () => frozen })
    api.getBacktest = async () => runJob
    detail(api)
    const panel = await openSection(user, '实验')

    await user.click(within(panel).getByRole('button', { name: '预览实验' }))

    // The warning names the frozen launch and revision it matches, and says what a
    // repeat is: the same engine input, not new evidence.
    const warning = await within(panel).findByText(/与已确认的冻结运行 li-1（修订 1）完全相同/)
    expect(warning.textContent).toMatch(/重复同一个引擎输入/)
    expect(warning.textContent).toMatch(/不构成新的证据/)
    // It is a warning, not a gate: nothing ran and nothing was disabled.
    expect(calls.preview).toBe(1)
    expect(calls.launch).toBe(0)
    expect((within(panel).getByRole('button', { name: '确认并运行一次' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('shows the run’s own job status, and says so when that read fails or is another job', async () => {
    const frozen = strategy({ experiments: [createdExperiment()] })
    const read = scripted({ strategies: [frozen], current: () => frozen })
    read.api.getBacktest = async () => runJob
    detail(read.api)
    if (window.location.pathname !== '/strategies') await userEvent.setup().click(await screen.findByRole('link', { name: '返回研究列表' }))
    const table = within(await screen.findByRole('navigation', { name: '策略列表' }))
    const link = await table.findByRole('link', { name: '运行中' })
    expect(link.getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
    expect(table.queryByText(/成功/)).toBeNull()

    cleanup()
    // A job that is not the one this row asked for is refused, not shown as its run.
    // The next step honestly reports the same refusal for its own read, so this row
    // behavior is read inside the list the row lives in; the strip's own rendering is
    // covered by the maturity tests.
    const other = scripted({ strategies: [frozen], current: () => frozen })
    other.api.getBacktest = async () => ({ ...runJob, job_id: 'job-2' })
    detail(other.api)
    await userEvent.setup().click(await screen.findByRole('link', { name: '返回研究列表' }))
    const mismatch = within(await screen.findByRole('navigation', { name: '策略列表' }))
    expect(await mismatch.findByText(/读取到的任务与这条运行不一致/)).toBeTruthy()

    cleanup()
    // A read that fails stays unreadable here, and still points at the run itself.
    const failed = scripted({ strategies: [frozen], current: () => frozen })
    failed.api.getBacktest = async () => { throw new Error('offline') }
    detail(failed.api)
    await userEvent.setup().click(await screen.findByRole('link', { name: '返回研究列表' }))
    const fallback = within(await screen.findByRole('navigation', { name: '策略列表' }))
    expect(await fallback.findByText(/运行状态不可读/)).toBeTruthy()
    expect(fallback.getByRole('link', { name: '打开运行' }).getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
  })

  it('opens the experiment tab from the next step, and runs nothing', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ strategies: [strategy()], current: () => strategy() })
    detail(api)
    await screen.findByRole('heading', { name: 'Bold entry' })

    // The strip names what is frozen, and it does not claim a created run has a
    // readable report. The revision that is open is not repeated here: the title chip
    // and the facts already name it, so the strip carries only the frozen side.
    const strip = screen.getByRole('region', { name: '下一步' })
    expect(within(strip).queryByText('当前草稿')).toBeNull()
    expect(within(strip).queryByText('v1 草稿')).toBeNull()
    expect(within(strip).getByText('还没有冻结运行')).toBeTruthy()
    // Nothing here claims an outcome the run has not reported, and the way forward is
    // a reading step, not a launch.
    expect(strip.textContent).not.toMatch(/成功|报告可读/)
    expect(strip.textContent).toMatch(/本草稿尚未冻结为实验/)

    await user.click(within(strip).getByRole('button', { name: '去预览这个草稿' }))

    // One click opens the existing tab: it reads no preview and starts no run.
    expect(await screen.findByRole('region', { name: '实验' })).toBeTruthy()
    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)
  })
})

/**
 * Maturity round 1: the same local research surface, made easier to move through
 * without adding a capability or weakening an approval or report rule. The composer
 * precedes the long turn it answers, a closed proposal is one collapsed disclosure
 * that still holds its whole history, and the next step reads the run's own job
 * state instead of the dispatch record it was created from.
 */
describe('maturity round 1', () => {
  /** One observed call, so a turn has an answer that belongs to it alone. */
  function observedCall(
    callId: string,
    overrides: Partial<UserStrategy['calls'][number]> = {},
  ): UserStrategy['calls'][number] {
    return {
      call_id: callId, base_revision: 1, status: 'observed',
      route: { provider: 'scripted-fixture', model: 'scripted-fixture-v1', endpoint: 'http://127.0.0.1:9/unused' },
      approved_budget: { max_model_calls: 1, max_backtests: 0 },
      reserved_at: '2026-10-06T10:01:00.000000Z', message: null, model: 'scripted-fixture-v1',
      input_tokens: 5, output_tokens: 3, usage_available: true,
      ...overrides,
    }
  }

  /** One application and the undo that reversed it, each with its own provenance. */
  function appliedThenUndone(overrides: Partial<UserStrategy> = {}): UserStrategy {
    return strategy({
      draft: { ...executable(strategy()), revision: 6, parameters: { target_quantity: '2', entry_delay_bars: 1 } },
      proposals: [proposal({ base_revision: 4, status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [
        { ...appliedChange, from_revision: 4, to_revision: 5 },
        {
          operation_id: 'op-undo-1', kind: 'undo', proposal_id: null, target_operation_id: 'op-apply-1',
          from_revision: 5, to_revision: 6, applied_paths: [], stale_paths: [], items: [],
          evidence: [], proposer: 'user', at: '2026-10-06T10:05:00.000000Z',
        },
      ],
      ...overrides,
    })
  }

  it('puts the composer before the turn it answers and the closed proposal beside it', async () => {
    const user = userEvent.setup()
    // The latest answer is the longest thing on the page: the reader must still meet
    // the composer before having to scroll or tab past all of it.
    const LONG_ANSWER = Array.from({ length: 160 }, (_, index) => `第${index}句`).join(' ')
    const reversed = appliedThenUndone({
      calls: [observedCall('call-0'), observedCall('call-1', { base_revision: 4 })],
      messages: [
        { message_id: 'msg-0', role: 'user', kind: 'message', text: 'An earlier question.', base_revision: 1, at: '2026-10-06T10:00:00.000000Z', call_id: 'call-0', proposal_id: null },
        { message_id: 'msg-1', role: 'model', kind: 'message', text: 'An earlier answer.', base_revision: 1, at: '2026-10-06T10:00:30.000000Z', call_id: 'call-0', proposal_id: null },
        { message_id: 'msg-2', role: 'user', kind: 'message', text: 'Explain the whole draft.', base_revision: 4, at: '2026-10-06T10:01:00.000000Z', call_id: 'call-1', proposal_id: null },
        { message_id: 'msg-3', role: 'model', kind: 'message', text: LONG_ANSWER, base_revision: 4, at: '2026-10-06T10:01:01.000000Z', call_id: 'call-1', proposal_id: null },
      ],
    })
    const { api, calls } = scripted({ strategies: [reversed], current: () => reversed })
    detail(api)

    // The chat region also carries the loading placeholder, so wait for the composer
    // itself before reading the region it lives in.
    await screen.findByLabelText('消息')
    const chat = screen.getByRole('region', { name: 'AI 协作' })
    const composer = within(chat).getByLabelText('消息')
    const turn = within(chat).getByRole('region', { name: '当前一轮' })
    const proposalPanel = within(chat).getByRole('region', { name: '建议修改' })

    // The usable action sequence is the DOM order, established by
    // compareDocumentPosition rather than read off a class name: the composer comes
    // before both the answer it belongs to and the closed proposal beside it.
    expect(precedes(composer, turn)).toBe(false)
    expect(precedes(composer, proposalPanel)).toBe(false)
    expect(precedes(turn, composer)).toBe(true)

    // The whole latest answer stays accessible, in its own bounded surface.
    const answer = within(turn).getByRole('group', { name: '本轮回答' })
    expect(within(answer).getByText(LONG_ANSWER)).toBeTruthy()

    // So does the earlier conversation, behind its own disclosure.
    await user.click(within(chat).getByText(/更早的对话/))
    expect(within(chat).getByText('An earlier answer.')).toBeTruthy()
    expect(within(chat).getByText('An earlier question.')).toBeTruthy()

    // Nothing ran on its own: no approval is set and no call was sent.
    expect(calls.ask).toBe(0)
    expect(asked).toEqual([])
    expect((within(chat).getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)
    expect((within(chat).getByLabelText('消息') as HTMLTextAreaElement).value).toBe('')
  })

  it('keeps a closed proposal whole inside one initially-collapsed disclosure that names the reversal', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ current: () => appliedThenUndone() })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    const summary = within(panel).getByText(/的修改与来源/)
    const disclosure = summary.closest('details') as HTMLDetailsElement | null
    expect(disclosure).toBeTruthy()
    // One native disclosure, closed until the reader opens it.
    expect(disclosure!.open).toBe(false)
    // Its summary carries the ledger's effective outcome alone — the reversal, not
    // the status the proposal was recorded with, so the collapsed line never signals
    // two states at once.
    expect(summary.textContent).toMatch(/已撤销的修改与来源/)
    expect(summary.textContent).not.toMatch(/已应用/)
    // The application was reversed, so none of these controls can do anything now.
    // They live with the rest of the record instead of on the first screen — the
    // still-unavailable Undo included.
    expect(collapsedAncestor(within(panel).getByText('应用所选'))).toBe(disclosure)
    expect(collapsedAncestor(within(panel).getByText('撤销最近一次修改'))).toBe(disclosure)

    await user.click(summary)
    expect(disclosure!.open).toBe(true)

    // The full history is inside it: origin, the status it was recorded with, the
    // reversal's provenance, the model's cited evidence and both sides of the field.
    expect(within(disclosure!).getByText(/由模型提出，已应用/)).toBeTruthy()
    // The status the proposal was recorded with is kept here, inside the record.
    expect(within(disclosure!).getByText('已应用')).toBeTruthy()
    expect(within(disclosure!).getByText(/针对草稿修订 4 · 提案 prop-1/)).toBeTruthy()
    expect(within(disclosure!).getByText(/这次应用已在修订 5 → 6 上被撤销/)).toBeTruthy()
    expect(within(disclosure!).getByText('之前 2')).toBeTruthy()
    expect(within(disclosure!).getByText('之后 3')).toBeTruthy()
    expect(within(disclosure!).getByText(/模型引用：idea: more size/)).toBeTruthy()

    // Opening the record reveals them, still inert: nothing here can apply, reject or
    // undo a change the ledger already reversed.
    expect((within(disclosure!).getByRole('button', { name: '应用所选' }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(disclosure!).getByRole('button', { name: '拒绝建议' }) as HTMLButtonElement).disabled).toBe(true)
    expect((within(disclosure!).getByRole('button', { name: '撤销最近一次修改' }) as HTMLButtonElement).disabled).toBe(true)
    expect(calls.apply).toBe(0)
    expect(calls.undo).toBe(0)
    expect(calls.reject).toBe(0)

    cleanup()
    // A closed proposal whose application is still the head revision keeps its Undo
    // live, and that Undo is reachable without opening the disclosure.
    const applied = strategy({
      draft: { ...executable(strategy()), revision: 2, parameters: { target_quantity: '3', entry_delay_bars: 0 } },
      proposals: [proposal({ status: 'applied', applied_paths: ['parameters.target_quantity'] })],
      changes: [appliedChange],
    })
    const second = scripted({ current: () => applied })
    detail(second.api)
    const appliedPanel = await screen.findByRole('region', { name: '建议修改' })
    const undo = within(appliedPanel).getByRole('button', { name: '撤销最近一次修改' }) as HTMLButtonElement
    expect(undo.disabled).toBe(false)
    const appliedDisclosure = within(appliedPanel).getByText(/的修改与来源/).closest('details') as HTMLDetailsElement
    expect(appliedDisclosure.open).toBe(false)
    expect(collapsedAncestor(undo)).toBeNull()

    cleanup()
    // An open proposal is the decision in front of the reader: its typed diff and its
    // Apply/reject controls are exposed at once, behind no disclosure.
    const opened = scripted({ current: () => strategy({ proposals: [proposal()] }) })
    detail(opened.api)
    const openedPanel = await screen.findByRole('region', { name: '建议修改' })
    const field = within(openedPanel).getByLabelText('parameters.target_quantity')
    expect(collapsedAncestor(field)).toBeNull()
    expect((within(openedPanel).getByRole('button', { name: '应用所选' }) as HTMLButtonElement).disabled).toBe(false)
    expect((within(openedPanel).getByRole('button', { name: '拒绝建议' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('reads the run’s own job state in the next step instead of the dispatch that created it', async () => {
    const frozen = strategy({ experiments: [createdExperiment()] })

    // A run that really succeeded and whose report is readable says exactly that, read
    // from the job itself rather than from the dispatch record.
    const succeededJob: BacktestJob = { ...runJob, status: 'succeeded', report_ready: true, report_sha256: 'd'.repeat(64) }
    const succeeded = scripted({ strategies: [frozen], current: () => frozen })
    succeeded.api.getBacktest = async () => succeededJob
    detail(succeeded.api)
    const strip = await screen.findByRole('region', { name: '下一步' })
    expect(await within(strip).findByRole('link', { name: '成功 · 报告可读' })).toBeTruthy()
    // The dispatch-only record is never repeated as if it were the run's outcome.
    expect(within(strip).queryByText(/运行已创建/)).toBeNull()

    cleanup()
    // A run that failed is never shown as a success here.
    const failedJob: BacktestJob = { ...runJob, status: 'failed', report_ready: false }
    const failed = scripted({ strategies: [frozen], current: () => frozen })
    failed.api.getBacktest = async () => failedJob
    detail(failed.api)
    const failedStrip = await screen.findByRole('region', { name: '下一步' })
    expect(await within(failedStrip).findByRole('link', { name: '失败' })).toBeTruthy()
    expect(failedStrip.textContent).not.toMatch(/成功/)

    cleanup()
    // A read that fails stays unreadable here and still points at the run itself —
    // and never becomes a success.
    const unreadable = scripted({ strategies: [frozen], current: () => frozen })
    unreadable.api.getBacktest = async () => { throw new Error('offline') }
    detail(unreadable.api)
    const unreadableStrip = await screen.findByRole('region', { name: '下一步' })
    expect(await within(unreadableStrip).findByText(/运行状态不可读/)).toBeTruthy()
    expect(within(unreadableStrip).getByRole('link', { name: '打开运行' }).getAttribute('href')).toBe('/strategies/us-1?tab=experiments&evidence_a=job-1')
    expect(unreadableStrip.textContent).not.toMatch(/成功/)
  })

  it('stops repeating the draft revision and the dispatch-only status where the facts already name them', async () => {
    const frozen = strategy({ experiments: [createdExperiment()] })

    // On the strategy's own page the revision is named by its title chip and its
    // facts, so the next step does not print it a third time.
    const detailRead = scripted({ strategies: [frozen], current: () => frozen })
    detailRead.api.getBacktest = async () => runJob
    detail(detailRead.api)
    await screen.findByRole('heading', { name: 'Bold entry' })
    const strip = await screen.findByRole('region', { name: '下一步' })
    expect(within(strip).queryByText('当前草稿')).toBeNull()
    expect(within(strip).queryByText('v1 草稿')).toBeNull()
    expect(strip.textContent).not.toMatch(/草稿修订 1/)
    expect(screen.getByText('v1 草稿')).toBeTruthy()

    cleanup()
    // The way back to the last strategy names the frozen revision, not a dispatch
    // state that is not the run's own outcome.
    const listed = scripted({ strategies: [frozen] })
    listed.api.getBacktest = async () => runJob
    window.history.pushState({}, '', '/strategies')
    render(<App api={listed.api} />)
    const continuation = await screen.findByRole('region', { name: '继续最近的策略' })
    expect(continuation.textContent).toMatch(/最近冻结 v1/)
    expect(continuation.textContent).not.toMatch(/运行已创建/)
  })
})

/**
 * Where a refused proposal action is announced. The decision controls live in the AI
 * column, which on a narrow screen sits far below whatever the page says at its top:
 * a refusal that is only announced up there is a refusal the reader never sees. It
 * belongs inside this proposal's own region, beside the controls that produced it —
 * and it belongs to that one attempt, so a later action does not leave it standing.
 */
describe('proposal action feedback', () => {
  it('announces a refused apply inside its own proposal region, beside the controls', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({
      current: () => strategy({ proposals: [proposal()] }),
      // The revision moved on while this page was open: the host refuses the apply.
      apply: async () => {
        throw new UserStrategyRequestError('stale_base_revision', 'the draft revision moved on')
      },
    })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    // The reader narrows the typed change to one field, so the selection that has to
    // survive the refusal is not the one the panel started with.
    await user.click(within(panel).getByLabelText('parameters.entry_delay_bars'))
    await user.click(within(panel).getByRole('button', { name: '应用所选' }))

    await waitFor(() => expect(calls.apply).toBe(1))
    // The refusal is announced where the click was made: inside this proposal's own
    // region, ahead of the controls, behind no disclosure.
    const alert = await within(panel).findByRole('alert')
    expect(alert.textContent).toContain('草稿修订已前进，这次修改没有被应用。')
    // The recovery fits the refusal: the same old proposal cannot be re-applied to a
    // draft that has moved on, so the reader is pointed at a new proposal or a
    // rejection — never at simply reloading and pressing Apply again.
    expect(alert.textContent).toContain('请基于当前草稿重新提出建议，或拒绝此建议。')
    expect(alert.textContent).toContain('the draft revision moved on')
    expect(collapsedAncestor(alert)).toBeNull()
    expect(precedes(alert, within(panel).getByRole('button', { name: '应用所选' }))).toBe(true)
    // Announced once, next to the action that failed — not also at the top of a page
    // the reader has since scrolled away from.
    expect(screen.getAllByText(/草稿修订已前进，这次修改没有被应用。/)).toHaveLength(1)

    // One click is one attempt: a refused apply is not retried on its own.
    expect(calls.apply).toBe(1)
    expect(applied).toEqual([{ proposalId: 'prop-1', operationId: 'op-prop-1-1-0', paths: ['parameters.target_quantity'] }])

    // The typed change and the reader's own selection are exactly as they were, and
    // nothing was written to the draft.
    expect((within(panel).getByLabelText('parameters.target_quantity') as HTMLInputElement).checked).toBe(true)
    expect((within(panel).getByLabelText('parameters.entry_delay_bars') as HTMLInputElement).checked).toBe(false)
    expect(within(panel).getByText('之前 2')).toBeTruthy()
    expect(within(panel).getByText('之后 3')).toBeTruthy()
    expect(within(panel).getByText('待处理')).toBeTruthy()
    expect(screen.getByText('v1 草稿')).toBeTruthy()
  })

  it('clears a refused apply when the reader decides the proposal instead', async () => {
    const user = userEvent.setup()
    // The strategy is seeded through the list the adapter persists against, so the
    // decision this test makes is answered by a persisted proposal that really
    // exists: a rejection returns the same strategy with that one proposal closed,
    // rather than a strategy the double had no proposal for.
    const seeded = strategy({ proposals: [proposal()] })
    const { api, calls } = scripted({
      strategies: [seeded],
      apply: async () => {
        throw new UserStrategyRequestError('stale_base_revision', 'the draft revision moved on')
      },
    })
    detail(api)
    const panel = await screen.findByRole('region', { name: '建议修改' })

    await user.click(within(panel).getByRole('button', { name: '应用所选' }))
    await waitFor(() => expect(calls.apply).toBe(1))
    await within(panel).findByRole('alert')

    await user.click(within(panel).getByRole('button', { name: '拒绝建议' }))
    expect(calls.reject).toBe(1)

    await waitFor(() => {
      // The decision is the visible closed line of the record, not a chip hidden
      // inside a disclosure the reader has not opened.
      expect(within(screen.getByRole('region', { name: '建议修改' })).getByText(/已拒绝的修改与来源/)).toBeTruthy()
    })
    // The refusal belonged to that attempt, not to the proposal: once the reader has
    // decided, no stale alert is left standing beside the decision.
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

/**
 * One selection, two surfaces.
 *
 * The report card at the top of the 证据 tab and the research panel below it are
 * about the same frozen run. The address is what they share: a return link from a
 * run's own report names that run, so the card shows that run's report — never the
 * latest one — and the research panel is grounded in that same run. Choosing on
 * either surface writes the address the other reads, an approval belongs to the
 * selection it was given for, and an address that names no run of this strategy
 * shows no report at all rather than the latest run's.
 *
 * Every job, report and figure below is a hand-written fixture: no engine result,
 * no model answer, and no run started by any of these tests.
 */
describe('shared evidence selection', () => {
  const OLD_JOB = 'job-1'
  const NEW_JOB = 'job-2'
  const LAST_JOB = 'job-3'

  /** What the address says now, read as parameters rather than as one string. */
  const address = () => new URLSearchParams(window.location.search)

  /** One run's own formal report. The figures differ per run, so a card is readable. */
  function figures(runId: string, equity: string, netPnl: string, totalReturn: string) {
    return {
      schema: 'ea.backtest-report.v1' as const,
      run_id: runId,
      economics: {
        ending_cash: [{ amount: equity, currency: 'USD' }],
        ending_positions: [],
        valuation: { price: '1', position_value: '0' },
        equity: { amount: equity, currency: 'USD' },
        net_pnl: { amount: netPnl, currency: 'USD' },
        total_return: { value: totalReturn },
        counts: { orders: 1, fills: 1 },
        execution: { order: null, fill: null },
      },
    }
  }

  /** One succeeded job, whose own report is the report read back for it. */
  function succeeded(jobId: string, engineRunId: string, sha: string): BacktestJob {
    return {
      schema: 'ea.local-web-job.v5', job_id: jobId, request_id: `request-${jobId}`,
      scenario_id: 'bounded-long.yaml',
      input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
      status: 'succeeded', engine_run_id: engineRunId, report_sha256: sha, summary_sha256: null,
      error_code: null, message: null, report_ready: true,
    }
  }

  const JOBS: Record<string, BacktestJob> = {
    [OLD_JOB]: succeeded(OLD_JOB, 'run-1', 'd'.repeat(64)),
    [NEW_JOB]: succeeded(NEW_JOB, 'run-2', 'e'.repeat(64)),
    [LAST_JOB]: succeeded(LAST_JOB, 'run-3', 'f'.repeat(64)),
  }
  const REPORTS = {
    [OLD_JOB]: figures('run-1', '10020.25', '20.25', '0.2025'),
    [NEW_JOB]: figures('run-2', '10012.5', '12.5', '0.125'),
    [LAST_JOB]: figures('run-3', '10005.75', '5.75', '0.0575'),
  }

  /** One created experiment: the dispatch exists, and its run is its own job. */
  function frozenRun(
    launchIntentId: string,
    jobId: string,
    revision: number,
    quantity: string,
    at: string,
  ) {
    return {
      launch_intent_id: launchIntentId, request_id: `request-${jobId}`, payload_sha256: 'f'.repeat(64),
      payload: {
        draft_revision: revision, scenario_id: 'bounded-long.yaml',
        parameters: { target_quantity: quantity, entry_delay_bars: 0 },
      },
      task_id: `task-${jobId}`, job_id: jobId, status: 'created' as const, created_at: at,
    }
  }

  /** One strategy with an older run, a newer one, and the latest one. */
  function frozenRuns(): UserStrategy {
    return strategy({
      experiments: [
        frozenRun('li-1', OLD_JOB, 1, '2', '2026-10-06T10:02:00.000000Z'),
        frozenRun('li-2', NEW_JOB, 2, '3', '2026-10-06T10:03:00.000000Z'),
        frozenRun('li-3', LAST_JOB, 3, '4', '2026-10-06T10:04:00.000000Z'),
      ],
    })
  }

  /** The workspace over those runs, with both surfaces reading the same jobs. */
  function wired() {
    const value = frozenRuns()
    const { api, calls } = scripted({ strategies: [value], current: () => value })
    api.getBacktest = async jobId => {
      const job = JOBS[jobId]
      if (!job) throw new Error(`this fixture has no run ${jobId}`)
      return job
    }
    api.getReport = async jobId => {
      const report = REPORTS[jobId as keyof typeof REPORTS]
      if (!report) throw new Error(`this fixture has no report for ${jobId}`)
      return report
    }
    return { api, calls }
  }

  // Exercise the two existing evidence readers together so all original shared
  // URL/identity assertions remain intact. The App's actual tab navigation is
  // separately tested in research workspace batch 1.
  function EvidenceViews({ api }: { api: ApiAdapter }) {
    const [current, setCurrent] = useState<UserStrategy | null>(null)
    useEffect(() => {
      let active = true
      api.getUserStrategy!('us-1').then(value => { if (active) setCurrent(value) })
      return () => { active = false }
    }, [api])
    return <BrowserRouter>{current && <>
      <EvidenceSection api={api} strategy={current} />
      <StrategyEvidenceResearch api={api} strategy={current} changed={setCurrent} failed={() => undefined} />
    </>}</BrowserRouter>
  }

  /** Opening the workspace at an address, the way a return link leaves one behind. */
  function openedAt(api: ApiAdapter, search: string) {
    window.history.pushState({}, '', `/strategies/us-1${search}`)
    render(<EvidenceViews api={api} />)
  }

  it('opens the run the address names, on the card and in the research panel', async () => {
    const { api, calls } = wired()
    // The address a return link from the older run's own report leaves behind.
    openedAt(api, `?evidence_a=${OLD_JOB}`)

    const card = await screen.findByRole('region', { name: '证据' })
    const research = await screen.findByRole('region', { name: '证据对比' })

    // The card is the older run's own report: its figures and its job, never the
    // latest run's.
    expect(await within(card).findByText('20.25 USD')).toBeTruthy()
    expect(within(card).getByText('10020.25 USD', { selector: '.evidence-metrics dd' })).toBeTruthy()
    expect(within(card).queryByText('5.75 USD')).toBeNull()
    expect(within(card).getByText(OLD_JOB)).toBeTruthy()
    // The card keeps its convenient selector, and that selector is A — the same
    // choice the research panel below calls A, named as such on the card.
    const selector = within(card).getByRole('combobox') as HTMLSelectElement
    expect(selector.value).toBe('li-1')
    expect(within(card).getByRole('combobox', { name: /A/ })).toBe(selector)
    // The panel is grounded in the same run: A first, and no B.
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')
    expect((within(research).getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
    expect(within(research).getByText(/修订 1 · 确认 li-1 · 运行 job-1/)).toBeTruthy()
    // Two surfaces of one selection still ask nothing and start nothing.
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
    expect(launched).toEqual([])
  })

  it('keeps the same A across a reload and across back and forward', async () => {
    const { api } = wired()
    openedAt(api, `?evidence_a=${OLD_JOB}`)
    let card = await screen.findByRole('region', { name: '证据' })
    let research = await screen.findByRole('region', { name: '证据对比' })
    expect(await within(card).findByText('20.25 USD')).toBeTruthy()
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')

    // A fresh load over the same address reads the same run: nothing here is
    // remembered from the visit before.
    cleanup()
    render(<EvidenceViews api={api} />)
    card = await screen.findByRole('region', { name: '证据' })
    research = await screen.findByRole('region', { name: '证据对比' })
    expect(await within(card).findByText('20.25 USD')).toBeTruthy()
    expect((within(card).getByRole('combobox') as HTMLSelectElement).value).toBe('li-1')
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')

    // The browser's own move to a newer run and back to this one: the card and the
    // panel travel together, because both read the address rather than a memory.
    window.history.pushState({ key: 'shared-newer' }, '', `/strategies/us-1?evidence_a=${NEW_JOB}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect((within(card).getByRole('combobox') as HTMLSelectElement).value).toBe('li-2'))
    await waitFor(() => expect(within(card).getByText('12.5 USD')).toBeTruthy())
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')

    window.history.pushState({ key: 'shared-older' }, '', `/strategies/us-1?evidence_a=${OLD_JOB}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect((within(card).getByRole('combobox') as HTMLSelectElement).value).toBe('li-1'))
    await waitFor(() => expect(within(card).getByText('20.25 USD')).toBeTruthy())
    expect(within(card).queryByText('12.5 USD')).toBeNull()
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')
  })

  it('makes the card selector and the research chooser one choice in the address', async () => {
    const user = userEvent.setup()
    const { api, calls } = wired()
    // An unrelated parameter already in the address is none of this selection's
    // business and stays where it is.
    openedAt(api, `?evidence_a=${OLD_JOB}&view=x`)
    const card = await screen.findByRole('region', { name: '证据' })
    const research = await screen.findByRole('region', { name: '证据对比' })
    await within(card).findByText('20.25 USD')

    // Choosing on the card is choosing A: the address says so, and the panel below is
    // grounded in the run that was chosen.
    await user.selectOptions(within(card).getByRole('combobox'), 'li-3')
    await waitFor(() => expect(address().get('evidence_a')).toBe(LAST_JOB))
    await waitFor(() => expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-3'))
    await waitFor(() => expect(within(card).getByText('5.75 USD')).toBeTruthy())
    expect(address().get('view')).toBe('x')

    // And the other way round: the chooser is the same choice, not a second copy of it.
    await user.selectOptions(within(research).getByLabelText('运行 A'), 'li-2')
    await waitFor(() => expect(address().get('evidence_a')).toBe(NEW_JOB))
    await waitFor(() => expect((within(card).getByRole('combobox') as HTMLSelectElement).value).toBe('li-2'))
    await waitFor(() => expect(within(card).getByText('12.5 USD')).toBeTruthy())
    expect(address().get('view')).toBe('x')
    // Selecting a run reads that run: it asks the model nothing and starts nothing.
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('retires the approval and keeps or drops B when the card changes A', async () => {
    const user = userEvent.setup()
    const { api, calls } = wired()
    // The older run as A and the latest as B: both are this strategy's own runs, and
    // the comparison link names them in that order.
    openedAt(api, `?evidence_a=${OLD_JOB}&evidence_b=${LAST_JOB}`)
    const card = await screen.findByRole('region', { name: '证据' })
    const research = await screen.findByRole('region', { name: '证据对比' })
    await within(card).findByText('20.25 USD')
    expect((within(research).getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-3')
    expect(within(research).getByRole('link', { name: '独立比较链接' }).getAttribute('href'))
      .toBe(`/backtests/compare/${OLD_JOB}/${LAST_JOB}`)

    // An approval given for exactly this pair, with nothing asked yet.
    const approve = () => within(research).getByLabelText('批准一次模型调用') as HTMLInputElement
    const explain = () => within(research).getByRole('button', { name: '解释所选证据' }) as HTMLButtonElement
    await waitFor(() => expect(approve().disabled).toBe(false))
    await user.click(approve())
    await waitFor(() => expect(explain().disabled).toBe(false))

    // The card chooses another A. The B it is not naming is still a different, valid
    // run, so it stays; the approval belonged to the pair that was on screen.
    await user.selectOptions(within(card).getByRole('combobox'), 'li-2')
    await waitFor(() => expect(address().get('evidence_a')).toBe(NEW_JOB))
    await waitFor(() => expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2'))
    expect(address().get('evidence_b')).toBe(LAST_JOB)
    expect((within(research).getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-3')
    expect(approve().checked).toBe(false)
    expect(explain().disabled).toBe(true)
    expect(calls.ask).toBe(0)

    // Choosing the run that is already B as A drops B: one run cannot be both sides.
    await user.selectOptions(within(card).getByRole('combobox'), 'li-3')
    await waitFor(() => expect(address().get('evidence_a')).toBe(LAST_JOB))
    await waitFor(() => expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-3'))
    expect(address().get('evidence_b')).toBeNull()
    expect((within(research).getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('shows no fallback report when the address names no run of this strategy', async () => {
    const user = userEvent.setup()
    const { api, calls } = wired()
    // A run that is not this strategy's own — not the latest one, not any of them.
    openedAt(api, '?evidence_a=job-elsewhere')

    const card = await screen.findByRole('region', { name: '证据' })
    const research = await screen.findByRole('region', { name: '证据对比' })
    expect(within(research).getByRole('alert').textContent).toContain('无效的证据选择')
    expect(within(research).queryByLabelText('运行 A')).toBeNull()
    // The card does not quietly become the latest run's report, and it names no run
    // it was not asked about.
    expect(within(card).queryByText(/USD/)).toBeNull()
    expect(card.textContent).not.toMatch(/job-\d/)
    expect(calls.ask).toBe(0)

    // Clearing the refused selection is the way out: both surfaces fall back to the
    // plain default — the latest run as A, and no B.
    await user.click(within(research).getByRole('button', { name: '清除无效的证据选择' }))
    await waitFor(() => expect(address().get('evidence_a')).toBeNull())
    await waitFor(() => expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-3'))
    await waitFor(() => expect(within(card).getByText('5.75 USD')).toBeTruthy())
    expect(within(card).getByText(LAST_JOB)).toBeTruthy()
  })

  it('refuses a report of another run on the card the address names', async () => {
    const { api } = wired()
    // The address names the older run, and the report read back for it belongs to a
    // different run: it is not that run's result, and no surface may show it as one.
    api.getReport = async jobId => jobId === OLD_JOB
      ? figures('run-2', '10020.25', '20.25', '0.2025')
      : REPORTS[jobId as keyof typeof REPORTS]
    openedAt(api, `?evidence_a=${OLD_JOB}`)

    const card = await screen.findByRole('region', { name: '证据' })
    const research = await screen.findByRole('region', { name: '证据对比' })
    expect(await within(card).findByText(/报告的身份与该运行的 engine run id 不一致/)).toBeTruthy()
    expect(within(card).queryByText(/USD/)).toBeNull()
    // The panel is grounded in the same run, and that run has no readable report: its
    // approval cannot be armed from one.
    expect((within(research).getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')
    expect((within(research).getByLabelText('批准一次模型调用') as HTMLInputElement).disabled).toBe(true)
    expect((within(research).getByRole('button', { name: '解释所选证据' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
