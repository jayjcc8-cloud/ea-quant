import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, ChronologicalHoldout, ScenarioSummary } from './app'
import {
  AVAILABILITY_PROVIDED,
  AVAILABILITY_SIMULATED,
  LAYER_ORDER,
  MAX_UPLOAD_CHARACTERS,
  ResearchRequestError,
  TIME_FORMAT_IDS,
  isMappedPreview,
  type DeclarationBody,
  type DeclarationCatalog,
  type DeclarationVerdict,
  type LayerName,
  type LocalStrategyCapability,
  type LocalStrategySlot,
  type ModelConfigurationStatus,
  type PluginCatalog,
  type PluginCatalogEntry,
  type PluginSelection,
  type RecipeResolution,
  type ResearchSpec,
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

function Disclosure({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return <details className="disclosure">
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
      {spec.selection && <div><dt>Selected capabilities</dt><dd>{LAYER_ORDER.flatMap(layer => {
        const component = spec.selection?.[layer]
        return component ? [`${LAYER_LABELS[layer]}: ${component.plugin_id} ${component.package_version}`] : []
      }).join(' · ') || 'Host default route'}</dd></div>}
    </dl>
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
  if (error) return <section className="panel" aria-label="Model provider configuration"><header><h2>Model provider</h2><span>Status unavailable</span></header><p className="notice error">{error}</p></section>
  if (!status) return <section className="panel" aria-label="Model provider configuration"><header><h2>Model provider</h2><span>Loading</span></header><p className="muted">Reading the backend model configuration status…</p></section>
  const capabilities = status.capabilities ?? null
  const missing = status.missing_capabilities ?? []
  const capability = (name: 'tool_use' | 'structured_output' | 'usage', whenDeclared: string, whenAbsent: string) =>
    capabilities ? (capabilities[name] ? whenDeclared : whenAbsent) : 'Not configured'
  return <section className="panel" aria-label="Model provider configuration">
    <header><h2>Model provider</h2><span>{status.configured ? 'Configured' : 'Not usable'}</span></header>
    <p className={status.configured ? 'muted' : 'notice error'}>{status.configured
      ? 'Drafting sends the idea to the configured provider. No default provider is used.'
      : `Research cannot draft a spec: ${status.configuration_error ?? 'the model provider is not configured'}. Configure the backend settings file; this page never falls back to a default provider.`}</p>
    <dl className="summary-list">
      <div><dt>Configuration present</dt><dd>{status.configuration_present === undefined ? 'Unavailable' : status.configuration_present ? 'Yes' : 'No'}</dd></div>
      <div><dt>Credential available</dt><dd>{status.credential_available === undefined ? 'Unavailable' : status.credential_available ? 'Yes' : 'No'}</dd></div>
      <div><dt>Real request validation</dt><dd>Not verified by settings; see observed task calls</dd></div>
      <div><dt>Provider value</dt><dd>{status.provider ?? 'Not configured'}</dd></div>
      <div><dt>Protocol</dt><dd>{status.protocol ?? 'Not configured'}</dd></div>
      <div><dt>Model</dt><dd>{status.model ?? 'Not configured'}</dd></div>
      <div><dt>Endpoint</dt><dd>{status.endpoint ?? 'Provider default endpoint'}</dd></div>
      <div><dt>Required capabilities</dt><dd>{(status.required_capabilities ?? []).map(name => `${name} · ${missing.includes(name) ? 'missing' : 'declared'}`).join(' · ') || 'Not configured'}</dd></div>
      <div><dt>Tool use</dt><dd>{capability('tool_use', 'Declared', 'Missing')}</dd></div>
      <div><dt>Structured output</dt><dd>{capability('structured_output', 'Declared', 'Not declared')}</dd></div>
      <div><dt>Usage reporting</dt><dd>{capability('usage', 'Declared', 'Not declared')}</dd></div>
      <div><dt>Settings file</dt><dd>{status.settings_file}</dd></div>
    </dl>
    {status.endpoint && <p className="muted">A custom endpoint must implement the reported protocol. A different URL is not assumed to be compatible.</p>}
    {missing.length > 0 && <p role="alert" className="notice error">The configured protocol does not declare: {missing.join(', ')}. Research fails closed and dispatches nothing.</p>}
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
  const previewUpload = api.previewUpload

  const mapped = mode === 'explicit-mapping'
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
      <label htmlFor="upload-mapping">Mapping declaration (JSON, every strict field declared)</label>
      <textarea id="upload-mapping" rows={5} spellCheck={false} placeholder={MAPPING_EXAMPLE} value={mappingText} disabled={busy !== null}
        onChange={event => { setMappingText(event.target.value); reset() }} />
      <label htmlFor="upload-time">Time declaration (JSON, exact formats and timezone)</label>
      <textarea id="upload-time" rows={3} spellCheck={false} placeholder={TIME_EXAMPLE} value={timeText} disabled={busy !== null}
        onChange={event => { setTimeText(event.target.value); reset() }} />
      <p className="muted">Each destination is one of the strict profile's own field names, and each timestamp names one exact form from {TIME_FORMAT_IDS.join(', ')}. A field you do not declare, a column this file does not have, and a format the values do not match are all refusals rather than guesses.</p>
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
      {mappedPreview.first_error && <p className="notice error">{mappedPreview.first_error.code}: {mappedPreview.first_error.message}{mappedPreview.first_error.record_number ? ` · CSV record ${mappedPreview.first_error.record_number}` : ''}{mappedPreview.first_error.field_name ? ` · field ${mappedPreview.first_error.field_name}` : ''}</p>}
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

function ResearchJob({ api, jobId, job, holdouts }: { api: ApiAdapter; jobId: string; job: BacktestJob | null; holdouts: ChronologicalHoldout[] }) {
  if (!job) return <li className="job-card"><small>{jobId} · persisted job record unavailable</small></li>
  const holdoutForJob = holdouts.filter(item => item.source_job_id === job.job_id)
  return <li className="job-card">
    <div className="job-card-title"><Link to={`/backtests/${job.job_id}`}><strong>{job.job_id}</strong></Link><span className={`status status-${job.status}`}>{job.status}</span></div>
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

export function ResearchTasks({ api }: { api: ApiAdapter }) {
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<ResearchTask[] | null>(null)
  const [idea, setIdea] = useState('')
  const [selection, setSelection] = useState<Partial<Record<LayerName, string>>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const unavailable = !api.listResearchTasks
  const busyRef = useRef(false)

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

  return <>
    <PageHeading title="Research Workspace" subtitle="Persisted ideas, registered restrictive specs, and the real installed backtests they start." />
    {error && <p role="alert" className="notice error">{error}</p>}
    {unavailable && <p className="notice error">This client cannot reach the local research API.</p>}
    <div className="research-list-layout"><section className="panel idea-panel">
      <header><h2>New research idea</h2><span>One active task per idea</span></header>
      <label htmlFor="research-idea">Research idea</label>
      <textarea id="research-idea" maxLength={4000} rows={3} value={idea} disabled={busy}
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
        return <li className="job-card" key={task.task_id}>
          <div className="job-card-title"><Link to={`/research/${task.task_id}`}><strong>{task.raw_idea}</strong></Link><span className={`status status-${task.status}`}>{STATUS_LABELS[task.status]}</span></div>
          <small>{spec ? `${spec.strategy_id} · ${spec.instrument.venue}:${spec.instrument.symbol} · ${spec.initial_cash}` : 'No spec drafted yet'}</small>
          <small>Updated {task.updated_at} · model calls {task.usage.model_calls}/{task.budget.max_model_calls} · backtests {task.usage.backtests}/{task.budget.max_backtests} · jobs {task.jobs.length}</small>
          {task.error && <small>{task.error.step}: {task.error.code}</small>}
          <div className="job-actions"><Link to={`/research/${task.task_id}`}>Open task</Link></div>
        </li>
      })}</ul>
    </section></div>
    {(api.previewUpload || api.publishUpload || api.listDeclarations) && <div className="research-workspace-tools">
      {(api.previewUpload || api.publishUpload) && <section className="panel" aria-label="Controlled dataset upload">
        <header><h2>Controlled data upload</h2><span>Preview, then publish one version</span></header>
        <DataUploadPanel api={api} />
      </section>}
      {api.listDeclarations && <section className="panel" aria-label="Registered declarations">
        <header><h2>Declarations</h2><span>Verified data, installed only by you</span></header>
        <DeclarationCatalogPanel api={api} />
      </section>}
      {(api.localStrategySlot || api.inspectLocalStrategy) && <section className="panel" aria-label="Local strategy package">
        <header><h2>Local strategy package</h2><span>Inspected statically, trusted explicitly</span></header>
        <LocalStrategyPanel api={api} />
      </section>}
    </div>}
    <div className="research-tools"><Link to="/backtests">Backtests</Link><Link to="/batches/new">Run batch</Link><Link to="/holdouts">Chronological holdouts</Link></div>
  </>
}

export function ResearchTaskDetailRoute({ api }: { api: ApiAdapter }) {
  const { taskId = '' } = useParams()
  return <ResearchTaskDetail key={taskId} api={api} taskId={taskId} />
}

export function ResearchTaskDetail({ api, taskId }: { api: ApiAdapter; taskId: string }) {
  const [task, setTask] = useState<ResearchTask | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'confirm' | 'cancel' | 'retry' | null>(null)
  const [settings, setSettings] = useState<ModelConfigurationStatus | null>(null)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [jobs, setJobs] = useState<Record<string, BacktestJob | null>>({})
  const [holdouts, setHoldouts] = useState<ChronologicalHoldout[]>([])
  const generation = useRef(0)
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

  useEffect(() => {
    let active = true
    const request = api.settingsStatus
    if (!request) return () => { active = false }
    request().then(value => { if (active) { setSettings(value); setSettingsError(null) } })
      .catch(caught => { if (active) setSettingsError(messageOf(caught, 'Model configuration status is unavailable.')) })
    return () => { active = false }
  }, [api])

  const jobKey = task?.jobs.join(',') ?? ''
  useEffect(() => {
    if (!jobKey) return
    let active = true
    Promise.all(jobKey.split(',').map(async jobId => {
      try { return [jobId, await api.getBacktest(jobId)] as const } catch { return [jobId, null] as const }
    })).then(entries => { if (active) setJobs(Object.fromEntries(entries)) })
    return () => { active = false }
  }, [api, jobKey, task?.status])

  useEffect(() => {
    if (!jobKey || !api.listHoldouts) return
    let active = true
    const bound = jobKey.split(',')
    api.listHoldouts()
      .then(items => { if (active) setHoldouts(items.filter(item => bound.includes(item.source_job_id))) })
      .catch(() => { if (active) setHoldouts([]) })
    return () => { active = false }
  }, [api, jobKey])

  const mutate = async (kind: 'confirm' | 'cancel' | 'retry', call: () => Promise<ResearchTask>, fallback: string) => {
    if (busyRef.current) return
    busyRef.current = true; setBusy(kind); setError(null)
    // A successful write is newer than every read dispatched before it completed.
    // Invalidate those reads even when the mutation keeps the same task status.
    try {
      const next = await call()
      generation.current += 1
      setTask(next)
    }
    catch (caught) {
      // A failed or uncertain mutation reloads the persisted state instead of
      // assuming what happened; the failure itself is reported after the reload.
      await refresh()
      setError(messageOf(caught, fallback))
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
  const succeededJobId = Object.values(jobs).find(job => job?.status === 'succeeded')?.job_id ?? null
  return <>
    <PageHeading title="Research Task" subtitle="Strategy specification and linked validation evidence." status={task.status} />
    <div className="result-toolbar"><Link to="/research">← Research workspace</Link><button onClick={() => { void refresh() }}>Refresh</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="research-detail-layout"><article className="research-artifact" aria-label="Strategy and validation evidence">
    {task.error && <section className="panel" aria-label="Failure explanation">
      <header><h2>Failure</h2><span>step {task.error.step}</span></header>
      <p>{ERROR_EXPLANATIONS[task.error.code] ?? 'The backend reported a failure for this step.'}</p>
      <dl className="summary-list"><div><dt>Code</dt><dd>{task.error.code}</dd></div><div><dt>Message</dt><dd>{task.error.message}</dd></div></dl>
      <p className="muted">{retryable
        ? 'Retry resumes the failed step in place from the persisted record; it does not restart the research.'
        : NON_RETRYABLE_CODES.has(task.error.code)
          ? 'This failure is not retryable in place: an attempt left this process and its outcome is unproven. Cancel this task and create a new one for another attempt.'
          : 'This failure is not retryable in place.'}</p>
    </section>}

    {spec && <section className="panel research-spec" aria-label="Specification">
      <header><h2>{task.spec_draft ? 'Draft specification' : 'Confirmed specification'}</h2><span>{task.spec_draft ? 'Unconfirmed draft' : `Version ${task.spec_confirmed.at(-1)?.version ?? 1} · confirmed ${task.spec_confirmed.at(-1)?.confirmed_at ?? ''}`}</span></header>
      <SpecFacts spec={spec} />
      {confirmable && api.confirmResearchTask && <div className="actions">
        <button className="primary" disabled={busy !== null} onClick={() => { void mutate('confirm', () => api.confirmResearchTask!(taskId), 'Confirmation was not accepted; the persisted task was reloaded.') }}>
          {busy === 'confirm' ? 'Confirming…' : 'Confirm spec and start real backtest'}
        </button>
      </div>}
      {task.spec_draft && (confirmable
        ? <p className="muted">Confirming registers this exact spec and starts one real installed-engine backtest with the frozen parameters. It creates no orders and no Paper session.</p>
        : <p className="muted">This draft can no longer be confirmed: the task is {STATUS_LABELS[task.status]}.</p>)}
    </section>}

    {confirmable && api.pluginCatalog && api.editResearchSpec && <SelectionEditor api={api} task={task} />}

    {task.declaration_draft && <DeclarationDraftPanel api={api} draft={task.declaration_draft} />}

    <SelectionEvidence task={task} />

    {task.jobs.length > 0 && <section className="panel" aria-label="Research backtests">
      <header><h2>Backtests started by this task</h2><span>Next: existing chronological holdout, then evaluated candidate</span></header>
      <ul className="job-list">{task.jobs.map(jobId => <ResearchJob key={jobId} api={api} jobId={jobId} job={jobs[jobId] ?? null} holdouts={holdouts} />)}</ul>
      <p className="muted">Holdout evaluation and candidate creation stay on the existing evidence pages with their own explicit actions. Candidate acceptance records a research judgment only and grants no execution permission.</p>
      {succeededJobId && <p><Link to={`/backtests/${succeededJobId}`}>Open the succeeded run to evaluate its holdout and create the evaluated candidate</Link></p>}
    </section>}

    </article><details className="research-context" open><summary>AI context & task controls</summary>
    <section className="panel identity-panel">
      <header><h2>Research idea</h2></header><p className="muted">{task.raw_idea}</p>
      <header className="context-identity-heading"><h2>Task identity</h2><span>Persisted evidence only</span></header>
      <dl className="summary-list">
        <div><dt>Task</dt><dd>{task.task_id}</dd></div>
        <div><dt>State</dt><dd>{STATUS_LABELS[task.status]}</dd></div>
        <div><dt>Created</dt><dd>{task.created_at}</dd></div>
        <div><dt>Updated</dt><dd>{task.updated_at}</dd></div>
      </dl>
      {task.status === 'cancelled' && <p className="notice">This task is cancelled and final. {task.cancel_note ?? 'Started runs keep their own terminal states; cancelling is not a rollback.'}</p>}
      {task.status === 'unsupported' && <p className="notice">Not executable with the registered strategies and data. {task.gap ?? 'The backend recorded no gap detail.'}</p>}
    </section>
    <ModelConfigurationPanel status={settings} error={api.settingsStatus ? settingsError : 'Model configuration status is unavailable in this client.'} />
    <UsagePanel task={task} />

    {(task.status !== 'cancelled' && task.status !== 'unsupported' && task.status !== 'succeeded') && <section className="panel" aria-label="Task actions">
      <header><h2>Task actions</h2><span>Explicit, never automatic</span></header>
      <p className="muted">Cancelling is final. It stops future steps and never claims a rollback or kills a backtest that already started; started runs keep their own terminal states. A refresh, back navigation or failed request never confirms, retries or submits anything.</p>
      <div className="actions">
        {retryable && api.retryResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('retry', () => api.retryResearchTask!(taskId), 'Retry was not accepted; the persisted task was reloaded.') }}>{busy === 'retry' ? 'Retrying…' : 'Retry failed step'}</button>}
        {api.cancelResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('cancel', () => api.cancelResearchTask!(taskId), 'Cancel was not confirmed; the persisted task was reloaded.') }}>{busy === 'cancel' ? 'Cancelling…' : 'Cancel task'}</button>}
      </div>
    </section>}
    </details></div>
  </>
}

function PageHeading({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{status && <span className={`status status-${status}`}>{STATUS_LABELS[status as ResearchTaskStatus] ?? status}</span>}</header>
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
  return <><PageHeading title="Settings" subtitle="Current model configuration and local research limits." /><ModelConfigurationPanel status={status} error={api.settingsStatus ? error : 'Model configuration status is unavailable in this client.'} /></>
}
