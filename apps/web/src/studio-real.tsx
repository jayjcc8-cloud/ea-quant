import { commissionLabel, latencyLabel, slippageLabel } from './execution-assumptions'
import { currentProposalValue, detailNextStep, experimentStatus, holdsExperiment, summaryNextStep } from './studio-journey'
import { Assistant } from './studio-assistant'
import { WorkspaceNav } from './studio-navigation'
import { reason, saved, useRead } from './studio-read'
import { PaperWorkspace } from './paper-workspace'
import { paperApi, type PaperApi } from './paper-api'
import { CompareBacktestsRoute } from './backtest-comparison'
/** v3's first real slice. No scenario adapter, fixture state or synthetic outcome. */
import { useCallback, useEffect, useRef, useState } from 'react'
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
import { AgentSurface, DetailSurface, DraftSurface, ReportOverviewSurface, PageHeader, Tabs, ErrorNote, Icon } from '../studio/src/layout'
import '../studio/src/report-overview.css'
import '../studio/src/model-connections.css'

// Only these host refusals are proven to precede persistence/dispatch on launch.
// A generic HTTP error (including an owner error) still has an uncertain outcome.
const beforeDispatch = (error: unknown) => error instanceof UserStrategyRequestError
  && ['stale_base_revision', 'approval_required', 'user_strategy_not_found'].includes(error.code)
const pendingKey = (id: string) => `quine.studio.confirm.${id}`

function fact(value: unknown): string {
  if (value === null || value === undefined) return '未记录'
  if (typeof value === 'object') return Object.entries(value).map(([key, entry]) => `${key}: ${fact(entry)}`).join(' · ') || '空声明'
  return String(value)
}
function costFact(kind: 'commission' | 'slippage' | 'latency', value: unknown): string {
  if (value === null) return kind === 'commission' ? commissionLabel(null) : kind === 'slippage' ? slippageLabel(null) : latencyLabel(null)
  if (!value || typeof value !== 'object') return fact(value)
  const item = value as Record<string, unknown>
  if (typeof item.policy !== 'string') return fact(value)
  if (kind === 'commission' && (typeof item.commission_bps === 'string' || typeof item.commission_bps === 'number')) return commissionLabel({ policy: item.policy, commission_bps: String(item.commission_bps) })
  if (kind === 'slippage' && (typeof item.slippage_bps === 'string' || typeof item.slippage_bps === 'number')) return slippageLabel({ policy: item.policy, slippage_bps: String(item.slippage_bps) })
  if (kind === 'latency' && typeof item.latency_ms === 'number') return latencyLabel({ policy: item.policy, latency_ms: item.latency_ms })
  return fact(value)
}
function Facts({ items }: { items: [string, unknown][] }) { return <dl className="key-values">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{fact(value)}</dd></div>)}</dl> }
function Unavailable() { return <div className="page"><h1>此候选尚未接入</h1><p>实盘、批量与候选晋级暂不可用。</p><Link to="/">返回研究</Link></div> }

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
    {list.value?.items.map(item => {
      const next = summaryNextStep(item)
      return <Link className="real-research-row" key={item.user_strategy_id} to={next.href}>
        <span><strong>{item.name}</strong><small>{item.intent}</small><small>草稿 r{item.draft.revision} · 最近保存 <time dateTime={item.updated_at}>{item.updated_at.replace('T', ' ').slice(0, 16)}</time></small></span>
        <span className="real-research-state"><strong>{next.label}<Icon name="arrow" size={16} /></strong><small>{next.detail}</small></span>
      </Link>
    })}{list.value?.items.length === 0 && <p className="muted">工作区尚无研究。保存一个问题开始。</p>}
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
  const draft = strategy.draft, next = detailNextStep(strategy)
  const canPrepare = !isIntentDraft(draft) && !strategy.experiments.some(holdsExperiment)
  const latest = strategy.experiments.at(-1), pendingProposals = strategy.proposals.filter(item => item.status === 'open')
  if (tab === 'confirm') return <div className="page"><PageHeader title="确认实验输入" subtitle="核对冻结输入后，明确启动一次本地引擎实验。" breadcrumb={<Link to={`/research/${encodeURIComponent(id)}/draft`}>{strategy.name} / 新建实验</Link>} /><Confirmation key={`${id}.${draft.revision}`} api={api} strategy={strategy} changed={changed} done={() => navigate(`/research/${encodeURIComponent(id)}/experiments`)} /></div>
  return <DetailSurface className="real-detail" assistant={<Assistant key={id} api={api} strategy={strategy} changed={changed} />}>
    <PageHeader title={strategy.name} subtitle={`草稿 r${draft.revision} · ${isIntentDraft(draft) ? '等待绑定策略与数据' : `${draft.venue}:${draft.symbol}`}`} breadcrumb={<Link to="/">研究 / {strategy.name}</Link>} actions={tab === 'experiments' && canPrepare && <Link className="button primary" to={`/research/${encodeURIComponent(id)}/confirm`}>准备一次实验</Link>} />
    <nav className="tabs" aria-label="页面标签">{[['overview', '概览'], ['draft', '策略草稿'], ['experiments', '实验']].map(([value, label]) => <Link key={value} to={`/research/${encodeURIComponent(id)}/${value}`} aria-current={tab === value ? 'page' : undefined}>{label}</Link>)}</nav>
    <ErrorNote>{error}</ErrorNote>
    {tab === 'overview' && <>
      <h2>研究问题</h2><section className="panel"><p>{strategy.intent}</p></section>
      <section className="panel research-next-step" aria-label="研究下一步"><h2>下一步</h2><p>{next.detail}</p>
        <div className="button-row"><Link className="button primary" to={next.href}>{next.label}</Link>
          {canPrepare && next.kind !== 'experiment' && <Link className="button" to={`/research/${encodeURIComponent(id)}/confirm`}>用当前草稿准备新实验</Link>}
          {pendingProposals.length > 0 && next.kind !== 'review' && <Link className="button" to={`/research/${encodeURIComponent(id)}/proposal`}>审阅 {pendingProposals.length} 项提案</Link>}
        </div>
      </section>
      <h2>研究进度</h2><ol className="plan-steps research-progress">
        <li><div><strong>策略与数据 · {isIntentDraft(draft) ? '待绑定' : '已绑定'}</strong><p>{isIntentDraft(draft) ? '选择已注册策略，绑定本次使用的数据。' : '已保存来源；修改绑定后需重新核对输入。'}</p></div></li>
        <li><div><strong>当前草稿 · r{draft.revision}</strong><p>{pendingProposals.length ? `${pendingProposals.length} 项提案待审阅；尚未应用到当前草稿。` : '修改参数只保存草稿，实验需要独立确认。'}</p></div></li>
        <li><div><strong>实验 · {latest ? experimentStatus(latest.status) : '尚未运行'}</strong><p>{latest ? `最近记录冻结草稿 r${latest.payload.draft_revision}。${latest.status === 'created' && latest.report_ready ? '正式报告可读。' : '尚未确认可读的正式报告。'}` : '核对预览后，明确启动一次本地实验。'}</p></div></li>
      </ol>
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
  const proposal = proposalId ? strategy.proposals.find(item => item.proposal_id === proposalId) : strategy.proposals.find(item => item.status === 'open')
  const stale = !!proposal && (proposal.base_revision !== strategy.draft.revision || proposal.stale_paths.length > 0)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), sending = useRef(false), navigate = useNavigate()
  const decide = async (apply: boolean) => {
    if (!proposal || sending.current || proposal.status !== 'open' || (apply && (stale || !api.applyUserStrategyProposal)) || (!apply && !api.rejectUserStrategyProposal)) return
    sending.current = true; setBusy(true); setError('')
    try {
      const next = apply ? await api.applyUserStrategyProposal!(strategy.user_strategy_id, proposal.proposal_id, { operation_id: `apply-${crypto.randomUUID()}`, paths: proposal.items.map(item => item.path) }) : await api.rejectUserStrategyProposal!(strategy.user_strategy_id, proposal.proposal_id)
      if (next.user_strategy_id !== strategy.user_strategy_id) throw new Error('研究身份不匹配')
      changed(next); navigate(`/research/${encodeURIComponent(strategy.user_strategy_id)}/draft`)
    } catch (e) { setError(`${reason(e)}。请重新读取研究核对结果，不会自动重发。`) }
    finally { sending.current = false; setBusy(false) }
  }
  if (!proposal) return <section className="panel"><p>{proposalId ? '未读取到此提案，请核对提案身份。' : '当前没有待审阅提案。'}</p><Link className="button" to={`/research/${encodeURIComponent(strategy.user_strategy_id)}/draft`}>返回当前草稿</Link></section>
  return <><h2>审阅修改提案</h2><p>基于草稿 r{proposal.base_revision} · {{ open: '待审阅', applied: '已应用', rejected: '已拒绝', superseded: '已被替代' }[proposal.status]}</p>
    {strategy.proposals.filter(item => item.status === 'open').length > 1 && <nav className="button-row" aria-label="待审提案">{strategy.proposals.filter(item => item.status === 'open').map((item, index) => <Link className="button" key={item.proposal_id} aria-current={item.proposal_id === proposal.proposal_id ? 'page' : undefined} to={`/research/${encodeURIComponent(strategy.user_strategy_id)}/proposal?proposal=${encodeURIComponent(item.proposal_id)}`}>提案 {index + 1} · r{item.base_revision}</Link>)}</nav>}
    {stale && proposal.status === 'open' && <p className="notice proposal-conflict" role="status">{proposal.base_revision !== strategy.draft.revision ? `草稿已从 r${proposal.base_revision} 更新至 r${strategy.draft.revision}，此提案不能直接应用。` : '提案包含已失效字段，不能直接应用。'} 可拒绝此提案，回到当前草稿继续检查或请求新建议。</p>}
    <div className="table-scroll"><table><thead><tr><th>参数</th><th>提案基线 r{proposal.base_revision}</th><th>提案建议</th><th>当前草稿 r{strategy.draft.revision}</th></tr></thead><tbody>{proposal.items.map(item => <tr key={item.path}><td>{item.path}<small className="muted">{item.reason}</small></td><td>{fact(item.before)}</td><td className="diff-change">{fact(item.after)}</td><td>{fact(currentProposalValue(strategy.draft, item.path))}</td></tr>)}</tbody></table></div>
    <p className="notice">应用仅保存草稿的新修订，不会运行实验。提案中的解释属于模型推断。</p><ErrorNote>{error}</ErrorNote>
    <div className="action-footer"><Link className="button" to={`/research/${encodeURIComponent(strategy.user_strategy_id)}/draft`}>返回草稿</Link><div className="button-row"><button className="button" disabled={busy || proposal.status !== 'open' || !!error || !api.rejectUserStrategyProposal} onClick={() => void decide(false)}>拒绝提案</button><button className="button primary" disabled={busy || proposal.status !== 'open' || !!error || stale || !api.applyUserStrategyProposal} onClick={() => void decide(true)}>应用修改到草稿</button></div></div>
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
  const blocked = strategy.experiments.some(holdsExperiment), next = detailNextStep(strategy)
  return <><div className="section-heading"><h2>策略草稿 · <span className="status">r{revision}</span></h2><Link className="button primary" to={blocked ? next.href : `/research/${encodeURIComponent(id)}/confirm`}>{blocked ? next.label : '准备一次实验'}</Link></div>
    <DraftSurface source={<><Facts items={[
      ['策略模板', draft.strategy_id], ['数据', draft.dataset_id ?? draft.scenario_id], ['标的', `${draft.venue}:${draft.symbol}`],
      ['样本区间（UTC）', source.value?.data?.start_utc && source.value.data.end_utc ? `${source.value.data.start_utc} — ${source.value.data.end_utc}` : '尚未读到'],
    ]} /><ErrorNote>{source.error}</ErrorNote></>}
      parameters={<DraftEditor presentation="studio" api={api} strategy={strategy} changed={changed} />}
      costs={<><Facts items={[[ '手续费', costFact('commission', source.value?.commission) ], ['滑点', costFact('slippage', source.value?.slippage)], ['延迟', costFact('latency', source.value?.latency)]]} /><p className="muted">每 1 bps 等于 0.01%。成本由所选场景提供，当前只读。保存草稿后，进入独立确认页核对本次执行。</p></>} />
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
      : isIntentDraft(strategy.draft) ? <section className="panel"><p>请先绑定策略与数据，再核对实验输入。</p><Link className="button primary" to={`/research/${encodeURIComponent(id)}/draft`}>前往绑定策略与数据</Link></section>
        : preview ? <><div className="two-column">
          <section className="panel"><h2>来源、参数与成本</h2><Facts items={[
            ['冻结草稿', `r${preview.draft_revision}`], ['策略', preview.strategy_id], ['场景', preview.scenario_id], ['标的', `${preview.venue}:${preview.symbol}`], ['记录数', preview.record_count], ['初始资金', preview.initial_cash], ['参数', preview.parameters], ['数据起点（UTC）', preview.data?.start_utc], ['数据终点（UTC）', preview.data?.end_utc], ['手续费', costFact('commission', preview.commission)], ['滑点', costFact('slippage', preview.slippage)], ['延迟', costFact('latency', preview.latency)],
          ]} /></section>
          <section className="panel"><h2>本次执行</h2><Facts items={[[ '实验数', 1 ], ['模式', '本地真实引擎'], ['模型调用', 0]]} /><hr /><h2>范围与限制</h2><p>每 1 bps 等于 0.01%。执行成本使用本次冻结假设。</p><p>数据性质以冻结来源声明为准。短合成样例仅验证流程，不用于判断策略收益。</p><p>这次运行只使用已核对的输入。后续修改草稿不会改变报告。</p></section>
        </div><details><summary>核对原始冻结输入与身份</summary><pre>{JSON.stringify(preview.normalized, null, 2)}</pre><pre>{JSON.stringify(preview.input_identity, null, 2)}</pre><small>预览身份：{preview.preview_identity} · 执行所有者：{preview.owner}</small></details>
          <div className="action-footer"><Link className="button" to={`/research/${encodeURIComponent(id)}/draft`}>返回修改</Link><button className="button primary" disabled={busy} onClick={() => void confirm()}>确认并运行一次</button></div></> : !error && <p role="status">正在读取真实预览…</p>}
  </section>

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
export function StudioRoutes({ api, paper = paperApi }: { api: ApiAdapter; paper?: PaperApi }) {
  return <div className="studio real-studio" data-theme="light"><WorkspaceNav api={api} />
    <main className="main"><Routes><Route path="/" element={<Home api={api} />} /><Route path="/research/:id/:tab?" element={<ResearchRoute api={api} />} /><Route path="/strategies/:id?" element={<LegacyResearchRedirect />} /><Route path="/simulations" element={<PaperWorkspace api={paper} studio />} /><Route path="/records" element={<Records api={api} />} /><Route path="/compare/:leftId/:rightId" element={<CompareBacktestsRoute api={api} studio />} /><Route path="/report/:id/:tab?" element={<ReportRoute api={api} />} /><Route path="/holdout/new/:jobId" element={<HoldoutCreatorRoute api={api} />} /><Route path="/holdout/:id" element={<HoldoutDetailRoute api={api} />} /><Route path="/candidates" element={<div className="page real-candidates"><CandidateHistory api={api} studio /></div>} /><Route path="/candidates/:candidateId" element={<div className="page real-candidates"><CandidateDetail api={api} studio /></div>} /><Route path="/data" element={<DataRedirect />} /><Route path="/resources/data" element={<div className="page"><h1>数据资源</h1><DataWorkspace api={api} /></div>} /><Route path="/resources/models" element={<Models api={api} />} /><Route path="/settings/workspace" element={<div className="page"><PageHeader title="工作区" subtitle="核对研究保存位置；更换目录在下次启动生效。" /><DesktopSettings api={api} section="workspace" onSaved={() => {}} /></div>} /><Route path="/settings/about" element={<div className="page"><PageHeader title="诊断与备份" /><DesktopAbout api={api} /></div>} /><Route path="*" element={<Unavailable />} /></Routes></main>
  </div>
}
export default function RealStudio() { return <BrowserRouter basename="/studio"><StudioRoutes api={browserApi} /></BrowserRouter> }
