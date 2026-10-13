import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ConversationInput } from './conversation-input'
import { conversationEvents } from './user-strategies-api'
import {
  paperApi, paperExplanationBudget, paperExplanationDraftKey, paperExplanationPendingKey,
  type PaperApi, type PaperExplanationConversation,
} from './paper-api'
import { observationTime, paperStateLabels } from './paper-observation'

export type PaperExplanationApi = Pick<PaperApi, 'conversation' | 'explain'>
const scopeLabel: Record<string, string> = {
  current: '读取时观测，不持续更新', historical: '历史观测，不代表当前状态',
  stale: '观测陈旧，当前就绪与许可未知', unknown: '观测未确认，当前状态未知',
}
const callLabel: Record<string, string> = {
  pending: '已记录 · 尚未确认发送', dispatched: '已发送 · 结果未确认', observed: '已回答',
  refused: '已拒绝 · 未执行操作', unknown: '结果未知 · 不会自动重发', unconfigured: '模型未配置 · 未发送',
}
const saved = (key: string) => { try { return sessionStorage.getItem(key) ?? '' } catch { return '' } }
const text = (value: unknown) => value === undefined || value === null || value === '' ? '未提供' : String(value)

/** The parent keys this panel by run ID; subscriptions, input and saved calls share that identity. */
export function PaperAi({ runId, instanceName, sourceJobId, api = paperApi }: {
  runId: string; instanceName: string; sourceJobId: string; api?: PaperExplanationApi
}) {
  const [conversation, setConversation] = useState<PaperExplanationConversation | null>(null)
  const [draft, setDraft] = useState(() => saved(paperExplanationDraftKey(runId)))
  const [streaming, setStreaming] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [pendingCall, setPendingCall] = useState(() => saved(paperExplanationPendingKey(runId)))
  const pending = useRef(pendingCall)
  const busyRef = useRef(false)
  const alive = useRef(false)
  const epoch = useRef(0)
  const abort = useRef<AbortController | null>(null)

  const forgetPending = useCallback(() => {
    try { sessionStorage.removeItem(paperExplanationPendingKey(runId)) } catch { /* Query remains safe after reload. */ }
    pending.current = ''; setPendingCall('')
  }, [runId])
  const read = useCallback(async () => {
    const token = ++epoch.current
    try {
      const value = await api.conversation(runId)
      if (!alive.current || token !== epoch.current || value.run_id !== runId) return null
      setConversation(value); setNotice(value.context_error ?? '')
      const retained = value.calls.find(call => call.call_id === pending.current)
      if (retained && !['pending', 'dispatched'].includes(retained.status)) forgetPending()
      return value
    } catch {
      if (alive.current && token === epoch.current) setNotice('连接中断；保留上次观测与回答，当前证据未确认。')
      return null
    }
  }, [api, runId, forgetPending])
  const invalidate = useCallback(() => { epoch.current++ }, [])
  useEffect(() => {
    alive.current = true
    const timer = window.setTimeout(() => { void read() }, 0)
    return () => { alive.current = false; invalidate(); window.clearTimeout(timer); abort.current?.abort() }
  }, [read, invalidate])
  const setDraftValue = (value: string) => {
    setDraft(value)
    try { sessionStorage.setItem(paperExplanationDraftKey(runId), value) } catch { /* Text stays in memory. */ }
  }
  const send = async () => {
    const question = draft.trim()
    if (!question || busyRef.current || pending.current || !conversation?.context || !conversation.route) return
    const callId = crypto.randomUUID()
    try { sessionStorage.setItem(paperExplanationPendingKey(runId), callId) }
    catch { setNotice('无法保存本次提问身份，尚未发送。'); return }
    pending.current = callId; setPendingCall(callId)
    busyRef.current = true; setBusy(true); setNotice(''); setStreaming(''); epoch.current++
    const controller = new AbortController(); abort.current = controller
    try {
      const response = await api.explain(runId, { call_id: callId, text: question, approved_budget: paperExplanationBudget }, controller.signal)
      for await (const item of conversationEvents(response)) {
        if (!alive.current) return
        if (item.kind === 'text_delta') setStreaming(value => value + item.text)
      }
      if (alive.current) { setStreaming(''); setDraftValue(''); await read() }
    } catch {
      if (alive.current) { setStreaming(''); setNotice('这次提问的结果未确认；请查询原请求，不会自动重发。') }
    } finally {
      abort.current = null; busyRef.current = false
      if (alive.current) setBusy(false)
    }
  }
  const queryPending = async () => {
    if (busyRef.current) return
    const retained = pending.current
    const value = await read()
    if (value && retained && !value.calls.some(call => call.call_id === retained)) {
      forgetPending(); setNotice('后端未记录这次提问。可再次明确发送；系统不会重发原请求。')
    }
  }
  const context = conversation?.context
  const latest = conversation?.calls.at(-1)
  return <aside className="paper-ai strategy-chat" aria-label="AI 运行解释">
    <header className="chat-head"><h2>AI 运行解释</h2><button onClick={() => void read()} disabled={busy}>刷新证据</button></header>
    <p className="paper-ai-context">只读解释：{instanceName}</p>
    <div className="chat-body">
      {context && <div className="paper-ai-evidence">
        <p><strong>{paperStateLabels[context.state] ?? '状态未知'}</strong> · {scopeLabel[context.state_scope]}</p>
        <p>证据观测时间：{observationTime(context.observed_at)}</p>
        <p>经济快照发布：{observationTime(typeof context.economics.observed_at === 'string' ? context.economics.observed_at : null)}</p>
        <p>{context.economics.available ? `快照权益 ${text(context.economics.equity)} ${text(context.economics.currency)}` : '经济快照不可用'}</p>
        <Link to={`/backtests/${encodeURIComponent(context.source_job_id)}`}>来源研究 ↗</Link>
      </div>}
      {!context && <p>当前证据不可用。已保存的回答仍保留原观测范围。<Link to={`/backtests/${encodeURIComponent(sourceJobId)}`}>来源研究 ↗</Link></p>}
      {!conversation?.messages.length && <p className="chat-empty-guidance">可以询问当前状态、阻断原因或经济快照。解释仅使用这次读取到的运行证据。</p>}
      <ol className="message-list paper-ai-thread">
        {conversation?.messages.map(message => <li key={message.message_id} className={`message message-${message.role}`}>
          <p className="message-role">{message.role === 'user' ? '你' : 'QUINE · 模型解释'}</p>
          <p className="paper-ai-answer-text">{message.text}</p>
          {message.role === 'assistant' && <p className="paper-ai-message-source">依据证据观测于 {observationTime(message.evidence_observed_at)}（{scopeLabel[message.evidence_scope ?? 'unknown'] ?? scopeLabel.unknown}）</p>}
        </li>)}
      </ol>
      {busy && <div className="turn-answer live-answer" role="status" aria-live="polite"><p>{streaming || '正在读取证据并请求解释…'}</p></div>}
      {latest && <p className="paper-ai-message-source">{callLabel[latest.status] ?? '调用状态未知'}</p>}
    </div>
    <div className="composer">
      {notice && <p role="alert" className="notice error">{notice}</p>}
      {pendingCall && <div className="paper-ai-unknown" role="status"><p>这次提问尚未确认；保留原请求身份。</p><button disabled={busy} onClick={() => void queryPending()}>查询原请求结果</button></div>}
      <ConversationInput id={`paper-question-${runId}`} label="围绕这次运行的证据提问" value={draft} onChange={setDraftValue} disabled={busy} placeholder="例如：为什么阻断？当前快照说明什么？" />
      <button className="paper-primary" disabled={busy || !draft.trim() || !!pendingCall || !context || !conversation?.route} onClick={() => void send()}>发送提问（1 次模型调用）</button>
      {conversation && !conversation.route && <p className="paper-caution">当前工作区模型未配置，无法发送提问。</p>}
      <p className="paper-caution">仅解释，不执行操作。合成行情与模拟资金；无收益历史。选择、刷新或重载不会调用模型。</p>
    </div>
  </aside>
}
