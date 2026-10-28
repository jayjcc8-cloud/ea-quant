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
import { isIntentDraft, UserStrategyRequestError, type UserStrategy, type UserStrategyExperiment, type UserStrategyPreview } from './user-strategies-api'
import { StorageFailureGuard } from './storage-failure-guard'
import { AgentSurface, DetailSurface, PageHeader, Tabs, Logo, Icon } from '../studio/src/layout'
import '../studio/src/report-overview.css'
import '../studio/src/model-connections.css'

const reason = (error: unknown) => error instanceof Error ? error.message : '读取失败'
const unresolved = new Set(['pending', 'confirmed', 'running', 'stopping', 'unknown', 'unconfirmed'])
const holdsExperiment = (item: UserStrategyExperiment) => unresolved.has(item.status)
  && !(item.status === 'unknown' && item.slot_held === false && item.recovery?.status === 'released')
// Only these host refusals are proven to precede persistence/dispatch on launch.
// A generic HTTP error (including an owner error) still has an uncertain outcome.
const beforeDispatch = (error: unknown) => error instanceof UserStrategyRequestError
  && ['stale_base_revision', 'approval_required', 'user_strategy_not_found'].includes(error.code)
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
function fact(value: unknown): string {
  if (value === null || value === undefined) return '未记录'
  if (typeof value === 'object') return Object.entries(value).map(([key, entry]) => `${key}: ${fact(entry)}`).join(' · ') || '空声明'
  return String(value)
}
function Facts({ items }: { items: [string, unknown][] }) { return <dl className="key-values">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{fact(value)}</dd></div>)}</dl> }
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
  if (tab === 'confirm') return <div className="page"><PageHeader title="确认实验输入" subtitle="核对冻结输入后，明确启动一次本地引擎实验。" breadcrumb={<Link to={`/research/${encodeURIComponent(id)}/draft`}>{strategy.name} / 新建实验</Link>} /><Confirmation key={`${id}.${draft.revision}`} api={api} strategy={strategy} changed={changed} done={() => navigate(`/research/${encodeURIComponent(id)}/experiments`)} /></div>
  return <DetailSurface className="real-detail" assistant={<Assistant key={id} api={api} strategy={strategy} changed={changed} />}>
    <PageHeader title={strategy.name} subtitle={`草稿 r${draft.revision} · ${isIntentDraft(draft) ? '等待绑定策略与数据' : `${draft.venue}:${draft.symbol}`}`} breadcrumb={<Link to="/">研究 / {strategy.name}</Link>} actions={<Link className="button primary" to={`/research/${encodeURIComponent(id)}/confirm`}>准备一次实验</Link>} />
    <Tabs active={tab} items={[{ id: 'overview', label: '概览' }, { id: 'draft', label: '策略草稿' }, { id: 'experiments', label: '实验' }]} onChange={value => navigate(`/research/${encodeURIComponent(id)}/${value}`)} />
    <ErrorNote>{error}</ErrorNote>
    {tab === 'overview' && <>
      <h2>研究问题</h2><section className="panel"><p>{strategy.intent}</p></section>
      <h2>计划</h2><ol className="plan-steps"><li><div><strong>绑定策略与数据</strong><p>选择已注册策略，核对本次使用的数据来源。</p></div></li><li><div><strong>检查参数与成本</strong><p>保存草稿修订，再检查服务端预览。</p></div></li><li><div><strong>确认实验并阅读证据</strong><p>一次确认一个实验，历史报告保留冻结输入。</p></div></li></ol>
      <section className="panel"><div className="binding-row"><Icon name="cube" size={25} /><div><strong>策略模板</strong><small>{isIntentDraft(draft) ? '尚未绑定' : draft.strategy_id}</small></div><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>检查策略草稿</Link></div><div className="binding-row"><Icon name="data" size={25} /><div><strong>数据</strong><small>{isIntentDraft(draft) ? '尚未绑定' : draft.dataset_id ?? `注册场景 ${draft.scenario_id}`}</small></div><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>绑定策略与数据</Link></div></section>
    </>}
    {tab === 'draft' && (isIntentDraft(draft) ? <BindScenario api={api} strategy={strategy} scenarios={catalog.value ?? []} catalogReady={!!catalog.value} catalogError={catalog.error || null} reloadCatalog={catalog.reload} changed={changed} failed={setError} />
      : <><section className="panel parameter-form"><h2>策略草稿 · r{draft.revision}</h2><DraftEditor api={api} strategy={strategy} changed={changed} /></section><SelectDataset key={draft.revision} api={api} strategy={strategy} changed={changed} failed={setError} /></>)}
    {tab === 'experiments' && <Experiments strategy={strategy} refresh={loaded.reload} />}
    {!['overview', 'draft', 'experiments'].includes(tab) && <Unavailable />}
  </DetailSurface>

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
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [rejected, setRejected] = useState(false)
  const sending = useRef(false), alive = useRef(true)
  const open = strategy.experiments.find(holdsExperiment)
  const queryId = pending || open?.launch_intent_id
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    if (queryId || rejected || isIntentDraft(strategy.draft)) return
    let active = true
    void api.previewUserStrategyExperiment!(id).then(value => {
      if (!active) return
      if (value.owner !== 'quine' || value.draft_revision !== strategy.draft.revision) throw new Error('预览的所有者或草稿修订不匹配，请重新读取研究。')
      setPreview(value)
    }).catch(e => { if (active) setError(reason(e)) })
    return () => { active = false }
  }, [api, id, queryId, rejected, strategy.draft])
  const repreview = async () => {
    if (sending.current) return
    sending.current = true; setBusy(true)
    try {
      const next = await api.getUserStrategy!(id)
      if (next.user_strategy_id !== id) throw new Error('研究身份不一致')
      if (alive.current) { changed(next); setRejected(false); setError('') }
    } catch (e) { if (alive.current) setError(reason(e)) }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
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
      else if (!holdsExperiment(record)) { sessionStorage.removeItem(key); setPending(''); done() }
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
    } catch (e) {
      if (beforeDispatch(e)) {
        sessionStorage.removeItem(key)
        if (alive.current) { setPending(''); setRejected(true); setError(`本次未派发：${reason(e)}。重新读取后核对新的预览。`) }
      } else if (alive.current) setError(`确认结果未知：${reason(e)}。请查询原请求。`)
    }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
  return <section className="real-confirm"><h2>确认一次实验</h2>
    <ErrorNote>{error}</ErrorNote>
    {rejected && <button className="button" disabled={busy} onClick={() => void repreview()}>重新读取并预览</button>}
    {queryId ? <section className="panel"><p>原请求：{queryId} · {open?.status ?? '结果待核对'}</p><p>再次读取只查询这个身份，不会派发另一次实验。</p><button className="button" disabled={busy} onClick={() => void query()}>查询原请求</button></section>
      : isIntentDraft(strategy.draft) ? <p>请先在草稿与数据页绑定策略和数据。</p>
        : preview ? <><div className="two-column">
          <section className="panel"><h2>来源、参数与成本</h2><Facts items={[
            ['冻结草稿', `r${preview.draft_revision}`], ['策略', preview.strategy_id], ['场景', preview.scenario_id], ['标的', `${preview.venue}:${preview.symbol}`], ['记录数', preview.record_count], ['初始资金', preview.initial_cash], ['参数', preview.parameters], ['数据起点（UTC）', preview.data?.start_utc], ['数据终点（UTC）', preview.data?.end_utc], ['手续费', preview.commission], ['滑点', preview.slippage], ['延迟', preview.latency],
          ]} /></section>
          <section className="panel"><h2>本次执行</h2><Facts items={[[ '实验数', 1 ], ['模式', '本地真实引擎'], ['模型调用', 0], ['经济执行所有者', preview.owner]]} /><hr /><h2>范围与限制</h2><p>数据性质以冻结来源声明为准。短合成样例仅验证流程，不用于判断策略收益。</p><p>这次运行只使用已核对的输入。后续修改草稿不会改变报告。</p></section>
        </div><details><summary>核对原始冻结输入与身份</summary><pre>{JSON.stringify(preview.normalized, null, 2)}</pre><pre>{JSON.stringify(preview.input_identity, null, 2)}</pre><small>预览身份：{preview.preview_identity}</small></details>
          <div className="action-footer"><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>返回修改</Link><button className="button primary" disabled={busy} onClick={() => void confirm()}>确认并运行一次</button></div></> : !error && <p role="status">正在读取真实预览…</p>}
  </section>

}

type RetainedCall = { call_id: string; base_revision: number }
function retainedCalls(key: string): RetainedCall[] {
  try { const value = JSON.parse(saved(key) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item.call_id === 'string' && Number.isInteger(item.base_revision)) : [] } catch { return [] }
}
function Assistant({ api, strategy, changed, jobId, evidence }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; jobId?: string; evidence?: ReactNode }) {
  const id = strategy.user_strategy_id, key = `quine.studio.calls.${id}`
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]))
  const [text, setText] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [retained, setRetained] = useState(() => retainedCalls(key))
  const sending = useRef(false), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const remember = (items: RetainedCall[]) => { sessionStorage.setItem(key, JSON.stringify(items)); if (alive.current) setRetained(items) }
  const accept = (next: UserStrategy) => {
    if (next.user_strategy_id !== id) throw new Error('研究身份不匹配')
    if (alive.current) changed(next)
    remember(retainedCalls(key).filter(item => !next.calls.some(call => call.call_id === item.call_id)))
  }
  const act = async (operation: () => Promise<void>) => {
    if (sending.current) return
    sending.current = true; setBusy(true); setError('')
    try { await operation() }
    catch (e) { if (alive.current) setError(`${reason(e)}。保留原请求身份，请读取核对，不会自动重发。`) }
    finally { sending.current = false; if (alive.current) setBusy(false) }
  }
  const query = () => act(async () => {
    const next = await api.getUserStrategy!(id)
    accept(next)
    if (retainedCalls(key).length && alive.current) setError('尚未读到此调用，结果仍未知。保留身份，只查询原请求。')
  })
  const send = () => act(async () => {
    const call_id = `studio-${crypto.randomUUID()}`
    // A new explicit question is allowed after a classified unknown, as in the
    // existing conversation contract. It never overwrites or replays the old id.
    remember([...retainedCalls(key), { call_id, base_revision: strategy.draft.revision }])
    const next = await api.sendUserStrategyMessage!(id, { call_id, base_revision: strategy.draft.revision, text, approved_budget: { max_model_calls: 1, max_backtests: 0 }, ...(jobId ? { evidence_job_ids: [jobId] } : {}) })
    if (!next.calls.some(call => call.call_id === call_id)) throw new Error('回执未包含本次模型调用')
    accept(next)
    if (alive.current) setText('')
  })
  const selectedModel = strategy.model_profile_id ? model.value?.profiles?.find(item => item.id === strategy.model_profile_id) : model.value
  const inFlight = strategy.calls.some(call => ['pending', 'dispatched'].includes(call.status) || ['running', 'awaiting_budget', 'awaiting_decision'].includes(call.task?.status ?? ''))
  const localUnknown = retained.filter(item => !strategy.calls.some(call => call.call_id === item.call_id))
  return <AgentSurface context={jobId ? `冻结报告 ${jobId} · 只读证据` : `当前草稿 r${strategy.draft.revision}`} report={!!jobId}
    history={<>
      {evidence}
      <div className="agent-note">{jobId ? '这次讨论只引用所选运行的冻结事实。' : '先检查草稿与数据，再决定下一次实验。'}<small>模型回答为推断；修改提案需要审阅，实验需要独立确认。</small></div>
      <details className="agent-context"><summary>来源与上下文</summary><p>研究 {id} · 草稿 r{strategy.draft.revision}</p><Link to={`/research/${encodeURIComponent(id)}/draft`}>查看当前草稿</Link>{jobId && <><p><Link to={`/report/${encodeURIComponent(jobId)}/inputs`}>冻结输入</Link></p><Link to={`/report/${encodeURIComponent(jobId)}/trades`}>交易证据</Link></>}</details>
      {strategy.messages.map(message => <article className={`message ${message.role}`} key={message.message_id}><small>{message.role === 'user' ? '你' : '模型 · 推断'}</small><p>{message.text}</p></article>)}
      {(strategy.calls.length > 0 || localUnknown.length > 0) && <section className="call-history" aria-label="模型调用记录"><h3>调用记录</h3>
        {strategy.calls.map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · {call.status}</p>{call.status === 'unknown' && <p>结果与用量未知，可能已到达提供方；不会重发。可以明确发送一个新问题。</p>}{call.message && <p>{call.message}</p>}<small>{call.usage_available ? `输入 ${call.input_tokens ?? '未提供'} / 输出 ${call.output_tokens ?? '未提供'} tokens` : '未报告用量，不按零计费'}</small></article>)}
        {localUnknown.map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · 回执未知，保留原请求身份。</p></article>)}
        <button className="button" disabled={busy} onClick={() => void query()}>查询模型请求</button>
      </section>}
      {strategy.proposals.filter(p => p.status === 'open').map(p => <section className="real-proposal" key={p.proposal_id}><h3>建议修改 · r{p.base_revision}</h3>{p.items.map(item => <p key={item.path}>{item.path}：{String(item.before)} → {String(item.after)}<br />{item.reason}</p>)}<button className="button" disabled={busy || p.base_revision !== strategy.draft.revision} onClick={() => void act(async () => accept(await api.applyUserStrategyProposal!(id, p.proposal_id, { operation_id: `apply-${crypto.randomUUID()}`, paths: p.items.map(item => item.path) })))}>应用修改到草稿</button><p className="muted">应用只保存新修订，运行仍需独立确认。</p></section>)}
      {!strategy.messages.length && !strategy.calls.length && !localUnknown.length && <div className="suggestions"><small>你可以这样问</small><button className="button" onClick={() => setText(jobId ? '解释这份报告的结果与限制' : '检查当前草稿的参数与数据')}>检查当前证据</button><button className="button" onClick={() => setText('下一步应该检查哪些证据？')}>检查下一步</button></div>}
    </>}
    input={<><ErrorNote>{error || model.error}</ErrorNote><label className="sr-only" htmlFor="studio-message">消息</label><textarea id="studio-message" value={text} onChange={e => setText(e.target.value)} placeholder={jobId ? '询问这份报告…' : '继续讨论这个研究…'} />
      <small>{selectedModel?.configured ? `${selectedModel.provider} · ${selectedModel.model}；一次发送授权 1 次模型调用` : '模型未配置或不可读；不会生成替代回答。'}</small>
      <button className="button primary" disabled={busy || inFlight || !selectedModel?.configured || !text.trim()} onClick={() => void send()}>发送一次模型请求</button>
      {localUnknown.length > 0 && <small>新发送是独立问题，不是补发上方未知请求；原身份仍可查询。</small>}
    </>} />
}

function Records({ api }: { api: ApiAdapter }) {
  const jobs = useRead(useCallback(() => api.listBacktests(), [api]))
  return <div className="page"><h1>回测记录</h1><ErrorNote>{jobs.error}</ErrorNote>{!jobs.value && !jobs.error && <p>正在读取…</p>}{jobs.value?.map(job => <Link className="real-research-row" to={`/report/${encodeURIComponent(job.job_id)}/overview`} key={job.job_id}><span>{job.job_id}</span><span>{job.status} · {job.report_ready ? '报告可读' : '无正式报告'}</span></Link>)}{jobs.value?.length === 0 && <p>暂无实验记录。</p>}<button className="button" onClick={jobs.reload}>重新读取记录</button></div>
}
function ReportRoute({ api }: { api: ApiAdapter }) { const { id = '' } = useParams(); return <Report key={id} api={api} id={id} /> }
function Report({ api, id }: { api: ApiAdapter; id: string }) {
  const { tab = 'overview' } = useParams(), [refresh, setRefresh] = useState(0), navigate = useNavigate()
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
  const evidence = run?.kind === 'ready' ? <section className="report-evidence" aria-label="报告解读与证据"><div className="report-evidence-heading"><h2>这份报告说明了什么</h2><small>本地引擎 · 已冻结事实 · 未调用模型</small></div><div className="report-reading"><p>净盈亏 <strong>{run.report.economics.net_pnl.amount} {run.report.economics.currency}</strong>；订单 / 成交 {run.report.economics.counts.orders} / {run.report.economics.counts.fills}。</p><p>单次运行不足以判断稳健性。数据与成本需先核对。</p></div><div className="report-evidence-links"><Link to={`/report/${encodeURIComponent(id)}/inputs`}>[1] 冻结输入与来源</Link><Link to={`/report/${encodeURIComponent(id)}/trades`}>[2] 交易明细</Link></div><div className="report-next-step"><h3>下一步检查</h3><p>核对交易与成本，再决定下一次独立实验。</p><Link className="button" to={`/report/${encodeURIComponent(id)}/trades`}>检查交易</Link></div></section> : undefined
  return <DetailSurface className="report-overview" assistant={source.value ? <Assistant api={api} strategy={source.value.owner} jobId={id} evidence={evidence} changed={owner => source.setValue(current => current ? { ...current, owner } : null)} /> : <AgentSurface context="冻结报告 · 只读证据" report history={<>{evidence}<p>未读取到唯一研究来源，会话暂不可用。</p><ErrorNote>{source.error}</ErrorNote></>} input={<small>不会生成替代回答。</small>} />}>
    <PageHeader title="冻结报告" subtitle={id} breadcrumb={<Link to="/records">回测记录 / {id}</Link>} actions={<button className="button" onClick={() => setRefresh(n => n + 1)}>重新读取报告</button>} />
    <Tabs active={tab} items={[{ id: 'overview', label: '概览' }, { id: 'trades', label: '交易' }, { id: 'inputs', label: '冻结输入' }, { id: 'files', label: '文件' }]} onChange={value => navigate(`/report/${encodeURIComponent(id)}/${value}`)} />
    {run?.kind === 'loading' && <p role="status">正在读取正式报告…</p>}{run?.kind === 'unavailable' && <ErrorNote>{run.note}</ErrorNote>}
    {run?.kind === 'ready' && <><FrozenReportFacts job={run.job} report={run.report} revision={source.value?.revision} />
      {tab === 'overview' && <><FrozenReportMetrics report={run.report} path={path.value} /><section className="panel chart"><h2>权益路径</h2>{path.value ? <EquityCurve analysis={path.value} /> : <p>权益曲线未提供。</p>}<ErrorNote>{path.error}</ErrorNote></section><section className="panel"><h2>来源与范围</h2><dl className="report-sources"><div><dt>冻结版本</dt><dd>{source.value?.revision == null ? '未记录' : `r${source.value.revision}`}<span>{run.report.source?.strategy.id ?? run.report.scenario?.strategy_id ?? '未记录'}</span></dd></div><div><dt>数据版本</dt><dd>{run.job.dataset_version?.dataset_id ?? '注册场景冻结数据'}<span>{run.job.input_identity.record_count ?? '未记录'} 条</span></dd></div><div><dt>运行身份</dt><dd>{run.report.run_id}<span>{run.report.schema}</span></dd></div></dl><p className="notice">单次回测只反映冻结场景，不代表未来收益。</p>{source.value && <Link className="button" to={`/research/${encodeURIComponent(source.value.owner.user_strategy_id)}/experiments`}>{source.value.owner.name} · 返回研究</Link>}</section></>}
      {tab === 'inputs' && <><p className="notice">冻结输入只读。当前草稿的后续修改不会改变本报告。</p><FrozenReportDetails job={run.job} report={run.report} path={path.value} /></>}
      {tab === 'trades' && <><ErrorNote>{analytics.error}</ErrorNote><ReportTrades report={run.report} analytics={analytics.value} view="complete" /><ReportTrades report={run.report} analytics={analytics.value} view="orders" /></>}
      {tab === 'files' && <section className="panel"><h2>正式产物</h2>{(['report.json', 'summary.txt', 'equity-path.json'] as const).map(name => <p key={name}><a className="button" href={api.artifactUrl(id, name)} download><Icon name="download" />{name}</a></p>)}<p>报告指纹：{run.job.report_sha256 ?? '未提供'}</p></section>}
      {!['overview', 'inputs', 'trades', 'files'].includes(tab) && <Unavailable />}
    </>}
  </DetailSurface>

}
function Models({ api }: { api: ApiAdapter }) {
  const model = useRead(useCallback(() => api.settingsStatus!(), [api])), [selectedId, setSelectedId] = useState('')
  const profiles = model.value?.profiles ?? [], selected = profiles.find(item => item.id === selectedId) ?? profiles.find(item => item.id === model.value?.default_profile_id) ?? profiles[0] ?? model.value
  return <div className="page model-connections"><PageHeader title="模型连接" subtitle="查看研究助手当前可用的连接。此候选不修改连接或凭据。" actions={<button className="button" onClick={model.reload}>重新读取模型状态</button>} />
    <ErrorNote>{model.error}</ErrorNote>
    <div className="model-layout"><section className="model-directory" aria-label="已保存的模型连接">
      {profiles.map(item => <button className={`directory-item ${selected === item ? 'selected' : ''}`} aria-current={selected === item ? 'true' : undefined} key={item.id} onClick={() => setSelectedId(item.id)}><Icon name="cube" /><span><span className="connection-name"><strong>{item.name}</strong>{item.id === model.value?.default_profile_id && <span className="status">默认</span>}</span><small>{item.configured ? '已配置 · 请求未验证' : '不可用'}</small></span></button>)}
      {profiles.length === 0 && <p>没有已保存的连接目录。</p>}
    </section><section className="panel model-editor"><h2>{selected?.configured ? '已配置' : '未配置或不可读'}</h2>{selected && <Facts items={[[ '提供方', selected.provider ], ['模型', selected.model], ['协议', selected.protocol], ['配置问题', selected.configuration_error ?? '未报告'], ['缺失能力', selected.missing_capabilities?.join('、') || '未报告']]} />}
      <section className="connection-test"><h3>请求验证</h3><p>配置可读不等于真实 provider 已通过验收。没有模型时，研究保存与本地实验仍可使用。</p><p>本切片仅以现有无密钥脚本适配器验证交互。</p></section>
    </section></div>
  </div>
}

function LegacyResearchRedirect() { const { id } = useParams(), location = useLocation(); return <Navigate replace to={id ? `/research/${encodeURIComponent(id)}/draft${location.search}` : '/'} /> }
export function StudioRoutes({ api }: { api: ApiAdapter }) {
  return <div className="studio real-studio" data-theme="light"><aside className="sidebar" aria-label="工作区导航"><Link className="real-logo" to="/"><Logo /></Link><Link className="button primary new-button" to="/" title="新建研究"><Icon name="plus" /><span>新建研究</span></Link><Link className="sidebar-link" to="/records" title="实验与报告"><Icon name="chart" /><span className="label">实验与报告</span></Link><Link className="sidebar-link" to="/resources/data" title="数据资源"><Icon name="data" /><span className="label">数据资源</span></Link><Link className="sidebar-link" to="/resources/models" title="模型连接"><Icon name="cube" /><span className="label">模型连接</span></Link><div className="sidebar-footer"><span className="demo-foot">v3 本地研究候选</span></div></aside>
    <header className="topbar"><nav className="primary-nav"><Link to="/">研究</Link><Link to="/records">回测</Link><Link to="/simulations">模拟 · 未接入</Link><Link to="/live">实盘 · 未接入</Link></nav><span className="environment">本地引擎 · 未发布</span></header>
    <main className="main"><Routes><Route path="/" element={<Home api={api} />} /><Route path="/research/:id/:tab?" element={<ResearchRoute api={api} />} /><Route path="/strategies/:id?" element={<LegacyResearchRedirect />} /><Route path="/records" element={<Records api={api} />} /><Route path="/report/:id/:tab?" element={<ReportRoute api={api} />} /><Route path="/resources/data" element={<div className="page"><h1>数据资源</h1><DataWorkspace api={api} /></div>} /><Route path="/resources/models" element={<Models api={api} />} /><Route path="*" element={<Unavailable />} /></Routes></main>
  </div>
}
export default function RealStudio() { return <BrowserRouter basename="/studio"><StudioRoutes api={browserApi} /></BrowserRouter> }
