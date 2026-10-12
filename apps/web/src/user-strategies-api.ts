/**
 * QUINE user-strategy contract.
 *
 * These types mirror the durable documents served by the installed engine
 * (src/ea/web/user_strategies.py). One user strategy is a saved intent — or, once
 * the user explicitly binds a scenario, an executable draft — plus the
 * record of what was done to it: the AI calls the user approved, the typed
 * proposals, the change history, and the frozen experiments confirmed from that
 * draft. This client derives nothing: it never invents a proposal, an outcome, a
 * measured cost or a started run, and every model sentence it displays is labelled
 * as inference rather than evidence.
 *
 * The criterion vocabulary is the report's own (see research-api): the user picks
 * one allowlisted report field and the sign they expect, and the host freezes it.
 */

import type { ReportCriterion } from './research-api'

/**
 * One validated draft revision: exactly what an experiment would execute. A
 * record written before intents existed carries no `kind` at all, so the
 * discriminator is optional here and absent means executable.
 */
export type DraftContract = {
  draft_revision: number
  scenario_id: string
  source: string
  fields: { path: string; kind: string; json_type: string; current: string | number;
    minimum: string | number | null; maximum: string | number | null; editable: boolean }[]
}
export type SaveDraftRequest = {
  operation_id: string
  expected_revision: number
  values: Record<string, string | number>
}

export type UserStrategyDraft = {
  kind?: 'strategy'
  revision: number
  strategy_id: string
  venue: string
  symbol: string
  scenario_id: string
  dataset_id: string | null
  source_sha256: string | null
  initial_cash: string
  parameters: Record<string, string | number>
  criterion: ReportCriterion | null
}

/**
 * One revision of a saved intent: what a strategy is before a scenario is bound.
 * It carries a revision and nothing else — no strategy, symbol, funding or
 * parameter for this client to fill in with a guess, and never a default
 * scenario. It cannot be previewed or run.
 */
export type UserStrategyIntentDraft = {
  kind: 'intent'
  revision: number
  scenario_id: null
}

/** One draft revision of either kind, told apart by the discriminator alone. */
export type UserStrategyDraftDocument = UserStrategyDraft | UserStrategyIntentDraft

/**
 * An intent is a draft of its own kind, and nothing about it is executable. The
 * missing discriminator is the pre-intent encoding, which was executable.
 */
export function isIntentDraft(draft: UserStrategyDraftDocument): draft is UserStrategyIntentDraft {
  return draft.kind === 'intent'
}

/**
 * The route this workspace would use, exactly as the host projects it. Never a
 * credential and never a resolution value: provider, model and endpoint only.
 */
export type UserStrategyRoute = {
  provider: string | null
  model: string | null
  endpoint: string | null
}

export type UserStrategyCallStatus =
  | 'pending'
  | 'dispatched'
  | 'observed'
  | 'refused'
  | 'unconfigured'
  | 'unknown'
  | 'interrupted_before_dispatch'

/**
 * One frozen run a conversation was grounded in, exactly as the host read and
 * verified it when that call was made. Every field is host-verified provenance:
 * the run's own frozen revision and criterion, the digests it was admitted and
 * published under, its admitted parameters and assumptions, the allowlisted report
 * facts with the report's own currency, the host's own reading of the criterion,
 * and the report's recorded limitations. A value this host could not read is
 * absent here rather than guessed — never zero.
 */
export type UserStrategyEvidenceRun = {
  job_id: string
  launch_intent_id: string | null
  draft_revision: number | null
  criterion: ReportCriterion | null
  run_id: string | null
  report_sha256: string | null
  input_sha256: string | null
  record_count: number | null
  parameters: Record<string, string | number>
  /** The admitted, normalized blocks: strategy, execution, risk, funding, instrument, data. */
  assumptions: Record<string, unknown>
  facts: { path: string; value: string | number | null; currency: string | null }[]
  reading: { outcome: string | null; inference: string | null }
  limitations: string[]
}

/**
 * One recorded model call. `input_tokens`/`output_tokens` are null — not zero —
 * when the provider never reported usage, and `cost` is unavailable without a
 * price source, so nothing here can be read as a measured charge. `evidence` is
 * present only on a call the user grounded in selected frozen runs, and it is the
 * only thing such a call was told about them.
 */
export type UserStrategyCall = {
  call_id: string
  base_revision: number
  status: UserStrategyCallStatus
  route: UserStrategyRoute
  approved_budget: { max_model_calls: number; max_backtests: number }
  reserved_at: string
  message: string | null
  model: string | null
  input_tokens: number | null
  output_tokens: number | null
  usage_available: boolean
  cost?: { amount: string | null; available: boolean; reason: string }
  evidence?: UserStrategyEvidenceRun[]
}

/** One change to one field, with the provenance that field was written with. */
export type UserStrategyChangeItem = {
  path: string
  before: string | number | null
  after: string | number
  reason: string
  /** Model-supplied references for this field. Claims, never verified facts. */
  evidence?: string[]
  proposer?: string
}

export type UserStrategyMessage = {
  message_id: string
  role: 'user' | 'model'
  kind: 'message' | 'proposal'
  text: string
  base_revision: number
  at: string
  call_id: string
  proposal_id: string | null
}

/** One typed field change: a registered parameter path, with its before value. */
export type UserStrategyProposalItem = {
  path: string
  before: string | number | null
  after: string | number
  reason: string
  /** Model-supplied references. They are claims, never host-verified facts. */
  evidence: string[]
  proposer: string
}

export type UserStrategyProposalStatus = 'open' | 'applied' | 'rejected' | 'superseded'

export type UserStrategyProposal = {
  proposal_id: string
  call_id: string
  base_revision: number
  status: UserStrategyProposalStatus
  created_at: string
  items: UserStrategyProposalItem[]
  applied_paths: string[]
  stale_paths: string[]
}

/** One applied, undone or bound revision, exactly as it was written. */
export type UserStrategyChange = {
  operation_id: string
  kind: 'apply' | 'undo' | 'bind' | 'select-dataset' | 'manual'
  proposal_id: string | null
  target_operation_id: string | null
  from_revision: number
  to_revision: number
  applied_paths: string[]
  stale_paths: string[]
  items: UserStrategyChangeItem[]
  evidence: string[]
  proposer: string
  at: string
  /** A binding names the scenario it bound; no other kind of change carries one. */
  scenario_id?: string | null
  /** A dataset selection names the dataset it selected, and nothing else does. */
  dataset_id?: string | null
  criterion?: ReportCriterion | null
}

/**
 * The one immutable binding request this strategy was formed by. It is kept on
 * the record itself, so replaying the same operation still resolves to itself
 * after the change ledger has moved on. Nothing else is ever read from it.
 */
export type UserStrategyBinding = {
  operation_id: string
  request: {
    action: 'bind'
    base_revision: number
    scenario_id: string
    criterion: ReportCriterion | null
  }
  request_sha256: string
  scenario_id: string
  from_revision: number
  to_revision: number
  bound_at: string
}

/**
 * The one most recent explicit dataset selection this strategy was formed by, kept
 * on the record itself for the same reason the binding is: replaying the same
 * operation still resolves to itself after the change ledger has moved on.
 */
export type UserStrategyDataset = {
  operation_id: string
  request: {
    action: 'select-dataset'
    base_revision: number
    dataset_id: string
  }
  request_sha256: string
  dataset_id: string
  from_revision: number
  to_revision: number
  selected_at: string
}

export type UserStrategyExperimentStatus =
  | 'pending'
  | 'created'
  | 'failed'
  | 'unknown'
  | 'interrupted_before_dispatch'
  // The states of an experiment the Rust core owns. `confirmed`, `running` and
  // `stopping` are the core's own progress; `unconfirmed` means the record exists
  // here but the core never answered it; `cancelled` is a stop the core accepted.
  | 'confirmed'
  | 'running'
  | 'stopping'
  | 'cancelled'
  | 'unconfirmed'

/** One host-verified economic reading of a core-registered report. */
export type UserStrategyExperimentFact = { path: string; value: string | number | null }

/**
 * The immutable request one launch intent froze: the draft revision, the exact
 * admitted input identity it was confirmed against, and the exact budget. The
 * same intent always resolves to this payload, never to today's draft.
 */
export type UserStrategyExperimentPayload = {
  draft_revision: number
  scenario_id: string
  parameters: Record<string, string | number>
  preview_identity?: string
  input_sha256?: string | null
  input_identity?: Record<string, unknown>
  approved_budget?: ApprovedBudget
  /**
   * The admitted, normalized blocks the preview showed — strategy, execution,
   * risk, funding, instrument and data — kept exactly as the engine rendered
   * them, so two frozen runs can be compared on the assumptions they really ran
   * under rather than on today's scenario.
   */
  normalized?: Record<string, unknown>
}

/**
 * One launch intent and the experiment it froze. The payload it was confirmed
 * with is retained on the record, so the same intent always resolves to the same
 * run even after the draft moves on.
 */
export type UserStrategyExperiment = {
  launch_intent_id: string
  request_id: string
  payload_sha256: string
  payload: UserStrategyExperimentPayload
  task_id: string | null
  job_id: string | null
  status: UserStrategyExperimentStatus
  message?: string | null
  created_at: string
  /**
   * Which owner holds this run. Absent means the original in-process owner; `quine`
   * means the Rust core, and then the fields below are rebuildable read projections
   * of that owner's own record — never a second authority.
   */
  owner?: 'quine' | null
  /** The core's own last bounded progress phase while the run is live. */
  phase?: string | null
  /** Whether the core registered a success *and* its official bytes are readable. */
  report_ready?: boolean
  report_sha256?: string | null
  /** The host-verified readings of the registered report, exactly as stated. */
  facts?: UserStrategyExperimentFact[] | null
  report_schema?: string | null
  attempt_id?: string | null
  frozen_sha256?: string | null
}

export type UserStrategy = {
  schema: 'ea.user-strategy.v1'
  user_strategy_id: string
  created_at: string
  updated_at: string
  name: string
  intent: string
  /** The head revision: an executable draft, or a saved intent with no scenario. */
  draft: UserStrategyDraftDocument
  history: UserStrategyDraftDocument[]
  messages: UserStrategyMessage[]
  calls: UserStrategyCall[]
  proposals: UserStrategyProposal[]
  changes: UserStrategyChange[]
  experiments: UserStrategyExperiment[]
  /** The one immutable binding that formed this strategy, if it has one. */
  binding?: UserStrategyBinding | null
  /** The most recent explicit dataset selection, if the user made one. */
  dataset?: UserStrategyDataset | null
}

/**
 * What one experiment would do, read from the live workspace: the draft revision,
 * the real admitted input of the scenario — its source identity, its normalized
 * cost, risk and strategy binding — and the budget this workspace grants. A
 * preview starts nothing, and `preview_identity` is what a confirmation carries
 * back: a confirmation whose preview no longer describes what would run is
 * refused instead of silently running something else.
 */
export type UserStrategyPreview = {
  draft_revision: number
  strategy_id: string
  scenario_id: string
  venue: string
  symbol: string
  initial_cash: string
  parameters: Record<string, string | number>
  record_count: number | null
  data: Record<string, unknown>
  input_identity: Record<string, unknown>
  input_sha256: string | null
  preview_identity: string
  normalized: Record<string, unknown>
  /** The scenario's real cost assumptions, exactly as admitted. */
  commission: unknown
  slippage: unknown
  latency: unknown
  criterion: ReportCriterion | null
  budget: { max_model_calls: number; max_backtests: number }
  /** Which owner a confirmation would reach: `quine` when the Rust core owns it. */
  owner?: 'quine' | null
}

export type ApprovedBudget = { max_model_calls: number; max_backtests: number }

/**
 * The criterion the creator states, in the report's own vocabulary: one
 * allowlisted field and the sign expected. The host validates it and the stored
 * result is a complete ReportCriterion.
 */
export type UserStrategyCriterionInput = { field: string; expectation: string }

/** A user-strategy request failure, carrying the host's own error code. */
export class UserStrategyRequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const payload = await response.json()
  if (!response.ok) {
    const detail = payload?.error ?? payload
    throw new UserStrategyRequestError(
      detail?.code ?? 'request_failed',
      detail?.message ?? 'User strategy request failed',
    )
  }
  return payload as T
}

const writeHeaders = { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }

const write = <T,>(path: string, body: unknown) =>
  apiRequest<T>(path, { method: 'POST', headers: writeHeaders, body: JSON.stringify(body) })

const base = (userStrategyId: string) =>
  `/api/user-strategies/${encodeURIComponent(userStrategyId)}`

export const userStrategiesApi = {
  async list(): Promise<UserStrategy[]> {
    return (await apiRequest<{ strategies: UserStrategy[] }>('/api/user-strategies')).strategies
  },
  /**
   * A save. With a scenario it is the executable revision 1 it always was; with
   * no scenario it saves an intent, which the host needs no catalog for. Either
   * way this creates no model call and starts no experiment.
   */
  create: (input: {
    name: string
    intent: string
    scenario_id?: string | null
    criterion?: UserStrategyCriterionInput | null
  }) => write<UserStrategy>('/api/user-strategies', input),
  get: (userStrategyId: string) => apiRequest<UserStrategy>(base(userStrategyId)),
  /**
   * One explicit choice of one scenario for one saved intent. The host validates
   * the scenario, forms the executable draft and advances the revision once.
   */
  bindScenario: (
    userStrategyId: string,
    input: {
      operation_id: string
      base_revision: number
      scenario_id: string
      criterion?: UserStrategyCriterionInput | null
    },
  ) => write<UserStrategy>(`${base(userStrategyId)}/bind-scenario`, input),
  /**
   * One explicit choice of one local dataset for one executable draft. The client
   * sends only the catalog identity it was shown: not a path and not a digest. The
   * host resolves it through its own registered root and controlled upload store,
   * binds the published version it finds there, validates the whole resulting
   * draft against that data, and advances the revision once — which is what makes
   * an experiment previewed before this selection no longer confirmable.
   */
  draftContract: (id: string) => apiRequest<DraftContract>(`${base(id)}/draft/contract`),
  saveDraft: (id: string, input: SaveDraftRequest) => write<UserStrategy>(`${base(id)}/draft`, input),
  selectDataset: (
    userStrategyId: string,
    input: { operation_id: string; base_revision: number; dataset_id: string },
  ) => write<UserStrategy>(`${base(userStrategyId)}/select-dataset`, input),
  /**
   * One approved turn. `evidence_job_ids` is absent for an ordinary conversation;
   * with it, the turn is grounded in one or two of this strategy's own frozen runs
   * — A first, then B — and the host verifies each of them before anything is
   * written or sent. Sending a message starts no experiment.
   */
  sendMessage: (
    userStrategyId: string,
    input: {
      call_id: string
      base_revision: number
      text: string
      approved_budget: ApprovedBudget
      evidence_job_ids?: string[]
    },
  ) => write<UserStrategy>(`${base(userStrategyId)}/messages`, input),
  applyProposal: (
    userStrategyId: string,
    proposalId: string,
    input: { operation_id: string; paths?: string[] | null },
  ) =>
    write<UserStrategy>(
      `${base(userStrategyId)}/proposals/${encodeURIComponent(proposalId)}/apply`,
      input,
    ),
  rejectProposal: (userStrategyId: string, proposalId: string) =>
    write<UserStrategy>(
      `${base(userStrategyId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
      {},
    ),
  reviseProposal: (
    userStrategyId: string,
    proposalId: string,
    input: { call_id: string; text?: string; approved_budget: ApprovedBudget },
  ) =>
    write<UserStrategy>(
      `${base(userStrategyId)}/proposals/${encodeURIComponent(proposalId)}/revise`,
      input,
    ),
  /**
   * Reverse one named application of the revision the caller is looking at: the
   * head revision and the application are both named, so a stale page cannot undo
   * whatever happens to be newest.
   */
  undo: (
    userStrategyId: string,
    input: { operation_id: string; head_revision: number; target_operation_id: string },
  ) => write<UserStrategy>(`${base(userStrategyId)}/undo`, input),
  previewExperiment: (userStrategyId: string) =>
    apiRequest<UserStrategyPreview>(`${base(userStrategyId)}/experiment/preview`),
  /** One confirmation, of exactly the input this preview identity named. */
  launchExperiment: (
    userStrategyId: string,
    input: {
      launch_intent_id: string
      base_revision: number
      preview_identity: string
      approved_budget: ApprovedBudget
    },
  ) => write<UserStrategy>(`${base(userStrategyId)}/experiments`, input),
  /**
   * Ask the run's owner to stop one confirmed experiment. Only the owner decides
   * what that means; this returns the strategy, not a claim that the run stopped.
   */
  cancelExperiment: (userStrategyId: string, launchIntentId: string) =>
    write<UserStrategy>(
      `${base(userStrategyId)}/experiments/${encodeURIComponent(launchIntentId)}/cancel`,
      {},
    ),
}
