import { useEffect, useRef, useState } from 'react'
import type { ApiAdapter } from './app'
import { DraftEditorForm } from './draft-editor-form'
import { UserStrategyRequestError, type DraftContract, type SaveDraftRequest, type UserStrategy } from './user-strategies-api'

type Buffer = { revision: number; values: Record<string, string>; pending?: SaveDraftRequest }
const bufferKey = (id: string) => `quine.manual-draft.${id}`
function readBuffer(id: string): Buffer | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(bufferKey(id)) ?? 'null') as Buffer | null
    return value && Number.isInteger(value.revision) && value.values &&
      Object.values(value.values).every(item => typeof item === 'string') ? value : null
  } catch { return null }
}

/** Local, per-research unsaved input only. The server record remains authoritative. */
export function DraftEditor({ api, strategy, changed }: {
  api: ApiAdapter; strategy: UserStrategy; changed: (value: UserStrategy) => void
}) {
  const id = strategy.user_strategy_id
  const [buffer, setBuffer] = useState<Buffer | null>(() => readBuffer(id))
  const [contract, setContract] = useState<DraftContract | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [retained, setRetained] = useState(true)
  const inFlight = useRef(false)
  const alive = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    let active = true
    api.userStrategyDraftContract?.(id).then(value => { if (active) setContract(value) })
      .catch(() => { if (active) setError('无法读取已注册字段，请重新打开此研究。') })
    return () => { active = false }
  }, [api, id, strategy.draft.revision])
  const persist = (next: Buffer | null) => {
    setBuffer(next)
    try {
      if (next) sessionStorage.setItem(bufferKey(id), JSON.stringify(next))
      else sessionStorage.removeItem(bufferKey(id))
      setRetained(true); return true
    } catch { setRetained(false); return false }
  }
  useEffect(() => {
    if (!buffer || retained) return
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    const leave = (event: MouseEvent) => {
      if ((event.target as Element).closest?.('a[href]') && !window.confirm('本地缓冲不可用，离开将丢失未保存输入。放弃并离开？')) {
        event.preventDefault(); event.stopPropagation()
      }
    }
    window.addEventListener('beforeunload', unload)
    document.addEventListener('click', leave, true)
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', leave, true) }
  }, [buffer, retained])
  const finish = (next: UserStrategy, operation: string) => {
    if (next.user_strategy_id !== id || !next.changes.some(item => item.operation_id === operation && item.kind === 'manual')) {
      throw new Error('保存结果未包含本次修订，请核对后继续。')
    }
    if (readBuffer(id)?.pending?.operation_id === operation) sessionStorage.removeItem(bufferKey(id))
    if (!alive.current) return
    persist(null); setError(null); setNotice(`已保存为修订 ${next.draft.revision}。旧预览已失效。`); changed(next)
  }
  const reconcile = async () => {
    if (inFlight.current || !buffer?.pending || !api.getUserStrategy) return
    inFlight.current = true; setBusy(true)
    try { finish(await api.getUserStrategy(id), buffer.pending.operation_id) }
    catch { if (alive.current) setError('尚未确认本次保存结果，输入已保留。不会自动重发，请稍后再次核对。') }
    finally { inFlight.current = false; if (alive.current) setBusy(false) }
  }
  const stale = Boolean(buffer && buffer.revision !== strategy.draft.revision)
  const save = async () => {
    if (inFlight.current || !buffer || buffer.pending || stale || !contract || !api.saveUserStrategyDraft) return
    const values: Record<string, string | number> = {}
    for (const [path, raw] of Object.entries(buffer.values)) {
      const field = contract.fields.find(item => item.path === path && item.editable)
      if (!field) { setError('字段合同已改变，请取消编辑后重新输入。'); return }
      if (field.json_type === 'integer') {
        if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) { setError(`${path} 必须是整数。`); return }
        values[path] = Number(raw)
      } else values[path] = raw
    }
    const request = { operation_id: `manual-${crypto.randomUUID()}`, expected_revision: buffer.revision, values }
    inFlight.current = true; setBusy(true); setError(null)
    if (!persist({ ...buffer, pending: request })) {
      // No request was sent: keep the editable draft instead of an unresolvable pending identity.
      setBuffer(buffer)
      inFlight.current = false; setBusy(false)
      setError('无法保留提交身份，本次未发送。请恢复本地存储后再保存。')
      return
    }
    try { finish(await api.saveUserStrategyDraft(id, request), request.operation_id) }
    catch (caught) {
      if (!alive.current) return
      const known = caught instanceof UserStrategyRequestError && ['stale_base_revision', 'operation_conflict', 'user_strategy_invalid', 'request_invalid'].includes(caught.code)
      if (known) persist({ revision: buffer.revision, values: buffer.values })
      setError(known ? `${(caught as Error).message}；输入已保留，请核对当前修订。` : '保存结果未知，输入已保留。请核对结果；不会自动重发。')
    } finally { inFlight.current = false; if (alive.current) setBusy(false) }
  }
  if (!api.userStrategyDraftContract || !api.saveUserStrategyDraft) return <p className="muted">此工作区不提供手工保存入口。</p>
  const currentContract = contract?.draft_revision === strategy.draft.revision ? contract : null
  return <>
    <DraftEditorForm fields={currentContract?.fields ?? []} values={buffer?.values ?? {}}
      revision={buffer?.revision ?? strategy.draft.revision} busy={busy}
      disabled={!currentContract || Boolean(buffer?.pending)} dirty={Boolean(buffer)}
      error={stale ? '草稿已有新修订。输入仍保留，请取消旧编辑并基于当前修订重新修改。' : error}
      notice={!retained ? '本地缓冲不可用，离开前请保存或取消。' : buffer ? '未保存输入已在本机此标签页缓冲，刷新或返回本研究后可继续。' : notice}
      onChange={(path, value) => { persist({ revision: buffer?.revision ?? strategy.draft.revision, values: { ...buffer?.values, [path]: value } }); setNotice(null); setError(null) }}
      onSave={() => { void save() }} onCancel={() => { persist(null); setError(null); setNotice('已取消编辑，未写入修订。') }} />
    {buffer?.pending && <button type="button" disabled={busy} onClick={() => { void reconcile() }}>核对保存结果</button>}
  </>
}
