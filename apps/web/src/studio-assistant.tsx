import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ApiAdapter } from './app'
import type { UserStrategy } from './user-strategies-api'
import { StorageFailureGuard } from './storage-failure-guard'
import { AgentSurface, ErrorNote, Icon } from '../studio/src/layout'
import { reason, saved, useRead } from './studio-read'

type RetainedCall = { call_id: string; base_revision: number; text?: string }
function retainedCalls(key: string): RetainedCall[] {
  try { const value = JSON.parse(saved(key) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item.call_id === 'string' && Number.isInteger(item.base_revision)) : [] } catch { return [] }
}
export function Assistant({ api, strategy, changed, jobId, evidence }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; jobId?: string; evidence?: ReactNode }) {
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

