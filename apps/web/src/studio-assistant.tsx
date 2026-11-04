import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ApiAdapter } from './app'
import type { UserStrategy, UserStrategyCall, UserStrategyConversation, UserStrategyConversationEvent, UserStrategyResumeRequest } from './user-strategies-api'
import { AgentFeed } from './agent-panel-layout'
import { StorageFailureGuard } from './storage-failure-guard'
import { AgentSurface, ErrorNote, Icon } from '../studio/src/layout'
import { reason, saved, useRead } from './studio-read'

type RetainedCall = { call_id: string; base_revision: number; text?: string; stream?: boolean }
type LiveTool = { id: string; tool: string; state: 'running' | 'done' | 'failed'; detail: string }
type Live = { call_id: string; revision: number; jobId?: string; text: string; tools: LiveTool[]; unconfirmed: boolean }
const toolLabels: Record<string, string> = { discover_resources: '发现策略与数据', inspect_research_input: '核查实验输入', read_experiments: '读取正式报告', describe_data: '读取数据说明', experiment_status: '读取实验状态与报告', propose_change: '提出字段修改' }
const callLabels: Record<UserStrategyCall['status'], string> = { pending: '等待派发', dispatched: '等待模型响应', observed: '响应已记录', refused: '请求被拒绝', unconfigured: '模型未配置', unknown: '结果未知', interrupted_before_dispatch: '派发前中断' }
const activeTask = (status?: string) => ['running', 'awaiting_budget', 'awaiting_decision'].includes(status ?? '')
const settled = (call: UserStrategyCall) => !['pending', 'dispatched'].includes(call.status)
function retainedCalls(key: string): RetainedCall[] {
  try { const value = JSON.parse(saved(key) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item.call_id === 'string' && Number.isInteger(item.base_revision)) : [] } catch { return [] }
}
function MessageContext({ call }: { call?: UserStrategyCall }) {
  return <small className="studio-message-context">{!call ? '历史消息 · 上下文未记录' : call.evidence?.length ? <>
    {call.evidence.map(run => <Link key={run.job_id} to={`/report/${encodeURIComponent(run.job_id)}/overview`}>报告 {run.job_id} · {run.draft_revision == null ? '修订未记录' : `r${run.draft_revision}`}</Link>)}
    <span>提问时草稿 r{call.base_revision}</span>
  </> : `草稿 r${call.base_revision}`}</small>
}
export function Assistant({ api, strategy, changed, jobId, evidence }: { api: ApiAdapter; strategy: UserStrategy; changed: (next: UserStrategy) => void; jobId?: string; evidence?: ReactNode }) {
  const id = strategy.user_strategy_id, key = `quine.studio.calls.${id}`
  const textKey = `quine.studio.unsent.${id}.${jobId ? `report.${jobId}` : 'draft'}`
  const streaming = Boolean(api.streamUserStrategyMessage && api.userStrategyConversation && api.resumeUserStrategyConversation)
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]))
  const [text, setText] = useState(() => saved(textKey)), [textRetained, setTextRetained] = useState(true)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [retained, setRetained] = useState(() => retainedCalls(key)), [live, setLive] = useState<Live | null>(null)
  const [conversation, setConversation] = useState<UserStrategyConversation | null>(null), [conversationError, setConversationError] = useState('')
  const [stopping, setStopping] = useState(false), [stopRequested, setStopRequested] = useState(false)
  const sending = useRef(false), stoppingRef = useRef(false), alive = useRef(true), current = useRef<AbortController | null>(null)
  useEffect(() => { alive.current = true; return () => { alive.current = false; current.current?.abort() } }, [])
  const readConversation = useCallback(async () => {
    if (!streaming) return null
    const next = await api.userStrategyConversation!(id)
    if (next.strategy_id !== id) throw new Error('会话研究身份不匹配')
    return next
  }, [api, id, streaming])
  // Reopening or receiving a saved revision only reads the host's task; it never resumes it.
  useEffect(() => {
    let active = true
    void readConversation().then(next => {
      if (!active) return
      setConversation(next); setConversationError('')
      if (current.current) return
      const retained = retainedCalls(key)
      const remaining = retained.filter(item => !strategy.calls.some(call => call.call_id === item.call_id && settled(call)))
      if (remaining.length === retained.length) return
      try { sessionStorage.setItem(key, JSON.stringify(remaining)); setRetained(remaining) }
      catch { setError('结果已读到，但本机请求缓存未能更新；请保留原请求身份。') }
    })
      .catch(e => { if (active) { setConversation(null); setConversationError(reason(e)) } })
    return () => { active = false }
  }, [readConversation, strategy, key])
  const edit = (value: string) => {
    setText(value)
    try { if (value) sessionStorage.setItem(textKey, value); else sessionStorage.removeItem(textKey); setTextRetained(true) }
    catch { setTextRetained(false) }
  }
  const remember = (items: RetainedCall[]) => { sessionStorage.setItem(key, JSON.stringify(items)); if (alive.current) setRetained(items) }
  const accept = (next: UserStrategy, resolve = true) => {
    if (next.user_strategy_id !== id) throw new Error('研究身份不匹配')
    if (alive.current) changed(next)
    if (resolve) remember(retainedCalls(key).filter(item => !next.calls.some(call => call.call_id === item.call_id && settled(call))))
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
    if (!alive.current) return
    accept(next)
    const state = await readConversation()
    if (!alive.current) return
    setConversation(state); setConversationError('')
    if (retainedCalls(key).length) setError('尚未读到此调用的最终回执，结果仍未知。保留身份，只查询原请求。')
    else { setLive(null); setStopRequested(false) }
  })
  const observe = (event: UserStrategyConversationEvent, callId: string) => {
    if (event.kind === 'text_delta') {
      if (event.call_id && event.call_id !== callId) throw new Error('流式回答身份不匹配')
      setLive(value => value ? { ...value, text: value.text + event.text } : value)
    } else if (event.kind === 'tool_start') {
      if (event.call_id && event.call_id !== callId) throw new Error('工具调用身份不匹配')
      setLive(value => value ? { ...value, tools: [...value.tools.filter(tool => tool.id !== event.tool_id), { id: event.tool_id, tool: event.tool, state: 'running', detail: '' }] } : value)
    } else if (event.kind === 'tool_end' || event.kind === 'tool_error') {
      setLive(value => value ? { ...value, tools: value.tools.map(tool => tool.id === event.tool_id ? { ...tool, state: event.kind === 'tool_error' ? 'failed' : 'done', detail: event.kind === 'tool_error' ? event.error : event.text } : tool) } : value)
    } else if (event.kind === 'error') throw new Error(event.error)
    else if (event.kind === 'state' && event.strategy) accept(event.strategy, false)
    else if (event.kind === 'interrupt' || event.kind === 'done') {
      const call = event.strategy.calls.find(item => item.call_id === callId)
      if (!call) throw new Error('回执未包含本次模型调用')
      accept(event.strategy)
      setLive(value => call.status === 'unknown' && value?.text ? { ...value, unconfirmed: true } : null)
      return true
    }
    return false
  }
  const send = () => act(async () => {
    const call_id = `studio-${crypto.randomUUID()}`
    const input = { call_id, base_revision: strategy.draft.revision, text, approved_budget: { max_model_calls: 1, max_backtests: 0 }, ...(jobId ? { evidence_job_ids: [jobId] } : {}) }
    try { remember([...retainedCalls(key), { call_id, base_revision: input.base_revision, text: text.trim(), ...(streaming ? { stream: true } : {}) }]) } catch { throw new Error('无法保留请求身份，本次未发送') }
    setStopRequested(false)
    if (streaming) {
      const controller = new AbortController()
      current.current = controller
      setLive({ call_id, revision: input.base_revision, jobId, text: '', tools: [], unconfirmed: false })
      let terminal = false
      try {
        // An explicit one-request task enables stop without taking the legacy default grant of 24.
        const events = await api.streamUserStrategyMessage!(id, { ...input, task_budget: 1 }, controller.signal)
        for await (const event of events) {
          if (!alive.current) return
          if (observe(event, call_id)) { terminal = true; break }
        }
        if (!terminal) throw new Error('响应未确认：连接结束前未收到本次请求的最终回执')
      } catch (e) {
        if (!alive.current) return
        setLive(value => value ? { ...value, unconfirmed: true } : null)
        throw e
      } finally { if (current.current === controller) current.current = null }
    } else {
      const next = await api.sendUserStrategyMessage!(id, input)
      if (!next.calls.some(call => call.call_id === call_id)) throw new Error('回执未包含本次模型调用')
      accept(next)
    }
    // A late receipt clears only the submitted buffer, never newer typing or another context.
    if (saved(textKey) === text) sessionStorage.removeItem(textKey)
    if (alive.current) setText(value => value === text ? '' : value)
  })
  const savedTask = [...strategy.calls].reverse().find(call => activeTask(call.task?.status))
  const task = activeTask(conversation?.task?.status) ? conversation?.task : null
  const receiving = busy && live !== null && !live.unconfirmed
  const turnId = (receiving ? live.call_id : null) ?? savedTask?.turn_id ?? savedTask?.call_id ?? task?.turn_id
  const stop = async () => {
    if (!turnId || stoppingRef.current || !api.resumeUserStrategyConversation) return
    stoppingRef.current = true; setStopping(true); setError('')
    try {
      let terminal = false
      // A separate control request; aborting the transport alone would not stop the task.
      for await (const event of await api.resumeUserStrategyConversation(id, { turn_id: turnId, action: 'cancel' })) {
        if (!alive.current) return
        if (event.kind === 'error') throw new Error(event.error)
        if ((event.kind === 'state' && event.strategy) || event.kind === 'done' || event.kind === 'interrupt') {
          const next = event.strategy!
          const original = next.calls.find(call => call.call_id === turnId)
          if (!original) throw new Error('停止回执未包含原任务')
          accept(next)
          if (event.kind === 'done' || event.kind === 'interrupt') {
            if (original.task?.status !== 'stopped' || original.task.stop_reason !== 'cancelled') throw new Error('停止回执尚未确认，请查询原请求')
            terminal = true
          }
        }
      }
      if (!terminal) throw new Error('停止结果未确认，请查询原请求')
      if (alive.current) setStopRequested(true)
    } catch (e) { if (alive.current) setError(`${reason(e)}。请读取核对，不会自动重发。`) }
    finally { stoppingRef.current = false; if (alive.current) setStopping(false) }
  }
  const selectedModel = strategy.model_profile_id ? model.value?.profiles?.find(item => item.id === strategy.model_profile_id) : model.value
  const inFlight = strategy.calls.some(call => !settled(call) || activeTask(call.task?.status)) || Boolean(task)
  const localUnknown = retained.filter(item => !busy || item.call_id !== live?.call_id)
  const pending = conversation?.pending
  const pendingProposal = pending?.phase === 'awaiting_proposal' ? strategy.proposals.find(proposal => proposal.proposal_id === pending.proposal_id) : null
  const waiting = pending?.phase === 'awaiting_proposal' ? pendingProposal?.status === 'applied' ? '提案已应用到草稿；结束本轮后可提出新问题。' : pendingProposal?.status === 'rejected' ? '提案已拒绝；结束本轮后可提出新问题。' : '等待提案审阅；应用只更新草稿，本轮需明确结束。'
    : pending?.phase === 'awaiting_budget' ? '本轮调用额度已用完；结束本轮后可提出新问题。'
      : pending?.phase === 'awaiting_confirmation' ? '已有实验等待决定；结束本轮后可在独立确认页核对。'
        : pending?.phase === 'awaiting_observation' ? '原实验仍需核对，请查看实验记录。' : ''
  const pendingCall = pending ? strategy.calls.find(call => call.call_id === pending.turn_id) : null
  const legacyPending = pending && !pendingCall?.task ? pending : null
  const legacyDecision: { label: string; input: UserStrategyResumeRequest } | null = !legacyPending || !pendingCall ? null
    : legacyPending.phase === 'awaiting_confirmation' ? { label: '取消原实验确认', input: { turn_id: legacyPending.turn_id, action: 'declined' } }
      : legacyPending.phase === 'awaiting_proposal' && pendingProposal?.status === 'rejected' ? { label: '结束已拒绝的旧会话', input: { turn_id: legacyPending.turn_id, action: 'rejected', proposal_id: legacyPending.proposal_id } }
        : legacyPending.phase === 'awaiting_proposal' && pendingProposal?.status === 'applied' ? { label: '核对已应用提案', input: { turn_id: legacyPending.turn_id, action: 'applied', proposal_id: legacyPending.proposal_id } } : null
  const legacyNote = !legacyPending ? '' : !pendingCall ? '历史会话身份不完整，请查询原请求核对。'
    : legacyPending.phase === 'awaiting_observation' ? '历史会话没有可停止的任务记录；请到原实验查看状态。本页不会续跑或代为停止实验。'
      : legacyPending.phase === 'awaiting_budget' ? '历史会话没有可结束的任务记录；请查询原请求核对，本页不会追加额度或续跑。'
        : legacyPending.phase === 'awaiting_confirmation' ? '历史会话在等待实验确认；可以取消这次确认，保留当前草稿。'
          : pendingProposal?.status === 'applied' ? '历史提案已应用。核对后可取消原会话的实验确认；不会调用模型或运行实验。'
            : pendingProposal?.status === 'rejected' ? '历史提案已拒绝，可以明确结束旧会话。'
              : '请先审阅或拒绝原提案，再核对或结束旧会话。'
  const decideLegacy = () => act(async () => {
    if (!legacyDecision || !legacyPending || !api.resumeUserStrategyConversation) return
    const before = await readConversation()
    if (!before?.pending || before.pending.turn_id !== legacyPending.turn_id || before.pending.phase !== legacyPending.phase || before.pending.proposal_id !== legacyPending.proposal_id) throw new Error('原会话状态已变化，请重新读取')
    let terminal = false
    // These existing phase-specific decisions only acknowledge a saved proposal or
    // decline a preview. They cannot dispatch a provider or confirm an experiment.
    for await (const event of await api.resumeUserStrategyConversation(id, legacyDecision.input)) {
      if (!alive.current) return
      if (event.kind === 'error') throw new Error(event.error)
      if (event.kind === 'state' && event.strategy) accept(event.strategy)
      if (event.kind === 'done' || event.kind === 'interrupt') {
        if (!event.strategy.calls.some(call => call.call_id === legacyDecision.input.turn_id)) throw new Error('回执未包含原会话')
        accept(event.strategy); terminal = true
      }
    }
    if (!terminal) throw new Error('旧会话决定的结果未确认')
    const after = await readConversation()
    if (!alive.current) return
    setConversation(after); setConversationError('')
    if (after?.pending?.turn_id === before.pending.turn_id && after.pending.phase === before.pending.phase) throw new Error('旧会话尚未完成核对，请查询原请求')
  })
  const completedStatus = conversation?.task?.stop_reason === 'completed' ? '本轮已完成' : conversation?.task?.stop_reason === 'cancelled' ? '本轮已结束，后续请求已停止' : conversation?.task?.stop_reason === 'unknown' ? '结果未知，请查询原请求' : '等待输入'
  const status = stopRequested && (busy || strategy.calls.some(call => !settled(call))) ? '停止已请求；正在核对本次响应。' : live?.unconfirmed ? '响应未确认，保留已收到的内容。'
    : busy ? receiving ? '正在接收回答…' : '正在核对保存状态…' : legacyNote || waiting || (conversationError ? '会话暂不可读，请重新读取。' : streaming && !conversation ? '正在读取会话状态…' : completedStatus)
  const locked = busy || inFlight || stopping || localUnknown.length > 0 || Boolean(pending)
  return <AgentSurface context={jobId ? `冻结报告 ${jobId.slice(0, 8)} · 只读证据` : `当前草稿 r${strategy.draft.revision}`} report={!!jobId}
    history={<div className="studio-conversation"><AgentFeed identity={`${id}.${jobId ?? 'draft'}`} version={`${strategy.updated_at}|${strategy.messages.length}|${strategy.calls.length}|${live?.text}|${JSON.stringify(live?.tools)}|${status}`}>
      {evidence}
      {!evidence && <div className="agent-note">{jobId ? '这次讨论只引用所选运行的冻结事实。' : '先检查草稿与数据，再决定下一次实验。'}<small>模型回答为推断；修改提案需要审阅，实验需要独立确认。</small></div>}
      <details className="agent-context"><summary>来源与上下文</summary><p>研究 {id} · 当前草稿 r{strategy.draft.revision}</p><Link to={`/research/${encodeURIComponent(id)}/draft`}>查看当前草稿</Link>{jobId && <><p><Link to={`/report/${encodeURIComponent(jobId)}/inputs`}>冻结输入 · {jobId}</Link></p><Link to={`/report/${encodeURIComponent(jobId)}/trades`}>交易证据</Link></>}</details>
      {strategy.messages.map(message => <article className={`message ${message.role}`} key={message.message_id}><small>{message.role === 'user' ? '你' : '模型 · 推断'}</small><MessageContext call={strategy.calls.find(call => call.call_id === message.call_id)} /><p>{message.text}</p></article>)}
      {live && <section aria-label="当前回答" className="studio-live-answer"><small className="studio-message-context">{live.jobId ? `报告 ${live.jobId} · 提问时草稿 r${live.revision}` : `草稿 r${live.revision}`}</small><p>{live.text || '等待模型输出…'}</p><small>{live.unconfirmed ? '已接收片段 · 结果未确认' : '生成中 · 模型推断'}</small>
        {live.tools.length > 0 && <details className="studio-tool-activity"><summary>本轮活动 · {live.tools.length} 项</summary><ul>{live.tools.map(tool => <li key={tool.id}><details><summary>{toolLabels[tool.tool] ?? tool.tool} · {tool.state === 'running' ? '执行中…' : tool.state === 'done' ? '已完成' : '失败'}</summary><p>{tool.detail || '尚未收到结果'}</p></details></li>)}</ul></details>}
      </section>}
      {(strategy.calls.length > 0 || localUnknown.length > 0) && <section className="call-history" aria-label="模型调用记录"><details><summary><h3>调用记录</h3></summary>
        {strategy.calls.map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · {callLabels[call.status] ?? call.status}</p>{call.model_profile && <small>当次连接：{call.model_profile.name} · v{call.model_profile.revision}</small>}{call.status === 'unknown' && <p>结果与用量未知，可能已到达提供方；不会重发。可以明确发送一个新问题。</p>}{call.message && <p>{call.message}</p>}<small>{call.usage_available ? `输入 ${call.input_tokens ?? '未提供'} / 输出 ${call.output_tokens ?? '未提供'} tokens` : '未报告用量，不按零计费'}</small>{call.tool_results?.map(tool => <details className="studio-tool-activity" key={tool.tool_id}><summary>{toolLabels[tool.tool] ?? tool.tool} · {tool.status === 'done' ? '已完成' : '失败'}</summary><p>{tool.text}</p></details>)}</article>)}
        {localUnknown.filter(call => !strategy.calls.some(saved => saved.call_id === call.call_id)).map(call => <article key={call.call_id}><code>{call.call_id}</code><p>r{call.base_revision} · 回执未知，保留原请求身份。</p></article>)}
      </details><button className="button" disabled={busy || stopping} onClick={() => void query()}>查询模型请求</button></section>}
      {strategy.proposals.filter(p => p.status === 'open').map(p => <section className="real-proposal" key={p.proposal_id}><h3>建议修改 · r{p.base_revision}</h3><p>{p.items.length} 项变更，等待你审阅。</p>{jobId && <small>修改对象是当前研究草稿，不改变冻结报告。</small>}<Link className="button" to={`/research/${encodeURIComponent(id)}/proposal?proposal=${encodeURIComponent(p.proposal_id)}`}>审阅修改提案</Link><p className="muted">应用只保存新修订，运行仍需独立确认。</p></section>)}
      {!strategy.messages.length && !strategy.calls.length && !localUnknown.length && !live && <div className="suggestions"><small>你可以这样问</small><button className="button" onClick={() => edit(jobId ? '解释这份报告的结果与限制' : '检查当前草稿的参数与数据')}>检查当前证据</button><button className="button" onClick={() => edit('下一步应该检查哪些证据？')}>检查下一步</button></div>}
      {(legacyNote || waiting) && <p className="studio-assistant-status">{legacyNote || waiting}{pending?.phase === 'awaiting_observation' && <Link to={`/research/${encodeURIComponent(id)}/experiments`}>查看原实验</Link>}</p>}
      {legacyDecision && <button className="button" disabled={busy || stopping} onClick={() => void decideLegacy()}>{legacyDecision.label}</button>}
    </AgentFeed></div>}
    input={<><ErrorNote>{error || conversationError || model.error}</ErrorNote>{conversationError && <button className="button" disabled={busy} onClick={() => void query()}>重新读取会话</button>}<label className="sr-only" htmlFor="studio-message">消息</label><textarea id="studio-message" rows={3} value={text} onChange={e => edit(e.target.value)} placeholder={jobId ? '询问这份报告…' : '继续讨论这个研究…'} />
      {!textRetained && text && <small>未发送消息无法在本机缓冲，离开前请保留输入。</small>}
      <StorageFailureGuard active={!textRetained && !!text} message="未发送消息无法在本机保留，离开将丢失输入。" />
      <details className="studio-model-controls"><summary>{selectedModel?.configured ? selectedModel.model : '选择模型连接'}</summary>
        {api.selectUserStrategyModelProfile && model.value?.profiles && <label className="research-model-picker">本研究模型连接<select value={strategy.model_profile_id ?? ''} disabled={locked} onChange={event => { const profileId = event.target.value; void act(async () => accept(await api.selectUserStrategyModelProfile!(id, profileId || null))) }}>
          <option value="">跟随工作区默认 · {model.value.profiles.find(item => item.id === model.value?.default_profile_id)?.name ?? '未设置'}</option>
          {strategy.model_profile_id && !model.value.profiles.some(item => item.id === strategy.model_profile_id) && <option value={strategy.model_profile_id}>已选连接不可用，请重新选择</option>}
          {model.value.profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}{item.configured ? '' : ' · 未配置'}</option>)}
        </select></label>}
        <small>选择仅影响本研究下一次发送；历史调用与已批准轮次保持原连接。</small>
        <div className="research-model-actions"><Link to="/resources/models">管理模型连接</Link><button className="button" disabled={busy} onClick={model.reload}>重新读取连接</button></div>
      </details>
      <div className="studio-composer-actions"><small className="studio-assistant-status" role="status" aria-live="polite">{status}</small>
        {streaming && turnId && <button className="button" disabled={stopping || stopRequested} onClick={() => void stop()} aria-label={receiving ? '停止生成' : '结束本轮'}><Icon name="stop" />{receiving ? '停止' : '结束本轮'}</button>}
        <button aria-label="发送一次模型请求" title="发送一次模型请求" className="button primary real-send" disabled={busy || stopping || inFlight || Boolean(pending) || (streaming && (!conversation || !!conversationError)) || localUnknown.some(call => call.text === text.trim()) || !selectedModel?.configured || !text.trim()} onClick={() => void send()}><Icon name="send" size={22} /></button>
      </div>
      <small>{selectedModel?.configured ? '一次发送授权 1 次模型调用；不授权运行实验。' : '模型未配置或不可读；不会生成替代回答。'}</small>
      {localUnknown.length > 0 && <small>同一问题只查询原请求。可以输入不同的新问题，原身份仍保留。</small>}
    </>} />
}
