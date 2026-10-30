import { CompareBacktestsRoute } from './backtest-comparison'
/** v3's first real slice. No scenario adapter, fixture state or synthetic outcome. */
import { useCallback, useEffect, useRef, useState, type ReactNode, type SetStateAction } from 'react'
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { browserApi, type ApiAdapter } from './app'
import { BindScenario, SelectDataset, UnknownRecoveryControls } from './user-strategies'
import { DraftEditor } from './draft-editor'
import { DataWorkspace } from './research'
import { DesktopSettings } from './desktop-settings'
import { DesktopAbout } from './desktop-about'
import { FrozenReportDetails, FrozenReportFacts, FrozenReportMetrics, ReportTrades } from './frozen-report'
import { EquityCurve } from './equity-path'
import { useRunRead } from './user-strategy-research'
import { isIntentDraft, UserStrategyRequestError, type UserStrategy, type UserStrategyExperiment, type UserStrategyPreview } from './user-strategies-api'
import { HoldoutCreatorRoute, HoldoutDetailRoute, HoldoutLinks } from './studio-holdout'
import { CandidateDetail, CandidateHistory } from './candidates'
import { StorageFailureGuard } from './storage-failure-guard'
import { AgentSurface, DetailSurface, DraftSurface, ReportOverviewSurface, PageHeader, Tabs, Logo, Icon } from '../studio/src/layout'
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
function Unavailable() { return <div className="page"><h1>此候选尚未接入</h1><p>模拟、实盘、批量与候选晋级暂不可用。</p><Link to="/">返回研究</Link></div> }

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
  const { tab = 'overview' } = useParams(), navigate = useNavigate(), location = useLocation()
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
    <PageHeader title={strategy.name} subtitle={`草稿 r${draft.revision} · ${isIntentDraft(draft) ? '等待绑定策略与数据' : `${draft.venue}:${draft.symbol}`}`} breadcrumb={<Link to="/">研究 / {strategy.name}</Link>} actions={tab !== 'draft' && <Link className="button primary" to={`/research/${encodeURIComponent(id)}/confirm`}>准备一次实验</Link>} />
    <nav className="tabs" aria-label="页面标签">{[['overview', '概览'], ['draft', '策略草稿'], ['experiments', '实验']].map(([value, label]) => <Link key={value} to={`/research/${encodeURIComponent(id)}/${value}`} aria-current={tab === value ? 'page' : undefined}>{label}</Link>)}</nav>
    <ErrorNote>{error}</ErrorNote>
    {tab === 'overview' && <>
      <h2>研究问题</h2><section className="panel"><p>{strategy.intent}</p></section>
      <h2>计划</h2><ol className="plan-steps"><li><div><strong>绑定策略与数据</strong><p>选择已注册策略，核对本次使用的数据来源。</p></div></li><li><div><strong>检查参数与成本</strong><p>保存草稿修订，再检查服务端预览。</p></div></li><li><div><strong>确认实验并阅读证据</strong><p>一次确认一个实验，历史报告保留冻结输入。</p></div></li></ol>
      <section className="panel"><div className="binding-row"><Icon name="cube" size={25} /><div><strong>策略模板</strong><small>{isIntentDraft(draft) ? '尚未绑定' : draft.strategy_id}</small></div><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>检查策略草稿</Link></div><div className="binding-row"><Icon name="data" size={25} /><div><strong>数据</strong><small>{isIntentDraft(draft) ? '尚未绑定' : draft.dataset_id ?? `注册场景 ${draft.scenario_id}`}</small></div><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>绑定策略与数据</Link></div></section>
    </>}
    {tab === 'draft' && (isIntentDraft(draft) ? <BindScenario api={api} strategy={strategy} scenarios={catalog.value ?? []} catalogReady={!!catalog.value} catalogError={catalog.error || null} reloadCatalog={catalog.reload} changed={changed} failed={setError} />
      : <><ResearchDraft api={api} strategy={strategy} changed={changed} /><details className="real-data-binding"><summary>修改绑定数据或导入 CSV</summary><SelectDataset key={draft.revision} api={api} strategy={strategy} changed={changed} failed={setError} /></details></>)}
    {tab === 'experiments' && <Experiments api={api} strategy={strategy} changed={changed} refresh={loaded.reload} />}
    {tab === 'proposal' && <ProposalReview api={api} strategy={strategy} proposalId={new URLSearchParams(location.search).get('proposal')} changed={changed} />}
    {!['overview', 'draft', 'experiments', 'proposal'].includes(tab) && <Unavailable />}
  </DetailSurface>

}
function ProposalReview({ api, strategy, proposalId, changed }: { api: ApiAdapter; strategy: UserStrategy; proposalId: string | null; changed: (next: UserStrategy) => void }) {
  const proposal = strategy.proposals.find(item => item.proposal_id === proposalId)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), sending = useRef(false), navigate = useNavigate()
  const decide = async (apply: boolean) => {
    if (!proposal || sending.current || proposal.status !== 'open') return
    sending.current = true; setBusy(true); setError('')
    try {
      const next = apply ? await api.applyUserStrategyProposal!(strategy.user_strategy_id, proposal.proposal_id, { operation_id: `apply-${crypto.randomUUID()}`, paths: proposal.items.map(item => item.path) }) : await api.rejectUserStrategyProposal!(strategy.user_strategy_id, proposal.proposal_id)
      if (next.user_strategy_id !== strategy.user_strategy_id) throw new Error('研究身份不匹配')
      changed(next); navigate(`/research/${encodeURIComponent(strategy.user_strategy_id)}/draft`)
    } catch (e) { setError(`${reason(e)}。请重新读取研究核对结果，不会自动重发。`) }
    finally { sending.current = false; setBusy(false) }
  }
  if (!proposal) return <p>未读取到此提案，请从研究助手打开已有提案。</p>
  return <><h2>审阅修改提案</h2><p>基于草稿 r{proposal.base_revision} · {proposal.status}</p>
    <div className="table-scroll"><table><thead><tr><th>参数</th><th>当前值</th><th>提案值</th></tr></thead><tbody>{proposal.items.map(item => <tr key={item.path}><td>{item.path}<small className="muted">{item.reason}</small></td><td>{String(item.before)}</td><td className="diff-change">{String(item.after)}</td></tr>)}</tbody></table></div>
    <p className="notice">应用仅保存草稿的新修订，不会运行实验。提案中的解释属于模型推断。</p><ErrorNote>{error}</ErrorNote>
    <div className="action-footer"><Link className="button" to={`/research/${encodeURIComponent(strategy.user_strategy_id)}/draft`}>返回草稿</Link><div className="button-row"><button className="button" disabled={busy || proposal.status !== 'open' || !!error || !api.rejectUserStrategyProposal} onClick={() => void decide(false)}>拒绝提案</button><button className="button primary" disabled={busy || proposal.status !== 'open' || !!error || proposal.base_revision !== strategy.draft.revision} onClick={() => void decide(true)}>应用修改到草稿</button></div></div>
  </>
}
function ResearchDraft({ api, strategy, changed }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void }) {
  const id = strategy.user_strategy_id, revision = strategy.draft.revision
  const source = useRead(useCallback(async () => {
    const next = await api.previewUserStrategyExperiment!(id)
    if (next.owner !== 'quine' || next.draft_revision !== revision) throw new Error('来源修订已改变，请重新读取研究。')
    return next
  }, [api, id, revision]))
  const draft = strategy.draft
  if (isIntentDraft(draft)) return null
  return <><div className="section-heading"><h2>策略草稿 · <span className="status">r{revision}</span></h2><Link className="button primary" to={`/research/${encodeURIComponent(id)}/confirm`}>准备一次实验</Link></div>
    <DraftSurface source={<><Facts items={[
      ['策略模板', draft.strategy_id], ['数据', draft.dataset_id ?? draft.scenario_id], ['标的', `${draft.venue}:${draft.symbol}`],
      ['样本区间（UTC）', source.value?.data?.start_utc && source.value.data.end_utc ? `${source.value.data.start_utc} — ${source.value.data.end_utc}` : '尚未读到'],
    ]} /><ErrorNote>{source.error}</ErrorNote></>}
      parameters={<DraftEditor presentation="studio" api={api} strategy={strategy} changed={changed} />}
      costs={<><Facts items={[[ '手续费', source.value?.commission ], ['滑点', source.value?.slippage], ['延迟', source.value?.latency]]} /><p className="muted">成本由所选场景提供，当前只读。保存草稿后，进入独立确认页核对本次执行。</p></>} />
  </>
}
function ExperimentControls({ api, strategyId, item, changed }: { api: ApiAdapter; strategyId: string; item: UserStrategyExperiment; changed: (next: UserStrategy) => void }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const sending = useRef(false), active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const stop = async () => {
    if (sending.current || !api.cancelUserStrategyExperiment) return
    sending.current = true; setBusy(true); setError('')
    try {
      const next = await api.cancelUserStrategyExperiment(strategyId, item.launch_intent_id)
      if (next.user_strategy_id !== strategyId) throw new Error('研究身份不一致')
      if (active.current) changed(next)
    } catch (e) {
      if (active.current) setError(`${reason(e)}。请重新读取研究核对停止结果；不会自动重试或重跑。`)
    } finally { sending.current = false; if (active.current) setBusy(false) }
  }
  if (item.owner !== 'quine') return null
  return <>
    {['confirmed', 'running', 'stopping'].includes(item.status) && <div className="real-experiment-controls">
      <button className="button" disabled={busy || item.status === 'stopping' || !api.cancelUserStrategyExperiment} onClick={() => void stop()}>{busy ? '正在请求停止…' : '停止这次运行'}</button>
      <p className="muted" role="status">{item.status === 'stopping' ? '停止请求已受理，尚未证实停止；等待运行所有者记录终态。' : '停止请求由运行所有者处理，以返回的状态为准。'}</p>
    </div>}
    <ErrorNote>{error}</ErrorNote>
    {item.status === 'unknown' && <UnknownRecoveryControls strategyId={strategyId} item={item} changed={changed} recover={api.recoverUserStrategyExperiment} diagnosticsHref="/settings/about" />}
  </>
}
function Experiments({ api, strategy, changed, refresh }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; refresh: () => void }) {
  return <section className="panel"><div className="section-heading"><h2>实验记录</h2><button className="button" onClick={refresh}>重新读取研究</button></div>
    {!strategy.experiments.length && <p className="muted">尚无实验；保存草稿不会自动执行。</p>}
    {strategy.experiments.map(item => <article className="real-experiment" key={item.launch_intent_id}><strong>{item.launch_intent_id}</strong><p>冻结 r{item.payload.draft_revision} · {item.status}{item.phase ? ` · ${item.phase}` : ''}</p>
      {item.message && <p>{item.message}</p>}{item.status === 'unknown' && <p>结果未知。只查询原请求，不重新派发。</p>}
      <ExperimentControls api={api} strategyId={strategy.user_strategy_id} item={item} changed={changed} />
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

type RetainedCall = { call_id: string; base_revision: number; text?: string }
function retainedCalls(key: string): RetainedCall[] {
  try { const value = JSON.parse(saved(key) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item.call_id === 'string' && Number.isInteger(item.base_revision)) : [] } catch { return [] }
}
function Assistant({ api, strategy, changed, jobId, evidence }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; jobId?: string; evidence?: ReactNode }) {
  const id = strategy.user_strategy_id, key = `quine.studio.calls.${id}`
  const textKey = `quine.studio.unsent.${id}.${jobId ? `report.${jobId}` : 'draft'}`
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]))
  const [text, setText] = useState(() => saved(textKey)), [textRetained, setTextRetained] = useState(true)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [retained, setRetained] = useState(() => retainedCalls(key))
  const sending = useRef(false), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const edit = (value: string) => {
    setText(value)
    try { if (value) sessionStorage.setItem(textKey, value); else sessionStorage.removeItem(textKey); setTextRetained(true) }
    catch { setTextRetained(false) }
  }
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
    catch (e) { if (alive.current) setError(`${reason(e)}。请读取核对，不会自动重发。`) }
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
    try { remember([...retainedCalls(key), { call_id, base_revision: strategy.draft.revision, text: text.trim() }]) } catch { throw new Error('无法保留请求身份，本次未发送') }
    const next = await api.sendUserStrategyMessage!(id, { call_id, base_revision: strategy.draft.revision, text, approved_budget: { max_model_calls: 1, max_backtests: 0 }, ...(jobId ? { evidence_job_ids: [jobId] } : {}) })
    if (!next.calls.some(call => call.call_id === call_id)) throw new Error('回执未包含本次模型调用')
    accept(next)
    // A late receipt may clear only the submitted draft, never newer typing or another context.
    if (saved(textKey) === text) sessionStorage.removeItem(textKey)
    if (alive.current) setText(current => current === text ? '' : current)
  })
  const selectedModel = strategy.model_profile_id ? model.value?.profiles?.find(item => item.id === strategy.model_profile_id) : model.value
  const inFlight = strategy.calls.some(call => ['pending', 'dispatched'].includes(call.status) || ['running', 'awaiting_budget', 'awaiting_decision'].includes(call.task?.status ?? ''))
  const localUnknown = retained.filter(item => !strategy.calls.some(call => call.call_id === item.call_id))
  return <AgentSurface context={jobId ? `冻结报告 ${jobId.slice(0, 8)} · 只读证据` : `当前草稿 r${strategy.draft.revision}`} report={!!jobId}
    history={<>
      {evidence}
      {!evidence && <div className="agent-note">{jobId ? '这次讨论只引用所选运行的冻结事实。' : '先检查草稿与数据，再决定下一次实验。'}<small>模型回答为推断；修改提案需要审阅，实验需要独立确认。</small></div>}
      <details className="agent-context"><summary>来源与上下文</summary><p>研究 {id} · 草稿 r{strategy.draft.revision}</p><Link to={`/research/${encodeURIComponent(id)}/draft`}>查看当前草稿</Link>{jobId && <><p><Link to={`/report/${encodeURIComponent(jobId)}/inputs`}>冻结输入</Link></p><Link to={`/report/${encodeURIComponent(jobId)}/trades`}>交易证据</Link></>}</details>
      {strategy.messages.map(message => <article className={`message ${message.role}`} key={message.message_id}><small>{message.role === 'user' ? '你' : '模型 · 推断'}</small><p>{message.text}</p></article>)}
      {(strategy.calls.length > 0 || localUnknown.length > 0) && <section className="call-history" aria-label="模型调用记录"><h3>调用记录</h3>
        {strategy.calls.map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · {call.status}</p>{call.model_profile && <small>当次连接：{call.model_profile.name} · v{call.model_profile.revision}</small>}{call.status === 'unknown' && <p>结果与用量未知，可能已到达提供方；不会重发。可以明确发送一个新问题。</p>}{call.message && <p>{call.message}</p>}<small>{call.usage_available ? `输入 ${call.input_tokens ?? '未提供'} / 输出 ${call.output_tokens ?? '未提供'} tokens` : '未报告用量，不按零计费'}</small></article>)}
        {localUnknown.map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · 回执未知，保留原请求身份。</p></article>)}
        <button className="button" disabled={busy} onClick={() => void query()}>查询模型请求</button>
      </section>}
      {strategy.proposals.filter(p => p.status === 'open').map(p => <section className="real-proposal" key={p.proposal_id}><h3>建议修改 · r{p.base_revision}</h3><p>{p.items.length} 项变更，等待你审阅。</p><Link className="button" to={`/research/${encodeURIComponent(id)}/proposal?proposal=${encodeURIComponent(p.proposal_id)}`}>审阅修改提案</Link><p className="muted">应用只保存新修订，运行仍需独立确认。</p></section>)}
      {!jobId && !strategy.messages.length && !strategy.calls.length && !localUnknown.length && <div className="suggestions"><small>你可以这样问</small><button className="button" onClick={() => edit(jobId ? '解释这份报告的结果与限制' : '检查当前草稿的参数与数据')}>检查当前证据</button><button className="button" onClick={() => edit('下一步应该检查哪些证据？')}>检查下一步</button></div>}
    </>}
    input={<><ErrorNote>{error || model.error}</ErrorNote><label className="sr-only" htmlFor="studio-message">消息</label><textarea id="studio-message" value={text} onChange={e => edit(e.target.value)} placeholder={jobId ? '询问这份报告…' : '继续讨论这个研究…'} />
      {!textRetained && text && <small>未发送消息无法在本机缓冲，离开前请保留输入。</small>}
      <StorageFailureGuard active={!textRetained && !!text} message="未发送消息无法在本机保留，离开将丢失输入。" />
      {api.selectUserStrategyModelProfile && model.value?.profiles && <label className="research-model-picker">本研究模型连接<select value={strategy.model_profile_id ?? ''} disabled={busy || inFlight || localUnknown.length > 0} onChange={event => { const profileId = event.target.value; void act(async () => accept(await api.selectUserStrategyModelProfile!(id, profileId || null))) }}>
        <option value="">跟随工作区默认 · {model.value.profiles.find(item => item.id === model.value?.default_profile_id)?.name ?? '未设置'}</option>
        {strategy.model_profile_id && !model.value.profiles.some(item => item.id === strategy.model_profile_id) && <option value={strategy.model_profile_id}>已选连接不可用，请重新选择</option>}
        {model.value.profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}{item.configured ? '' : ' · 未配置'}</option>)}
      </select></label>}
      <small>选择仅影响本研究下一次发送；历史调用与已批准轮次保持原连接。</small>
      <div className="research-model-actions"><Link to="/resources/models">管理模型连接</Link><button className="button" disabled={busy} onClick={model.reload}>重新读取连接</button></div>
      <small>{selectedModel?.configured ? `${selectedModel.provider} · ${selectedModel.model}；一次发送授权 1 次模型调用` : '模型未配置或不可读；不会生成替代回答。'}</small>
      <button aria-label="发送一次模型请求" title="发送一次模型请求" className="button primary real-send" disabled={busy || inFlight || localUnknown.some(call => call.text === text.trim()) || !selectedModel?.configured || !text.trim()} onClick={() => void send()}><Icon name="send" size={22} /></button>
      {localUnknown.length > 0 && <small>同一问题只查询原请求。可以输入不同的新问题，原身份仍保留。</small>}
    </>} />
}

function Records({ api }: { api: ApiAdapter }) {
  const [selected, setSelected] = useState<string[]>([])
  const navigate = useNavigate()
  const records = useRead(useCallback(async () => {
    const refs = await api.researchReferences!()
    return Promise.all(refs.flatMap(research => research.experiments.map(async experiment => {
      if (!experiment.job_id) return { research, experiment, job: null, error: '' }
      try {
        const job = await api.getBacktest(experiment.job_id)
        if (job.job_id !== experiment.job_id) throw new Error('运行身份不一致')
        return { research, experiment, job, error: '' }
      } catch (e) { return { research, experiment, job: null, error: reason(e) } }
    })))
  }, [api]))
  const ready = new Set(records.value?.flatMap(({ job }) => job?.status === 'succeeded' && job.report_ready ? [job.job_id] : []) ?? [])
  const comparison = selected.filter(id => ready.has(id))
  return <div className="page"><PageHeader title="回测记录" subtitle="从已保存研究读取实验与正式报告。" actions={<><Link className="button" to="/candidates">研究候选历史</Link><button className="button" onClick={records.reload}>重新读取记录</button></>} />
    <ErrorNote>{records.error}</ErrorNote>{!records.value && !records.error && <p>正在读取…</p>}
    <div className="result-toolbar"><p>选择两份已完成的冻结报告，查看参数、成本与结果差异。</p><button className="button" disabled={comparison.length !== 2} onClick={() => navigate(`/compare/${encodeURIComponent(comparison[0])}/${encodeURIComponent(comparison[1])}`)}>对比所选报告</button></div>
    {records.value?.map(({ research, experiment, job, error }, index) => <div key={`${research.user_strategy_id}.${experiment.launch_intent_id ?? index}`}>
      {job ? <div className="comparison-record"><label><input type="checkbox" aria-label={`选择 ${job.job_id} 进行对比`} checked={comparison.includes(job.job_id)} disabled={!ready.has(job.job_id) || (comparison.length === 2 && !comparison.includes(job.job_id))} onChange={() => setSelected(current => current.includes(job.job_id) ? current.filter(id => id !== job.job_id) : [...current.filter(id => ready.has(id)), job.job_id])} /><span className="sr-only">选择报告</span></label><Link className="real-research-row" to={`/report/${encodeURIComponent(job.job_id)}/overview`}><span><strong>{research.name}</strong><small>{job.job_id}</small></span><span>{job.status} · {job.report_ready ? '报告可读' : '无正式报告'}</span></Link></div>
        : <section className="panel"><Link to={`/research/${encodeURIComponent(research.user_strategy_id)}/experiments`}>{research.name} · 查询原实验</Link><p>{experiment.launch_intent_id ?? '确认身份未提供'}</p><p>{experiment.job_id ? '运行暂不可读，未推断为失败或完成。' : '尚未登记运行，请查询原研究。'}</p><ErrorNote>{error}</ErrorNote></section>}
    </div>)}{records.value?.length === 0 && <p>暂无实验记录。</p>}
    <HoldoutLinks api={api} />
  </div>
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
    <PageHeader title={source.value ? `${source.value.owner.name} · 报告` : "冻结报告"} subtitle={<span title={id}>冻结报告 · {id.slice(0, 8)}{run?.kind === 'ready' ? ' · 已完成' : ''}</span>} breadcrumb={<Link to="/records" title={id}>回测记录 / {id.slice(0, 8)}</Link>} actions={<><button className="button" onClick={() => setRefresh(n => n + 1)}>重新读取报告</button>{run?.kind === 'ready' && <Link className="button" to={`/holdout/new/${encodeURIComponent(id)}`}>配置留出验证</Link>}{source.value && <Link className="button primary" to={`/research/${encodeURIComponent(source.value.owner.user_strategy_id)}/draft`}>继续研究</Link>}</>} />
    <Tabs active={tab} items={[{ id: 'overview', label: '概览' }, { id: 'trades', label: '交易' }, { id: 'inputs', label: '冻结输入' }, { id: 'files', label: '文件' }]} onChange={value => navigate(`/report/${encodeURIComponent(id)}/${value}`)} />
    {run?.kind === 'loading' && <p role="status">正在读取正式报告…</p>}{run?.kind === 'unavailable' && <ErrorNote>{run.note}</ErrorNote>}
    {run?.kind === 'ready' && <><FrozenReportFacts job={run.job} report={run.report} revision={source.value?.revision} />
      {tab === 'overview' && <ReportOverviewSurface metrics={<FrozenReportMetrics report={run.report} path={path.value} />} chart={<>{path.value ? <EquityCurve analysis={path.value} /> : <p>权益曲线未提供。</p>}<ErrorNote>{path.error}</ErrorNote></>} sources={<><dl className="report-sources"><div><dt>冻结版本</dt><dd>{source.value?.revision == null ? '未记录' : `r${source.value.revision}`}<span>{run.report.source?.strategy.id ?? run.report.scenario?.strategy_id ?? '未记录'}</span></dd></div><div><dt>数据版本</dt><dd>{run.job.dataset_version?.dataset_id ?? '注册场景冻结数据'}<span>{run.job.input_identity.record_count ?? '未记录'} 条</span></dd></div><div><dt>运行身份</dt><dd>{run.report.run_id}<span>{run.report.schema}</span></dd></div></dl><p className="notice">单次回测只反映冻结场景，不代表未来收益。</p>{source.value && <Link className="button" to={`/research/${encodeURIComponent(source.value.owner.user_strategy_id)}/experiments`}>{source.value.owner.name} · 返回研究</Link>}</>} />}
      {tab === 'inputs' && <><p className="notice">冻结输入只读。当前草稿的后续修改不会改变本报告。</p><FrozenReportDetails job={run.job} report={run.report} path={path.value} /></>}
      {tab === 'trades' && <><ErrorNote>{analytics.error}</ErrorNote><ReportTrades report={run.report} analytics={analytics.value} view="complete" /><ReportTrades report={run.report} analytics={analytics.value} view="orders" /></>}
      {tab === 'files' && <section className="panel"><h2>正式产物</h2>{(['report.json', 'summary.txt', 'equity-path.json'] as const).map(name => <p key={name}><a className="button" href={api.artifactUrl(id, name)} download><Icon name="download" />{name}</a></p>)}<p>报告指纹：{run.job.report_sha256 ?? '未提供'}</p></section>}
      <HoldoutLinks key={id} api={api} jobId={id} />
      {!['overview', 'inputs', 'trades', 'files'].includes(tab) && <Unavailable />}
    </>}
  </DetailSurface>

}
function Models({ api }: { api: ApiAdapter }) {
  return <div className="page model-connections"><PageHeader title="模型连接" subtitle="保存工作区连接，再到研究中选择并发送消息。" />
    <DesktopSettings api={api} section="model" onSaved={() => {}} />
  </div>
}

function DataRedirect() { const location = useLocation(); return <Navigate replace to={`/resources/data${location.search}${location.hash}`} /> }
function LegacyResearchRedirect() { const { id } = useParams(), location = useLocation(); return <Navigate replace to={id ? `/research/${encodeURIComponent(id)}/draft${location.search}` : '/'} /> }
function WorkspaceNav({ api }: { api: ApiAdapter }) {
  const location = useLocation(), [query, setQuery] = useState('')
  const recent = useRead(useCallback(() => /^\/(?:$|research\/|report\/|records$|resources\/|data$|strategies)/.test(location.pathname) ? api.listResearch!({ q: query, sort: 'updated' }) : Promise.resolve(null), [api, query, location.pathname]))
  const rows = recent.value?.items ?? []
  return <><aside className="sidebar" aria-label="工作区导航">
    <Link className="real-logo" to="/"><Logo /></Link>
    <div className="workspace-select"><Link className="button" to="/settings/workspace" aria-label="工作区位置"><Icon name="folder" /><span className="label">本地研究空间</span></Link></div>
    <Link className="button primary new-button" to="/" title="新建研究"><Icon name="plus" /><span>新建研究</span></Link>
    <label className="sidebar-search"><Icon name="search" /><input aria-label="搜索研究" placeholder="搜索研究" value={query} onChange={e => setQuery(e.target.value)} /></label>
    <div className="recent-heading">{query ? '搜索结果' : '最近研究'}</div>
    <div className="recent-list"><ErrorNote>{recent.error}</ErrorNote>{rows.slice(0, 5).map(item => <Link key={item.user_strategy_id} title={item.name} className={`recent-link ${location.pathname.includes(item.user_strategy_id) ? 'selected' : ''}`} to={`/research/${encodeURIComponent(item.user_strategy_id)}/overview`}><i className="recent-dot" /><span><strong>{item.name}</strong><small>草稿 r{item.draft.revision} · {item.draft.pending_proposals ? '待审阅' : item.draft.kind === 'intent' ? '待绑定' : '已绑定'}</small></span></Link>)}{recent.value && !rows.length && <small className="muted">{query ? '没有匹配研究' : '尚无研究'}</small>}</div>
    <div className="sidebar-footer"><Link className="sidebar-link" to="/resources/data" title="数据资源"><Icon name="data" /><span className="label">数据资源</span></Link><Link className="sidebar-link" to="/resources/models" title="模型连接"><Icon name="cube" /><span className="label">模型连接</span></Link><span className="demo-foot">本地研究候选 · 未发布</span></div>
  </aside><header className="topbar"><nav className="primary-nav" aria-label="主要导航">{[['/', '研究'], ['/simulations', '模拟运行'], ['/live', '实盘运行'], ['/records', '回测记录']].map(([path, label]) => <Link key={path} aria-current={(path === '/' ? location.pathname === '/' || location.pathname.startsWith('/research/') : path === '/records' ? /\/(records|report|holdout|candidates)/.test(location.pathname) : location.pathname === path) ? 'page' : undefined} to={path}>{label}{['/simulations', '/live'].includes(path) && <small>未接入</small>}</Link>)}</nav><span className="environment"><i className="recent-dot" />本地</span></header></>
}
export function StudioRoutes({ api }: { api: ApiAdapter }) {
  return <div className="studio real-studio" data-theme="light"><WorkspaceNav api={api} />
    <main className="main"><Routes><Route path="/" element={<Home api={api} />} /><Route path="/research/:id/:tab?" element={<ResearchRoute api={api} />} /><Route path="/strategies/:id?" element={<LegacyResearchRedirect />} /><Route path="/records" element={<Records api={api} />} /><Route path="/compare/:leftId/:rightId" element={<CompareBacktestsRoute api={api} studio />} /><Route path="/report/:id/:tab?" element={<ReportRoute api={api} />} /><Route path="/holdout/new/:jobId" element={<HoldoutCreatorRoute api={api} />} /><Route path="/holdout/:id" element={<HoldoutDetailRoute api={api} />} /><Route path="/candidates" element={<div className="page real-candidates"><CandidateHistory api={api} studio /></div>} /><Route path="/candidates/:candidateId" element={<div className="page real-candidates"><CandidateDetail api={api} studio /></div>} /><Route path="/data" element={<DataRedirect />} /><Route path="/resources/data" element={<div className="page"><h1>数据资源</h1><DataWorkspace api={api} /></div>} /><Route path="/resources/models" element={<Models api={api} />} /><Route path="/settings/workspace" element={<div className="page"><PageHeader title="工作区" subtitle="核对研究保存位置；更换目录在下次启动生效。" /><DesktopSettings api={api} section="workspace" onSaved={() => {}} /></div>} /><Route path="/settings/about" element={<div className="page"><PageHeader title="诊断与备份" /><DesktopAbout api={api} /></div>} /><Route path="*" element={<Unavailable />} /></Routes></main>
  </div>
}
export default function RealStudio() { return <BrowserRouter basename="/studio"><StudioRoutes api={browserApi} /></BrowserRouter> }
