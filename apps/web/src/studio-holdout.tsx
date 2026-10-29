/** Real v3 chronological holdout. The existing owner supplies all eligibility,
 * frozen inputs and relationships; this module only selects, confirms and reads. */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, ChronologicalHoldout, ScenarioSummary } from './app'
import { useRunRead } from './user-strategy-research'
import { PageHeader, Icon } from '../studio/src/layout'

const reason = (error: unknown) => error instanceof Error ? error.message : '读取失败'
const reportUrl = (id: string) => `/report/${encodeURIComponent(id)}/overview`
const targetKey = (item: ScenarioSummary) => `${item.scenario_id}|${item.input_identity?.dataset_id ?? ''}`
const pendingKey = (id: string) => `quine.studio.holdout.${id}`
function Note({ children }: { children: ReactNode }) { return children ? <p className="notice error" role="alert">{children}</p> : null }
function Facts({ items }: { items: [string, ReactNode][] }) { return <dl className="key-values">{items.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value ?? '未记录'}</dd></div>)}</dl> }
function Window({ data }: { data?: { start_utc: string; end_utc: string } }) { return <>{data ? `${data.start_utc} — ${data.end_utc}` : '区间未记录'}</> }
function SourceFacts({ job }: { job: BacktestJob }) {
  const input = job.input_snapshot?.scenario
  return <Facts items={[
    ['来源运行', job.job_id], ['策略', input?.strategy.id],
    ['冻结参数', Object.entries(input?.strategy.parameters ?? job.parameters ?? {}).map(([key, value]) => `${key}: ${value}`).join(' · ') || '未记录'],
    ['初始资金', input?.funding && `${input.funding.initial_cash} ${input.funding.currency}`],
    ['执行与成本', input?.execution ? <pre>{JSON.stringify(input.execution, null, 2)}</pre> : '未记录'],
    ['数据指纹', job.input_identity.data_sha256],
  ]} />
}
function TargetFacts({ target }: { target: ScenarioSummary }) {
  return <Facts items={[
    ['留出输入', target.name], ['注册场景', target.scenario_id], ['数据版本', target.input_identity?.dataset_id ?? '注册场景数据'],
    ['区间（UTC）', <Window data={target.summary?.data} />], ['输入指纹', target.input_identity?.scenario_sha256],
    ['数据指纹', target.input_identity?.data_sha256], ['源文件指纹', target.input_identity?.source_sha256 ?? '未提供'], ['记录数', target.input_identity?.record_count],
  ]} />
}
function Relations({ items }: { items: ChronologicalHoldout[] }) {
  return <>{items.map(item => <Link className="real-research-row" key={item.validation_id} to={`/holdout/${encodeURIComponent(item.validation_id)}`}><span><strong>查看留出 {item.validation_id}</strong><small>{item.source_job_id} → {item.holdout_job_id}</small></span><small>{item.created_at}</small></Link>)}</>
}
export function HoldoutLinks({ api, jobId }: { api: ApiAdapter; jobId?: string }) {
  const [items, setItems] = useState<ChronologicalHoldout[] | null>(null), [error, setError] = useState(''), [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    void api.listHoldouts().then(rows => { if (active) { setItems(rows.filter(row => !jobId || row.source_job_id === jobId || row.holdout_job_id === jobId)); setError('') } }).catch(e => { if (active) setError(reason(e)) })
    return () => { active = false }
  }, [api, jobId, refresh])
  return <section aria-label="留出记录"><div className="section-heading"><h2>留出记录</h2><button className="button" onClick={() => setRefresh(n => n + 1)}>重新读取留出</button></div><Note>{error}</Note>{items && <Relations items={items} />}{items?.length === 0 && <p className="muted">尚无留出记录。</p>}</section>
}

export function HoldoutCreatorRoute({ api }: { api: ApiAdapter }) { const { jobId = '' } = useParams(); return <HoldoutCreator key={jobId} api={api} jobId={jobId} /> }
function HoldoutCreator({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  const source = useRunRead(api, jobId), ready = source?.kind === 'ready'
  const [catalog, setCatalog] = useState<ScenarioSummary[] | null>(null), [relations, setRelations] = useState<ChronologicalHoldout[]>([])
  const [selected, setSelected] = useState(''), [confirming, setConfirming] = useState(false), [refresh, setRefresh] = useState(0)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  // This is a pending UI receipt, never a result. As in experiment confirmation,
  // retain it before sending so remount cannot turn a lost reply into a retry.
  const [uncertain, setUncertain] = useState(() => { try { return !!sessionStorage.getItem(pendingKey(jobId)) } catch { return true } })
  const sending = useRef(false), navigate = useNavigate(), heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (confirming) heading.current?.focus() }, [confirming])
  useEffect(() => {
    if (!ready) return
    let active = true
    // Reopen always reads saved relations before enabling another selection.
    void Promise.all([api.holdoutScenarios(jobId), api.listHoldouts()]).then(([offers, rows]) => {
      if (active) { setCatalog(offers); setRelations(rows.filter(row => row.source_job_id === jobId)) }
    }).catch(e => { if (active) setError(reason(e)) })
    return () => { active = false }
  }, [api, jobId, ready, refresh])
  const target = catalog?.find(item => targetKey(item) === selected)
  const query = async () => {
    if (sending.current) return
    setBusy(true)
    try {
      const rows = (await api.listHoldouts()).filter(row => row.source_job_id === jobId)
      setRelations(rows)
      setError(rows.length ? '已读到来源的留出记录，请打开核对目标与状态；不会重新提交。' : '尚未读到关联，这不代表未执行。保留未知结果，稍后再次查询。')
    } catch (e) { setError(`关系暂不可读：${reason(e)}。保留未知结果，不会重新提交。`) }
    finally { setBusy(false) }
  }
  const submit = async () => {
    if (sending.current || uncertain || !confirming || !ready || !target?.input_identity) return
    try { sessionStorage.setItem(pendingKey(jobId), 'pending') }
    catch { setError('无法保留确认状态，尚未发送。请恢复本机存储后再确认。'); return }
    sending.current = true; setBusy(true); setUncertain(true); setError('')
    try {
      const identity = target.input_identity
      const relation = await api.createHoldout({ source_job_id: jobId, scenario_id: target.scenario_id, input_identity: identity,
        ...(identity.dataset_id ? { dataset_id: identity.dataset_id, source_sha256: identity.source_sha256 } : {}) })
      sessionStorage.removeItem(pendingKey(jobId))
      navigate(`/holdout/${encodeURIComponent(relation.validation_id)}`)
    } catch (e) {
      // Only this existing input refusal is proven to precede owner confirmation.
      if (e instanceof Error && 'code' in e && e.code === 'input_conflict') {
        sessionStorage.removeItem(pendingKey(jobId)); setUncertain(false); setConfirming(false); setSelected(''); setCatalog(null)
        setError(`输入已变化，请重新读取并重新选择后确认。${reason(e)}`)
      } else { setError(`确认结果未知：${reason(e)}。请查询已有留出，不会自动重发。`) }
    } finally { sending.current = false; setBusy(false) }
  }
  return <div className="page real-holdout"><PageHeader title="留出验证" subtitle="使用来源报告的冻结配置，执行一次严格更晚的独立实验。" breadcrumb={<Link to={reportUrl(jobId)}>来源报告 / 留出验证</Link>} />
    {source?.kind === 'loading' && <p role="status">正在读取来源报告…</p>}
    {source?.kind === 'unavailable' && <Note>{source.note}</Note>}
    {ready && <>
      <div className="window-bar"><div><strong>来源区间（UTC）</strong><small><Window data={source.job.input_snapshot?.scenario.data} /></small></div><Icon name="lock" /><div><strong>留出区间（UTC）</strong><small>{target ? <Window data={target.summary?.data} /> : '尚未选择'}</small></div></div>
      <p className="muted">仅列出服务端确认兼容且严格更晚的输入。时间先后不代表未见数据或泛化能力。</p>
      {relations.length > 0 && <section className="panel"><h2>来源的已有留出</h2><Relations items={relations} /></section>}
      <div className="two-column"><section className="panel"><h2>来源与冻结参数</h2><SourceFacts job={source.job} /><Link to={reportUrl(jobId)}>打开来源报告</Link></section>
        <section className="panel"><h2 ref={heading} tabIndex={-1}>{confirming ? '确认留出验证' : '留出数据'}</h2>
          {!confirming && !uncertain && <><label className="field" htmlFor="holdout-input">留出输入<select id="holdout-input" value={selected} disabled={!catalog} onChange={event => setSelected(event.target.value)}><option value="">选择兼容且严格更晚的输入</option>{catalog?.map(item => <option key={targetKey(item)} value={targetKey(item)}>{item.name}</option>)}</select></label>
            {catalog?.length === 0 && <p>没有兼容且严格更晚的输入。</p>}<button className="button" onClick={() => { setCatalog(null); setSelected(''); setError(''); setRefresh(n => n + 1) }}>重新读取输入</button></>}
          {target && <TargetFacts target={target} />}{target && !target.input_identity && <Note>输入身份未提供，不能确认。请重新读取输入。</Note>}
          <p className="notice">策略、参数、资金与成本保持来源冻结值。原报告不变；候选晋级、模拟、实盘与批量尚未接入。</p>
          <Note>{error}</Note>
          {uncertain ? <><p role="status">{busy ? '正在等待运行所有者响应…' : '这次确认只查询已有记录，不再提交。'}</p><button className="button" disabled={busy} onClick={() => void query()}>查询已有留出</button></>
            : <div className="button-row">{confirming ? <><button className="button" onClick={() => { setConfirming(false); setError('') }}>返回选择</button><button className="button primary" disabled={busy} onClick={() => void submit()}>确认并运行留出</button></>
              : <button className="button primary" disabled={!target?.input_identity} onClick={() => { setConfirming(true); setError('') }}>审阅冻结配置</button>}</div>}
        </section></div>
    </>}
  </div>
}

function RunEvidence({ api, id, label, refresh }: { api: ApiAdapter; id: string; label: string; refresh: number }) {
  const run = useRunRead(api, id, String(refresh))
  return <section className="panel" aria-label={label}><h2>{label}</h2><p>{id}</p>
    {run?.kind === 'loading' && <p role="status">正在读取运行…</p>}{run?.kind === 'unavailable' && <Note>{run.note}</Note>}
    {run?.kind === 'ready' && <><p className="status">succeeded · 正式报告可读</p><p><Window data={run.job.input_snapshot?.scenario.data} /></p>
      <Facts items={[
        ['净盈亏', `${run.report.economics.net_pnl.amount} ${run.report.economics.net_pnl.currency ?? run.report.economics.currency ?? '币种未记录'}`],
        ['期末权益', `${run.report.economics.equity.amount} ${run.report.economics.equity.currency ?? run.report.economics.currency ?? '币种未记录'}`],
        ['手续费', run.report.economics.fees ? `${run.report.economics.fees.amount} ${run.report.economics.fees.currency}` : '未提供'],
        ['数据指纹', run.job.input_identity.data_sha256], ['报告指纹', run.job.report_sha256],
      ]} /><Link className="button" to={reportUrl(id)}>打开{label}</Link></>}
  </section>
}
export function HoldoutDetailRoute({ api }: { api: ApiAdapter }) { const { id = '' } = useParams(); return <HoldoutDetail key={id} api={api} id={id} /> }
function HoldoutDetail({ api, id }: { api: ApiAdapter; id: string }) {
  const [relation, setRelation] = useState<ChronologicalHoldout | null>(null), [error, setError] = useState(''), [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    void api.getHoldout(id).then(row => { if (active) { setRelation(row); setError('') } }).catch(e => { if (active) setError(reason(e)) })
    return () => { active = false }
  }, [api, id, refresh])
  return <div className="page real-holdout"><PageHeader title="留出验证" subtitle={id} breadcrumb={<Link to="/records">回测记录 / 留出</Link>} actions={<button className="button" onClick={() => setRefresh(n => n + 1)}>重新读取状态</button>} />
    <Note>{error}</Note>{relation && <><p>关联创建于 {relation.created_at}；结果以各自运行及正式报告为准。</p><div className="two-column"><RunEvidence api={api} id={relation.source_job_id} label="来源报告" refresh={refresh} /><RunEvidence api={api} id={relation.holdout_job_id} label="留出报告" refresh={refresh} /></div><p className="notice">两次运行使用来源冻结的策略、参数、资金与成本。时间先后不代表验证通过；候选晋级尚未接入。</p></>}
  </div>
}
