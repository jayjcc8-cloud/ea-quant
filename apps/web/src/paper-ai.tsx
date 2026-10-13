import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ConversationInput } from './conversation-input'
import { conversationEvents } from './user-strategies-api'
import {
  paperApi, paperExplanationBudget, paperExplanationDraftKey, paperExplanationPendingKey,
  type PaperApi, type PaperExplanationConversation, type PaperExplanationEvidence,
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

/** Deterministic presentation of this call's frozen host evidence, never an AI summary. */
function HostEvidenceOverview({ evidence, scope }: { evidence: PaperExplanationEvidence | null; scope?: string | null }) {
  if (!evidence) return <section className="paper-ai-overview" aria-label="宿主证据概览">
    <h3>宿主证据概览</h3><p className="paper-ai-overview-caption">本轮冻结证据 · 非 AI 摘要</p>
    <p>本轮结构化证据未确认，无法生成概览；请查看已保存的 AI 原文。</p>
    <p>{scopeLabel[scope ?? 'unknown'] ?? scopeLabel.unknown}</p>
  </section>
  const health = evidence.health
  const current = evidence.state_scope === 'current' && health.available === true && health.permission_scope === 'observed'
  const shortScope = { current: '读取时观测', historical: '历史观测', stale: '观测陈旧', unknown: '观测未确认' }[evidence.state_scope] ?? '观测未确认'
  const readiness = current && typeof health.runtime_ready === 'boolean' ? health.runtime_ready ? '读取时就绪' : '读取时未就绪' : '就绪未知'
  const permission = current && typeof health.trade_permitted === 'boolean' ? health.trade_permitted ? '读取时未阻断' : '读取时受阻' : '许可未知'
  const reason = evidence.state_scope === 'stale' ? '观测已陈旧，当前就绪与许可未知。'
    : evidence.state_scope === 'historical' ? '历史记录不能作为当前就绪或许可。'
    : !current ? '缺少已确认的当前观测，就绪与许可未知。'
    : health.runtime_ready === false || health.trade_permitted === false ? '后端记录了未就绪或受阻状态；具体原因见本轮证据来源。'
    : health.runtime_ready === true && health.trade_permitted === true ? '本轮未报告就绪或许可阻断；具体订单仍须独立检查。'
    : '本轮未提供完整就绪与许可字段，缺失项保持未知。'
  const equity = typeof evidence.economics.equity === 'string' && evidence.economics.equity.trim() ? evidence.economics.equity : null
  const currency = typeof evidence.economics.currency === 'string' ? evidence.economics.currency : '币种未提供'
  const economics = evidence.economics.available !== true ? '经济快照不可用。'
    : equity === null ? '经济快照未提供权益数值；不代表当前权益。'
    : `已保存快照权益 ${equity} ${currency}，不代表当前权益。`
  return <section className="paper-ai-overview" aria-label="宿主证据概览">
    <h3>宿主证据概览</h3><p className="paper-ai-overview-caption">本轮冻结证据 · 非 AI 摘要</p>
    <dl>
      <div><dt>状态</dt><dd>记录为{paperStateLabels[evidence.state] ?? '状态未知'}（{shortScope}）{current ? `；${readiness}，${permission}` : ''}。</dd></div>
      <div><dt>原因</dt><dd>{reason}</dd></div>
      <div><dt>限制</dt><dd>{economics}{evidence.market.kind === 'local_synthetic_feed' ? '合成行情；' : '行情来源未确认；'}无收益历史，不能推断盈利或业绩。</dd></div>
    </dl>
  </section>
}

/** The parent keys this panel by run ID; subscriptions, input and saved calls share that identity. */
export function PaperAi({ runId, instanceName, sourceJobId, onOverview, api = paperApi }: {
  runId: string; instanceName: string; sourceJobId: string; onOverview?: () => void; api?: PaperExplanationApi
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

  const scroller = useRef<HTMLDivElement | null>(null)
  const latestRound = useRef<HTMLLIElement | null>(null)
  const positioned = useRef(false)
  const followEnd = useRef(false)
  const scrollToEnd = () => {
    const node = scroller.current
    if (node) node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight)
  }
  const readScrollPosition = () => {
    const node = scroller.current
    if (node) followEnd.current = node.scrollHeight - node.clientHeight - node.scrollTop <= 32
  }
  const pauseForDetails = (event: React.SyntheticEvent<HTMLDetailsElement>) => {
    if (event.currentTarget.open) followEnd.current = false
  }
  // Only the panel scrolls. A keyed remount starts each instance at its latest
  // retained turn; subsequent reads/deltas respect the reader's position.
  useLayoutEffect(() => {
    const node = scroller.current
    if (!node || !conversation) return
    if (!positioned.current) {
      positioned.current = true
      const target = latestRound.current
      if (target) node.scrollTop = Math.max(0, Math.min(node.scrollHeight - node.clientHeight,
        node.scrollTop + target.getBoundingClientRect().top - node.getBoundingClientRect().top))
      readScrollPosition()
    } else if (followEnd.current) scrollToEnd()
  }, [conversation, streaming, busy])
  useEffect(() => {
    const node = scroller.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => { if (positioned.current && followEnd.current) scrollToEnd() })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

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
    followEnd.current = true
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
  const latestCallId = latest?.call_id ?? conversation?.messages.at(-1)?.call_id
  const latestRoundIndex = conversation?.messages.findIndex(message => message.call_id === latestCallId)
  return <aside className="paper-ai strategy-chat" aria-label="AI 运行解释">
    <header className="chat-head"><h2 id="paper-ai-heading" tabIndex={-1}>AI 运行解释</h2><button onClick={() => void read()} disabled={busy}>刷新证据</button></header>
    <p className="paper-ai-context">只读解释：{instanceName}</p>
    <nav className="paper-ai-navigation" aria-label="解释会话导航">
      {onOverview && <button className="paper-ai-back" onClick={onOverview}>返回概览</button>}
      <button onClick={() => { followEnd.current = true; scrollToEnd() }}>回到最新</button>
    </nav>
    <div className="chat-body" ref={scroller} role="region" aria-label="运行解释会话" tabIndex={0} onScroll={readScrollPosition}>
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
        {conversation?.messages.map((message, index) => {
          const recorded = conversation.calls.find(call => call.call_id === message.call_id)?.evidence
          // Never borrow refreshed context or a different call/instance for an old answer.
          const evidence = recorded?.schema === 'ea.paper-explanation-evidence.v1' && recorded.run_id === runId
            && recorded.observed_at === message.evidence_observed_at && recorded.state_scope === message.evidence_scope ? recorded : null
          return <li ref={index === latestRoundIndex ? latestRound : undefined} key={message.message_id} className={`message message-${message.role}`}>
            <p className="message-role">{message.role === 'user' ? '你' : 'QUINE · 运行解释'}</p>
            {message.role === 'user' ? <p className="paper-ai-answer-text">{message.text}</p> : <>
              <HostEvidenceOverview evidence={evidence} scope={message.evidence_scope} />
              <details className="paper-ai-original" onToggle={pauseForDetails}><summary>完整 AI 回答（原文）</summary><p className="paper-ai-answer-text">{message.text}</p></details>
              <details className="paper-ai-message-source" onToggle={pauseForDetails}><summary>本轮证据来源</summary>
                <p>依据证据观测于 {observationTime(message.evidence_observed_at)}</p>
                <p>原始观测时间：<time dateTime={message.evidence_observed_at ?? undefined}>{text(message.evidence_observed_at)}</time></p>
                {evidence && <><p>本轮技术依据（完整记录）</p><pre className="paper-ai-technical">{JSON.stringify(evidence, null, 2)}</pre></>}
              </details>
            </>}
          </li>
        })}
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
