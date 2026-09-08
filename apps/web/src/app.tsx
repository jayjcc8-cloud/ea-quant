import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { compareCanonicalDecimal, exactDelta } from './decimal'

type ParameterMap = Record<string, string | number>
type StrategySource = { kind: string; package_id: string; artifact_sha256: string }
type StrategyDescriptor = { display_name?: string; strategy_id: string; strategy_version: number; research_visible: boolean; parameters: { name: string; type: 'integer' | 'decimal' }[] }
const parameterDefaults = (scenario?: ScenarioSummary): ParameterMap => Object.fromEntries((scenario?.strategy_parameters ?? []).map(p => [p.name, p.current_value ?? p.default ?? '']))
const jobParameters = (job: BacktestJob): ParameterMap => job.parameters ?? job.input_snapshot?.scenario.strategy.parameters ?? {}
const parameterText = (parameters: ParameterMap) => Object.entries(parameters).map(([name, value]) => `${name}: ${value}`).join(' · ')
function ParameterControls({ contracts, values, update, prefix = '' }: { contracts: StrategyParameterContract[]; values: ParameterMap; update: (name: string, value: string | number) => void; prefix?: string }) {
  return <div className="parameter-grid">{contracts.map(p => <div key={p.name}>
    <label htmlFor={`${prefix}${p.name}`}>{prefix}{p.name}</label>
    <input id={`${prefix}${p.name}`} type={p.type === 'integer' ? 'number' : 'text'} inputMode={p.type === 'integer' ? 'numeric' : 'decimal'}
      min={p.minimum === null ? undefined : String(p.minimum)} max={p.maximum === null ? undefined : String(p.maximum)} step={p.type === 'integer' ? '1' : undefined}
      value={Number.isNaN(values[p.name]) ? '' : values[p.name] ?? ''}
      onChange={event => update(p.name, p.type === 'integer' ? event.target.valueAsNumber : event.target.value)} />
    <small>{p.minimum === null ? '' : `Minimum ${p.minimum}`}{p.maximum === null ? '' : ` · Maximum ${p.maximum}`}</small>
  </div>)}</div>
}

type Dataset = { dataset_id: string; valid: boolean; source_sha256?: string; record_count?: number; replay_start_utc?: string; replay_end_utc?: string }
type DatasetReference = { dataset_id?: string; source_sha256?: string }
export type InputIdentity = DatasetReference & { scenario_sha256: string; data_sha256: string; record_count: number }
export type StrategyParameterContract = {
  name: string; type: 'decimal' | 'integer'
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
const commissionLabel = (commission?: Commission | null) => commission ? `${commission.policy} · ${commission.commission_bps} bps` : 'Legacy zero commission · 0 bps'

export type InputSnapshot = {
  schema: 'ea.local-web-input.v1' | 'ea.local-web-input.v2'; research_input?: Dataset; scenario_id: string; source_identity: InputIdentity; identity: InputIdentity
  scenario: {
    data?: ReplayData
    execution?: { policy: string; commission?: Commission | null }
    funding: { currency: string; initial_cash: string }
    strategy: { source?: StrategySource; id: string; version?: number; parameters?: ParameterMap; [key: string]: unknown }
    instrument: { venue: string; symbol: string }
  }
}
export type ScenarioSummary = {
  scenario_id: string; name: string; valid: boolean; input_identity?: InputIdentity
  summary?: { data?: ReplayData; commission?: Commission | null; strategy_id: string; venue: string; symbol: string; initial_cash: string; parameters?: ParameterMap; record_count: number; [key: string]: unknown }
  strategy_parameters?: StrategyParameterContract[]
  strategy_descriptor?: StrategyDescriptor
  normalized_input_identity?: InputIdentity
  error_code?: string; message?: string
}
export type BacktestJob = {
  schema: string; job_id: string; request_id: string; scenario_id: string; input_identity: InputIdentity
  status: 'accepted' | 'running' | 'succeeded' | 'failed' | 'interrupted'; engine_run_id: string | null
  report_sha256: string | null; summary_sha256: string | null; error_code: string | null; message: string | null; report_ready: boolean
  parameters?: ParameterMap; strategy_descriptor?: StrategyDescriptor
  created_at?: string; input_snapshot?: InputSnapshot; input_sha256?: string; attempt_id?: string | null
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
export type BacktestReport = {
  schema: 'ea.backtest-report.v1'; run_id: string
  source?: { strategy: { id: string }; instrument: { venue: string; symbol: string } }
  scenario?: { strategy_id: string; instrument: { venue: string; symbol: string } }
  economics: {
    currency?: string; initial_funding?: Money; ending_cash: Money[]
    ending_positions: { quantity: string; venue: string; symbol: string }[]
    valuation: { price: string; position_value: string }; equity: Money; net_pnl: Money
    fees?: { amount: string; currency: string; count: number; rule: string }
    total_return: { value: string }; counts: { orders: number; fills: number }
    execution: { order: { quantity: string; side: string } | null; fill: { quantity: string; price: string; side: string } | null }
  }
}
export type ApiAdapter = {
  holdoutScenarios(sourceJobId: string): Promise<ScenarioSummary[]>
  listDatasets?(): Promise<Dataset[]>
  createHoldout(request: { source_job_id: string; scenario_id: string; dataset_id?: string; source_sha256?: string }): Promise<ChronologicalHoldout>
  getHoldout(validationId: string): Promise<ChronologicalHoldout>
  listHoldouts(): Promise<ChronologicalHoldout[]>
  listScenarios(): Promise<ScenarioSummary[]>
  validateScenario(scenarioId: string, parameters: BacktestParameters, dataset?: DatasetReference): Promise<ScenarioSummary>
  createBacktest(request: { scenario_id: string; input_identity: InputIdentity; parameters: BacktestParameters; request_id: string }): Promise<BacktestJob>
  createBatch(request: BatchRequest): Promise<ExperimentBatch>
  getBatch(batchId: string): Promise<ExperimentBatch>
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
  getReport(jobId) { return apiRequest(`/api/backtests/${encodeURIComponent(jobId)}/report`) },
  artifactUrl(jobId, name) { return `/api/backtests/${encodeURIComponent(jobId)}/artifacts/${name}` },
}

function DatasetSelector({ api, value, change }: { api: ApiAdapter; value: DatasetReference; change: (value: DatasetReference) => void }) {
  const [items, setItems] = useState<Dataset[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api.listDatasets?.().then(items => { if (active) setItems(items) }).catch(() => { if (active) setError('Dataset catalog unavailable') })
    return () => { active = false }
  }, [api])
  if (!items.length && !value.dataset_id && !error) return null
  return <div><label htmlFor="research-dataset">Research dataset</label>
    <select id="research-dataset" value={value.dataset_id ?? ''} onChange={event => {
      const item = items.find(item => item.dataset_id === event.target.value)
      change(item ? { dataset_id: item.dataset_id, source_sha256: item.source_sha256 } : {})
    }}><option value="">Registered scenario data</option>
      {value.dataset_id && !items.some(item => item.dataset_id === value.dataset_id) && <option value={value.dataset_id}>{value.dataset_id} · unavailable</option>}
      {items.map(item => <option key={item.dataset_id} value={item.dataset_id} disabled={!item.valid}>{item.dataset_id}{item.valid ? '' : ' · invalid'}</option>)}
    </select>{error && <p className="notice error">{error}</p>}</div>
}

function PageTitle({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{status && <span className={`status status-${status}`}>{status}</span>}</header>
}

function Shell({ api }: { api: ApiAdapter }) {
  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/backtests"><span>EA</span><strong>QUANT</strong></Link>
      <p className="workspace">LOCAL OFFLINE CONSOLE</p>
      <nav aria-label="Primary navigation"><NavLink to="/backtests">Backtests</NavLink><NavLink to="/batches/new">Run Batch</NavLink><NavLink to="/holdouts">Chronological Holdout</NavLink></nav>
      <div className="sidebar-footer"><span className="status-dot" />Loopback only · live unavailable</div>
    </aside>
    <main><header className="topbar"><span>Installed Python engine</span><span>Offline simulation</span></header><div className="content">
      <Routes>
        <Route path="/backtests" element={<Backtests api={api} />} />
        <Route path="/backtests/compare/:leftId/:rightId" element={<CompareBacktests api={api} />} />
        <Route path="/backtests/:jobId" element={<BacktestDetailRoute api={api} />} />
        <Route path="/holdouts" element={<HoldoutHistory api={api} />} />
        <Route path="/holdouts/new/:jobId" element={<HoldoutCreatorRoute api={api} />} />
        <Route path="/holdouts/:validationId" element={<HoldoutDetailRoute api={api} />} />
        <Route path="/batches/new" element={<BatchCreator api={api} />} />
        <Route path="/batches/:batchId" element={<BatchDetailRoute api={api} />} />
        <Route path="*" element={<Navigate replace to="/backtests" />} />
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
      <DatasetSelector api={api} value={dataset} change={setDataset} />
      <div className="parameter-grid">
        <div><label htmlFor="batch-scenario">Scenario</label><select id="batch-scenario" value={selected} onChange={(event) => selectScenario(scenarios.find((item) => item.scenario_id === event.target.value))}>{scenarios.filter((item) => item.valid && (item.strategy_parameters?.length ?? 0) > 0).map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}</option>)}</select></div>
        <div><label htmlFor="batch-initial-cash">Initial cash</label><input id="batch-initial-cash" value={initialCash} onChange={(event) => setInitialCash(event.target.value)} /></div>
      </div>
      {candidate?.summary && <p className="muted">{candidate.summary.strategy_id} · {candidate.summary.venue}:{candidate.summary.symbol}</p>}
      {candidate?.summary && <p>{commissionLabel(candidate.summary.commission)}</p>}
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
  const [reports, setReports] = useState<Record<string, BacktestReport | null>>({})
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
  }, [batch, reports, sortDirection, sortField, statusFilter])
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
    <section className="panel comparison-panel"><header><h2>Member runs</h2><span>Real jobs and formal reports</span></header><div className="analysis-controls"><div><label htmlFor="batch-status-filter">Status filter</label><select id="batch-status-filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as BatchStatusFilter)}><option value="all">All</option><option value="succeeded">Succeeded</option><option value="risk.rejected">Risk Rejected</option><option value="failed">Failed</option><option value="running">Running</option><option value="queued">Queued</option></select></div><div><label htmlFor="batch-sort">Sort by</label><select id="batch-sort" value={sortField} onChange={(event) => setSortField(event.target.value as BatchSortField)}><option value="member">Member order</option>{parameterNames.map(name => <option key={name} value={`parameter:${name}`}>{name}</option>)}<option value="equity">Final equity</option><option value="net_pnl">Net P&amp;L</option><option value="total_return">Total return</option></select></div><div><label htmlFor="batch-sort-direction">Direction</label><select id="batch-sort-direction" value={sortDirection} onChange={(event) => setSortDirection(event.target.value as SortDirection)}><option value="ascending">Ascending</option><option value="descending">Descending</option></select></div></div><table><thead><tr><th>Run</th>{parameterNames.map(name => <th key={name}>{name}</th>)}<th>Status</th><th>Final equity</th><th>Net P&amp;L</th><th>Total return</th><th>Action</th><th>Compare</th></tr></thead><tbody>{rows.map(({ member, ordinal, report }) => {
      const equityCurrency = report?.economics.equity.currency ?? report?.economics.currency
      const pnlCurrency = report?.economics.net_pnl.currency ?? report?.economics.currency
      const money = (amount: string, currency: string | undefined) => `${amount} ${currency ?? 'Currency unavailable'}`
      return <tr key={member.job_id}><th>Run {ordinal}</th>{parameterNames.map(name => <td key={name}>{jobParameters(member)[name] ?? 'Unavailable'}</td>)}<td><span className={`status status-${member.presentation_status}`}>{member.presentation_status}</span></td>{report ? <><td>{money(report.economics.equity.amount, equityCurrency)}</td><td>{money(report.economics.net_pnl.amount, pnlCurrency)}</td><td>{percent(report.economics.total_return.value)}</td></> : <td colSpan={3}>No report</td>}<td><Link to={`/backtests/${member.job_id}`}>View</Link></td><td><input type="checkbox" aria-label={`Select ${member.job_id} for comparison`} checked={comparison.includes(member.job_id)} onChange={() => toggle(member.job_id)} /></td></tr>
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
    {error && <section className="notice error">{error}</section>}
    <div className="backtest-grid">
      <section className="panel control-panel">
        <header><h2>New backtest</h2><span>One active job</span></header>
        <label htmlFor="scenario">Scenario</label>
        <select id="scenario" value={selected} onChange={(event) => changeScenario(event.target.value)}>
          <option value="">Select prepared scenario</option>
          {scenarios.filter(item => item.strategy_descriptor?.research_visible !== false).map((item) => <option key={item.scenario_id} value={item.scenario_id}>{item.name}{item.valid ? '' : ' · invalid'}</option>)}
        </select>
        <DatasetSelector api={api} value={dataset} change={value => { setDataset(value); setValidated(null) }} />
        {candidate?.summary && <dl className="summary-list">
          <div><dt>Strategy</dt><dd>{candidate.summary.strategy_id}</dd></div><div><dt>Instrument</dt><dd>{candidate.summary.venue}:{candidate.summary.symbol}</dd></div>
          <div><dt>Commission</dt><dd>{commissionLabel(candidate.summary.commission)}</dd></div>
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
    ['Commission', commissionLabel(leftInput.execution?.commission), commissionLabel(rightInput.execution?.commission)],
  ]
  const sameImplementation = (leftInput.strategy.source?.kind ?? 'builtin') === (rightInput.strategy.source?.kind ?? 'builtin') && leftInput.strategy.source?.artifact_sha256 === rightInput.strategy.source?.artifact_sha256
  const sameStrategy = sameImplementation && leftInput.strategy.id === rightInput.strategy.id && (leftInput.strategy.version ?? 1) === (rightInput.strategy.version ?? 1)
  const parameterRows = sameStrategy ? (left.job.strategy_descriptor?.parameters.map(p => p.name) ?? Object.keys(jobParameters(left.job))).map(name => [name, String(jobParameters(left.job)[name]), String(jobParameters(right.job)[name])]) : []
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
    { label: 'Fees', before: left.report?.economics.fees?.amount, after: right.report?.economics.fees?.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.fees?.currency, rightCurrency: right.report?.economics.fees?.currency },
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
    <section className="panel comparison-panel">{!sameImplementation ? <p>Strategy implementation changed</p> : !sameStrategy && <p>NO_PARAMETER_DELTA · Different strategy ID/version</p>}<header><h2>Parameter Delta</h2><span>Persisted normalized strategy inputs</span></header><table><thead><tr><th>Parameter</th><th>Run A</th><th>Run B</th><th>Difference</th></tr></thead><tbody>{parameterRows.map(([label, before, after]) => <tr className={before === after ? '' : 'changed'} key={label}><th>{label}</th><td>{before}</td><td>{after}</td><td>{exactDelta(before, after)}</td></tr>)}</tbody></table></section>
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
      {job.input_snapshot?.research_input && <><div><dt>Dataset</dt><dd>{job.input_snapshot.research_input.dataset_id}</dd></div><div><dt>Source SHA-256</dt><dd>{job.input_snapshot.research_input.source_sha256}</dd></div></>}
    </dl></section>
    {report && job.strategy_descriptor?.research_visible && <p><Link to={`/holdouts/new/${job.job_id}`}>Evaluate chronological holdout</Link></p>}
    {job.input_snapshot && <p>{commissionLabel(job.input_snapshot.scenario.execution?.commission)}</p>}
    {economics ? <>
      <section className="metrics">
        <Metric label="Initial cash" value={economics.initial_funding ? `${economics.initial_funding.amount} ${currency}` : 'not available'} />
        <Metric label="Ending cash" value={`${economics.ending_cash[0]?.amount ?? '0'} ${currency}`} />
        <Metric label="Equity" value={`${economics.equity.amount} ${currency}`} />
        <Metric label="Net P&L" value={`${economics.net_pnl.amount} ${currency}`} />
        <Metric label="Total return" value={economics.total_return.value} />
        <Metric label="Fees" value={economics.fees ? `${economics.fees.amount} ${economics.fees.currency}` : 'not available'} />
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

function HoldoutEvidence({ role, job, report }: { role: string; job: BacktestJob; report: BacktestReport | null }) {
  const data = job.input_snapshot?.scenario.data
  const economics = report?.economics
  const currency = economics?.currency ?? job.input_snapshot?.scenario.funding.currency
  return <section className="panel"><header><h2>{role}</h2><span>{job.status}</span></header><dl className="summary-list">
    <div><dt>Scenario</dt><dd>{job.scenario_id}</dd></div>
    {job.input_snapshot?.research_input && <div><dt>Dataset</dt><dd>{job.input_snapshot.research_input.dataset_id}</dd></div>}
    <div><dt>Window (UTC)</dt><dd>{data ? `${data.start_utc} → ${data.end_utc}` : 'Unavailable'}</dd></div>
    <div><dt>Data fingerprint</dt><dd>{data?.fingerprint.sha256 ?? 'Unavailable'}</dd></div>
    <div><dt>Record count</dt><dd>{data?.fingerprint.record_count ?? 'Unavailable'}</dd></div>
    <div><dt>Commission assumption</dt><dd>{commissionLabel(job.input_snapshot?.scenario.execution?.commission)}</dd></div>
    <div><dt>Fees</dt><dd>{economics?.fees ? `${economics.fees.amount} ${economics.fees.currency}` : 'No report'}</dd></div>
    <div><dt>Final equity</dt><dd>{economics ? `${economics.equity.amount} ${currency}` : 'No report'}</dd></div>
    <div><dt>Net P&amp;L</dt><dd>{economics ? `${economics.net_pnl.amount} ${currency}` : 'No report'}</dd></div>
    <div><dt>Total return</dt><dd>{economics ? percent(economics.total_return.value) : 'No report'}</dd></div>
  </dl><Link to={`/backtests/${job.job_id}`}>Open {role} formal run</Link></section>
}

async function holdoutRun(api: ApiAdapter, id: string) {
  const job = await api.getBacktest(id)
  let report: BacktestReport | null = null
  if (job.status === 'succeeded') {
    try { const value = await api.getReport(id); if (value.run_id === job.engine_run_id) report = value } catch { /* Unavailable evidence stays unavailable. */ }
  }
  return { job, report }
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
    try { const relation = await api.createHoldout({ source_job_id: jobId, scenario_id: candidate!.scenario_id, ...(candidate?.input_identity?.dataset_id ? { dataset_id: candidate.input_identity.dataset_id, source_sha256: candidate.input_identity.source_sha256 } : {}) }); navigate(`/holdouts/${relation.validation_id}`) }
    catch (caught) { setError(String(caught)); setBusy(false) }
  }
  return <><PageTitle title="New Chronological Holdout" subtitle="Chronological holdout evaluation using parameters frozen from your selected source." />
    {error && <p className="notice error">{error}</p>}
    {source && <><FrozenParameters job={source.job} /><HoldoutEvidence role="IS" {...source} /></>}
    <section className="panel"><label htmlFor="holdout-scenario">Holdout scenario</label>
      <select id="holdout-scenario" value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select a compatible later registered scenario</option>{candidates.map(item => <option key={(item.input_identity?.dataset_id ? `${item.scenario_id}|${item.input_identity.dataset_id}` : item.scenario_id)} value={(item.input_identity?.dataset_id ? `${item.scenario_id}|${item.input_identity.dataset_id}` : item.scenario_id)}>{item.name}</option>)}</select>
      {source && candidates.length === 0 && <p>No compatible later registered scenario with valid frozen parameters is available.</p>}
      {candidate?.summary?.data && <dl className="summary-list"><div><dt>Window (UTC)</dt><dd>{candidate.summary.data.start_utc} → {candidate.summary.data.end_utc}</dd></div><div><dt>Data fingerprint</dt><dd>{candidate.summary.data.fingerprint.sha256}</dd></div><div><dt>Record count</dt><dd>{candidate.summary.data.fingerprint.record_count}</dd></div></dl>}
      <button className="primary" disabled={!selected || !source?.report || busy} onClick={() => { void submit() }}>Run chronological holdout</button>
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
        if (next.some(run => ['accepted', 'running'].includes(run.job.status))) timer = window.setTimeout(() => { void refresh() }, 500)
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
