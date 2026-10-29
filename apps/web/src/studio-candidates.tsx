/** Candidate creation selects the relationship already open in real v3.
 * The existing service alone verifies and publishes its frozen evidence. */
import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import type { ApiAdapter } from './app'

export function CandidateFromHoldout({ api, validationId, ready }: { api: ApiAdapter; validationId: string; ready: boolean }) {
  const key = `quine.studio.candidate.${validationId}`
  const [pending, setPending] = useState(() => { try { return !!sessionStorage.getItem(key) } catch { return true } })
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const sending = useRef(false), navigate = useNavigate()
  const create = async () => {
    if (!ready || pending || sending.current) return
    // As for holdout confirmation, keep a pending UI receipt before dispatch.
    // The canonical Candidate is only the service record, never this receipt.
    try { sessionStorage.setItem(key, 'pending') }
    catch { setError('无法保留请求状态，尚未发送。请恢复本机存储后再创建。'); return }
    sending.current = true; setPending(true); setBusy(true); setError('')
    try {
      const record = await api.createCandidate!(validationId)
      sessionStorage.removeItem(key)
      navigate(`/candidates/${encodeURIComponent(record.candidate_id)}`)
    } catch (caught) {
      setError(`创建结果未确认：${caught instanceof Error ? caught.message : '回执不可读'}。请查询此留出的已保存候选；不会重新提交。`)
    } finally { sending.current = false; setBusy(false) }
  }
  return <section className="panel" aria-label="创建研究候选"><h2>研究候选</h2>
    <p>使用当前留出关系的两份完成报告创建 EVALUATED 候选，再填写理由作出人工研究决定。</p>
    <p>选定留出：{validationId}</p>
    {!ready && <p>两份正式报告均可读后才能创建。</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {pending && <p role="status">{busy ? '正在保存选定证据…' : '请求已提交，只查询已保存候选。'}</p>}
    <div className="actions"><button className="button" disabled={!ready || pending || busy} onClick={() => void create()}>创建 EVALUATED 候选</button>
      <Link to={`/candidates?validation=${encodeURIComponent(validationId)}`}>查询此留出的候选</Link></div>
  </section>
}
