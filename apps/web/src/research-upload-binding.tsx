import type { ResearchSummary } from './research-workbench-api'
import { useEffect, useRef, useState } from 'react'
import { SourceLink as Link } from './workspace-navigation'
import type { ApiAdapter } from './app'
import { isIntentDraft, UserStrategyRequestError, type UserStrategy } from './user-strategies-api'

export type ImportTarget = { id: string; revision: number; returnTo: string }
export function importTarget(search: string): ImportTarget | null {
  const query = new URLSearchParams(search)
  const id = query.get('research') ?? ''
  const revision = Number(query.get('revision'))
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id) || !Number.isSafeInteger(revision) || revision < 1) return null
  const path = `/strategies/${encodeURIComponent(id)}`
  const requested = query.get('return') ?? ''
  return { id, revision, returnTo: requested === path || requested.startsWith(path + '?') ? requested : `${path}?tab=strategy` }
}

/** An uploaded version stays unbound until this exact, revision-bound choice. */
function BoundResearchUpload({ api, target, datasetId }: {
  api: ApiAdapter; target: ImportTarget | null; datasetId: string
}) {
  const [strategy, setStrategy] = useState<UserStrategy | null>(null)
  const [dataset, setDataset] = useState<{ venue?: string; symbol?: string } | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState(false)
  const [busy, setBusy] = useState(false)
  const key = `quine.csv-binding.${target?.id}.${target?.revision}.${datasetId}`
  const [pending, setPending] = useState<string | null>(() => {
    try { return sessionStorage.getItem(key) } catch { return null }
  })
  const active = useRef(false)
  const inFlight = useRef(false)
  const targetId = target?.id
  useEffect(() => {
    active.current = true
    void api.listDatasets?.().then(items => { if (active.current) setDataset(items.find(item => item.dataset_id === datasetId) ?? null) }).catch(() => { /* backend rechecks compatibility */ })
    if (targetId && api.getUserStrategy) api.getUserStrategy(targetId)
      .then(value => { if (active.current && value.user_strategy_id === targetId) setStrategy(value) })
      .catch(() => { if (active.current) setError('无法读取目标研究，请返回研究核对。') })
    return () => { active.current = false }
  }, [api, targetId, datasetId]) // target is captured by the keyed upload panel.
  const recordPending = (operation: string | null) => {
    setPending(operation)
    if (operation) sessionStorage.setItem(key, operation)
    else sessionStorage.removeItem(key)
  }
  const finish = (value: UserStrategy, operation: string) => {
    if (!target || value.user_strategy_id !== target.id || value.dataset?.operation_id !== operation ||
      value.dataset.dataset_id !== datasetId || isIntentDraft(value.draft) || value.draft.dataset_id !== datasetId) {
      throw new Error('尚未确认绑定结果。请返回目标研究核对。')
    }
    if (!active.current) return
    recordPending(null); setStrategy(value); setSuccess(true); setError('')
  }
  const incompatible = strategy && !isIntentDraft(strategy.draft) && dataset?.venue && dataset.symbol && (dataset.venue !== strategy.draft.venue || dataset.symbol !== strategy.draft.symbol)
  const choose = async () => {
    if (incompatible || inFlight.current || pending || !target || !api.selectUserStrategyDataset || !strategy) return
    inFlight.current = true; setBusy(true); setError('')
    const operation = `csv-${crypto.randomUUID()}`
    try {
      recordPending(operation)
      finish(await api.selectUserStrategyDataset(target.id, {
        operation_id: operation, base_revision: target.revision, dataset_id: datasetId,
      }), operation)
    } catch (caught) {
      if (!active.current) return
      if (caught instanceof UserStrategyRequestError && ['stale_base_revision', 'operation_conflict', 'user_strategy_invalid'].includes(caught.code)) {
        recordPending(null); setError(`未绑定：${caught.message}。已发布版本与导入输入已保留。`)
      } else setError('绑定结果未知。请核对结果，不会自动重发。')
    } finally { inFlight.current = false; if (active.current) setBusy(false) }
  }
  const reconcile = async () => {
    if (inFlight.current || !pending || !target || !api.getUserStrategy) return
    inFlight.current = true; setBusy(true)
    try { finish(await api.getUserStrategy(target.id), pending) }
    catch { if (active.current) setError('尚未确认绑定结果，请稍后核对；不会自动重发。') }
    finally { inFlight.current = false; if (active.current) setBusy(false) }
  }
  if (!target) return <p className="muted">已发布，尚未绑定研究。请打开目标研究并明确选择此版本。</p>
  const stale = strategy?.draft.revision !== target.revision
  return <section aria-label="用于当前研究">
    <p>{strategy ? `目标研究：${strategy.name} · 导入时修订 ${target.revision}` : '正在读取目标研究…'}</p>
    {success ? <p role="status">已用于当前研究，草稿修订 {strategy?.draft.revision}。旧预览已失效，尚未运行实验。</p>
      : <><p className="muted">已发布，尚未绑定。后端将校验此版本与整份草稿的兼容性。</p>
        {strategy && isIntentDraft(strategy.draft) && <p>此研究还只有想法。请返回当前研究，选择支持模板及这个已发布版本；无需重新导入。</p>}
        {strategy && stale && <p role="alert">目标草稿已改变，请返回研究后重新选择此已发布版本。</p>}
        {incompatible && <p role="alert">标的不兼容：数据为 {dataset?.venue}:{dataset?.symbol}，目标研究使用 {strategy && !isIntentDraft(strategy.draft) ? `${strategy.draft.venue}:${strategy.draft.symbol}` : '未绑定'}。不可绑定。</p>}
        <button type="button" className="primary" disabled={!!incompatible || !strategy || stale || isIntentDraft(strategy.draft) || busy || Boolean(pending) || !api.selectUserStrategyDataset}
          onClick={() => { void choose() }}>用于当前研究</button></>}
    {pending && <button type="button" disabled={busy} onClick={() => { void reconcile() }}>核对绑定结果</button>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <p><Link to={target.returnTo}>返回当前研究</Link></p>
  </section>
}

/** One target picker for both a library dataset and a newly published version. */
export function ResearchUploadBinding({ api, target, datasetId }: { api: ApiAdapter; target: ImportTarget | null; datasetId: string }) {
  const [strategies, setStrategies] = useState<ResearchSummary[] | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState('')
  useEffect(() => {
    let active = true
    if (!target && api.listResearch) void api.listResearch().then(collection => { if (active) setStrategies(collection.items) }).catch(() => { if (active) setError('研究列表不可读，请返回研究检查。') })
    return () => { active = false }
  }, [api, target])
  const item = strategies?.find(value => value.user_strategy_id === selected)
  const chosen = target ?? (item ? { id: item.user_strategy_id, revision: item.draft.revision, returnTo: `/strategies/${encodeURIComponent(item.user_strategy_id)}?tab=strategy` } : null)
  return <section className="dataset-binding-picker">
    {!target && <><h3>选择研究并绑定</h3><p>只修改目标的可编辑草稿，冻结报告不变。</p><label>目标研究<select value={selected} onChange={e => setSelected(e.target.value)}><option value="">请选择目标研究</option>{strategies?.map(value => <option value={value.user_strategy_id} key={value.user_strategy_id}>{value.name} · 草稿 v{value.draft.revision} · {value.exchange_id ?? '未指定交易所'}</option>)}</select></label>{error && <p role="alert">{error}</p>}<Link to="/strategies" state={{ createStrategy: true }}>新建研究</Link></>}
    {chosen && <BoundResearchUpload key={`${chosen.id}.${chosen.revision}.${datasetId}`} api={api} target={chosen} datasetId={datasetId} />}
  </section>
}
