import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, ChronologicalHoldout } from './app'
import {
  ResearchRequestError,
  type ModelConfigurationStatus,
  type ResearchSpec,
  type ResearchTask,
  type ResearchTaskStatus,
} from './research-api'

/**
 * Minimal research workspace: idea → registered restrictive spec → explicit
 * confirm → the real installed backtest → the existing chronological holdout
 * and evaluated candidate.
 *
 * Every value on these pages is read from the persisted task record; this
 * client never derives a spec, a result or an outcome, never starts a provider
 * call or a backtest on its own, and never falls back to a default provider.
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

function SpecFacts({ spec }: { spec: ResearchSpec }) {
  return <>
    <dl className="summary-list">
      <div><dt>Registered strategy</dt><dd>{spec.strategy_id}</dd></div>
      <div><dt>Instrument</dt><dd>{spec.instrument.venue}:{spec.instrument.symbol}</dd></div>
      <div><dt>Registered scenario</dt><dd>{spec.scenario_id ?? 'Resolved from the registered catalog at confirmation'}</dd></div>
      <div><dt>Research data</dt><dd>{spec.dataset_id ?? 'Registered scenario data'}</dd></div>
      <div><dt>Initial cash</dt><dd>{spec.initial_cash}</dd></div>
      <div><dt>Parameters</dt><dd>{Object.entries(spec.parameters).map(([name, value]) => `${name}: ${value}`).join(' · ') || 'Registered defaults'}</dd></div>
    </dl>
    <h3>Rationale</h3>
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

function ResearchJob({ api, jobId, job, holdouts }: { api: ApiAdapter; jobId: string; job: BacktestJob | null; holdouts: ChronologicalHoldout[] }) {
  if (!job) return <li className="job-card"><small>{jobId} · persisted job record unavailable</small></li>
  const holdoutForJob = holdouts.filter(item => item.source_job_id === job.job_id)
  return <li className="job-card">
    <div className="job-card-title"><Link to={`/backtests/${job.job_id}`}><strong>{job.job_id}</strong></Link><span className={`status status-${job.status}`}>{job.status}</span></div>
    <small>Engine run {job.engine_run_id ?? 'not available'} · {job.scenario_id}{job.error_code ? ` · ${job.error_code}` : ''}</small>
    {job.message && <small>{job.message}</small>}
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
    try { navigate(`/research/${(await api.createResearchTask(value)).task_id}`) }
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
    <section className="panel">
      <header><h2>New research idea</h2><span>One active task per idea</span></header>
      <label htmlFor="research-idea">Research idea</label>
      <textarea id="research-idea" maxLength={4000} rows={3} value={idea} disabled={busy}
        onChange={event => setIdea(event.target.value)} />
      <p className="muted">The idea is drafted into a spec that can only reference the registered strategy, instrument and scenario catalog. Confirmation is a separate, explicit step.</p>
      <div className="actions"><button className="primary" disabled={busy || !idea.trim() || unavailable} onClick={() => { void create() }}>{busy ? 'Creating draft…' : 'Create draft'}</button></div>
    </section>
    <section className="panel">
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
    </section>
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
    try { setTask(await call()) }
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
    <PageHeading title="Research Task" subtitle={task.raw_idea} status={task.status} />
    <div className="result-toolbar"><Link to="/research">← Research workspace</Link><button onClick={() => { void refresh() }}>Refresh</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    <section className="panel identity-panel">
      <header><h2>Task identity</h2><span>Persisted evidence only</span></header>
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

    {task.jobs.length > 0 && <section className="panel" aria-label="Research backtests">
      <header><h2>Backtests started by this task</h2><span>Next: existing chronological holdout, then evaluated candidate</span></header>
      <ul className="job-list">{task.jobs.map(jobId => <ResearchJob key={jobId} api={api} jobId={jobId} job={jobs[jobId] ?? null} holdouts={holdouts} />)}</ul>
      <p className="muted">Holdout evaluation and candidate creation stay on the existing evidence pages with their own explicit actions. Candidate acceptance records a research judgment only and grants no execution permission.</p>
      {succeededJobId && <p><Link to={`/backtests/${succeededJobId}`}>Open the succeeded run to evaluate its holdout and create the evaluated candidate</Link></p>}
    </section>}

    {(task.status !== 'cancelled' && task.status !== 'unsupported' && task.status !== 'succeeded') && <section className="panel" aria-label="Task actions">
      <header><h2>Task actions</h2><span>Explicit, never automatic</span></header>
      <p className="muted">Cancelling is final. It stops future steps and never claims a rollback or kills a backtest that already started; started runs keep their own terminal states. A refresh, back navigation or failed request never confirms, retries or submits anything.</p>
      <div className="actions">
        {retryable && api.retryResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('retry', () => api.retryResearchTask!(taskId), 'Retry was not accepted; the persisted task was reloaded.') }}>{busy === 'retry' ? 'Retrying…' : 'Retry failed step'}</button>}
        {api.cancelResearchTask && <button disabled={busy !== null} onClick={() => { void mutate('cancel', () => api.cancelResearchTask!(taskId), 'Cancel was not confirmed; the persisted task was reloaded.') }}>{busy === 'cancel' ? 'Cancelling…' : 'Cancel task'}</button>}
      </div>
    </section>}
  </>
}

function PageHeading({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{status && <span className={`status status-${status}`}>{STATUS_LABELS[status as ResearchTaskStatus] ?? status}</span>}</header>
}
