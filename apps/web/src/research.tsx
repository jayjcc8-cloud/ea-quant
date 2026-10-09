import { Fragment, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, BacktestReport, ChronologicalHoldout, ScenarioSummary } from './app'
import { AppearanceSettings } from './appearance-settings'
import { GuidedMappingFields, readGuidedDeclaration } from './csv-mapping-form'
import { pathPercent } from './equity-path'
import {
  AVAILABILITY_PROVIDED,
  AVAILABILITY_SIMULATED,
  LAYER_ORDER,
  MAX_UPLOAD_CHARACTERS,
  ResearchRequestError,
  TIME_FORMAT_IDS,
  isMappedPreview,
  researchApi,
  type DeclarationBody,
  type DeclarationCatalog,
  type DeclarationVerdict,
  type LayerName,
  type LocalStrategyCapability,
  type LocalStrategySlot,
  type ModelConfigurationStatus,
  type OfflineHistoryDeclaration,
  type OfflineHistoryExample,
  type OfflineHistoryPreview,
  type OfflineHistoryVersion,
  type PluginCatalog,
  type PluginCatalogEntry,
  type PluginSelection,
  type RecipeResolution,
  type ResearchError,
  type ResearchSpec,
  type ResearchDataScope,
  type ResearchInterpretation,
  type ResearchTask,
  type ResearchTaskStatus,
  type UploadDeclaration,
  type UploadPreviewResponse,
  type UploadVersion,
} from './research-api'

/**
 * Minimal research workspace: idea → registered restrictive spec → explicit
 * confirm → the real installed backtest → the existing chronological holdout
 * and evaluated candidate.
 *
 * Every value on these pages is read from the persisted task record; this
 * client never derives a spec, a result or an outcome, never starts a provider
 * call or a backtest on its own, and never falls back to a default provider.
 *
 * Three optional surfaces sit beside a task, each of them a host verdict this
 * client only reads: the capability selection over the five layers, the
 * controlled upload preview/publish pair, and declaration verification with
 * explicit registration. Publishing a version and registering a declaration are
 * separate user writes; nothing on these pages installs, selects or runs
 * anything by itself.
 */

const POLL_STATUSES: ResearchTaskStatus[] = ['drafting', 'confirmed', 'running']
const NON_RETRYABLE_CODES = new Set(['model_outcome_unknown', 'job_outcome_unknown', 'task_in_flight'])
/** The most the idea field holds, so the most a prepared one may bring with it. */
const IDEA_LIMIT = 4000

/**
 * Display names for the allowlisted criterion fields. This map is presentation
 * only: the field list itself is the host's, and an unknown path is shown as
 * written rather than guessed at.
 */
const CRITERION_FIELD_LABELS: Record<string, string> = {
  'economics.net_pnl.amount': 'Net P&L',
  'economics.realized_pnl.amount': 'Realized P&L',
  'economics.fees.amount': 'Fees paid',
  'economics.total_return.value': 'Total return',
  'economics.counts.fills': 'Fills',
  'economics.counts.orders': 'Orders',
  'economics.completed_round_trips': 'Completed round trips',
}

/** Why the conclusion step did not publish; the run's own result is unaffected. */
const INTERPRETATION_ERROR_EXPLANATIONS: Record<string, string> = {
  model_not_configured: 'No model is configured, so no conclusion call was made. The completed run and its report are unchanged.',
  model_outcome_unknown: 'A conclusion call left this process and its outcome is unproven. The call stays charged and is never re-dispatched automatically.',
  claim_rejected: 'The model tried to record a conclusion that does not follow from the verified report, so nothing was published.',
  no_conclusion_call: 'The model did not record a conclusion, so nothing was published.',
  report_unavailable: 'The verified report could not be read, so no conclusion was published.',
  report_identity_changed: 'The verified report no longer matches the one the conclusion was produced against, so nothing was published.',
  budget_exceeded: 'The research model-call budget has no call left for a conclusion.',
  cancelled_by_user: 'You cancelled before a conclusion was published. The completed run and its report are unchanged.',
  interpretation_schema_unsupported: 'A stored conclusion was written under a semantics this version cannot re-render, so it is not served. The completed run and its report are unchanged.',
}

const STATUS_LABELS: Record<ResearchTaskStatus, string> = {
  drafting: 'drafting',
  awaiting_confirmation: 'awaiting confirmation',
  confirmed: 'confirmed',
  running: 'backtest running',
  succeeded: 'backtest succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
  unsupported: 'unsupported by the registered surface',
}

/**
 * Short headings for the state a task is actually in. They describe the state
 * and nothing more: each one is paired with a sentence that only repeats what
 * the persisted record already settled.
 */
const STATE_HEADINGS: Record<ResearchTaskStatus, string> = {
  drafting: 'Drafting a specification',
  awaiting_confirmation: 'Waiting for your confirmation',
  confirmed: 'Confirmed, not yet dispatched',
  running: 'One backtest running',
  succeeded: 'Backtest succeeded',
  failed: 'This task failed',
  cancelled: 'This task is cancelled',
  unsupported: 'Not executable as stated',
}

const STATE_STATEMENTS: Record<ResearchTaskStatus, string> = {
  drafting: 'The host is drafting one restrictive specification from your idea. Nothing is confirmable yet, and submitting the same idea again is refused as an existing task rather than starting a second one.',
  awaiting_confirmation: 'One draft specification is waiting for your decision. Nothing runs until you confirm it explicitly; the criterion and the frozen budget you are confirming against are beside it.',
  confirmed: 'The specification is confirmed and handed to the installed engine as one backtest. Nothing else is dispatched for this task.',
  running: 'One backtest is running in the installed engine. Refreshing re-reads what the backend persisted; it never re-submits or re-dispatches anything.',
  succeeded: 'The backtest succeeded. Its verified report is the authority for every figure.',
  failed: 'This task stopped with the recorded failure. Whether any step can be retried in place is decided by the backend, not by this page.',
  cancelled: 'No further step is dispatched for this task, and runs that already started keep their own terminal states; a cancel is never a rollback.',
  unsupported: 'The host recorded that this idea cannot be run with the registered strategies and data, so nothing was dispatched and no backtest was started for it.',
}

const criterionText = (criterion: ResearchSpec['criterion']) => criterion
  ? `${CRITERION_FIELD_LABELS[criterion.field] ?? criterion.field} expected ${criterion.expectation}`
  : 'None was stated before the run'

const ERROR_EXPLANATIONS: Record<string, string> = {
  model_not_configured: 'No usable model provider configuration exists, so no proposal could be requested.',
  provider_error: 'The provider call left this process and its outcome was never observed; the call stays charged and is not re-dispatched automatically.',
  model_outcome_unknown: 'A dispatched model call was never observed; the call stays charged and is not re-dispatched automatically.',
  job_outcome_unknown: 'A dispatched backtest submission was never confirmed; the slot stays charged and nothing is re-submitted automatically.',
  interrupted_before_dispatch: 'The attempt was interrupted before anything was dispatched, so the reserved charge was released once.',
  task_in_flight: 'An operation is already in flight for this task; wait for it to settle before acting.',
  budget_exceeded: 'The frozen research budget for this task is exhausted.',
  no_tool_call: 'The model did not propose a registered spec or declare the idea unsupported.',
  too_many_rounds: 'The model did not reach a valid proposal within the allowed rounds.',
  scenario_invalid: 'The confirmed spec no longer validates against the registered scenario catalog.',
  no_confirmed_spec: 'No confirmed spec exists for this task.',
}

const messageOf = (caught: unknown, fallback: string) =>
  caught instanceof Error && caught.message ? caught.message : fallback

// The host's own layer → kind mapping and layer order (src/ea/plugin/selection.py).
const LAYER_KINDS: Record<LayerName, string> = {
  model: 'model',
  data: 'data',
  strategy: 'strategy',
  validator: 'validator',
  backtest: 'backtest-method',
}

const LAYER_LABELS: Record<LayerName, string> = {
  model: 'Model',
  data: 'Data',
  strategy: 'Strategy',
  validator: 'Validator',
  backtest: 'Backtest method',
}

// One selectable option is one exact plugin id and one exact package version.
// Neither an identifier nor a version can contain '@', so the value round-trips.
const optionValue = (entry: PluginCatalogEntry) => `${entry.plugin_id}@${entry.package_version}`

function componentOf(value: string | undefined): { plugin_id: string; package_version: string } | null {
  if (!value) return null
  const index = value.lastIndexOf('@')
  if (index < 1 || index === value.length - 1) return null
  return { plugin_id: value.slice(0, index), package_version: value.slice(index + 1) }
}

const valueLabel = (value: string) => {
  const component = componentOf(value)
  return component ? `${component.plugin_id} · ${component.package_version}` : value
}

function selectionOf(values: Partial<Record<LayerName, string>>): PluginSelection {
  const selection: PluginSelection = {}
  for (const layer of LAYER_ORDER) {
    const component = componentOf(values[layer])
    if (component) selection[layer] = component
  }
  return selection
}

function valuesOf(selection: PluginSelection | null | undefined): Partial<Record<LayerName, string>> {
  const values: Partial<Record<LayerName, string>> = {}
  for (const layer of LAYER_ORDER) {
    const component = selection?.[layer]
    if (component) values[layer] = `${component.plugin_id}@${component.package_version}`
  }
  return values
}

function Disclosure({ title, subtitle, className, children }: { title: string; subtitle?: string; className?: string; children: ReactNode }) {
  return <details className={className ? `disclosure ${className}` : 'disclosure'}>
    <summary><strong>{title}</strong>{subtitle && <span>{subtitle}</span>}</summary>
    <div className="disclosure-body">{children}</div>
  </details>
}

const latestSpec = (task: ResearchTask): ResearchSpec | null =>
  task.spec_confirmed.at(-1)?.spec ?? task.spec_draft

// Mirrors the backend's retry gate: a failure whose attempt left the process is
// never retried in place, whether it was classified as unknown or observed as
// unknown after dispatch.
const canRetry = (task: ResearchTask) =>
  task.status === 'failed'
  && task.error !== null
  && !NON_RETRYABLE_CODES.has(task.error.code)
  && task.last_model?.outcome !== 'unknown_after_dispatch'

function parameterDescription(name: string, value: string | number): string {
  if (name !== 'rule' || typeof value !== 'string') return `${name}: ${value}`
  try {
    const rule = JSON.parse(value)
    const operand = (item: { kind?: string; field?: string; window?: number }) =>
      item.kind === 'price' && item.field === 'close' ? 'Close' : item.kind === 'sma' && Number.isInteger(item.window) ? `SMA(${item.window})` : null
    const left = operand(rule.when.left), right = operand(rule.when.right)
    const operator: Record<string, string> = { gt: 'is above', lt: 'is below', crosses_above: 'crosses above', crosses_below: 'crosses below' }
    if (rule.action === 'ENTER_LONG' && left && right && operator[rule.when.op]) return `Enter long when ${left} ${operator[rule.when.op]} ${right}`
  } catch { /* Preserve unknown contracts exactly. */ }
  return `${name}: ${value}`
}

function SpecFacts({ spec }: { spec: ResearchSpec }) {
  return <>
    <dl className="summary-list">
      <div><dt>Registered strategy</dt><dd>{spec.strategy_id}</dd></div>
      <div><dt>Instrument</dt><dd>{spec.instrument.venue}:{spec.instrument.symbol}</dd></div>
      <div><dt>Registered scenario</dt><dd>{spec.scenario_id ?? 'Resolved from the registered catalog at confirmation'}</dd></div>
      <div><dt>Research data</dt><dd>{spec.dataset_id ?? 'Registered scenario data'}</dd></div>
      <div><dt>Initial cash</dt><dd>{spec.initial_cash}</dd></div>
      <div><dt>Parameters</dt><dd>{Object.entries(spec.parameters).map(([name, value]) => parameterDescription(name, value)).join(' · ') || 'Registered defaults'}</dd></div>
      {spec.criterion && <div><dt>Frozen criterion</dt><dd>{CRITERION_FIELD_LABELS[spec.criterion.field] ?? spec.criterion.field} expected {spec.criterion.expectation} · frozen before the run</dd></div>}
      {!spec.criterion && <div><dt>Frozen criterion</dt><dd>None was stated, so this run cannot be concluded for or against the idea.</dd></div>}
      {spec.selection && <div><dt>Selected capabilities</dt><dd>{LAYER_ORDER.flatMap(layer => {
        const component = spec.selection?.[layer]
        return component ? [`${LAYER_LABELS[layer]}: ${component.plugin_id} ${component.package_version}`] : []
      }).join(' · ') || 'Host default route'}</dd></div>}
    </dl>
  </>
}

/** The drafted reasoning behind a spec: detail, not a decision a reader acts on. */
function SpecRationale({ spec }: { spec: ResearchSpec }) {
  return <>
    <h3>Rationale</h3>
    <p className="muted">Recorded when the proposal was drafted. Current backtest and holdout states appear in the linked evidence below.</p>
    <p>{spec.rationale}</p>
    <h3>Assumptions</h3>
    {spec.assumptions.length
      ? <ul className="assumption-list">{spec.assumptions.map(assumption => <li key={assumption}>{assumption}</li>)}</ul>
      : <p className="muted">No assumptions were stated for this draft.</p>}
    <p className="muted">This spec references the registered catalog only. Confirming it executes no generated code and creates no order authority.</p>
  </>
}

function ModelConfigurationPanel({ status, error }: { status: ModelConfigurationStatus | null; error: string | null }) {
  if (error) return <section className="panel" aria-label="模型提供方配置"><header><h2>模型提供方</h2><span>状态不可用</span></header><p className="notice error">{error}</p></section>
  if (!status) return <section className="panel" aria-label="模型提供方配置"><header><h2>模型提供方</h2><span>读取中</span></header><p className="muted">正在读取后端模型配置状态…</p></section>
  const capabilities = status.capabilities ?? null
  const missing = status.missing_capabilities ?? []
  const capability = (name: 'tool_use' | 'structured_output' | 'usage', whenDeclared: string, whenAbsent: string) =>
    capabilities ? (capabilities[name] ? whenDeclared : whenAbsent) : '未配置'
  // The visible part is what an operator wrote and what this page did not: the
  // configuration, the credential, and the fact that no request was sent from
  // here. An absent configuration_present or credential_available stays 未知 —
  // never a false, never a zero. The protocol, the capability declarations and
  // the settings file are the technical detail behind one native disclosure; a
  // required capability the protocol does not declare stays an alert outside it.
  return <section className="panel" aria-label="模型提供方配置">
    <header><h2>模型提供方</h2><span>{status.configured ? '已配置' : '配置不可用'}</span></header>
    <p className={status.configured ? 'muted' : 'notice error'}>{status.configured
      ? '起草会把想法发送给已配置的提供方；不使用任何默认提供方。'
      : `研究无法起草规范：${status.configuration_error ?? '模型提供方未配置'}。请在后端配置文件里配置；本页不会回退到默认提供方。`}</p>
    <dl className="summary-list">
      <div><dt>是否已配置</dt><dd>{status.configuration_present === undefined ? '未知' : status.configuration_present ? '是' : '否'}</dd></div>
      <div><dt>凭据是否可用</dt><dd>{status.credential_available === undefined ? '未知' : status.credential_available ? '是' : '否'}</dd></div>
      <div><dt>提供方</dt><dd>{status.provider ?? '未配置'}</dd></div>
      <div><dt>模型</dt><dd>{status.model ?? '未配置'}</dd></div>
      <div><dt>端点</dt><dd>{status.endpoint ?? '未配置'}</dd></div>
      <div><dt>真实请求校验</dt><dd>本页未发送验证请求；成功调用请查看策略中的调用记录</dd></div>
    </dl>
    <p className="muted">配置可用不代表连接或协议兼容性已验证。</p>
    <details className="disclosure"><summary>协议、能力声明与配置文件</summary>
      <dl className="summary-list">
        <div><dt>协议</dt><dd>{status.protocol ?? '未配置'}</dd></div>
        <div><dt>配置文件</dt><dd>{status.settings_file}</dd></div>
        <div><dt>必需能力</dt><dd>{(status.required_capabilities ?? []).map(name => `${name} · ${missing.includes(name) ? '缺失' : '已声明'}`).join(' · ') || '未配置'}</dd></div>
        <div><dt>工具调用</dt><dd>{capability('tool_use', '已声明', '缺失')}</dd></div>
        <div><dt>结构化输出</dt><dd>{capability('structured_output', '已声明', '未声明')}</dd></div>
        <div><dt>用量回报</dt><dd>{capability('usage', '已声明', '未声明')}</dd></div>
      </dl>
      {status.endpoint && <p className="muted">自定义端点必须实现所报告的协议；不同的地址不假定兼容。</p>}
    </details>
    {missing.length > 0 && <p role="alert" className="notice error">必需能力未声明：{missing.join('、')}。当前配置无法起草，未发送模型请求。</p>}
  </section>
}

function UsagePanel({ task }: { task: ResearchTask }) {
  const reservation = task.backtest_reservation ?? task.reservation
  const unavailableCalls = task.usage.unavailable_model_calls ?? 0
  const tokenCount = (reported: number) => unavailableCalls === 0 ? reported
    : unavailableCalls === task.usage.model_calls ? 'Unknown (no usage reported)'
      : `${reported} reported (partial)`
  return <section className="panel" aria-label="Frozen budget and measured usage">
    <header><h2>Frozen budget and measured usage</h2><span>Recorded by the backend</span></header>
    <dl className="summary-list">
      <div><dt>Model calls</dt><dd>{task.usage.model_calls} of {task.budget.max_model_calls}</dd></div>
      <div><dt>Backtests</dt><dd>{task.usage.backtests} of {task.budget.max_backtests}</dd></div>
      <div><dt>Reported input tokens</dt><dd>{tokenCount(task.usage.input_tokens)}</dd></div>
      <div><dt>Reported output tokens</dt><dd>{tokenCount(task.usage.output_tokens)}</dd></div>
      <div><dt>Charged calls</dt><dd>{task.spent_calls.length ? task.spent_calls.join(', ') : 'None'}</dd></div>
      <div><dt>Last model result</dt><dd>{task.last_model ? `${task.last_model.model} · ${task.last_model.outcome} · ${task.last_model.at}` : 'None observed'}</dd></div>
    </dl>
    {!!task.usage.unavailable_model_calls && <p className="muted">Token usage unavailable for {task.usage.unavailable_model_calls} model calls. Reported totals are incomplete; these calls remain charged.</p>}
    {reservation && <p className="muted">Reservation {reservation.kind} #{reservation.seq} is {reservation.status}. A pending reservation may be released; a dispatched one stays charged and is never re-submitted automatically.</p>}
    {task.runtime_diagnostic && <p role="alert" className="notice error">{task.runtime_diagnostic}</p>}
  </section>
}

/** One explicit capability selection across the five layers, verified by the host. */
function CapabilitySelector({ api, values, change, disabled = false }: {
  api: ApiAdapter
  values: Partial<Record<LayerName, string>>
  change: (values: Partial<Record<LayerName, string>>) => void
  disabled?: boolean
}) {
  const request = api.pluginCatalog
  const [catalog, setCatalog] = useState<PluginCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [verdict, setVerdict] = useState<RecipeResolution | null>(null)
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!request) return
    let active = true
    request().then(next => { if (active) { setCatalog(next); setError(null) } })
      .catch(caught => { if (active) setError(messageOf(caught, 'The host capability catalog is unavailable.')) })
    return () => { active = false }
  }, [request])

  if (!request) return null

  // A version published, or a declaration registered, after this list was read is
  // offered only after this explicit refresh. It replaces the catalog and nothing
  // else: a value already chosen for a layer is kept exactly as it is, including
  // when the refreshed catalog no longer offers it.
  const refresh = async () => {
    if (!request || refreshing) return
    setRefreshing(true)
    try { setCatalog(await request()); setError(null) }
    catch (caught) { setError(messageOf(caught, 'The host capability catalog is unavailable.')) }
    finally { setRefreshing(false) }
  }

  const selection = selectionOf(values)
  const selected = Object.keys(selection).length
  const usable = (kind: string) => (catalog?.plugins ?? []).filter(entry => entry.kind === kind && entry.usable)
  const verify = async () => {
    if (!api.resolveRecipe || busy) return
    setBusy(true); setError(null); setVerdict(null)
    try { setVerdict(await api.resolveRecipe(selection)) }
    catch (caught) { setError(messageOf(caught, 'The selection could not be verified by this host.')) }
    finally { setBusy(false) }
  }

  // The host's catalog is what decides which versions exist. Until it has
  // answered, no option exists to select, so this offers no select at all rather
  // than an empty one a user could read as "this host offers nothing".
  if (catalog === null) return <div className="capability-selector">
    {error
      ? <>
        <p className="notice error">{error}</p>
        <div className="actions"><button type="button" disabled={refreshing} onClick={() => { void refresh() }}>{refreshing ? 'Refreshing…' : 'Refresh capabilities'}</button></div>
      </>
      : <p className="muted">Reading the host capability catalog…</p>}
  </div>

  return <div className="capability-selector">
    <p className="muted">Every layer is optional and names one exact version; a range or a tag is not selectable. The host verifies and freezes the selection before anything runs, and an unselected layer keeps the host default route. A version that no longer matches the frozen identity fails the run instead of being replaced by a newer one. An uploaded dataset version is research data only when you explicitly select its Data capability here: with no data layer selected there is no binding to freeze the version's evidence, and a run over that upload is refused.</p>
    <div className="parameter-grid">{LAYER_ORDER.map(layer => {
      const entries = usable(LAYER_KINDS[layer])
      const value = values[layer] ?? ''
      return <div key={layer}>
        <label htmlFor={`capability-${layer}`}>{LAYER_LABELS[layer]} capability</label>
        <select id={`capability-${layer}`} value={value} disabled={disabled}
          onChange={event => {
            const next = { ...values }
            next[layer] = event.target.value
            change(next); setVerdict(null); setError(null)
          }}>
          <option value="">Host default (unselected)</option>
          {entries.map(entry => <option key={optionValue(entry)} value={optionValue(entry)}>{valueLabel(optionValue(entry))}</option>)}
          {value && !entries.some(entry => optionValue(entry) === value) && <option value={value}>{valueLabel(value)} · not usable now</option>}
        </select>
      </div>
    })}</div>
    <div className="actions">
      <button type="button" disabled={disabled || busy || selected === 0 || !api.resolveRecipe} onClick={() => { void verify() }}>{busy ? 'Verifying…' : 'Verify selection'}</button>
      <button type="button" disabled={refreshing} onClick={() => { void refresh() }}>{refreshing ? 'Refreshing…' : 'Refresh capabilities'}</button>
      <span className="muted">{selected === 0 ? 'No layer is selected.' : `${selected} of ${LAYER_ORDER.length} layers selected.`}</span>
    </div>
    <p className="muted">A version you published, or a declaration you registered, is offered here only after you refresh this list. Refreshing changes no selected value and selects nothing for you.</p>
    {verdict && (verdict.usable
      ? <div className="validated" aria-label="Verified selection"><strong>Selection verified by the host</strong>
        <span>{verdict.binding ? `binding ${verdict.binding.binding_sha256.slice(0, 16)}…` : 'Host default route'}</span>
        <small>{verdict.binding ? verdict.binding.components.map(item => `${item.layer}: ${item.plugin_id} ${item.package_version}`).join(' · ') : 'No capability is selected; the host default route is used.'}</small>
      </div>
      : <p className="notice error">{verdict.code ?? 'selection_rejected'}: {verdict.message ?? 'the host refused this selection'}</p>)}
    {error && <p className="notice error">{error}</p>}
  </div>
}

/** The user's selection, and the exact identity frozen for it, as persisted. */
function SelectionEvidence({ task }: { task: ResearchTask }) {
  const selection = latestSpec(task)?.selection ?? task.selection ?? null
  const binding = task.pinned_binding ?? null
  if (!selection && !binding) return null
  const frozen = new Map((binding?.components ?? []).map(item => [item.layer as LayerName, item] as const))
  return <section className="panel" aria-label="Selected capabilities">
    <header><h2>Selected capabilities</h2><span>{binding ? 'Frozen at confirmation' : 'Recorded when the task was created'}</span></header>
    <dl className="summary-list">{LAYER_ORDER.map(layer => {
      const component = frozen.get(layer)
      const chosen = component ? { plugin_id: component.plugin_id, package_version: component.package_version } : selection?.[layer]
      return <div key={layer}><dt>{LAYER_LABELS[layer]}</dt><dd>{chosen ? `${chosen.plugin_id} · ${chosen.package_version}` : 'Host default route'}</dd></div>
    })}</dl>
    {binding && <p className="muted">binding {binding.binding_sha256.slice(0, 24)}… This exact identity is re-verified before every dispatch: a changed catalog, plugin, configuration or declaration fails the run instead of silently substituting another capability.</p>}
    <p className="muted">The model cannot change this selection: it is not a tool argument, and a proposed spec must use exactly the selected strategy and dataset identity.</p>
  </section>
}

/** Edit the selection of one unconfirmed draft; the host re-verifies it. */
function SelectionEditor({ api, task }: { api: ApiAdapter; task: ResearchTask }) {
  const [values, setValues] = useState<Partial<Record<LayerName, string>>>(() => valuesOf(latestSpec(task)?.selection ?? task.selection))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [applied, setApplied] = useState(false)
  const edit = api.editResearchSpec
  if (!edit) return null
  const apply = async () => {
    if (busy) return
    setBusy(true); setError(null); setApplied(false)
    try {
      // An empty selection is sent as {} so the host clears back to its default.
      await edit(task.task_id, { selection: selectionOf(values) })
      setApplied(true)
    } catch (caught) { setError(messageOf(caught, 'The selection was not applied; the persisted draft is unchanged.')) }
    finally { setBusy(false) }
  }
  return <section className="panel" aria-label="Draft capabilities">
    <header><h2>Draft capabilities</h2><span>Editable while the draft is unconfirmed</span></header>
    <CapabilitySelector api={api} values={values} change={value => { setValues(value); setApplied(false); setError(null) }} disabled={busy} />
    <div className="actions"><button type="button" disabled={busy} onClick={() => { void apply() }}>{busy ? 'Applying…' : 'Apply selection to draft'}</button></div>
    {applied && <p className="muted">The persisted draft now names this selection. It is verified again, and frozen into the binding above, at confirmation.</p>}
    {error && <p className="notice error">{error}</p>}
  </section>
}

function DeclarationWorkbench({ api, initial, initialVerdict = null, heading, note, prefix, registerLabel, onRegistered }: {
  api: ApiAdapter
  initial?: DeclarationBody | null
  /** A verdict the host already returned for `initial`; never a client-side judgment. */
  initialVerdict?: DeclarationVerdict | null
  heading: string
  note: string
  prefix: string
  registerLabel: string
  onRegistered?: () => void
}) {
  const text = (body: DeclarationBody | null | undefined) => (body ? JSON.stringify(body, null, 2) : '')
  // The initial body and the host's verdict for it are read once, as this
  // component's starting state. A caller that has a different draft or verdict
  // gives this workbench a different key, so it is mounted against the new one
  // rather than resynchronized afterwards: an edit in progress is never silently
  // overwritten by a later read of the task.
  const [draft, setDraft] = useState(() => text(initial))
  const [verdict, setVerdict] = useState<DeclarationVerdict | null>(initialVerdict)
  const [registered, setRegistered] = useState<DeclarationVerdict | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'validate' | 'register' | null>(null)

  const submit = async (register: boolean) => {
    if (busy) return
    const call = register ? api.registerDeclaration : api.validateDeclaration
    if (!call) return
    let body: DeclarationBody
    try {
      const parsed: unknown = JSON.parse(draft)
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('one JSON object')
      body = parsed as DeclarationBody
    } catch (caught) {
      setError(`A declaration body must be one JSON object: ${messageOf(caught, 'unreadable JSON')}`)
      return
    }
    setBusy(register ? 'register' : 'validate'); setError(null); setRegistered(null)
    try {
      const next = await call(body)
      if (register && next.registered) { setRegistered(next); onRegistered?.() } else setVerdict(next)
    } catch (caught) {
      // A refused registration answers with its own verdict document, not only a code.
      if (caught instanceof ResearchRequestError) setVerdict({ usable: false, code: caught.code, message: caught.message, declaration: null, capability: null, registered: false })
      else setError(messageOf(caught, 'The host could not read this declaration request.'))
    } finally { setBusy(null) }
  }

  const shown = registered ?? verdict
  // Read the identity once, outside the JSX, so a nested field is a named string.
  const capability = shown?.usable ? shown.capability : null
  const identity: Record<string, unknown> = capability?.identity ?? {}
  const declaredRule = typeof identity.declared_rule === 'string' ? identity.declared_rule : null
  const declaredConstraints = identity.validator_constraints
  const declaration = shown?.usable ? shown.declaration : null
  return <div className="declaration-workbench">
    <h3>{heading}</h3>
    <p className="muted">{note}</p>
    <label htmlFor={`${prefix}-declaration-body`}>Declaration body (JSON)</label>
    <textarea id={`${prefix}-declaration-body`} rows={10} spellCheck={false} value={draft} disabled={busy !== null}
      onChange={event => { setDraft(event.target.value); setVerdict(null); setRegistered(null); setError(null) }} />
    <div className="actions">
      <button type="button" disabled={busy !== null || !draft.trim() || !api.validateDeclaration} onClick={() => { void submit(false) }}>{busy === 'validate' ? 'Verifying…' : 'Verify declaration'}</button>
      {verdict?.usable && !registered && api.registerDeclaration && <button type="button" className="primary" disabled={busy !== null} onClick={() => { void submit(true) }}>{busy === 'register' ? 'Registering…' : registerLabel}</button>}
    </div>
    {shown && <section className={shown.usable ? 'validated' : 'notice error'} aria-label="Declaration verdict">
      <strong>{shown.usable ? (shown.registered ? 'Registered by the host' : 'Verified by the host · not installed') : 'Refused by the host'}</strong>
      <span>{shown.usable
        ? 'Nothing is installed, selected or run until you register it explicitly. Registration is add-only and never overwrites an existing identity.'
        : `${shown.code ?? 'declaration_rejected'}: ${shown.message ?? 'the host refused this declaration'}`}</span>
      {declaration && <small>{declaration.declaration_id} · {declaration.kind} · {declaration.package_version} · {declaration.artifact_digest?.slice(0, 24) ?? ''}…</small>}
    </section>}
    {capability && <dl className="summary-list">
      <div><dt>Wraps</dt><dd>{String(identity.wraps ?? 'A host primitive')}</dd></div>
      <div><dt>Grants</dt><dd>{capability.effects.granted.join(', ') || 'No effect beyond local compute'}</dd></div>
      {declaredRule !== null && <div><dt>Declared rule</dt><dd>{declaredRule}</dd></div>}
      {declaredConstraints !== undefined && <div><dt>Declared constraints</dt><dd>{JSON.stringify(declaredConstraints)}</dd></div>}
    </dl>}
    {error && <p className="notice error">{error}</p>}
  </div>
}

function DeclarationCatalogPanel({ api }: { api: ApiAdapter }) {
  const [catalog, setCatalog] = useState<DeclarationCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const request = api.listDeclarations
  const load = useCallback(async () => {
    if (!request) return
    try { setCatalog(await request()); setError(null) }
    catch (caught) { setError(messageOf(caught, 'Registered declarations are unavailable.')) }
  }, [request])
  // The first read is dispatched here and settles asynchronously; this effect
  // installs no state of its own. Every later read is an explicit action: the
  // refresh after a registration below.
  useEffect(() => {
    if (!request) return
    let active = true
    request()
      .then(items => { if (active) { setCatalog(items); setError(null) } })
      .catch(caught => { if (active) setError(messageOf(caught, 'Registered declarations are unavailable.')) })
    return () => { active = false }
  }, [request])
  if (!request || !api.validateDeclaration) return null
  return <div className="declaration-catalog">
    <p className="muted">A declaration is bounded data: it names one already-authorized host primitive and fixes a preset for it. It is never imported or evaluated, it can only make a run stricter, and only your explicit registration installs one.</p>
    {catalog && <dl className="summary-list">
      <div><dt>Supported kinds</dt><dd>{catalog.supported_kinds.join(', ')}</dd></div>
      <div><dt>Validator constraints</dt><dd>{catalog.validator_constraints.join(', ')}</dd></div>
      <div><dt>Registered here</dt><dd>{catalog.declarations.length}{catalog.declaration_root_configured ? '' : ' · this service has no declaration store'}</dd></div>
    </dl>}
    {catalog?.declarations.length === 0 && <p className="muted">No declaration is registered in this workspace yet.</p>}
    <ul className="job-list">{catalog?.declarations.map(item => <li className="job-card" key={item.declaration_id}>
      <div className="job-card-title"><strong>{item.declaration_id}</strong><span className={`status status-${item.usable ? 'succeeded' : 'failed'}`}>{item.usable ? 'usable' : 'refused'}</span></div>
      <small>{item.kind} · {item.package_version} · {item.title}</small>
      <small>{item.usable
        ? `${item.capability ? String(item.capability.identity.wraps ?? '') : ''} · ${item.capability ? item.capability.effects.granted.join(', ') : ''}`
        : (item.reason ? `${item.reason.code}: ${item.reason.message}` : 'not usable in this catalog')}</small>
      <small>{item.artifact_digest}</small>
    </li>)}</ul>
    {error && <p className="notice error">{error}</p>}
    <DeclarationWorkbench api={api} prefix="register" heading="Register one declaration" registerLabel="Register this declaration"
      note="Paste a body you wrote or one a model proposed to you. The host verifies it first; only the click below stores it, and a body the host refuses is never installed."
      onRegistered={() => { void load() }} />
  </div>
}

/** One declaration the model drafted: verified evidence, never an installation. */
function DeclarationDraftPanel({ api, draft }: { api: ApiAdapter; draft: DeclarationVerdict }) {
  if (draft.usable !== true) return <section className="panel" aria-label="Declaration proposal">
    <header><h2>Declaration proposal</h2><span>Refused by the host</span></header>
    <p className="muted">The model proposed a declaration this host refused, so there is nothing here to install. {draft.code ?? 'declaration_rejected'}: {draft.message ?? 'the host refused it'}</p>
  </section>
  return <section className="panel" aria-label="Declaration proposal">
    <header><h2>Declaration proposal</h2><span>Host-verified draft</span></header>
    <p className="muted">The model drafted this declaration and the host verified it against its own primitives. It is evidence for you to read: nothing was installed, selected, replaced or run, and the model has no path to registering it. Registration is the explicit click below, it is add-only, and it never overwrites an existing identity.</p>
    <DeclarationWorkbench key={draft.declaration?.artifact_digest ?? 'draft'} api={api} initial={draft.declaration} initialVerdict={draft} prefix="draft" heading="Proposed declaration body" registerLabel="Register this declaration"
      note="Registering stores this exact verified body in the workspace declaration store; it then appears in the catalog as one more selectable capability." />
  </section>
}

/**
 * One workspace-local strategy package: inspect it statically, then trust it
 * explicitly, or withdraw that trust.
 *
 * Nothing here runs, generates or installs a package: a captured container and the
 * manifest it claims are read by the host, which reports the identity it measured.
 * Trust is a separate, explicit act over exactly that identity — every expected
 * value below is the operator's own reading of the preview — and revocation is
 * immediate. The package, once trusted, runs with this process's own privileges and
 * no sandbox, which the panel states rather than hides.
 */
function LocalStrategyPanel({ api }: { api: ApiAdapter }) {
  // Destructured so the load effect depends on the two calls it makes rather than
  // on the whole adapter object, which a caller may rebuild on every render.
  const { localStrategySlot, listScenarios } = api
  const [slot, setSlot] = useState<LocalStrategySlot | null>(null)
  const [fileName, setFileName] = useState('')
  const [packageBase64, setPackageBase64] = useState<string | null>(null)
  const [manifestText, setManifestText] = useState('')
  const [inspected, setInspected] = useState<LocalStrategyCapability | null>(null)
  const [inspectedFor, setInspectedFor] = useState<string | null>(null)
  const [trusted, setTrusted] = useState(false)
  const [localCompute, setLocalCompute] = useState(false)
  const [baseScenario, setBaseScenario] = useState('')
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [busy, setBusy] = useState<'inspect' | 'trust' | 'revoke' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        if (localStrategySlot) { const current = await localStrategySlot(); if (active) setSlot(current) }
        if (listScenarios) { const list = await listScenarios(); if (active) setScenarios(list.filter(item => item.valid)) }
      } catch (caught) { if (active) setError(messageOf(caught, 'This workspace has no readable local package slot.')) }
    })()
    return () => { active = false }
  }, [localStrategySlot, listScenarios])

  const signature = JSON.stringify([packageBase64, manifestText])
  // Every expected value the trust decision names comes from the preview the
  // operator is looking at, and only while it is still the same preview.
  const fresh = inspected !== null && inspectedFor === signature

  const inspect = async () => {
    if (busy || !api.inspectLocalStrategy || packageBase64 === null) return
    setBusy('inspect'); setError(null); setNotice(null)
    try {
      const manifest = parseDeclaration(manifestText, 'The manifest')
      if ('error' in manifest) { setError(manifest.error); return }
      const capability = await api.inspectLocalStrategy(packageBase64, manifest.value as Record<string, unknown>)
      setInspected(capability); setInspectedFor(signature)
      setTrusted(false); setLocalCompute(false)
    } catch (caught) { setError(messageOf(caught, 'The host refused this package.')) }
    finally { setBusy(null) }
  }

  const trust = async () => {
    if (busy || !api.registerLocalStrategy || !inspected || packageBase64 === null || !trusted) return
    setBusy('trust'); setError(null); setNotice(null)
    try {
      const manifest = parseDeclaration(manifestText, 'The manifest')
      if ('error' in manifest) { setError(manifest.error); return }
      const current = await api.registerLocalStrategy({
        content_base64: packageBase64,
        manifest: manifest.value as Record<string, unknown>,
        trusted: true,
        effects: localCompute ? ['local_compute'] : [],
        base_scenario_id: baseScenario,
        expected_plugin_id: inspected.plugin_id,
        expected_package_version: inspected.package_version,
        expected_manifest_digest: inspected.manifest_digest,
        expected_implementation_digest: `sha256:${inspected.package.artifact_sha256}`,
      })
      setSlot(current)
      setNotice(`Trusted ${current.capability?.plugin_id ?? inspected.plugin_id}: research now offers the scenario ${current.scenario_id}.`)
    } catch (caught) { setError(messageOf(caught, 'The host refused this trust decision.')) }
    finally { setBusy(null) }
  }

  const revoke = async () => {
    if (busy || !api.revokeLocalStrategy) return
    setBusy('revoke'); setError(null); setNotice(null)
    try { setSlot(await api.revokeLocalStrategy()); setNotice('Trust withdrawn. This package is no longer selectable, and no later run can start it.') }
    catch (caught) { setError(messageOf(caught, 'The trust could not be withdrawn.')) }
    finally { setBusy(null) }
  }

  if (!api.localStrategySlot && !api.inspectLocalStrategy) return null
  return <div className="local-package-panel">
    <p className="muted">A local strategy package is a captured <code>.eastrategy</code> container. Inspection reads its identity statically and executes nothing; trusting it is your decision, and from then on it runs with this process's own privileges, in this process, with no sandbox. Revoking stops every later selection and dispatch, and cannot interrupt code that is already running.</p>
    <label htmlFor="local-strategy-file">Package file (.eastrategy)</label>
    <p className="muted">{fileName ? `Captured in this browser as ${fileName}; its bytes are sent unchanged.` : 'No file selected: only the bytes this browser read are ever sent, never a path or a name.'}</p>
    <input id="local-strategy-file" type="file" accept=".eastrategy" disabled={busy !== null} onChange={event => {
      const file = event.target.files?.[0]
      if (!file) return
      setFileName(file.name); setInspected(null); setInspectedFor(null); setError(null); setNotice(null)
      void file.arrayBuffer().then(bytes => setPackageBase64(base64Of(bytes)))
        .catch(() => setError('The selected file could not be read in this browser.'))
    }} />
    <label htmlFor="local-strategy-manifest">Capability manifest (JSON, sealed by the host on inspection)</label>
    <textarea id="local-strategy-manifest" rows={5} spellCheck={false} placeholder={LOCAL_MANIFEST_EXAMPLE} value={manifestText} disabled={busy !== null}
      onChange={event => { setManifestText(event.target.value); setInspected(null); setInspectedFor(null); setNotice(null) }} />
    <div className="actions">
      <button type="button" disabled={busy !== null || packageBase64 === null || !api.inspectLocalStrategy} onClick={() => { void inspect() }}>{busy === 'inspect' ? 'Inspecting…' : 'Inspect package'}</button>
      {slot?.registered && api.revokeLocalStrategy && <button type="button" disabled={busy !== null} onClick={() => { void revoke() }}>{busy === 'revoke' ? 'Revoking…' : 'Revoke trust'}</button>}
    </div>
    {error && <p className="notice error">{error}</p>}
    {notice && <p className="muted">{notice}</p>}

    {slot && <dl className="summary-list" aria-label="Local package slot">
      <div><dt>Slot</dt><dd>{slot.registered ? `${slot.capability?.plugin_id ?? 'unreadable'} · ${slot.capability?.package_version ?? ''}` : 'No package registered'}{slot.revoked ? ' · trust revoked' : ''}</dd></div>
      <div><dt>Trusted now</dt><dd>{String(slot.trusted)} · effects {slot.grant?.effects.join(', ') || 'none'}</dd></div>
      <div><dt>Virtual scenario</dt><dd>{slot.scenario_id} · one slot per workspace; registering again replaces it</dd></div>
      {slot.capability && <div><dt>Strategy / digest</dt><dd>{slot.capability.descriptor.strategy_id} · v{slot.capability.descriptor.strategy_version} · {slot.capability.package.artifact_sha256}</dd></div>}
    </dl>}

    {fresh && inspected && <section className="upload-preview" aria-label="Package inspection">
      <dl className="summary-list">
        <div><dt>Package / version</dt><dd>{inspected.package.package_id} · {inspected.package_version} · package schema {inspected.package.schema_version}</dd></div>
        <div><dt>Strategy</dt><dd>{inspected.descriptor.strategy_id} · v{inspected.descriptor.strategy_version} · {inspected.descriptor.display_name}</dd></div>
        <div><dt>Mandatory local compute</dt><dd>This package runs in this process, with ordinary privileges and no sandbox. {inspected.warning}</dd></div>
        <div><dt>Manifest digest (this host sealed)</dt><dd>{inspected.manifest_digest}</dd></div>
        <div><dt>Implementation digest</dt><dd>sha256:{inspected.package.artifact_sha256}</dd></div>
        <div><dt>Declared parameters</dt><dd>{(inspected.descriptor.parameters ?? []).map(parameter => `${parameter.name} = ${String(parameter.default)}`).join(' · ') || 'None'}</dd></div>
      </dl>
      <div className="parameter-grid">
        <div><label htmlFor="local-strategy-base">Base scenario (data, funding, risk, execution)</label>
          <select id="local-strategy-base" value={baseScenario} disabled={busy !== null} onChange={event => { setBaseScenario(event.target.value); setNotice(null) }}>
            <option value="">Choose a registered scenario…</option>
            {scenarios.map(scenario => <option key={scenario.scenario_id} value={scenario.scenario_id}>{scenario.scenario_id}</option>)}
          </select></div>
      </div>
      <label htmlFor="local-strategy-trusted"><input id="local-strategy-trusted" type="checkbox" checked={trusted} disabled={busy !== null} onChange={event => setTrusted(event.target.checked)} /> I have read these digests and trust this exact identity</label>
      <label htmlFor="local-strategy-compute"><input id="local-strategy-compute" type="checkbox" checked={localCompute} disabled={busy !== null} onChange={event => setLocalCompute(event.target.checked)} /> Grant local_compute (required: reading these bytes is local computation)</label>
      <div className="actions">
        <button type="button" className="primary" disabled={busy !== null || !trusted || !localCompute || !baseScenario || !api.registerLocalStrategy} onClick={() => { void trust() }}>{busy === 'trust' ? 'Registering…' : 'Trust and register'}</button>
      </div>
      <p className="muted">The four expected values are taken from this inspection, so a manifest edited after it, a different file, or a bumped version is refused rather than trusted by approximation. Nothing installs or runs the package here.</p>
    </section>}
  </div>
}

/** One harmless but complete example of a local capability manifest: real field
 * names, real entrypoint, real interface, and a blank artifact_digest, which is the
 * one field this host fills in. No package is behind it; nothing is sent unless the
 * user sends it. The package_id in the plugin_id is the one the package itself must
 * declare: the host derives the capability id and never takes it from the package. */
const LOCAL_MANIFEST_EXAMPLE = JSON.stringify({
  schema_version: 1,
  plugin_id: 'ea.local.strategy.example.package',
  kind: 'strategy',
  package_version: '1.0.0',
  // Blank is the only value this host fills in: it is the digest of this whole body.
  artifact_digest: '',
  interface: { name: 'ea.strategy-contract', major: 1, minor: 0 },
  entrypoint: 'ea.host.adapter.local-strategy',
  provides: ['strategy.research'],
  requires: [],
  dependencies: [],
  effects: ['local_compute'],
  runtime_profile: 'ea.runtime-profile.default-offline-research.v1',
  config_schema: {},
  author: 'whoever wrote the package you captured',
  license: 'the licence you distribute it under',
}, null, 2)

// One harmless, exact example of each declaration: the strict field names the
// mapper accepts, and the ids of the timestamp forms it allows. Nothing here is
// applied unless the user sends it.
const MAPPING_EXAMPLE = JSON.stringify({
  columns: {
    schema_version: 'v',
    venue: 'mic',
    symbol: 'ticker',
    interval_start: 'bar_start',
    interval_end: 'bar_end',
    adjustment: 'adj',
    open: 'o',
    high: 'h',
    low: 'lo',
    close: 'c',
    volume: 'vlm',
    source: 'feed',
    source_sequence: 'seq',
    revision: 'rev',
    available_at: 'seen',
  },
  constants: {},
})
const TIME_EXAMPLE = JSON.stringify({
  timezone: 'UTC',
  formats: {
    interval_start: 'utc-microseconds',
    interval_end: 'utc-microseconds',
    available_at: 'utc-microseconds',
  },
})

/** One pasted declaration, or the reason it is not one. */
function parseDeclaration(text: string, label: string): { value: unknown } | { error: string } {
  try {
    return { value: JSON.parse(text) }
  } catch {
    return { error: `${label} is not JSON. Type the declaration you mean; the host never guesses one.` }
  }
}

/** One file's bytes as standard base64, exactly as the browser read them. */
function base64Of(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes)
  let binary = ''
  for (const byte of view) binary += String.fromCharCode(byte)
  return btoa(binary)
}

/**
 * Controlled upload: bounded preview first, then one explicitly policied version.
 *
 * Either the file already has the strict profile, or the user declares how its own
 * columns and timestamps become it. A mapped version is published only from a
 * preview of the exact declaration on screen, so the digest the preview reports is
 * the digest the sealed version carries.
 */
function DataUploadPanel({ api }: { api: ApiAdapter }) {
  const [content, setContent] = useState('')
  const [fileBase64, setFileBase64] = useState<string | null>(null)
  const [fileName, setFileName] = useState('')
  const [mode, setMode] = useState<'strict' | 'explicit-mapping'>('strict')
  const [mappingText, setMappingText] = useState('')
  const [timeText, setTimeText] = useState('')
  const [policy, setPolicy] = useState('')
  const [latency, setLatency] = useState('0')
  const [sourceReference, setSourceReference] = useState('')
  const [licenseReference, setLicenseReference] = useState('')
  const [preview, setPreview] = useState<UploadPreviewResponse | null>(null)
  const [previewedFor, setPreviewedFor] = useState<string | null>(null)
  const [published, setPublished] = useState<UploadVersion | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'preview' | 'publish' | null>(null)
  // Which of the two editors writes the one declaration. The guided controls are
  // the first-use path; the JSON fields are chosen explicitly.
  const [declarationEditor, setDeclarationEditor] = useState<'guided' | 'advanced'>('guided')
  const previewUpload = api.previewUpload

  const mapped = mode === 'explicit-mapping'
  // The declaration on screen, read back as choices for the guided controls. The
  // JSON text is the only state, so the two editors cannot hold different
  // declarations: a declaration the controls cannot express keeps the JSON editor
  // and its reason instead of being dropped or re-pointed to fit a control.
  const guided = useMemo(() => readGuidedDeclaration(mappingText, timeText), [mappingText, timeText])
  // The exact inputs a mapped preview verified. Any change to the payload, the
  // declaration or the policy makes the previewed declaration a different one,
  // so the preview no longer authorizes a publication.
  const signature = JSON.stringify([fileBase64 ?? content, mode, mappingText, timeText, policy, latency])
  const previewFresh = preview !== null && previewedFor === signature
  // A mapped version needs a preview of this exact declaration that also
  // accepted the whole source: a refused preview is never publishable, and the
  // host remains the authority that rejects an invalid declaration anyway.
  const mappedVerified = mapped && preview !== null && previewFresh && preview.valid
  const mappedBlocked = mapped && !mappedVerified
    ? previewFresh
      ? 'This declaration does not verify the whole source: a refused preview is never published. Correct the declaration and preview it again.'
      : 'A mapped version is published only from a preview of this exact declaration: preview it, and publish while that preview is still current. Changing the file, the mapping, the time declaration or the policy clears it.'
    : null

  const reset = () => { setPreview(null); setPreviewedFor(null); setPublished(null); setError(null) }
  // A guided choice rewrites the declaration the panel sends, and the preview
  // that verified the previous declaration stops authorizing a publication the
  // moment it changes. The two JSON fields stay the one place a declaration
  // lives, so the advanced editor and the guided controls cannot disagree: they
  // are the same two strings.
  const applyGuidedDeclaration = useCallback((nextMapping: string, nextTime: string) => {
    setMappingText(nextMapping)
    setTimeText(nextTime)
    setPreview(null); setPreviewedFor(null); setPublished(null); setError(null)
  }, [])
  // A file's own bytes are the payload; editing the text makes the text the payload.
  const tooLarge = fileBase64 === null && content.length > MAX_UPLOAD_CHARACTERS

  const declarationOf = (): UploadDeclaration | null => {
    if (!policy) { setError('A mapped source states how it became available; no policy is assumed.'); return null }
    const mapping = parseDeclaration(mappingText, 'The mapping declaration')
    if ('error' in mapping) { setError(mapping.error); return null }
    const time = parseDeclaration(timeText, 'The time declaration')
    if ('error' in time) { setError(time.error); return null }
    return {
      mode: 'explicit-mapping',
      mapping: mapping.value as UploadDeclaration['mapping'],
      time_declaration: time.value as UploadDeclaration['time_declaration'],
      availability_policy: policy,
      ...(policy === AVAILABILITY_SIMULATED ? { latency_seconds: Number(latency) } : {}),
    }
  }

  const run = async (kind: 'preview' | 'publish') => {
    if (busy || tooLarge) return
    if (!fileBase64 && !content) return
    setBusy(kind); setError(null)
    try {
      const declaration = mapped ? declarationOf() : undefined
      if (mapped && declaration === null) return
      if (kind === 'preview') {
        setPublished(null)
        if (!previewUpload) return
        // A selected file is previewed as its own bytes or not at all: decoding
        // it into text and sending that back would preview a different payload.
        if (fileBase64 !== null && !api.previewUploadFile) {
          setError('This workspace cannot preview a selected file as its own bytes; clear the file and paste the content instead.')
          return
        }
        const verified = fileBase64 !== null && api.previewUploadFile
          ? await api.previewUploadFile(fileBase64, declaration as UploadDeclaration, fileName || undefined)
          : await previewUpload(content, fileName || undefined, declaration ?? undefined)
        setPreview(verified); setPreviewedFor(signature)
        return
      }
      if (!api.publishUpload) return
      // A mapped publication is only ever the declaration a preview verified.
      if (mapped && !mappedVerified) {
        setError(mappedBlocked ?? 'Preview this declaration and correct it before publishing.')
        return
      }
      const version = await api.publishUpload({
        ...(fileBase64 !== null ? { content_base64: fileBase64 } : { content }),
        ...(fileName ? { file_name: fileName } : {}),
        ...(sourceReference ? { source_reference: sourceReference } : {}),
        ...(licenseReference ? { license_reference: licenseReference } : {}),
        ...(declaration ?? {}),
        availability_policy: policy,
        ...(policy === AVAILABILITY_SIMULATED ? { latency_seconds: Number(latency) } : {}),
      })
      setPreview(null); setPreviewedFor(null); setPublished(version)
    } catch (caught) { setError(messageOf(caught, 'The host refused this upload.')) }
    finally { setBusy(null) }
  }

  if (!previewUpload && !api.publishUpload) return null
  const strictPreview = preview && preview.schema === 'ea.local-upload-preview.v1' ? preview : null
  const mappedPreview = preview && isMappedPreview(preview) ? preview : null
  const columns = strictPreview ? (strictPreview.header.length ? strictPreview.header : Object.keys(strictPreview.rows[0] ?? {}).filter(name => name !== 'record_number')) : []
  const mappedColumns = mappedPreview?.preview ? Object.keys(mappedPreview.preview.rows[0] ?? {}).filter(name => name !== 'record_number') : []
  return <div className="upload-panel">
    <p className="muted">An uploaded file is decoded by the same strict Phase 1 OHLCV decoder the read-only registered root uses, and published as one immutable version. Either the file already has that fixed 15-column UTC interval profile, or you declare, field by field, how your own columns and timestamps become it. Nothing is inferred from the data in either case.</p>
    <label htmlFor="upload-mode">Upload mode</label>
    <select id="upload-mode" value={mode} disabled={busy !== null} onChange={event => { setMode(event.target.value === 'explicit-mapping' ? 'explicit-mapping' : 'strict'); reset() }}>
      <option value="strict">Strict profile, unchanged</option>
      <option value="explicit-mapping">Explicit mapping of my own file</option>
    </select>
    <label htmlFor="upload-file">CSV file</label>
    <input id="upload-file" type="file" accept=".csv,text/csv" disabled={busy !== null} onChange={event => {
      const file = event.target.files?.[0]
      if (!file) return
      setFileName(file.name); reset()
      void file.arrayBuffer().then(bytes => {
        // The file's own bytes are what a publication sends; the text below is
        // only a readable view of them, and editing it replaces that payload.
        setFileBase64(base64Of(bytes))
        setContent(new TextDecoder().decode(bytes))
      }).catch(() => setError('The selected file could not be read in this browser.'))
    }} />
    {fileBase64 !== null && <p className="muted">This file is sent as its own bytes (base64), not as re-encoded text: a byte-order mark, a CRLF ending or a non-ASCII column stays exactly as the file has it.</p>}
    <label htmlFor="upload-content">{fileBase64 !== null ? 'CSV content (a readable view; editing it sends this text instead)' : 'CSV content'}</label>
    <textarea id="upload-content" rows={6} spellCheck={false} value={content} disabled={busy !== null}
      onChange={event => { setContent(event.target.value); setFileBase64(null); reset() }} />
    {tooLarge && <p className="notice error">This content is above the {MAX_UPLOAD_CHARACTERS}-character ceiling this host accepts; it would be refused before anything is parsed.</p>}
    {mapped && <>
      {/* One declaration, an explicit choice of how to write it. The guided
          controls are read from the JSON fields, so neither editor can overwrite
          the other's declaration. */}
      <fieldset className="declaration-editor">
        <legend>How to declare this mapping</legend>
        <label>
          <input type="radio" name="upload-declaration-editor" checked={declarationEditor === 'guided'}
            disabled={busy !== null || !guided.ok} onChange={() => setDeclarationEditor('guided')} />
          Guided controls
        </label>
        <label>
          <input type="radio" name="upload-declaration-editor" checked={declarationEditor === 'advanced'}
            disabled={busy !== null} onChange={() => setDeclarationEditor('advanced')} />
          Advanced JSON
        </label>
      </fieldset>
      {!guided.ok && <p className="notice error" role="alert">
        The guided controls cannot show the declaration on screen: {guided.problem} It stays here as JSON,
        unchanged, until it is edited into one the controls can hold.
      </p>}
      {declarationEditor === 'guided' && guided.ok
        ? <GuidedMappingFields content={content} policy={policy} disabled={busy !== null}
            state={guided.state} onChange={applyGuidedDeclaration} />
        : <div className="advanced-declaration">
          <p className="muted">This is the same declaration, written as JSON. Nothing on this page decides whether it is acceptable: it is sent as it stands, and the host is the authority that accepts or refuses it. Switching back to the guided controls reads exactly what is here.</p>
          <label htmlFor="upload-mapping">Mapping declaration (JSON, every strict field declared)</label>
          <textarea id="upload-mapping" rows={5} spellCheck={false} placeholder={MAPPING_EXAMPLE} value={mappingText} disabled={busy !== null}
            onChange={event => { setMappingText(event.target.value); reset() }} />
          <label htmlFor="upload-time">Time declaration (JSON, exact formats and timezone)</label>
          <textarea id="upload-time" rows={3} spellCheck={false} placeholder={TIME_EXAMPLE} value={timeText} disabled={busy !== null}
            onChange={event => { setTimeText(event.target.value); reset() }} />
          <p className="muted">Each destination is one of the strict profile's own field names, and each timestamp names one exact form from {TIME_FORMAT_IDS.join(', ')}. A field you do not declare, a column this file does not have, and a format the values do not match are all refusals rather than guesses. Under the simulated policy the host derives available_at itself, so a declaration that also states it is refused.</p>
        </div>}
    </>}
    <div className="parameter-grid">
      <div><label htmlFor="upload-source">Source reference (your assertion)</label><input id="upload-source" maxLength={300} value={sourceReference} disabled={busy !== null} onChange={event => { setSourceReference(event.target.value); setPublished(null) }} /></div>
      <div><label htmlFor="upload-license">Licence reference</label><input id="upload-license" maxLength={300} value={licenseReference} disabled={busy !== null} onChange={event => { setLicenseReference(event.target.value); setPublished(null) }} /></div>
      <div><label htmlFor="upload-policy">Availability policy</label>
        <select id="upload-policy" value={policy} disabled={busy !== null} onChange={event => { setPolicy(event.target.value); setPreview(null); setPreviewedFor(null); setPublished(null); setError(null) }}>
          <option value="">Choose explicitly…</option>
          <option value={AVAILABILITY_PROVIDED}>Provided in the capture (uploader assertion)</option>
          <option value={AVAILABILITY_SIMULATED}>Simulated: interval end + latency</option>
        </select></div>
      {policy === AVAILABILITY_SIMULATED && <div><label htmlFor="upload-latency">Simulated latency (seconds)</label><input id="upload-latency" inputMode="numeric" value={latency} disabled={busy !== null} onChange={event => { setLatency(event.target.value); setPreview(null); setPreviewedFor(null); setPublished(null) }} /></div>}
    </div>
    <p className="muted">There is no default policy: an upload that does not state how the capture became available is refused rather than recorded as if you had asserted it. Neither policy is ever reported as verified point-in-time availability, and the source and licence strings are your assertion, not a legal verification.</p>
    <div className="actions">
      <button type="button" disabled={busy !== null || (!content && fileBase64 === null) || tooLarge || !previewUpload} onClick={() => { void run('preview') }}>{busy === 'preview' ? 'Previewing…' : 'Preview sample'}</button>
      <button type="button" className="primary" disabled={busy !== null || (!content && fileBase64 === null) || tooLarge || !policy || !api.publishUpload || (mapped && !mappedVerified)} onClick={() => { void run('publish') }}>{busy === 'publish' ? 'Publishing…' : 'Publish version'}</button>
    </div>
    {mappedBlocked && <p className="muted">{mappedBlocked}</p>}
    {error && <p className="notice error">{error}</p>}
    {strictPreview && <section className="upload-preview" aria-label="Upload preview">
      <dl className="summary-list">
        <div><dt>Bytes / profile</dt><dd>{strictPreview.byte_count} · {strictPreview.profile}</dd></div>
        <div><dt>Sampled rows</dt><dd>{strictPreview.row_count_sampled}{strictPreview.truncated ? ' · sample truncated' : ''}</dd></div>
        <div><dt>Header</dt><dd>{strictPreview.header_ok ? 'Matches the strict profile' : `Expected: ${strictPreview.expected_header.join(', ')}`}</dd></div>
        <div><dt>Full validation</dt><dd>{strictPreview.full_validation}{strictPreview.valid ? ' · verified' : ''}</dd></div>
      </dl>
      {strictPreview.first_error && <p className="notice error">{strictPreview.first_error.code}: {strictPreview.first_error.message}{strictPreview.first_error.record_number ? ` · CSV record ${strictPreview.first_error.record_number}` : ''}{strictPreview.first_error.field_name ? ` · field ${strictPreview.first_error.field_name}` : ''}</p>}
      {strictPreview.quality && <dl className="summary-list">
        <div><dt>Bars / revisions</dt><dd>{strictPreview.quality.bar_count} / {strictPreview.quality.revision_count}</dd></div>
        <div><dt>Observed gaps</dt><dd>{strictPreview.quality.observed_gap_count}{strictPreview.quality.largest_gap_seconds ? ` · largest ${strictPreview.quality.largest_gap_seconds}s` : ''}</dd></div>
        <div><dt>Bar coverage (UTC)</dt><dd>{strictPreview.quality.bar_start_utc} — {strictPreview.quality.bar_end_utc}</dd></div>
        <div><dt>Bar durations</dt><dd>{strictPreview.quality.bar_durations_seconds.map(value => `${value}s`).join(', ') || 'Unavailable'}</dd></div>
      </dl>}
      {columns.length > 0 && strictPreview.rows.length > 0 && <table><thead><tr><th>Record</th>{columns.map(name => <th key={name}>{name}</th>)}</tr></thead>
        <tbody>{strictPreview.rows.map(row => <tr key={String(row.record_number)}><th scope="row">{String(row.record_number)}</th>{columns.map(name => <td key={name}>{String(row[name] ?? '')}</td>)}</tr>)}</tbody></table>}
      {strictPreview.limitations.length > 0 && <details><summary>What a preview cannot show</summary>
        <ul className="assumption-list">{strictPreview.limitations.map(item => <li key={item}>{item}</li>)}</ul>
      </details>}
    </section>}
    {mappedPreview && <section className="upload-preview" aria-label="Mapped upload preview">
      <dl className="summary-list">
        <div><dt>Mapped bytes / profile</dt><dd>{mappedPreview.capture?.byte_count ?? mappedPreview.byte_count} · {mappedPreview.profile ?? 'strict Phase 1 OHLCV'}</dd></div>
        <div><dt>Sampled rows</dt><dd>{mappedPreview.preview?.row_count_sampled ?? 0}{mappedPreview.preview?.truncated ? ' · sample truncated' : ''}</dd></div>
        <div><dt>Whole file mapped</dt><dd>{mappedPreview.valid ? 'verified — the whole source normalizes and re-decodes' : 'refused'}</dd></div>
        <div><dt>Raw source SHA-256 (not the capture)</dt><dd>{mappedPreview.source?.sha256 ?? 'Unavailable'} · {mappedPreview.source?.byte_count ?? 0} source bytes; retained only after publication</dd></div>
        <div><dt>Mapped capture SHA-256</dt><dd>{mappedPreview.capture?.sha256 ?? 'Unavailable'} · {mappedPreview.capture?.record_count ?? 0} records</dd></div>
        <div><dt>Mapping evidence SHA-256</dt><dd>{mappedPreview.mapping_evidence_sha256 ?? 'Unavailable'}</dd></div>
        <div><dt>Declared timezone / formats</dt><dd>{mappedPreview.time?.timezone ?? 'Unavailable'} · {Object.entries(mappedPreview.time?.formats ?? {}).map(([field, format]) => `${field}: ${format}`).join(' · ') || 'Unavailable'}</dd></div>
      </dl>
      {mappedPreview.first_error && <p className="notice error" role="alert">{mappedPreview.first_error.code}: {mappedPreview.first_error.message}{mappedPreview.first_error.record_number ? ` · CSV record ${mappedPreview.first_error.record_number}` : ''}{mappedPreview.first_error.field_name ? ` · field ${mappedPreview.first_error.field_name}` : ''}</p>}
      {mappedPreview.quality && <dl className="summary-list">
        <div><dt>Bars / revisions</dt><dd>{mappedPreview.quality.bar_count} / {mappedPreview.quality.revision_count}</dd></div>
        <div><dt>Observed gaps</dt><dd>{mappedPreview.quality.observed_gap_count}{mappedPreview.quality.largest_gap_seconds ? ` · largest ${mappedPreview.quality.largest_gap_seconds}s` : ''}</dd></div>
        <div><dt>Bar coverage (UTC)</dt><dd>{mappedPreview.quality.bar_start_utc} — {mappedPreview.quality.bar_end_utc}</dd></div>
      </dl>}
      {mappedPreview.mapping && <details><summary>Declared mapping, as the host resolved it</summary>
        <ul className="assumption-list">
          {Object.entries(mappedPreview.mapping.columns).map(([field, column]) => <li key={field}>{field} ← column {column}</li>)}
          {Object.entries(mappedPreview.mapping.constants).map(([field, value]) => <li key={field}>{field} = {value}</li>)}
        </ul>
      </details>}
      {mappedColumns.length > 0 && (mappedPreview.preview?.rows.length ?? 0) > 0 && <table><thead><tr><th>Record</th>{mappedColumns.map(name => <th key={name}>{name}</th>)}</tr></thead>
        <tbody>{mappedPreview.preview?.rows.map(row => <tr key={String(row.record_number)}><th scope="row">{String(row.record_number)}</th>{mappedColumns.map(name => <td key={name}>{String(row[name] ?? '')}</td>)}</tr>)}</tbody></table>}
      {mappedPreview.limitations.length > 0 && <details><summary>What a preview cannot show</summary>
        <ul className="assumption-list">{mappedPreview.limitations.map(item => <li key={item}>{item}</li>)}</ul>
      </details>}
    </section>}
    {published && <section className="upload-version" aria-label="Published dataset version">
      <dl className="summary-list">
        <div><dt>Dataset version</dt><dd>{published.dataset_id} · v{published.version} · {published.published_at}</dd></div>
        <div><dt>Instrument / records</dt><dd>{published.instrument.venue}:{published.instrument.symbol} · {published.record_count}</dd></div>
        <div><dt>Availability policy</dt><dd>{published.availability_policy.id} · {published.availability_policy.evidence} · point-in-time verified: {String(published.availability_policy.pit_verified)}</dd></div>
        <div><dt>Source assertion</dt><dd>{published.source.reference ?? 'No reference given'} · asserted by {published.source.declared_by}, not legally verified</dd></div>
        <div><dt>Capture SHA-256</dt><dd>{published.capture_sha256}</dd></div>
        <div><dt>Canonical data SHA-256</dt><dd>{published.data_sha256}</dd></div>
        <div><dt>Raw source SHA-256</dt><dd>{published.source_bytes_sha256} · {published.source_byte_count} bytes retained as provenance only</dd></div>
        {published.mapping_evidence_sha256 && <div><dt>Mapping evidence SHA-256</dt><dd>{published.mapping_evidence_sha256} · the declaration is sealed with the version and re-derived on every read</dd></div>}
        <div><dt>Version manifest SHA-256</dt><dd>{published.manifest_sha256}</dd></div>
      </dl>
      <p className="muted">This version is now one entry in the dataset catalog. It is usable as research data only when you explicitly select its Data capability: that selection is what freezes the version's evidence for a run, and a direct backtest or batch over an uploaded version carries no such binding and is refused before anything is prepared. Research binds the canonical capture digest; an edited manifest fails that binding before any run is dispatched, and the retained raw bytes only ever identify where the version came from.</p>
      {published.limitations.length > 0 && <ul className="assumption-list">{published.limitations.map(item => <li key={item}>{item}</li>)}</ul>}
    </section>}
  </div>
}

/**
 * The offline import methods this workspace calls.
 *
 * The shipped browser adapter is built from ``research-api`` in another module,
 * so a caller may carry these three methods itself; when it does not, the module
 * this file already reads every other host verdict from is the same one.
 */
type OfflineHistoryMethods = {
  offlineHistoryExample?: () => Promise<OfflineHistoryExample>
  previewOfflineHistory?: (
    content: string,
    declaration: OfflineHistoryDeclaration,
  ) => Promise<OfflineHistoryPreview>
  publishOfflineHistory?: (
    content: string,
    declaration: OfflineHistoryDeclaration,
    previewSha256: string,
  ) => Promise<OfflineHistoryVersion>
}

function offlineHistoryApi(api: ApiAdapter): OfflineHistoryMethods {
  const injected = api as unknown as OfflineHistoryMethods
  if (injected.previewOfflineHistory || injected.publishOfflineHistory) return injected
  return researchApi as unknown as OfflineHistoryMethods
}

/** The adapter's own single-response bound; a longer paste is refused unread. */
const MAX_OFFLINE_RESPONSE_CHARACTERS = 512 * 1024

const offlineInteger = (value: string): number | null =>
  /^-?\d+$/.test(value.trim()) ? Number(value.trim()) : null

/**
 * Offline import of a historical response the user already holds.
 *
 * No transport is involved anywhere in this path: the pasted text is the whole
 * input, the host normalizes it with the one documented 12-field adapter and
 * seals the exact bytes as the version's provenance. The panel marks the
 * built-in example synthetic and refuses to carry that mark onto a paste, and a
 * publication is only ever the exact bytes and declarations a preview verified.
 */
function OfflineHistoryPanel({ api }: { api: ApiAdapter }) {
  const offline = offlineHistoryApi(api)
  const [open, setOpen] = useState(false)
  const [content, setContent] = useState('')
  const [venue, setVenue] = useState('')
  const [symbol, setSymbol] = useState('')
  const [interval, setIntervalValue] = useState('')
  const [startMs, setStartMs] = useState('')
  const [endMs, setEndMs] = useState('')
  const [latency, setLatency] = useState('')
  const [source, setSource] = useState('')
  const [rights, setRights] = useState('')
  const [intendedUse, setIntendedUse] = useState('')
  const [synthetic, setSynthetic] = useState(false)
  const [preview, setPreview] = useState<OfflineHistoryPreview | null>(null)
  const [previewedFor, setPreviewedFor] = useState<string | null>(null)
  const [published, setPublished] = useState<OfflineHistoryVersion | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'example' | 'preview' | 'publish' | null>(null)
  // Every reply is tagged with the generation it started in, so a reply that
  // arrives after an edit, a close or a newer request is dropped unread. The
  // busy ref is synchronous: a second click in the same tick, before React has
  // disabled the button, is refused rather than sent again.
  const generation = useRef(0)
  const busyRef = useRef(false)

  const clearPreview = () => { setPreview(null); setPreviewedFor(null); setPublished(null) }
  // The exact paste and declarations a preview verified. Any change to either is
  // a different import, so the preview stops authorizing a publication.
  const signature = JSON.stringify([content, venue, symbol, interval, startMs, endMs, latency, source, rights, intendedUse, synthetic])
  const previewFresh = preview !== null && previewedFor === signature
  const verified = previewFresh && preview !== null && preview.valid && preview.preview_sha256 !== null
  const tooLarge = content.length > MAX_OFFLINE_RESPONSE_CHARACTERS

  // Editing the response is what the user does with their own bytes, so the
  // synthetic designation can never survive an edit: only loading the host's own
  // example sets it, and the host refuses the marking for anything else.
  const edit = (apply: () => void) => { apply(); setSynthetic(false); clearPreview(); setError(null) }

  const declarationOf = (): OfflineHistoryDeclaration | null => {
    const start = offlineInteger(startMs)
    const end = offlineInteger(endMs)
    const latencyValue = offlineInteger(latency)
    if (!venue.trim() || !symbol.trim() || !interval.trim()) {
      setError('State the venue, the symbol and the fixed interval your own response answers, so the host normalizes it under exactly that query.')
      return null
    }
    if (start === null || end === null || latencyValue === null) {
      setError('State the query start, the query end and the simulated availability latency as whole numbers.')
      return null
    }
    if (!synthetic && (!source.trim() || !rights.trim() || !intendedUse.trim())) {
      setError('Declare the source, the rights/licence and the intended use of your own pasted response; the host records no assertion nobody made and verifies no licence.')
      return null
    }
    return {
      venue: venue.trim(),
      symbol: symbol.trim(),
      interval: interval.trim(),
      start_ms: start,
      end_ms: end,
      latency_seconds: latencyValue,
      synthetic,
      ...(synthetic
        ? {}
        : { declared_source: source.trim(), declared_rights: rights.trim(), intended_use: intendedUse.trim() }),
    }
  }

  const run = async (kind: 'example' | 'preview' | 'publish') => {
    if (busyRef.current) return
    if (kind === 'example') {
      if (!offline.offlineHistoryExample) return
      busyRef.current = true
      const current = ++generation.current
      setBusy('example'); setError(null)
      try {
        const example = await offline.offlineHistoryExample()
        if (current !== generation.current) return
        setContent(example.response_body)
        setVenue(example.venue); setSymbol(example.symbol); setIntervalValue(example.interval)
        setStartMs(String(example.start_ms)); setEndMs(String(example.end_ms))
        setLatency(String(example.latency_seconds))
        setSource(''); setRights(''); setIntendedUse('')
        setSynthetic(true)
        clearPreview()
      } catch (caught) {
        if (current === generation.current) setError(messageOf(caught, 'The built-in example could not be read.'))
      } finally {
        // One request is in flight at a time, so its own reply is what releases
        // the synchronous guard, even when that reply is dropped unread.
        busyRef.current = false
        setBusy(null)
      }
      return
    }
    if (!content) return
    if (tooLarge) { setError(`This response is above the ${MAX_OFFLINE_RESPONSE_CHARACTERS}-character ceiling the host reads; it would be refused before it is parsed.`); return }
    const declaration = declarationOf()
    if (declaration === null) return
    busyRef.current = true
    const current = ++generation.current
    setBusy(kind); setError(null)
    try {
      if (kind === 'preview') {
        if (!offline.previewOfflineHistory) return
        const verdict = await offline.previewOfflineHistory(content, declaration)
        if (current !== generation.current) return
        setPreview(verdict); setPreviewedFor(signature); setPublished(null)
        return
      }
      if (!offline.publishOfflineHistory) return
      // A publication is only ever the exact preview this host handed out: a
      // changed response, a changed declaration or a refused preview is not one.
      if (!verified || preview === null || preview.preview_sha256 === null) {
        setError('An offline version is published only from a preview of these exact bytes and declarations: preview them and publish while that preview is still current.')
        return
      }
      const version = await offline.publishOfflineHistory(content, declaration, preview.preview_sha256)
      if (current !== generation.current) return
      setPreview(null); setPreviewedFor(null); setPublished(version)
    } catch (caught) {
      if (current === generation.current) setError(messageOf(caught, 'The host refused this offline import.'))
    } finally {
      busyRef.current = false
      setBusy(null)
    }
  }

  if (!offline.previewOfflineHistory && !offline.publishOfflineHistory && !offline.offlineHistoryExample) return null

  const columns = preview && preview.preview ? Object.keys(preview.preview.rows[0] ?? {}).filter(name => name !== 'record_number') : []
  return <div className="upload-panel">
    <p className="muted"><strong>Offline import.</strong> API NOT connected: paste a historical response you already hold and the host normalizes it with the one documented 12-field klines adapter, with no network request and no provider credential. The response is read in this process only. Point-in-time availability is unverified; the declared availability is simulated interval end plus the latency you state.</p>
    <div className="actions">
      <button type="button" onClick={() => {
        // Closing invalidates the preview on screen; it cannot recall a
        // publication that was already sent, which the host settles under its
        // own lock and answers with the one version that identity seals.
        if (open) { generation.current += 1; setError(null); clearPreview() }
        setOpen(!open)
      }}>{open ? 'Close offline history import' : 'Open offline history import'}</button>
      {!open && <span className="muted">Nothing is sent until you preview; nothing is published until you confirm that preview.</span>}
    </div>
    {open && <>
      <div className="actions">
        <button type="button" disabled={busy !== null || !offline.offlineHistoryExample} onClick={() => { void run('example') }}>{busy === 'example' ? 'Loading example…' : 'Load built-in synthetic example'}</button>
        <span className="muted">{synthetic
          ? 'Synthetic: these are the host\'s own fixture bytes, marked synthetic end to end, not market data and not your data. Editing the response clears this designation.'
          : 'Your own response. It is never recorded as synthetic: that mark is reserved for the host\'s built-in example and cannot be requested for a paste.'}</span>
      </div>
      <label htmlFor="offline-response">Offline response (JSON, exactly as received)</label>
      <textarea id="offline-response" rows={8} spellCheck={false} value={content} disabled={busy !== null}
        onChange={event => edit(() => setContent(event.target.value))} />
      {tooLarge && <p className="notice error">This response is above the {MAX_OFFLINE_RESPONSE_CHARACTERS}-character ceiling the host reads; it would be refused before it is parsed.</p>}
      <div className="parameter-grid">
        <div><label htmlFor="offline-venue">Offline venue</label><input id="offline-venue" value={venue} disabled={busy !== null} onChange={event => edit(() => setVenue(event.target.value))} /></div>
        <div><label htmlFor="offline-symbol">Offline symbol</label><input id="offline-symbol" value={symbol} disabled={busy !== null} onChange={event => edit(() => setSymbol(event.target.value))} /></div>
        <div><label htmlFor="offline-interval">Offline interval</label><input id="offline-interval" placeholder="1h" value={interval} disabled={busy !== null} onChange={event => edit(() => setIntervalValue(event.target.value))} /></div>
        <div><label htmlFor="offline-start">Offline query start (epoch ms)</label><input id="offline-start" inputMode="numeric" value={startMs} disabled={busy !== null} onChange={event => edit(() => setStartMs(event.target.value))} /></div>
        <div><label htmlFor="offline-end">Offline query end (epoch ms, exclusive)</label><input id="offline-end" inputMode="numeric" value={endMs} disabled={busy !== null} onChange={event => edit(() => setEndMs(event.target.value))} /></div>
        <div><label htmlFor="offline-latency">Simulated availability latency (seconds)</label><input id="offline-latency" inputMode="numeric" value={latency} disabled={busy !== null} onChange={event => edit(() => setLatency(event.target.value))} /></div>
      </div>
      <p className="muted">The query is fixed here and never inferred from the bytes: the whole pasted response must answer exactly this venue, symbol, interval and range, and a response that is one full page short of the declared range end is refused rather than truncated silently.</p>
      <div className="parameter-grid">
        <div><label htmlFor="offline-source">Declared source (your assertion)</label><input id="offline-source" maxLength={300} value={source} disabled={busy !== null || synthetic} onChange={event => edit(() => setSource(event.target.value))} /></div>
        <div><label htmlFor="offline-rights">Declared rights/licence (your assertion)</label><input id="offline-rights" maxLength={300} value={rights} disabled={busy !== null || synthetic} onChange={event => edit(() => setRights(event.target.value))} /></div>
        <div><label htmlFor="offline-use">Declared intended use (your assertion)</label><input id="offline-use" maxLength={300} value={intendedUse} disabled={busy !== null || synthetic} onChange={event => edit(() => setIntendedUse(event.target.value))} /></div>
      </div>
      <p className="muted">These three strings are your own assertion, recorded with the version and not legally verified. The built-in example asserts nothing and takes no such declaration.</p>
      <div className="actions">
        <button type="button" disabled={busy !== null || !content || tooLarge || !offline.previewOfflineHistory} onClick={() => { void run('preview') }}>{busy === 'preview' ? 'Previewing…' : 'Preview offline import'}</button>
        <button type="button" className="primary" disabled={busy !== null || !verified || !offline.publishOfflineHistory} onClick={() => { void run('publish') }}>{busy === 'publish' ? 'Publishing…' : 'Publish offline version'}</button>
      </div>
      <p className="muted">Closing this panel invalidates the preview on screen, but it does not revoke a publication already submitted: that version still seals. Publishing the same preview identity again is answered with the one version it already sealed, not with a second one.</p>
      {previewFresh && preview && !verified && <p className="muted">The host refused this response: a refused preview is never published. Correct the response or the declarations and preview again.</p>}
      {error && <p className="notice error">{error}</p>}
      {preview && !previewFresh && <p className="muted">These bytes or declarations changed since the last preview, so that preview no longer authorizes a publication.</p>}
      {previewFresh && preview?.valid && <section className="upload-preview" aria-label="Offline history import preview">
        <dl className="summary-list">
          <div><dt>Response bytes / SHA-256</dt><dd>{preview.byte_count} · {preview.response_sha256 ?? 'Unavailable'}</dd></div>
          <div><dt>Provenance</dt><dd>{preview.provenance ?? 'Unavailable'}{preview.synthetic ? ' · synthetic, host fixture' : ' · user paste, not supplier-verified'}</dd></div>
          <div><dt>Instrument / records</dt><dd>{preview.instrument?.venue ?? 'Unavailable'}:{preview.instrument?.symbol ?? ''} · {preview.capture?.record_count ?? 0}</dd></div>
          <div><dt>Normalized capture SHA-256</dt><dd>{preview.capture?.sha256 ?? 'Unavailable'}</dd></div>
          <div><dt>Availability policy</dt><dd>{preview.availability_policy?.id ?? 'Unavailable'} · point-in-time verified: {String(preview.availability_policy?.pit_verified ?? false)}</dd></div>
          <div><dt>Declarations (your assertion, not verified)</dt><dd>{preview.synthetic
            ? 'None: the built-in example asserts no source, rights or intended use'
            : `${preview.declarations?.source ?? 'None'} · ${preview.declarations?.rights ?? 'None'} · ${preview.declarations?.intended_use ?? 'None'}`}</dd></div>
          {preview.synthetic && <div><dt>Synthetic example SHA-256</dt><dd>{preview.response_sha256 ?? 'Unavailable'} · the host's own fixture, re-checked on every read</dd></div>}
          <div><dt>Preview identity SHA-256</dt><dd>{preview.preview_sha256 ?? 'Unavailable'} · the exact identity a publication must name</dd></div>
        </dl>
        {preview.quality && <dl className="summary-list">
          <div><dt>Bars / revisions</dt><dd>{preview.quality.bar_count} / {preview.quality.revision_count}</dd></div>
          <div><dt>Observed gaps</dt><dd>{preview.quality.observed_gap_count}{preview.quality.largest_gap_seconds ? ` · largest ${preview.quality.largest_gap_seconds}s` : ''}</dd></div>
          <div><dt>Bar coverage (UTC)</dt><dd>{preview.quality.bar_start_utc} — {preview.quality.bar_end_utc}</dd></div>
        </dl>}
        <p className="muted">Sampled rows {preview.preview?.row_count_sampled ?? 0}{preview.preview?.truncated ? ' · sample truncated' : ''}: the whole response was normalized to reach this verdict, and the sample below is a bounded view of it.</p>
        {columns.length > 0 && (preview.preview?.rows.length ?? 0) > 0 && <table><thead><tr><th>Record</th>{columns.map(name => <th key={name}>{name}</th>)}</tr></thead>
          <tbody>{preview.preview?.rows.map(row => <tr key={String(row.record_number)}><th scope="row">{String(row.record_number)}</th>{columns.map(name => <td key={name}>{String(row[name] ?? '')}</td>)}</tr>)}</tbody></table>}
        {preview.limitations.length > 0 && <details><summary>What this import cannot show</summary>
          <ul className="assumption-list">{preview.limitations.map(item => <li key={item}>{item}</li>)}</ul>
        </details>}
      </section>}
      {published && <section className="upload-version" aria-label="Published offline version">
        <dl className="summary-list">
          <div><dt>Dataset version</dt><dd>{published.dataset_id} · v{published.version} · {published.published_at}</dd></div>
          <div><dt>Instrument / records</dt><dd>{published.instrument.venue}:{published.instrument.symbol} · {published.record_count}</dd></div>
          <div><dt>Provenance</dt><dd>{published.source.provenance ?? 'Unavailable'} · declared by {published.source.declared_by}, asserted: {String(published.source.asserted)}</dd></div>
          <div><dt>Availability policy</dt><dd>{published.availability_policy.id} · point-in-time verified: {String(published.availability_policy.pit_verified)}</dd></div>
          <div><dt>Raw response SHA-256</dt><dd>{published.source_bytes_sha256} · {published.source_byte_count} bytes of the response envelope retained as provenance</dd></div>
          <div><dt>Normalized capture SHA-256</dt><dd>{published.capture_sha256}</dd></div>
          <div><dt>Version manifest SHA-256</dt><dd>{published.manifest_sha256}</dd></div>
        </dl>
        <p className="muted">The retained envelope keeps your exact pasted bytes, the adapter version and the whole source, time and declaration record, so every read normalizes them again under the sealed query. This version is now one entry in the dataset catalog; it becomes research data only when you explicitly select its Data capability.</p>
        {published.limitations.length > 0 && <ul className="assumption-list">{published.limitations.map(item => <li key={item}>{item}</li>)}</ul>}
      </section>}
    </>}
  </div>
}

/** The job's own lifecycle wording, so a stop reads as what it is. */
const JOB_STATUS_LABELS: Record<string, string> = {
  accepted: 'queued',
  running: 'running',
  stopping: 'stopping · stop accepted, not yet confirmed',
  stopped: 'stopped · no result was published',
  succeeded: 'succeeded',
  failed: 'failed',
  interrupted: 'interrupted',
}

function ResearchJob({ api, jobId, job, holdouts }: { api: ApiAdapter; jobId: string; job: BacktestJob | null; holdouts: ChronologicalHoldout[] }) {
  if (!job) return <li className="job-card"><small>{jobId} · persisted job record unavailable</small></li>
  const holdoutForJob = holdouts.filter(item => item.source_job_id === job.job_id)
  return <li className="job-card">
    <div className="job-card-title"><Link to={`/backtests/${job.job_id}`}><strong>{job.job_id}</strong></Link><span className={`status status-${job.status}`}>{JOB_STATUS_LABELS[job.status] ?? job.status}</span></div>
    <small>Engine run {job.engine_run_id ?? 'not available'} · {job.scenario_id}{job.error_code ? ` · ${job.error_code}` : ''}</small>
    {job.message && <small>{job.message}</small>}
    {job.input_identity.record_count <= 6 && <small>Small sample · {job.input_identity.record_count} records. Workflow validation only; no robustness claim.</small>}
    <div className="job-actions">
      <Link to={`/backtests/${job.job_id}`}>Open job and report</Link>
      {job.report_ready && <a href={api.artifactUrl(job.job_id, 'report.json')}>Download report.json</a>}
      {job.status === 'succeeded' && job.report_ready && job.strategy_descriptor?.research_visible !== false && <Link to={`/holdouts/new/${job.job_id}`}>Evaluate chronological holdout</Link>}
      {holdoutForJob.map(item => <Link key={item.validation_id} to={`/holdouts/${item.validation_id}`}>Open holdout {item.validation_id}</Link>)}
      {holdoutForJob.length > 0 && <Link to={`/backtests/${job.job_id}`}>Create evaluated candidate</Link>}
    </div>
  </li>
}

/**
 * The one handoff this workspace accepts from a finished task: the user's own
 * idea, and the task it was read from.
 *
 * It arrives in this route's navigation state, so it is kept nowhere else — no
 * query string, no storage, no endpoint — and it is validated here rather than
 * trusted. Anything this page cannot read in full is ignored: the form stays
 * empty and nothing is reported about it, because ambient navigation state is
 * never a source of an experiment. Only those two fields cross over: no
 * specification, run, report, budget, capability selection or confirmation is
 * copied with them.
 */
function preparedExperiment(state: unknown): { sourceTaskId: string; idea: string } | null {
  if (typeof state !== 'object' || state === null) return null
  const { recovery } = state as { recovery?: unknown }
  if (typeof recovery !== 'object' || recovery === null) return null
  const { task_id: sourceTaskId, idea } = recovery as { task_id?: unknown; idea?: unknown }
  if (typeof sourceTaskId !== 'string' || sourceTaskId === '') return null
  if (typeof idea !== 'string' || idea.trim() === '' || idea.length > IDEA_LIMIT) return null
  return { sourceTaskId, idea }
}

export function ResearchTasks({ api }: { api: ApiAdapter }) {
  const navigate = useNavigate()
  const location = useLocation()
  // Read once, when this route is mounted. This fills a form the user then edits,
  // so it is not state this page keeps or re-applies: a later render, a refresh
  // or a return to this route does not put the idea back.
  const [prepared] = useState(() => preparedExperiment(location.state))
  const [tasks, setTasks] = useState<ResearchTask[] | null>(null)
  const [idea, setIdea] = useState(prepared?.idea ?? '')
  const [selection, setSelection] = useState<Partial<Record<LayerName, string>>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const unavailable = !api.listResearchTasks
  const busyRef = useRef(false)
  // The offline panel is offered wherever this client can already upload: the
  // shipped adapter is the same host boundary, and an injected adapter may carry
  // the offline methods itself.
  const offlineOffered = Boolean(
    (api as unknown as OfflineHistoryMethods).previewOfflineHistory ?? api.previewUpload ?? api.publishUpload,
  )

  const load = useCallback(async () => {
    if (!api.listResearchTasks) return
    try { setTasks(await api.listResearchTasks()); setError(null) }
    catch (caught) { setError(messageOf(caught, 'Research tasks unavailable')) }
  }, [api])

  useEffect(() => {
    let active = true
    const request = api.listResearchTasks
    if (!request) return () => { active = false }
    request().then(items => { if (active) { setTasks(items); setError(null) } })
      .catch(caught => { if (active) setError(messageOf(caught, 'Research tasks unavailable')) })
    return () => { active = false }
  }, [api])

  const create = async () => {
    const value = idea.trim()
    if (!value || busyRef.current || !api.createResearchTask) return
    busyRef.current = true; setBusy(true); setError(null)
    // Only the layers the user actually selected are sent; an empty selection
    // leaves the request exactly as it was and keeps the host default route.
    const chosen = selectionOf(selection)
    try { navigate(`/research/${(await api.createResearchTask(value, Object.keys(chosen).length ? chosen : undefined)).task_id}`) }
    catch (caught) {
      const message = caught instanceof ResearchRequestError && caught.code === 'research_task_exists'
        ? 'An active research task already exists for this idea. Open it below instead of submitting it again.'
        : messageOf(caught, 'The draft was not created; persisted tasks were reloaded.')
      // Reload first so the failure message is not cleared by the reload itself.
      await load()
      setError(message)
    } finally { busyRef.current = false; setBusy(false) }
  }

  const advancedOffered = Boolean(api.previewUpload || api.publishUpload || api.listDeclarations || offlineOffered || api.localStrategySlot || api.inspectLocalStrategy)
  return <>
    <PageHeading title="Research Workspace" subtitle="Persisted ideas, registered restrictive specs, and the real installed backtests they start." />
    {error && <p role="alert" className="notice error">{error}</p>}
    {unavailable && <p className="notice error">This client cannot reach the local research API.</p>}
    {/* Reachable without scrolling past the workspace: the evidence pages this
        task leads to are one click from the top of the page. */}
    <nav className="research-tools" aria-label="Research destinations">
      <Link to="/backtests">Backtests</Link>
      <Link to="/batches/new">Run batch</Link>
      <Link to="/holdouts">Chronological holdouts</Link>
    </nav>
    <div className="research-list-layout"><section className="panel idea-panel">
      <header><h2>New research idea</h2><span>One active task per idea</span></header>
      {prepared && <p role="note" aria-label="Prepared from a task" className="notice">
        This idea was copied from task <code>{prepared.sourceTaskId}</code>, which is left exactly as it is. Only your own words came across: no specification, run, report, budget, capability selection or confirmation did. Review the capability selection again below — leaving it empty uses the host default route. Nothing is created or confirmed until you press the button yourself.
      </p>}
      <label htmlFor="research-idea">Research idea</label>
      <textarea id="research-idea" maxLength={IDEA_LIMIT} rows={3} value={idea} disabled={busy}
        onChange={event => setIdea(event.target.value)} />
      <p className="muted">The idea is drafted into a spec that can only reference the registered strategy, instrument and scenario catalog. Confirmation is a separate, explicit step.</p>
      {api.pluginCatalog && <Disclosure title="Capability selection" subtitle="Optional · one exact version per layer">
        <CapabilitySelector api={api} values={selection} change={setSelection} disabled={busy} />
      </Disclosure>}
      <div className="actions"><button className="primary" disabled={busy || !idea.trim() || unavailable} onClick={() => { void create() }}>{busy ? 'Creating draft…' : 'Create draft'}</button></div>
      <p className="muted">Creating the draft records the selected capabilities on it; the host verifies that exact identity again and freezes it when you confirm. No selection means the host default route, and the model can never change it.</p>
    </section>
    <section className="panel research-history">
      <header><h2>Persisted research tasks</h2><span>Refresh-safe list</span></header>
      <div className="result-toolbar"><button onClick={() => { void load() }}>Refresh</button></div>
      {tasks === null && !error && !unavailable && <p className="muted">Loading persisted tasks…</p>}
      {tasks?.length === 0 && <p className="muted">No research tasks yet. Create a draft from an idea above.</p>}
      <ul className="job-list">{tasks?.map(task => {
        const spec = latestSpec(task)
        // The registered identity a reader recognizes leads the card; the idea it
        // came from stays on the card as the record it was drafted from.
        return <li className="job-card" key={task.task_id}>
          <div className="job-card-title"><Link to={`/research/${task.task_id}`}><strong>{spec ? `${spec.strategy_id} · ${spec.instrument.venue}:${spec.instrument.symbol}` : 'No spec drafted yet'}</strong></Link><span className={`status status-${task.status}`}>{STATUS_LABELS[task.status]}</span></div>
          <small className="job-idea">{task.raw_idea}</small>
          <small>{spec ? `${spec.initial_cash} initial cash · ${spec.scenario_id ?? 'scenario resolved at confirmation'}` : 'No registry-verified spec exists for this task yet'}</small>
          <small>Updated {task.updated_at} · model calls {task.usage.model_calls}/{task.budget.max_model_calls} · backtests {task.usage.backtests}/{task.budget.max_backtests} · jobs {task.jobs.length}</small>
          {task.error && <small>{task.error.step}: {task.error.code}</small>}
          <div className="job-actions"><Link to={`/research/${task.task_id}`}>Open task</Link></div>
        </li>
      })}</ul>
    </section></div>
    {/* Everything that changes what this workspace can research with. Nothing
        here is applied by opening it: each panel keeps its own explicit action. */}
    {advancedOffered && <section className="panel advanced-panel" aria-label="Data, declarations and local packages">
      <header><h2>Data, declarations and local packages</h2><span>Advanced · nothing is selected or run from here</span></header>
      <p className="muted">These controls change what this workspace can research with. Opening one applies nothing: every version published, declaration registered and package trusted below is a separate explicit decision, and each panel states its own effect before its action.</p>
      {(api.previewUpload || api.publishUpload) && <Disclosure title="Controlled data upload" subtitle="Preview, then publish one version">
        <DataUploadPanel api={api} />
      </Disclosure>}
      {offlineOffered && <Disclosure title="Offline historical import" subtitle="Pasted historical response; no network is used">
        <OfflineHistoryPanel api={api} />
      </Disclosure>}
      {api.listDeclarations && <Disclosure title="Declarations" subtitle="Verified data, installed only by you">
        <DeclarationCatalogPanel api={api} />
      </Disclosure>}
      {(api.localStrategySlot || api.inspectLocalStrategy) && <Disclosure title="Local strategy package" subtitle="Inspected statically, trusted explicitly">
        <LocalStrategyPanel api={api} />
      </Disclosure>}
    </section>}
  </>
}

export function ResearchTaskDetailRoute({ api }: { api: ApiAdapter }) {
  const { taskId = '' } = useParams()
  return <ResearchTaskDetail key={taskId} api={api} taskId={taskId} />
}

/**
 * The bounded data provenance the host projected for a conclusion.
 *
 * A fact this product cannot prove is shown as unavailable rather than omitted,
 * so a reader can never take this one run for more data than it actually had.
 */
function ConclusionScope({ scope }: { scope: ResearchDataScope | null | undefined }) {
  if (!scope) return null
  const text = (value: string | number | boolean | null) => value === null ? 'Not available' : String(value)
  // The replay window is the time and input policy the run recorded. It is shown
  // as recorded and never read as coverage: an absent window stays unavailable.
  const window = scope.time_range
  const windowText = window === null
    ? 'Not available'
    : `${text(window.start_inclusive)} → ${text(window.end_exclusive)}${window.timezone === null ? '' : ` (${window.timezone}${window.selection_field === null ? '' : `, selected by ${window.selection_field}`})`}`
  return <dl className="summary-list">
    <div><dt>Records</dt><dd>{text(scope.record_count)}</dd></div>
    <div><dt>Dataset</dt><dd>{text(scope.dataset_id)}</dd></div>
    <div><dt>Input SHA-256</dt><dd>{text(scope.input_sha256)}</dd></div>
    <div><dt>Scenario SHA-256</dt><dd>{text(scope.scenario_sha256)}</dd></div>
    <div><dt>Recorded replay window</dt><dd>{windowText}</dd></div>
    <div><dt>Point in time</dt><dd>{text(scope.point_in_time)}</dd></div>
    <div><dt>Independently verified</dt><dd>{scope.quality_verified ? 'Verified' : 'Not available'}</dd></div>
  </dl>
}

/**
 * The one line of data scope that belongs beside the conclusion itself.
 *
 * The full provenance stays in the disclosure below it; this line carries the
 * two facts a reader needs to judge the claim — how much data, and which facts
 * the run does not carry at all.
 */
function scopeLine(scope: ResearchDataScope | null | undefined): string {
  if (!scope) return 'The host recorded no data scope for this conclusion, so no scope is claimed here.'
  const window = scope.time_range
  const windowText = window === null
    ? 'no recorded replay window'
    : `${window.start_inclusive} → ${window.end_exclusive}${window.timezone === null ? '' : ` (${window.timezone}${window.selection_field === null ? '' : `, selected by ${window.selection_field}`})`}`
  return `${scope.record_count} records, recorded replay window ${windowText}. Facts this run does not carry: ${scope.unavailable.length > 0 ? scope.unavailable.join(', ') : 'none'}.`
}

/**
 * The host's own rendering of the value it read from the verified report.
 *
 * `value_text` is what the host published and is shown as written; a record
 * published before that field existed falls back to the value with the exact
 * currency the host recorded. A unit word is never presented as a currency, and
 * a currency the host did not record is named as missing rather than filled in.
 */
function observedReading(reading: ResearchInterpretation['observed']): string {
  if (reading.state !== 'value' || reading.value === undefined) return 'No value could be read for this criterion'
  const text = reading.value_text
    ?? (reading.unit === 'currency'
      ? `${reading.value}${reading.currency ? ` ${reading.currency}` : ' (currency not recorded by the host)'}`
      : `${reading.value}${reading.unit ? ` (${reading.unit})` : ''}`)
  return reading.sign ? `${text} · ${reading.sign}` : text
}

/**
 * The phase this task is in, and what it has spent, in three lines.
 *
 * Every sentence is derived from persisted fields this client already holds: the
 * status, the frozen budget and the measured usage. It says what the state allows
 * and nothing more, and it never offers a step the backend cannot accept from it.
 */
function TaskStatePanel({ task }: { task: ResearchTask }) {
  return <section className="panel task-state" aria-label="Task state">
    <header><h2>{STATE_HEADINGS[task.status]}</h2><span className={`status status-${task.status}`}>{STATUS_LABELS[task.status]}</span></header>
    <p>{STATE_STATEMENTS[task.status]}</p>
    {task.status === 'cancelled' && <p className="notice">This task is cancelled and final. {task.cancel_note ?? 'Started runs keep their own terminal states; cancelling is not a rollback.'}</p>}
    {/* A cancelled task is never resumed here, so the only path forward is a new
        one: a separate experiment, with its own budget and its own confirmation. */}
    {task.status === 'cancelled' && <p className="muted">Nothing further is dispatched for this task and no step of it is resumed here. To run the question again, draft a separate experiment in the <Link to="/research">Research workspace</Link>: a new task has its own budget and requires explicit confirmation before a backtest starts.</p>}
    {task.status === 'unsupported' && <p className="notice">Not executable with the registered strategies and data. {task.gap ?? 'The backend recorded no gap detail.'}</p>}
    <dl className="summary-list">
      <div><dt>Recorded usage</dt><dd>Model calls {task.usage.model_calls}/{task.budget.max_model_calls} · backtests {task.usage.backtests}/{task.budget.max_backtests}</dd></div>
      <div><dt>Last recorded change</dt><dd>{task.updated_at}</dd></div>
    </dl>
  </section>
}

/**
 * Preparing a separate experiment from a terminal task.
 *
 * Every figure is read from the persisted task and the run records this page
 * loaded, and the copy is scoped to what this one action does — it prepares a new,
 * separately budgeted, separately confirmed experiment — rather than to the page,
 * which has its own legal in-place retry and cancel in the block above. The
 * provider's own monetary charge is the one thing this workspace cannot state, and
 * a dispatched or unobserved reservation is not refunded locally.
 *
 * The host refuses a second task for an idea a failed task already holds
 * (`find_active_by_idea`), so while this task is failed the action is disabled and
 * the legal order is stated: cancel first, then prepare. Cancelling is the
 * reader's own explicit click; nothing here cancels on its own, and the prepared
 * experiment is created and confirmed separately.
 */
function TaskRecoveryPanel({ task, readyReports }: { task: ResearchTask; readyReports: number }) {
  const navigate = useNavigate()
  const draft = task.spec_draft ? 1 : 0
  const unavailableCalls = task.usage.unavailable_model_calls ?? 0
  const tokens = unavailableCalls === 0
    ? `${task.usage.input_tokens} input · ${task.usage.output_tokens} output`
    : unavailableCalls === task.usage.model_calls
      ? `None reported for any of the ${task.usage.model_calls} model call(s)`
      : `${task.usage.input_tokens} reported input · ${task.usage.output_tokens} reported output · ${unavailableCalls} call(s) reported none`
  const needsCancelFirst = task.status === 'failed'
  return <section className="panel task-recovery" aria-label="Recovery">
    <header><h2>Starting a separate experiment</h2><span>Kept as it is</span></header>
    <dl className="summary-list">
      <div><dt>Specifications kept</dt><dd>{task.spec_confirmed.length} confirmed · {draft} unconfirmed draft</dd></div>
      <div><dt>Runs started</dt><dd>{task.jobs.length} · {readyReports} of the run records read here have a ready report</dd></div>
      <div><dt>Usage recorded</dt><dd>{task.usage.model_calls} of {task.budget.max_model_calls} model calls · {task.usage.backtests} of {task.budget.max_backtests} backtests</dd></div>
      <div><dt>Reported tokens</dt><dd>{tokens}</dd></div>
    </dl>
    <p className="muted">The provider's charge for those calls is not recorded here: this workspace records usage, not money, so it neither reports a cost nor refunds one, and a reservation that was dispatched or never observed is not refunded locally.</p>
    {needsCancelFirst && <p className="notice">To start a separate experiment with the same idea, cancel this task first using <strong>Cancel task</strong>. Stored runs/reports remain; this does not undo any dispatched work.</p>}
    <div className="actions">
      <button className="primary" disabled={needsCancelFirst}
        onClick={() => { navigate('/research', { state: { recovery: { task_id: task.task_id, idea: task.raw_idea } } }) }}>
        Prepare a new experiment
      </button>
    </div>
    <p className="muted">Preparing copies only your idea and this task's id, and creates and confirms nothing by itself. The new draft gets its own capability review — an empty selection uses the host default route — and its own confirmation before any backtest.</p>
  </section>
}

/**
 * The one conclusion for a succeeded run, exactly as the host rendered it.
 *
 * Nothing here is composed by this client: the outcome, the inference sentence,
 * the criterion reading and the scope sentence are strings the host wrote from
 * the verified report. The question is shown as the user's own input, never as
 * a finding, and the model's own wording is never displayed at all.
 */
function ConclusionHero({ job, interpretation, error }: {
  job: BacktestJob | null
  interpretation: ResearchInterpretation | null
  error: ResearchError | null
}) {
  // The report entry belongs with the outcome, not only in the machinery below
  // it: the verified report is the authority every sentence here is read from.
  const reportEntry = job
    ? <div className="actions hero-actions"><Link className="primary-link" to={`/backtests/${job.job_id}`}>Open the run and its verified report</Link></div>
    : <p className="muted">The backend bound no run to this task, so there is no report to open for it.</p>

  if (!interpretation && !error) return null

  if (!interpretation) {
    return <section className="panel outcome-hero" aria-label="Conclusion">
      <header><h2>Backtest succeeded</h2><span>conclusion not published</span></header>
      {reportEntry}
      <p className="notice">{error ? INTERPRETATION_ERROR_EXPLANATIONS[error.code] ?? 'The conclusion step did not publish a result.' : 'No conclusion was published for this run.'}</p>
      <p className="muted">This is the conclusion axis alone. The host closed it for this run when it recorded the failure, so this page never asks again and no retry exists for it here. The run, its status and its verified report are unchanged.</p>
      {/* The boundary is final, so the reader is given the two things that are
          still open to them instead of a control that would have to pretend
          otherwise: the report that exists, and a new experiment that carries its
          own budget and its own confirmation. */}
      <p className="muted">{job
        ? 'Use the report link above to inspect the run and its current report availability. '
        : 'No run is bound to this task, so there is no report to open for it. '}
        Asking the same question again means a separate experiment, and the preparation for it is offered below: that experiment is budgeted and confirmed on its own.</p>
      {error && <Disclosure className="progressive" title="Recorded conclusion failure" subtitle="The code the host stored">
        <dl className="summary-list"><div><dt>Code</dt><dd>{error.code}</dd></div><div><dt>Detail</dt><dd>{error.message}</dd></div></dl>
      </Disclosure>}
    </section>
  }

  const limitations = interpretation.limitations ?? []
  // The result itself is the host's own outcome sentence, rendered verbatim; the
  // host's outcome code rides along as a machine-readable attribute instead of a
  // second, longer line of status chrome above it. Nothing here composes either.
  return <section className="panel outcome-hero" aria-label="Conclusion" data-outcome={interpretation.outcome}>
    <header><h2>Conclusion</h2></header>
    <p className="outcome-text">{interpretation.outcome_text}</p>
    <dl className="summary-list hero-facts">
      <div><dt>Frozen criterion</dt><dd>{criterionText(interpretation.criterion)}</dd></div>
      <div><dt>Criterion met</dt><dd>{interpretation.criterion_met === true
        ? 'Met'
        : interpretation.criterion_met === false ? 'Not met' : 'Not determined'}</dd></div>
      <div><dt>Read from the verified report</dt><dd>{observedReading(interpretation.observed)}</dd></div>
    </dl>
    <p className="muted">{interpretation.conclusion_scope_text}</p>
    {limitations.length > 0 && <p className="muted">Key limitation: {limitations[0].text}</p>}
    {reportEntry}
    <h3>Data scope</h3>
    <p className="muted">{scopeLine(interpretation.data_scope)}</p>
    <Disclosure className="progressive" title="Inference, every limitation, provenance and the question" subtitle="Host strings, unedited">
      <p className="muted">{interpretation.inference_text}</p>
      <dl className="summary-list"><div><dt>Question (your input)</dt><dd>{interpretation.question}</dd></div></dl>
      {limitations.length > 0 && <>
        <h3>What this run does not show</h3>
        <ul className="assumption-list">{limitations.map(item => <li key={item.code}>{item.text}</li>)}</ul>
      </>}
      <ConclusionScope scope={interpretation.data_scope} />
    </Disclosure>
    <p className="muted">Backtest only. This is one frozen criterion in one deterministic run, not a forecast, a promise of profit or a statement about the idea in general; no figure or sentence here was written by the model.</p>
  </section>
}

/**
 * The key figures of a succeeded run, read from its verified report.
 *
 * The report is loaded through the same read-only adapter and checked against
 * the same run identity the backtest page checks. A report that cannot be read,
 * or that is not this job's own run, leaves this summary out with an explicit
 * sentence instead of showing a stale or invented figure. A currency the report
 * does not record is named as missing and never assumed to be USD.
 */
function RunReportSummary({ api, job, reportLink = false }: { api: ApiAdapter; job: BacktestJob; reportLink?: boolean }) {
  const jobId = job.job_id
  const runId = job.engine_run_id
  // Every settled read is bound to the exact identity it was read for: the run,
  // and the report digest the record claims for it where the record carries one.
  // A refresh of this same job that changes any part of that identity — a
  // withdrawn readiness, a replaced report, a different run — therefore withdraws
  // the stored result instead of leaving it on screen as if it described the newer
  // record; the read below is keyed by the same identity, so it is requested once
  // per identity rather than once per render.
  const identity = `${jobId}@${runId ?? 'no-run'}@${job.report_sha256 ?? 'no-report'}@${job.report_ready ? 'ready' : 'not-ready'}`
  const [loaded, setLoaded] = useState<{ identity: string; report: BacktestReport | null } | null>(null)

  useEffect(() => {
    // A run whose record does not claim a ready report is never read as if it had
    // one: the verified report is claimed only for a run that persisted it.
    if (!job.report_ready) return
    let active = true
    api.getReport(jobId).then(value => {
      if (!active) return
      setLoaded({ identity, report: !runId || value.run_id !== runId ? null : value })
    }).catch(() => { if (active) setLoaded({ identity, report: null }) })
    return () => { active = false }
  }, [api, identity, jobId, runId, job.report_ready])

  const header = <header><h2>What this run measured</h2><span>Read from the verified report</span></header>
  // When this summary is the block a reader meets first, the report it was read
  // from is one click away from here rather than only from the evidence list
  // below. With a conclusion on screen that link belongs to the conclusion, and
  // this panel is supporting detail, so it is not repeated inside it.
  const entry = reportLink
    ? <div className="actions hero-actions"><Link className="primary-link" to={`/backtests/${job.job_id}`}>Open the run and its verified report</Link></div>
    : null
  const panel = (body: ReactNode) => <section className="panel report-summary" aria-label="Verified report summary">{header}{body}{entry}</section>
  if (!job.report_ready) return panel(
    <p className="notice">This run has not recorded a ready verified report, so no figure is read from one here. Open the run for its own persisted state.</p>,
  )
  if (!loaded || loaded.identity !== identity) return panel(<p className="muted">Reading the verified report for this run…</p>)
  if (!loaded.report) return panel(
    <p className="notice">The verified report for this run could not be read, or it does not match this run's identity, so no figure from it is shown here. Open the run for the report itself; nothing is inferred from the job record instead.</p>,
  )
  const report = loaded.report

  const economics = report.economics
  // The unit of an amount is the one the report records for it. A currency this
  // report never recorded stays missing; it is never filled in with a guess.
  const currency = economics.currency ?? economics.ending_cash[0]?.currency ?? economics.initial_funding?.currency ?? null
  const money = (value: { amount: string; currency?: string } | null | undefined) => {
    if (!value) return 'Not recorded'
    const unit = value.currency ?? currency
    return unit ? `${value.amount} ${unit}` : `${value.amount} · currency not recorded by the report`
  }
  // report v1 records no realized/unrealized split at all; the two later schemas do.
  const roundTrip = report.schema === 'ea.backtest-report.v2' || report.schema === 'ea.backtest-report.v3' ? report.economics : null
  const records = job.input_identity.record_count
  return panel(<>
    <dl className="summary-list">
      <div><dt>Net P&amp;L</dt><dd>{money(economics.net_pnl)}</dd></div>
      <div><dt>Fees</dt><dd>{money(economics.fees)}</dd></div>
      <div><dt>Equity at the end of the window</dt><dd>{money(economics.equity)}</dd></div>
      <div><dt>Total return</dt><dd>{pathPercent(economics.total_return.value)} <span className="muted">({economics.total_return.value})</span></dd></div>
      {roundTrip && <div><dt>Realized P&amp;L</dt><dd>{money(roundTrip.realized_pnl)}</dd></div>}
      {roundTrip && <div><dt>Gross unrealized P&amp;L</dt><dd>{money(roundTrip.gross_unrealized_pnl)}</dd></div>}
      <div><dt>Ending position quantity</dt><dd>{economics.ending_positions[0]?.quantity ?? '0'} units{economics.ending_positions.length > 1 ? ` · ${economics.ending_positions.length} ending positions` : ''} — a quantity, not a count of trades</dd></div>
      <div><dt>Records in this run</dt><dd>{records} records</dd></div>
      <div><dt>Report schema</dt><dd>{report.schema} · run {report.run_id}</dd></div>
    </dl>
    {!currency && <p className="muted">This report records no currency, so no unit is stated for its amounts; none is assumed.</p>}
    {!roundTrip && <p className="muted">This report schema records no realized/unrealized split, so none is shown — the net figure above is not presented as realized profit.</p>}
    <p className="muted">Net P&amp;L includes the valuation of any position still open at the end of the window; it is not realized profit. One deterministic run of {records} records carries no robustness claim, and these figures are read from the report rather than recomputed here.</p>
  </>)
}

/** The explicit actions whose refusal is reported beside the control that made it. */
type ActionKind = 'confirm' | 'cancel' | 'retry' | 'interpret'

const ACTION_KIND_LABELS: Record<ActionKind, string> = {
  confirm: 'Confirming the specification',
  cancel: 'Cancelling',
  retry: 'Retrying the failed step',
  interpret: 'Drawing the conclusion',
}

/** A request the backend refused, together with the action that made it. */
type ActionFailureState = { kind: ActionKind; message: string }

/**
 * One refused action, announced where that action was taken.
 *
 * The page-level alert sits under the toolbar, which on a phone is a screen away
 * from the button that failed, so a refused write is reported by the panel that
 * holds that button instead. It takes focus because the reader's own click is the
 * only reason it exists, and it is rendered by exactly one place at a time: the
 * detail page hands it to the panel that owns the action, or summarises it itself
 * when the reload that followed the refusal took that panel away.
 */
function ActionFailure({ failure }: { failure: ActionFailureState }) {
  const ref = useRef<HTMLParagraphElement | null>(null)
  useEffect(() => { ref.current?.focus() }, [failure])
  return <p className="notice error" role="alert" tabIndex={-1} ref={ref}>{failure.message}</p>
}

export function ResearchTaskDetail({ api, taskId }: { api: ApiAdapter; taskId: string }) {
  const [task, setTask] = useState<ResearchTask | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<ActionKind | null>(null)
  // A refused write is kept apart from the read errors above: the read error is
  // about the page, this one is about one button the reader pressed.
  const [actionFailure, setActionFailure] = useState<ActionFailureState | null>(null)
  const [settings, setSettings] = useState<ModelConfigurationStatus | null>(null)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [jobs, setJobs] = useState<Record<string, BacktestJob | null>>({})
  const [jobEpoch, setJobEpoch] = useState(0)
  const [holdouts, setHoldouts] = useState<ChronologicalHoldout[]>([])
  const generation = useRef(0)
  const jobRead = useRef(0)
  const busyRef = useRef(false)

  const refresh = useCallback(async () => {
    const current = ++generation.current
    if (!api.getResearchTask) { setError('This client cannot read research tasks.'); return null }
    try {
      const next = await api.getResearchTask(taskId)
      if (current !== generation.current) return null
      setTask(next); setError(null)
      return next.status
    } catch (caught) {
      if (current === generation.current) setError(messageOf(caught, 'Research task unavailable'))
      return null
    }
  }, [api, taskId])

  useEffect(() => {
    let active = true; let timer: number | undefined
    const poll = async () => {
      const status = await refresh()
      // Poll only while the backend can still advance this task on its own.
      if (active && status && POLL_STATUSES.includes(status)) timer = window.setTimeout(() => { void poll() }, 1000)
    }
    void poll()
    return () => { active = false; generation.current += 1; if (timer !== undefined) window.clearTimeout(timer) }
  }, [refresh, task?.status])

  /**
   * One explicit refresh: the task, and the runs it is bound to.
   *
   * The poll above re-reads the task alone, so a run whose report was withdrawn or
   * replaced while the task kept the same status and the same job ids would keep
   * its last-read figures on screen. This re-reads each bound run once, for the
   * reader who asked for it, and never on a timer of its own.
   */
  const refreshAll = useCallback(async () => {
    const status = await refresh()
    setJobEpoch(value => value + 1)
    return status
  }, [refresh])

  useEffect(() => {
    let active = true
    const request = api.settingsStatus
    if (!request) return () => { active = false }
    request().then(value => { if (active) { setSettings(value); setSettingsError(null) } })
      .catch(caught => { if (active) setSettingsError(messageOf(caught, 'Model configuration status is unavailable.')) })
    return () => { active = false }
  }, [api])

  const jobKey = task?.jobs.join(',') ?? ''
  // A bound run is live while it is accepted, running, or waiting for a stop to be
  // confirmed; the page keeps reading those so `stopping` becomes the real
  // `stopped`/`failed` the controller records, without polling a terminal run.
  const liveJobKey = Object.values(jobs)
    .filter(item => item !== null && ['accepted', 'running', 'stopping'].includes(item.status))
    .map(item => `${item!.job_id}:${item!.status}`)
    .sort()
    .join(',')
  // Each read of the bound runs is bound to the read it started in, so an answer
  // that lands after a newer one was dispatched is discarded rather than allowed
  // to overwrite it. `jobEpoch` moves only on one explicit refresh, so this stays
  // one read per identity and one more per explicit ask instead of per render.
  useEffect(() => {
    if (!jobKey) return
    const at = ++jobRead.current
    let active = true
    Promise.all(jobKey.split(',').map(async jobId => {
      try { return [jobId, await api.getBacktest(jobId)] as const } catch { return [jobId, null] as const }
    })).then(entries => { if (active && at === jobRead.current) setJobs(Object.fromEntries(entries)) })
    return () => { active = false }
  }, [api, jobKey, task?.status, jobEpoch])

  useEffect(() => {
    if (!liveJobKey) return
    const timer = window.setTimeout(() => setJobEpoch(value => value + 1), 400)
    return () => window.clearTimeout(timer)
  }, [liveJobKey])

  useEffect(() => {
    if (!jobKey || !api.listHoldouts) return
    let active = true
    const bound = jobKey.split(',')
    api.listHoldouts()
      .then(items => { if (active) setHoldouts(items.filter(item => bound.includes(item.source_job_id))) })
      .catch(() => { if (active) setHoldouts([]) })
    return () => { active = false }
  }, [api, jobKey])

  const mutate = async (kind: ActionKind, call: () => Promise<ResearchTask>, fallback: string) => {
    if (busyRef.current) return
    // A new attempt clears the last refusal: the reader asked again, and a stale
    // message beside a button that is being pressed now would be a claim about
    // the wrong request.
    busyRef.current = true; setBusy(kind); setError(null); setActionFailure(null)
    // A successful write is newer than every read dispatched before it completed.
    // Invalidate those reads even when the mutation keeps the same task status.
    try {
      const next = await call()
      generation.current += 1
      setTask(next)
    }
    catch (caught) {
      // A failed or uncertain mutation reloads the persisted state instead of
      // assuming what happened; the failure itself is reported beside the action
      // that made it, once, after the reload.
      await refresh()
      setActionFailure({ kind, message: messageOf(caught, fallback) })
    } finally { busyRef.current = false; setBusy(null) }
  }

  if (!task) return <>
    <PageHeading title="Research Task" subtitle={taskId} />
    {error && <p role="alert" className="notice error">{error}</p>}
    {!error && <p className="muted">Loading the persisted research task…</p>}
  </>

  const spec = task.spec_draft ?? task.spec_confirmed.at(-1)?.spec ?? null
  const retryable = canRetry(task)
  // Confirmation is offered only while the backend can still accept it.
  const confirmable = task.status === 'awaiting_confirmation' && task.spec_draft !== null
  const succeededJob = Object.values(jobs).find(job => job?.status === 'succeeded') ?? null
  const succeededJobId = succeededJob?.job_id ?? null

  // Each block is written once, and the order below is the order a reader meets
  // them in: the outcome and the state this task is actually in lead, the legal
  // actions sit beside the state they belong to, and the machinery that produced
  // them stays collapsed under a summary that names it.
  const failure = task.error ? <section className="panel" aria-label="Failure explanation">
    <header><h2>Failure</h2><span>step {task.error.step}</span></header>
    <p>{ERROR_EXPLANATIONS[task.error.code] ?? 'The backend reported a failure for this step.'}</p>
    <dl className="summary-list"><div><dt>Code</dt><dd>{task.error.code}</dd></div><div><dt>Message</dt><dd>{task.error.message}</dd></div></dl>
    <p className="muted">{retryable
      ? 'Retry resumes the failed step in place from the persisted record; it does not restart the research.'
      : NON_RETRYABLE_CODES.has(task.error.code)
        ? 'This failure is not retryable in place: an attempt left this process and its outcome is unproven. Cancel this task and create a new one for another attempt.'
        : 'This failure is not retryable in place.'}</p>
  </section> : null

  // A succeeded task leads with its conclusion, or with the report figures when
  // no conclusion was drawn; its own state needs no separate panel there.
  const state = task.status === 'succeeded' ? null : <TaskStatePanel task={task} />

  // A task the backend can no longer advance either way — failed, cancelled, or a
  // run whose conclusion was refused — offers the same way forward: the record it
  // keeps stays readable, and the question is asked again as a separate
  // experiment. Unknown dispatches are among these, and the panel states plainly
  // that they are never re-submitted from here.
  const readyReports = Object.values(jobs).filter(job => job?.report_ready).length
  const terminal = task.status === 'failed' || task.status === 'cancelled'
    || (task.status === 'succeeded' && task.interpretation_error != null)
  const recovery = terminal ? <TaskRecoveryPanel task={task} readyReports={readyReports} /> : null

  const concluded = task.interpretation != null || task.interpretation_error != null
  const reportSummary = task.status === 'succeeded' && succeededJob
    ? <RunReportSummary key={succeededJob.job_id} api={api} job={succeededJob} reportLink={!concluded} />
    : null
  // With a conclusion on screen the figures it was read from are supporting
  // detail; without one they are the first thing a reader needs.
  const reportBlock = reportSummary && concluded
    ? <Disclosure className="progressive" title="Report figures for this run" subtitle={succeededJobId ?? ''}>{reportSummary}</Disclosure>
    : reportSummary

  // A draft that can still be confirmed leads with the facts being confirmed —
  // the registered surface, the frozen parameters and the frozen criterion — and
  // with the confirm itself, because that is the decision. Its rationale and
  // assumptions are the drafted reasoning behind it, so they sit under a summary
  // that names them rather than pushing the decision off the first screen.
  const specFacts = spec ? <SpecFacts spec={spec} /> : null

  const specification = spec ? (confirmable
    ? <section className="panel research-spec" aria-label="Specification">
      <header><h2>Draft specification</h2><span>Unconfirmed draft</span></header>
      {specFacts}
      {api.confirmResearchTask && <div className="actions">
        <button className="primary" disabled={busy !== null} onClick={() => { void mutate('confirm', () => api.confirmResearchTask!(taskId), 'Confirmation was not accepted; the persisted task was reloaded.') }}>
          {busy === 'confirm' ? 'Confirming…' : 'Confirm spec and start real backtest'}
        </button>
      </div>}
      {actionFailure !== null && actionFailure.kind === 'confirm' && <ActionFailure failure={actionFailure} />}
      <p className="muted">Confirming registers this exact spec and starts one real installed-engine backtest with the frozen parameters. It creates no orders and no Paper session.</p>
      <Disclosure className="progressive" title="Rationale and assumptions" subtitle="Recorded when this draft was proposed">
        <SpecRationale spec={spec} />
      </Disclosure>
    </section>
    : <details className="disclosure progressive">
      <summary><h2>{task.spec_draft ? 'Draft specification' : 'Confirmed specification'}</h2><span>{task.spec_draft ? 'Unconfirmed draft' : `Version ${task.spec_confirmed.at(-1)?.version ?? 1} · confirmed ${task.spec_confirmed.at(-1)?.confirmed_at ?? ''}`}</span></summary>
      <div className="disclosure-body">
        <section className="panel research-spec" aria-label="Specification">
          {specFacts}
          <SpecRationale spec={spec} />
          {task.spec_draft && <p className="muted">This draft can no longer be confirmed: the task is {STATUS_LABELS[task.status]}.</p>}
        </section>
      </div>
    </details>) : null

  const selectionEditor = confirmable && api.pluginCatalog && api.editResearchSpec ? <SelectionEditor api={api} task={task} /> : null

  const declarationDraft = task.declaration_draft
    ? <Disclosure title="Declaration verification" subtitle="Verified against the registered surface">{<DeclarationDraftPanel api={api} draft={task.declaration_draft} />}</Disclosure>
    : null

  const selectionEvidence = <SelectionEvidence task={task} />

  // The runs a task started are machinery below the decision, so they take one
  // summary that names them and counts them; the run cards, their download links
  // and their holdout and candidate evidence stay inside it, unchanged.
  const backtests = task.jobs.length > 0 ? <Disclosure className="progressive" title="Backtests started by this task"
    subtitle={task.jobs.length === 1 ? 'One run · its holdout and candidate evidence' : `${task.jobs.length} runs · their holdout and candidate evidence`}>
    <section className="panel" aria-label="Research backtests">
      <ul className="job-list">{task.jobs.map(jobId => <ResearchJob key={jobId} api={api} jobId={jobId} job={jobs[jobId] ?? null} holdouts={holdouts} />)}</ul>
      <p className="muted">Holdout evaluation and candidate creation stay on the existing evidence pages with their own explicit actions. Candidate acceptance records a research judgment only and grants no execution permission.</p>
      {succeededJobId && <p><Link to={`/backtests/${succeededJobId}`}>Open the succeeded run to evaluate its holdout and create the evaluated candidate</Link></p>}
    </section>
  </Disclosure> : null

  const conclusion = <ConclusionHero
    job={succeededJob}
    interpretation={task.interpretation ?? null}
    error={task.interpretation_error ?? null}
  />

  const conclusionAction = task.status === 'succeeded' && !concluded && (api.interpretResearchTask || api.cancelResearchTask) ? <section className="panel" aria-label="Conclusion action">
    <header><h2>Draw one conclusion</h2><span>Explicit, never automatic</span></header>
    <p className="muted">This asks the host to read the verified report for this run and publish one bounded conclusion against the criterion frozen before it. It spends at most one model call, starts no new backtest, and never changes the result above.</p>
    <div className="actions">
      {api.interpretResearchTask && <button className="primary" disabled={busy !== null} onClick={() => { void mutate('interpret', () => api.interpretResearchTask!(taskId), 'The conclusion request was not accepted; the persisted task was reloaded.') }}>
        {busy === 'interpret' ? 'Interpreting…' : 'Interpret this run'}
      </button>}
      {api.cancelResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('cancel', () => api.cancelResearchTask!(taskId), 'Cancel was not confirmed; the persisted task was reloaded.') }}>{busy === 'cancel' ? 'Cancelling…' : 'Cancel without a conclusion'}</button>}
    </div>
    {actionFailure !== null && (actionFailure.kind === 'interpret' || actionFailure.kind === 'cancel') && <ActionFailure failure={actionFailure} />}
    <p className="muted">Cancelling here closes the conclusion only. It is final, and the completed run and its verified report are left exactly as they are.</p>
  </section> : null

  // Cancellation and retry are legal only while the backend can still accept
  // them, and they belong beside the state they act on, never behind a summary.
  const taskActions = (task.status === 'drafting' || task.status === 'awaiting_confirmation' || task.status === 'confirmed' || task.status === 'running' || task.status === 'failed') && (retryable || api.cancelResearchTask)
    ? <section className="panel" aria-label="Task actions">
      <header><h2>Task actions</h2><span>Explicit, never automatic</span></header>
      <p className="muted">Cancelling is final: it stops future steps and never claims a rollback or kills a backtest that already started. Started runs keep their own terminal states, and a refresh, a back navigation or a failed request never confirms, retries or submits anything.</p>
      <div className="actions">
        {retryable && api.retryResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('retry', () => api.retryResearchTask!(taskId), 'Retry was not accepted; the persisted task was reloaded.') }}>{busy === 'retry' ? 'Retrying…' : 'Retry failed step'}</button>}
        {api.cancelResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('cancel', () => api.cancelResearchTask!(taskId), 'Cancel was not confirmed; the persisted task was reloaded.') }}>{busy === 'cancel' ? 'Cancelling…' : 'Cancel task'}</button>}
      </div>
      {actionFailure !== null && (actionFailure.kind === 'retry' || actionFailure.kind === 'cancel') && <ActionFailure failure={actionFailure} />}
    </section> : null

  // A refusal belongs to the panel that holds the action that made it. The reload
  // that follows it can take that panel away — a refused confirm whose task was
  // confirmed elsewhere, a conclusion the host published anyway — so what no panel
  // shows is summarised on its own below rather than dropped.
  const raisedAnywhere = actionFailure !== null && (
    (actionFailure.kind === 'confirm' && confirmable && api.confirmResearchTask != null)
    || (taskActions !== null && (actionFailure.kind === 'cancel' || actionFailure.kind === 'retry'))
    || (conclusionAction !== null && (actionFailure.kind === 'cancel' || actionFailure.kind === 'interpret')))

  const unclaimedFailure = actionFailure !== null && !raisedAnywhere ? <section className="panel" aria-label="Action failure">
    <header><h2>Review the action outcome</h2><span>{ACTION_KIND_LABELS[actionFailure.kind]}</span></header>
    <ActionFailure failure={actionFailure} />
    <p className="muted">The persisted task was reloaded and no longer offers that action. Review its recorded state below before taking another action; this message alone cannot establish whether the request took effect.</p>
  </section> : null

  // Waiting for a decision is the one state where the specification itself is the
  // decision, so the facts and the confirmation lead and the drafted reasoning
  // follows them; every other state keeps the specification as progressive detail.
  // On a failed or cancelled task the in-place actions come before the prepared
  // experiment: they are the legal next steps, and the copy of the separate
  // experiment must not push them out of reach. Each action is still rendered once.
  const blocked = task.status === 'failed' || task.status === 'cancelled'
  const blocks: ReactNode[] = confirmable
    ? [conclusion, failure, unclaimedFailure, state, recovery, specification, taskActions, selectionEditor, selectionEvidence, declarationDraft, backtests]
    : blocked
      ? [conclusion, failure, unclaimedFailure, state, taskActions, recovery, reportBlock, conclusionAction, specification, selectionEditor, selectionEvidence, backtests, declarationDraft]
      : [conclusion, failure, unclaimedFailure, state, recovery, reportBlock, conclusionAction, taskActions, specification, selectionEditor, selectionEvidence, backtests, declarationDraft]

  return <>
    {/* The page's own scope is stated where the result is, so the heading carries
        no standing subtitle and the chrome above stays a compact bar. */}
    <PageHeading title="Research Task" status={task.status} />
    <div className="result-toolbar"><Link to="/research">← Research workspace</Link><button onClick={() => { void refreshAll() }}>Refresh</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="research-detail-layout"><article className="research-artifact" aria-label="Strategy and validation evidence">
    {blocks.map((node, index) => <Fragment key={index}>{node}</Fragment>)}
    </article><aside className="research-context">
    <UsagePanel task={task} />
    <Disclosure title="Provider, protocol and capabilities" subtitle="Read from the backend configuration">
      <ModelConfigurationPanel status={settings} error={api.settingsStatus ? settingsError : 'Model configuration status is unavailable in this client.'} />
    </Disclosure>
    <Disclosure title="Research idea and task identity" subtitle="Persisted evidence only">
      <p className="muted">{task.raw_idea}</p>
      <dl className="summary-list">
        <div><dt>Task</dt><dd>{task.task_id}</dd></div>
        <div><dt>State</dt><dd>{STATUS_LABELS[task.status]}</dd></div>
        <div><dt>Created</dt><dd>{task.created_at}</dd></div>
        <div><dt>Updated</dt><dd>{task.updated_at}</dd></div>
      </dl>
    </Disclosure>
    </aside></div>
  </>
}

function PageHeading({ title, subtitle, status }: { title: string; subtitle?: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{status && <span className={`status status-${status}`}>{STATUS_LABELS[status as ResearchTaskStatus] ?? status}</span>}</header>
}

export function ResearchSettings({ api }: { api: ApiAdapter }) {
  const [status, setStatus] = useState<ModelConfigurationStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let current = true
    if (!api.settingsStatus) return
    void api.settingsStatus().then(value => { if (current) setStatus(value) }).catch(caught => { if (current) setError(messageOf(caught, 'Configuration status unavailable')) })
    return () => { current = false }
  }, [api])
  // The model connection and configuration lead this page, so what AI research
  // would draft with is inspectable before the local appearance controls.
  return <><PageHeading title="设置" subtitle="当前模型配置、本地外观与本地研究限制。" /><ModelConfigurationPanel status={status} error={api.settingsStatus ? error : '本客户端无法读取模型配置状态。'} /><AppearanceSettings /></>
}
