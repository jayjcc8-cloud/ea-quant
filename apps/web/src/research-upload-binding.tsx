import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
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
export function ResearchUploadBinding({ api, target, datasetId }: {
  api: ApiAdapter; target: ImportTarget | null; datasetId: string
}) {
  const [strategy, setStrategy] = useState<UserStrategy | null>(null)
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
    if (targetId && api.getUserStrategy) api.getUserStrategy(targetId)
      .then(value => { if (active.current && value.user_strategy_id === targetId) setStrategy(value) })
      .catch(() => { if (active.current) setError('无法读取目标研究，请返回研究核对。') })
    return () => { active.current = false }
  }, [api, targetId]) // target is captured by the keyed upload panel.
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
  const choose = async () => {
    if (inFlight.current || pending || !target || !api.selectUserStrategyDataset || !strategy) return
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
        {strategy && stale && <p role="alert">目标草稿已改变，请返回研究后重新选择此已发布版本。</p>}
        <button type="button" className="primary" disabled={!strategy || stale || isIntentDraft(strategy.draft) || busy || Boolean(pending) || !api.selectUserStrategyDataset}
          onClick={() => { void choose() }}>用于当前研究</button></>}
    {pending && <button type="button" disabled={busy} onClick={() => { void reconcile() }}>核对绑定结果</button>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <p><Link to={target.returnTo}>返回当前研究</Link></p>
  </section>
}
