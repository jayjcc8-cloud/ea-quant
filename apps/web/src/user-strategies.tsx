/**
 * QUINE: one durable user strategy, the AI work the user approved, and its frozen runs.
 *
 * The page is three areas and one owner. The left column is every strategy in this
 * workspace and the draft revision each one is on; the centre is the selected
 * strategy — its draft, the typed changes proposed against it, and the experiments
 * confirmed from it; the right column is the AI conversation, which is always the
 * conversation of the strategy that is open.
 *
 * Three rules this file exists to keep visible:
 *
 * * creating a strategy is a save — it calls no model and starts no backtest;
 * * one model call happens only after an explicit approval click, and everything a
 *   model says is labelled inference, never evidence;
 * * an experiment starts only from an explicit confirmation of facts the user just
 *   read, and the run itself lives on the existing backtest and report pages.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, ScenarioSummary } from './app'
import type { ReportCriterion } from './research-api'
import {
  UserStrategyRequestError,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyDraft,
  type UserStrategyExperiment,
  type UserStrategyPreview,
  type UserStrategyProposal,
  type UserStrategyProposalStatus,
} from './user-strategies-api'

/**
 * The allowlisted criterion fields, in the report's own paths. This list is
 * presentation only: the host owns the vocabulary and refuses a field it does not
 * know, so a field this map has never heard of is shown exactly as it was written.
 */
const CRITERION_FIELDS: { field: string; label: string }[] = [
  { field: 'economics.net_pnl.amount', label: 'Net P&L' },
  { field: 'economics.realized_pnl.amount', label: 'Realized P&L' },
  { field: 'economics.fees.amount', label: 'Fees paid' },
  { field: 'economics.total_return.value', label: 'Total return' },
  { field: 'economics.counts.fills', label: 'Fills' },
  { field: 'economics.counts.orders', label: 'Orders' },
  { field: 'economics.completed_round_trips', label: 'Completed round trips' },
]
const EXPECTATIONS = ['positive', 'negative', 'zero'] as const

/** One approval allows exactly one model call, and no experiment. */
const CALL_APPROVAL: ApprovedBudget = { max_model_calls: 1, max_backtests: 0 }
/** One confirmation allows exactly one experiment, and no model call. */
const RUN_APPROVAL: ApprovedBudget = { max_model_calls: 0, max_backtests: 1 }

/** What the host's own refusal means, in the user's words. */
const REQUEST_EXPLANATIONS: Record<string, string> = {
  stale_base_revision:
    'The draft revision moved on. Reload the strategy before applying this change.',
  operation_conflict:
    'That operation id is already bound to a different change, so nothing was applied.',
  approval_required: 'An explicit approval is required, and it must fit this workspace budget.',
  user_strategy_not_found: 'That strategy is not in this workspace. Reload the list.',
  request_failed: 'The local service refused the request and nothing changed.',
}

const CALL_STATUS_LABELS: Record<UserStrategyCall['status'], string> = {
  pending: 'Recorded, not yet sent',
  dispatched: 'Asked, outcome not yet observed',
  observed: 'Observed',
  refused: 'Refused',
  unconfigured: 'Unconfigured',
  unknown: 'Unknown outcome',
  interrupted_before_dispatch: 'Interrupted before it was sent',
}

const PROPOSAL_STATUS_LABELS: Record<UserStrategyProposalStatus, string> = {
  open: 'Open',
  applied: 'Applied',
  rejected: 'Rejected',
  superseded: 'Superseded',
}

const EXPERIMENT_NOTES: Record<string, string> = {
  pending: 'This confirmation was recorded before a run existed. Nothing was dispatched.',
  interrupted_before_dispatch:
    'No run was ever dispatched for this confirmation, and no duplicate was created.',
  unknown:
    'A run may have been dispatched before the interruption. It is not re-sent, not counted, and not refunded until it is found.',
  failed: 'This experiment was refused before any run existed.',
}

const EXPERIMENT_STATUS_LABELS: Record<UserStrategyExperiment['status'], string> = {
  pending: 'recorded, not dispatched',
  created: 'run started',
  failed: 'refused before any run',
  unknown: 'outcome unknown',
  interrupted_before_dispatch: 'never dispatched',
}

const explain = (error: unknown) =>
  error instanceof UserStrategyRequestError
    ? REQUEST_EXPLANATIONS[error.code] ?? error.message
    : error instanceof Error
      ? error.message
      : 'The request failed and nothing changed.'

const strategyLine = (draft: UserStrategyDraft) =>
  `${draft.strategy_id} · ${draft.venue}:${draft.symbol}`

const revisionLine = (revision: number) => `Draft revision ${revision}`

/** The criterion exactly as the report names it: schema, path, expectation. */
const criterionLine = (criterion: ReportCriterion | null) =>
  criterion === null
    ? 'None was stated, so this run cannot be concluded for or against the idea'
    : `${criterion.schema} ${criterion.field} ${criterion.expectation}`

const criterionLabel = (criterion: ReportCriterion | null) => {
  if (criterion === null) return 'None stated'
  const known = CRITERION_FIELDS.find(item => item.field === criterion.field)
  return `${known ? known.label : criterion.field} expected ${criterion.expectation}`
}

const fingerprintOf = (data: Record<string, unknown>) => {
  const fingerprint = data?.fingerprint as { sha256?: string } | undefined
  return typeof fingerprint?.sha256 === 'string' ? fingerprint.sha256 : null
}

const dataLine = (preview: UserStrategyPreview) => {
  const sha = fingerprintOf(preview.data)
  if (preview.record_count === null && sha === null) return 'Data identity unavailable'
  return `${preview.record_count ?? 'Unknown'} records${sha ? ` · ${sha.slice(0, 8)}` : ''}`
}

/**
 * One cost assumption exactly as the scenario declares it. A scenario that
 * declares none says so; nothing is filled in, guessed or priced here.
 */
const factLine = (value: unknown) => {
  if (value === null || value === undefined) return 'not declared in this scenario'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([key, item]) => `${key} ${String(item)}`).join(', ') : 'declared empty'
  }
  return String(value)
}

const costLine = (preview: UserStrategyPreview) =>
  `commission ${factLine(preview.commission)} · slippage ${factLine(preview.slippage)} · latency ${factLine(preview.latency)}`

const usageLine = (call: UserStrategyCall) =>
  call.usage_available
    ? `${call.input_tokens ?? 0} input tokens · ${call.output_tokens ?? 0} output tokens`
    : 'Token usage was not reported for this call, so it is not counted as zero'

/**
 * What to do next when a call did not produce an answer. Unconfigured and unknown
 * are different states: one has a provider to set up, the other may already have
 * reached one and is never re-sent silently.
 */
function CallNextStep({ call }: { call: UserStrategyCall }) {
  if (call.status === 'unconfigured') {
    return <p className="muted">
      No model provider is configured here, so this call was recorded and never sent.
      Configure a provider in <Link to="/settings">Settings</Link>, then ask again — that
      is a new call with a new approval.
    </p>
  }
  if (call.status === 'unknown') {
    return <p className="muted">
      This call may have reached the provider before the interruption, so it is not re-sent
      and not refunded. If you want an answer, approve a new call explicitly. Check
      <Link to="/settings"> Settings</Link> if the provider is not the one you expected.
    </p>
  }
  return null
}

/**
 * Create one strategy. This is a save: the scenario is read to freeze a valid
 * starting draft, and no provider, job or child experiment is touched.
 */
function CreateStrategy({ api, scenarios, created }: {
  api: ApiAdapter
  scenarios: ScenarioSummary[]
  created: (strategy: UserStrategy) => void
}) {
  const [name, setName] = useState('')
  const [intent, setIntent] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [field, setField] = useState('')
  const [expectation, setExpectation] = useState<string>('positive')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const usable = scenarios.filter(item => item.valid && item.summary && item.scenario_id)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    // Captured once: the approved action is the one this click was made with.
    const createStrategy = api.createUserStrategy
    if (!createStrategy) {
      setNote('Creating a strategy is unavailable in this workspace.')
      return
    }
    setBusy(true)
    setNote('')
    try {
      const strategy = await createStrategy({
        name: name.trim(),
        intent: intent.trim(),
        scenario_id: scenarioId,
        // The host validates this against the report's own vocabulary: what the
        // creator states is a field and a sign, and what is stored is the complete
        // validated criterion the host froze.
        ...(field ? { criterion: { field, expectation } } : {}),
      })
      setName('')
      setIntent('')
      setField('')
      created(strategy)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="New strategy" className="strategy-create">
    <h2>New strategy</h2>
    <form onSubmit={submit}>
      <div className="field">
        <label htmlFor="strategy-name">Strategy name</label>
        <input id="strategy-name" value={name} maxLength={120} required
          onChange={event => setName(event.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="strategy-intent">Intent</label>
        <textarea id="strategy-intent" value={intent} maxLength={2000} required rows={3}
          onChange={event => setIntent(event.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="strategy-scenario">Scenario</label>
        <select id="strategy-scenario" value={scenarioId} required
          onChange={event => setScenarioId(event.target.value)}>
          <option value="">Select a registered scenario</option>
          {usable.map(item => <option key={item.scenario_id} value={item.scenario_id}>
            {`${item.summary?.strategy_id} · ${item.summary?.venue}:${item.summary?.symbol}`}
          </option>)}
        </select>
        {!usable.length && <small>No valid registered scenario is available right now.</small>}
      </div>
      <div className="field">
        <label htmlFor="strategy-criterion-field">Criterion field</label>
        <select id="strategy-criterion-field" value={field}
          onChange={event => setField(event.target.value)}>
          <option value="">No criterion</option>
          {CRITERION_FIELDS.map(item => <option key={item.field} value={item.field}>{item.label}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor="strategy-criterion-expectation">Expected sign</label>
        <select id="strategy-criterion-expectation" value={expectation} disabled={!field}
          onChange={event => setExpectation(event.target.value)}>
          {EXPECTATIONS.map(item => <option key={item} value={item}>{item}</option>)}
        </select>
      </div>
      <p className="muted">Creating a strategy saves a draft. It calls no model and starts no backtest.</p>
      <button type="submit" disabled={busy || !name.trim() || !intent.trim() || !scenarioId}>
        Create strategy
      </button>
      {note && <p role="alert" className="notice error">{note}</p>}
    </form>
  </section>
}

function DraftPanel({ draft }: { draft: UserStrategyDraft }) {
  return <section aria-label="Draft" className="strategy-panel">
    <h2>Draft</h2>
    <p>{revisionLine(draft.revision)}</p>
    <p>{strategyLine(draft)}</p>
    <p>{`Initial cash ${draft.initial_cash} USD`}</p>
    <p>Criterion: {criterionLabel(draft.criterion)}</p>
    <p className="muted">Frozen experiment evidence is reported against the revision it ran, never against this head.</p>
    <details>
      <summary>Registered parameters</summary>
      <ul className="parameter-list">
        {Object.entries(draft.parameters).map(([key, value]) =>
          <li key={key}>{`${key}: ${value}`}</li>)}
      </ul>
    </details>
  </section>
}

/** The AI conversation: approved calls, labelled inference, no implied authority. */
function ChatPanel({ api, strategy, changed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
}) {
  const [message, setMessage] = useState('')
  const [approved, setApproved] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const latest = strategy.calls.at(-1)
  const unresolved = strategy.calls.some(call =>
    call.status === 'pending' || call.status === 'dispatched')

  const ask = async () => {
    if (!approved) {
      setNote('Approving one model call is required before the model is asked.')
      return
    }
    const send = api.sendUserStrategyMessage
    if (!send) {
      setNote('Model collaboration is unavailable in this workspace.')
      return
    }
    setBusy(true)
    setNote('')
    try {
      const updated = await send(strategy.user_strategy_id, {
        call_id: `call-${strategy.calls.length + 1}`,
        base_revision: strategy.draft.revision,
        text: message.trim(),
        approved_budget: CALL_APPROVAL,
      })
      changed(updated)
      setMessage('')
      setApproved(false)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="AI collaboration" className="strategy-panel strategy-chat">
    <h2>AI collaboration</h2>
    <p className="muted">
      One approval allows exactly one call against this draft revision. The model may explain,
      and may propose a typed change, but it never runs anything and never decides a result.
    </p>
    <div className="field">
      <label htmlFor="strategy-message">Message</label>
      <textarea id="strategy-message" rows={3} value={message} maxLength={4000}
        onChange={event => setMessage(event.target.value)} />
    </div>
    <label className="check" htmlFor="strategy-approve">
      <input id="strategy-approve" type="checkbox" checked={approved}
        onChange={event => { setApproved(event.target.checked); setNote('') }} />
      Approve one model call
    </label>
    <button type="button" onClick={ask} disabled={busy || !message.trim()}>Ask the model</button>
    {note && <p role="alert" className="notice error">{note}</p>}
    {latest && <div className="call-note">
      <p><span className={`status status-${latest.status}`}>{CALL_STATUS_LABELS[latest.status]}</span></p>
      <p className="muted">{usageLine(latest)}</p>
      {latest.cost && !latest.cost.available &&
        <p className="muted">{`Cost unavailable: ${latest.cost.reason}.`}</p>}
      {latest.message && <p className="notice">{latest.message}</p>}
      <CallNextStep call={latest} />
    </div>}
    {unresolved && <p className="notice">
      A recorded call is still unresolved. Its outcome is settled before another one is
      asked, so nothing is overwritten.
    </p>}
    {strategy.messages.length > 0 && <ol className="message-list">
      {strategy.messages.map(item => <li key={item.message_id} className={`message message-${item.role}`}>
        <p>{item.text}</p>
        {item.role === 'model' && <p className="muted">Model inference — not formal evidence.</p>}
      </li>)}
    </ol>}
  </section>
}

/**
 * One typed proposal: the checked subset is exactly what Apply sends.
 *
 * This panel is mounted per proposal identity and per revision, so the checked
 * set and the approval below always belong to the proposal on screen: switching
 * strategy or revision mounts a fresh panel instead of carrying a stale
 * selection, a stale approval or a stale undo target across.
 */
function ProposalPanel({ api, strategy, proposal, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  proposal: UserStrategyProposal
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const [checked, setChecked] = useState<string[]>(proposal.items.map(item => item.path))
  const [reviseMessage, setReviseMessage] = useState('')
  const [reviseApproved, setReviseApproved] = useState(false)
  const attempts = useRef(0)
  const [busy, setBusy] = useState(false)

  const open = proposal.status === 'open'

  const toggle = (path: string, on: boolean) =>
    setChecked(current => on ? [...current, path] : current.filter(item => item !== path))

  const apply = async () => {
    const selected = proposal.items.map(item => item.path).filter(path => checked.includes(path))
    if (!selected.length) {
      failed('Select at least one field to apply, or reject the proposal instead.')
      return
    }
    const applyChange = api.applyUserStrategyProposal
    if (!applyChange) {
      failed('Applying a change is unavailable in this workspace.')
      return
    }
    setBusy(true)
    failed('')
    const attempt = attempts.current
    try {
      // One intent, one operation id: a retry of this click replays it instead of
      // applying a second change.
      const updated = await applyChange(
        strategy.user_strategy_id,
        proposal.proposal_id,
        { operation_id: `op-${proposal.proposal_id}-${proposal.base_revision}-${attempt}`.slice(0, 64), paths: selected },
      )
      attempts.current += 1
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const reject = async () => {
    const rejectChange = api.rejectUserStrategyProposal
    if (!rejectChange) return
    setBusy(true)
    failed('')
    try {
      changed(await rejectChange(strategy.user_strategy_id, proposal.proposal_id))
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const undo = async () => {
    const undoChange = api.undoUserStrategyChange
    if (!undoChange) return
    // The change this click means, named exactly: the latest application of the
    // revision on screen. A stale page cannot undo whatever happens to be newest.
    const target = strategy.changes.filter(item => item.kind === 'apply').at(-1)
    if (!target || target.to_revision !== strategy.draft.revision) {
      failed('There is no applied change on this revision to undo.')
      return
    }
    setBusy(true)
    failed('')
    try {
      changed(await undoChange(strategy.user_strategy_id, {
        operation_id: `undo-${target.operation_id}-${strategy.draft.revision}`.slice(0, 64),
        head_revision: strategy.draft.revision,
        target_operation_id: target.operation_id,
      }))
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const revise = async () => {
    if (!reviseApproved) {
      failed('Approving one model call is required before the change is revised.')
      return
    }
    const reviseChange = api.reviseUserStrategyProposal
    if (!reviseChange) {
      failed('Revising a proposal is unavailable in this workspace.')
      return
    }
    setBusy(true)
    failed('')
    try {
      // A fresh call with a fresh approval: the old proposal is kept as history and
      // this one is superseded by whatever the model proposes on the current base.
      const updated = await reviseChange(strategy.user_strategy_id, proposal.proposal_id, {
        call_id: `revise-${proposal.proposal_id}-${strategy.draft.revision}`.slice(0, 64),
        text: reviseMessage.trim() || 'Revise the proposed change',
        approved_budget: CALL_APPROVAL,
      })
      setReviseApproved(false)
      setReviseMessage('')
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const undone = strategy.changes.filter(item => item.kind === 'apply').at(-1)
  const canUndo = Boolean(undone && undone.to_revision === strategy.draft.revision)

  return <section aria-label="Proposed change" className="strategy-panel">
    <h2>{`Proposed change to revision ${proposal.base_revision}`}</h2>
    <p><span className={`status status-${proposal.status}`}>{PROPOSAL_STATUS_LABELS[proposal.status]}</span></p>
    <ul className="proposal-list">
      {proposal.items.map(item => <li key={item.path}>
        <label className="check" htmlFor={`${proposal.proposal_id}-${item.path}`}>
          <input id={`${proposal.proposal_id}-${item.path}`} type="checkbox"
            checked={checked.includes(item.path)} disabled={!open}
            onChange={event => toggle(item.path, event.target.checked)} />
          {item.path}
        </label>
        <p className="change"><span>{item.before ?? 'none'} → {item.after}</span></p>
        <p className="reason">{item.reason}</p>
        {item.evidence.length > 0 && <p className="muted">
          {`The model cited: ${item.evidence.join(', ')} — a claim it made, not a verified fact.`}
        </p>}
      </li>)}
    </ul>
    <div className="actions">
      <button type="button" onClick={apply} disabled={busy || !open}>Apply selected</button>
      <button type="button" onClick={reject} disabled={busy || !open}>Reject proposal</button>
      <button type="button" onClick={undo} disabled={busy || !canUndo}>Undo the latest change</button>
    </div>
    <p className="muted">
      Applying writes one new revision from the whole validated draft: if the selected fields cannot
      hold together, nothing is applied at all. Fields you leave unchecked stay in the old proposal.
    </p>
    {open && <div className="revise">
      <div className="field">
        <label htmlFor={`${proposal.proposal_id}-revise`}>Ask for a different change</label>
        <textarea id={`${proposal.proposal_id}-revise`} rows={2} maxLength={4000}
          value={reviseMessage} onChange={event => setReviseMessage(event.target.value)} />
      </div>
      <label className="check" htmlFor={`${proposal.proposal_id}-revise-approve`}>
        <input id={`${proposal.proposal_id}-revise-approve`} type="checkbox" checked={reviseApproved}
          onChange={event => { setReviseApproved(event.target.checked); failed('') }} />
        Approve one model call for this revision
      </label>
      <button type="button" onClick={revise} disabled={busy}>Revise this proposal</button>
      <p className="muted">
        Revising keeps this proposal as history and asks the model again, on the current
        revision, with this one approved call.
      </p>
    </div>}
  </section>
}

/** An experiment preview and the one explicit confirmation that launches it. */
function ExperimentPanel({ api, strategy, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const [preview, setPreview] = useState<UserStrategyPreview | null>(null)
  const [busy, setBusy] = useState(false)

  const previewExperiment = async () => {
    const readPreview = api.previewUserStrategyExperiment
    if (!readPreview) return
    setBusy(true)
    failed('')
    try {
      setPreview(await readPreview(strategy.user_strategy_id))
    } catch (error) {
      setPreview(null)
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    if (!preview) {
      failed('Preview the experiment first: nothing starts before you have read what it would run.')
      return
    }
    const launch = api.launchUserStrategyExperiment
    if (!launch) {
      failed('Starting an experiment is unavailable in this workspace.')
      return
    }
    setBusy(true)
    failed('')
    const intent = `li-${strategy.experiments.length + 1}`
    try {
      const updated = await launch(strategy.user_strategy_id, {
        launch_intent_id: intent,
        base_revision: preview.draft_revision,
        // The confirmation names the input it was made against, so a preview that
        // no longer describes what would run is refused instead of silently run.
        preview_identity: preview.preview_identity,
        approved_budget: RUN_APPROVAL,
      })
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="Experiment" className="strategy-panel">
    <h2>Experiment</h2>
    <div className="actions">
      <button type="button" onClick={previewExperiment} disabled={busy}>Preview experiment</button>
      <button type="button" onClick={start} disabled={busy || !preview}>Start experiment</button>
    </div>
    {preview && <dl className="preview-facts">
      <div><dt>Draft</dt><dd>{revisionLine(preview.draft_revision)}</dd></div>
      <div><dt>Strategy</dt><dd>{`${preview.strategy_id} · ${preview.venue}:${preview.symbol}`}</dd></div>
      <div><dt>Funding</dt><dd>{`Initial cash ${preview.initial_cash} USD`}</dd></div>
      <div><dt>Data</dt><dd>{dataLine(preview)}</dd></div>
      <div><dt>Cost assumptions</dt><dd>{costLine(preview)}</dd></div>
      <div><dt>Input identity</dt><dd>{preview.preview_identity.slice(0, 19)}</dd></div>
      <div><dt>Criterion</dt><dd>{`Criterion ${criterionLine(preview.criterion)}`}</dd></div>
      <div><dt>Budget this workspace grants</dt><dd>
        {`${preview.budget.max_model_calls} model calls · ${preview.budget.max_backtests} backtests`}
      </dd></div>
    </dl>}
    {strategy.experiments.length > 0 && <ul className="experiment-list">
      {strategy.experiments.map(item => <li key={item.launch_intent_id}>
        {item.status === 'created'
          ? <p>{`Started experiment ${item.launch_intent_id} from revision ${item.payload.draft_revision}`}</p>
          : <p>{`Experiment ${item.launch_intent_id} from revision ${item.payload.draft_revision} — ${EXPERIMENT_STATUS_LABELS[item.status]}`}</p>}
        {item.status === 'created' && item.job_id &&
          <Link to={`/backtests/${encodeURIComponent(item.job_id)}`}>
            Open the run to check its status and report
          </Link>}
        {item.status !== 'created' && <p className="notice">
          {EXPERIMENT_NOTES[item.status] ?? 'This experiment has no confirmed run.'}
          {item.message ? ` ${item.message}` : ''}
        </p>}
      </li>)}
    </ul>}
    <p className="muted">
      The run and its status live on the backtest page, and its report exists only once that
      run succeeded — a started run is not a report. A confirmed experiment keeps the revision
      it froze even after the draft moves on.
    </p>
    {strategy.experiments.some(item => item.status === 'created') && <p className="muted">
      One run is one frozen result, not research that is finished: a holdout, the next question
      and the conclusion belong on the <Link to="/holdouts">holdout</Link> and
      <Link to="/research"> research</Link> pages.
    </p>}
  </section>
}

/** The latest frozen experiment of one strategy, or nothing when it has none. */
const latestExperimentLine = (strategy: UserStrategy) => {
  const latest = strategy.experiments.at(-1)
  if (!latest) return null
  return `Frozen run from revision ${latest.payload.draft_revision} · ${EXPERIMENT_STATUS_LABELS[latest.status]}`
}

function StrategyList({ strategies, selectedId }: { strategies: UserStrategy[]; selectedId?: string }) {
  return <nav aria-label="Strategies" className="strategy-list">
    {strategies.length === 0
      ? <p>No strategies yet.</p>
      : <ul>{strategies.map(item => <li key={item.user_strategy_id}
          className={item.user_strategy_id === selectedId ? 'selected' : undefined}>
          <Link to={`/strategies/${encodeURIComponent(item.user_strategy_id)}`}>{item.name}</Link>
          <span className="muted">{revisionLine(item.draft.revision)}</span>
          <span className="muted">
            {item.changes.length === 0
              ? 'No applied change yet'
              : `${item.changes.length} recorded change${item.changes.length === 1 ? '' : 's'}`}
          </span>
          {latestExperimentLine(item) && <span className="muted">{latestExperimentLine(item)}</span>}
        </li>)}</ul>}
  </nav>
}

/** The workspace itself: the list, the selected strategy and its AI column. */
export function StrategiesWorkspace({ api }: { api: ApiAdapter }) {
  const { userStrategyId } = useParams()
  const navigate = useNavigate()
  const [strategies, setStrategies] = useState<UserStrategy[]>([])
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [detail, setDetail] = useState<{ id: string; value: UserStrategy } | null>(null)
  const [listNote, setListNote] = useState('')
  const [detailNote, setDetailNote] = useState('')
  const [actionNote, setActionNote] = useState('')

  useEffect(() => {
    let active = true
    const load = async () => {
      const list = api.listUserStrategies
      if (!list) {
        // The optional method is settled asynchronously, so a workspace that
        // unmounts or swaps its api never writes this note from a stale load.
        await Promise.resolve()
        if (active) setListNote('This workspace does not serve user strategies.')
        return
      }
      try {
        const items = await list()
        if (active) {
          setStrategies(items)
          setListNote('')
        }
      } catch (error) {
        if (active) setListNote(explain(error))
      }
    }
    void load()
    return () => { active = false }
  }, [api])

  useEffect(() => {
    let active = true
    api.listScenarios?.().then(items => { if (active) setScenarios(items) }).catch(() => { if (active) setScenarios([]) })
    return () => { active = false }
  }, [api])

  useEffect(() => {
    const read = api.getUserStrategy
    if (!userStrategyId || !read) return
    let active = true
    read(userStrategyId)
      .then(value => { if (active) { setDetail({ id: value.user_strategy_id, value }); setDetailNote('') } })
      .catch(error => { if (active) setDetailNote(explain(error)) })
    return () => { active = false }
  }, [api, userStrategyId])

  // The render is derived from the routed id, never from whatever was read last:
  // a strategy that is no longer the open one is never rendered as if it were, so
  // a slow answer or a stale callback cannot put another object's editable draft,
  // preview or approval under this route.
  const current = detail && detail.id === userStrategyId ? detail.value : null

  const changed = useCallback((value: UserStrategy) => {
    setDetail({ id: value.user_strategy_id, value })
    setActionNote('')
    setStrategies(items => items.some(item => item.user_strategy_id === value.user_strategy_id)
      ? items.map(item => item.user_strategy_id === value.user_strategy_id ? value : item)
      : [...items, value])
  }, [])

  const created = useCallback((value: UserStrategy) => {
    setStrategies(items => [...items, value])
    navigate(`/strategies/${encodeURIComponent(value.user_strategy_id)}`)
  }, [navigate])

  const proposal = current && current.proposals.length > 0
    ? current.proposals[current.proposals.length - 1]
    : null

  return <div className="strategy-workspace">
    <div className="strategy-column">
      <header className="strategy-heading">
        <h1>Strategies</h1>
        <p>One saved draft per idea. Nothing here runs until you confirm it.</p>
      </header>
      <StrategyList strategies={strategies} selectedId={userStrategyId} />
      {listNote && <p role="alert" className="notice error">{listNote}</p>}
      {current === null && <CreateStrategy api={api} scenarios={scenarios} created={created} />}
      {detailNote && <p role="alert" className="notice error">{detailNote}</p>}
      {current && <>
        <header className="strategy-title">
          <h1>{current.name}</h1>
          <p className="muted">{current.intent}</p>
          <p><Link to="/strategies">New strategy</Link></p>
        </header>
        {actionNote && <p role="alert" className="notice error">{actionNote}</p>}
        <DraftPanel draft={current.draft} />
        {proposal && <ProposalPanel
          key={`${current.user_strategy_id}-${proposal.proposal_id}-${proposal.base_revision}-${proposal.status}`}
          api={api} strategy={current} proposal={proposal}
          changed={changed} failed={setActionNote} />}
        <ExperimentPanel
          key={`${current.user_strategy_id}-${current.draft.revision}`}
          api={api} strategy={current} changed={changed} failed={setActionNote} />
      </>}
    </div>
    {current && <aside className="strategy-ai">
      <ChatPanel
        key={`${current.user_strategy_id}-${current.draft.revision}`}
        api={api} strategy={current} changed={changed} />
    </aside>}
  </div>
}
