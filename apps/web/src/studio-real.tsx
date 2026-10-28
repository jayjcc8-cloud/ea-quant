/** v3's first real slice. No scenario adapter, fixture state or synthetic outcome. */
import { useCallback, useEffect, useRef, useState, type ReactNode, type SetStateAction } from 'react'
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { browserApi, type ApiAdapter } from './app'
import { BindScenario, SelectDataset } from './user-strategies'
import { DraftEditor } from './draft-editor'
import { DataWorkspace } from './research'
import { FrozenReportDetails, FrozenReportFacts, FrozenReportMetrics, ReportTrades } from './frozen-report'
import { EquityCurve } from './equity-path'
import { useRunRead } from './user-strategy-research'
import { isIntentDraft, type UserStrategy, type UserStrategyPreview } from './user-strategies-api'
import { StorageFailureGuard } from './storage-failure-guard'

const reason = (error: unknown) => error instanceof Error ? error.message : '读取失败'
const unresolved = new Set(['pending', 'confirmed', 'running', 'stopping', 'unknown', 'unconfirmed'])
const pendingKey = (id: string) => `quine.studio.confirm.${id}`
function saved(key: string): string { try { return sessionStorage.getItem(key) ?? '' } catch { return '' } }

function useRead<T>(read: () => Promise<T>) {
  const [version, setVersion] = useState(0)
  const [loaded, setLoaded] = useState<{ read: typeof read; version: number; value: T | null; error: string } | null>(null)
  useEffect(() => {
    let active = true
    void read().then(value => { if (active) setLoaded({ read, version, value, error: '' }) }).catch(e => { if (active) setLoaded({ read, version, value: null, error: reason(e) }) })
    return () => { active = false }
  }, [read, version])
  const setValue = useCallback((next: SetStateAction<T | null>) => setLoaded(current => ({ read, version, error: '', value: typeof next === 'function' ? (next as (current: T | null) => T | null)(current?.read === read && current.version === version ? current.value : null) : next })), [read, version])
  const current = loaded?.read === read && loaded.version === version ? loaded : null
  return { value: current?.value ?? null, setValue, error: current?.error ?? '', reload: () => setVersion(n => n + 1) }
}
function ErrorNote({ children }: { children: ReactNode }) { return children ? <p className="notice error" role="alert">{children}</p> : null }
function Unavailable() { return <div className="page"><h1>此候选尚未接入</h1><p>模拟、实盘、批量、留出与候选晋级暂不可用。</p><Link to="/">返回研究</Link></div> }

function Home({ api }: { api: ApiAdapter }) {
  const list = useRead(useCallback(() => api.listResearch!(), [api]))
  const [goal, setGoal] = useState(() => saved('quine.studio.idea')), [error, setError] = useState('')
  const [busy, setBusy] = useState(false), [uncertain, setUncertain] = useState(false), [retained, setRetained] = useState(true)
  const sending = useRef(false), navigate = useNavigate()
  const update = (text: string) => { setGoal(text); try { sessionStorage.setItem('quine.studio.idea', text); setRetained(true) } catch { setRetained(false) } }
  const create = async () => {
    if (sending.current || uncertain || !goal.trim()) return
    sending.current = true; setBusy(true); setError('')
    try {
      const next = await api.createUserStrategy!({ name: goal.trim().slice(0, 64), intent: goal.trim() })
      update(''); navigate(`/research/${encodeURIComponent(next.user_strategy_id)}/overview`)
    } catch (e) { setError(`保存结果未确认：${reason(e)}。请刷新研究列表核对，不要重复保存。`); setUncertain(true); list.reload() }
    finally { sending.current = false; setBusy(false) }
  }
  return <div className="home"><h1>今天，验证什么想法？</h1><p className="subtitle">描述目标，或继续一项研究。保存后，在独立确认页启动实验。</p>
    <div className="composer"><label className="sr-only" htmlFor="real-goal">研究目标</label><textarea id="real-goal" value={goal} onChange={e => update(e.target.value)} placeholder="例如：检验策略在不同交易成本下的表现…" />
      <div className="composer-bottom"><span>保存不调用模型、不运行实验</span><button className="button primary" disabled={busy || uncertain || !goal.trim()} onClick={() => void create()}>保存研究并继续</button></div><ErrorNote>{error}</ErrorNote>
    </div>
    <StorageFailureGuard active={!retained && !!goal} message="输入未能缓存在本机。离开前请保存研究。" />
    <div className="quick-actions"><Link to="/resources/data">导入与检查数据</Link><Link to="/records">阅读冻结报告</Link><Link to="/resources/models">模型状态</Link></div>
    <div className="section-heading"><h2>继续研究</h2><button className="button" onClick={list.reload}>刷新研究列表</button></div>
    <ErrorNote>{list.error}</ErrorNote>{!list.value && !list.error && <p role="status">正在读取工作区…</p>}
    {list.value?.items.map(item => <Link className="real-research-row" key={item.user_strategy_id} to={`/research/${encodeURIComponent(item.user_strategy_id)}/overview`}>
      <span><strong>{item.name}</strong><small>{item.intent}</small></span><span>草稿 r{item.draft.revision} · {item.latest_experiment?.launch_status ?? '尚无实验'}</span>
    </Link>)}{list.value?.items.length === 0 && <p className="muted">工作区尚无研究。保存一个问题开始。</p>}
  </div>
}

function ResearchRoute({ api }: { api: ApiAdapter }) { const { id = '' } = useParams(); return <ResearchDetail key={id} api={api} id={id} /> }
function ResearchDetail({ api, id }: { api: ApiAdapter; id: string }) {
  const { tab = 'overview' } = useParams(), navigate = useNavigate()
  const read = useCallback(async () => { const next = await api.getUserStrategy!(id); if (next.user_strategy_id !== id) throw new Error('研究身份不一致'); return next }, [api, id])
  const loaded = useRead(read), catalog = useRead(useCallback(() => api.listScenarios(true), [api]))
  const [error, setError] = useState('')
  const setStrategy = loaded.setValue
  const changed = useCallback((next: UserStrategy) => { if (next.user_strategy_id === id) setStrategy(next) }, [id, setStrategy])
  const strategy = loaded.value
  useEffect(() => {
    if (!strategy?.experiments.some(item => ['pending', 'confirmed', 'running', 'stopping'].includes(item.status))) return
    let active = true
    const timer = setTimeout(() => { void read().then(next => { if (active) changed(next) }).catch(e => { if (active) setError(`进度读取失败：${reason(e)}；最后读到的状态不代表完成。`) }) }, 1200)
    return () => { active = false; clearTimeout(timer) }
  }, [strategy, read, changed])
  if (!strategy) return <div className="page"><ErrorNote>{loaded.error}</ErrorNote><p role="status">{loaded.error ? '研究不可读' : '正在读取研究…'}</p><button className="button" onClick={loaded.reload}>重新读取研究</button></div>
  const draft = strategy.draft
  return <div className="page real-detail"><header className="page-header"><div><Link to="/">研究 /</Link><h1>{strategy.name}</h1><p className="subtitle">草稿 r{draft.revision} · {isIntentDraft(draft) ? '等待绑定策略与数据' : `${draft.venue}:${draft.symbol}`}</p></div>
    <Link className="button primary" to={`/research/${encodeURIComponent(id)}/confirm`}>准备一次实验</Link></header>
    <nav className="tabs" aria-label="研究页面">{[['overview', '概览'], ['draft', '草稿与数据'], ['experiments', '实验记录']].map(([key, label]) => <Link aria-current={tab === key ? 'page' : undefined} key={key} to={`/research/${encodeURIComponent(id)}/${key}`}>{label}</Link>)}</nav>
    <ErrorNote>{error}</ErrorNote>
    {tab === 'confirm' ? <Confirmation key={`${id}.${draft.revision}`} api={api} strategy={strategy} changed={changed} done={() => navigate(`/research/${encodeURIComponent(id)}/experiments`)} />
      : <div className="real-detail-grid"><section>
        {tab === 'overview' && <><section className="panel"><h2>研究目标</h2><p>{strategy.intent}</p><p className="muted">修改草稿与运行实验分开确认，历史报告保持原始输入。</p></section><Experiments strategy={strategy} refresh={loaded.reload} /></>}
        {tab === 'draft' && (isIntentDraft(draft) ? <BindScenario api={api} strategy={strategy} scenarios={catalog.value ?? []} catalogReady={!!catalog.value} catalogError={catalog.error || null} reloadCatalog={catalog.reload} changed={changed} failed={setError} />
          : <><section className="panel"><h2>当前草稿</h2><DraftEditor api={api} strategy={strategy} changed={changed} /></section><SelectDataset key={draft.revision} api={api} strategy={strategy} changed={changed} failed={setError} /></>)}
        {tab === 'overview' && isIntentDraft(draft) && <Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>绑定策略与数据</Link>}
        {tab === 'experiments' && <Experiments strategy={strategy} refresh={loaded.reload} />}
        {!['overview', 'draft', 'experiments'].includes(tab) && <Unavailable />}
      </section><Assistant key={`${id}.${draft.revision}`} api={api} strategy={strategy} changed={changed} /></div>}
  </div>
}
function Experiments({ strategy, refresh }: { strategy: UserStrategy; refresh: () => void }) {
  return <section className="panel"><div className="section-heading"><h2>实验记录</h2><button className="button" onClick={refresh}>重新读取研究</button></div>
    {!strategy.experiments.length && <p className="muted">尚无实验；保存草稿不会自动执行。</p>}
    {strategy.experiments.map(item => <article className="real-experiment" key={item.launch_intent_id}><strong>{item.launch_intent_id}</strong><p>冻结 r{item.payload.draft_revision} · {item.status}{item.phase ? ` · ${item.phase}` : ''}</p>
      {item.message && <p>{item.message}</p>}{item.status === 'unknown' && <p>结果未知。只查询原请求，不重新派发。</p>}
      {item.job_id && <Link className="button" to={`/report/${encodeURIComponent(item.job_id)}/overview`}>{item.report_ready ? '打开冻结报告' : '查看运行与报告状态'}</Link>}
    </article>)}
  </section>
}

function Confirmation({ api, strategy, changed, done }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; done: () => void }) {
  const id = strategy.user_strategy_id, key = pendingKey(id)
  const [pending, setPending] = useState(() => saved(key)), [preview, setPreview] = useState<UserStrategyPreview | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const sending = useRef(false), alive = useRef(true)
  const open = strategy.experiments.find(item => unresolved.has(item.status))
  const queryId = pending || open?.launch_intent_id
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    if (queryId || isIntentDraft(strategy.draft)) return
    let active = true
    void api.previewUserStrategyExperiment!(id).then(value => {
      if (!active) return
      if (value.owner !== 'quine' || value.draft_revision !== strategy.draft.revision) throw new Error('预览的所有者或草稿修订不匹配，请重新读取研究。')
      setPreview(value)
    }).catch(e => { if (active) setError(reason(e)) })
    return () => { active = false }
  }, [api, id, queryId, strategy.draft])
  const query = async () => {
    if (sending.current) return
    sending.current = true; setBusy(true)
    try {
      const next = await api.getUserStrategy!(id)
      if (next.user_strategy_id !== id) throw new Error('研究身份不一致')
      if (!alive.current) return
      changed(next)
      const record = next.experiments.find(item => item.launch_intent_id === queryId)
      if (!record) setError('尚未读到原请求。保留确认身份，只能再次查询；不会重新派发。')
      else if (!unresolved.has(record.status)) { sessionStorage.removeItem(key); setPending(''); done() }
      else setError(`原请求 ${record.launch_intent_id}：${record.status}。继续保留，不重新派发。`)
    } catch (e) { if (alive.current) setError(reason(e)) }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
  const confirm = async () => {
    if (sending.current || queryId || !preview) return
    const confirmed = preview, intent = `studio-${crypto.randomUUID()}`
    sending.current = true; setBusy(true); setError('')
    // Retain identity before dispatch. Failure to retain means no request is sent.
    try { sessionStorage.setItem(key, intent) } catch { setError('无法保留确认身份，本次未发送。'); sending.current = false; setBusy(false); return }
    setPending(intent); setPreview(null)
    try {
      const next = await api.launchUserStrategyExperiment!(id, { launch_intent_id: intent, base_revision: confirmed.draft_revision, preview_identity: confirmed.preview_identity, approved_budget: { max_model_calls: 0, max_backtests: 1 } })
      if (next.user_strategy_id !== id || !next.experiments.some(item => item.launch_intent_id === intent)) throw new Error('回执未包含本次确认')
      if (!alive.current) return
      changed(next); sessionStorage.removeItem(key); setPending(''); done()
    } catch (e) { if (alive.current) setError(`确认结果未知：${reason(e)}。请查询原请求。`) }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
  return <section className="panel real-confirm"><h2>确认一次实验</h2><p>核对服务端接纳的冻结输入。本次允许 1 次回测、0 次模型调用。</p>
    <ErrorNote>{error}</ErrorNote>
    {queryId ? <><p>原请求：{queryId} · {open?.status ?? '结果待核对'}</p><button className="button" disabled={busy} onClick={() => void query()}>查询原请求</button></>
      : isIntentDraft(strategy.draft) ? <p>请先在草稿与数据页绑定策略和数据。</p>
        : preview ? <><dl className="key-values"><div><dt>草稿与策略</dt><dd>r{preview.draft_revision} · {preview.strategy_id}</dd></div><div><dt>场景</dt><dd>{preview.scenario_id}</dd></div><div><dt>标的 / 记录数</dt><dd>{preview.venue}:{preview.symbol} · {preview.record_count ?? '未知'} 条</dd></div><div><dt>初始资金</dt><dd>{preview.initial_cash}</dd></div><div><dt>参数</dt><dd>{JSON.stringify(preview.parameters)}</dd></div></dl>
          <details open><summary>冻结输入、成本与来源</summary><pre>{JSON.stringify(preview.normalized, null, 2)}</pre><pre>{JSON.stringify(preview.input_identity, null, 2)}</pre><small>预览身份：{preview.preview_identity}</small></details>
          <button className="button primary" disabled={busy} onClick={() => void confirm()}>确认并运行一次</button></> : !error && <p role="status">正在读取真实预览…</p>}
  </section>
}

function Assistant({ api, strategy, changed, jobId }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; jobId?: string }) {
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]))
  const [text, setText] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false), [uncertain, setUncertain] = useState(false)
  const sending = useRef(false), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const act = async (operation: () => Promise<UserStrategy>) => {
    if (sending.current || uncertain) return
    sending.current = true; setBusy(true); setError('')
    try { const next = await operation(); if (next.user_strategy_id !== strategy.user_strategy_id) throw new Error('研究身份不匹配'); if (alive.current) { changed(next); setText('') } }
    catch (e) { if (alive.current) { setError(`${reason(e)}。请重新读取研究核对结果，不会自动重发。`); setUncertain(true) } }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
  const selectedModel = strategy.model_profile_id ? model.value?.profiles?.find(item => item.id === strategy.model_profile_id) : model.value
  return <aside className="real-assistant panel" aria-label="研究助手"><h2>QUINE 研究助手</h2><p className="muted">{jobId ? `冻结报告 ${jobId}` : `当前草稿 r${strategy.draft.revision}`} · 模型回答为推断</p>
    <div className="real-messages">{strategy.messages.map(message => <article key={message.message_id}><small>{message.role === 'user' ? '你' : '模型 · 推断'}</small><p>{message.text}</p></article>)}</div>
    {strategy.proposals.filter(p => p.status === 'open').map(p => <section className="real-proposal" key={p.proposal_id}><h3>建议修改 · r{p.base_revision}</h3>{p.items.map(item => <p key={item.path}>{item.path}：{String(item.before)} → {String(item.after)}<br />{item.reason}</p>)}<button className="button" disabled={busy || uncertain || p.base_revision !== strategy.draft.revision} onClick={() => void act(() => api.applyUserStrategyProposal!(strategy.user_strategy_id, p.proposal_id, { operation_id: `apply-${crypto.randomUUID()}`, paths: p.items.map(item => item.path) }))}>应用修改到草稿</button><p className="muted">应用只保存新修订，运行仍需独立确认。</p></section>)}
    <label htmlFor="studio-message">消息</label><textarea id="studio-message" value={text} onChange={e => setText(e.target.value)} placeholder="检查当前草稿，或解释所选证据…" />
    <p className="muted">{selectedModel?.configured ? `${selectedModel.provider} · ${selectedModel.model}；一次发送授权 1 次模型调用` : '模型未配置或不可读；不会生成替代回答。'}</p>
    <button className="button primary" disabled={busy || uncertain || !selectedModel?.configured || !text.trim()} onClick={() => void act(() => api.sendUserStrategyMessage!(strategy.user_strategy_id, { call_id: `studio-${crypto.randomUUID()}`, base_revision: strategy.draft.revision, text, approved_budget: { max_model_calls: 1, max_backtests: 0 }, ...(jobId ? { evidence_job_ids: [jobId] } : {}) }))}>发送一次模型请求</button><ErrorNote>{error || model.error}</ErrorNote>
  </aside>
}

function Records({ api }: { api: ApiAdapter }) {
  const jobs = useRead(useCallback(() => api.listBacktests(), [api]))
  return <div className="page"><h1>回测记录</h1><ErrorNote>{jobs.error}</ErrorNote>{!jobs.value && !jobs.error && <p>正在读取…</p>}{jobs.value?.map(job => <Link className="real-research-row" to={`/report/${encodeURIComponent(job.job_id)}/overview`} key={job.job_id}><span>{job.job_id}</span><span>{job.status} · {job.report_ready ? '报告可读' : '无正式报告'}</span></Link>)}{jobs.value?.length === 0 && <p>暂无实验记录。</p>}<button className="button" onClick={jobs.reload}>重新读取记录</button></div>
}
function ReportRoute({ api }: { api: ApiAdapter }) { const { id = '' } = useParams(); return <Report key={id} api={api} id={id} /> }
function Report({ api, id }: { api: ApiAdapter; id: string }) {
  const { tab = 'overview' } = useParams(), [refresh, setRefresh] = useState(0)
  const run = useRunRead(api, id, String(refresh))
  const path = useRead(useCallback(async () => { if (run?.kind !== 'ready') return null; const value = await api.getEquityPath(id); if (value.run_id !== run.report.run_id || value.report_sha256 !== run.job.report_sha256) throw new Error('曲线身份与报告不一致'); return value }, [api, id, run]))
  const analytics = useRead(useCallback(async () => { if (run?.kind !== 'ready' || tab !== 'trades' || !api.getTradeAnalytics) return null; const value = await api.getTradeAnalytics(id); if (value.run_id !== run.report.run_id || value.report_sha256 !== run.job.report_sha256) throw new Error('交易分析身份与报告不一致'); return value }, [api, id, run, tab]))
  const source = useRead(useCallback(async () => {
    const refs = await api.researchReferences!(id)
    const matches = refs.flatMap(item => item.experiments.filter(e => e.job_id === id).map(e => ({ item, experiment: e })))
    if (matches.length !== 1) return null
    const owner = await api.getUserStrategy!(matches[0].item.user_strategy_id)
    if (owner.user_strategy_id !== matches[0].item.user_strategy_id || owner.experiments.filter(e => e.job_id === id).length !== 1) throw new Error('报告研究来源无法唯一核对')
    return { owner, revision: matches[0].experiment.payload?.draft_revision }
  }, [api, id]))
  return <div className="page"><header className="page-header"><div><Link to="/records">回测记录 /</Link><h1>冻结报告</h1><p className="subtitle">{id}</p></div><button className="button" onClick={() => setRefresh(n => n + 1)}>重新读取报告</button></header>
    <nav className="tabs">{[['overview', '概览'], ['trades', '交易'], ['inputs', '冻结输入'], ['files', '文件']].map(([key, label]) => <Link aria-current={tab === key ? 'page' : undefined} key={key} to={`/report/${encodeURIComponent(id)}/${key}`}>{label}</Link>)}</nav>
    {run?.kind === 'loading' && <p role="status">正在读取正式报告…</p>}{run?.kind === 'unavailable' && <ErrorNote>{run.note}</ErrorNote>}
    {run?.kind === 'ready' && <><FrozenReportFacts job={run.job} report={run.report} revision={source.value?.revision} /><div className="real-detail-grid"><section>
      {tab === 'overview' && <><FrozenReportMetrics report={run.report} path={path.value} />{path.value ? <EquityCurve analysis={path.value} /> : <p>权益曲线未提供。</p>}<ErrorNote>{path.error}</ErrorNote><section className="panel"><h2>来源与范围</h2><p>运行 {run.report.run_id} · {run.report.schema}</p><p>只展示这次运行的冻结事实，单次回测不代表未来收益。</p><p>{source.value ? <Link to={`/research/${encodeURIComponent(source.value.owner.user_strategy_id)}/experiments`}>{source.value.owner.name} · 返回研究</Link> : '没有唯一可核对的研究来源。'}</p></section></>}
      {tab === 'inputs' && <FrozenReportDetails job={run.job} report={run.report} path={path.value} />}
      {tab === 'trades' && <><ErrorNote>{analytics.error}</ErrorNote><ReportTrades report={run.report} analytics={analytics.value} view="complete" /><ReportTrades report={run.report} analytics={analytics.value} view="orders" /></>}
      {tab === 'files' && <section className="panel"><h2>正式产物</h2>{(['report.json', 'summary.txt', 'equity-path.json'] as const).map(name => <p key={name}><a href={api.artifactUrl(id, name)} download>{name}</a></p>)}<p>报告指纹：{run.job.report_sha256 ?? '未提供'}</p></section>}
    </section>{source.value ? <Assistant api={api} strategy={source.value.owner} jobId={id} changed={owner => source.setValue(current => current ? { ...current, owner } : null)} /> : <aside className="panel"><h2>研究助手</h2><p>未读取到唯一来源，会话暂不可用。</p><ErrorNote>{source.error}</ErrorNote></aside>}</div></>}
  </div>
}
function Models({ api }: { api: ApiAdapter }) {
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]))
  return <div className="page"><h1>模型连接</h1><p>此候选只读取现有配置。没有模型时，研究保存与本地实验仍可使用。</p><ErrorNote>{model.error}</ErrorNote>{model.value && <section className="panel"><h2>{model.value.configured ? '已配置' : '未配置'}</h2><p>{model.value.provider ?? '未提供'} · {model.value.model ?? '未提供'}</p><p>已配置不代表真实 provider 验收；此切片使用无密钥脚本验证交互。</p></section>}<button className="button" onClick={model.reload}>重新读取模型状态</button></div>
}
function LegacyResearchRedirect() { const { id } = useParams(), location = useLocation(); return <Navigate replace to={id ? `/research/${encodeURIComponent(id)}/draft${location.search}` : '/'} /> }
export function StudioRoutes({ api }: { api: ApiAdapter }) {
  return <div className="studio real-studio" data-theme="light"><aside className="sidebar" aria-label="工作区导航"><Link className="real-logo" to="/">QUINE<small>研究工作区</small></Link><Link className="button primary new-button" to="/">新建研究</Link><Link className="sidebar-link" to="/records">实验与报告</Link><Link className="sidebar-link" to="/resources/data">数据资源</Link><Link className="sidebar-link" to="/resources/models">模型连接</Link><div className="sidebar-footer"><span className="demo-foot">v3 本地研究候选</span><a href="/strategies">打开旧入口（回退）</a></div></aside>
    <header className="topbar"><nav className="primary-nav"><Link to="/">研究</Link><Link to="/records">回测</Link><Link to="/simulations">模拟 · 未接入</Link><Link to="/live">实盘 · 未接入</Link></nav><span className="environment">本地引擎 · 未发布</span></header>
    <main className="main"><Routes><Route path="/" element={<Home api={api} />} /><Route path="/research/:id/:tab?" element={<ResearchRoute api={api} />} /><Route path="/strategies/:id?" element={<LegacyResearchRedirect />} /><Route path="/records" element={<Records api={api} />} /><Route path="/report/:id/:tab?" element={<ReportRoute api={api} />} /><Route path="/resources/data" element={<div className="page"><h1>数据资源</h1><DataWorkspace api={api} /></div>} /><Route path="/resources/models" element={<Models api={api} />} /><Route path="*" element={<Unavailable />} /></Routes></main>
  </div>
}
export default function RealStudio() { return <BrowserRouter basename="/studio"><StudioRoutes api={browserApi} /></BrowserRouter> }
