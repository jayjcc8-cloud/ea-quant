/**
 * The conclusion seam: one verified run, one host-rendered conclusion.
 *
 * The adapter below is a scripted double. Every conclusion, reading and
 * limitation in these tests is a fixture the host would have written, so what
 * is proved here is only that this client shows what the backend persisted,
 * renders no model wording of its own, and never asks for a conclusion unless
 * the user presses the one explicit button.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter, type BacktestJob } from './app'
import type { ModelConfigurationStatus, ResearchInterpretation, ResearchTask } from './research-api'

const spec = {
  strategy_id: 'bounded-long-v1',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'One registered entry with a two-unit target.',
  scenario_id: null,
  dataset_id: null,
  source_sha256: null,
  parameters: { target_quantity: '2', entry_delay_bars: 0 },
  assumptions: ['The registered scenario window is representative.'],
  criterion: {
    schema: 'radian.report-criterion.v1' as const,
    field: 'economics.net_pnl.amount',
    expectation: 'positive' as const,
  },
}

const interpretation: ResearchInterpretation = {
  schema: 'radian.research-interpretation.v2',
  question: 'Test a two-unit entry on AAPL',
  question_is_user_input: true,
  criterion: spec.criterion,
  criterion_met: true,
  observed: {
    state: 'value',
    field: 'economics.net_pnl.amount',
    unit: 'currency',
    value: '12.5',
    sign: 'positive',
  },
  conclusion_scope: 'frozen_single_run',
  conclusion_scope_text: 'HOST SCOPE SENTENCE',
  host_inference: 'criterion_direction_matched',
  outcome: 'supported_in_scope',
  outcome_text: 'HOST OUTCOME SENTENCE',
  inference: 'criterion_direction_matched',
  inference_text: 'HOST INFERENCE SENTENCE',
  limitations: [{ code: 'no_out_of_sample_period', text: 'HOST LIMITATION SENTENCE' }],
  report: { job_id: 'job-1' },
  data_scope: {
    record_count: 40,
    dataset_id: null,
    source_sha256: 'e'.repeat(64),
    scenario_sha256: 'f'.repeat(64),
    input_sha256: 'a'.repeat(64),
    time_range: {
      start_inclusive: '2026-01-02T09:31:00.000000Z',
      end_exclusive: '2026-01-02T09:34:00.000000Z',
      timezone: 'UTC',
      selection_field: 'available_at',
    },
    point_in_time: null,
    quality_verified: false,
    unavailable: ['point_in_time', 'quality_verified'],
  },
  model: { provider: 'fixture', model: 'fixture-model' },
  versions: {},
}

function task(overrides: Partial<ResearchTask> = {}): ResearchTask {
  return {
    schema: 'radian.research-task.v1',
    task_id: 'task-1',
    created_at: '2026-10-05T10:00:00.000000Z',
    updated_at: '2026-10-05T10:00:00.000000Z',
    status: 'succeeded',
    raw_idea: 'Test a two-unit entry on AAPL',
    gap: null,
    budget: { max_model_calls: 10, max_backtests: 5 },
    usage: { model_calls: 2, input_tokens: 812, output_tokens: 143, backtests: 1 },
    spec_draft: null,
    spec_confirmed: [{ version: 1, confirmed_at: '2026-10-05T10:01:00.000000Z', spec }],
    jobs: ['job-1'],
    request_id: 'request-1',
    error: null,
    cancel_note: null,
    last_model: { model: 'fixture-model', at: '2026-10-05T10:02:00.000000Z', outcome: 'observed' },
    reservation: null,
    backtest_reservation: null,
    spent_calls: [1, 2],
    operation_generation: 3,
    operation_completion_generation: 3,
    operation_kind: 'interpret',
    runtime_diagnostic: null,
    interpretation: null,
    interpretation_error: null,
    ...overrides,
  }
}

const job: BacktestJob = {
  schema: 'ea.local-web-job.v2',
  job_id: 'job-1',
  request_id: 'request-1',
  scenario_id: 'bounded-long.yaml',
  input_identity: { scenario_sha256: 'a'.repeat(64), data_sha256: 'b'.repeat(64), record_count: 40 },
  status: 'succeeded',
  engine_run_id: 'run-1',
  report_sha256: 'c'.repeat(64),
  summary_sha256: 'd'.repeat(64),
  error_code: null,
  message: null,
  report_ready: true,
  strategy_descriptor: {
    strategy_id: 'bounded-long-v1',
    strategy_version: 1,
    research_visible: true,
    parameters: [],
  },
}

const configured: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1',
  settings_file: '/tmp/radian/settings.json',
  configured: true,
  provider: 'example-provider',
  model: 'example-model-v1',
  base_url: 'http://127.0.0.1:9999',
  api_key_env: null,
  endpoint: 'http://127.0.0.1:9999',
  protocol: 'example-protocol',
  capabilities: { tool_use: true, structured_output: false, usage: true },
  required_capabilities: ['tool_use'],
  missing_capabilities: [],
  configuration_error: null,
}

function scripted(script: { task?: ResearchTask; interpret?: () => Promise<ResearchTask> } = {}) {
  const calls = { interpret: 0, get: 0, backtest: 0 }
  let persisted = task(script.task)
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    createHoldout: async () => { throw new Error('unused') },
    getHoldout: async () => { throw new Error('unused') },
    listHoldouts: async () => [],
    listScenarios: async () => [],
    validateScenario: async () => { throw new Error('unused') },
    createBacktest: async () => { calls.backtest += 1; return job },
    createBatch: async () => { throw new Error('unused') },
    getBatch: async () => { throw new Error('unused') },
    listBacktests: async () => [],
    getBacktest: async () => job,
    getReport: async () => { throw new Error('unused') },
    getEquityPath: async () => { throw new Error('unused') },
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    settingsStatus: async () => configured,
    listResearchTasks: async () => [persisted],
    getResearchTask: async () => { calls.get += 1; return persisted },
    interpretResearchTask: async () => {
      calls.interpret += 1
      persisted = script.interpret ? await script.interpret() : persisted
      return persisted
    },
  }
  return { api, calls }
}

afterEach(cleanup)

async function openTask(api: ApiAdapter) {
  window.history.pushState({}, '', '/research/task-1')
  render(<App api={api} />)
  await screen.findByRole('heading', { name: 'Research Task', level: 1 })
}

describe('the conclusion for one verified run', () => {
  it('shows only what the host wrote, and carries the question as user input', async () => {
    const { api } = scripted({ task: task({ interpretation }) })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('HOST OUTCOME SENTENCE')
    expect(panel.textContent).toContain('HOST INFERENCE SENTENCE')
    expect(panel.textContent).toContain('HOST LIMITATION SENTENCE')
    // The host wrote the value with the exact currency it read; this client adds
    // no unit of its own and never turns the unit word into a currency.
    expect(panel.textContent).toContain('12.5')
    expect(panel.textContent).toContain('currency not recorded by the host')
    expect(panel.textContent).not.toContain('USD')
    expect(panel.textContent).toContain('Question (your input)')
    expect(panel.textContent).toContain('HOST SCOPE SENTENCE')
    // The host's own three-valued fact about the frozen criterion is shown as
    // the host computed it, beside the sentence it came from.
    expect(screen.getByText('Criterion met').parentElement?.textContent).toContain('Met')
    expect(panel.textContent).toContain('2026-01-02T09:31:00.000000Z')
    expect(panel.textContent).toContain('Backtest only')
  })

  it('shows the data scope the conclusion was allowed to lean on, and what it does not carry', async () => {
    const { api } = scripted({ task: task({ interpretation }) })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('Data scope')
    expect(panel.textContent).toContain('40')
    expect(panel.textContent).toContain('a'.repeat(64))
    // A fact this product cannot prove is named, never left blank and never
    // silently assumed: the conclusion cannot be read as more scope than it has.
    expect(panel.textContent).toContain('point_in_time')
    expect(panel.textContent).toContain('quality_verified')
    expect(panel.textContent).toContain('Not available')
  })

  it('claims no data scope at all when the host recorded none', async () => {
    const { api } = scripted({ task: task({ interpretation: { ...interpretation, data_scope: null } }) })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('no data scope for this conclusion')
  })

  it('shows the one frozen scope the host published, with the reading intact', async () => {
    const { api } = scripted({ task: task({ interpretation }) })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    // The accepted input freezes one single-run scope, so that is the only
    // scope sentence a conclusion can carry: a claim wider than the run is
    // refused by the host and can never reach this page, and nothing here
    // offers to widen one. What the report did settle stays settled.
    expect(panel.textContent).toContain('HOST SCOPE SENTENCE')
    expect(screen.getByText('Criterion met').parentElement?.textContent).toContain('Met')
    expect(screen.queryByRole('button', { name: /wider|beyond|scope/i })).toBeNull()
  })

  it('never asks for a conclusion from reading or refreshing the page', async () => {
    const { api, calls } = scripted({ task: task({ interpretation }) })
    await openTask(api)
    await screen.findByRole('region', { name: 'Conclusion' })
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls.get).toBeGreaterThan(1))
    expect(calls.interpret).toBe(0)
  })

  it('asks the host exactly once, and only when the button is pressed', async () => {
    const { api, calls } = scripted({
      interpret: async () => task({ interpretation }),
    })
    await openTask(api)

    const button = await screen.findByRole('button', { name: 'Interpret this run' })
    expect(calls.interpret).toBe(0)
    await userEvent.click(button)
    await waitFor(() => expect(calls.interpret).toBe(1))
    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('HOST OUTCOME SENTENCE')
    // The action is gone once a conclusion exists.
    expect(screen.queryByRole('button', { name: 'Interpret this run' })).toBeNull()
    // Exactly one provider call: drawing a conclusion starts no backtest.
    expect(calls.backtest).toBe(0)
  })

  it('shows the value the host published, with the unit word kept as a unit', async () => {
    const { api } = scripted({
      task: task({ interpretation: { ...interpretation, observed: { ...interpretation.observed, value_text: '12.5 USD', currency: 'USD' } } }),
    })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('12.5 USD · positive')
    // The unit word is never rendered as if it were a currency of its own.
    expect(panel.textContent).not.toContain('(currency)')
    expect(panel.textContent).not.toContain('currency not recorded')
  })

  it('retries a failed conclusion step and starts no backtest for it', async () => {
    let attempts = 0
    const { api, calls } = scripted({
      interpret: async () => {
        attempts += 1
        if (attempts === 1) throw new Error('an operation is already in flight')
        return task({ interpretation })
      },
    })
    await openTask(api)

    await userEvent.click(await screen.findByRole('button', { name: 'Interpret this run' }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'an operation is already in flight')
    expect(calls.interpret).toBe(1)
    // Only the conclusion step is re-asked: the run it reads is untouched and
    // nothing dispatches a second backtest.
    await userEvent.click(screen.getByRole('button', { name: 'Interpret this run' }))
    await waitFor(() => expect(calls.interpret).toBe(2))
    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('HOST OUTCOME SENTENCE')
    expect(calls.backtest).toBe(0)
  })

  it('reports a conclusion-step failure on its own axis without touching the run', async () => {
    const { api } = scripted({
      task: task({
        interpretation: null,
        interpretation_error: {
          step: 'interpret',
          code: 'model_outcome_unknown',
          message: 'the call left this process',
        },
      }),
    })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('unproven')
    expect(panel.textContent).toContain('model_outcome_unknown')
    expect(panel.textContent).toContain('unchanged')
    // The run's own succeeded computation is still what the page reports.
    const title = screen.getByRole('heading', { name: 'Research Task', level: 1 })
    expect(title.closest('header')?.textContent).toContain('backtest succeeded')
    expect(screen.queryByRole('button', { name: 'Interpret this run' })).toBeNull()
  })

  it('shows a cancelled conclusion as final and offers no further call', async () => {
    const { api } = scripted({
      task: task({
        interpretation: null,
        cancel_note: 'The completed run and its verified report are unchanged.',
        interpretation_error: {
          step: 'interpret',
          code: 'cancelled_by_user',
          message: 'the user cancelled before a conclusion was published',
        },
      }),
    })
    await openTask(api)

    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('You cancelled')
    expect(panel.textContent).toContain('unchanged')
    expect(screen.queryByRole('button', { name: 'Interpret this run' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel without a conclusion' })).toBeNull()
  })

  it('shows the criterion frozen before the run beside the confirmed spec', async () => {
    const { api } = scripted({ task: task({ interpretation }) })
    await openTask(api)
    const specPanel = await screen.findByRole('region', { name: 'Specification' })
    expect(specPanel.textContent).toContain('Frozen criterion')
    expect(specPanel.textContent).toContain('Net P&L')
    expect(specPanel.textContent).toContain('expected positive')
  })

  it('says plainly when no criterion was frozen', async () => {
    const withoutCriterion = { ...spec, criterion: null }
    const { api } = scripted({
      task: task({
        spec_confirmed: [{ version: 1, confirmed_at: '2026-10-05T10:01:00.000000Z', spec: withoutCriterion }],
        interpretation: {
          ...interpretation,
          criterion: null,
          criterion_met: null,
          outcome: 'insufficient_evidence',
          outcome_text: 'HOST OUTCOME SENTENCE',
          inference: 'criterion_not_stated',
          inference_text: 'HOST INFERENCE SENTENCE',
          observed: { state: 'not_stated' },
          limitations: [],
        },
      }),
    })
    await openTask(api)
    const specPanel = await screen.findByRole('region', { name: 'Specification' })
    expect(specPanel.textContent).toContain('None was stated')
    const panel = await screen.findByRole('region', { name: 'Conclusion' })
    expect(panel.textContent).toContain('None was stated before the run')
    expect(panel.textContent).toContain('HOST OUTCOME SENTENCE')
    expect(screen.getByText('Criterion met').parentElement?.textContent).toContain('Not determined')
  })
})
