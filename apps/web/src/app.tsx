import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { exactDelta } from './decimal'

export type InputIdentity = { scenario_sha256: string; data_sha256: string; record_count: number }
export type StrategyParameterContract = {
  name: 'target_quantity' | 'entry_delay_bars'; type: 'decimal' | 'integer'
  default: string | number | null; current_value: string | number | null
  minimum: string | number; maximum: string | number | null
}
export type BacktestParameters = {
  initial_cash: string
  strategy_parameters: { target_quantity: string | null; entry_delay_bars: number } | null
}
export type InputSnapshot = {
  schema: 'ea.local-web-input.v1'; scenario_id: string; source_identity: InputIdentity; identity: InputIdentity
  scenario: {
    funding: { currency: string; initial_cash: string }
    strategy: { id: string; target_quantity: string | null; entry_delay_bars?: number }
    instrument: { venue: string; symbol: string }
  }
}
export type ScenarioSummary = {
  scenario_id: string; name: string; valid: boolean; input_identity?: InputIdentity
  summary?: { strategy_id: string; venue: string; symbol: string; initial_cash: string; target_quantity: string | null; entry_delay_bars: number; record_count: number }
  strategy_parameters?: StrategyParameterContract[]
  normalized_input_identity?: InputIdentity
  error_code?: string; message?: string
}
export type BacktestJob = {
  schema: string; job_id: string; request_id: string; scenario_id: string; input_identity: InputIdentity
  status: 'accepted' | 'running' | 'succeeded' | 'failed' | 'interrupted'; engine_run_id: string | null
  report_sha256: string | null; summary_sha256: string | null; error_code: string | null; message: string | null; report_ready: boolean
  created_at?: string; input_snapshot?: InputSnapshot; input_sha256?: string; attempt_id?: string | null
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
  validateScenario(scenarioId: string, parameters: BacktestParameters): Promise<ScenarioSummary>
  createBacktest(request: { scenario_id: string; input_identity: InputIdentity; parameters: BacktestParameters; request_id: string }): Promise<BacktestJob>
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
  validateScenario(scenarioId, parameters) {
    return apiRequest(`/api/scenarios/${encodeURIComponent(scenarioId)}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({ parameters }),
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
        <Route path="/backtests/compare/:leftId/:rightId" element={<CompareBacktests api={api} />} />
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
  const [initialCash, setInitialCash] = useState('')
  const [quantity, setQuantity] = useState('')
  const [entryDelayBars, setEntryDelayBars] = useState(0)
  const [comparison, setComparison] = useState<string[]>([])
  const [validated, setValidated] = useState<ScenarioSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disconnected, setDisconnected] = useState(false)

  useEffect(() => {
    let current = true
    Promise.all([api.listScenarios(), api.listBacktests()]).then(([nextScenarios, nextJobs]) => {
      if (!current) return
      const first = nextScenarios.find((item) => item.valid)
      setScenarios(nextScenarios); setJobs(nextJobs); setSelected(first?.scenario_id ?? '')
      setInitialCash(first?.summary?.initial_cash ?? ''); setQuantity(first?.summary?.target_quantity ?? '')
      setEntryDelayBars(first?.summary?.entry_delay_bars ?? 0)
    }).catch(() => { if (current) setDisconnected(true) })
    return () => { current = false }
  }, [api])

  const candidate = useMemo(() => scenarios.find((item) => item.scenario_id === selected), [scenarios, selected])
  const targetContract = candidate?.strategy_parameters?.find((item) => item.name === 'target_quantity')
  const delayContract = candidate?.strategy_parameters?.find((item) => item.name === 'entry_delay_bars')
  const parameters = (): BacktestParameters => ({
    initial_cash: initialCash,
    strategy_parameters: targetContract && delayContract
      ? { target_quantity: quantity, entry_delay_bars: entryDelayBars }
      : null,
  })
  const changeScenario = (scenarioId: string) => {
    const next = scenarios.find((item) => item.scenario_id === scenarioId)
    setSelected(scenarioId); setInitialCash(next?.summary?.initial_cash ?? '')
    setQuantity(next?.summary?.target_quantity ?? '')
    setEntryDelayBars(next?.summary?.entry_delay_bars ?? 0); setValidated(null); setError(null)
  }
  const changeParameter = (setter: (value: string) => void, value: string) => {
    setter(value); setValidated(null); setError(null)
  }
  const validate = async () => {
    setBusy(true); setError(null)
    try { setValidated(await api.validateScenario(selected, parameters())) }
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
    setSelected(item.scenario_id); setInitialCash(snapshot.funding.initial_cash)
    setQuantity(snapshot.strategy.target_quantity ?? '')
    setEntryDelayBars(snapshot.strategy.entry_delay_bars ?? 0); setValidated(null); setError(null)
  }
  const toggleComparison = (jobId: string) => {
    setComparison((current) => current.includes(jobId)
      ? current.filter((value) => value !== jobId)
      : current.length < 2 ? [...current, jobId] : [current[1], jobId])
  }

  return <>
    <PageTitle title="Offline Backtests" subtitle="Run, replay, and compare real installed-engine attempts." />
    {disconnected && <section className="notice error">Local service is unreachable</section>}
    {error && <section className="notice error">{error}</section>}
    <div className="backtest-grid">
      <section className="panel control-panel">
        <header><h2>New backtest</h2><span>One active job</span></header>
        <label htmlFor="scenario">Scenario</label>
        <select id="scenario" value={selected} onChange={(event) => changeScenario(event.target.value)}>
          <option value="">Select prepared scenario</option>
          {scenarios.map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}{item.valid ? '' : ' · invalid'}</option>)}
        </select>
        {candidate?.summary && <dl className="summary-list">
          <div><dt>Strategy</dt><dd>{candidate.summary.strategy_id}</dd></div><div><dt>Instrument</dt><dd>{candidate.summary.venue}:{candidate.summary.symbol}</dd></div>
          <div><dt>Initial cash</dt><dd>{candidate.summary.initial_cash}</dd></div><div><dt>Records</dt><dd>{candidate.summary.record_count}</dd></div>
          <div><dt>Target</dt><dd>{candidate.summary.target_quantity ?? 'flat'}</dd></div>
        </dl>}
        <div className="parameter-grid">
          <div><label htmlFor="initial-cash">Initial cash</label><input id="initial-cash" value={initialCash} onChange={(event) => changeParameter(setInitialCash, event.target.value)} /></div>
          <div><label htmlFor="symbol">Symbol</label><input id="symbol" value={candidate?.summary?.symbol ?? ''} readOnly aria-describedby="symbol-source" /><small id="symbol-source">Registered scenario/data only</small></div>
        </div>
        {targetContract && delayContract && <section className="strategy-parameters">
          <h3>Strategy Parameters</h3>
          <div className="parameter-grid">
            <div><label htmlFor="quantity">Quantity</label><input id="quantity" type="number" min={String(targetContract.minimum)} step={String(targetContract.minimum)} value={quantity} onChange={(event) => changeParameter(setQuantity, event.target.value)} /></div>
            <div><label htmlFor="entry-delay-bars">Entry delay bars</label><input id="entry-delay-bars" type="number" min={String(delayContract.minimum)} max={delayContract.maximum === null ? undefined : String(delayContract.maximum)} step="1" value={Number.isNaN(entryDelayBars) ? '' : entryDelayBars} onChange={(event) => { setEntryDelayBars(event.target.valueAsNumber); setValidated(null); setError(null) }} /></div>
          </div>
        </section>}
        <div className="actions"><button disabled={!selected || busy} onClick={validate}>Validate input</button><button className="primary" disabled={!validated || busy} onClick={run}>Run new backtest</button></div>
        {validated?.summary && <div className="validated"><strong>Validated input</strong><span>Target quantity: {validated.summary.target_quantity ?? 'flat'} · Entry delay bars: {validated.summary.entry_delay_bars}</span><small>{validated.normalized_input_identity?.scenario_sha256.slice(0, 12) ?? validated.input_identity?.scenario_sha256.slice(0, 12)}…</small></div>}
      </section>
      <section className="panel jobs-panel"><header><h2>Recent Runs</h2><span>Refresh-safe history</span></header>
        {jobs.length === 0 ? <p className="muted">No backtests yet.</p> : <ul className="job-list">{jobs.map((item) => {
          const input = item.input_snapshot?.scenario
          return <li key={item.job_id} className="job-card">
            <div className="job-card-title"><Link to={`/backtests/${item.job_id}`}><strong>{item.scenario_id}</strong></Link><span className={`status status-${item.status}`}>{item.status}</span></div>
            {item.created_at && <time>{item.created_at}</time>}
            <small>{input ? `${input.instrument.symbol} · Cash ${input.funding.initial_cash} · Quantity ${input.strategy.target_quantity ?? 'flat'} · Entry delay ${input.strategy.entry_delay_bars ?? 0}` : 'Legacy run · input snapshot unavailable'}</small>
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

type ComparedRun = { job: BacktestJob; report: BacktestReport | null }

function percent(value: string): string {
  return `${exactDelta('0', value, 2).replace(/^\+/, '')}%`
}

function CompareBacktests({ api }: { api: ApiAdapter }) {
  const { leftId = '', rightId = '' } = useParams()
  const [runs, setRuns] = useState<[ComparedRun, ComparedRun] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const load = async (jobId: string): Promise<ComparedRun> => {
      const job = await api.getBacktest(jobId)
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
      return { job, report }
    }
    Promise.all([load(leftId), load(rightId)])
      .then(([left, right]) => { if (active) setRuns([left, right]) })
      .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : 'Comparison failed') })
    return () => { active = false }
  }, [api, leftId, rightId])

  if (!runs) return <><PageTitle title="Compare Backtests" subtitle="Loading two persisted runs and verified reports." />{error && <section className="notice error">{error}</section>}</>
  const [left, right] = runs
  const leftInput = left.job.input_snapshot?.scenario
  const rightInput = right.job.input_snapshot?.scenario
  if (!leftInput || !rightInput) return <><PageTitle title="Compare Backtests" subtitle="Pairwise comparison requires v2 input snapshots." /><section className="notice error">Input snapshot unavailable</section></>
  const inputRows = [
    ['Initial Cash', leftInput.funding.initial_cash, rightInput.funding.initial_cash],
    ['Symbol', leftInput.instrument.symbol, rightInput.instrument.symbol],
  ]
  const parameterRows = [
    ['target_quantity', leftInput.strategy.target_quantity ?? 'flat', rightInput.strategy.target_quantity ?? 'flat'],
    ['entry_delay_bars', String(leftInput.strategy.entry_delay_bars ?? 0), String(rightInput.strategy.entry_delay_bars ?? 0)],
  ]
  const metricRows = [
    {
      label: 'Final Equity', before: left.report?.economics.equity.amount, after: right.report?.economics.equity.amount, shift: 0, suffix: '',
      monetary: true,
      leftCurrency: left.report?.economics.equity.currency ?? left.report?.economics.currency,
      rightCurrency: right.report?.economics.equity.currency ?? right.report?.economics.currency,
    },
    {
      label: 'Net P&L', before: left.report?.economics.net_pnl.amount, after: right.report?.economics.net_pnl.amount, shift: 0, suffix: '',
      monetary: true,
      leftCurrency: left.report?.economics.net_pnl.currency ?? left.report?.economics.currency,
      rightCurrency: right.report?.economics.net_pnl.currency ?? right.report?.economics.currency,
    },
    { label: 'Return', before: left.report?.economics.total_return.value, after: right.report?.economics.total_return.value, shift: 2, suffix: ' pp', monetary: false },
    { label: 'Orders', before: left.report ? String(left.report.economics.counts.orders) : undefined, after: right.report ? String(right.report.economics.counts.orders) : undefined, shift: 0, suffix: '', monetary: false },
    { label: 'Fills', before: left.report ? String(left.report.economics.counts.fills) : undefined, after: right.report ? String(right.report.economics.counts.fills) : undefined, shift: 0, suffix: '', monetary: false },
  ]
  return <>
    <PageTitle title="Compare Backtests" subtitle="Derived read-only view over two persisted jobs and their formal reports." />
    <div className="result-toolbar"><Link to="/backtests">← Recent Runs</Link><span>No comparison artifact is stored</span></div>
    <section className="compare-identities">
      {[left, right].map((run, index) => <article className="panel" key={run.job.job_id}><header><h2>Run {index === 0 ? 'A' : 'B'}</h2><span className={`status status-${run.job.status}`}>{run.job.status}</span></header><dl className="summary-list"><div><dt>Web job</dt><dd>{run.job.job_id}</dd></div><div><dt>Engine run</dt><dd>{run.job.engine_run_id ?? 'not available'}</dd></div><div><dt>Input SHA-256</dt><dd>{run.job.input_sha256 ?? 'not available'}</dd></div><div><dt>Outcome</dt><dd>{run.report ? 'Formal report' : run.job.error_code ?? 'No report'}</dd></div></dl>{!run.report && <p className="no-report">No report</p>}</article>)}
    </section>
    <section className="panel comparison-panel"><header><h2>Input Diff</h2><span>Normalized snapshots</span></header><table><thead><tr><th>Parameter</th><th>Run A</th><th>Run B</th><th>Difference</th></tr></thead><tbody>{inputRows.map(([label, before, after]) => <tr className={before === after ? '' : 'changed'} key={label}><th>{label}</th><td>{before}</td><td>{after}</td><td>{before === after ? 'Unchanged' : 'Changed'}</td></tr>)}</tbody></table></section>
    <section className="panel comparison-panel"><header><h2>Parameter Delta</h2><span>Persisted normalized strategy inputs</span></header><table><thead><tr><th>Parameter</th><th>Run A</th><th>Run B</th><th>Difference</th></tr></thead><tbody>{parameterRows.map(([label, before, after]) => <tr className={before === after ? '' : 'changed'} key={label}><th>{label}</th><td>{before}</td><td>{after}</td><td>{before === after ? 'Unchanged' : 'Changed'}</td></tr>)}</tbody></table></section>
    <section className="panel comparison-panel"><header><h2>Result Diff</h2><span>Formal reports only</span></header><table><thead><tr><th>Metric</th><th>Run A</th><th>Run B</th><th>Δ</th></tr></thead><tbody>{metricRows.map(({ label, before, after, shift, suffix, monetary, leftCurrency, rightCurrency }) => {
      const leftValue = before === undefined ? 'No report' : label === 'Return' ? percent(before) : `${before}${leftCurrency ? ` ${leftCurrency}` : ''}`
      const rightValue = after === undefined ? 'No report' : label === 'Return' ? percent(after) : `${after}${rightCurrency ? ` ${rightCurrency}` : ''}`
      let delta = '—'
      if (before !== undefined && after !== undefined) {
        if (monetary && (!leftCurrency || !rightCurrency)) delta = 'Currency unavailable'
        else if (monetary && leftCurrency !== rightCurrency) delta = 'Different currencies'
        else delta = `${exactDelta(before, after, shift)}${suffix}${monetary ? ` ${leftCurrency}` : ''}`
      }
      return <tr key={label}><th>{label}</th><td>{leftValue}</td><td>{rightValue}</td><td>{delta}</td></tr>
    })}</tbody></table></section>
  </>
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
