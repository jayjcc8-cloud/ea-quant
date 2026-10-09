import { EventValidation } from './events'
import { CandidateCreate, CandidateDetail, CandidateHistory, type Candidate } from './candidates'
import { TradeAnalyticsPanel, type TradeAnalytics } from './trade-analytics'
import { ResearchTaskDetailRoute, ResearchTasks, ResearchSettings } from './research'
import { StrategiesWorkspace } from './user-strategies'
import { PaperStatus } from './paper-status'
import { ResourcesPage } from './resources'
import {
  userStrategiesApi,
  type ApprovedBudget as UserStrategyApprovedBudget,
  type UserStrategy,
  type UserStrategyExperimentPayload,
  type UserStrategyPreview,
} from './user-strategies-api'
import { radianApi } from './radian-api'
import { researchApi, type AvailabilityPolicy, type DeclarationBody, type DeclarationCatalog, type DeclarationVerdict, type DatasetEntry, type LocalStrategyCapability, type LocalStrategyRegisterRequest, type LocalStrategySlot, type ModelConfigurationStatus, type PluginBinding, type PluginCatalog, type PluginSelection, type RecipeResolution, type ResearchTask, type UploadDeclaration, type UploadPreviewResponse, type UploadPublishRequest, type UploadVersion } from './research-api'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { compareCanonicalDecimal, exactDelta } from './decimal'
import { EquityCurve, pathPercent, type EquityPath } from './equity-path'
// The user-strategy workspace's own stylesheet, imported where the routes that
// render it are declared, the way the entry module imports styles.css.
import './user-strategies.css'

type ParameterMap = Record<string, string | number>
type StrategySource = { kind: string; package_id: string; artifact_sha256: string }
type StrategyDescriptor = { display_name?: string; action_contract?: string; position_lifecycle?: string; strategy_id: string; strategy_version: number; research_visible: boolean; parameters: { name: string; type: 'integer' | 'decimal' }[] }
const parameterDefaults = (scenario?: ScenarioSummary): ParameterMap => Object.fromEntries((scenario?.strategy_parameters ?? []).map(p => [p.name, p.current_value ?? p.default ?? '']))
const jobParameters = (job: BacktestJob): ParameterMap => job.parameters ?? job.input_snapshot?.scenario.strategy.parameters ?? {}
const parameterText = (parameters: ParameterMap) => Object.entries(parameters).map(([name, value]) => `${name}: ${value}`).join(' · ')
function ParameterControls({ contracts, values, update, prefix = '' }: { contracts: StrategyParameterContract[]; values: ParameterMap; update: (name: string, value: string | number) => void; prefix?: string }) {
  return <div className="parameter-grid">{contracts.map(p => <div key={p.name}>
    <label htmlFor={`${prefix}${p.name}`}>{prefix}{p.name}</label>
    <input id={`${prefix}${p.name}`} type={p.type === 'integer' ? 'number' : 'text'} inputMode={p.type === 'integer' ? 'numeric' : p.type === 'price-rule-v1' ? 'text' : 'decimal'}
      min={p.minimum === null ? undefined : String(p.minimum)} max={p.maximum === null ? undefined : String(p.maximum)} step={p.type === 'integer' ? '1' : undefined}
      value={Number.isNaN(values[p.name]) ? '' : values[p.name] ?? ''}
      onChange={event => update(p.name, p.type === 'integer' && /^-?\d+$/.test(event.target.value) && Number.isSafeInteger(event.target.valueAsNumber) ? event.target.valueAsNumber : event.target.value)} />
    <small>{p.minimum === null ? '' : `Minimum ${p.minimum}`}{p.maximum === null ? '' : ` · Maximum ${p.maximum}`}</small>
  </div>)}</div>
}

type Dataset = {
  dataset_id: string; valid: boolean; source_sha256?: string; record_count?: number
  venue?: string; symbol?: string; replay_start_utc?: string; replay_end_utc?: string
  bar_count?: number; revision_count?: number; bar_durations_seconds?: string[]
  observed_gap_count?: number; largest_gap_seconds?: string | null; bar_start_utc?: string; bar_end_utc?: string
  error_code?: string; record_number?: number | null; field_name?: string | null; message?: string
  // The version evidence the host projects for a published or registered
  // version. A registered scenario's own data carries none of it, so the
  // resource directory reads these as the projection it is: present or absent,
  // never inferred from the id.
  origin?: 'registered' | 'upload'; capture_sha256?: string; data_sha256?: string
  availability_policy?: AvailabilityPolicy; version_evidence_digest?: string
}
type DatasetReference = { dataset_id?: string; source_sha256?: string }
export type InputIdentity = DatasetReference & { scenario_sha256: string; data_sha256: string; record_count: number }
export type StrategyParameterContract = {
  name: string; type: 'decimal' | 'integer' | 'price-rule-v1' | 'price-rule-v1' | 'price-rule-v1' | 'price-rule-v1'
  default: string | number | null; current_value: string | number | null
  minimum: string | number; maximum: string | number | null
}
export type BacktestParameters = {
  initial_cash: string
  strategy_source?: StrategySource
  strategy_parameters: ParameterMap | null
}
type ReplayData = { start_utc: string; end_utc: string; fingerprint: { sha256: string; record_count: number } }
export type ChronologicalHoldout = { schema: 'ea.chronological-holdout.v1'; validation_id: string; created_at: string; source_job_id: string; holdout_job_id: string }
type Commission = { policy: string; commission_bps: string }
type Slippage = { policy: string; slippage_bps: string }
type Latency = { policy: string; latency_ms: number }
const latencyLabel = (latency?: Latency | null) => `${latency?.latency_ms ?? 0} ms execution latency`
const slippageLabel = (slippage?: Slippage | null) => slippage ? `${slippage.slippage_bps} bps adverse slippage` : '0 bps slippage'
const commissionLabel = (commission?: Commission | null) => commission ? `${commission.policy} · ${commission.commission_bps} bps` : 'Legacy zero commission · 0 bps'

export type InputSnapshot = {
  schema: 'ea.local-web-input.v1' | 'ea.local-web-input.v2'; research_input?: Dataset; scenario_id: string; source_identity: InputIdentity; identity: InputIdentity
  scenario: {
    data?: ReplayData
    execution?: { policy: string; commission?: Commission | null; slippage?: Slippage | null; latency?: Latency | null }
    funding: { currency: string; initial_cash: string }
    strategy: { source?: StrategySource; id: string; version?: number; parameters?: ParameterMap; [key: string]: unknown }
    instrument: { venue: string; symbol: string }
  }
}
export type ScenarioSummary = {
  scenario_id: string; name: string; valid: boolean; input_identity?: InputIdentity
  summary?: { data?: ReplayData; commission?: Commission | null; slippage?: Slippage | null; latency?: Latency | null; strategy_id: string; venue: string; symbol: string; initial_cash: string; parameters?: ParameterMap; record_count: number; [key: string]: unknown }
  strategy_parameters?: StrategyParameterContract[]
  strategy_descriptor?: StrategyDescriptor
  normalized_input_identity?: InputIdentity
  error_code?: string; message?: string
}
/** The published dataset version one job recorded beside its accepted input. The
 * host serves it as an additive job field; a run written before it existed
 * carries none, and the catalog then says the version is unknown rather than
 * reading today's version back into a historical run. */
export type JobDatasetVersion = {
  dataset_id: string; schema?: string; kind?: string; version?: number
  capture_sha256?: string; data_sha256?: string; record_count?: number
  [key: string]: unknown
}
export type BacktestJob = {
  schema: string; job_id: string; request_id: string; scenario_id: string; input_identity: InputIdentity
  status: 'accepted' | 'running' | 'stopping' | 'stopped' | 'succeeded' | 'failed' | 'interrupted'; engine_run_id: string | null
  report_sha256: string | null; summary_sha256: string | null; error_code: string | null; message: string | null; report_ready: boolean
  equity_path_sha256?: string | null
  parameters?: ParameterMap; strategy_descriptor?: StrategyDescriptor
  created_at?: string; input_snapshot?: InputSnapshot; input_sha256?: string; attempt_id?: string | null
  /** The exact capability identity this run froze. Absent on older runs. */
  plugin_binding?: PluginBinding | null
  /** The published dataset version this run's input was admitted from. */
  dataset_version?: JobDatasetVersion | null
}
export type BatchMember = BacktestJob & { presentation_status: string }
export type ExperimentBatch = {
  schema: 'ea.local-web-batch.v1'; batch_id: string; created_at: string
  scenario_id: string; input_identity: InputIdentity; member_job_ids: string[]
  member_count: number; status: 'running' | 'complete'; members: BatchMember[]
}
export type BatchRequest = {
  scenario_id: string; input_identity: InputIdentity; initial_cash: string
  runs: { strategy_parameters: ParameterMap }[]
}
type Money = { amount: string; currency?: string }
type ReportBase = {
  run_id: string
  source?: { strategy: { id: string }; instrument: { venue: string; symbol: string } }
  scenario?: { strategy_id: string; instrument: { venue: string; symbol: string } }
  economics: {
    currency?: string; initial_funding?: Money; ending_cash: Money[]
    ending_positions: { quantity: string; venue: string; symbol: string }[]
    valuation: { price: string; position_value: string }; equity: Money; net_pnl: Money
    fees?: { amount: string; currency: string; count: number; rule: string }
    total_return: { value: string }; counts: { orders: number; fills: number }
  }
}
type RoundTripEconomics = ReportBase['economics'] & {
  position_state: string; position_outcome: 'FLAT_INITIAL' | 'OPEN_AT_END' | 'CLOSED'
  realized_pnl: Money; gross_unrealized_pnl: Money; entry_fee: string; exit_fee: string
  execution_legs: { role: 'entry' | 'exit'; outcome: string; fill: { quantity: string; price: string; occurred_at: string; fees: Money[] } | null }[]
}
export type BacktestReport = (ReportBase & { schema: 'ea.backtest-report.v1'; economics: ReportBase['economics'] & {
  execution: { order: { quantity: string; side: string } | null; fill: { quantity: string; price: string; side: string } | null }
}}) | (ReportBase & { schema: 'ea.backtest-report.v2'; economics: RoundTripEconomics })
 | (ReportBase & { schema: 'ea.backtest-report.v3'; economics: MultiRoundTripEconomics })

type MultiRoundTripEconomics = Omit<RoundTripEconomics, 'position_outcome' | 'entry_fee' | 'exit_fee'> & {
  position_outcome: 'FLAT_NO_TRADE' | 'FLAT_AFTER_TRADES' | 'OPEN_AT_END'
  completed_round_trips: number
  trades: { quantity: string; entry_settled_notional: string; exit_settled_notional: string;
    entry_fee: string; exit_fee: string; realized_pnl: string;
    entry_order_id: { owner_sequence: number }; exit_order_id: { owner_sequence: number };
    entry_fill_id: { owner_sequence: number }; exit_fill_id: { owner_sequence: number } }[]
  open_position: { quantity: string; entry_fee: string; last_valuation: string; gross_unrealized_pnl: string } | null
}
function MultiRoundTripExecution({ economics }: { economics: MultiRoundTripEconomics }) {
  return <>
    <div><dt>Position outcome</dt><dd>{economics.position_outcome}</dd></div>
    <div><dt>Completed round trips</dt><dd>{economics.completed_round_trips}</dd></div>
    <div className="trade-history"><dt>Complete trades</dt><dd><table><thead><tr><th>Trade</th><th>Entry order / fill</th><th>Exit order / fill</th><th>Quantity</th><th>Entry notional</th><th>Exit notional</th><th>Entry fee</th><th>Exit fee</th><th>Realized P&amp;L</th></tr></thead><tbody>
      {economics.trades.map((trade, i) => <tr key={trade.entry_order_id.owner_sequence}>
        <td>{i + 1}</td><td>{trade.entry_order_id.owner_sequence} / {trade.entry_fill_id.owner_sequence}</td><td>{trade.exit_order_id.owner_sequence} / {trade.exit_fill_id.owner_sequence}</td>
        <td>{trade.quantity}</td><td>{trade.entry_settled_notional}</td><td>{trade.exit_settled_notional}</td><td>{trade.entry_fee}</td><td>{trade.exit_fee}</td><td>{trade.realized_pnl}</td>
      </tr>)}
    </tbody></table></dd></div>
    <div><dt>Open position</dt><dd>{economics.open_position ? `${economics.open_position.quantity} · entry fee ${economics.open_position.entry_fee} · valuation ${economics.open_position.last_valuation}` : '—'}</dd></div>
    <div><dt>Realized P&amp;L</dt><dd>{economics.realized_pnl.amount}</dd></div>
    <div><dt>Gross unrealized P&amp;L</dt><dd>{economics.gross_unrealized_pnl.amount}</dd></div>
  </>
}

function RoundTripExecution({ economics }: { economics: RoundTripEconomics }) {
  return <>
    <div><dt>Position outcome</dt><dd>{economics.position_outcome}</dd></div>
    <div><dt>Position state</dt><dd>{economics.position_state}</dd></div>
    {(['entry', 'exit'] as const).map(role => {
      const leg = economics.execution_legs.find(item => item.role === role)
      return <div key={role}><dt>{role === 'entry' ? 'Entry' : 'Exit'}</dt><dd>{leg?.fill ? `${leg.fill.quantity} @ ${leg.fill.price} · ${leg.fill.occurred_at} · fee ${leg.fill.fees[0]?.amount ?? '0'}` : '—'}</dd></div>
    })}
    <div><dt>Realized P&amp;L</dt><dd>{economics.realized_pnl.amount}</dd></div>
    <div><dt>Gross unrealized P&amp;L</dt><dd>{economics.gross_unrealized_pnl.amount}</dd></div>
  </>
}
export type ApiAdapter = {
  listCandidates?(): Promise<Candidate[]>
  getCandidate?(id: string): Promise<Candidate>
  prepareSimulation?(id: string): Promise<import('./candidates').SimulationPreparation>
  createCandidate?(validationId: string): Promise<Candidate>
  decideCandidate?(id: string, outcome: string, reason: string): Promise<Candidate>
  holdoutScenarios(sourceJobId: string): Promise<ScenarioSummary[]>
  listDatasets?(): Promise<Dataset[]>
  createHoldout(request: { source_job_id: string; scenario_id: string; input_identity?: InputIdentity; dataset_id?: string; source_sha256?: string }): Promise<ChronologicalHoldout>
  getHoldout(validationId: string): Promise<ChronologicalHoldout>
  listHoldouts(): Promise<ChronologicalHoldout[]>
  listScenarios(): Promise<ScenarioSummary[]>
  validateScenario(scenarioId: string, parameters: BacktestParameters, dataset?: DatasetReference): Promise<ScenarioSummary>
  createBacktest(request: { scenario_id: string; input_identity: InputIdentity; parameters: BacktestParameters; request_id: string }): Promise<BacktestJob>
  createBatch(request: BatchRequest): Promise<ExperimentBatch>
  getBatch(batchId: string): Promise<ExperimentBatch>
  listBacktests(): Promise<BacktestJob[]>
  getBacktest(jobId: string): Promise<BacktestJob>
  /**
   * Ask the controller to stop one live run. The answer is the job's own record,
   * so a live run reads back as `stopping` and only its supervisor records the
   * confirmed `stopped`; a run that already finished is returned unchanged.
   */
  stopBacktest?(jobId: string): Promise<BacktestJob>
  getTradeAnalytics?(jobId: string): Promise<TradeAnalytics>
  getReport(jobId: string): Promise<BacktestReport>
  getEquityPath(jobId: string): Promise<EquityPath>
  artifactUrl(jobId: string, name: 'report.json' | 'summary.txt' | 'equity-path.json'): string
  settingsStatus?(): Promise<ModelConfigurationStatus>
  listResearchTasks?(): Promise<ResearchTask[]>
  createResearchTask?(idea: string, selection?: PluginSelection): Promise<ResearchTask>
  getResearchTask?(taskId: string): Promise<ResearchTask>
  editResearchSpec?(taskId: string, fields: Record<string, unknown>): Promise<ResearchTask>
  confirmResearchTask?(taskId: string): Promise<ResearchTask>
  retryResearchTask?(taskId: string): Promise<ResearchTask>
  cancelResearchTask?(taskId: string): Promise<ResearchTask>
  /** The explicit conclusion step for one succeeded, verified run. */
  interpretResearchTask?(taskId: string): Promise<ResearchTask>
  pluginCatalog?(): Promise<PluginCatalog>
  resolveRecipe?(selection: PluginSelection): Promise<RecipeResolution>
  listDeclarations?(): Promise<DeclarationCatalog>
  validateDeclaration?(declaration: DeclarationBody): Promise<DeclarationVerdict>
  registerDeclaration?(declaration: DeclarationBody): Promise<DeclarationVerdict>
  previewUpload?(content: string, fileName?: string, declaration?: UploadDeclaration): Promise<UploadPreviewResponse>
  previewUploadFile?(contentBase64: string, declaration: UploadDeclaration, fileName?: string): Promise<UploadPreviewResponse>
  publishUpload?(request: UploadPublishRequest): Promise<UploadVersion & { datasets: DatasetEntry[] }>
  /** The workspace's one local strategy package slot, and the explicit decisions
   * that change it. Absent means this workspace offers no local packages. */
  localStrategySlot?(): Promise<LocalStrategySlot>
  inspectLocalStrategy?(contentBase64: string, manifest: Record<string, unknown>): Promise<LocalStrategyCapability>
  registerLocalStrategy?(request: LocalStrategyRegisterRequest): Promise<LocalStrategySlot>
  revokeLocalStrategy?(): Promise<LocalStrategySlot>
  /** One durable user strategy: its intent or draft, its approved AI calls, its
   * frozen runs. Every member is optional so an existing injected adapter keeps
   * working, and a workspace that serves none of them renders the honest empty
   * state instead. `scenario_id` is absent when a save is a bare intent, and the
   * binding below is the one explicit choice that later gives it a scenario. */
  listUserStrategies?(): Promise<UserStrategy[]>
  createUserStrategy?(input: {
    name: string
    intent: string
    scenario_id?: string | null
    criterion?: { field: string; expectation: string } | null
  }): Promise<UserStrategy>
  getUserStrategy?(userStrategyId: string): Promise<UserStrategy>
  bindUserStrategyScenario?(
    userStrategyId: string,
    input: {
      operation_id: string
      base_revision: number
      scenario_id: string
      criterion?: { field: string; expectation: string } | null
    },
  ): Promise<UserStrategy>
  /** One explicit dataset choice for one executable draft. The client names only
   * a catalog identity: the host resolves and binds the version it finds. */
  selectUserStrategyDataset?(
    userStrategyId: string,
    input: { operation_id: string; base_revision: number; dataset_id: string },
  ): Promise<UserStrategy>
  sendUserStrategyMessage?(
    userStrategyId: string,
    input: {
      call_id: string
      base_revision: number
      text: string
      approved_budget: UserStrategyApprovedBudget
      /** One or two of this strategy's own frozen runs, ordered A then B. Absent
       * is an ordinary turn: the host verifies each id and refuses the selection
       * whole rather than grounding a conversation in a run the user did not
       * select. Sending a message never starts an experiment. */
      evidence_job_ids?: string[]
    },
  ): Promise<UserStrategy>
  applyUserStrategyProposal?(
    userStrategyId: string,
    proposalId: string,
    input: { operation_id: string; paths?: string[] | null },
  ): Promise<UserStrategy>
  rejectUserStrategyProposal?(userStrategyId: string, proposalId: string): Promise<UserStrategy>
  reviseUserStrategyProposal?(
    userStrategyId: string,
    proposalId: string,
    input: { call_id: string; text?: string; approved_budget: UserStrategyApprovedBudget },
  ): Promise<UserStrategy>
  undoUserStrategyChange?(
    userStrategyId: string,
    input: { operation_id: string; head_revision: number; target_operation_id: string },
  ): Promise<UserStrategy>
  previewUserStrategyExperiment?(userStrategyId: string): Promise<UserStrategyPreview>
  launchUserStrategyExperiment?(
    userStrategyId: string,
    input: {
      launch_intent_id: string
      base_revision: number
      preview_identity: string
      approved_budget: UserStrategyApprovedBudget
    },
  ): Promise<UserStrategy>
  /** Ask the run's owner to stop one confirmed experiment. Absent means this
   * workspace cannot ask for a stop from here. */
  cancelUserStrategyExperiment?(
    userStrategyId: string,
    launchIntentId: string,
  ): Promise<UserStrategy>
}

async function loadTradeAnalytics(api: ApiAdapter, job: BacktestJob): Promise<TradeAnalytics | null> {
  if (!api.getTradeAnalytics || job.status !== 'succeeded' || !job.report_ready) return null
  try {
    const value = await api.getTradeAnalytics(job.job_id)
    if (value.schema !== 'ea.trade-analytics.v1' || value.run_id !== job.engine_run_id || value.report_sha256 !== job.report_sha256) return null
    return value
  } catch { return null }
}

class ApiFailure extends Error {
  constructor(readonly code: string, message: string) { super(message) }
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const payload = await response.json()
  if (!response.ok) {
    const detail = payload?.error
    throw new ApiFailure(detail?.code ?? 'request_failed', detail?.message ?? 'Request failed')
  }
  return payload as T
}

const browserApi: ApiAdapter = {
  async listCandidates() { return (await apiRequest<{ candidates: Candidate[] }>('/api/candidates')).candidates },
  getCandidate: id => apiRequest(`/api/candidates/${id}`),
  prepareSimulation: id => apiRequest(`/api/candidates/${id}/simulation-preparation`),
  createCandidate: validationId => apiRequest('/api/candidates', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({ validation_id: validationId }) }),
  decideCandidate: (id, outcome, reason) => apiRequest(`/api/candidates/${id}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({ outcome, reason }) }),
  async listDatasets() { return (await apiRequest<{ datasets: Dataset[] }>('/api/datasets')).datasets },
  async holdoutScenarios(jobId) { return (await apiRequest<{ scenarios: ScenarioSummary[] }>(`/api/backtests/${encodeURIComponent(jobId)}/holdout-scenarios`)).scenarios },
  createHoldout(request) { return apiRequest('/api/holdouts', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify(request) }) },
  getHoldout(id) { return apiRequest(`/api/holdouts/${encodeURIComponent(id)}`) },
  async listHoldouts() { return (await apiRequest<{ holdouts: ChronologicalHoldout[] }>('/api/holdouts')).holdouts },
  async listScenarios() { return (await apiRequest<{ scenarios: ScenarioSummary[] }>('/api/scenarios')).scenarios },
  validateScenario(scenarioId, parameters, dataset) {
    return apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({ parameters, ...dataset }),
    })
  },
  createBacktest(request) {
    return apiRequest('/api/backtests', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify(request),
    })
  },
  createBatch(request) {
    return apiRequest('/api/batches', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify(request),
    })
  },
  getBatch(batchId) { return apiRequest(`/api/batches/${encodeURIComponent(batchId)}`) },
  async listBacktests() { return (await apiRequest<{ jobs: BacktestJob[] }>('/api/backtests')).jobs },
  getBacktest(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}`) },
  stopBacktest(jobId) {
    return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/stop`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({}),
    })
  },
  getTradeAnalytics(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/trade-analytics`) },
  getReport(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/report`) },
  getEquityPath(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/artifacts/equity-path.json`) },
  artifactUrl(jobId, name) { return `/api/backtests/${encodeURIComponent(jobId)}/artifacts/${name}` },
  settingsStatus: () => radianApi.settingsStatus(),
  listResearchTasks: () => researchApi.listTasks(),
  createResearchTask: (idea, selection) => researchApi.createTask(idea, selection),
  getResearchTask: taskId => researchApi.getTask(taskId),
  editResearchSpec: (taskId, fields) => researchApi.editSpec(taskId, fields),
  confirmResearchTask: taskId => researchApi.confirmTask(taskId),
  retryResearchTask: taskId => researchApi.retryTask(taskId),
  cancelResearchTask: taskId => researchApi.cancelTask(taskId),
  interpretResearchTask: taskId => researchApi.interpretTask(taskId),
  pluginCatalog: () => researchApi.pluginCatalog(),
  resolveRecipe: selection => researchApi.resolveRecipe(selection),
  listDeclarations: () => researchApi.listDeclarations(),
  validateDeclaration: declaration => researchApi.validateDeclaration(declaration),
  registerDeclaration: declaration => researchApi.registerDeclaration(declaration),
  previewUpload: (content, fileName, declaration) => researchApi.previewUpload(content, fileName, declaration),
  previewUploadFile: (contentBase64, declaration, fileName) => researchApi.previewUploadFile(contentBase64, declaration, fileName),
  publishUpload: request => researchApi.publishUpload(request),
  localStrategySlot: () => researchApi.localStrategySlot(),
  inspectLocalStrategy: (contentBase64, manifest) => researchApi.inspectLocalStrategy(contentBase64, manifest),
  registerLocalStrategy: request => researchApi.registerLocalStrategy(request),
  revokeLocalStrategy: () => researchApi.revokeLocalStrategy(),
  listUserStrategies: () => userStrategiesApi.list(),
  createUserStrategy: input => userStrategiesApi.create(input),
  getUserStrategy: userStrategyId => userStrategiesApi.get(userStrategyId),
  bindUserStrategyScenario: (userStrategyId, input) =>
    userStrategiesApi.bindScenario(userStrategyId, input),
  selectUserStrategyDataset: (userStrategyId, input) =>
    userStrategiesApi.selectDataset(userStrategyId, input),
  sendUserStrategyMessage: (userStrategyId, input) =>
    userStrategiesApi.sendMessage(userStrategyId, input),
  applyUserStrategyProposal: (userStrategyId, proposalId, input) =>
    userStrategiesApi.applyProposal(userStrategyId, proposalId, input),
  rejectUserStrategyProposal: (userStrategyId, proposalId) =>
    userStrategiesApi.rejectProposal(userStrategyId, proposalId),
  reviseUserStrategyProposal: (userStrategyId, proposalId, input) =>
    userStrategiesApi.reviseProposal(userStrategyId, proposalId, input),
  undoUserStrategyChange: (userStrategyId, input) =>
    userStrategiesApi.undo(userStrategyId, input),
  previewUserStrategyExperiment: userStrategyId =>
    userStrategiesApi.previewExperiment(userStrategyId),
  launchUserStrategyExperiment: (userStrategyId, input) =>
    userStrategiesApi.launchExperiment(userStrategyId, input),
  cancelUserStrategyExperiment: (userStrategyId, launchIntentId) =>
    userStrategiesApi.cancelExperiment(userStrategyId, launchIntentId),
}

async function loadPath(api: ApiAdapter, job: BacktestJob): Promise<EquityPath | null> {
  if (job.status !== 'succeeded' || !job.equity_path_sha256) return null
  try {
    const path = await api.getEquityPath(job.job_id)
    if (path.schema !== (job.schema === 'ea.local-web-job.v5' ? 'ea.backtest-equity-path.v3' : job.schema === 'ea.local-web-job.v4' ? 'ea.backtest-equity-path.v2' : 'ea.backtest-equity-path.v1') || path.run_id !== job.engine_run_id || path.report_sha256 !== job.report_sha256) return null
    return path
  } catch { return null }
}
function pathUnavailable(job: BacktestJob) {
  return ['ea.local-web-job.v3', 'ea.local-web-job.v4', 'ea.local-web-job.v5'].includes(job.schema ?? '') ? 'Path analysis unavailable.' : 'Path analysis unavailable for this legacy run.'
}

function DatasetSelector({ api, value, change, instrument }: { api: ApiAdapter; value: DatasetReference; change: (value: DatasetReference) => void; instrument?: { venue: string; symbol: string } }) {
  const [items, setItems] = useState<Dataset[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api.listDatasets?.().then(items => { if (active) setItems(items) }).catch(() => { if (active) setError('Dataset catalog unavailable') })
    return () => { active = false }
  }, [api])
  const selected = items.find(item => item.dataset_id === value.dataset_id)
  const incompatible = (item: Dataset) => Boolean(instrument && item.venue && item.symbol && (item.venue !== instrument.venue || item.symbol !== instrument.symbol))
  const diagnostic = (item: Dataset) => `${item.error_code ?? item.message ?? 'Unavailable'}${item.record_number ? ` · CSV record ${item.record_number}` : ''}${item.field_name ? ` · field ${item.field_name}` : ''}`
  if (!items.length && !value.dataset_id && !error) return null
  return <div><label htmlFor="research-dataset">Research dataset</label>
    <select id="research-dataset" value={value.dataset_id ?? ''} onChange={event => {
      const item = items.find(item => item.dataset_id === event.target.value)
      change(item ? { dataset_id: item.dataset_id, source_sha256: item.source_sha256 } : {})
    }}><option value="">Registered scenario data</option>
      {value.dataset_id && !items.some(item => item.dataset_id === value.dataset_id) && <option value={value.dataset_id}>{value.dataset_id} · unavailable</option>}
      {items.map(item => <option key={item.dataset_id} value={item.dataset_id} disabled={!item.valid || incompatible(item)}>{item.dataset_id}{item.valid ? (incompatible(item) ? ' · incompatible instrument' : '') : ' · invalid'}</option>)}
    </select>
    {selected?.valid && <section aria-label="Dataset quality" className="dataset-quality">
      <p><strong>{selected.venue}:{selected.symbol}</strong> · Strict format validated</p>
      <dl className="summary-list">
        <div><dt>Records / bars / revisions</dt><dd>{selected.record_count ?? 'Unavailable'} / {selected.bar_count ?? 'Unavailable'} / {selected.revision_count ?? 'Unavailable'}</dd></div>
        <div><dt>Observed bar durations</dt><dd>{selected.bar_durations_seconds?.map(value => `${value}s`).join(', ') ?? 'Unavailable'}</dd></div>
        <div><dt>Bar coverage (UTC)</dt><dd>{selected.bar_start_utc ?? 'Unavailable'} — {selected.bar_end_utc ?? 'Unavailable'}</dd></div>
        <div><dt>Replay availability (UTC)</dt><dd>{selected.replay_start_utc} — {selected.replay_end_utc}</dd></div>
        <div><dt>Observed gaps</dt><dd>{selected.observed_gap_count ?? 'Unavailable'}{selected.largest_gap_seconds ? ` · largest ${selected.largest_gap_seconds}s` : ''}</dd></div>
      </dl>
      <p className="muted">Gaps describe uncovered time between captured bars, not missing exchange sessions. Bars are source-scoped; revisions count records with revision greater than zero.</p>
      {incompatible(selected) && <p role="alert" className="notice error">Dataset instrument {selected.venue}:{selected.symbol} is incompatible with scenario {instrument?.venue}:{instrument?.symbol}. Select compatible data.</p>}
      {value.source_sha256 && value.source_sha256 !== selected.source_sha256 && <p role="alert" className="notice error">Dataset content changed since selection. Explicitly select and validate it again.</p>}
    </section>}
    {value.dataset_id && !selected && <p className="notice error">Selected dataset is unavailable. Historical reports remain readable.</p>}
    {items.some(item => !item.valid) && <details><summary>Invalid datasets</summary><ul>{items.filter(item => !item.valid).map(item => <li key={item.dataset_id}><strong>{item.dataset_id}</strong>: {diagnostic(item)}</li>)}</ul></details>}
    {error && <p className="notice error">{error}</p>}</div>
}

function PageTitle({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{status && <span className={`status status-${status}`}>{status}</span>}</header>
}

/**
 * The QUINE working-name mark: one restrained black glyph, drawn rather than
 * imported so the shell carries no asset dependency.
 */
function QuineMark() {
  return <svg className="brand-mark" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">
    <rect x="1.7" y="1.7" width="20.6" height="20.6" rx="5.6" fill="none" stroke="currentColor" strokeWidth="2.3" />
    <circle cx="12" cy="11.6" r="4.4" fill="none" stroke="currentColor" strokeWidth="2.3" />
    <path d="M14.7 14.5 17.8 17.6" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
  </svg>
}

/** The compact outline glyphs the sidebar navigation uses. */
const NAV_ICONS = {
  strategies: 'M4 4h16v16H4zM8 8h8M8 12h8M8 16h5',
  runs: 'M7 4.5 19 12 7 19.5z',
  backtests: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  settings: 'M12 9.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2ZM12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6',
} as const

function NavIcon({ name }: { name: keyof typeof NAV_ICONS }) {
  return <svg className="nav-icon" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
    <path d={NAV_ICONS[name]} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
}

function Shell({ api }: { api: ApiAdapter }) {
  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/strategies"><QuineMark /><span>QUINE</span></Link>
      <p className="workspace">工作名 · 本地研究环境</p>
      {/* Every link is a real route. Capabilities this workspace does not serve —
          external accounts, live or paper connectivity — are stated as unavailable
          in the footer instead of being offered as navigation. */}
      <nav aria-label="主导航">
        {/* The first three stay the top-level destinations, 策略 first: 模拟观测 is the
            read-only local simulation snapshot, 回测实验 the research backtests, and
            they must not be read as one another. */}
        <NavLink to="/strategies"><NavIcon name="strategies" />策略</NavLink>
        <NavLink to="/runs"><NavIcon name="runs" />模拟观测</NavLink>
        <NavLink to="/backtests"><NavIcon name="backtests" />回测实验</NavLink>
        {/* Research and its validation surfaces share one plain group label; the
            resource directory keeps its own. */}
        <p className="nav-group">研究与验证</p>
        <NavLink className="nav-child" to="/research">研究任务</NavLink>
        <NavLink className="nav-child" to="/holdouts">留出验证</NavLink>
        <NavLink className="nav-child" to="/candidates">候选</NavLink>
        <NavLink className="nav-child" to="/events">事件校验</NavLink>
        <p className="nav-group">资源</p>
        <NavLink className="nav-child" to="/resources">资源中心</NavLink>
      </nav>
      <div className="sidebar-bottom">
        <NavLink className="nav-settings" to="/settings"><NavIcon name="settings" />设置</NavLink>
        <div className="sidebar-footer"><span className="status-dot" />仅本地回环 · 未连接实盘</div>
      </div>
    </aside>
    <main><header className="topbar"><span>本地研究</span><span>离线回测 · 不下单</span></header><div className="content">
      <Routes>
        <Route path="/strategies" element={<StrategiesWorkspace api={api} />} />
        <Route path="/strategies/:userStrategyId" element={<StrategiesWorkspace api={api} />} />
        <Route path="/runs" element={<PaperStatus />} />
        <Route path="/backtests" element={<Backtests api={api} />} />
        <Route path="/backtests/compare/:leftId/:rightId" element={<CompareBacktestsRoute api={api} />} />
        <Route path="/backtests/:jobId" element={<BacktestDetailRoute api={api} />} />
        <Route path="/settings" element={<ResearchSettings api={api} />} />
        <Route path="/research" element={<ResearchTasks api={api} />} />
        <Route path="/resources" element={<ResourcesPage api={api} />} />
        <Route path="/events" element={<EventValidation />} />
        <Route path="/research/:taskId" element={<ResearchTaskDetailRoute api={api} />} />
        <Route path="/candidates" element={<CandidateHistory api={api} />} />
        <Route path="/candidates/:candidateId" element={<CandidateDetail api={api} />} />
        <Route path="/holdouts" element={<HoldoutHistory api={api} />} />
        <Route path="/holdouts/new/:jobId" element={<HoldoutCreatorRoute api={api} />} />
        <Route path="/holdouts/:validationId" element={<HoldoutDetailRoute api={api} />} />
        <Route path="/batches/new" element={<BatchCreator api={api} />} />
        <Route path="/batches/:batchId" element={<BatchDetailRoute api={api} />} />
        <Route path="*" element={<Navigate replace to="/strategies" />} />
      </Routes>
    </div></main>
  </div>
}

type BatchConfiguration = ParameterMap
type BatchStatusFilter = 'all' | 'succeeded' | 'risk.rejected' | 'failed' | 'running' | 'queued'
type BatchSortField = string
type SortDirection = 'ascending' | 'descending'

function BatchCreator({ api }: { api: ApiAdapter }) {
  const navigate = useNavigate()
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [selected, setSelected] = useState('')
  const [dataset, setDataset] = useState<DatasetReference>({})
  const [initialCash, setInitialCash] = useState('')
  const [runs, setRuns] = useState<BatchConfiguration[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selectScenario = useCallback((scenario: ScenarioSummary | undefined) => {
    setSelected(scenario?.scenario_id ?? '')
    setInitialCash(scenario?.summary?.initial_cash ?? '')
    const initial = parameterDefaults(scenario)
    setRuns([{ ...initial }, { ...initial }])
    setError(null)
  }, [])

  useEffect(() => {
    let active = true
    api.listScenarios().then((items) => {
      if (!active) return
      setScenarios(items)
      selectScenario(items.find((item) => item.valid && (item.strategy_parameters?.length ?? 0) > 0))
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : 'Scenarios unavailable') })
    return () => { active = false }
  }, [api, selectScenario])

  const candidate = scenarios.find((item) => item.scenario_id === selected)

  const updateRun = (index: number, update: BatchConfiguration) => {
    setRuns((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...update } : item))
    setError(null)
  }
  const submit = async () => {
    if (!candidate?.input_identity) return
    setBusy(true); setError(null)
    try {
      const checked = dataset.dataset_id ? await api.validateScenario(selected, { initial_cash: initialCash, strategy_parameters: runs[0] }, dataset) : candidate
      const created = await api.createBatch({
        scenario_id: candidate.scenario_id,
        input_identity: checked.input_identity!,
        initial_cash: initialCash,
        runs: runs.map((item) => ({ strategy_parameters: item })),
      })
      navigate(`/batches/${created.batch_id}`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Batch request failed')
      setBusy(false)
    }
  }

  return <>
    <PageTitle title="New Experiment Batch" subtitle="Run 2–10 explicit parameter combinations through the installed engine." />
    {error && <section className="notice error">{error}</section>}
    <section className="panel batch-control-panel">
      <header><h2>Batch input</h2><span>One strategy and scenario</span></header>
      <DatasetSelector api={api} value={dataset} change={setDataset} instrument={candidate?.summary} />
      <div className="parameter-grid">
        <div><label htmlFor="batch-scenario">Scenario</label><select id="batch-scenario" value={selected} onChange={(event) => selectScenario(scenarios.find((item) => item.scenario_id === event.target.value))}>{scenarios.filter((item) => item.valid && (item.strategy_parameters?.length ?? 0) > 0).map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}</option>)}</select></div>
        <div><label htmlFor="batch-initial-cash">Initial cash</label><input id="batch-initial-cash" value={initialCash} onChange={(event) => setInitialCash(event.target.value)} /></div>
      </div>
      {candidate?.summary && <p className="muted">{candidate.summary.strategy_id} · {candidate.summary.venue}:{candidate.summary.symbol}</p>}
      {candidate?.summary && <p><span>{commissionLabel(candidate.summary.commission)}</span> · <span>{slippageLabel(candidate.summary.slippage)}</span> · <span>{latencyLabel(candidate.summary.latency)}</span></p>}
      <div className="batch-configurations">{runs.map((item, index) => <section className="strategy-parameters batch-configuration" key={index}>
        <header><h3>Run {index + 1}</h3><button aria-label={`Remove Run ${index + 1}`} disabled={runs.length <= 2} onClick={() => setRuns((current) => current.filter((_, itemIndex) => itemIndex !== index))}>Remove</button></header>
        <ParameterControls contracts={candidate?.strategy_parameters ?? []} values={item} prefix={`Run ${index + 1} `} update={(name, value) => updateRun(index, { [name]: value })} />
      </section>)}</div>
      <div className="actions"><button disabled={runs.length >= 10} onClick={() => setRuns((current) => [...current, { ...parameterDefaults(candidate) }])}>Add configuration</button><button className="primary" disabled={!candidate?.input_identity || runs.length < 2 || busy} onClick={() => { void submit() }}>Run batch</button></div>
    </section>
  </>
}

function BatchDetailRoute({ api }: { api: ApiAdapter }) {
  const { batchId = '' } = useParams()
  return <BatchDetail key={batchId} api={api} batchId={batchId} />
}

function BatchDetail({ api, batchId }: { api: ApiAdapter; batchId: string }) {
  const navigate = useNavigate()
  const [batch, setBatch] = useState<ExperimentBatch | null>(null)
  const [analytics, setAnalytics] = useState<Record<string, TradeAnalytics | null>>({})
  const [reports, setReports] = useState<Record<string, BacktestReport | null>>({})
  const [paths, setPaths] = useState<Record<string, EquityPath | null>>({})
  const [comparison, setComparison] = useState<string[]>([])
  const [statusFilter, setStatusFilter] = useState<BatchStatusFilter>('all')
  const [sortField, setSortField] = useState<BatchSortField>('member')
  const [sortDirection, setSortDirection] = useState<SortDirection>('ascending')
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)

  const refresh = useCallback(async () => {
    const currentGeneration = ++generation.current
    try {
      const current = await api.getBatch(batchId)
      if (currentGeneration !== generation.current) return null
      setBatch(current); setError(null)
      const available = await Promise.all(current.members.map(async (member) => {
        if (member.status !== 'succeeded' || !member.report_ready) return [member.job_id, null] as const
        try {
          const report = await api.getReport(member.job_id)
          if (!member.engine_run_id || report.run_id !== member.engine_run_id) throw new ApiFailure('report_identity_conflict', 'Verified report identity does not match this job')
          return [member.job_id, report] as const
        } catch (caught) {
          if (caught instanceof ApiFailure && caught.code !== 'report_unavailable') throw caught
          return [member.job_id, null] as const
        }
      }))
      if (currentGeneration === generation.current) setReports(Object.fromEntries(available))
      const availablePaths = await Promise.all(current.members.map(async member => [member.job_id, await loadPath(api, member)] as const))
      if (currentGeneration === generation.current) setPaths(Object.fromEntries(availablePaths))
      const tradeStats = await Promise.all(current.members.map(async member => [member.job_id, await loadTradeAnalytics(api, member)] as const))
      if (currentGeneration === generation.current) setAnalytics(Object.fromEntries(tradeStats))
      return current.status
    } catch (caught) {
      if (currentGeneration === generation.current) setError(caught instanceof Error ? caught.message : 'Batch unavailable')
      return null
    }
  }, [api, batchId])

  useEffect(() => {
    let active = true; let timer: number | undefined
    const poll = async () => {
      const status = await refresh()
      if (active && status === 'running') timer = window.setTimeout(poll, 150)
    }
    void poll()
    return () => { active = false; generation.current += 1; if (timer !== undefined) window.clearTimeout(timer) }
  }, [refresh])

  const toggle = (jobId: string) => setComparison((current) => current.includes(jobId)
    ? current.filter((value) => value !== jobId)
    : current.length < 2 ? [...current, jobId] : [current[1], jobId])
  const rows = useMemo(() => {
    if (!batch) return []
    const result = batch.members
      .map((member, index) => ({ member, ordinal: index + 1, report: reports[member.job_id] }))
      .filter(({ member }) => statusFilter === 'all' || member.presentation_status === statusFilter)
    const value = (row: typeof result[number]): string | undefined => {
      if (sortField.startsWith('parameter:')) {
        const value = jobParameters(row.member)[sortField.slice(10)]
        return value === undefined ? undefined : String(value)
      }
      if (sortField.startsWith('trade:')) {
        const key = sortField.slice(6) as 'closed_trades' | 'win_rate' | 'payoff_ratio' | 'average_win' | 'average_loss' | 'average_holding_seconds'
        const amount = analytics[row.member.job_id]?.[key]
        return amount == null ? undefined : String(amount)
      }
      if (sortField === 'max_drawdown_ratio') return paths[row.member.job_id]?.max_drawdown.ratio
      if (sortField === 'equity') return row.report?.economics.equity.amount
      if (sortField === 'net_pnl') return row.report?.economics.net_pnl.amount
      if (sortField === 'total_return') return row.report?.economics.total_return.value
      return String(row.ordinal)
    }
    return result.sort((left, right) => {
      const leftValue = value(left)
      const rightValue = value(right)
      if (leftValue === undefined || rightValue === undefined) {
        if (leftValue === rightValue) return left.ordinal - right.ordinal
        return leftValue === undefined ? 1 : -1
      }
      const compared = compareCanonicalDecimal(leftValue, rightValue)
      return compared === 0 ? left.ordinal - right.ordinal : compared * (sortDirection === 'ascending' ? 1 : -1)
    })
  }, [batch, reports, paths, analytics, sortDirection, sortField, statusFilter])
  const parameterNames = batch?.members[0]?.strategy_descriptor?.parameters.map(p => p.name) ?? Object.keys(batch?.members[0] ? jobParameters(batch.members[0]) : {})
  const analysisSummary = useMemo(() => {
    const members = batch?.members ?? []
    const count = (status: string) => members.filter((member) => member.presentation_status === status).length
    return {
      total: members.length,
      succeeded: count('succeeded'),
      riskRejected: count('risk.rejected'),
      failed: count('failed'),
      running: count('running'),
      queued: count('queued'),
      reports: Object.values(reports).filter((report) => report !== null).length,
    }
  }, [batch, reports])
  if (!batch) return <><PageTitle title="Experiment Batch" subtitle="Loading persisted batch members." />{error && <section className="notice error">{error}</section>}</>
  return <>
    <PageTitle title="Experiment Batch" subtitle={`${batch.scenario_id} · batch ${batch.batch_id}`} status={batch.status} />
    {error && <section className="notice error">{error}</section>}
    <div className="result-toolbar"><Link to="/batches/new">← Run another batch</Link><button onClick={() => { void refresh() }}>Refresh</button></div>
    <section className="panel identity-panel"><header><h2>Batch identity</h2><span>Persisted grouping only</span></header><dl className="summary-list"><div><dt>Created</dt><dd>{batch.created_at}</dd></div><div><dt>Scenario</dt><dd>{batch.scenario_id}</dd></div><div><dt>Members</dt><dd>{batch.member_count}</dd></div></dl></section>
    <section className="panel analysis-summary" aria-label="Analysis summary"><header><h2>Analysis summary</h2><span>Derived from current jobs and verified reports</span></header><dl className="summary-list"><div><dt>Total members</dt><dd data-summary="total">{analysisSummary.total}</dd></div><div><dt>Succeeded</dt><dd data-summary="succeeded">{analysisSummary.succeeded}</dd></div><div><dt>Risk rejected</dt><dd data-summary="risk.rejected">{analysisSummary.riskRejected}</dd></div><div><dt>Failed</dt><dd data-summary="failed">{analysisSummary.failed}</dd></div><div><dt>Running</dt><dd data-summary="running">{analysisSummary.running}</dd></div><div><dt>Queued</dt><dd data-summary="queued">{analysisSummary.queued}</dd></div><div><dt>Reports available</dt><dd data-summary="reports">{analysisSummary.reports}</dd></div></dl></section>
    <section className="panel comparison-panel"><header><h2>Member runs</h2><span>Real jobs and formal reports</span></header><div className="analysis-controls"><div><label htmlFor="batch-status-filter">Status filter</label><select id="batch-status-filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as BatchStatusFilter)}><option value="all">All</option><option value="succeeded">Succeeded</option><option value="risk.rejected">Risk Rejected</option><option value="failed">Failed</option><option value="running">Running</option><option value="queued">Queued</option></select></div><div><label htmlFor="batch-sort">Sort by</label><select id="batch-sort" value={sortField} onChange={(event) => setSortField(event.target.value as BatchSortField)}><option value="member">Member order</option>{parameterNames.map(name => <option key={name} value={`parameter:${name}`}>{name}</option>)}<option value="equity">Final equity</option><option value="net_pnl">Net P&amp;L</option><option value="total_return">Total return</option><option value="max_drawdown_ratio">Max Drawdown</option>{Object.entries({ closed_trades: "Closed trades", win_rate: "Win rate", payoff_ratio: "Payoff ratio", average_win: "Average win", average_loss: "Average loss", average_holding_seconds: "Holding time" }).map(([key, label]) => <option key={key} value={`trade:${key}`}>{label}</option>)}</select></div><div><label htmlFor="batch-sort-direction">Direction</label><select id="batch-sort-direction" value={sortDirection} onChange={(event) => setSortDirection(event.target.value as SortDirection)}><option value="ascending">Ascending</option><option value="descending">Descending</option></select></div></div><table><thead><tr><th>Run</th>{parameterNames.map(name => <th key={name}>{name}</th>)}<th>Status</th><th>Final equity</th><th>Net P&amp;L</th><th>Total return</th><th>Max Drawdown</th><th>Closed / W / L / B</th><th>Win rate</th><th>Average win / loss</th><th>Payoff ratio</th><th>Holding time (s)</th><th>Action</th><th>Compare</th></tr></thead><tbody>{rows.map(({ member, ordinal, report }) => {
      const stats = analytics[member.job_id]
      const metric = (value: string | null | undefined) => value ?? "Unavailable"
      const equityCurrency = report?.economics.equity.currency ?? report?.economics.currency
      const pnlCurrency = report?.economics.net_pnl.currency ?? report?.economics.currency
      const money = (amount: string, currency: string | undefined) => `${amount} ${currency ?? 'Currency unavailable'}`
      return <tr key={member.job_id}><th scope="row">Run {ordinal}</th>{parameterNames.map(name => <td key={name}>{jobParameters(member)[name] ?? 'Unavailable'}</td>)}<td><span className={`status status-${member.presentation_status}`}>{member.presentation_status}</span></td>{report ? <><td>{money(report.economics.equity.amount, equityCurrency)}</td><td>{money(report.economics.net_pnl.amount, pnlCurrency)}</td><td>{percent(report.economics.total_return.value)}</td></> : <td colSpan={3}>No report</td>}<td>{paths[member.job_id] ? pathPercent(paths[member.job_id]!.max_drawdown.ratio) : 'Unavailable'}</td><td>{stats ? `${stats.closed_trades} / ${stats.wins} / ${stats.losses} / ${stats.breakeven}` : 'Unavailable'}</td><td>{stats?.win_rate != null ? percent(stats.win_rate) : 'Unavailable'}</td><td>{stats ? `${metric(stats.average_win)} / ${metric(stats.average_loss)} ${stats.currency}` : 'Unavailable'}</td><td>{metric(stats?.payoff_ratio)}</td><td>{metric(stats?.average_holding_seconds)}</td><td><Link to={`/backtests/${member.job_id}`}>View</Link></td><td><input type="checkbox" aria-label={`Select ${member.job_id} for comparison`} checked={comparison.includes(member.job_id)} onChange={() => toggle(member.job_id)} /></td></tr>
    })}</tbody></table><button className="primary compare-button" disabled={comparison.length !== 2} onClick={() => navigate(`/backtests/compare/${comparison[0]}/${comparison[1]}`)}>Compare selected runs</button></section>
  </>
}

function Backtests({ api }: { api: ApiAdapter }) {
  const navigate = useNavigate()
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [jobs, setJobs] = useState<BacktestJob[]>([])
  const [selected, setSelected] = useState('')
  const [initialCash, setInitialCash] = useState('')
  const [values, setValues] = useState<ParameterMap>({})
  const [dataset, setDataset] = useState<DatasetReference>({})
  const [frozenSource, setFrozenSource] = useState<StrategySource | undefined>()
  const [comparison, setComparison] = useState<string[]>([])
  const [validated, setValidated] = useState<ScenarioSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  // A refused validation or a refused dispatch is announced where the user just
  // acted, and focus moves to it: the failure was previously rendered above the
  // whole form, off screen on a narrow viewport.
  const errorRef = useRef<HTMLParagraphElement | null>(null)

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  useEffect(() => {
    let current = true
    Promise.all([api.listScenarios(), api.listBacktests()]).then(([nextScenarios, nextJobs]) => {
      if (!current) return
      const first = nextScenarios.find((item) => item.valid && item.strategy_descriptor?.research_visible !== false)
      setScenarios(nextScenarios); setJobs(nextJobs); setSelected(first?.scenario_id ?? '')
      setInitialCash(first?.summary?.initial_cash ?? ''); setValues(parameterDefaults(first))
    }).catch(() => { if (current) setDisconnected(true) })
    return () => { current = false }
  }, [api])

  const candidate = useMemo(() => scenarios.find((item) => item.scenario_id === selected), [scenarios, selected])

  const parameters = (): BacktestParameters => ({
    initial_cash: initialCash,
    strategy_parameters: values,
    ...(frozenSource ? { strategy_source: frozenSource } : {}),
  })
  const changeScenario = (scenarioId: string) => {
    const next = scenarios.find((item) => item.scenario_id === scenarioId)
    setFrozenSource(undefined)
    setSelected(scenarioId); setInitialCash(next?.summary?.initial_cash ?? '')
    setValues(parameterDefaults(next)); setValidated(null); setError(null)
  }
  const changeParameter = (setter: (value: string) => void, value: string) => {
    setter(value); setValidated(null); setError(null)
  }
  const validate = async () => {
    setBusy(true); setError(null)
    try { setValidated(await api.validateScenario(selected, parameters(), dataset)) }
    catch (caught) { setValidated(null); setError(caught instanceof Error ? caught.message : 'Validation failed') }
    finally { setBusy(false) }
  }
  const run = async () => {
    if (!validated?.input_identity) return
    setBusy(true); setError(null)
    try {
      const random = globalThis.crypto?.randomUUID?.() ?? `request-${Date.now()}-${Math.random().toString(16).slice(2)}`
      const accepted = await api.createBacktest({ scenario_id: validated.scenario_id, input_identity: validated.input_identity, parameters: parameters(), request_id: random })
      navigate(`/backtests/${accepted.job_id}`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Run request failed'); setBusy(false) }
  }
  const restoreParameters = (item: BacktestJob) => {
    const snapshot = item.input_snapshot?.scenario
    if (!snapshot) return
    setDataset(item.input_snapshot?.research_input ? { dataset_id: item.input_snapshot.research_input.dataset_id, source_sha256: item.input_snapshot.research_input.source_sha256 } : {})
    setFrozenSource(snapshot.strategy.source)
    setSelected(item.scenario_id); setInitialCash(snapshot.funding.initial_cash)
    setValues(jobParameters(item)); setValidated(null); setError(null)
  }
  const toggleComparison = (jobId: string) => {
    setComparison((current) => current.includes(jobId)
      ? current.filter((value) => value !== jobId)
      : current.length < 2 ? [...current, jobId] : [current[1], jobId])
  }

  return <>
    <PageTitle title="Offline Backtests" subtitle="Run, replay, and compare real installed-engine attempts." />
    {disconnected && <section className="notice error">Local service is unreachable</section>}
    <div className="backtest-grid">
      <section className="panel control-panel">
        <header><h2>New backtest</h2><span>One active job</span></header>
        <label htmlFor="scenario">Scenario</label>
        <select id="scenario" value={selected} onChange={(event) => changeScenario(event.target.value)}>
          <option value="">Select prepared scenario</option>
          {scenarios.filter(item => item.strategy_descriptor?.research_visible !== false).map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}{item.valid ? '' : ' · invalid'}</option>)}
        </select>
        <DatasetSelector api={api} value={dataset} change={value => { setDataset(value); setValidated(null) }} instrument={candidate?.summary} />
        {candidate?.summary && <dl className="summary-list">
          <div><dt>Strategy</dt><dd>{candidate.summary.strategy_id}</dd></div><div><dt>Instrument</dt><dd>{candidate.summary.venue}:{candidate.summary.symbol}</dd></div>
          <div><dt>Commission</dt><dd>{commissionLabel(candidate.summary.commission)}</dd></div>
          <div><dt>Slippage</dt><dd>{slippageLabel(candidate.summary.slippage)}</dd></div>
          <div><dt>Execution latency</dt><dd>{latencyLabel(candidate.summary.latency)}</dd></div>
          <div><dt>Initial cash</dt><dd>{candidate.summary.initial_cash}</dd></div><div><dt>Records</dt><dd>{candidate.summary.record_count}</dd></div>

        </dl>}
        <div className="parameter-grid">
          <div><label htmlFor="initial-cash">Initial cash</label><input id="initial-cash" value={initialCash} onChange={(event) => changeParameter(setInitialCash, event.target.value)} /></div>
          <div><label htmlFor="symbol">Symbol</label><input id="symbol" value={candidate?.summary?.symbol ?? ''} readOnly aria-describedby="symbol-source" /><small id="symbol-source">Registered scenario/data only</small></div>
        </div>
        <section className="strategy-parameters"><h3>Strategy Parameters</h3>
          <ParameterControls contracts={validated?.strategy_parameters ?? candidate?.strategy_parameters ?? []} values={values} update={(name, value) => { setValues(current => ({ ...current, [name]: value })); setValidated(null); setError(null) }} />
        </section>
        <div className="actions"><button disabled={!selected || busy} onClick={validate}>Validate input</button><button className="primary" disabled={!validated || busy} onClick={run}>Run new backtest</button></div>
        {error && <p className="notice error" role="alert" tabIndex={-1} ref={errorRef}>{error}</p>}
        {validated?.summary && <div className="validated"><strong>Validated input</strong><span>{parameterText(validated.summary.parameters ?? {})}</span><small>{validated.normalized_input_identity?.scenario_sha256.slice(0, 12) ?? validated.input_identity?.scenario_sha256.slice(0, 12)}…</small></div>}
      </section>
      <section className="panel jobs-panel"><header><h2>Recent Runs</h2><span>Refresh-safe history</span></header>
        {jobs.length === 0 ? <p className="muted">No backtests yet.</p> : <ul className="job-list">{jobs.map((item) => {
          const input = item.input_snapshot?.scenario
          return <li key={item.job_id} className="job-card">
            <div className="job-card-title"><Link to={`/backtests/${item.job_id}`}><strong>{item.scenario_id}</strong></Link><span className={`status status-${item.status}`}>{item.status}</span></div>
            {item.input_snapshot?.research_input && <small>Dataset: {item.input_snapshot.research_input.dataset_id}</small>}
            {item.created_at && <time>{item.created_at}</time>}
            <small>{input ? `${input.instrument.symbol} · Cash ${input.funding.initial_cash} · ${parameterText(jobParameters(item))}` : 'Legacy run · input snapshot unavailable'}</small>
            {input && <small>{item.strategy_descriptor?.display_name ?? input.strategy.id} · {input.strategy.id} v{input.strategy.version ?? 1}{input.strategy.source && ` · Local package · ${input.strategy.source.package_id} · ${input.strategy.source.artifact_sha256.slice(0, 12)}`}</small>}
            <small>{item.engine_run_id ?? item.job_id}</small>
            <div className="job-actions"><Link to={`/backtests/${item.job_id}`}>View</Link><button disabled={!input} aria-label={`Use parameters for ${item.job_id}`} onClick={() => restoreParameters(item)}>Use parameters</button><label><input type="checkbox" aria-label={`Select ${item.job_id} for comparison`} checked={comparison.includes(item.job_id)} onChange={() => toggleComparison(item.job_id)} /> Compare</label></div>
          </li>
        })}</ul>}
        <button className="primary compare-button" disabled={comparison.length !== 2} onClick={() => navigate(`/backtests/compare/${comparison[0]}/${comparison[1]}`)}>Compare selected runs</button>
      </section>
    </div>
  </>
}

function BacktestDetailRoute({ api }: { api: ApiAdapter }) {
  const { jobId = '' } = useParams()
  return <BacktestDetail key={jobId} api={api} jobId={jobId} />
}

type ComparedRun = { analytics?: TradeAnalytics | null; job: BacktestJob; report: BacktestReport | null; path?: EquityPath | null }

function percent(value: string): string {
  return `${exactDelta('0', value, 2).replace(/^\+/, '')}%`
}

/** One row of the formal comparison. `key` is the stable internal identity the
 * arithmetic and the availability rules read; `label` is only what the reader
 * sees, so translating a heading can never change which rule runs. */
type CompareMetric = {
  key: string
  label: string
  before?: string
  after?: string
  shift: number
  suffix: string
  monetary: boolean
  leftCurrency?: string
  rightCurrency?: string
}

/** Trade analytics are the secondary evidence: an unavailable denominator is
 * named, never folded into the primary comparison as zero. */
const ANALYTICS_KEYS = ['closed_trades', 'wins', 'losses', 'breakeven', 'win_rate', 'average_win', 'average_loss', 'payoff_ratio', 'average_holding_seconds'] as const
const ANALYTICS_LABELS: Record<(typeof ANALYTICS_KEYS)[number], string> = {
  closed_trades: 'Closed trades', wins: 'Winning trades', losses: 'Losing trades', breakeven: 'Breakeven trades',
  win_rate: 'Win rate (ratio)', average_win: 'Average win', average_loss: 'Average loss', payoff_ratio: 'Payoff ratio',
  average_holding_seconds: 'Average holding time (seconds)',
}
const metricUnavailable = (key: string) =>
  key.startsWith('max_drawdown') || (ANALYTICS_KEYS as readonly string[]).includes(key)
function compareCell(row: CompareMetric, side: 'before' | 'after'): string {
  const value = side === 'before' ? row.before : row.after
  const currency = side === 'before' ? row.leftCurrency : row.rightCurrency
  if (value === undefined) return metricUnavailable(row.key) ? 'Unavailable' : 'No report'
  if (row.key === 'return') return percent(value)
  return `${value}${currency ? ` ${currency}` : ''}`
}
function compareDelta(row: CompareMetric): string {
  const { before, after } = row
  if (before === undefined || after === undefined) return '—'
  if (row.monetary && (!row.leftCurrency || !row.rightCurrency)) return 'Currency unavailable'
  if (row.monetary && row.leftCurrency !== row.rightCurrency) return 'Different currencies'
  return `${exactDelta(before, after, row.shift)}${row.suffix}${row.monetary ? ` ${row.leftCurrency}` : ''}`
}

/**
 * The comparison route. The instance is keyed on the exact route pair, the way the
 * report route is keyed on its job: naming another pair mounts a fresh comparison
 * instead of leaving the previous pair's runs, reports, owner or errors on screen
 * while the new one is still being read. Nothing of the old pair can be rendered
 * under the new pair's ids, and a late answer for the old pair lands in a
 * component that no longer exists.
 */
function CompareBacktestsRoute({ api }: { api: ApiAdapter }) {
  const { leftId = '', rightId = '' } = useParams()
  return <CompareBacktests key={`${leftId}:${rightId}`} api={api} leftId={leftId} rightId={rightId} />
}

function CompareBacktests({ api, leftId, rightId }: { api: ApiAdapter; leftId: string; rightId: string }) {
  const [runs, setRuns] = useState<[ComparedRun, ComparedRun] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Two identical route ids are not two distinct experiments, so this page never
  // presents them as a comparison and never claims a common owner for them. That is
  // derived from the route instead of stored by the effect, so the page states it in
  // the same render that decides to read nothing.
  const identicalPairError = leftId === rightId ? '同一个运行不能与它自己对比：请选择两个不同的已持久化运行。' : null

  useEffect(() => {
    let active = true
    // A pair the route does not really name is not read at all.
    if (leftId === rightId) return () => { active = false }
    const load = async (jobId: string): Promise<ComparedRun> => {
      const job = await api.getBacktest(jobId)
      // The route names the run this page may show. A host that answers with some
      // other job is an identity conflict, not a comparison.
      if (job.job_id !== jobId) throw new ApiFailure('job_identity_conflict', 'Loaded run identity does not match the requested run')
      if (job.status !== 'succeeded' || !job.report_ready) return { job, report: null }
      let report: BacktestReport
      try {
        report = await api.getReport(jobId)
      } catch (caught) {
        if (typeof caught === 'object' && caught !== null && 'code' in caught && caught.code === 'report_unavailable') return { job, report: null }
        throw caught
      }
      if (!job.engine_run_id || report.run_id !== job.engine_run_id) {
        throw new ApiFailure('report_identity_conflict', 'Verified report identity does not match this job')
      }
      return { job, report, path: await loadPath(api, job), analytics: await loadTradeAnalytics(api, job) }
    }
    Promise.all([load(leftId), load(rightId)])
      .then(([left, right]) => { if (active) setRuns([left, right]) })
      .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : 'Comparison failed') })
    return () => { active = false }
  }, [api, leftId, rightId])

  // The way back is part of every state of this route, and each persisted run
  // keeps a direct link to its own formal report — visible, not behind a
  // disclosure — so the comparison is never a dead end.
  const toolbar = <div className="result-toolbar">
    <Link to="/backtests">回测列表</Link>
    {runs && <span className="compare-report-links">
      <Link to={`/backtests/${runs[0].job.job_id}`}>运行 A 正式报告</Link>
      <Link to={`/backtests/${runs[1].job.job_id}`}>运行 B 正式报告</Link>
    </span>}
  </div>

  if (!runs) return <div className="compare-page">
    <PageTitle title="回测对比" subtitle="读取两个已持久化运行及其正式报告的只读视图。" />
    {toolbar}
    {(error ?? identicalPairError) && <section className="notice error" role="alert">{error ?? identicalPairError}</section>}
  </div>
  const [left, right] = runs
  const leftInput = left.job.input_snapshot?.scenario
  const rightInput = right.job.input_snapshot?.scenario
  if (!leftInput || !rightInput) return <div className="compare-page">
    <PageTitle title="回测对比" subtitle="两两对比需要已持久化的 v2 输入快照。" />
    {toolbar}
    <section className="notice error">输入快照不可用</section>
  </div>
  const inputRows = [
    ['Initial Cash', leftInput.funding.initial_cash, rightInput.funding.initial_cash],
    ['Action contract', String(leftInput.strategy.action_contract ?? 'V1'), String(rightInput.strategy.action_contract ?? 'V1')],
    ['Position lifecycle', String(leftInput.strategy.position_lifecycle ?? 'single-entry'), String(rightInput.strategy.position_lifecycle ?? 'single-entry')],
    ['Max round trips', String(leftInput.strategy.max_round_trips ?? '—'), String(rightInput.strategy.max_round_trips ?? '—')],
    ['Report version', left.report?.schema ?? '—', right.report?.schema ?? '—'],
    ['Symbol', leftInput.instrument.symbol, rightInput.instrument.symbol],
    ['Commission', commissionLabel(leftInput.execution?.commission), commissionLabel(rightInput.execution?.commission)],
    ['Slippage', slippageLabel(leftInput.execution?.slippage), slippageLabel(rightInput.execution?.slippage)],
    ['Execution latency', latencyLabel(leftInput.execution?.latency), latencyLabel(rightInput.execution?.latency)],
  ]
  const sameImplementation = (leftInput.strategy.source?.kind ?? 'builtin') === (rightInput.strategy.source?.kind ?? 'builtin') && leftInput.strategy.source?.artifact_sha256 === rightInput.strategy.source?.artifact_sha256
  const sameStrategy = sameImplementation && leftInput.strategy.id === rightInput.strategy.id && (leftInput.strategy.version ?? 1) === (rightInput.strategy.version ?? 1)
  const parameterRows = sameStrategy
    ? (left.job.strategy_descriptor?.parameters.map(p => p.name) ?? Object.keys(jobParameters(left.job)))
        .filter((name, index, all) => all.indexOf(name) === index)
        .map(name => ({ label: name, before: String(jobParameters(left.job)[name]), after: String(jobParameters(right.job)[name]) }))
        // Changed parameters lead: the reader came for what moved, and an
        // unchanged row is context that follows it.
        .sort((a, b) => Number(a.before === a.after) - Number(b.before === b.after))
    : []
  // The four figures a comparison is read for lead; every other recorded number
  // stays in its own native disclosure below them, so none is dropped and none
  // competes with these.
  const primaryMetricRows: CompareMetric[] = [
    { key: 'equity', label: '期末权益', before: left.report?.economics.equity.amount, after: right.report?.economics.equity.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.equity.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.equity.currency ?? right.report?.economics.currency },
    { key: 'net_pnl', label: '净盈亏', before: left.report?.economics.net_pnl.amount, after: right.report?.economics.net_pnl.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.net_pnl.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.net_pnl.currency ?? right.report?.economics.currency },
    { key: 'fees', label: '手续费', before: left.report?.economics.fees?.amount, after: right.report?.economics.fees?.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.fees?.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.fees?.currency ?? right.report?.economics.currency },
    { key: 'return', label: '收益率', before: left.report?.economics.total_return.value, after: right.report?.economics.total_return.value, shift: 2, suffix: ' pp', monetary: false },
  ]
  const secondaryMetricRows: CompareMetric[] = [
    { key: 'max_drawdown_amount', label: '最大回撤金额', before: left.path?.max_drawdown.amount, after: right.path?.max_drawdown.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.path?.currency, rightCurrency: right.path?.currency },
    { key: 'max_drawdown_ratio', label: '最大回撤比率', before: left.path?.max_drawdown.ratio, after: right.path?.max_drawdown.ratio, shift: 0, suffix: '', monetary: false },
    { key: 'orders', label: '订单数', before: left.report ? String(left.report.economics.counts.orders) : undefined, after: right.report ? String(right.report.economics.counts.orders) : undefined, shift: 0, suffix: '', monetary: false },
    { key: 'fills', label: '成交数', before: left.report ? String(left.report.economics.counts.fills) : undefined, after: right.report ? String(right.report.economics.counts.fills) : undefined, shift: 0, suffix: '', monetary: false },
  ]
  // Trade analytics are the secondary evidence: a metric the verified report does
  // not carry is named unavailable and stays in its own disclosure, never folded
  // into the primary comparison as a zero.
  const analyticsRows: CompareMetric[] = ANALYTICS_KEYS.map(key => {
    // An analytics value this workspace never served stays absent — the cell says
    // so — instead of becoming a zero that would read as a measured result.
    const before = left.analytics ? left.analytics[key] : null
    const after = right.analytics ? right.analytics[key] : null
    const monetary = key === 'average_win' || key === 'average_loss'
    return {
      key, label: ANALYTICS_LABELS[key],
      before: before == null ? undefined : String(before),
      after: after == null ? undefined : String(after),
      shift: 0, suffix: '', monetary,
      leftCurrency: monetary ? left.analytics?.currency : undefined,
      rightCurrency: monetary ? right.analytics?.currency : undefined,
    }
  })
  return <div className="compare-page">
    <PageTitle title="回测对比" subtitle="基于两个已持久化运行及其正式报告的只读对比。" />
    {toolbar}
    {/* The association sits directly under the toolbar, so which saved strategy
        these two runs belong to is read before the differences, not after them.
        The whole comparison is keyed on the route pair by CompareBacktestsRoute,
        which is what keeps another pair's owner off this page. */}
    <StrategyComparisonAssociation api={api} leftJobId={left.job.job_id} rightJobId={right.job.job_id} />
    <section className="panel comparison-panel" aria-label="参数差异">
      <header><h2>参数差异</h2><span>已持久化的规范化策略输入</span></header>
      {!sameImplementation ? <p>Strategy implementation changed</p> : !sameStrategy && <p>NO_PARAMETER_DELTA · Different strategy ID/version</p>}
      <table><thead><tr><th>参数</th><th>运行 A</th><th>运行 B</th><th>差异</th></tr></thead><tbody>{parameterRows.map(row => <tr className={row.before === row.after ? '' : 'changed'} key={row.label}><th>{row.label}</th><td>{row.before}</td><td>{row.after}</td><td>{exactDelta(row.before, row.after)}</td></tr>)}</tbody></table>
    </section>
    <section className="panel comparison-panel" aria-label="结果对比">
      <header><h2>结果对比</h2><span>已核验的正式报告与路径证据</span></header>
      {/* What the reader has to know before reading a single number: how small each
          window is, which way the delta runs, and what the P&L does not mean. The
          digests themselves stay in the technical disclosure below. */}
      <div className="compare-limits">
        <dl className="summary-list">
          {[left, right].map((run, index) => <div key={`count-${run.job.job_id}`}><dt>运行 {index === 0 ? 'A' : 'B'} 记录数</dt><dd>{run.job.input_identity.record_count} 条记录{run.job.input_identity.record_count <= 6 ? ' · 小样本：仅用于流程验证，不作稳健性结论' : ''}</dd></div>)}
          <div><dt>差异方向</dt><dd>运行 B − 运行 A</dd></div>
        </dl>
        <p className="muted">差异仅作描述：数据、成本与运行条件都可能不同，本页不排序、不推荐。单一窗口不足以确立稳健性，正式报告是每个数字的唯一权威。</p>
        <p className="muted">净盈亏包含窗口结束时仍持有头寸的估值，不是已实现利润。</p>
      </div>
      <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{primaryMetricRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      <details className="disclosure"><summary>其他结果指标</summary>
        <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{secondaryMetricRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      </details>
      <details className="disclosure"><summary>分析指标明细</summary>
        <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{analyticsRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      </details>
    </section>
    <details className="disclosure"><summary>规范化输入</summary>
      <table><thead><tr><th>参数</th><th>运行 A</th><th>运行 B</th><th>差异</th></tr></thead><tbody>{inputRows.map(([label, before, after]) => <tr className={before === after ? '' : 'changed'} key={label}><th>{label}</th><td>{before}</td><td>{after}</td><td>{before === after ? 'Unchanged' : 'Changed'}</td></tr>)}</tbody></table>
    </details>
    <details className="disclosure"><summary>运行身份与技术细节</summary>
      <div className="compare-run-detail">
        {[left, right].map((run, index) => <section className="panel" key={run.job.job_id}><header><h2>运行 {index === 0 ? 'A' : 'B'}</h2><span className={`status status-${run.job.status}`}>{run.job.status}</span></header>
          <dl className="summary-list">
            <div><dt>Web job</dt><dd>{run.job.job_id}</dd></div>
            <div><dt>Engine run</dt><dd>{run.job.engine_run_id ?? 'not available'}</dd></div>
            <div><dt>Input SHA-256</dt><dd>{run.job.input_sha256 ?? 'not available'}</dd></div>
            <div><dt>Scenario SHA-256</dt><dd>{run.job.input_identity.scenario_sha256}</dd></div>
            <div><dt>Data SHA-256</dt><dd>{run.job.input_identity.data_sha256}</dd></div>
            <div><dt>Outcome</dt><dd>{run.report ? '正式报告' : run.job.error_code ?? 'No report'}</dd></div>
          </dl>
          <div className="downloads"><Link to={`/backtests/${run.job.job_id}`}>打开正式报告</Link><a href={api.artifactUrl(run.job.job_id, 'report.json')}>Download report.json</a><a href={api.artifactUrl(run.job.job_id, 'summary.txt')}>Download summary.txt</a></div>
        </section>)}
      </div>
    </details>
  </div>
}

type StrategyAssociationRecord = { userStrategyId: string; name: string; revision: number | null }

/**
 * One frozen record's revision, printed only when the record really carries a
 * positive whole revision. A payload that is missing, empty or nonsensical is
 * named unknown; this page never invents a revision to fill the gap.
 */
function frozenRevision(payload: UserStrategyExperimentPayload | undefined): number | null {
  const revision = payload?.draft_revision
  return typeof revision === 'number' && Number.isInteger(revision) && revision > 0 ? revision : null
}

const frozenRevisionText = (revision: number | null) =>
  revision === null ? '冻结修订未知' : `冻结 v${revision}`

/**
 * Every frozen record in the saved strategies whose own experiment names this
 * exact job. This is the one association rule: the record's persisted `job_id`,
 * never the report's strategy id, engine implementation, symbol, name, or a
 * "first/latest/current" choice. Both the report page and the comparison page
 * read it here, so neither can drift into a different rule.
 */
function experimentRecordsForJob(strategies: UserStrategy[], jobId: string): StrategyAssociationRecord[] {
  return strategies.flatMap(strategy => strategy.experiments
    .filter(experiment => experiment.job_id === jobId)
    .map(experiment => ({
      userStrategyId: strategy.user_strategy_id,
      name: strategy.name,
      revision: frozenRevision(experiment.payload),
    })))
}

/**
 * The way back from one formal report to the persisted strategy whose frozen run
 * it is. The association is read on its own and matched only by the job each
 * experiment recorded: never the report's own strategy id, engine implementation,
 * symbol, name, or a "first/latest/current" choice. One exact record offers the
 * return link; no record, an unavailable strategy API or a refused read says so
 * honestly and falls back to the strategy list; several records are a conflict
 * this page refuses to resolve silently. The caller keys the component on the job
 * id and on the explicit refresh, and its read is generation-guarded, so a route
 * change, a refresh or a late answer can never put another job's strategy on this
 * page.
 */
function StrategyAssociation({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  type Association =
    | { kind: 'reading' }
    | { kind: 'none'; note: string }
    | { kind: 'one'; record: StrategyAssociationRecord }
    | { kind: 'several'; count: number; records: StrategyAssociationRecord[] }
  // The first render of every mount reads; the caller keys this component on the
  // job and on the explicit refresh, so a new job or a new refresh starts here in
  // the reading state without the effect having to write it synchronously.
  const [association, setAssociation] = useState<Association>({ kind: 'reading' })
  const generation = useRef(0)

  useEffect(() => {
    const read = ++generation.current
    const list = api.listUserStrategies
    if (!list) {
      // The optional read settles asynchronously, so a workspace that unmounts or
      // swaps its api never writes this page from a stale answer.
      void Promise.resolve().then(() => {
        if (generation.current === read) setAssociation({ kind: 'none', note: '这个工作区不提供已保存的用户策略。' })
      })
      return () => { generation.current += 1 }
    }
    list().then(strategies => {
      if (generation.current !== read) return
      const records = experimentRecordsForJob(strategies, jobId)
      if (records.length === 1) setAssociation({ kind: 'one', record: records[0] })
      else if (records.length === 0) setAssociation({ kind: 'none', note: '没有已保存的用户策略冻结了这个运行。' })
      else setAssociation({ kind: 'several', count: records.length, records })
    }).catch(error => {
      if (generation.current !== read) return
      setAssociation({
        kind: 'none',
        note: `${error instanceof Error ? error.message : '策略关联读取失败。'} · 这次关联读取失败了，正式报告不受影响。`,
      })
    })
    return () => { generation.current += 1 }
  }, [api, jobId])

  // One candidate per distinct user strategy, named with its own id so two
  // strategies that share a name stay told apart. No revision is printed here:
  // records that disagree about the frozen revision are exactly the case this
  // page refuses to resolve, so it names the strategies and picks no revision.
  const candidates = association.kind === 'several'
    ? association.records.filter((record, index) =>
        association.records.findIndex(item => item.userStrategyId === record.userStrategyId) === index)
    : []

  return <section className="panel association-panel" aria-label="策略关联">
    <header><h2>策略关联</h2><span>只读 · 不启动任何运行</span></header>
    {association.kind === 'reading' && <p className="muted">正在读取冻结了这个运行的已保存策略……</p>}
    {association.kind === 'none' && <>
      <p className="muted">{association.note}</p>
      <p><Link to="/strategies">打开策略列表</Link></p>
    </>}
    {association.kind === 'one' && <>
      <p><strong>{association.record.name}</strong> · <span>{frozenRevisionText(association.record.revision)}</span></p>
      {/* The way back names the frozen run this report is about, so the strategy
          opens on that run instead of resetting the evidence to the latest one. */}
      <p><Link to={`/strategies/${encodeURIComponent(association.record.userStrategyId)}?evidence_a=${encodeURIComponent(jobId)}`}>返回此策略继续研究</Link></p>
    </>}
    {association.kind === 'several' && <>
      <p className="notice error">{`这个运行匹配到 ${association.count} 条冻结记录，无法确定唯一的来源策略，因此这里不替你选择，也不显示任何冻结修订：请自己打开要继续的那一个。`}</p>
      <ul className="association-candidates">{candidates.map(candidate => <li key={candidate.userStrategyId}>
        <Link to={`/strategies/${encodeURIComponent(candidate.userStrategyId)}`}>{`${candidate.name} · ${candidate.userStrategyId}`}</Link>
      </li>)}</ul>
    </>}
  </section>
}

/**
 * The read-only association of a two-run comparison with one saved strategy.
 *
 * A comparison has one honest answer only when each of the two distinct route
 * jobs names exactly one frozen record, and both records belong to the same
 * saved strategy. Anything else — two different owners, no record for a run, a
 * run frozen twice, a workspace with no strategy list, a refused read — is named
 * as such and falls back to the strategy list; this panel never guesses a common
 * owner and never prints a revision it could not read. Sharing a registered
 * engine strategy is not sharing an owner, so nothing here is matched by
 * strategy id, name or the current draft.
 *
 * The read is generation-guarded and the caller keys this component on the route
 * pair, so a late answer for one pair can never name an owner on another. A
 * failed or missing association read stays local: the formal comparison above it
 * keeps rendering.
 */
function StrategyComparisonAssociation({ api, leftJobId, rightJobId }: { api: ApiAdapter; leftJobId: string; rightJobId: string }) {
  type Pair =
    | { kind: 'reading' }
    | { kind: 'none'; note: string }
    | { kind: 'owner'; record: StrategyAssociationRecord; leftRevision: number | null; rightRevision: number | null }
  const [pair, setPair] = useState<Pair>({ kind: 'reading' })
  const generation = useRef(0)

  useEffect(() => {
    const read = ++generation.current
    const list = api.listUserStrategies
    if (!list) {
      void Promise.resolve().then(() => {
        if (generation.current === read) setPair({ kind: 'none', note: '这个工作区不提供已保存的用户策略。' })
      })
      return () => { generation.current += 1 }
    }
    const settled = (next: Pair) => { if (generation.current === read) setPair(next) }
    list().then(strategies => {
      const left = experimentRecordsForJob(strategies, leftJobId)
      const right = experimentRecordsForJob(strategies, rightJobId)
      if (left.length > 1 || right.length > 1) {
        settled({ kind: 'none', note: '其中一个运行匹配到多条冻结记录，无法确定唯一来源，这里不替你选择，也不显示冻结修订。' })
      } else if (left.length === 0 || right.length === 0) {
        settled({ kind: 'none', note: '没有同一个已保存用户策略同时冻结了这两个运行。' })
      } else if (left[0].userStrategyId !== right[0].userStrategyId) {
        settled({ kind: 'none', note: '这两个运行分别由不同的已保存用户策略冻结，没有共同来源策略。' })
      } else {
        settled({ kind: 'owner', record: left[0], leftRevision: left[0].revision, rightRevision: right[0].revision })
      }
    }).catch(error => {
      settled({
        kind: 'none',
        note: `${error instanceof Error ? error.message : '策略关联读取失败。'} · 这次关联读取失败了，上面的正式对比不受影响。`,
      })
    })
    return () => { generation.current += 1 }
  }, [api, leftJobId, rightJobId])

  return <section className="panel association-panel" aria-label="对比策略关联">
    <header><h2>对比策略关联</h2><span>只读 · 不启动任何运行</span></header>
    {pair.kind === 'reading' && <p className="muted">正在读取冻结了这两个运行的已保存策略……</p>}
    {pair.kind === 'none' && <>
      <p className="muted">{pair.note}</p>
      <p><Link to="/strategies">打开策略列表</Link></p>
    </>}
    {pair.kind === 'owner' && <>
      <p><strong>{pair.record.name}</strong></p>
      <p><span>{`运行 A · ${frozenRevisionText(pair.leftRevision)}`}</span> · <span>{`运行 B · ${frozenRevisionText(pair.rightRevision)}`}</span></p>
      {/* The way back names both runs, in the order this comparison shows them, so
          the strategy reopens on A then B instead of resetting the evidence. */}
      <p><Link to={`/strategies/${encodeURIComponent(pair.record.userStrategyId)}?evidence_a=${encodeURIComponent(leftJobId)}&evidence_b=${encodeURIComponent(rightJobId)}`}>返回此策略继续研究</Link></p>
    </>}
  </section>
}

function BacktestDetail({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  const [job, setJob] = useState<BacktestJob | null>(null)
  const [report, setReport] = useState<BacktestReport | null>(null)
  const [analytics, setAnalytics] = useState<TradeAnalytics | null>(null)
  const [path, setPath] = useState<EquityPath | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [associationRefresh, setAssociationRefresh] = useState(0)
  // One in-flight stop request, so the control cannot be asked twice while the
  // controller is still accepting or confirming the first one.
  const [stopping, setStopping] = useState(false)
  const refreshGeneration = useRef(0)

  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current
    setReport(null)
    setPath(null)
    setAnalytics(null)
    setError(null)
    try {
      const current = await api.getBacktest(jobId)
      if (generation !== refreshGeneration.current) return null
      setJob(current); setDisconnected(false)
      if (current.status === 'succeeded' && current.report_ready) {
        const currentReport = await api.getReport(jobId)
        if (generation !== refreshGeneration.current) return null
        if (!current.engine_run_id || currentReport.run_id !== current.engine_run_id) {
          throw new ApiFailure('report_identity_conflict', 'Verified report identity does not match this job')
        }
        setReport(currentReport)
        const currentAnalytics = await loadTradeAnalytics(api, current)
        if (generation === refreshGeneration.current) setAnalytics(currentAnalytics)
        const currentPath = await loadPath(api, current)
        if (generation === refreshGeneration.current) setPath(currentPath)
      }
      return current.status
    } catch (caught) {
      if (generation !== refreshGeneration.current) return null
      setReport(null)
      if (caught instanceof Error) setError(caught.message)
      if (!(caught instanceof ApiFailure)) setDisconnected(true)
      return 'failed'
    }
  }, [api, jobId])

  useEffect(() => {
    let active = true; let timer: number | undefined
    const poll = async () => {
      const status = await refresh()
      // `stopping` is an accepted request, not an outcome: the page keeps reading
      // until the controller confirms `stopped` (or the run finishes first).
      if (active && (status === 'accepted' || status === 'running' || status === 'stopping')) {
        timer = window.setTimeout(poll, 150)
      }
    }
    void poll()
    return () => { active = false; refreshGeneration.current += 1; if (timer !== undefined) window.clearTimeout(timer) }
  }, [refresh])

  if (!job) return <div className="report-page">
    <PageTitle title="正式报告" subtitle="正在读取工作区运行与正式报告。" />
    {/* The way back is part of the loading and failure states too, so a report that
        never arrives is still a page the reader can leave. */}
    <div className="result-toolbar"><Link to="/backtests">回测列表</Link></div>
    {disconnected && <section className="notice error">Local service is unreachable</section>}
    {error && <section className="notice error">{error}</section>}
  </div>
  const economics = report?.economics
  // Each amount is shown with the unit the report recorded for that amount; the
  // run-level currency is the fallback for an amount that recorded none, and a
  // unit no part of the report recorded is named as missing rather than assumed
  // to be USD. A v1 report carries the run's currency once, a v2/v3 report
  // carries it on each amount.
  const amount = (value: Money | null | undefined) => {
    if (!value) return 'not available'
    const currency = value.currency ?? economics?.currency ?? null
    return currency ? `${value.amount} ${currency}` : `${value.amount} · currency not recorded by the report`
  }
  const strategy = report?.source?.strategy.id ?? report?.scenario?.strategy_id
  const instrument = report?.source?.instrument ?? report?.scenario?.instrument
  const stop = async () => {
    if (!api.stopBacktest || stopping) return
    setStopping(true)
    setError(null)
    try {
      setJob(await api.stopBacktest(jobId))
    } catch (caught) {
      // A refused stop is reported beside the control that made it, and the page
      // re-reads the job rather than assuming what the refusal left behind.
      if (caught instanceof Error) setError(caught.message)
      void refresh()
    } finally {
      setStopping(false)
    }
  }
  // Only a live run offers a stop; the controller answers a terminal job with its
  // own unchanged record, so the control disappears with the state.
  const stoppable = job.status === 'accepted' || job.status === 'running' || job.status === 'stopping'
  return <div className="report-page">
    <PageTitle title="正式报告" subtitle={`${job.scenario_id} · 离线回测正式报告`} status={job.status} />
    {disconnected && <section className="notice error">Local service is unreachable · loaded job status may be historical</section>}
    {error && <section className="notice error">{error}</section>}
    {job.message && <section className="notice error"><strong>{job.error_code}</strong> · {job.message}</section>}
    <div className="result-toolbar"><Link to="/backtests">回测列表</Link><button onClick={() => { setAssociationRefresh(value => value + 1); void refresh() }}>刷新</button>{api.stopBacktest && stoppable && <button className="danger stop-run" disabled={stopping} onClick={() => void stop()}>停止运行</button>}</div>
    {/* Keyed on the job and on the explicit refresh: another route — or a refresh —
        mounts a fresh association that starts out reading, instead of carrying this
        job's strategy across. The read below is independent of the report's own
        load, and the explicit refresh re-reads it without touching the report. */}
    <StrategyAssociation key={`${jobId}:${associationRefresh}`} api={api} jobId={jobId} />
    {economics ? <>
      <section className="metrics">
        <Metric label="期末权益" value={amount(economics.equity)} />
        <Metric label="净盈亏" value={amount(economics.net_pnl)} />
        <Metric label="总收益率" value={percent(economics.total_return.value)} />
        <Metric label="手续费" value={amount(economics.fees)} />
      </section>
      <section className="panel result-limits" aria-label="本结果不展示什么">
        <header><h2>本结果不展示什么</h2><span>一次确定性运行</span></header>
        <dl className="summary-list">
          <div><dt>本次运行记录数</dt><dd>{job.input_identity.record_count} 条记录{job.input_identity.record_count <= 6 ? ' · 小样本：仅用于流程验证，不作稳健性结论' : ''}</dd></div>
          <div><dt>净盈亏</dt><dd>包含窗口结束时仍持有头寸的估值，不是已实现利润。</dd></div>
          <div><dt>稳健性</dt><dd>单一窗口，无基准对比，无样本外区间。</dd></div>
        </dl>
        {/* The reporter's own full limitations stay readable in one place; the
            concise lines above are the same facts, not a summary that replaces them. */}
        <details className="disclosure"><summary>完整限制说明</summary>
          <p className="muted">These are the limits the reporter itself records. The formal report is the authority for every figure above; nothing here is extrapolated from it.</p>
          <dl className="summary-list">
            <div><dt>Position quantity</dt><dd>A number of units in one ending position, not a count of trades.</dd></div>
            <div><dt>Fees</dt><dd>Charged by the recorded commission policy, not observed from a venue.</dd></div>
            <div><dt>Robustness</dt><dd>One window, no benchmark comparison, no out-of-sample period. A chronological holdout is a separate evaluation with its own explicit action.</dd></div>
            <div><dt>Statistical claims</dt><dd>None are made here: no Sharpe ratio, no confidence interval and no significance test exists for this run.</dd></div>
          </dl>
        </details>
      </section>
    </> : <section className="panel"><p className="muted">{
      job.status === 'failed' || job.status === 'interrupted' || job.status === 'stopped'
        ? 'No success report is available.'
        : job.status === 'succeeded'
          ? error ? 'No current verified report is available.' : 'Loading the formal report.'
          : job.status === 'stopping'
            ? 'This run is stopping. No result of it will be published, and its staged files are never served as a report.'
            : 'The installed engine is running in its own worker process. This page will refresh automatically.'
    }</p></section>}
    {/* The technical identity is outside the report branch on purpose: the job and
        engine a page is about are known from the route and the job record, so they
        stay readable while the report is still loading or has failed. The fields
        that only a report can supply stay named unavailable until one arrives. */}
    <details className="disclosure"><summary>技术身份</summary>
      <section className="panel identity-panel"><dl className="summary-list">
        <div><dt>Web job</dt><dd>{job.job_id}</dd></div><div><dt>Engine run_id</dt><dd>{job.engine_run_id ?? 'not available'}</dd></div><div><dt>Strategy</dt><dd>{strategy ?? 'not available'}</dd></div><div><dt>Instrument</dt><dd>{instrument ? `${instrument.venue}:${instrument.symbol}` : 'not available'}</dd></div>
        {job.input_snapshot?.research_input && <><div><dt>Dataset</dt><dd>{job.input_snapshot.research_input.dataset_id}</dd></div><div><dt>Source SHA-256</dt><dd>{job.input_snapshot.research_input.source_sha256}</dd></div></>}
      </dl></section>
    </details>
    {economics && <>
      {path ? <EquityCurve analysis={path} /> : <section className="panel">{pathUnavailable(job)}</section>}
      <section className="panel report-economics" aria-label="执行与经济事实">
        <header><h2>执行与经济事实</h2><span>正式报告只读字段</span></header>
        <dl className="summary-list">
          <div><dt>Initial cash</dt><dd>{amount(economics.initial_funding)}</dd></div>
          <div><dt>Ending cash</dt><dd>{amount(economics.ending_cash[0] ?? { amount: '0' })}</dd></div>
          <div><dt>Position quantity</dt><dd>{economics.ending_positions[0]?.quantity ?? '0'}</dd></div>
        </dl>
        <div className="backtest-grid">
          <section className="panel"><header><h2>Execution</h2><span>Canonical strings</span></header><dl className="summary-list">
            {report?.schema === 'ea.backtest-report.v3' ? <MultiRoundTripExecution economics={report.economics} /> : report?.schema === 'ea.backtest-report.v2' ? <RoundTripExecution economics={report.economics} /> : report?.schema === 'ea.backtest-report.v1' ? <>
            <div><dt>Order</dt><dd>{report.economics.execution.order ? `${report.economics.execution.order.side} ${report.economics.execution.order.quantity}` : 'none'}</dd></div>
            <div><dt>Fill quantity</dt><dd>{report.economics.execution.fill?.quantity ?? 'none'}</dd></div><div><dt>Fill price</dt><dd>{report.economics.execution.fill?.price ?? 'none'}</dd></div>
            </> : null}
            <div><dt>Valuation price</dt><dd>{economics.valuation.price}</dd></div><div><dt>Position value</dt><dd>{economics.valuation.position_value}</dd></div>
            <div><dt>Counts</dt><dd><span>Orders {economics.counts.orders}</span> · <span>Fills {economics.counts.fills}</span></dd></div>
          </dl></section>
          <section className="panel"><header><h2>Artifacts</h2><span>Read-only downloads</span></header><div className="downloads"><a href={api.artifactUrl(jobId, 'report.json')}>Download report.json</a><a href={api.artifactUrl(jobId, 'summary.txt')}>Download summary.txt</a>{path && <a href={api.artifactUrl(jobId, 'equity-path.json')}>Download equity-path.json</a>}</div><p className="muted">Net P&amp;L may include open-position valuation; it is not presented as realized profit.</p></section>
        </div>
      </section>
    </>}
    {job.input_snapshot && <p><span>{commissionLabel(job.input_snapshot.scenario.execution?.commission)}</span> · <span>{slippageLabel(job.input_snapshot.scenario.execution?.slippage)}</span> · <span>{latencyLabel(job.input_snapshot.scenario.execution?.latency)}</span></p>}
    {job.schema === 'ea.local-web-job.v5' && <p>Round trip limit: {String(job.input_snapshot?.scenario.strategy.max_round_trips)}</p>}
    {report && job.strategy_descriptor?.research_visible && <p><Link to={`/holdouts/new/${job.job_id}`}>Evaluate chronological holdout</Link></p>}
    {report && <TradeAnalyticsPanel analytics={analytics} />}
    {report && api.createCandidate && <CandidateCreate api={api} jobId={jobId} />}
  </div>
}

function HoldoutHistory({ api }: { api: ApiAdapter }) {
  const [items, setItems] = useState<ChronologicalHoldout[]>([])
  const [error, setError] = useState('')
  useEffect(() => { let active = true; api.listHoldouts().then(value => { if (active) setItems(value) }).catch(caught => { if (active) setError(String(caught)) }); return () => { active = false } }, [api])
  return <><PageTitle title="Chronological Holdout" subtitle="Saved chronological holdout evaluations. Select a completed run to create one." />
    {error && <p className="notice error">{error}</p>}
    <section className="panel"><Link to="/backtests">Choose a source run</Link><ul>{items.map(item => <li key={item.validation_id}><Link to={`/holdouts/${item.validation_id}`}>{item.validation_id}</Link> · {item.created_at}</li>)}</ul></section></>
}

function FrozenParameters({ job }: { job: BacktestJob }) {
  return <section className="panel"><header><h2>Frozen Parameters</h2><span>Frozen from source</span></header><dl className="summary-list">
    {Object.entries(jobParameters(job)).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}
  </dl></section>
}

function HoldoutEvidence({ role, job, report, path }: { role: string } & ComparedRun) {
  const data = job.input_snapshot?.scenario.data
  const economics = report?.economics
  // Each amount carries the unit the report recorded for that amount; the run's
  // currency and then the frozen scenario funding are the fallbacks for an amount
  // that recorded none, and a missing unit is named rather than assumed to be USD.
  const unit = (value: Money | null | undefined) => {
    if (!value) return 'not available'
    const currency = value.currency ?? economics?.currency ?? job.input_snapshot?.scenario.funding.currency ?? null
    return currency ? `${value.amount} ${currency}` : `${value.amount} · currency not recorded by the report`
  }
  return <section className="panel"><header><h2>{role}</h2><span>{job.status}</span></header><dl className="summary-list">
    <div><dt>Scenario</dt><dd>{job.scenario_id}</dd></div>
    {job.input_snapshot?.research_input && <div><dt>Dataset</dt><dd>{job.input_snapshot.research_input.dataset_id}</dd></div>}
    <div><dt>Window (UTC)</dt><dd>{data ? `${data.start_utc} → ${data.end_utc}` : 'Unavailable'}</dd></div>
    <div><dt>Data fingerprint</dt><dd>{data?.fingerprint.sha256 ?? 'Unavailable'}</dd></div>
    <div><dt>Record count</dt><dd>{data?.fingerprint.record_count ?? 'Unavailable'}</dd></div>
    <div><dt>Commission assumption</dt><dd>{commissionLabel(job.input_snapshot?.scenario.execution?.commission)}</dd></div>
    <div><dt>Slippage assumption</dt><dd>{slippageLabel(job.input_snapshot?.scenario.execution?.slippage)}</dd></div>
    <div><dt>Execution latency</dt><dd>{latencyLabel(job.input_snapshot?.scenario.execution?.latency)}</dd></div>
    <div><dt>Fees</dt><dd>{economics ? unit(economics.fees) : 'No report'}</dd></div>
    <div><dt>Final equity</dt><dd>{economics ? unit(economics.equity) : 'No report'}</dd></div>
    <div><dt>Net P&amp;L</dt><dd>{economics ? unit(economics.net_pnl) : 'No report'}</dd></div>
    <div><dt>Total return</dt><dd>{economics ? percent(economics.total_return.value) : 'No report'}</dd></div>
    <div><dt>Maximum Drawdown</dt><dd>{path ? `${path.max_drawdown.amount} ${path.currency} / ${pathPercent(path.max_drawdown.ratio)}` : pathUnavailable(job)}</dd></div>
  </dl><Link to={`/backtests/${job.job_id}`}>Open {role} formal run</Link></section>
}

async function holdoutRun(api: ApiAdapter, id: string) {
  const job = await api.getBacktest(id)
  let report: BacktestReport | null = null
  if (job.status === 'succeeded') {
    try { const value = await api.getReport(id); if (value.run_id === job.engine_run_id) report = value } catch { /* Unavailable evidence stays unavailable. */ }
  }
  return { job, report, path: report ? await loadPath(api, job) : null }
}

function HoldoutCreatorRoute({ api }: { api: ApiAdapter }) {
  const { jobId = '' } = useParams()
  return <HoldoutCreator key={jobId} api={api} jobId={jobId} />
}

function HoldoutCreator({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  const navigate = useNavigate()
  const [source, setSource] = useState<Awaited<ReturnType<typeof holdoutRun>> | null>(null)
  const [candidates, setCandidates] = useState<ScenarioSummary[]>([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    Promise.all([holdoutRun(api, jobId), api.holdoutScenarios(jobId)]).then(([run, items]) => {
      if (active) { setSource(run); setCandidates(items) }
    }).catch(caught => { if (active) setError(String(caught)) })
    return () => { active = false }
  }, [api, jobId])
  const candidate = candidates.find(item => (item.input_identity?.dataset_id ? `${item.scenario_id}|${item.input_identity.dataset_id}` : item.scenario_id) === selected)
  const submit = async () => {
    setBusy(true); setError('')
    try {
      // The identity shown beside the selected target is the input the user is
      // confirming, so it is sent exactly as displayed: the server refuses a target
      // that changed between this selection and the click rather than running data
      // the user never saw.
      const relation = await api.createHoldout({ source_job_id: jobId, scenario_id: candidate!.scenario_id, input_identity: candidate!.input_identity!, ...(candidate?.input_identity?.dataset_id ? { dataset_id: candidate.input_identity.dataset_id, source_sha256: candidate.input_identity.source_sha256 } : {}) }); navigate(`/holdouts/${relation.validation_id}`)
    }
    catch (caught) { setError(String(caught)); setBusy(false) }
  }
  return <><PageTitle title="New Chronological Holdout" subtitle="Chronological holdout evaluation using parameters frozen from your selected source." />
    {error && <p className="notice error">{error}</p>}
    {source && <><FrozenParameters job={source.job} /><HoldoutEvidence role="IS" {...source} /></>}
    <section className="panel"><label htmlFor="holdout-scenario">Holdout scenario</label>
      <select id="holdout-scenario" value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select a compatible later registered scenario</option>{candidates.map(item => <option key={(item.input_identity?.dataset_id ? `${item.scenario_id}|${item.input_identity.dataset_id}` : item.scenario_id)} value={(item.input_identity?.dataset_id ? `${item.scenario_id}|${item.input_identity.dataset_id}` : item.scenario_id)}>{item.name}</option>)}</select>
      {source && candidates.length === 0 && <p>No compatible later registered scenario with valid frozen parameters is available.</p>}
      {candidate && !candidate.input_identity && <p className="notice error">This target carries no confirmed input identity, so it cannot be evaluated. Reopen this page to refresh the candidates.</p>}
      {candidate?.summary?.data && <dl className="summary-list"><div><dt>Window (UTC)</dt><dd>{candidate.summary.data.start_utc} → {candidate.summary.data.end_utc}</dd></div><div><dt>Data fingerprint</dt><dd>{candidate.summary.data.fingerprint.sha256}</dd></div><div><dt>Record count</dt><dd>{candidate.summary.data.fingerprint.record_count}</dd></div></dl>}
      <button className="primary" disabled={!selected || !source?.report || !candidate?.input_identity || busy} onClick={() => { void submit() }}>Run chronological holdout</button>
    </section></>
}

function HoldoutDetailRoute({ api }: { api: ApiAdapter }) {
  const { validationId = '' } = useParams()
  return <HoldoutDetail key={validationId} api={api} validationId={validationId} />
}

function HoldoutDetail({ api, validationId }: { api: ApiAdapter; validationId: string }) {
  const [runs, setRuns] = useState<Awaited<ReturnType<typeof holdoutRun>>[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true; let timer: number | undefined
    const refresh = async () => {
      try {
        const relation = await api.getHoldout(validationId)
        const next = await Promise.all([holdoutRun(api, relation.source_job_id), holdoutRun(api, relation.holdout_job_id)])
        if (!active) return
        setRuns(next); setError('')
        if (next.some(run => ['accepted', 'running', 'stopping'].includes(run.job.status))) timer = window.setTimeout(() => { void refresh() }, 500)
      } catch (caught) { if (active) { setRuns([]); setError(String(caught)) } }
    }
    void refresh()
    return () => { active = false; window.clearTimeout(timer) }
  }, [api, validationId])
  return <><PageTitle title="Chronological Holdout" subtitle={`Chronological holdout evaluation · ${validationId}`} />
    <p><Link to="/holdouts">Saved evaluations</Link></p>{error && <p className="notice error">{error}</p>}
    {runs[0] && <FrozenParameters job={runs[0].job} />}
    <div className="backtest-grid">{runs.map((run, index) => <HoldoutEvidence key={run.job.job_id} role={index === 0 ? 'IS' : 'OOS'} {...run} />)}</div>
  </>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <article className="metric-card"><span>{label}</span><strong>{value}</strong></article>
}

export function App({ api = browserApi }: { readonly api?: ApiAdapter }) {
  return <BrowserRouter><Shell api={api} /></BrowserRouter>
}
