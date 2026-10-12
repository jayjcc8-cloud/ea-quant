/**
 * The evidence a user strategy's conversation may be grounded in.
 *
 * Every strategy document, job and report below is hand-written fixture data, not
 * a model output and not an engine result. What these tests prove is the product
 * rule: the panel lists only this strategy's own frozen runs, shows the report's
 * own figures and exact B − A deltas — or says honestly that they are unavailable
 * — and each of its two actions is one explicitly approved model call that carries
 * the selected ids and starts nothing. No test here starts a run, applies a change
 * on its own, or lets a missing value pass as a zero.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob, type BacktestReport, type ScenarioSummary } from './app'
import {
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyEvidenceRun,
  type UserStrategyExperiment,
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

const EARLIER_JOB = 'job-1'
const LATEST_JOB = 'job-2'

function job(id: string, over: Partial<BacktestJob> = {}): BacktestJob {
  return {
    schema: 'ea.local-web-job.v5',
    job_id: id,
    request_id: `request-${id}`,
    scenario_id: 'bounded-long.yaml',
    input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'c'.repeat(64), record_count: 4 },
    status: 'succeeded',
    engine_run_id: `run-${id}`,
    report_sha256: 'd'.repeat(64),
    summary_sha256: 'e'.repeat(64),
    error_code: null,
    message: null,
    report_ready: true,
    ...over,
  }
}

/** One report's own figures, printed by the panel exactly as they are written here. */
type Figures = {
  equity: string
  net_pnl: string
  fees: string
  total_return: string
  orders: number
  fills: number
  currency?: string
}

function report(runId: string, figures: Figures): BacktestReport {
  const unit = (amount: string) => figures.currency ? { amount, currency: figures.currency } : { amount }
  return {
    schema: 'ea.backtest-report.v1',
    run_id: runId,
    economics: {
      ...(figures.currency ? { currency: figures.currency } : {}),
      initial_funding: unit('10000'),
      ending_cash: [unit('10000')],
      ending_positions: [],
      valuation: { price: '150', position_value: '0' },
      equity: unit(figures.equity),
      net_pnl: unit(figures.net_pnl),
      fees: {
        amount: figures.fees, count: figures.fills, rule: 'deterministic-commission-v1',
        ...(figures.currency ? { currency: figures.currency } : {}),
      },
      total_return: { value: figures.total_return },
      counts: { orders: figures.orders, fills: figures.fills },
      execution: { order: null, fill: null },
    },
  } as unknown as BacktestReport
}

/** Two frozen runs of one strategy, each with the assumptions it really ran under. */
function experiment(over: Partial<UserStrategyExperiment> & {
  launch_intent_id: string
  job_id: string
  revision: number
  quantity: string
  commission_bps: string
  data_window: [string, string]
}): UserStrategyExperiment {
  const {
    launch_intent_id, job_id, revision, quantity, commission_bps,
    data_window: [start, end], ...rest
  } = over
  return {
    launch_intent_id,
    request_id: `request-${launch_intent_id}`,
    payload_sha256: 'a'.repeat(64),
    payload: {
      draft_revision: revision,
      scenario_id: 'bounded-long.yaml',
      parameters: { target_quantity: quantity, entry_delay_bars: 0 },
      input_sha256: 'f'.repeat(64),
      input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'c'.repeat(64), record_count: 4 },
      normalized: {
        strategy: { target_quantity: quantity, entry_delay_bars: 0 },
        execution: {
          policy: 'phase1.next-bar-close.v1',
          commission: { policy: 'deterministic-commission-v1', commission_bps },
        },
        risk: { max_order_quantity: '2' },
        funding: { currency: 'USD' },
        instrument: { venue: 'XNAS', symbol: 'AAPL' },
        data: { path: 'prices.csv', start_utc: start, end_utc: end },
      },
    },
    task_id: `task-${launch_intent_id}`,
    job_id,
    status: 'created',
    created_at: '2026-10-06T10:02:00.000000Z',
    ...rest,
  }
}

const EARLIER = experiment({
  launch_intent_id: 'li-1', job_id: EARLIER_JOB, revision: 1, quantity: '2', commission_bps: '100',
  data_window: ['2024-01-02T09:31:00.000000Z', '2024-01-05T09:34:00.000000Z'],
})
const LATEST = experiment({
  launch_intent_id: 'li-2', job_id: LATEST_JOB, revision: 2, quantity: '3', commission_bps: '200',
  data_window: ['2024-02-01T09:31:00.000000Z', '2024-02-05T09:34:00.000000Z'],
})

/** The two reports: the earlier run made more, and the panel must not say why. */
const REPORTS: Record<string, BacktestReport> = {
  [EARLIER_JOB]: report('run-' + EARLIER_JOB, {
    equity: '10020.25', net_pnl: '20.25', fees: '1.25', total_return: '0.2025',
    orders: 4, fills: 3, currency: 'USD',
  }),
  [LATEST_JOB]: report('run-' + LATEST_JOB, {
    equity: '10012.5', net_pnl: '12.5', fees: '0.5', total_return: '0.125',
    orders: 2, fills: 2, currency: 'USD',
  }),
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
      revision: 2,
      strategy_id: 'bounded-long-v1',
      venue: 'XNAS',
      symbol: 'AAPL',
      scenario_id: 'bounded-long.yaml',
      dataset_id: null,
      source_sha256: null,
      initial_cash: '10000',
      parameters: { target_quantity: '3', entry_delay_bars: 0 },
      criterion: CRITERION,
    },
    history: [],
    messages: [],
    calls: [],
    proposals: [],
    changes: [],
    experiments: [EARLIER, LATEST],
    ...overrides,
  }
}

/** One recorded call, grounded in the two runs the user selected. */
function call(evidence: UserStrategyEvidenceRun[], callId = 'call-1'): UserStrategyCall {
  return {
    call_id: callId,
    base_revision: 2,
    status: 'observed',
    route: { provider: 'fixture', model: 'fixture-model', endpoint: 'https://fixture.invalid/v1/messages' },
    approved_budget: { max_model_calls: 1, max_backtests: 0 },
    reserved_at: '2026-10-06T10:04:00.000000Z',
    message: null,
    model: 'fixture-model',
    input_tokens: 10,
    output_tokens: 5,
    usage_available: true,
    evidence,
  }
}

const SELECTED: UserStrategyEvidenceRun[] = [
  {
    job_id: LATEST_JOB, launch_intent_id: 'li-2', draft_revision: 2, criterion: CRITERION,
    run_id: `run-${LATEST_JOB}`, report_sha256: 'd'.repeat(64), input_sha256: 'f'.repeat(64),
    record_count: 4, parameters: { target_quantity: '3', entry_delay_bars: 0 },
    assumptions: {}, facts: [], reading: { outcome: 'met', inference: 'criterion_met' }, limitations: [],
  },
  {
    job_id: EARLIER_JOB, launch_intent_id: 'li-1', draft_revision: 1, criterion: CRITERION,
    run_id: `run-${EARLIER_JOB}`, report_sha256: 'd'.repeat(64), input_sha256: 'f'.repeat(64),
    record_count: 4, parameters: { target_quantity: '2', entry_delay_bars: 0 },
    assumptions: {}, facts: [], reading: { outcome: 'met', inference: 'criterion_met' }, limitations: [],
  },
]

const NEXT_PROPOSAL: UserStrategyProposal = {
  proposal_id: 'prop-next',
  call_id: 'call-2',
  base_revision: 2,
  status: 'open',
  created_at: '2026-10-06T10:05:00.000000Z',
  items: [{
    path: 'parameters.target_quantity', before: '3', after: '4',
    reason: 'Test a larger size next, against the selected runs.',
    evidence: ['the two selected frozen runs'], proposer: 'model',
  }],
  applied_paths: [],
  stale_paths: [],
}

type Sent = {
  callId: string
  baseRevision: number
  text: string
  approvedBudget: { max_model_calls: number; max_backtests: number }
  evidenceJobIds?: string[]
}

type Script = {
  report?: (jobId: string) => Promise<BacktestReport>
  backtest?: (jobId: string) => Promise<BacktestJob>
  ask?: (id: string) => Promise<UserStrategy>
}

function scripted(script: Script = {}) {
  const calls = { get: 0, ask: 0, preview: 0, launch: 0, apply: 0 }
  const sent: Sent[] = []
  const applied: { proposalId: string; paths: string[] | null }[] = []
  const readJobs: string[] = []
  let persisted = strategy()
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    listScenarios: async () => [scenario],
    settingsStatus: async () => ({ schema: 'radian.settings-status.v1', configured: true, settings_file: '/fixture/settings.json', provider: 'fixture', model: 'fixture', base_url: null, api_key_env: null }),
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { throw new Error('unused') },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async (jobId) => {
      readJobs.push(jobId)
      return script.backtest ? script.backtest(jobId) : job(jobId)
    },
    getReport: async (jobId) => script.report ? script.report(jobId) : REPORTS[jobId],
    getEquityPath: async () => { throw new Error('no equity path in these fixtures') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    listUserStrategies: async () => [persisted],
    getUserStrategy: async () => { calls.get += 1; return persisted },
    previewUserStrategyExperiment: async () => { calls.preview += 1; throw new Error('unused') },
    launchUserStrategyExperiment: async () => { calls.launch += 1; throw new Error('unused') },
    sendUserStrategyMessage: async (id, input) => {
      calls.ask += 1
      sent.push({
        callId: input.call_id,
        baseRevision: input.base_revision,
        text: input.text,
        approvedBudget: input.approved_budget,
        evidenceJobIds: input.evidence_job_ids,
      })
      if (!script.ask) throw new Error('unused')
      persisted = await script.ask(id)
      return persisted
    },
    applyUserStrategyProposal: async (_id, proposalId, input) => {
      calls.apply += 1
      applied.push({ proposalId, paths: input.paths ?? null })
      persisted = {
        ...persisted,
        draft: {
          kind: 'strategy',
          revision: 3,
          strategy_id: 'bounded-long-v1',
          venue: 'XNAS',
          symbol: 'AAPL',
          scenario_id: 'bounded-long.yaml',
          dataset_id: null,
          source_sha256: null,
          initial_cash: '10000',
          parameters: { target_quantity: '4', entry_delay_bars: 0 },
          criterion: CRITERION,
        },
        proposals: persisted.proposals.map(item =>
          item.proposal_id === proposalId ? { ...item, status: 'applied' as const } : item),
      }
      return persisted
    },
  }
  return { api, calls, sent, applied, readJobs }
}

function detail(api: ApiAdapter) {
  window.history.pushState({}, '', '/strategies/us-1')
  render(<App api={api} />)
}

/** The same strategy opened at an explicit address, as a return link would open it. */
function detailAt(api: ApiAdapter, search: string) {
  window.history.pushState({}, '', `/strategies/us-1${search}`)
  render(<App api={api} />)
}

/** The evidence tab of the open strategy, and this panel inside it. */
async function openResearch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: '比较' }))
  const region = await screen.findByRole('region', { name: '证据对比' })
  return within(region)
}

const disabled = (element: HTMLElement) => (element as HTMLButtonElement).disabled

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('user strategy evidence comparison', () => {
  it('shows one frozen run honestly, with no B and no A/B difference', async () => {
    const user = userEvent.setup()
    // One real run is one run: there is no second report to compare against, and
    // nothing here invents one.
    const { api, calls, sent } = scripted({
      ask: async () => { throw new Error('asking was not approved') },
    })
    api.listUserStrategies = async () => [strategy({ experiments: [LATEST] })]
    api.getUserStrategy = async () => strategy({ experiments: [LATEST] })
    detail(api)
    const region = await openResearch(user)

    const b = region.getByLabelText('运行 B') as HTMLSelectElement
    expect(Array.from(b.options).map(option => option.textContent))
      .toEqual(['不含 B（只解释 A）'])
    expect(region.getByText('这条策略只有一次冻结运行，因此没有 B 可选，也不会显示任何 A/B 差值。')).toBeTruthy()
    expect(region.queryByText(/^B − A/)).toBeNull()
    // Reading and mounting are reads: no model call, no preview, no run.
    expect(calls.ask).toBe(0)
    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)
    expect(sent).toEqual([])
  })

  it('reads two runs and shows exact B − A deltas, their differences and their limits', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted()
    detail(api)
    const region = await openResearch(user)

    // A defaults to the latest frozen run; B is the earlier one the user selects.
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    expect(region.queryByText(/不显示任何 A\/B 差值/)).toBeNull()

    await waitFor(() => expect(region.getAllByText('+7.75 USD').length).toBe(2))
    expect(region.getByText('+0.75 USD')).toBeTruthy()
    expect(region.getByText('+7.75 pp')).toBeTruthy()
    expect(region.getByText('+2')).toBeTruthy()
    expect(region.getByText('+1')).toBeTruthy()
    // The differences that make these two runs incomparable are named as limits,
    // and no winner is declared. The edited quantity is a configuration difference,
    // so the block that carries it is not called an implementation difference.
    expect(region.getByText('parameters.target_quantity：3 → 2')).toBeTruthy()
    expect(region.getByText(/在数据窗口、策略配置、成本假设上不同/)).toBeTruthy()
    expect(region.queryByText(/策略实现/)).toBeNull()
    expect(region.getByText(/这里也不排名、不选优/)).toBeTruthy()
    expect(region.queryByText(/获胜|胜出|推荐/)).toBeNull()
    expect(new URLSearchParams(window.location.search).get('evidence_b')).toBe(EARLIER_JOB)
    // The identity of what would be explained is visible without asking anything.
    expect(region.getByText(/修订 2 · 确认 li-2 · 运行 job-2/)).toBeTruthy()
    expect(region.getByText(/修订 1 · 确认 li-1 · 运行 job-1/)).toBeTruthy()
    expect(calls.ask).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('refuses to difference amounts whose currency the report never recorded', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      report: async jobId => report(`run-${jobId}`, {
        equity: jobId === LATEST_JOB ? '10012.5' : '10020.25',
        net_pnl: jobId === LATEST_JOB ? '12.5' : '20.25',
        fees: jobId === LATEST_JOB ? '0.5' : '1.25',
        total_return: jobId === LATEST_JOB ? '0.125' : '0.2025',
        orders: jobId === LATEST_JOB ? 2 : 4,
        fills: jobId === LATEST_JOB ? 2 : 3,
      }),
    })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')

    await waitFor(() => expect(region.getAllByText('币种未记录，不做差').length).toBe(3))
    // The counts and the return still difference exactly: only the unit is missing.
    expect(region.getByText('+2')).toBeTruthy()
    expect(region.getByText('+7.75 pp')).toBeTruthy()
    expect(region.queryByText('+0 USD')).toBeNull()
    expect(region.queryByText('0 USD')).toBeNull()
  })

  it('refuses to difference amounts recorded in different currencies', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      report: async jobId => report(`run-${jobId}`, {
        equity: jobId === LATEST_JOB ? '10012.5' : '10020.25',
        net_pnl: jobId === LATEST_JOB ? '12.5' : '20.25',
        fees: jobId === LATEST_JOB ? '0.5' : '1.25',
        total_return: jobId === LATEST_JOB ? '0.125' : '0.2025',
        orders: jobId === LATEST_JOB ? 2 : 4,
        fills: jobId === LATEST_JOB ? 2 : 3,
        currency: jobId === LATEST_JOB ? 'USD' : 'EUR',
      }),
    })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')

    await waitFor(() => expect(region.getAllByText('币种不同，不做差').length).toBe(3))
    expect(region.queryByText('+7.75 USD')).toBeNull()
    expect(region.queryByText('+0.75 USD')).toBeNull()
  })

  it('shows an unavailable run as unavailable, never as a zero', async () => {
    const user = userEvent.setup()
    // The report claims to belong to another run, so it is not this run's result.
    const { api } = scripted({
      report: async jobId => ({ ...REPORTS[jobId], run_id: 'another-run' } as unknown as BacktestReport),
    })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')

    await waitFor(() => expect(region.getByText(/运行 A：报告的身份与该运行的 engine run id 不一致/)).toBeTruthy())
    expect(region.getByText('两次运行的正式报告没有同时可读，因此这里不显示任何 A/B 差值，也不会把缺失的值当作零。'))
      .toBeTruthy()
    expect(region.queryByText('+7.75 USD')).toBeNull()
    expect(region.queryByText('+7.75 pp')).toBeNull()
    // The frozen parameters are record facts, so they stay visible either way.
    expect(region.getByText('parameters.target_quantity：3 → 2')).toBeTruthy()
  })

  it('shows a run that has no report yet as not yet readable', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      backtest: async jobId => jobId === LATEST_JOB
        ? job(jobId, { status: 'running', report_ready: false, engine_run_id: null })
        : job(jobId),
    })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')

    await waitFor(() => expect(region.getByText(/运行 A：该运行当前的状态是 running/)).toBeTruthy())
    expect(region.queryByText('+7.75 USD')).toBeNull()
  })

  it('retires an approval when the selection changes and asks nothing before it', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ ask: async () => strategy() })
    detail(api)
    const region = await openResearch(user)

    const explain = region.getByRole('button', { name: '解释所选证据' })
    const next = region.getByRole('button', { name: '提出下一实验' })
    // Without an approval of its own, neither action does anything at all.
    expect(disabled(explain)).toBe(true)
    expect(disabled(next)).toBe(true)
    expect(calls.ask).toBe(0)

    await user.click(region.getByLabelText('批准一次模型调用'))
    expect(disabled(explain)).toBe(false)
    // A different selection is a different request: the approval goes with it.
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(true)
    expect(disabled(region.getByRole('button', { name: '提出下一实验' }))).toBe(true)
    expect(calls.ask).toBe(0)
  })

  it('sends the selected runs with one fresh approval and starts nothing', async () => {
    const user = userEvent.setup()
    const { api, calls, sent } = scripted({ ask: async () => strategy({ calls: [call(SELECTED)] }) })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await user.click(region.getByLabelText('批准一次模型调用'))

    await user.click(region.getByRole('button', { name: '解释所选证据' }))
    await waitFor(() => expect(sent.length).toBe(1))
    // A first, then B, the exact approval for one model call, and no backtest.
    expect(sent[0].evidenceJobIds).toEqual([LATEST_JOB, EARLIER_JOB])
    expect(sent[0].approvedBudget).toEqual({ max_model_calls: 1, max_backtests: 0 })
    expect(sent[0].baseRevision).toBe(2)
    expect(sent[0].text).toContain('解释所选证据')
    expect(sent[0].text).toContain('不要运行任何东西')
    // The approval was spent on that one click.
    expect(disabled(within(await screen.findByRole('region', { name: '证据对比' }))
      .getByRole('button', { name: '解释所选证据' }))).toBe(true)

    // The next experiment is the second action, and it says what it is asking for.
    const reopened = within(await screen.findByRole('region', { name: '证据对比' }))
    await user.click(reopened.getByLabelText('批准一次模型调用'))
    await user.click(reopened.getByRole('button', { name: '提出下一实验' }))
    await waitFor(() => expect(sent.length).toBe(2))
    expect(sent[1].text).toContain('提出下一实验')
    expect(sent[1].text).toContain('针对当前草稿修订 2')
    expect(sent[1].text).toContain('不要声称任何结果')
    // The preset the host measured its own refusal against: the reading is asked
    // for in the reply text, in four separated parts, and the tool's own fields are
    // told the byte limits this workspace really enforces instead of being asked
    // for the whole explanation a 400-byte reason cannot hold. It no longer asks
    // for exactly one change: an honest "nothing to change" is an answer too.
    expect(sent[1].text).toContain('用 propose_change 提出一个有类型的字段修改')
    expect(sent[1].text).toContain('不要写进 reason')
    expect(sent[1].text).toContain('（1）事实：')
    expect(sent[1].text).toContain('（2）推断：')
    expect(sent[1].text).toContain('（3）限制：')
    expect(sent[1].text).toContain('（4）下一个假设：')
    expect(sent[1].text).toMatch(/reason[^。\n]*400 字节/)
    expect(sent[1].text).toMatch(/evidence[^。\n]*400 字节/)
    expect(sent[1].text).toContain('60 个汉字')
    expect(sent[1].evidenceJobIds).toEqual([LATEST_JOB, EARLIER_JOB])
    expect(sent[1].callId).not.toBe(sent[0].callId)
    // Two clicks, two calls: nothing was asked again, and nothing ran.
    expect(calls.ask).toBe(2)
    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('retires an approval when the draft revision moves on', async () => {
    const user = userEvent.setup()
    const moved = strategy({
      draft: {
        revision: 3, strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL',
        scenario_id: 'bounded-long.yaml', dataset_id: null, source_sha256: null,
        initial_cash: '10000', parameters: { target_quantity: '3', entry_delay_bars: 0 },
        criterion: CRITERION,
      },
    })
    const { api } = scripted({ ask: async () => moved })
    detail(api)
    const region = await openResearch(user)
    await user.click(region.getByLabelText('批准一次模型调用'))
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(false)

    // The AI column asks on the revision on screen, and the host answers with a new
    // one: the approval given for the old revision is not an approval for this one.
    const chat = within(await screen.findByRole('region', { name: 'AI 协作' }))
    await user.type(chat.getByLabelText('消息'), '随便问一句')
    await user.click(chat.getByLabelText('批准一次模型调用'))
    await user.click(chat.getByRole('button', { name: '询问模型' }))

    await waitFor(() => expect(disabled(within(screen.getByRole('region', { name: '证据对比' }))
      .getByRole('button', { name: '解释所选证据' }))).toBe(true))
  })

  it('drops an answer the page has moved past, and asks once per click', async () => {
    const user = userEvent.setup()
    // The response is handed over by the test itself, so it can arrive after the page
    // has moved on. It is a document of this same strategy, so applying it would put
    // the revision the page just left back on screen and empty the runs it is showing.
    // Answering before anything asked is a mistake in the test, not a quiet pass.
    const handover: { respond: (value: UserStrategy) => void } = {
      respond: value => { throw new Error(`nothing was awaiting an answer about ${value.user_strategy_id}`) },
    }
    const late = new Promise<UserStrategy>(resolve => { handover.respond = resolve })
    const stale = strategy({ name: 'STALE ANSWER', experiments: [] })
    const moved = strategy({
      draft: {
        revision: 3, strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL',
        scenario_id: 'bounded-long.yaml', dataset_id: null, source_sha256: null,
        initial_cash: '10000', parameters: { target_quantity: '3', entry_delay_bars: 0 },
        criterion: CRITERION,
      },
      messages: [{
        message_id: 'm-2', role: 'model', kind: 'message', text: '草稿已推进到修订 3',
        base_revision: 3, at: '2026-10-06T10:06:00.000000Z', call_id: 'call-9', proposal_id: null,
      }],
    })
    const { api, calls } = scripted({
      ask: async () => calls.ask === 1 ? await late : moved,
    })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await user.click(region.getByLabelText('批准一次模型调用'))
    await user.click(region.getByRole('button', { name: '解释所选证据' }))
    await waitFor(() => expect(calls.ask).toBe(1))

    // One click is one call: the second click, while the first is in flight, is
    // inert — it asks nothing and spends nothing, and it writes no refusal either.
    await user.click(region.getByRole('button', { name: '解释所选证据' }))
    expect(calls.ask).toBe(1)
    expect(screen.queryByRole('alert')).toBeNull()

    // The draft moves on under the in-flight request, from the conversation beside
    // it: the approval given for the old revision is not an approval for this one.
    const chat = within(await screen.findByRole('region', { name: 'AI 协作' }))
    await user.type(chat.getByLabelText('消息'), '把草稿推进一个修订')
    await user.click(chat.getByLabelText('批准一次模型调用'))
    await user.click(chat.getByRole('button', { name: '询问模型' }))
    await screen.findByText('草稿已推进到修订 3')
    expect((region.getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)

    // The answer to the request this panel has moved past is dropped whole: it does
    // not replace the strategy on the page, and it does not report a refusal for a
    // request the page is no longer looking at. The panel it belonged to is usable
    // again, and what it was showing is still there.
    handover.respond(stale)
    await waitFor(() => expect(disabled(region.getByLabelText('运行 A'))).toBe(false))
    expect(screen.queryByText('STALE ANSWER')).toBeNull()
    expect(screen.queryByText(/这条策略还没有冻结运行/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(within(screen.getByRole('region', { name: '证据对比' }))
      .getByText('parameters.target_quantity：3 → 2')).toBeTruthy()
  })

  it('writes nothing at all when it is unmounted before the answer arrives', async () => {
    const user = userEvent.setup()
    // The response is handed over by the test itself, so it can arrive after the panel
    // it belonged to is gone. Answering before anything asked is a mistake, not a pass.
    const handover: { respond: (value: UserStrategy) => void } = {
      respond: value => { throw new Error(`nothing was awaiting an answer about ${value.user_strategy_id}`) },
    }
    const late = new Promise<UserStrategy>(resolve => { handover.respond = resolve })
    const stale = strategy({ name: 'STALE ANSWER', experiments: [] })
    const { api, calls } = scripted({ ask: async () => await late })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await user.click(region.getByLabelText('批准一次模型调用'))
    await user.click(region.getByRole('button', { name: '解释所选证据' }))
    await waitFor(() => expect(calls.ask).toBe(1))

    // The panel is left while its request is still in flight, so the answer belongs
    // to a panel that no longer exists.
    await user.click(screen.getByRole('tab', { name: '实验' }))
    expect(screen.queryByRole('region', { name: '证据对比' })).toBeNull()

    // Nothing of it reaches the page: not the document it carries, not a refusal.
    // The strategy that is open is still the one that was open.
    handover.respond(stale)
    await user.click(screen.getByRole('tab', { name: '比较' }))
    const reopened = within(await screen.findByRole('region', { name: '证据对比' }))
    expect(reopened.getByLabelText('运行 A')).toBeTruthy()
    expect(screen.queryByText('STALE ANSWER')).toBeNull()
    expect(screen.queryByText(/这条策略还没有冻结运行/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('turns the next proposal into the existing apply flow, and applies nothing by itself', async () => {
    const user = userEvent.setup()
    const answered = strategy({ calls: [call(SELECTED, 'call-2')], proposals: [NEXT_PROPOSAL] })
    const { api, calls, applied } = scripted({ ask: async () => answered })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await user.click(region.getByLabelText('批准一次模型调用'))
    await user.click(region.getByRole('button', { name: '提出下一实验' }))

    // The typed proposal lands in the panel the user already applies changes in,
    // with the frozen evidence it was grounded in recorded on its own call.
    const panel = within(await screen.findByRole('region', { name: '建议修改' }))
    expect(panel.getByText('parameters.target_quantity')).toBeTruthy()
    expect(panel.getByText('这次调用所依据的冻结证据（2）')).toBeTruthy()
    expect(panel.getByText(/修订 2 · 确认 li-2 · 运行 job-2/)).toBeTruthy()
    expect(panel.getByText(/修订 1 · 确认 li-1 · 运行 job-1/)).toBeTruthy()
    // Nothing was applied, previewed or started on its own.
    expect(applied).toEqual([])
    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)

    await user.click(panel.getByRole('button', { name: '应用所选' }))
    await waitFor(() => expect(applied.length).toBe(1))
    expect(applied[0].paths).toEqual(['parameters.target_quantity'])
    // Applying is the user's own decision, and it starts no experiment.
    expect(calls.launch).toBe(0)
  })
})

/**
 * The address is the whole of the selection: a verified comparison carries its two
 * runs back to this strategy in the order the comparison showed them, and that
 * address — not a hidden copy of the last render — is what the panel reads.
 */
describe('evidence address continuity', () => {
  it('continuity: a comparison link opens the same two runs in the same order', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted()
    detailAt(api, '?evidence_a=job-1&evidence_b=job-2')
    const region = await openResearch(user)

    // A is the left run of that comparison and B is the right one: the order the
    // address states is the order shown here and the order a request would carry.
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-2')
    expect(region.getByText(/修订 1 · 确认 li-1 · 运行 job-1/)).toBeTruthy()
    expect(region.getByText(/修订 2 · 确认 li-2 · 运行 job-2/)).toBeTruthy()
    expect(region.getByRole('link', { name: '独立比较链接' }).getAttribute('href'))
      .toBe('/backtests/compare/job-1/job-2')
    // Opening a link is a read: nothing is asked and nothing runs.
    expect(calls.ask).toBe(0)
    expect(calls.preview).toBe(0)
    expect(calls.launch).toBe(0)
  })

  it('continuity: an explicit selection survives leaving the tab and a fresh load', async () => {
    const user = userEvent.setup()
    const { api } = scripted()
    detailAt(api, '?evidence_a=job-2&evidence_b=job-1')
    const region = await openResearch(user)
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-1')

    // Leaving the tab and coming back mounts the panel again from scratch: what it
    // shows is read from the address, not kept in a hidden copy of the last render.
    await user.click(screen.getByRole('tab', { name: '实验' }))
    await user.click(screen.getByRole('tab', { name: '比较' }))
    const reopened = within(await screen.findByRole('region', { name: '证据对比' }))
    expect((reopened.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
    expect((reopened.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-1')

    // A fresh load over the same address shows the same two runs.
    cleanup()
    render(<App api={api} />)
    const reloaded = await openResearch(user)
    expect((reloaded.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
    expect((reloaded.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('li-1')
  })

  it('continuity: a manual selection writes the address and the panel follows it both ways', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted()
    detail(api)
    const region = await openResearch(user)
    // A plain strategy address keeps its existing default: the latest run as A and
    // no B at all.
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
    expect(new URLSearchParams(window.location.search).has('evidence_a')).toBe(false)

    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await waitFor(() => expect(window.location.search).toBe('?tab=compare&evidence_a=job-2&evidence_b=job-1'))

    // The address changes under the panel — another link, or the browser's own back
    // and forward — and what is shown is what the address says, at once.
    window.history.pushState(
      { ...window.history.state, key: 'continuity-a' }, '', '/strategies/us-1?tab=compare&evidence_a=job-1')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1'))
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')

    window.history.pushState(
      { ...window.history.state, key: 'continuity-b' }, '', '/strategies/us-1?tab=compare')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2'))
    expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
    expect(calls.ask).toBe(0)
  })

  it('continuity: an approval does not survive a change of the evidence address', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ ask: async () => strategy() })
    detail(api)
    const region = await openResearch(user)
    await user.click(region.getByLabelText('批准一次模型调用'))
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(false)

    window.history.pushState(
      { ...window.history.state, key: 'continuity-approval' },
      '',
      '/strategies/us-1?tab=compare&evidence_a=job-1&evidence_b=job-2',
    )
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(true))
    expect(disabled(region.getByRole('button', { name: '提出下一实验' }))).toBe(true)
    expect(calls.ask).toBe(0)
  })

  // Each of these names a context this strategy cannot verify. None of them falls
  // back to the latest run, keeps a B it could not check, or asks anything.
  it('continuity: an unverifiable evidence address is refused and can be cleared', async () => {
    const user = userEvent.setup()
    const cases = [
      '?evidence_a=',
      '?evidence_a=job-9',
      '?evidence_a=job-1&evidence_a=job-2',
      '?evidence_b=job-2',
      '?evidence_a=job-1&evidence_b=',
      '?evidence_a=job-1&evidence_b=job-1&evidence_b=job-2',
      '?evidence_a=job-1&evidence_b=job-1',
    ]
    for (const search of cases) {
      const { api, calls, sent } = scripted({ ask: async () => { throw new Error('nothing may be asked') } })
      detailAt(api, search)
      const region = await openResearch(user)

      const alert = region.getByRole('alert')
      expect([search, alert.textContent].join(' ')).toContain('无效的证据选择')
      // Nothing is filled in from the latest run, so nothing here looks valid.
      expect(region.queryByLabelText('运行 A')).toBeNull()
      expect(region.queryByLabelText('批准一次模型调用')).toBeNull()
      expect(sent).toEqual([])
      expect(calls.ask).toBe(0)

      // The explicit recovery clears the address and returns the panel to its plain
      // default — the latest run as A, no B — where the user selects again.
      await user.click(region.getByRole('button', { name: '清除无效的证据选择' }))
      await waitFor(() => expect(new URLSearchParams(window.location.search).has('evidence_a')).toBe(false))
      expect(region.queryByRole('alert')).toBeNull()
      expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2')
      expect((region.getByLabelText('运行 B') as HTMLSelectElement).value).toBe('')
      expect(sent).toEqual([])

      cleanup()
    }
  })

  it('continuity: an evidence address that names two frozen records of one run is refused', async () => {
    const user = userEvent.setup()
    // One job id recorded twice on this strategy: exactly the ambiguity the host
    // refuses, and exactly what this panel must not resolve on its own.
    const doubled = strategy({ experiments: [EARLIER, { ...EARLIER, launch_intent_id: 'li-3' }] })
    const { api, calls, sent } = scripted({ ask: async () => { throw new Error('nothing may be asked') } })
    api.listUserStrategies = async () => [doubled]
    api.getUserStrategy = async () => doubled
    detailAt(api, '?evidence_a=job-1')
    const region = await openResearch(user)

    expect(region.getByRole('alert').textContent).toContain('无效的证据选择')
    expect(region.queryByLabelText('运行 A')).toBeNull()
    expect(sent).toEqual([])
    expect(calls.ask).toBe(0)
  })

  it('continuity: a run that is still being read, or whose report does not match, stops both actions', async () => {
    const user = userEvent.setup()
    // While either selected run is still being read, neither action is available:
    // a value that has not arrived never looks like a verified one.
    const pending = new Promise<BacktestReport>(() => {})
    const loading = scripted({
      report: async jobId => jobId === 'job-1' ? await pending : REPORTS[jobId],
      ask: async () => { throw new Error('nothing may be asked') },
    })
    detailAt(loading.api, '?evidence_a=job-1&evidence_b=job-2')
    let region = await openResearch(user)
    await waitFor(() => expect(region.getByText('正在读取运行 A 的状态与正式报告……')).toBeTruthy())
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(true)
    expect(disabled(region.getByRole('button', { name: '提出下一实验' }))).toBe(true)

    // A report that belongs to another run is not a report for the selected one.
    cleanup()
    const mismatched = scripted({
      report: async jobId => ({ ...REPORTS[jobId], run_id: 'another-run' } as unknown as BacktestReport),
      ask: async () => { throw new Error('nothing may be asked') },
    })
    detailAt(mismatched.api, '?evidence_a=job-1&evidence_b=job-2')
    region = await openResearch(user)
    await waitFor(() => expect(region.getByText(/运行 A：报告的身份与该运行的 engine run id 不一致/)).toBeTruthy())
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(true)
    expect(disabled(region.getByRole('button', { name: '提出下一实验' }))).toBe(true)
    expect(loading.sent).toEqual([])
    expect(mismatched.sent).toEqual([])
    expect(loading.calls.ask).toBe(0)
    expect(mismatched.calls.ask).toBe(0)
  })

  it('continuity: the next-experiment prompt allows an honest no-change and refuses repetition', async () => {
    const user = userEvent.setup()
    const { api, sent } = scripted({ ask: async () => strategy({ calls: [call(SELECTED)] }) })
    detail(api)
    const region = await openResearch(user)
    await user.selectOptions(region.getByLabelText('运行 B'), 'li-1')
    await user.click(region.getByLabelText('批准一次模型调用'))
    await user.click(region.getByRole('button', { name: '提出下一实验' }))
    await waitFor(() => expect(sent.length).toBe(1))

    const prompt = sent[0].text
    // No change at all is an honest answer when the evidence does not support one.
    expect(prompt).toContain('如果没有值得检验的、有类型的修改，就明确说明这次没有修改')
    expect(prompt).toContain('不要为了提出修改而修改')
    expect(prompt).not.toContain('恰好一个')
    // A repeated frozen input is not new evidence, and no parameter change is
    // useful research by itself.
    expect(prompt).toContain('不要把已经冻结过的输入当作新的证据')
    expect(prompt).toContain('也不要把任何参数改动都当作有用的研究')
    // What a proposed change has to name, against the selected runs.
    expect(prompt).toContain('它相对所选 A/B 的差异')
    expect(prompt).toContain('为什么这个差异有用')
    // The request still carries the evidence it is grounded in, A then B.
    expect(sent[0].evidenceJobIds).toEqual([LATEST_JOB, EARLIER_JOB])
  })

  it('continuity: a visit the page has left cannot hold the next one busy or revive its request', async () => {
    const user = userEvent.setup()
    // The answer is handed over by the test, so it can arrive after the page has left
    // the visit that sent it — and after it has come back to the very address, and the
    // very history key, that sent it. Answering before anything asked is a mistake in
    // the test, not a quiet pass.
    const handover: { respond: (value: UserStrategy) => void } = {
      respond: value => { throw new Error(`nothing was awaiting an answer about ${value.user_strategy_id}`) },
    }
    const late = new Promise<UserStrategy>(resolve => { handover.respond = resolve })
    const stale = strategy({ name: 'STALE ANSWER', experiments: [] })
    const { api, calls, sent } = scripted({ ask: async () => await late })
    detailAt(api, '?evidence_a=job-1')
    const visit = { ...window.history.state }
    const region = await openResearch(user)
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')

    // One approval, one click: the request is in flight, so this visit is waiting and
    // holds both selects while it does.
    await waitFor(() => expect(disabled(region.getByLabelText('批准一次模型调用'))).toBe(false))
    await user.click(region.getByLabelText('批准一次模型调用'))
    await user.click(region.getByRole('button', { name: '解释所选证据' }))
    await waitFor(() => expect(calls.ask).toBe(1))
    await waitFor(() => expect(disabled(region.getByLabelText('运行 A'))).toBe(true))

    // The page leaves for another evidence address while that answer is still owed.
    window.history.pushState(
      { ...window.history.state, key: 'continuity-departed' },
      '',
      '/strategies/us-1?tab=compare&evidence_a=job-2',
    )
    window.dispatchEvent(new PopStateEvent('popstate'))
    // The visit the user arrives at is usable at once: it is not held by a request it
    // never sent, and it has no approval of its own to be spent.
    await waitFor(() => expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-2'))
    await waitFor(() => expect(disabled(region.getByLabelText('批准一次模型调用'))).toBe(false))
    expect(disabled(region.getByLabelText('运行 A'))).toBe(false)
    expect((region.getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)

    // Back to the address — and the history key — the departed request was sent from.
    // Arriving there again is a new visit, not a return to that request's ownership.
    window.history.pushState(visit, '', '/strategies/us-1?tab=compare&evidence_a=job-1')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1'))
    expect(disabled(region.getByLabelText('运行 A'))).toBe(false)
    const approve = region.getByLabelText('批准一次模型调用') as HTMLInputElement
    await waitFor(() => expect(disabled(approve)).toBe(false))
    expect(approve.checked).toBe(false)
    expect(disabled(region.getByRole('button', { name: '解释所选证据' }))).toBe(true)

    // The answer finally arrives. It is dropped whole: it does not put its document on
    // the page and it does not report a refusal for a request this visit never made.
    // Nothing was asked again while it was owed, and nothing ran.
    await act(async () => { handover.respond(stale) })
    expect(screen.queryByText('STALE ANSWER')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(/这条策略还没有冻结运行/)).toBeNull()
    expect((region.getByLabelText('运行 A') as HTMLSelectElement).value).toBe('li-1')
    expect(disabled(region.getByLabelText('运行 A'))).toBe(false)
    expect(region.getByText(/修订 1 · 确认 li-1 · 运行 job-1/)).toBeTruthy()
    expect(sent.length).toBe(1)
    expect(calls.ask).toBe(1)
    expect(calls.launch).toBe(0)
  })
})


describe('research workspace batch 1', () => {
  it('opens the report beside AI, with three durable tabs and scoped navigation', async () => {
    const user = userEvent.setup()
    const { api } = scripted()
    detail(api)
    const experimentTab = await screen.findByRole('tab', { name: '实验' })
    expect(experimentTab.getAttribute('aria-selected')).toBe('true')
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['策略', '实验', '比较'])
    expect(await screen.findByRole('heading', { name: '运行 A · 正式报告' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    expect(within(screen.getByRole('navigation', { name: '主导航' })).getAllByRole('link').map(a => a.textContent)).toEqual(['研究', '运行', '数据'])
    await user.click(screen.getByRole('tab', { name: '比较' }))
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('compare')
    cleanup()
    render(<App api={api} />)
    expect((await screen.findByRole('tab', { name: '比较' })).getAttribute('aria-selected')).toBe('true')
  })

  it('sends the composer question with exactly the selected A/B and one approval', async () => {
    const user = userEvent.setup()
    const { api, sent, calls } = scripted({ ask: async () => strategy() })
    api.settingsStatus = async () => ({ configured: true } as Awaited<ReturnType<NonNullable<ApiAdapter['settingsStatus']>>>)
    detailAt(api, '?tab=compare&evidence_a=job-1&evidence_b=job-2')
    await screen.findByLabelText('消息')
    const chat = within(screen.getByRole('region', { name: 'AI 协作' }))
    await user.type(chat.getByLabelText('消息'), '比较这两次实验的成本')
    await user.click(chat.getByLabelText('批准一次模型调用'))
    await waitFor(() => expect(disabled(chat.getByRole('button', { name: '询问模型' }))).toBe(false))
    await user.dblClick(chat.getByRole('button', { name: '询问模型' }))
    expect(calls.ask).toBe(1)
    expect(sent[0].evidenceJobIds).toEqual(['job-1', 'job-2'])
    expect(sent[0].approvedBudget).toEqual({ max_model_calls: 1, max_backtests: 0 })
  })
})


describe('research composer visit ownership', () => {
  it('drops an old completion after leaving and returning to the same selected pair', async () => {
    const user = userEvent.setup()
    let complete: ((value: UserStrategy) => void) | undefined
    const response = new Promise<UserStrategy>(resolve => { complete = resolve })
    const { api, calls, sent } = scripted({ ask: async () => response })
    detailAt(api, '?tab=compare&evidence_a=job-1&evidence_b=job-2')
    await screen.findByLabelText('消息')
    const chat = within(screen.getByRole('region', { name: 'AI 协作' }))
    await user.type(chat.getByLabelText('消息'), 'Explain this pair')
    await user.click(chat.getByLabelText('批准一次模型调用'))
    await waitFor(() => expect(disabled(chat.getByRole('button', { name: '询问模型' }))).toBe(false))
    const originalState = window.history.state
    await user.click(chat.getByRole('button', { name: '询问模型' }))
    expect(calls.ask).toBe(1)
    const originalUrl = window.location.href
    act(() => {
      window.history.pushState({ ...originalState, key: 'different-pair' }, '', '/strategies/us-1?tab=compare&evidence_a=job-2&evidence_b=job-1')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect((chat.getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)
    expect(chat.getByLabelText('AI 当前上下文').textContent).toContain('A：修订 2 / B：修订 1')
    act(() => {
      window.history.pushState(originalState, '', originalUrl)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await act(async () => { complete!(strategy({ name: 'Obsolete completion' })); await response })
    expect(screen.queryByRole('heading', { name: 'Obsolete completion' })).toBeNull()
    expect(screen.getByRole('heading', { name: 'Bold entry' })).toBeTruthy()
    expect((chat.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('Explain this pair')
    expect((chat.getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)
    expect(disabled(chat.getByRole('button', { name: '询问模型' }))).toBe(true)
    expect(sent[0].evidenceJobIds).toEqual(['job-1', 'job-2'])
    expect(calls.ask).toBe(1)
  })

  it.each(['unreadable', 'wrong-identity'])('blocks the composer while selected evidence is %s', async mode => {
    const user = userEvent.setup()
    const { api, calls } = scripted({ backtest: async id => {
      if (mode === 'unreadable') throw new Error('Report unavailable')
      return job(id, { job_id: 'different-job' })
    } })
    detailAt(api, '?tab=experiments&evidence_a=job-1')
    await screen.findByLabelText('消息')
    const chat = within(screen.getByRole('region', { name: 'AI 协作' }))
    await user.type(chat.getByLabelText('消息'), 'Explain this report')
    await user.click(chat.getByLabelText('批准一次模型调用'))
    expect(disabled(chat.getByRole('button', { name: '询问模型' }))).toBe(true)
    expect(chat.getByText('所选证据尚未就绪，请检查实验或比较标签。')).toBeTruthy()
    expect(calls.ask).toBe(0)
  })

  it('prefills a question without sending or approving it', async () => {
    const user = userEvent.setup()
    const { api, calls } = scripted()
    detail(api)
    await screen.findByLabelText('消息')
    const chat = within(screen.getByRole('region', { name: 'AI 协作' }))
    await user.click(chat.getByRole('button', { name: '解读所选实验' }))
    expect((chat.getByLabelText('消息') as HTMLTextAreaElement).value).toContain('当前所选实验')
    expect((chat.getByLabelText('批准一次模型调用') as HTMLInputElement).checked).toBe(false)
    expect(disabled(chat.getByRole('button', { name: '询问模型' }))).toBe(true)
    expect(calls.ask).toBe(0)
  })
})


it('does not show trade analytics belonging to another report in the inline report', async () => {
  const user = userEvent.setup()
  const { api } = scripted()
  api.getTradeAnalytics = async () => ({
    schema: 'ea.trade-analytics.v1', run_id: 'other-run', report_sha256: 'wrong-report', currency: 'USD',
    closed_trades: 91, wins: 91, losses: 0, breakeven: 0, win_rate: '1', average_win: '999',
    average_loss: null, payoff_ratio: null, average_holding_seconds: '60', has_open_position: false, trades: [],
  })
  detail(api)
  await screen.findByRole('heading', { name: '运行 A · 正式报告' })
  await user.click(await screen.findByText('成交与完整报告'))
  const analytics = within(screen.getByRole('region', { name: 'Trade analytics' }))
  expect(analytics.getByText('Complete-trade analytics unavailable for this report.')).toBeTruthy()
  expect(analytics.queryByText('999 USD')).toBeNull()
})
