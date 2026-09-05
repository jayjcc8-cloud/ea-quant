import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'

export type InputIdentity = { scenario_sha256: string; data_sha256: string; record_count: number }
export type ScenarioSummary = {
  scenario_id: string; name: string; valid: boolean; input_identity?: InputIdentity
  summary?: { strategy_id: string; venue: string; symbol: string; initial_cash: string; target_quantity: string | null; record_count: number }
  error_code?: string; message?: string
}
export type BacktestJob = {
  schema: string; job_id: string; request_id: string; scenario_id: string; input_identity: InputIdentity
  status: 'accepted' | 'running' | 'succeeded' | 'failed' | 'interrupted'; engine_run_id: string | null
  report_sha256: string | null; error_code: string | null; message: string | null; report_ready: boolean
}
type Money = { amount: string; currency?: string }
export type BacktestReport = {
  schema: 'ea.backtest-report.v1'; run_id: string
  source?: { strategy: { id: string; target_quantity: string | null }; instrument: { venue: string; symbol: string } }
  scenario?: { strategy_id: string; instrument: { venue: string; symbol: string } }
  economics: {
    currency?: string; initial_funding?: Money; ending_cash: Money[]
    ending_positions: { quantity: string; venue: string; symbol: string }[]
    valuation: { price: string; position_value: string }; equity: Money; net_pnl: Money
    total_return: { value: string }; counts: { orders: number; fills: number }
    execution: { order: { quantity: string; side: string } | null; fill: { quantity: string; price: string; side: string } | null }
  }
}
export type ApiAdapter = {
  listScenarios(): Promise<ScenarioSummary[]>
  validateScenario(scenarioId: string): Promise<ScenarioSummary>
  createBacktest(request: { scenario_id: string; input_identity: InputIdentity; request_id: string }): Promise<BacktestJob>
  listBacktests(): Promise<BacktestJob[]>
  getBacktest(jobId: string): Promise<BacktestJob>
  getReport(jobId: string): Promise<BacktestReport>
  artifactUrl(jobId: string, name: 'report.json' | 'summary.txt'): string
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
  async listScenarios() { return (await apiRequest<{ scenarios: ScenarioSummary[] }>('/api/scenarios')).scenarios },
  validateScenario(scenarioId) {
    return apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: '{}',
    })
  },
  createBacktest(request) {
    return apiRequest('/api/backtests', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify(request),
    })
  },
  async listBacktests() { return (await apiRequest<{ jobs: BacktestJob[] }>('/api/backtests')).jobs },
  getBacktest(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}`) },
  getReport(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/report`) },
  artifactUrl(jobId, name) { return `/api/backtests/${encodeURIComponent(jobId)}/artifacts/${name}` },
}

function PageTitle({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{status && <span className={`status status-${status}`}>{status}</span>}</header>
}

function Shell({ api }: { api: ApiAdapter }) {
  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/backtests"><span>EA</span><strong>QUANT</strong></Link>
      <p className="workspace">LOCAL OFFLINE CONSOLE</p>
      <nav aria-label="Primary navigation"><NavLink to="/backtests">Backtests</NavLink></nav>
      <div className="sidebar-footer"><span className="status-dot" />Loopback only · live unavailable</div>
    </aside>
    <main><header className="topbar"><span>Installed Python engine</span><span>Offline simulation</span></header><div className="content">
      <Routes>
        <Route path="/backtests" element={<Backtests api={api} />} />
        <Route path="/backtests/:jobId" element={<BacktestDetailRoute api={api} />} />
        <Route path="*" element={<Navigate replace to="/backtests" />} />
      </Routes>
    </div></main>
  </div>
}

function Backtests({ api }: { api: ApiAdapter }) {
  const navigate = useNavigate()
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [jobs, setJobs] = useState<BacktestJob[]>([])
  const [selected, setSelected] = useState('')
  const [validated, setValidated] = useState<ScenarioSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disconnected, setDisconnected] = useState(false)

  useEffect(() => {
    let current = true
    Promise.all([api.listScenarios(), api.listBacktests()]).then(([nextScenarios, nextJobs]) => {
      if (!current) return
      setScenarios(nextScenarios); setJobs(nextJobs); setSelected(nextScenarios.find((item) => item.valid)?.scenario_id ?? '')
    }).catch(() => { if (current) setDisconnected(true) })
    return () => { current = false }
  }, [api])

  const candidate = useMemo(() => scenarios.find((item) => item.scenario_id === selected), [scenarios, selected])
  const validate = async () => {
    setBusy(true); setError(null)
    try { setValidated(await api.validateScenario(selected)) }
    catch (caught) { setValidated(null); setError(caught instanceof Error ? caught.message : 'Validation failed') }
    finally { setBusy(false) }
  }
  const run = async () => {
    if (!validated?.input_identity) return
    setBusy(true); setError(null)
    try {
      const random = globalThis.crypto?.randomUUID?.() ?? `request-${Date.now()}-${Math.random().toString(16).slice(2)}`
      const accepted = await api.createBacktest({ scenario_id: validated.scenario_id, input_identity: validated.input_identity, request_id: random })
      navigate(`/backtests/${accepted.job_id}`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Run request failed'); setBusy(false) }
  }

  return <>
    <PageTitle title="Offline Backtests" subtitle="Validate prepared local scenarios and run one real installed-engine attempt." />
    {disconnected && <section className="notice error">Local service is unreachable</section>}
    {error && <section className="notice error">{error}</section>}
    <div className="backtest-grid">
      <section className="panel control-panel">
        <header><h2>New backtest</h2><span>One active job</span></header>
        <label htmlFor="scenario">Scenario</label>
        <select id="scenario" value={selected} onChange={(event) => { setSelected(event.target.value); setValidated(null); setError(null) }}>
          <option value="">Select prepared scenario</option>
          {scenarios.map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}{item.valid ? '' : ' · invalid'}</option>)}
        </select>
        {candidate?.summary && <dl className="summary-list">
          <div><dt>Strategy</dt><dd>{candidate.summary.strategy_id}</dd></div><div><dt>Instrument</dt><dd>{candidate.summary.venue}:{candidate.summary.symbol}</dd></div>
          <div><dt>Initial cash</dt><dd>{candidate.summary.initial_cash}</dd></div><div><dt>Records</dt><dd>{candidate.summary.record_count}</dd></div>
          <div><dt>Target</dt><dd>{candidate.summary.target_quantity ?? 'flat'}</dd></div>
        </dl>}
        <div className="actions"><button disabled={!selected || busy} onClick={validate}>Validate input</button><button className="primary" disabled={!validated || busy} onClick={run}>Run new backtest</button></div>
        {validated?.summary && <div className="validated"><strong>Validated input</strong><span>Target quantity: {validated.summary.target_quantity ?? 'flat'}</span><small>{validated.input_identity?.scenario_sha256.slice(0, 12)}…</small></div>}
      </section>
      <section className="panel jobs-panel"><header><h2>Workspace jobs</h2><span>Refresh-safe index</span></header>
        {jobs.length === 0 ? <p className="muted">No backtests yet.</p> : <ul className="job-list">{jobs.map((item) => <li key={item.job_id}><Link to={`/backtests/${item.job_id}`}><strong>{item.scenario_id}</strong><span>{item.status}</span><small>{item.engine_run_id ?? item.job_id}</small></Link></li>)}</ul>}
      </section>
    </div>
  </>
}

function BacktestDetailRoute({ api }: { api: ApiAdapter }) {
  const { jobId = '' } = useParams()
  return <BacktestDetail key={jobId} api={api} jobId={jobId} />
}

function BacktestDetail({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  const [job, setJob] = useState<BacktestJob | null>(null)
  const [report, setReport] = useState<BacktestReport | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const refreshGeneration = useRef(0)

  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current
    setReport(null)
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
      if (active && (status === 'accepted' || status === 'running')) timer = window.setTimeout(poll, 150)
    }
    void poll()
    return () => { active = false; refreshGeneration.current += 1; if (timer !== undefined) window.clearTimeout(timer) }
  }, [refresh])

  if (!job) return <><PageTitle title="Backtest Result" subtitle="Loading the workspace job and formal report." />{disconnected && <section className="notice error">Local service is unreachable</section>}{error && <section className="notice error">{error}</section>}</>
  const economics = report?.economics
  const currency = economics?.currency ?? economics?.ending_cash[0]?.currency ?? economics?.initial_funding?.currency ?? 'USD'
  const strategy = report?.source?.strategy.id ?? report?.scenario?.strategy_id
  const instrument = report?.source?.instrument ?? report?.scenario?.instrument
  return <>
    <PageTitle title="Backtest Result" subtitle={`${job.scenario_id} · job ${job.job_id}`} status={job.status} />
    {disconnected && <section className="notice error">Local service is unreachable · loaded job status may be historical</section>}
    {error && <section className="notice error">{error}</section>}
    {job.message && <section className="notice error"><strong>{job.error_code}</strong> · {job.message}</section>}
    <div className="result-toolbar"><Link to="/backtests">← New or saved backtest</Link><button onClick={() => { void refresh() }}>Refresh</button></div>
    <section className="panel identity-panel"><header><h2>Evidence identity</h2><span>Formal reporter only</span></header><dl className="summary-list">
      <div><dt>Engine run_id</dt><dd>{job.engine_run_id ?? 'not available'}</dd></div><div><dt>Strategy</dt><dd>{strategy ?? 'not available'}</dd></div><div><dt>Instrument</dt><dd>{instrument ? `${instrument.venue}:${instrument.symbol}` : 'not available'}</dd></div>
    </dl></section>
    {economics ? <>
      <section className="metrics">
        <Metric label="Initial cash" value={economics.initial_funding ? `${economics.initial_funding.amount} ${currency}` : 'not available'} />
        <Metric label="Ending cash" value={`${economics.ending_cash[0]?.amount ?? '0'} ${currency}`} />
        <Metric label="Equity" value={`${economics.equity.amount} ${currency}`} />
        <Metric label="Net P&L" value={`${economics.net_pnl.amount} ${currency}`} />
        <Metric label="Total return" value={economics.total_return.value} />
        <Metric label="Position quantity" value={economics.ending_positions[0]?.quantity ?? '0'} />
      </section>
      <div className="backtest-grid">
        <section className="panel"><header><h2>Execution</h2><span>Canonical strings</span></header><dl className="summary-list">
          <div><dt>Order</dt><dd>{economics.execution.order ? `${economics.execution.order.side} ${economics.execution.order.quantity}` : 'none'}</dd></div>
          <div><dt>Fill quantity</dt><dd>{economics.execution.fill?.quantity ?? 'none'}</dd></div><div><dt>Fill price</dt><dd>{economics.execution.fill?.price ?? 'none'}</dd></div>
          <div><dt>Valuation price</dt><dd>{economics.valuation.price}</dd></div><div><dt>Position value</dt><dd>{economics.valuation.position_value}</dd></div>
          <div><dt>Counts</dt><dd><span>Orders {economics.counts.orders}</span> · <span>Fills {economics.counts.fills}</span></dd></div>
        </dl></section>
        <section className="panel"><header><h2>Artifacts</h2><span>Read-only downloads</span></header><div className="downloads"><a href={api.artifactUrl(jobId, 'report.json')}>Download report.json</a><a href={api.artifactUrl(jobId, 'summary.txt')}>Download summary.txt</a></div><p className="muted">Net P&amp;L may include open-position valuation; it is not presented as realized profit.</p></section>
      </div>
    </> : <section className="panel"><p className="muted">{
      job.status === 'failed' || job.status === 'interrupted'
        ? 'No success report is available.'
        : job.status === 'succeeded'
          ? error ? 'No current verified report is available.' : 'Loading the formal report.'
          : 'The installed engine is running. This page will refresh automatically.'
    }</p></section>}
  </>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <article className="metric-card"><span>{label}</span><strong>{value}</strong></article>
}

export function App({ api = browserApi }: { readonly api?: ApiAdapter }) {
  return <BrowserRouter><Shell api={api} /></BrowserRouter>
}
