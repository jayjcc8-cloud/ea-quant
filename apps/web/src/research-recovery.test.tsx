/**
 * Research recovery, scenario 3: what a terminal task keeps, what it recorded,
 * and the one explicit action that starts a *separate* experiment from it — plus
 * everything this client must never do on its own.
 *
 * Every document below is a hand-written fixture and every adapter member is a
 * scripted double, so what is asserted is what the panel renders, what it sends,
 * and what it never sends: no retry, no interpret, no create and no confirmation
 * without the click that asked for it.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob } from './app'
import type { PluginCatalog, PluginSelection, ResearchTask } from './research-api'

const IDEA = 'Test a two-unit entry on AAPL'

const SPEC = {
  strategy_id: 'bounded-long-v1',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'One registered entry with a two-unit target.',
  scenario_id: null,
  dataset_id: null,
  source_sha256: null,
  parameters: { target_quantity: '2' },
  assumptions: ['The registered scenario window is representative.'],
}

const CONFIRMED = [{ version: 1, confirmed_at: '2026-10-03T10:01:00.000000Z', spec: SPEC }]

// The persisted task shape, mirrored from the research workspace contract's own
// fixture so both files describe the same document.
function task(overrides: Partial<ResearchTask> = {}): ResearchTask {
  return {
    schema: 'radian.research-task.v1',
    task_id: 'task-1',
    created_at: '2026-10-03T10:00:00.000000Z',
    updated_at: '2026-10-03T10:05:00.000000Z',
    status: 'awaiting_confirmation',
    raw_idea: IDEA,
    gap: null,
    budget: { max_model_calls: 10, max_backtests: 5 },
    usage: { model_calls: 3, input_tokens: 812, output_tokens: 143, backtests: 0 },
    spec_draft: SPEC,
    spec_confirmed: [],
    jobs: [],
    request_id: null,
    error: null,
    cancel_note: null,
    last_model: { model: 'scripted-proposal-fixture', at: '2026-10-03T10:00:00.000000Z', outcome: 'tool_call' },
    reservation: null,
    backtest_reservation: null,
    spent_calls: [],
    operation_generation: 2,
    operation_completion_generation: 2,
    operation_kind: null,
    runtime_diagnostic: null,
    ...overrides,
  }
}

const SUCCEEDED_JOB: BacktestJob = {
  schema: 'ea.local-web-job.v2',
  job_id: 'job-1',
  request_id: 'request-1',
  scenario_id: 'bounded-long.yaml',
  input_identity: { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 4 },
  status: 'succeeded',
  engine_run_id: 'run-1',
  report_sha256: 'c'.repeat(64),
  summary_sha256: 'd'.repeat(64),
  error_code: null,
  message: null,
  report_ready: true,
  strategy_descriptor: { strategy_id: 'bounded-long-v1', strategy_version: 1, research_visible: true, parameters: [] },
}

const CATALOG: PluginCatalog = { schema: 'ea.plugin-catalog.v1', supported_kinds: [], plugins: [] }

type Calls = {
  create: number
  confirm: number
  retry: number
  cancel: number
  interpret: number
  backtests: number
  reads: number
  created: Array<[string, PluginSelection | undefined]>
  writes: string[]
}

/** The scripted host: it writes only for the endpoint the click asked for. */
function host(initial: ResearchTask) {
  const calls: Calls = { create: 0, confirm: 0, retry: 0, cancel: 0, interpret: 0, backtests: 0, reads: 0, created: [], writes: [] }
  const tasks = new Map<string, ResearchTask>([[initial.task_id, initial]])
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    listScenarios: async () => [],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { calls.backtests += 1; return SUCCEEDED_JOB },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => SUCCEEDED_JOB,
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    settingsStatus: async () => { throw new Error('the scripted host reports no settings') },
    pluginCatalog: async () => CATALOG,
    listResearchTasks: async () => [...tasks.values()],
    getResearchTask: async (taskId: string) => {
      calls.reads += 1
      const found = tasks.get(taskId)
      if (!found) throw new Error(`the scripted host has no task ${taskId}`)
      return found
    },
    createResearchTask: async (idea: string, selection?: PluginSelection) => {
      calls.create += 1
      calls.created.push([idea, selection])
      calls.writes.push('create:task-2')
      const created = task({ task_id: 'task-2', raw_idea: idea, created_at: '2026-10-03T11:00:00.000000Z', updated_at: '2026-10-03T11:00:00.000000Z' })
      tasks.set(created.task_id, created)
      return created
    },
    confirmResearchTask: async (taskId: string) => {
      calls.confirm += 1
      calls.writes.push(`confirm:${taskId}`)
      const confirmed: ResearchTask = { ...(tasks.get(taskId) ?? initial), status: 'confirmed' }
      tasks.set(taskId, confirmed)
      return confirmed
    },
    retryResearchTask: async (taskId: string) => { calls.retry += 1; calls.writes.push(`retry:${taskId}`); return tasks.get(taskId) ?? initial },
    cancelResearchTask: async (taskId: string) => {
      calls.cancel += 1
      calls.writes.push(`cancel:${taskId}`)
      // The host records a terminal cancel on this task and keeps everything it
      // already stored: the specification, the run records and the measured usage
      // survive, which is what the page reads again after the write.
      const cancelled: ResearchTask = { ...(tasks.get(taskId) ?? initial), status: 'cancelled', cancel_note: 'Cancelled by the user.' }
      tasks.set(taskId, cancelled)
      return cancelled
    },
    interpretResearchTask: async (taskId: string) => { calls.interpret += 1; calls.writes.push(`interpret:${taskId}`); return tasks.get(taskId) ?? initial },
  }
  return { api, calls }
}

function openTask(api: ApiAdapter, taskId = 'task-1') {
  window.history.pushState({}, '', `/research/${taskId}`)
  render(<App api={api} />)
}

const pageText = () => document.body.textContent ?? ''
const noWrites = (calls: Calls) => {
  expect(calls.create).toBe(0)
  expect(calls.confirm).toBe(0)
  expect(calls.retry).toBe(0)
  expect(calls.cancel).toBe(0)
  expect(calls.interpret).toBe(0)
  // Nothing here starts a run either: the new experiment needs its own
  // confirmation before any backtest is dispatched.
  expect(calls.backtests).toBe(0)
}
const panelText = () => screen.getByRole('region', { name: 'Recovery' }).textContent ?? ''

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('research recovery', () => {
  it('states what a retryable failure kept and retries it in place only on the click', async () => {
    const user = userEvent.setup()
    const { api, calls } = host(task({
      status: 'failed',
      error: { step: 'propose', code: 'no_tool_call', message: 'The model did not propose a registered spec.' },
    }))
    openTask(api)
    // The task's own failure block explains the same stored sentence as the error
    // detail, so this waits for the region itself instead of for that text.
    await screen.findByRole('region', { name: 'Recovery' })

    // Stored work is described as stored, recorded usage as recorded, and the
    // provider's own charge as the one thing this workspace cannot state.
    expect(panelText()).toMatch(/kept as it is/i)
    expect(panelText()).toMatch(/0 confirmed · 1 unconfirmed draft/)
    expect(panelText()).toMatch(/3 of 10 model calls/)
    expect(panelText()).toMatch(/0 of 5 backtests/)
    expect(panelText()).toMatch(/812 input/)
    expect(panelText()).toMatch(/143 output/)
    expect(panelText()).toMatch(/records usage, not money/i)
    expect(panelText()).toMatch(/not refunded locally/i)

    // The existing in-place retry stays available, and it is offered above the
    // separate experiment. While this failed task still holds the idea, the host
    // would refuse a second one for it, so preparing waits for the reader's own
    // cancel and says so instead of promising an action that cannot succeed.
    expect(screen.getByRole('button', { name: /retry failed step/i })).toBeTruthy()
    const prepare = screen.getByRole('button', { name: /prepare a new experiment/i })
    expect(prepare.hasAttribute('disabled')).toBe(true)
    expect(panelText()).toMatch(/cancel this task first/i)
    expect(panelText()).toMatch(/does not undo any dispatched work/i)
    // The in-place actions are not buried behind the separate experiment's copy.
    const actions = screen.getByRole('region', { name: 'Task actions' })
    const panel = screen.getByRole('region', { name: 'Recovery' })
    expect(actions.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    noWrites(calls)

    // Only the click retries, and it resumes this task in place: no new task, no
    // confirmation, no conclusion and no run of any kind follows from it.
    await user.click(screen.getByRole('button', { name: /retry failed step/i }))
    await waitFor(() => expect(calls.retry).toBe(1))
    expect(calls.writes).toEqual(['retry:task-1'])
    expect(calls.create).toBe(0)
    expect(calls.confirm).toBe(0)
    expect(calls.cancel).toBe(0)
    expect(calls.interpret).toBe(0)
    expect(calls.backtests).toBe(0)
  })

  it('reports an unreported usage as incomplete and a dispatched reservation as charged, not refunded', async () => {
    const { api, calls } = host(task({
      status: 'failed',
      error: { step: 'propose', code: 'provider_error', message: 'The provider call was never observed.' },
      usage: { model_calls: 4, input_tokens: 0, output_tokens: 0, backtests: 1, unavailable_model_calls: 2 },
      spent_calls: [4],
      reservation: { kind: 'model_call', seq: 4, status: 'dispatched', at: '2026-10-03T10:04:00.000000Z' },
    }))
    openTask(api)
    await screen.findByText(/provider call left this process/i)

    // The known tokens and the count that was never reported are named as such,
    // and the recorded usage is the record's, not a promise about the provider.
    expect(panelText()).toMatch(/4 of 10 model calls/)
    expect(panelText()).toMatch(/1 of 5 backtests/)
    expect(panelText()).toMatch(/2 call\(s\) reported none/)
    expect(panelText()).toMatch(/not refunded locally/i)
    // This task is failed too, so it still holds the idea and the separate
    // experiment waits for the reader's own cancel.
    expect(screen.getByRole('button', { name: /prepare a new experiment/i }).hasAttribute('disabled')).toBe(true)
    expect(panelText()).toMatch(/cancel this task first/i)

    // The usage panel keeps its own reading: the totals are incomplete, and the
    // dispatched reservation stays charged.
    expect(pageText()).toMatch(/token usage unavailable/i)
    expect(pageText()).toMatch(/incomplete/i)
    expect(pageText()).toMatch(/dispatched/i)
    noWrites(calls)
  })

  it('keeps the run and its report when the conclusion fails, with no retry and no concluding call', async () => {
    const user = userEvent.setup()
    const { api, calls } = host(task({
      status: 'succeeded',
      spec_draft: null,
      spec_confirmed: CONFIRMED,
      jobs: ['job-1'],
      interpretation: null,
      interpretation_error: { step: 'interpret', code: 'claim_rejected', message: 'The claim did not follow from the report.' },
    }))
    openTask(api)
    await screen.findByText(/nothing was published/i)

    // The run and its verified report are untouched and counted, this page never
    // retries or re-asks the conclusion, and the separate experiment is offered.
    // Both are read from this run's own loaded records, so they are awaited.
    expect(await screen.findByRole('link', { name: /open the run and its verified report/i })).toBeTruthy()
    await waitFor(() => expect(panelText()).toMatch(/1 of the run records read here have a ready report/))
    expect(panelText()).toMatch(/1 confirmed · 0 unconfirmed draft/)
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    // A refused conclusion is not a failed task holding the idea, so the separate
    // experiment is offered straight away here.
    expect(screen.getByRole('button', { name: /prepare a new experiment/i }).hasAttribute('disabled')).toBe(false)

    // The page's own refresh re-reads and never asks the host to conclude again.
    await user.click(screen.getAllByRole('button', { name: /^refresh$/i })[0])
    await waitFor(() => expect(calls.reads).toBeGreaterThan(1))
    noWrites(calls)
  })

  it('prefills a new experiment only from that action and ignores navigation state it cannot read', async () => {
    const user = userEvent.setup()
    const { api, calls } = host(task({ status: 'cancelled', spec_draft: null, spec_confirmed: CONFIRMED, cancel_note: 'Cancelled by the user.' }))
    openTask(api)
    await user.click(await screen.findByRole('button', { name: /prepare a new experiment/i }))

    // The idea is the user's own words, the source task is named on screen, and
    // the capability choice is the user's to make again: nothing was sent.
    const idea = (await screen.findByLabelText('Research idea')) as HTMLTextAreaElement
    expect(idea.value).toBe(IDEA)
    const prepared = screen.getByLabelText('Prepared from a task')
    expect(prepared.textContent).toContain('task-1')
    expect(prepared.textContent).toMatch(/capabilit/i)
    noWrites(calls)

    // Ambient state is never a source of an experiment. The entry is shaped the
    // way the router writes its own — the payload under `usr` — and the payload
    // carries the handoff's fields with nothing readable in them: the task id is
    // not a task id and the idea is empty. It is ignored whole: no banner, an
    // empty form, and the same string in the query is no source either.
    cleanup()
    window.history.pushState({ usr: { recovery: { task_id: 7, idea: '' } }, key: 'malformed', idx: 0 }, '', '/research?recovery=%7B%22task_id%22%3A%22task-1%22%7D')
    render(<App api={api} />)
    const empty = (await screen.findByLabelText('Research idea')) as HTMLTextAreaElement
    expect(empty.value).toBe('')
    expect(screen.queryByLabelText('Prepared from a task')).toBeNull()
    expect(calls.create).toBe(0)

    // The form still works from what is typed, and sends only that.
    await user.type(empty, 'A different idea')
    await user.click(screen.getByRole('button', { name: /create draft/i }))
    await waitFor(() => expect(calls.create).toBe(1))
    expect(calls.created[0]).toEqual(['A different idea', undefined])
  })

  it('creates the new experiment on one explicit click and confirms it separately, leaving the original alone', async () => {
    const user = userEvent.setup()
    const original = task({ status: 'cancelled', spec_draft: null, spec_confirmed: CONFIRMED, cancel_note: 'Cancelled by the user.' })
    const before = JSON.stringify(original)
    const { api, calls } = host(original)
    openTask(api)
    await user.click(await screen.findByRole('button', { name: /prepare a new experiment/i }))
    await screen.findByLabelText('Research idea')

    // Creating is the only write, and it sends the idea unchanged with no
    // capability selection, spec, budget or job identity copied from task-1.
    await user.click(screen.getByRole('button', { name: /create draft/i }))
    await waitFor(() => expect(calls.create).toBe(1))
    expect(calls.created[0]).toEqual([IDEA, undefined])
    expect(calls.writes).toEqual(['create:task-2'])

    // The new task is its own task and needs its own confirmation: the recovery
    // path alone starts no backtest.
    await screen.findByRole('button', { name: /confirm spec and start real backtest/i })
    expect(calls.backtests).toBe(0)
    await user.click(screen.getByRole('button', { name: /confirm spec and start real backtest/i }))
    await waitFor(() => expect(calls.confirm).toBe(1))
    expect(calls.writes).toEqual(['create:task-2', 'confirm:task-2'])

    // The original task document is exactly as it was, and no write named it.
    expect(JSON.stringify(original)).toBe(before)
    expect(calls.writes.some(entry => entry.includes('task-1'))).toBe(false)
  })

  it('never resubmits an unknown dispatch, and offers the new experiment only after the legal cancel', async () => {
    const user = userEvent.setup()
    const { api, calls } = host(task({
      status: 'failed',
      error: { step: 'dispatch', code: 'job_outcome_unknown', message: 'The submission was never confirmed.' },
      last_model: { model: 'scripted-proposal-fixture', at: '2026-10-03T10:00:00.000000Z', outcome: 'unknown_after_dispatch' },
      reservation: { kind: 'backtest', seq: 3, status: 'unknown', at: '2026-10-03T10:04:00.000000Z' },
      spent_calls: [3],
    }))
    openTask(api)
    await screen.findByText(/nothing is re-submitted automatically/i)

    // A render and the page's own refresh re-read; neither re-submits. This task is
    // failed, so it still holds the idea and preparing a second one is refused
    // until the reader cancels: the page says so and writes nothing by itself.
    await user.click(screen.getAllByRole('button', { name: /^refresh$/i })[0])
    await waitFor(() => expect(calls.reads).toBeGreaterThan(1))
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    expect(screen.getByRole('button', { name: /prepare a new experiment/i }).hasAttribute('disabled')).toBe(true)
    expect(panelText()).toMatch(/cancel this task first/i)
    expect(panelText()).toMatch(/does not undo any dispatched work/i)
    expect(panelText()).toMatch(/not refunded locally/i)
    expect(panelText()).toMatch(/3 of 10 model calls/)
    noWrites(calls)

    // Navigating away and back is the same: the dispatch is never re-offered and
    // the refused preparation survives the round trip.
    await user.click(screen.getByRole('link', { name: /research workspace/i }))
    await screen.findByLabelText('Research idea')
    await user.click(await screen.findByRole('link', { name: /open task/i }))
    await screen.findByText(/nothing is re-submitted automatically/i)
    expect(screen.getByRole('button', { name: /prepare a new experiment/i }).hasAttribute('disabled')).toBe(true)
    expect(calls.cancel).toBe(0)

    // Cancelling is the reader's own explicit click and the only write here: the
    // host records a terminal cancel that keeps the specification and the measured
    // usage, and only after that answer does the preparation become available.
    await user.click(screen.getByRole('button', { name: /^cancel task$/i }))
    await waitFor(() => expect(calls.cancel).toBe(1))
    expect(calls.writes).toEqual(['cancel:task-1'])
    await waitFor(() => expect(screen.getByRole('button', { name: /prepare a new experiment/i }).hasAttribute('disabled')).toBe(false))
    expect(panelText()).toMatch(/3 of 10 model calls/)
    expect(panelText()).toMatch(/0 confirmed · 1 unconfirmed draft/)
    expect(panelText()).not.toMatch(/cancel this task first/i)

    // Only now, and only on that click, does the preparation leave for the form:
    // it copies the idea and names the task, and starts nothing at all.
    await user.click(screen.getByRole('button', { name: /prepare a new experiment/i }))
    const idea = (await screen.findByLabelText('Research idea')) as HTMLTextAreaElement
    expect(idea.value).toBe(IDEA)
    expect((await screen.findByLabelText('Prepared from a task')).textContent).toContain('task-1')
    expect(calls.create).toBe(0)
    expect(calls.confirm).toBe(0)
    expect(calls.retry).toBe(0)
    expect(calls.backtests).toBe(0)
    expect(calls.interpret).toBe(0)
    expect(calls.cancel).toBe(1)
  })
})
