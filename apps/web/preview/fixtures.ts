/**
 * Fixtures for the isolated UI preview.
 *
 * Two kinds of document live here, and every block below says which it is:
 *
 * - COPIED: the succeeded task, its verified report, its equity path and the host
 *   conclusion are the documents observed in
 *   `implementation-context/observed-data.json` — one real local run. Amounts,
 *   instants, digests, ids and host sentences are copied, never paraphrased: the
 *   net P&L is 14.97 USD after 2.03 in fees, equity ends at 10014.97, the ending
 *   position is 2 units, the bound data has four records, the report schema is
 *   ea.backtest-report.v1 and the replay window is
 *   2026-01-02T09:31:00.000000Z → 2026-01-02T09:34:00.000000Z. A v1 report
 *   records no realized/unrealized split, so none is invented here.
 * - SCRIPTED: every other lifecycle document (drafting, running, retryable
 *   failure, unknown outcome, unsupported, cancelled, and the same succeeded run
 *   before a conclusion was drawn) is handwritten on top of the copied frozen
 *   spec, so the shipped components can be seen in those states. The trade
 *   analytics, holdout, batch and model-configuration documents on the evidence
 *   and result pages are scripted for the same reason.
 *
 * One document is a projection rather than a byte-for-byte copy: the web types
 * carry the fields this client renders, and the stored records carry more. Where
 * a field of the observed document has no place in that projection — the
 * conclusion's nested claim evidence, the report's digest-bound guarantee rules,
 * the path's data and scenario digests — it is named in the comment above the
 * field rather than dropped silently or filled in with a guess.
 *
 * Nothing here is a model output, an engine result or a live request: the
 * in-memory adapter in `main.tsx` serves all of it, and this file imports no
 * network, service or engine code of any kind.
 */
import type { BacktestJob, BacktestReport, ChronologicalHoldout, ExperimentBatch, ScenarioSummary } from '../src/app'
import type { EquityPath } from '../src/equity-path'
import type { ModelConfigurationStatus, ResearchInterpretation, ResearchSpec, ResearchTask } from '../src/research-api'
import type { TradeAnalytics } from '../src/trade-analytics'

/**
 * Identity copied from the observed record.
 *
 * The web-job document itself (request id, summary digest, equity-path digest)
 * was not part of the observed record, so those three fields are scripted and
 * marked as such below; nothing reads them.
 */
const OBSERVED = {
  task_id: 'bc8a82cb-129c-4de0-b7c2-bdfe9df125b3',
  job_id: '3cae942b-b9dd-4c26-ac23-aaf488873ee8',
  run_id: '8cc87dc2-acb3-4865-93af-b56613b4688a',
  report_sha256: 'bcca043dfe5c31f063f3c0d043b567bde7f2b4eb42ca049938758737dc0171f4',
  semantic_outcome_sha256: '9912caf8c6750a1b7561a597f70c73d97c5d16d00cf96fec56628756e3cc103b',
  scenario_sha256: '8d17544deb8047d8451c16c0a36e5f0f05e47f05366b70b85cea46af418c3420',
  data_sha256: 'c95c6182ba68d8c03726336172b5ce089c464fdd87ba28cb4444460fcdbae2fb',
  input_sha256: 'f3545f8d20517ae5257831ce26a343a8b4ec4dc84986f217dd13d1d5aea06c90',
  created_at: '2026-10-05T14:16:31.443924Z',
  confirmed_at: '2026-10-05T14:17:25.496261Z',
  updated_at: '2026-10-05T14:34:47.925416Z',
  record_count: 4,
} as const

/** Scripted: the observed record carries no web-job request or digest fields. */
const SCRIPTED_REQUEST_ID = 'scripted-request-1'
const SCRIPTED_SUMMARY_SHA = '0'.repeat(64)
const SCRIPTED_PATH_SHA = '1'.repeat(64)

/** Copied: the observed scenario identity and the observed strategy parameters. */
export const scenario: ScenarioSummary = {
  scenario_id: 'bounded-long-commission.yaml',
  name: 'bounded-long-commission',
  valid: true,
  input_identity: { scenario_sha256: OBSERVED.scenario_sha256, data_sha256: OBSERVED.data_sha256, record_count: OBSERVED.record_count },
  summary: {
    strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000',
    parameters: { target_quantity: '2', entry_delay_bars: 0 }, record_count: OBSERVED.record_count,
  },
}

/** Copied ids and digests; the three scripted fields are named above. */
export const job: BacktestJob = {
  schema: 'ea.local-web-job.v3',
  job_id: OBSERVED.job_id,
  request_id: SCRIPTED_REQUEST_ID,
  scenario_id: 'bounded-long-commission.yaml',
  input_identity: { scenario_sha256: OBSERVED.scenario_sha256, data_sha256: OBSERVED.data_sha256, record_count: OBSERVED.record_count },
  status: 'succeeded',
  engine_run_id: OBSERVED.run_id,
  report_sha256: OBSERVED.report_sha256,
  summary_sha256: SCRIPTED_SUMMARY_SHA,
  equity_path_sha256: SCRIPTED_PATH_SHA,
  error_code: null,
  message: null,
  report_ready: true,
  strategy_descriptor: { strategy_id: 'bounded-long-v1', strategy_version: 1, research_visible: true, parameters: [] },
}

/**
 * Copied: the observed run's verified report, as the web client models it.
 *
 * Identity, source and every economics field are the stored values. It is a v1
 * document, so it carries no realized/unrealized split, no position_state and no
 * execution legs, and the client must show its net figure without presenting it as
 * realized profit. The stored document also carries its digest-bound guarantee
 * rules, lineage and generator blocks, which this projection has no field for.
 */
export const report: BacktestReport = {
  schema: 'ea.backtest-report.v1',
  run_id: OBSERVED.run_id,
  source: { strategy: { id: 'bounded-long-v1' }, instrument: { venue: 'XNAS', symbol: 'AAPL' } },
  economics: {
    currency: 'USD',
    initial_funding: { amount: '10000', currency: 'USD' },
    ending_cash: [{ amount: '9794.97', currency: 'USD' }],
    ending_positions: [{ quantity: '2', venue: 'XNAS', symbol: 'AAPL' }],
    valuation: { price: '110', position_value: '220' },
    equity: { amount: '10014.97' },
    net_pnl: { amount: '14.97' },
    fees: { amount: '2.03', currency: 'USD', count: 1, rule: 'deterministic-commission-v1' },
    total_return: { value: '0.001497' },
    counts: { orders: 1, fills: 1 },
    execution: { order: { quantity: '2', side: 'buy' }, fill: { quantity: '2', price: '101.5', side: 'buy' } },
  },
}

/**
 * Copied: the derived equity-path evidence of the same observed run.
 *
 * The five display points, the sampling label and every drawdown fact are the
 * stored values. The stored document also binds the data and scenario digests,
 * the record count and the valuation rule, which this projection has no field for.
 */
export const equityPath: EquityPath = {
  schema: 'ea.backtest-equity-path.v1',
  run_id: OBSERVED.run_id,
  report_sha256: OBSERVED.report_sha256,
  currency: 'USD',
  point_count: 5,
  display_sampling: 'uniform-index-extrema-v1',
  display_points: [
    { index: 0, time: '2026-01-02T09:31:00.000000Z', equity: '10000' },
    { index: 1, time: '2026-01-02T09:31:00.000000Z', equity: '10000' },
    { index: 2, time: '2026-01-02T09:31:30.000000Z', equity: '10000' },
    { index: 3, time: '2026-01-02T09:32:00.000000Z', equity: '9997.97' },
    { index: 4, time: '2026-01-02T09:33:00.000000Z', equity: '10014.97' },
  ],
  max_drawdown: {
    amount: '2.03', ratio: '0.000203', peak_index: 0, peak_time: '2026-01-02T09:31:00.000000Z',
    peak_equity: '10000', trough_index: 3, trough_time: '2026-01-02T09:32:00.000000Z', trough_equity: '9997.97',
  },
}

/**
 * Scripted: the observed record carries no trade-analytics document.
 *
 * The run ends holding two units and closes no trade, so every denominator that
 * needs a closed trade stays unavailable rather than invented.
 */
export const analytics: TradeAnalytics = {
  schema: 'ea.trade-analytics.v1',
  run_id: OBSERVED.run_id,
  report_sha256: OBSERVED.report_sha256,
  currency: 'USD',
  closed_trades: 0, wins: 0, losses: 0, breakeven: 0,
  win_rate: null, average_win: null, average_loss: null, payoff_ratio: null,
  average_holding_seconds: null, has_open_position: true, trades: [],
}

/** Scripted: no holdout was run for the observed task. */
export const holdout: ChronologicalHoldout = {
  schema: 'ea.chronological-holdout.v1',
  validation_id: 'scripted-holdout-1',
  created_at: '2026-10-05T14:40:00.000000Z',
  source_job_id: OBSERVED.job_id,
  holdout_job_id: OBSERVED.job_id,
}

/** Scripted: no batch was created for the observed task. */
export const batch: ExperimentBatch = {
  schema: 'ea.local-web-batch.v1',
  batch_id: 'scripted-batch-1',
  created_at: '2026-10-05T14:35:00.000000Z',
  scenario_id: 'bounded-long-commission.yaml',
  input_identity: job.input_identity,
  member_job_ids: [OBSERVED.job_id],
  member_count: 1,
  status: 'complete',
  members: [{ ...job, presentation_status: 'succeeded' }],
}

/**
 * Scripted document, with the provider identity copied from the observed
 * conclusion: the observed run's conclusion was produced by this provider and
 * model.
 *
 * Nothing else in it was observed. No settings file is read by this page, and the
 * protocol id is left unclaimed rather than guessed for a real provider; the
 * capability flags exist only so the panel renders its normal configured shape,
 * and they assert nothing about what that provider actually declares. A reviewer
 * reading this page is reading a scripted configuration, not a verified one.
 */
export const settings: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1',
  settings_file: 'preview-only: no settings file is read by this page',
  configured: true,
  provider: 'deepseek',
  model: 'deepseek-flash',
  base_url: 'https://api.deepseek.com/v1',
  api_key_env: null,
  endpoint: 'https://api.deepseek.com/v1',
  protocol: null,
  capabilities: { tool_use: true, structured_output: true, usage: true },
  required_capabilities: ['tool_use'],
  missing_capabilities: [],
  configuration_error: null,
}

/** Copied: the idea the observed task was created from. */
export const idea = 'For one bounded workflow check, use only the registered bounded-long-commission.yaml AAPL/XNAS example scenario and bounded-long-v1 strategy. Use initial_cash 10000, target_quantity 2 and entry_delay_bars 0. Freeze one question before the run: is this single run\'s after-commission net P&L positive? State criterion field economics.net_pnl.amount with expectation positive. Do not optimize, use holdout/private data, claim profitability or robustness, or widen the conclusion beyond this frozen single run.'

/** Copied: the confirmed spec of the observed run, verbatim. */
const spec: ResearchSpec = {
  strategy_id: 'bounded-long-v1',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'The idea asks one narrow, pre-registered question of a single default-configured run: is the after-commission net P&L of bounded-long-v1 on AAPL/XNAS under bounded-long-commission.yaml positive? Freezing economics.net_pnl.amount with expectation positive before the run fixes the interpretation in advance, so the result is read against that one criterion and nothing else, avoiding post-hoc metric selection. Because the parameter values equal the registered defaults, the run adds no selection pressure, and no optimization, holdout data, or extra scenarios are involved. What this run can show: the sign of net P&L for exactly this one scenario/strategy/instrument configuration, including whatever commission the registered scenario applies. What it cannot show: that the strategy is profitable in general, that the sign would persist on other symbols, venues, date ranges, parameter values, or data splits, that the result is robust to costs, slippage or latency (the slippage and latency scenario variants are deliberately not run), or anything about statistical significance from a sample of one run. Even a positive net P&L would be a single observation, not evidence of an edge, and no profit claim is made or implied.',
  scenario_id: 'bounded-long-commission.yaml',
  dataset_id: null,
  source_sha256: null,
  parameters: { target_quantity: '2', entry_delay_bars: 0 },
  assumptions: [
    'The registered scenario bounded-long-commission.yaml is used unchanged; the only parameter values supplied (target_quantity 2, entry_delay_bars 0) are the registered defaults for bounded-long-v1, so this is a default-configuration run, not a tuned one.',
    'The instrument is the registered AAPL/XNAS example; no other symbol, venue, or dataset is introduced.',
    'The commission model is whatever the registered bounded-long-commission.yaml scenario already embeds; it is not modified, added to, or re-specified here.',
    'initial_cash is 10000, matching the registered scenario.',
    'The run is a single bounded workflow check on one scenario configuration: no parameter sweep, no holdout/private data, no re-runs to select a favorable outcome.',
    'economics.net_pnl.amount is read as the after-commission net P&L of this one run as reported by the engine; no other field is used to interpret the outcome.',
  ],
  criterion: { schema: 'radian.report-criterion.v1', field: 'economics.net_pnl.amount', expectation: 'positive' },
}

/**
 * Copied: the host conclusion published for the observed run.
 *
 * Every sentence, limitation and scope fact below is the host's own text, and its
 * identity is the identity of the job and report above: `report.job_id` and
 * `report.run_id` are the job and run bound to this fixture, and the observed
 * value is that report's own net P&L, 14.97 USD.
 */
export const interpretation: ResearchInterpretation = {
  schema: 'radian.research-interpretation.v2',
  question: idea,
  question_is_user_input: true,
  criterion: spec.criterion ?? null,
  criterion_met: true,
  observed: {
    state: 'value',
    field: 'economics.net_pnl.amount',
    unit: 'currency',
    value: '14.97',
    sign: 'positive',
    value_text: '14.97 USD',
    currency: 'USD',
    report_schema: 'ea.backtest-report.v1',
    json_type: 'string',
  },
  conclusion_scope: 'frozen_single_run',
  conclusion_scope_text: 'This conclusion is about the single frozen experiment above and claims nothing wider.',
  host_inference: 'criterion_direction_matched',
  outcome: 'supported_in_scope',
  outcome_text: 'Within this single frozen experiment, the result is consistent with the stated expectation. This is a statement about one criterion and one run, not about the idea in general.',
  inference: 'criterion_direction_matched',
  inference_text: 'The frozen criterion field changed in the expected direction.',
  limitations: [
    { code: 'no_out_of_sample_period', text: 'No out-of-sample period was held back from this run.' },
    { code: 'no_benchmark_comparison', text: 'The verified report carries no benchmark or buy-and-hold comparison.' },
    { code: 'deterministic_engine_no_uncertainty_estimate', text: 'The engine is deterministic; this is one path with no uncertainty estimate.' },
    { code: 'fees_are_modelled_not_observed', text: 'Fees come from the rule recorded in the report, not from a venue.' },
    { code: 'backtest_only_no_live_execution', text: 'Backtest only; nothing was executed live.' },
    { code: 'coverage_unknown', text: 'Nothing here proves the captured window covers the time the question is about, so time coverage is unknown rather than known to be short.' },
    { code: 'point_in_time_not_proven', text: 'no PIT proof provided: nothing here shows the capture is the point-in-time membership that was tradeable then.' },
    { code: 'data_quality_not_independently_verified', text: 'The captured data is host-verified by hash only; no independent audit of its content was performed.' },
  ],
  report: {
    job_id: OBSERVED.job_id,
    run_id: OBSERVED.run_id,
    report_schema: 'ea.backtest-report.v1',
    report_sha256: OBSERVED.report_sha256,
    input_sha256: OBSERVED.input_sha256,
    semantic_outcome_sha256: OBSERVED.semantic_outcome_sha256,
  },
  data_scope: {
    record_count: OBSERVED.record_count,
    dataset_id: null,
    source_sha256: null,
    scenario_sha256: OBSERVED.scenario_sha256,
    input_sha256: OBSERVED.input_sha256,
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
  // Copied from the observed record: the provider and model that wrote it.
  model: { provider: 'deepseek', model: 'deepseek-flash', endpoint: 'https://api.deepseek.com/v1' },
  // Copied whole. The web type carries the rendered conclusion, not the nested
  // claim-evidence document the host keeps, so `observed.evidence_ref` is the one
  // field of the stored record that has no place in this projection.
  versions: {
    criterion_schema: 'radian.report-criterion.v1', dataset_id: null,
    operation: 'radian.research-interpretation.v1', pinned_binding: null,
    prompt: 'radian.research-interpretation-prompt.v2', renderer: 'radian.research-interpretation.v2',
    source_sha256: null, spec_version: 1, strategy_id: 'bounded-long-v1',
    tools: 'radian.research-interpretation-tools.v2',
  },
}

/** Copied: the frozen spec as the host confirmed it, at the instant it recorded. */
const frozen = [{ version: 1, confirmed_at: OBSERVED.confirmed_at, spec }]
/** Copied: the usage the record measured for the finished task. */
const measured = { model_calls: 2, input_tokens: 7622, output_tokens: 767, backtests: 1, unavailable_model_calls: 0 }
/** Scripted: the measured usage of the propose step, before any run was dispatched. */
const proposed = { model_calls: 1, input_tokens: 4100, output_tokens: 210, backtests: 0, unavailable_model_calls: 0 }
/** Scripted: one model call and one dispatched run, before a conclusion was asked for. */
const dispatched = { model_calls: 1, input_tokens: 4100, output_tokens: 210, backtests: 1, unavailable_model_calls: 0 }

/**
 * Copied: the observed task as it was stored, at the position it was captured in.
 *
 * Every field the record carries is the recorded value: both instants, the idea,
 * the budget, the measured usage of the finished run, the confirmed spec with its
 * frozen criterion, and the bound job. The record carries no operation, spend or
 * reservation bookkeeping at all, so those fields are the empty shape a task with
 * nothing in flight has and are scripted rather than copied; the scripted states
 * below override the status, the spec split, the jobs and the usage they need.
 */
const observed: ResearchTask = {
  schema: 'radian.research-task.v1',
  task_id: OBSERVED.task_id,
  created_at: OBSERVED.created_at,
  updated_at: OBSERVED.updated_at,
  status: 'succeeded',
  raw_idea: idea,
  gap: null,
  budget: { max_model_calls: 10, max_backtests: 5 },
  usage: measured,
  spec_draft: null,
  spec_confirmed: frozen,
  jobs: [OBSERVED.job_id],
  request_id: null,
  error: null,
  cancel_note: null,
  last_model: null,
  reservation: null,
  backtest_reservation: null,
  spent_calls: [],
  operation_generation: 3,
  operation_completion_generation: 3,
  operation_kind: null,
  runtime_diagnostic: null,
  interpretation: null,
  interpretation_error: null,
}

/** Copied: the observed task as served, conclusion included. */
const concluded: ResearchTask = { ...observed, interpretation }

/** Scripted: cancelling is a terminal record; the run and its report are untouched. */
export const cancelTask = (from: ResearchTask): ResearchTask => ({
  ...from,
  status: 'cancelled',
  cancel_note: 'Already started backtests keep their own terminal states; cancelling is not a rollback.',
})

/** One preview state: a task, the route it is read at, and its legal next steps. */
export type PreviewState = {
  id: string
  label: string
  path: string
  task: ResearchTask
  /** What this state's homepage list contains; absent means just this task. */
  recent?: ResearchTask[]
  /** What an accepted confirm / retry / create returns, and the conclusion an interpret publishes. */
  confirmed?: ResearchTask
  retried?: ResearchTask
  created?: ResearchTask
  concluded?: ResearchTask
  /** The refusal the conclusion step records, if this state previews a failure. */
  interpretError?: { code: string; message: string }
  /**
   * True when this state's retry re-dispatches the backtest instead of resuming a
   * model step, so the preview can count a real dispatch for it.
   */
  retryDispatchesBacktest?: boolean
}

/** Scripted: the observed spec caught while the host was still drafting it. */
const drafting: ResearchTask = {
  ...observed, status: 'drafting', spec_draft: null, spec_confirmed: [], jobs: [],
  usage: proposed, spent_calls: [1], operation_kind: 'propose',
}

/** Scripted: the observed spec offered for confirmation, with no run yet. */
const awaiting: ResearchTask = {
  ...observed, status: 'awaiting_confirmation', spec_draft: spec, spec_confirmed: [], jobs: [],
  usage: proposed, operation_kind: null,
}

/** Scripted: the observed spec confirmed and handed to the engine as one run. */
const running: ResearchTask = {
  ...observed, status: 'running', usage: dispatched, operation_kind: 'start_backtest',
}

/** Scripted: a failed proposal. Retrying resumes that step; nothing is re-dispatched. */
const failed: ResearchTask = {
  ...observed, status: 'failed', spec_draft: null, spec_confirmed: [], jobs: [], usage: proposed,
  operation_kind: 'propose',
  error: { step: 'propose', code: 'model_not_configured', message: 'no usable model provider configuration exists' },
}

/** Scripted: a dispatched submission that was never observed. Nothing may be re-sent. */
const unknown: ResearchTask = {
  ...observed, status: 'failed', usage: dispatched, operation_kind: 'start_backtest',
  error: { step: 'start_backtest', code: 'job_outcome_unknown', message: 'the dispatched submission was never confirmed' },
  last_model: { model: 'deepseek-flash', at: OBSERVED.updated_at, outcome: 'unknown_after_dispatch' },
  backtest_reservation: { kind: 'backtest', seq: 1, status: 'unknown', at: OBSERVED.updated_at },
}

/** Scripted: the same run stopped before dispatch, so a retry does dispatch one. */
const failedBeforeDispatch: ResearchTask = {
  ...observed, status: 'failed', spec_draft: null, spec_confirmed: [], jobs: [], usage: proposed,
  operation_kind: 'start_backtest',
  error: { step: 'start_backtest', code: 'interrupted_before_dispatch', message: 'the attempt was interrupted before the submission was dispatched' },
}

/** Scripted: the host recorded the idea as not executable on the registered surface. */
const unsupported: ResearchTask = {
  ...observed, status: 'unsupported', spec_draft: null, spec_confirmed: [], jobs: [], usage: proposed,
  gap: 'The registered scenario window has four bars: too few for the requested entry delay.',
}

const withId = (task: ResearchTask, id: string): ResearchTask => ({ ...task, task_id: id })

export const STATES: PreviewState[] = [
  {
    id: 'home-empty', label: 'Homepage · no task yet', path: '/research',
    task: withId(drafting, 'scripted-task-home'), recent: [],
    created: withId(drafting, 'scripted-task-created'),
  },
  {
    id: 'home-recent', label: 'Homepage · recent tasks', path: '/research',
    task: awaiting,
    recent: [
      withId({ ...drafting, raw_idea: 'Draft a delayed entry on the registered window' }, 'scripted-task-recent'),
      awaiting,
      concluded,
    ],
    created: withId(drafting, 'scripted-task-created'),
  },
  { id: 'drafting', label: 'Drafting', path: '/research/scripted-task-drafting', task: withId(drafting, 'scripted-task-drafting'), retried: awaiting },
  {
    id: 'awaiting', label: 'Awaiting confirmation', path: '/research/scripted-task-awaiting',
    task: withId(awaiting, 'scripted-task-awaiting'), confirmed: running, retried: awaiting,
  },
  {
    id: 'running', label: 'Running one backtest', path: '/research/scripted-task-running',
    // A run that succeeds is not concluded by itself: this state's own conclusion
    // is published only when the reader asks for it.
    task: withId(running, 'scripted-task-running'), confirmed: observed, concluded,
  },
  {
    id: 'succeeded', label: 'Succeeded · conclusion published', path: '/research/scripted-task-succeeded',
    task: withId(concluded, 'scripted-task-succeeded'), retried: awaiting,
  },
  {
    id: 'conclusion-pending', label: 'Succeeded · no conclusion drawn yet', path: '/research/scripted-task-conclusion-pending',
    task: withId(observed, 'scripted-task-conclusion-pending'), concluded, retried: awaiting,
  },
  {
    id: 'interpretation-error', label: 'Succeeded · conclusion step failed', path: '/research/scripted-task-interpretation-error',
    task: withId({
      ...observed,
      interpretation_error: { step: 'interpret', code: 'model_outcome_unknown', message: 'the conclusion call left this process and was never observed' },
    }, 'scripted-task-interpretation-error'),
    concluded, retried: awaiting,
  },
  {
    id: 'failed', label: 'Failed · retryable model step', path: '/research/scripted-task-failed',
    task: withId(failed, 'scripted-task-failed'), retried: awaiting,
  },
  {
    id: 'failed-dispatch', label: 'Failed · retryable backtest dispatch', path: '/research/scripted-task-failed-dispatch',
    task: withId(failedBeforeDispatch, 'scripted-task-failed-dispatch'), retried: running,
    retryDispatchesBacktest: true,
  },
  // No retry is offered for this one: the submission left this process, so the
  // preview must not suggest a re-send is available.
  { id: 'failed-unknown', label: 'Failed · outcome unknown', path: '/research/scripted-task-unknown', task: withId(unknown, 'scripted-task-unknown') },
  { id: 'cancelled', label: 'Cancelled', path: '/research/scripted-task-cancelled', task: cancelTask(withId(awaiting, 'scripted-task-cancelled')) },
  { id: 'unsupported', label: 'Unsupported idea', path: '/research/scripted-task-unsupported', task: withId(unsupported, 'scripted-task-unsupported') },
  { id: 'backtests', label: 'Backtests · validation form', path: '/backtests', task: observed },
  { id: 'backtest', label: 'Backtest result · report, limits, curve', path: `/backtests/${OBSERVED.job_id}`, task: concluded },
]
