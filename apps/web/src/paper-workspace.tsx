import { ResearchSplitLayout } from './agent-panel-layout'
import { SourceLink as Link } from './workspace-navigation'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import type { Candidate } from './candidates'
import { PaperApiError, paperApi, type PaperApi, type PaperInstance, type PaperPreparation, type PaperRequest } from './paper-api'
import { observationTime, paperObservation, paperStateLabels } from './paper-observation'
import { PaperAi } from './paper-ai'
import { PaperRuntimeStatus } from './paper-runtime-status'
import type { PaperEvents, PaperOverview } from './radian-api'
import { PaperStatus } from './paper-status'
import './paper-workspace.css'

const PENDING = 'quine.local-paper.pending.v1'
const OFFLINE = '连接中断：保留上次观测，当前进程与停止状态未知。'
const label = (value: unknown) => value === undefined || value === null || value === '' ? '未提供' : String(value)
const blockers: Record<string, string> = {
  explicit_research_acceptance_required: '需先完成人工研究决定',
  unsupported_paper_profile_requires_action_v2_v4_v5: '当前策略不支持持续模拟，需使用已有 Action V2 / V4 或 V5 候选',
  recorded_implementation_required: '此证据属于另一代码版本，请使用相同版本或重新研究验证',
}
function pendingRequest(): PaperRequest | null {
  try { return JSON.parse(sessionStorage.getItem(PENDING) ?? 'null') as PaperRequest | null } catch { return null }
}

export function PaperWorkspace({ api = paperApi }: { api?: PaperApi }) {
  const [params, setParams] = useSearchParams()
  const [instances, setInstances] = useState<PaperInstance[]>([])
  const navigate = useNavigate()
  const selected = params.get('instance') ?? ''
  const selectInstance = useCallback((id: string) => navigate(`/runs?instance=${encodeURIComponent(id)}`, { replace: true }), [navigate])
  const [available, setAvailable] = useState<boolean | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const candidateId = params.get('candidate') ?? ''
  const lastCandidate = useRef(candidateId)
  const [prepared, setPrepared] = useState<{ id: string; value: PaperPreparation } | null>(null)
  const preparation = prepared?.id === candidateId ? prepared.value : null
  const [draft] = useState(() => { try { return JSON.parse(sessionStorage.getItem('quine.paper-setup') ?? '{}') as Record<string, string> } catch { return {} as Record<string, string> } })
  const [name, setName] = useState(draft.name ?? '本地模拟 01')
  const [prices, setPrices] = useState(draft.prices ?? '100')
  const [interval, setInterval] = useState(draft.interval ?? '1')
  const location = useLocation()
  const confirmationKey = JSON.stringify([location.key, candidateId, preparation?.fingerprint, name, prices, interval])
  const [confirmation, setConfirmation] = useState({ key: confirmationKey, checked: false })
  const confirmed = confirmation.key === confirmationKey && confirmation.checked
  if (confirmation.key !== confirmationKey) setConfirmation({ key: confirmationKey, checked: false })
  const setConfirmed = (checked: boolean) => setConfirmation({ key: confirmationKey, checked })
  const configure = params.get('view') === 'new' || !!candidateId
  useEffect(() => { try { sessionStorage.setItem('quine.paper-setup', JSON.stringify({ name, prices, interval })) } catch { /* optional local draft */ } }, [name, prices, interval])
  const tab = params.get('tab') === 'position' ? 'position' : params.get('tab') === 'events' ? 'events' : 'overview'
  const setTab = (value: string) => setParams(previous => { const next = new URLSearchParams(previous); next.set('tab', value); return next })
  const [eventResult, setEvents] = useState<PaperEvents | null>(null)
  const [pending, setPending] = useState<PaperRequest | null>(pendingRequest)
  const pendingRef = useRef(pending)
  const epoch = useRef(0)
  const mounted = useRef(true)
  const overviewHeading = useRef<HTMLHeadingElement | null>(null)
  const focusSection = (heading: HTMLElement | null) => {
    heading?.focus({ preventScroll: true })
    heading?.scrollIntoView?.({ block: 'start' })
  }
  const focusOverview = () => { setTab('overview'); focusSection(overviewHeading.current) }
  const row = instances.find(item => item.run_id === selected)
  const events = eventResult?.run_id === row?.run_id ? eventResult : null
  const observation = row ? paperObservation(row, disconnected) : null
  const overview = observation?.overview
  const observedHealth = observation?.health
  const fresh = observation?.current === true
  const active = instances.some(item => !item.terminal_confirmed)
  const updatePending = (value: PaperRequest | null) => {
    if (value) sessionStorage.setItem(PENDING, JSON.stringify(value)); else sessionStorage.removeItem(PENDING)
    pendingRef.current = value; setPending(value)
  }
  const refresh = useCallback(async () => {
    const token = ++epoch.current
    try {
      const response = await api.list()
      if (!mounted.current || epoch.current !== token) return
      setInstances(response.instances); setAvailable(response.available); setDisconnected(false)
      const retained = pendingRef.current
      if (retained && response.instances.some(item => item.run_id === retained.request_id)) {
        selectInstance(retained.request_id); updatePending(null)
      }
    } catch {
      if (mounted.current && epoch.current === token) setDisconnected(true)
    }
  }, [api, selectInstance])
  useEffect(() => {
    mounted.current = true
    const initial = window.setTimeout(() => { void refresh() }, 0)
    api.candidates().then(value => { if (mounted.current) setCandidates(value.candidates) }).catch(() => { if (mounted.current) setError('候选证据暂不可读；请返回研究检查。') })
    const timer = window.setInterval(() => { if (!busyRef.current) void refresh() }, 2000)
    return () => { mounted.current = false; window.clearTimeout(initial); window.clearInterval(timer) }
  }, [api, refresh])
  useEffect(() => {
    let current = true
    if (candidateId) api.prepare(candidateId).then(value => { if (current) setPrepared({ id: candidateId, value }) }).catch(() => { if (current) setError('候选检查失败：证据缺失、发生变化或研究服务不可用。') })
    return () => { current = false }
  }, [api, candidateId])
  useEffect(() => {
    let current = true
    if (row && tab === 'events') api.events(row.run_id).then(value => { if (current && value.run_id === row.run_id) setEvents(value) }).catch(() => { if (current) setEvents({ available: false, run_id: row.run_id, reason: '事件读取失败' }) })
    return () => { current = false }
  }, [api, row?.run_id, row?.overview.updated_at, tab]) // eslint-disable-line react-hooks/exhaustive-deps

  const queryPending = async () => {
    const intent = pendingRef.current
    if (!intent) return
    try {
      const found = await api.get(intent.request_id)
      if (!mounted.current) return
      selectInstance(found.run_id); updatePending(null); setError(''); await refresh()
    } catch (caught) {
      if (caught instanceof PaperApiError && caught.status === 404) {
        updatePending(null); setError('后端确认没有这次请求的记录。请重新检查配置后确认启动。'); setConfirmed(false)
      } else { setError('仍无法确认原请求；请继续查询，勿重复启动。') }
    }
  }
  const start = async () => {
    if (busyRef.current || !preparation?.eligible || !preparation.start_available || !confirmed || pendingRef.current || active || disconnected) return
    const values = prices.split(/[,，\s]+/).filter(Boolean).map(Number)
    if (!values.length || values.length > 64 || values.some(value => !Number.isFinite(value) || value <= 0) || !Number.isFinite(Number(interval)) || Number(interval) < .01 || Number(interval) > 10 || !name.trim()) { setError('请输入实例名、1–64 个正数价格，及 0.01–10 秒间隔。'); return }
    const intent: PaperRequest = { request_id: crypto.randomUUID(), candidate_id: candidateId, fingerprint: preparation.fingerprint, name: name.trim(), prices: values, interval_seconds: Number(interval) }
    try { updatePending(intent) } catch { setError('无法保存启动身份；本次没有发送启动请求。'); return }
    busyRef.current = true; setBusy(true); setError(''); epoch.current++
    try {
      const found = await api.start(intent)
      if (!mounted.current) return
      selectInstance(found.run_id); updatePending(null); setConfirmed(false); await refresh()
    } catch (caught) { if (mounted.current) setError(`启动结果未确认：${caught instanceof Error ? caught.message : '连接不可用'}。只查询原请求，不会自动重发。`) }
    finally { busyRef.current = false; if (mounted.current) setBusy(false) }
  }
  const stop = async () => {
    if (!row || busyRef.current) return
    busyRef.current = true; setBusy(true); setError(''); epoch.current++
    setInstances(items => items.map(item => item.run_id === row.run_id ? { ...item, state: 'stop_requested', stop_requested: true } : item))
    try {
      const result = await api.stop(row.run_id)
      if (!mounted.current) return
      setInstances(items => items.map(item => item.run_id === result.run_id ? result : item))
      if (!result.terminal_confirmed) setError('停止请求已记录，真实终态与租约释放尚未确认。仍禁止新启动。')
    } catch { if (mounted.current) { setDisconnected(true); setError('停止响应未知；请查询原实例。未确认停止前不能新启动。') } }
    finally { busyRef.current = false; if (mounted.current) setBusy(false) }
  }
  if (available === false) return <><p className="notice">此入口提供只读观测。请在 QUINE 桌面应用中显式启动本地模拟。</p><PaperStatus /></>
  return <ResearchSplitLayout storageKey={`paper.${selected || 'list'}`}>
    <div className="strategy-column paper-workspace"><div className="paper-main">
      <header className="paper-heading"><div><p className="paper-eyebrow">运行工作区</p><h1 ref={overviewHeading} tabIndex={-1}>{row?.request.name ?? (configure ? '配置本地模拟' : '模拟运行')}</h1><div className="paper-meta"><span className="paper-mode">本地模拟 · 无真实资金</span>{row?.strategy && <span>策略版本 v{row.strategy.version}</span>}{row && <Link to={`/backtests/${row.source_job_id}`}>查看来源研究 ↗</Link>}<span>仅应用打开期间运行</span>{row && <button className="paper-ai-jump" onClick={() => focusSection(document.getElementById('paper-ai-heading'))}>查看 AI 解释</button>}</div></div><div className="paper-actions"><button onClick={() => { setError(''); void refresh() }}>刷新状态</button><button disabled={active || !!pending || busy || disconnected} onClick={() => { setConfirmed(false); setError(''); setParams({ view: 'new', ...(lastCandidate.current ? { candidate: lastCandidate.current } : {}) }) }}>配置新实例</button>{row && !row.terminal_confirmed && <button className="paper-stop" disabled={busy || (disconnected && !row.stop_requested)} onClick={() => { if (row.stop_requested) void refresh(); else void stop() }}>{row.stop_requested ? '查询原停止请求' : '停止运行'}</button>}</div></header>
      {disconnected && <p role="alert" className="notice error">{OFFLINE}</p>}
      {error && <p role="alert" className="notice error">{error}</p>}
      {pending && <div className="paper-unknown" role="status"><strong>启动结果尚未确认</strong><p>保留同一请求身份；不会自动重发，也不会新建第二个实例。</p><button onClick={() => void queryPending()}>查询原启动请求</button></div>}
      {row && instances.length > 1 && <label className="paper-history">运行实例<select aria-label="运行实例" value={selected} onChange={event => setParams({ instance: event.target.value })}>{instances.map(item => <option key={item.run_id} value={item.run_id}>{item.request.name}</option>)}</select></label>}
      {(selected || configure) && <p><Link to="/runs" onClick={() => setConfirmed(false)}>{configure ? '取消配置并返回列表' : '返回模拟运行列表'}</Link>{configure && <span className="muted"> · 保留输入，启动确认不会保留</span>}</p>}
      {!selected && !configure && <section aria-label="运行实例列表" className="paper-instance-list">
        {available === null && !disconnected && <p role="status">正在读取运行实例…</p>}
        {available === true && !instances.length && <><h2>还没有本地模拟实例</h2><p>从已完成人工决定的研究候选配置实例，再单独确认启动。</p><Link to="/candidates">查看研究候选</Link></>}
        {!!instances.length && <div className="table-scroll"><table><thead><tr><th>实例</th><th>最近观测状态</th><th>创建时间</th></tr></thead><tbody>{instances.map(item => <tr key={item.run_id}><th><Link to={`/runs?instance=${encodeURIComponent(item.run_id)}`}>{item.request.name}</Link></th><td>{disconnected ? '当前状态未知' : paperStateLabels[item.state] ?? '状态未知'}</td><td>{observationTime(item.created_at)}</td></tr>)}</tbody></table></div>}
      </section>}
      {selected && !row && available === true && <p role="alert">找不到这个运行实例，请返回列表重新选择。</p>}
      {configure && !pending && <section className="paper-setup" aria-label="本地模拟配置"><header><h2>从研究候选开始</h2><Link to="/candidates">查看候选与人工决定 ↗</Link></header><p>先完成成功来源研究和后续时间窗验证，再人工接受候选。启动始终需要单独确认。</p><label>选择研究候选<select aria-label="选择研究候选" disabled={busy || active} value={candidateId} onChange={event => { setParams(previous => { const next = new URLSearchParams(previous); lastCandidate.current = event.target.value; next.set('candidate', event.target.value); next.set('view', 'new'); return next }, { replace: true }); setConfirmed(false); setError('') }}><option value="">请选择已保存的候选</option>{candidates.map(item => <option key={item.candidate_id} value={item.candidate_id}>{item.projection?.strategy.id ?? '证据不可用'} · {item.status} · {item.candidate_id.slice(0, 8)}</option>)}</select></label>{!candidates.length && <p>还没有候选。<Link to="/strategies">返回研究</Link>，完成来源实验与后续时间窗验证后创建候选。</p>}
        {preparation && <><div className="paper-fixed"><h3>固定研究配置</h3><p>初始资金与币种属于已接受候选的冻结输入，不能在模拟创建时覆盖。修改需重新形成证据并接受。</p><p><Link to={`/candidates/${candidateId}`}>查看候选与后续时间窗证据</Link></p><dl><div><dt>策略版本</dt><dd>{preparation.configuration.strategy.id} v{preparation.configuration.strategy.version ?? 1}</dd></div><div><dt>模拟标的</dt><dd>{preparation.configuration.instrument.venue}:{preparation.configuration.instrument.symbol}</dd></div><div><dt>初始模拟资金</dt><dd>{preparation.configuration.funding.initial_cash} {preparation.configuration.funding.currency}</dd></div><div><dt>来源报告</dt><dd><Link to={`/backtests/${preparation.source_job_id}`}>打开正式研究报告 ↗</Link></dd></div></dl><details><summary>策略参数、成本与风险配置</summary><pre>{JSON.stringify({ parameters: preparation.configuration.strategy.parameters, execution: preparation.configuration.execution, risk: preparation.configuration.risk }, null, 2)}</pre></details></div>{!preparation.eligible && <p role="alert">{preparation.blockers.map(item => blockers[item] ?? item).join('；')}</p>}{preparation.small_sample_warning && <p className="paper-caution">证据为短样本，仅可验证流程，不代表策略稳健性。</p>}<div className="paper-fields"><label>实例名<input value={name} maxLength={80} onChange={event => setName(event.target.value)} /></label><label>生成价格序列<input value={prices} onChange={event => setPrices(event.target.value)} placeholder="例如 100, 101, 99" /><small>本机循环生成，非外部实时行情</small></label><label>行情间隔（秒）<input type="number" min="0.01" max="10" step="0.01" value={interval} onChange={event => setInterval(event.target.value)} /></label></div><label className="paper-confirm"><input type="checkbox" checked={confirmed} disabled={!preparation.eligible || active} onChange={event => setConfirmed(event.target.checked)} />我确认以以上固定版本与生成行情启动本地模拟；停止不代表平仓。</label><button className="paper-primary" disabled={busy || active || !confirmed || !preparation.eligible || !preparation.start_available || disconnected} onClick={() => void start()}>确认启动本地模拟</button></>}
      </section>}
      {row && <>
        <dl className="fact-strip"><div><dt>归属</dt><dd>未指定交易所 · 市场类型未记录</dd></div><div><dt>价格来源</dt><dd>本地生成行情 · 每 {row.request.interval_seconds} 秒</dd></div><div><dt>最近观测</dt><dd>{observationTime(row.observed_at)}</dd></div><div><dt>候选</dt><dd><Link to={`/candidates/${row.request.candidate_id}`} title={row.request.candidate_id}>{row.request.candidate_id.slice(0, 8)}</Link></dd></div></dl>
        {observation && <PaperRuntimeStatus observation={observation} compact />}
        <nav className="paper-tabs" aria-label="运行视图">{([['overview', '概览'], ['position', '持仓与成交'], ['events', '事件']] as const).map(([key, title]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{title}</button>)}</nav>
        {tab === 'overview' && (overview ? <><section className="paper-account" data-scope={observation?.kind}><header><h2>账户权益（模拟）</h2><span>{fresh ? '读取时经济快照' : '历史经济快照'}</span></header>{!fresh && <p className="paper-snapshot-scope">{observation?.kind === 'stale' ? '观测陈旧' : observation?.kind === 'history' ? '历史记录' : '当前观测未确认'} · 以下为已保存快照，不代表当前权益。</p>}<div className="paper-times"><span>状态发布（经济快照）：{observationTime(overview?.updated_at)}</span><span>估值时点：{observationTime(overview?.valuation_time)}</span></div><div className="paper-equity">{label(overview?.equity)} <small>{label(overview?.currency)}</small></div><p>权益来自已确认的现金与持仓估值。当前接口未提供历史权益序列。</p><div className="paper-metrics"><div><span>可用现金</span><strong>{label(overview?.cash)}</strong></div><div><span>持仓数量</span><strong>{label(overview?.position)}</strong></div><div><span>已确认成交</span><strong>{label(overview?.fills)}</strong></div><div><span>本地对账</span><strong>{label(observedHealth?.reconciliation_state ?? overview?.reconciliation)}</strong></div></div></section><section className="paper-facts"><h2>当前持仓</h2><Position overview={overview} /><p>均价、已实现 / 浮动盈亏与费用拆分未提供。</p></section></> : <section className="paper-account"><header><h2>账户权益（模拟）</h2><span>经济快照不可用</span></header><p>尚无可读取的经济快照，无法显示权益、现金、持仓或成交。可刷新状态或查看来源研究。</p></section>)}
        {tab === 'position' && <section className="paper-facts"><h2>持仓与成交事实</h2>{overview ? <><Position overview={overview} /><dl><div><dt>已确认成交数</dt><dd>{label(overview?.fills)}</dd></div><div><dt>观察到的成交数</dt><dd>{label(overview?.observed_fills)}</dd></div><div><dt>订单数</dt><dd>{label(overview?.orders)}</dd></div><div><dt>账本序号</dt><dd>{label(overview?.ledger_sequence)}</dd></div></dl><p>当前投影未提供逐笔成交价格、均价与盈亏拆分。可查看已有事件；事件日志不替代账本。</p></> : <p>尚无可读取的经济快照，无法显示持仓与成交。</p>}</section>}
        {tab === 'events' && <section className="paper-facts"><h2>运行事件</h2>{events?.available ? <ul className="paper-event-list">{events.events?.slice().reverse().map((event, index) => <li key={index}><time>{observationTime(typeof event.timestamp === 'string' ? event.timestamp : null)}</time><strong>{label(event.event)}</strong><span>{event.outcome ? String(event.outcome) : ''}</span></li>)}</ul> : <p>{events ? '事件不可用，未当作空记录。' : '正在读取事件…'}</p>}</section>}
        <footer className="paper-footnote"><span>{row.terminal_confirmed ? '终态与租约释放已确认；保留原实例历史。' : '停止请求不等于停止确认；未确认时禁止启动另一实例。'}</span><span>停止仅截断新决策并排空已发出事实，不平仓。</span>{row.detail && <details><summary>运行诊断</summary><p>{row.detail}</p></details>}</footer>
      </>}
    </div></div>
    <aside className="strategy-ai paper-workspace">{row
      ? <PaperAi key={row.run_id} runId={row.run_id} instanceName={row.request.name} sourceJobId={row.source_job_id} onOverview={focusOverview} api={api} />
      : <aside className="paper-ai" aria-label="AI 运行助手"><header><span className="paper-ai-spark">✦</span><h2>AI 运行助手</h2></header><p className="paper-ai-context">当前上下文：尚未启动本地模拟</p><div className="paper-ai-note"><h3>选择已存在的实例后可用</h3><p>解释只针对一个已经存在的本地模拟实例，并基于后端冻结的运行证据快照。</p><Link to="/strategies">继续研究 ↗</Link></div></aside>}</aside>
  </ResearchSplitLayout>
}
function Position({ overview }: { overview?: PaperOverview }) {
  return <div className="paper-table-wrap"><table><thead><tr><th>标的</th><th>数量</th><th>估值价格</th><th>币种</th></tr></thead><tbody><tr><td>{label(overview?.symbol)}</td><td>{label(overview?.position)}</td><td>{label(overview?.valuation_price)}</td><td>{label(overview?.currency)}</td></tr></tbody></table></div>
}
