/**
 * The core-owned experiment surface: confirmed progress, stop, unknown and reports.
 *
 * The adapter below is a scripted double, so nothing here proves the engine ran: it
 * proves what the page does with the record an owner holds. A run the Rust core owns
 * is read from the owner's own projection — its status, its phase, its registered
 * report and facts — a stop is a request the owner answers, an unresolved
 * confirmation keeps its intent instead of generating a second one, and a report is
 * only ever linked for a success the owner registered and published.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter } from './app'
import {
  UserStrategyRequestError,
  type UserStrategy,
  type UserStrategyDraft,
  type UserStrategyExperiment,
} from './user-strategies-api'

const PREVIEW_IDENTITY = `sha256:${'a'.repeat(64)}`

/** The five readings an owner-registered report states, exactly as registered. */
const FACTS = [
  { path: 'economics.net_pnl.amount', value: '137.53' },
  { path: 'economics.fees.amount', value: '2.03' },
  { path: 'economics.total_return.value', value: '1.3753' },
  { path: 'economics.counts.orders', value: 2 },
  { path: 'economics.counts.fills', value: 2 },
]

function ownedRun(overrides: Partial<UserStrategyExperiment> = {}): UserStrategyExperiment {
  return {
    launch_intent_id: 'li-1',
    request_id: 'request-1',
    payload_sha256: 'f'.repeat(64),
    payload: {
      draft_revision: 1,
      scenario_id: 'bounded-long.yaml',
      parameters: { target_quantity: '2', entry_delay_bars: 0 },
      preview_identity: PREVIEW_IDENTITY,
      input_sha256: 'c'.repeat(64),
      input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
      approved_budget: { max_model_calls: 0, max_backtests: 1 },
      normalized: { funding: { currency: 'USD' } },
    },
    task_id: null,
    job_id: '9f0a1b2c-0000-4000-8000-000000000001',
    status: 'running',
    message: null,
    created_at: '2026-10-06T10:02:00.000000Z',
    owner: 'quine',
    phase: 'engine',
    report_ready: false,
    report_sha256: null,
    facts: null,
    attempt_id: '9f0a1b2c-0000-4000-8000-000000000002',
    frozen_sha256: 'd'.repeat(64),
    ...overrides,
  }
}

function strategy(experiments: UserStrategyExperiment[]): UserStrategy {
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
      parameters: { target_quantity: '2', entry_delay_bars: 0 },
      criterion: {
        schema: 'radian.report-criterion.v1',
        field: 'economics.net_pnl.amount',
        expectation: 'positive',
      },
    },
    history: [],
    messages: [],
    calls: [],
    proposals: [],
    changes: [],
    experiments,
  }
}

type Script = {
  initial?: UserStrategy
  read?: () => Promise<UserStrategy>
  cancel?: (id: string, intent: string) => Promise<UserStrategy>
}

const stopped: { id: string; intent: string }[] = []
const launched: { intent: string; preview: string }[] = []

function scripted(script: Script) {
  const counts = { get: 0 }
  let persisted = script.initial ?? strategy([])
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
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
    listUserStrategies: async () => [persisted],
    getUserStrategy: async () => {
      counts.get += 1
      return script.read ? script.read() : persisted
    },
    previewUserStrategyExperiment: async () => ({
      draft_revision: 1,
      strategy_id: 'bounded-long-v1',
      scenario_id: 'bounded-long.yaml',
      venue: 'XNAS',
      symbol: 'AAPL',
      initial_cash: '10000',
      parameters: { target_quantity: '2', entry_delay_bars: 0 },
      record_count: 4,
      data: { path: 'prices.csv', fingerprint: { sha256: 'a'.repeat(64), record_count: 4 } },
      input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
      input_sha256: 'c'.repeat(64),
      preview_identity: PREVIEW_IDENTITY,
      normalized: { funding: { currency: 'USD' } },
      commission: null,
      slippage: null,
      latency: null,
      criterion: null,
      budget: { max_model_calls: 10, max_backtests: 5 },
      owner: 'quine',
    }),
    launchUserStrategyExperiment: async (_id, input) => {
      launched.push({ intent: input.launch_intent_id, preview: input.preview_identity })
      persisted = strategy([
        ownedRun({ launch_intent_id: input.launch_intent_id, status: 'confirmed', phase: null }),
      ])
      return persisted
    },
    cancelUserStrategyExperiment: async (id, intent) => {
      stopped.push({ id, intent })
      if (!script.cancel) throw new Error('unused')
      persisted = await script.cancel(id, intent)
      return persisted
    },
  }
  return { api, counts }
}

function detail(api: ApiAdapter) {
  window.history.pushState({}, '', '/strategies/us-1')
  render(<App api={api} />)
}

async function openExperimentTab(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: '实验' }))
  await waitFor(() => expect(screen.getByRole('region', { name: '实验' })).toBeTruthy())
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const pause = (milliseconds: number) => new Promise(resolve => setTimeout(resolve, milliseconds))

afterEach(() => {
  cleanup()
  stopped.length = 0
  launched.length = 0
  clearPendingSelections()
  window.history.pushState({}, '', '/backtests')
})

describe('core-owned experiments', () => {
  it('keeps one progress read in flight and stops once the run is terminal', async () => {
    const user = userEvent.setup()
    let reads = 0
    const held = deferred<UserStrategy>()
    const initial = strategy([ownedRun({ status: 'running' })])
    const { api } = scripted({
      initial,
      // The page's own read answers; every later read is the progress poll, which
      // this test holds open so a second poll could only be an overlap.
      read: () => { reads += 1; return reads === 1 ? Promise.resolve(initial) : held.promise },
    })
    detail(api)
    await openExperimentTab(user)

    await waitFor(() => expect(reads).toBe(2), { timeout: 4000 })
    await pause(1400)
    expect(reads).toBe(2)

    held.resolve(strategy([
      ownedRun({ status: 'created', phase: null, report_ready: true, facts: FACTS, report_sha256: 'e'.repeat(64) }),
    ]))
    await waitFor(
      () => expect(screen.getByText(/已从修订 1 启动实验 li-1/)).toBeTruthy(),
      { timeout: 5000 },
    )
    await pause(1400)
    // The owner has nothing left to move, so no further read is scheduled.
    expect(reads).toBe(2)
  }, 20_000)

  it('asks the owner to stop a live run and shows the owner own answer', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([ownedRun({ status: 'running', phase: 'engine' })]),
      cancel: async () => strategy([ownedRun({ status: 'stopping', phase: null })]),
    })
    detail(api)
    await openExperimentTab(user)

    await user.click(await screen.findByRole('button', { name: '停止这次运行' }))
    await waitFor(() => expect(stopped).toEqual([{ id: 'us-1', intent: 'li-1' }]))
    // What the page shows is the record the owner answered with: an accepted stop,
    // not a local claim that the run has stopped.
    await screen.findByText(/所有者状态：正在停止/, {}, { timeout: 5000 })
    const panel = screen.getByRole('region', { name: '实验' })
    const stop = within(panel).getByRole('button', { name: '停止这次运行' }) as HTMLButtonElement
    expect(stop.disabled).toBe(true)
    expect(within(panel).getByText(/停止请求只是请求/)).toBeTruthy()
  }, 20_000)

  it('shows the owner own refusal instead of claiming a stop', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([ownedRun({ status: 'running' })]),
      cancel: async () => {
        throw new UserStrategyRequestError(
          'late_cancel_refused', 'the run already committed a result',
        )
      },
    })
    detail(api)
    await openExperimentTab(user)

    await user.click(await screen.findByRole('button', { name: '停止这次运行' }))
    // The host's own message is what the page repeats; nothing here decides that a
    // stop happened, so the run is still shown as the owner's running record.
    const refusal = await screen.findByText(/the run already committed a result/, {}, { timeout: 5000 })
    expect(refusal.textContent).toBeTruthy()
    const panel = screen.getByRole('region', { name: '实验' })
    expect(within(panel).getByText(/所有者状态：运行中/)).toBeTruthy()
  }, 20_000)

  it('reuses the unresolved confirmation instead of generating a second intent', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([ownedRun({ launch_intent_id: 'li-1', status: 'unconfirmed', phase: null })]),
    })
    detail(api)
    await openExperimentTab(user)

    await user.click(await screen.findByRole('button', { name: '预览实验' }))
    await screen.findByText('输入身份')
    await user.click(screen.getByRole('button', { name: '确认并运行一次' }))
    await waitFor(() => expect(launched.length).toBe(1))
    // The receipt of li-1 was never observed, so that same confirmation is the one
    // asked about again: no second intent is minted while it is unresolved.
    expect(launched[0]).toEqual({ intent: 'li-1', preview: PREVIEW_IDENTITY })
  }, 20_000)

  it('reads a core-owned success through the existing run page', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([
        ownedRun({
          status: 'created', phase: null, report_ready: true,
          report_sha256: 'e'.repeat(64), facts: FACTS,
        }),
      ]),
    })
    detail(api)
    await openExperimentTab(user)

    // The run keeps the mature view: the experiment panel links to the run's own
    // page, which reads the owner's registered report, path and analytics through
    // the same APIs every other run uses — never a second, thinner reading here.
    const link = await screen.findByRole('link', { name: '查看这次实验报告' })
    expect(link.getAttribute('href')).toBe(`/strategies/us-1?tab=experiments&evidence_a=${ownedRun().job_id}`)
    expect(screen.queryByRole('region', { name: '两次运行的事实对比' })).toBeNull()
  }, 20_000)

  it('links a run whose owner registered a success it cannot publish', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([ownedRun({ status: 'created', phase: null, report_ready: false })]),
    })
    detail(api)
    await openExperimentTab(user)

    // The run page is still the place that says what is readable: the panel does
    // not fabricate a result, and it does not hide the run either.
    const link = await screen.findByRole('link', { name: '查看这次实验报告' })
    expect(link.getAttribute('href')).toBe(`/strategies/us-1?tab=experiments&evidence_a=${ownedRun().job_id}`)
  }, 20_000)

  it('says an unresolved run is unresolved rather than showing a result', async () => {
    const user = userEvent.setup()
    const { api } = scripted({
      initial: strategy([
        ownedRun({
          status: 'unknown',
          phase: null,
          job_id: null,
          message: 'the core could not prove how this run ended',
        }),
      ]),
    })
    detail(api)
    await openExperimentTab(user)

    const panel = await screen.findByRole('region', { name: '实验' })
    expect(within(panel).getByText(/结果未知/)).toBeTruthy()
    expect(within(panel).getByText(/the core could not prove how this run ended/)).toBeTruthy()
    expect(within(panel).queryByRole('link', { name: '查看这次实验报告' })).toBeNull()
  }, 20_000)
})

/**
 * The operation id one dataset selection makes: this click's own lower-case
 * UUIDv4. It identifies the operation, so no strategy identity is inside it.
 */
const DATASET_OPERATION_ID = /^dataset-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

/** The one kind of catalog entry this panel may offer. */
const USABLE_DATASET = { dataset_id: 'registered.csv', valid: true, origin: 'registered' as const }

/** The strategy document a host returns once it has bound one catalog identity. */
function boundToDataset(datasetId: string): UserStrategy {
  const head = strategy([]).draft as UserStrategyDraft
  return { ...strategy([]), draft: { ...head, revision: 2, dataset_id: datasetId, source_sha256: 'e'.repeat(64) } }
}

/** The panel's own pending-selection keys, cleared so no test answers another's. */
function clearPendingSelections() {
  const owned = Array.from(
    { length: sessionStorage.length },
    (_, index) => sessionStorage.key(index) ?? '',
  ).filter(key => key.startsWith('quine.dataset-selection.'))
  for (const key of owned) sessionStorage.removeItem(key)
}

describe('one explicit dataset selection', () => {
  it('offers only usable catalog identities and applies the revision the host returns', async () => {
    const user = userEvent.setup()
    const chosen: { id: string; operation_id: string; base_revision: number; dataset_id: string }[] = []
    const { api } = scripted({})
    const revised = strategy([])
    const selecting: ApiAdapter = {
      ...api,
      listDatasets: async () => [
        {
          dataset_id: 'registered.csv',
          valid: true,
          origin: 'registered',
          venue: 'XNAS',
          symbol: 'AAPL',
          record_count: 4,
        },
        // An entry the host lists as unusable is not offered as a choice: this
        // panel never turns a broken dataset into a selectable one.
        { dataset_id: 'broken.csv', valid: false },
      ],
      selectUserStrategyDataset: async (id, input) => {
        chosen.push({ id, ...input })
        const head = revised.draft as UserStrategyDraft
        return {
          ...revised,
          draft: {
            ...head,
            revision: 2,
            dataset_id: 'registered.csv',
            source_sha256: 'e'.repeat(64),
          },
          dataset: {
            operation_id: input.operation_id,
            request: { action: 'select-dataset', base_revision: input.base_revision, dataset_id: input.dataset_id },
            request_sha256: 'f'.repeat(64),
            dataset_id: input.dataset_id,
            from_revision: 1,
            to_revision: 2,
            selected_at: '2026-10-06T10:05:00.000000Z',
          },
        }
      },
    }
    detail(selecting)

    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const panel = await screen.findByRole('region', { name: '选择数据集' })
    const picker = await within(panel).findByLabelText('数据集')
    const options = within(picker).getAllByRole('option').map(option => option.textContent ?? '')
    expect(options.some(text => text.includes('registered.csv'))).toBe(true)
    expect(options.some(text => text.includes('broken.csv'))).toBe(false)

    await user.selectOptions(picker, 'registered.csv')
    await user.click(within(panel).getByRole('button', { name: '选择这个数据集' }))

    await waitFor(() => expect(chosen.length).toBe(1))
    // One explicit choice, one call, addressed to the open strategy: the strategy
    // identity is that argument and never part of the operation id.
    expect(chosen).toHaveLength(1)
    expect(chosen[0].id).toBe('us-1')
    // The client sends the catalog identity it was shown, against the revision it
    // was reading: not a path and not a digest of its own.
    expect(chosen[0].dataset_id).toBe('registered.csv')
    expect(chosen[0].base_revision).toBe(1)
    expect(chosen[0].operation_id).toMatch(DATASET_OPERATION_ID)
    // The revision the host returned is the one on screen afterwards, standing on
    // the dataset the host bound at the digest the host wrote.
    expect(await screen.findByText('v2 草稿')).toBeTruthy()
    const returned = screen.getByRole('region', { name: '策略详情' })
    expect(within(returned).getByText(/registered\.csv · e{12}/)).toBeTruthy()
  }, 20_000)

  it('keeps the host refusal visible and writes no selection', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const { api } = scripted({})
    const failing: ApiAdapter = {
      ...api,
      listDatasets: async () => [{ dataset_id: 'registered.csv', valid: true, origin: 'registered' }],
      selectUserStrategyDataset: async () => {
        attempts += 1
        throw new UserStrategyRequestError('stale_base_revision', '草稿修订已前进')
      },
    }
    detail(failing)

    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const panel = await screen.findByRole('region', { name: '选择数据集' })
    await user.selectOptions(await within(panel).findByLabelText('数据集'), 'registered.csv')
    await user.click(within(panel).getByRole('button', { name: '选择这个数据集' }))

    await waitFor(() => expect(attempts).toBe(1))
    await waitFor(() => expect(screen.getAllByText(/草稿修订已前进/).length).toBeGreaterThan(0))
  }, 20_000)

  it('keeps one unresolved selection to one click and one stored operation id', async () => {
    const user = userEvent.setup()
    const pending = deferred<UserStrategy>()
    const attempts: { id: string; operation_id: string; base_revision: number; dataset_id: string }[] = []
    const { api } = scripted({})
    detail({
      ...api,
      listDatasets: async () => [USABLE_DATASET],
      selectUserStrategyDataset: async (id, input) => { attempts.push({ id, ...input }); return pending.promise },
    })

    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const panel = await screen.findByRole('region', { name: '选择数据集' })
    await user.selectOptions(await within(panel).findByLabelText('数据集'), 'registered.csv')
    const choose = () => within(panel).getByRole('button', { name: '选择这个数据集' }) as HTMLButtonElement
    await user.click(choose())

    // The first answer has not arrived, so the control is inert: a second click is
    // not a second selection.
    await waitFor(() => expect(attempts.length).toBe(1))
    expect(choose().disabled).toBe(true)
    await user.click(choose())
    expect(attempts.length).toBe(1)

    // The intent this click stored is the one in flight, under this strategy and
    // this reading of the draft, and it is this click's own operation id.
    expect(sessionStorage.getItem('quine.dataset-selection.us-1.1')).toBe(attempts[0].operation_id)
    expect(attempts[0].operation_id).toMatch(DATASET_OPERATION_ID)
    expect(attempts[0].id).toBe('us-1')
    expect(attempts[0].dataset_id).toBe('registered.csv')
    expect(attempts[0].base_revision).toBe(1)

    pending.resolve(strategy([]))
    // The answer settles the one intent that was in flight, and sends nothing more.
    await waitFor(() => expect(sessionStorage.getItem('quine.dataset-selection.us-1.1')).toBeNull())
    expect(attempts.length).toBe(1)
  })

  it('never resends an unresolved selection, on another click or after a remount', async () => {
    const user = userEvent.setup()
    const attempts: { id: string; operation_id: string; base_revision: number; dataset_id: string }[] = []
    const { api } = scripted({})
    const unreachable: ApiAdapter = {
      ...api,
      listDatasets: async () => [USABLE_DATASET],
      // The request left and nothing came back. That is not a refusal, so the
      // outcome stays unknown and the client may not decide the host wrote nothing.
      selectUserStrategyDataset: async (id, input) => {
        attempts.push({ id, ...input })
        throw new Error('the connection dropped before the host answered')
      },
    }
    detail(unreachable)

    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const panel = await screen.findByRole('region', { name: '选择数据集' })
    await user.selectOptions(await within(panel).findByLabelText('数据集'), 'registered.csv')
    await user.click(within(panel).getByRole('button', { name: '选择这个数据集' }))

    await waitFor(() => expect(attempts.length).toBe(1))
    expect(await within(panel).findByText(/选择结果未知/)).toBeTruthy()
    expect(sessionStorage.getItem('quine.dataset-selection.us-1.1')).toBe(attempts[0].operation_id)
    // The unresolved intent is still pending, so another click sends nothing.
    await user.click(within(panel).getByRole('button', { name: '选择这个数据集' }))
    expect(attempts.length).toBe(1)

    // Reloading the strategy brings that same intent back unresolved, and the panel
    // still sends nothing by itself.
    cleanup()
    detail(unreachable)
    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const reloaded = await screen.findByRole('region', { name: '选择数据集' })
    expect(await within(reloaded).findByText(/不会自动重发/)).toBeTruthy()
    expect((within(reloaded).getByRole('button', { name: '选择这个数据集' }) as HTMLButtonElement).disabled).toBe(true)
    expect(attempts.length).toBe(1)
  })

  it('permits one new explicit attempt after a recorded refusal, on a fresh operation id', async () => {
    const user = userEvent.setup()
    const attempts: { id: string; operation_id: string; base_revision: number; dataset_id: string }[] = []
    const { api } = scripted({})
    const refusing: ApiAdapter = {
      ...api,
      listDatasets: async () => [USABLE_DATASET],
      selectUserStrategyDataset: async (id, input) => {
        attempts.push({ id, ...input })
        // A refusal the client can classify: the host wrote nothing, so this same
        // choice may be made again — explicitly, with an operation id of its own.
        if (attempts.length === 1) throw new UserStrategyRequestError('stale_base_revision', '草稿修订已前进')
        return boundToDataset(input.dataset_id)
      },
    }
    detail(refusing)

    await user.click(await screen.findByRole('button', { name: '编辑草稿' }))
    const panel = await screen.findByRole('region', { name: '选择数据集' })
    await user.selectOptions(await within(panel).findByLabelText('数据集'), 'registered.csv')
    const choose = () => within(panel).getByRole('button', { name: '选择这个数据集' }) as HTMLButtonElement
    await user.click(choose())

    await waitFor(() => expect(attempts.length).toBe(1))
    expect((await screen.findAllByText(/草稿修订已前进/)).length).toBeGreaterThan(0)
    // A classified refusal leaves nothing pending, and the choice is still the
    // user's: the way to try again is another click.
    expect(sessionStorage.getItem('quine.dataset-selection.us-1.1')).toBeNull()
    expect(choose().disabled).toBe(false)
    expect(attempts.length).toBe(1)

    await user.click(choose())
    await waitFor(() => expect(attempts.length).toBe(2))
    // A fresh operation id for the new attempt, and the same binding: this
    // strategy, this dataset, this reading of the draft.
    expect(attempts[1].operation_id).not.toBe(attempts[0].operation_id)
    expect(attempts[1].operation_id).toMatch(DATASET_OPERATION_ID)
    expect(attempts.map(item => [item.id, item.dataset_id, item.base_revision]))
      .toEqual([['us-1', 'registered.csv', 1], ['us-1', 'registered.csv', 1]])
    // The revision the host returned is the one on screen afterwards.
    expect(await screen.findByText('v2 草稿')).toBeTruthy()
    const returned = screen.getByRole('region', { name: '策略详情' })
    expect(within(returned).getByText(/registered\.csv · e{12}/)).toBeTruthy()
  })
})
