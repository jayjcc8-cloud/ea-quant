/**
 * QUINE: the frozen runs one strategy's conversation may be grounded in.
 *
 * This panel is the explicit selection behind one approved turn. It lists only
 * this strategy's own created runs, reads the selected ones through the existing
 * read APIs, and shows their frozen parameters and assumptions, the report's own
 * verified figures and exact B − A deltas with the report's own currency. A
 * report that is missing, or whose identity is not the run it was read for, is
 * said out loud: nothing here is filled in with a zero, and nothing here is
 * ranked, scored or called a winner.
 *
 * Two actions leave this panel, and both are model calls rather than runs:
 * 解释所选证据 asks for the host-verified reading of exactly the runs selected
 * here, and 提出下一实验 asks for typed editable fields against the current
 * draft. Each one needs its own fresh approval, carries the selected ids with the
 * request, and starts nothing — a run still happens only from the user's own
 * confirmation of a preview on the 实验 tab, and a proposal is still applied by
 * the user on the 建议修改 panel. Reading this section, choosing a run and
 * mounting it call no model and create no run.
 *
 * The address is the selection: a verified report or comparison carries the runs
 * it is about back in evidence_a and evidence_b, and this panel reads that
 * address instead of remembering a selection of its own, so a reload, the
 * browser's own back and forward and a fresh load all agree. An address it cannot
 * verify — an unknown, foreign or ambiguous run — is refused out loud, with one
 * way to clear it, rather than answered from the latest run.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'
import { exactDelta } from './decimal'
import { evidenceAddress, readEvidenceSelection, selectableRuns, selectedRuns } from './evidence-selection'
import {
  UserStrategyRequestError,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyExperiment,
} from './user-strategies-api'

/** One model call, and no experiment: selecting evidence never runs anything. */
const RESEARCH_APPROVAL: ApprovedBudget = { max_model_calls: 1, max_backtests: 0 }

/** What the host's own refusal means here; the host's own message stays visible. */
const REFUSALS: Record<string, string> = {
  stale_base_revision: '草稿修订已前进，这次请求没有被发送。请重新加载该策略后再试。',
  operation_conflict: '这个调用编号已经绑定到另一个请求，因此什么都没有发送。',
  approval_required: '这里需要一次明确的批准，而且它必须在该工作区的预算之内。',
  user_strategy_invalid:
    '所选运行不是这条策略自己的、可读且身份一致的冻结运行，因此宿主拒绝了整次请求：没有写任何记录，也没有询问模型。',
  user_strategy_not_found: '该策略不在这个工作区里。请重新加载策略列表。',
  request_failed: '本地服务拒绝了这次请求，什么都没有发送。',
}

const reason = (error: unknown) => {
  if (error instanceof UserStrategyRequestError) {
    const known = REFUSALS[error.code]
    return known ? `${known} · 服务端：${error.message}` : `服务端：${error.message}`
  }
  return error instanceof Error ? error.message : '这次请求失败了，什么都没有发送。'
}

/** One amount as a report states it: the unit is optional in the wire type. */
type Amount = { amount: string; currency?: string }

/** One amount with the unit that was actually recorded for it — never an assumed one. */
type Money = { amount: string; currency: string | null }

type RunState =
  | { kind: 'loading' }
  | { kind: 'unavailable'; note: string }
  | { kind: 'ready'; job: BacktestJob; report: BacktestReport }

/**
 * One frozen run's own state and formal report, kept with the run it was read for.
 *
 * Nothing of another run is ever shown as this one's — not even while a read is in
 * flight — and a report whose run id is not the job's own engine run is reported
 * as unavailable rather than shown as a result.
 */
function useRunRead(api: ApiAdapter, jobId: string | null): RunState | null {
  const [loaded, setLoaded] = useState<{ key: string; state: RunState } | null>(null)

  useEffect(() => {
    if (!jobId) return
    let active = true
    const load = async () => {
      const job = await api.getBacktest(jobId)
      if (!active) return
      if (job.status !== 'succeeded' || !job.report_ready) {
        setLoaded({
          key: jobId,
          state: {
            kind: 'unavailable',
            note: job.status === 'succeeded'
              ? '该运行被标记为成功，但当前没有可读的正式报告。'
              : `该运行当前的状态是 ${job.status}；正式报告只在运行成功之后才存在。`,
          },
        })
        return
      }
      const report = await api.getReport(jobId)
      if (!active) return
      if (!job.engine_run_id || report.run_id !== job.engine_run_id) {
        setLoaded({
          key: jobId,
          state: {
            kind: 'unavailable',
            note: '报告的身份与该运行的 engine run id 不一致，因此这里不显示任何结果。',
          },
        })
        return
      }
      setLoaded({ key: jobId, state: { kind: 'ready', job, report } })
    }
    load().catch(error => {
      if (active) setLoaded({ key: jobId, state: { kind: 'unavailable', note: reason(error) } })
    })
    return () => { active = false }
  }, [api, jobId])

  if (!jobId) return null
  return loaded && loaded.key === jobId ? loaded.state : { kind: 'loading' }
}

/**
 * B − A exactly, or null. The engine writes canonical decimals; a value this host
 * cannot read as one is unavailable, never zero and never a rounded figure.
 */
function delta(before: string | null | undefined, after: string | null | undefined, shift = 0) {
  if (typeof before !== 'string' || typeof after !== 'string') return null
  try {
    return exactDelta(before, after, shift)
  } catch {
    return null
  }
}

/**
 * One amount with the unit the report recorded for it, or for the run it came
 * from. When neither states one the unit stays absent — the difference below
 * refuses rather than reading a missing unit as any particular currency.
 */
const money = (
  report: BacktestReport,
  value: Amount | null | undefined,
): Money | null =>
  !value ? null : { amount: value.amount, currency: value.currency ?? report.economics.currency ?? null }

/** B − A of one amount: a difference across units is refused rather than assumed. */
function moneyDelta(left: Money | null, right: Money | null) {
  if (!left || !right) return '不可用'
  if (!left.currency || !right.currency) return '币种未记录，不做差'
  if (left.currency !== right.currency) return '币种不同，不做差'
  const value = delta(left.amount, right.amount, 0)
  return value === null ? '不可用' : `${value} ${left.currency}`
}

/** One assumption block, read from the run's own retained, admitted input. */
function blockText(value: unknown): string | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const entries = Object.entries(value as Record<string, unknown>)
  if (!entries.length) return null
  return entries
    .map(([key, item]) => `${key} ${item === null || item === undefined
      ? '未记录'
      : typeof item === 'object' ? JSON.stringify(item) : String(item)}`)
    .join(' · ')
}

/** B − A in percentage points, or unavailable. */
function pointsDelta(before: string, after: string) {
  const value = delta(before, after, 2)
  return value === null ? '不可用' : `${value} pp`
}

/** B − A of one count, or unavailable. */
function countDelta(before: number, after: number) {
  const value = delta(String(before), String(after))
  return value === null ? '不可用' : value
}

const ASSUMPTION_BLOCKS: { key: string; label: string }[] = [
  { key: 'instrument', label: '标的' },
  { key: 'data', label: '数据窗口' },
  // This block is the strategy's configured parameters, not its implementation:
  // the same strategy id with one edited quantity is a configuration difference,
  // and calling it an implementation difference would be a claim this panel cannot
  // make from two runs of one strategy.
  { key: 'strategy', label: '策略配置' },
  { key: 'execution', label: '成本假设' },
  { key: 'risk', label: '风险' },
  { key: 'funding', label: '资金' },
]

const blocksOf = (experiment: UserStrategyExperiment) => experiment.payload.normalized ?? {}

const EXPLAIN_PROMPT = [
  '解释所选证据。',
  '只依据宿主为这次调用冻结的那些运行自己的事实：它们的冻结修订、输入与报告摘要、冻结参数、'
    + '被采纳的假设、报告里允许读取的事实，以及宿主对那次运行自己冻结的准则的读数。',
  '把「宿主已验证的事实」和「你的推断」分开说：哪一句来自报告，哪一句是你自己的判断。',
  '如果这次只选了一次运行，就说明单次、单窗口的运行不能支持因果、盈利或稳健性结论；'
    + '净盈亏里包含未平仓头寸的估值，它不是已实现盈利；如果那次运行没有冻结准则，'
    + '就说它无法支持或否定这个想法。',
  '如果选了两次运行，先说明它们在数据窗口、标的、策略实现、成本假设或参数上是否可比：'
    + '不可比之处是这次比较的限制，不是结论，也不要说哪一次更好。',
  '不要运行任何东西，不要启动或确认任何实验，也不要应用任何修改。',
].join('\n')

/**
 * What the next experiment should test, against the draft the user is looking at.
 *
 * The model's own reading belongs in the reply text, because the host refuses a
 * whole answer whose tool `reason` or evidence reference is longer than its own
 * 400 UTF-8 byte limit — a refusal this panel cannot turn into a proposal. So the
 * fields this workspace really bounds are asked for as one concise hypothesis and
 * test, and the limits are stated here rather than discovered by a refusal.
 *
 * One change is asked for only when the selected runs support one: a turn may
 * answer that there is nothing worth changing. This is a prompt, not a guarantee
 * that a proposal is well founded, and it neither forbids a deliberate rerun nor
 * makes an arbitrary numeric difference count as a finding.
 */
const nextPrompt = (revision: number) => [
  '提出下一实验。',
  `针对当前草稿修订 ${revision}：如果你能说出一个值得检验的、有类型的字段修改，就用 propose_change `
    + '提出一个有类型的字段修改，写明 path、value、reason 和可选的 evidence；'
    + '如果没有值得检验的、有类型的修改，就明确说明这次没有修改，也不要为了提出修改而修改：'
    + '在正文里说清还缺哪一类证据、准则或数据，并建议用户做一个有边界的下一步'
    + '（重新选择一次运行、明确地重跑一次，或者先把要检验的问题写清楚），'
    + '由用户自己决定要不要做。',
  '这一次修改只能有一个，并且要说清它相对所选 A/B 的差异：它要检验的假设、怎么检验这个假设，'
    + '以及为什么这个差异有用、它能带来什么现在还没有的信息。'
    + '不要把已经冻结过的输入当作新的证据；也不要把任何参数改动都当作有用的研究：'
    + '只因为数值不同而不同，不构成值得检验的差异。',
  '完整解释写在回复正文里，不要写进 reason：正文用简体中文写一段约 400 个汉字以内的说明，'
    + '并把四件事分开写：（1）事实：来自哪一次运行（写明修订与 job）和报告里哪一个字段名；'
    + '（2）推断：你自己从这些事实读出的判断；（3）限制：所选运行读不出来的东西；'
    + '（4）下一个假设：这次修改要检验什么、为什么这样检验。',
  'reason 是这次修改的一句话理由，不能超过 400 字节（UTF-8），最好不超过 60 个汉字：'
    + '只写简要的假设与检验，不要复述事实、推断或限制。',
  'evidence 里的每一条引用也不能超过 400 字节（UTF-8）：只写简短的来源定位，'
    + '例如修订、job 或报告字段路径。',
  '如果相关，就诚实说明所选运行在数据窗口与运行条件上是否可比，并说明小样本、'
    + '没有预先冻结的准则、以及未平仓头寸估值这些限制：净盈亏包含未平仓头寸的估值，'
    + '它不是已实现盈利。',
  '每一个理由都要落在你这次被允许看到的、所选冻结运行的事实上；'
    + '哪一句来自报告、哪一句是你的推断，要分开说；不要排名，也不要承诺盈利、因果或稳健性。',
  '不要声称任何结果，也不要说这次实验已经运行、已经得出结论或已经更好。',
  '不要运行任何东西，不要启动实验，也不要应用任何修改：应用由用户在建议修改面板里自己决定。',
].join('\n')

/**
 * The two selected runs, the differences between them, and the two approved turns
 * that may be grounded in them. It is mounted inside the 证据 tab of the strategy
 * it belongs to, and everything it writes goes through the same update channel the
 * rest of the page uses.
 */
export function StrategyEvidenceResearch({ api, strategy, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const idBase = `research-${strategy.user_strategy_id}`
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  // Only this strategy's own created runs can be selected: a run with no job, a
  // failed launch or another strategy's run is not evidence this conversation may
  // be grounded in, and the host refuses it rather than explaining it.
  const runs = useMemo(() => selectableRuns(strategy), [strategy])

  // The address is the selection, and it is the only copy of it: a return link from
  // a report or a comparison, a refresh, the browser's own back and forward and a
  // remount of this tab all read these same two parameters, and choosing a run
  // writes them back. Nothing is remembered beside them, so this panel can never
  // show a selection the address does not state — and the report card above reads
  // that same address through the same rules, so the two cannot disagree.
  const selection = useMemo(() => readEvidenceSelection(searchParams, runs), [searchParams, runs])

  const invalid = selection.kind === 'invalid'
  // A plain strategy address keeps the selection this panel has always had — the
  // latest frozen run as A, and no B — while an address that names runs is used
  // exactly as it is written, A first. An address this panel could not verify has
  // no selection at all: it reads nothing, not even the latest run as a fallback.
  const { a: aRun, b: bRun } = selectedRuns(selection, runs)
  const aState = useRunRead(api, aRun?.job_id ?? null)
  const bState = useRunRead(api, bRun?.job_id ?? null)
  // Both actions are a request about these runs, so neither is available while a
  // selected run is still being read, cannot be read, or its report is not the
  // report of the run it was read for.
  const contextReady = !invalid && aRun !== null
    && aState?.kind === 'ready' && (bRun === null || bState?.kind === 'ready')
  const [busy, setBusy] = useState(false)

  // This panel looks at exactly one request at a time: this strategy, this draft
  // revision and these two frozen runs, in the order they are sent. It is also the
  // identity of the dispatch below, so an answer can be told apart from the answer
  // to a request the page has already moved past.
  const requestKey = [
    strategy.user_strategy_id,
    strategy.draft.revision,
    aRun?.job_id ?? '',
    bRun?.job_id ?? '',
  ].join('|')

  // The approval is that identity rather than a boolean, so a different selection,
  // revision or strategy is a different key and the box is unchecked in the same
  // render that shows it: nothing has to be retired from an effect afterwards, and
  // an approval can never outlive the request it was given for.
  const [approvedFor, setApprovedFor] = useState<string | null>(null)
  const approved = approvedFor === requestKey

  // Every arrival at an address is its own visit, including the browser's own back
  // to an address and a history key this panel already showed: the number only ever
  // grows, so a request sent during an earlier visit can never own the panel again.
  // What the departed visit was holding — the approval it was given and the busy
  // state of the request it had in flight — is retired with it, so the visit the
  // user arrives at is usable at once. That request is only ever ignored from here:
  // nothing is cancelled, re-sent or retried, and it cannot report a refusal or a
  // result into a visit it does not belong to.
  const visitAt = `${location.key}|${location.pathname}${location.search}`
  const [visit, setVisit] = useState({ at: visitAt, id: 0 })
  if (visit.at !== visitAt) {
    setVisit({ at: visitAt, id: visit.id + 1 })
    setApprovedFor(null)
    setBusy(false)
  }
  const visitId = visit.at === visitAt ? visit.id : visit.id + 1

  // Only the dispatch that is still this panel's current one may write anything, and
  // only while this panel is mounted: the answer to a request the page has moved on
  // from — a revision change, another selection, another strategy, an unmount — never
  // reaches the conversation, the refusal note or the busy state of the panel that
  // replaced it.
  const live = useRef(true)
  // The request and the visit the page is looking at as of the latest committed
  // render, and the newest dispatch of this panel: together they say whether a
  // completion still owns it. Both are written after the commit rather than during
  // render, so a completion can never read a context the page has already left —
  // and a visit that has ended never comes back, not even by returning to the same
  // address and history key.
  const dispatchKey = `${visitId}|${requestKey}`
  const shown = useRef(dispatchKey)
  useLayoutEffect(() => { shown.current = dispatchKey })
  const newest = useRef(0)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  // Choosing writes the address, and the address is what this panel reads back: the
  // approval given for the old selection is retired here rather than kept beside it,
  // and unrelated parameters already in the address stay where they are.
  const choose = (nextA: UserStrategyExperiment | null, nextB: UserStrategyExperiment | null) => {
    setApprovedFor(null)
    setSearchParams(evidenceAddress(searchParams, nextA, nextB))
  }

  // The correction for an address that cannot be verified: it removes exactly the
  // selection this panel could not check and keeps everything else, leaving the
  // default selection in its place rather than a pair the panel guessed at.
  const clearSelection = () => {
    setApprovedFor(null)
    setSearchParams(evidenceAddress(searchParams, null, null), { replace: true })
  }

  const send = async (kind: 'explain' | 'next') => {
    if (!approved) {
      failed('在询问模型之前，需要先批准一次模型调用。')
      return
    }
    const sendMessage = api.sendUserStrategyMessage
    if (!sendMessage) {
      failed('这个工作区不提供模型协作。')
      return
    }
    // The buttons already refuse this, and the request refuses it again: nothing is
    // sent while a selected run is unreadable, or while its report is not the report
    // of the run it was read for — whatever the panel happens to be showing.
    if (!contextReady) {
      failed('所选运行还没有可读、身份一致的正式报告，因此这次什么都没有发送。')
      return
    }
    const evidence = [aRun?.job_id ?? null, bRun?.job_id ?? null]
      .filter((item): item is string => Boolean(item))
    if (!evidence.length) {
      failed('请先选择一次冻结运行：没有选中的运行就没有可以解释的证据。')
      return
    }
    const strategyId = strategy.user_strategy_id
    const revision = strategy.draft.revision
    // This dispatch's own identity, fixed at the moment it is sent: what it was sent
    // for, and which dispatch of this panel it is. Buttons are disabled while it is
    // in flight, so a second click asks nothing; if a completion ever races one, only
    // the newer ticket owns the panel.
    const dispatched = dispatchKey
    const ticket = newest.current + 1
    newest.current = ticket
    const mine = () => live.current && shown.current === dispatched
      && newest.current === ticket
    setBusy(true)
    failed('')
    try {
      const updated = await sendMessage(strategyId, {
        // One click, one call id: asking the same thing again about the same
        // revision replays that call instead of spending a second one.
        call_id: `research-${kind}-${revision}-${strategy.calls.length + 1}`.slice(0, 64),
        base_revision: revision,
        text: kind === 'explain' ? EXPLAIN_PROMPT : nextPrompt(revision),
        approved_budget: RESEARCH_APPROVAL,
        // The selection travels with the request, and the host verifies every id
        // before it writes anything: this turn is told about exactly these runs.
        evidence_job_ids: evidence,
      })
      // An obsolete answer is dropped whole: it is not applied here, it does not
      // clear the approval of the selection now on screen, and it is not shown as a
      // refusal either. The strategy id is checked too, so this panel only ever
      // reports the update of the strategy it was opened on.
      if (!mine() || updated.user_strategy_id !== strategyId) return
      setApprovedFor(null)
      changed(updated)
    } catch (error) {
      if (!mine()) return
      failed(reason(error))
    } finally {
      // Only the newest dispatch releases the panel; an obsolete one leaves the busy
      // state to the request that is actually being waited on.
      if (live.current && newest.current === ticket) setBusy(false)
    }
  }

  const comparability = ASSUMPTION_BLOCKS
    .filter(block => aRun && bRun
      && blockText(blocksOf(aRun)[block.key]) !== blockText(blocksOf(bRun)[block.key]))

  const parameterPaths = aRun && bRun
    ? [...new Set([...Object.keys(aRun.payload.parameters), ...Object.keys(bRun.payload.parameters)])]
      .filter(path => aRun.payload.parameters[path] !== bRun.payload.parameters[path])
    : []

  const bothReady = aState?.kind === 'ready' && bState?.kind === 'ready'
    ? { a: aState, b: bState }
    : null
  const rows: [string, string][] = bothReady ? [
    ['期末权益', moneyDelta(
      money(bothReady.a.report, bothReady.a.report.economics.equity),
      money(bothReady.b.report, bothReady.b.report.economics.equity),
    )],
    ['净盈亏', moneyDelta(
      money(bothReady.a.report, bothReady.a.report.economics.net_pnl),
      money(bothReady.b.report, bothReady.b.report.economics.net_pnl),
    )],
    ['手续费', moneyDelta(
      money(bothReady.a.report, bothReady.a.report.economics.fees),
      money(bothReady.b.report, bothReady.b.report.economics.fees),
    )],
    ['总收益', pointsDelta(
      bothReady.a.report.economics.total_return.value,
      bothReady.b.report.economics.total_return.value,
    )],
    ['订单', countDelta(
      bothReady.a.report.economics.counts.orders,
      bothReady.b.report.economics.counts.orders,
    )],
    ['成交', countDelta(
      bothReady.a.report.economics.counts.fills,
      bothReady.b.report.economics.counts.fills,
    )],
  ] : []

  return <section aria-label="证据对比" className="strategy-panel">
    <header className="panel-head">
      <h2>证据对比</h2>
      <span className="chip">冻结运行</span>
    </header>
    <p className="muted">
      这里选择要解释的冻结运行：A 默认是最近一次运行，B 可以留空。选中的运行会随请求一起发送，
      宿主会先逐条核对它们确实是这条策略自己的、可读的冻结运行，然后这次对话只被这些运行的事实支撑。
    </p>
    {selection.kind === 'invalid'
      ? <>
          <p className="notice error" role="alert">{`无效的证据选择：${selection.note}`}</p>
          <p className="muted">
            这个地址里的选择无法核对，因此这里既不解释它，也不回退到最近一次运行，更不会发送任何请求。
            清除之后可以重新选择要解释的运行。
          </p>
          <div className="actions">
            <button type="button" onClick={clearSelection}>清除无效的证据选择</button>
          </div>
        </>
      : runs.length === 0
      ? <p className="muted">
          这条策略还没有冻结运行。在「实验」里确认一次实验、并且那次运行成功之后，这里才会出现可选的运行。
        </p>
      : <>
          <div className="create-fields">
            <div className="field">
              <label htmlFor={`${idBase}-run-a`}>运行 A</label>
              <select id={`${idBase}-run-a`} value={aRun?.launch_intent_id ?? ''} disabled={busy}
                onChange={event => {
                  // A different A that is the current B would name one run twice: the B
                  // that cannot stay is dropped here rather than sent as a duplicate.
                  const nextA = runs.find(item => item.launch_intent_id === event.target.value) ?? null
                  const nextB = bRun && bRun.launch_intent_id !== nextA?.launch_intent_id ? bRun : null
                  choose(nextA, nextB)
                }}>
                {runs.map(item => <option key={item.launch_intent_id} value={item.launch_intent_id}>
                  {`修订 ${item.payload.draft_revision} · ${item.launch_intent_id}`}
                </option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor={`${idBase}-run-b`}>运行 B</label>
              <select id={`${idBase}-run-b`} value={bRun?.launch_intent_id ?? ''} disabled={busy}
                onChange={event => {
                  const nextB = runs.find(item => item.launch_intent_id === event.target.value) ?? null
                  choose(aRun, nextB)
                }}>
                <option value="">不含 B（只解释 A）</option>
                {runs.filter(item => item.launch_intent_id !== aRun?.launch_intent_id)
                  .map(item => <option key={item.launch_intent_id} value={item.launch_intent_id}>
                    {`修订 ${item.payload.draft_revision} · ${item.launch_intent_id}`}
                  </option>)}
              </select>
            </div>
          </div>
          {runs.length < 2
            ? <p className="muted">
                这条策略只有一次冻结运行，因此没有 B 可选，也不会显示任何 A/B 差值。
              </p>
            : !bRun && <p className="muted">
                还没有选择 B：现在只解释 A，因此不显示任何 A/B 差值。
              </p>}

          {/* What the address resolved to, said before anything is asked: the two
              revisions this panel is holding, named compactly. The job ids and the
              digests stay in the disclosure below, which remains the authority. */}
          {aRun && <p className="muted">
            {`这次要解释的运行：A 是修订 ${aRun.payload.draft_revision} 的冻结运行（确认 ${aRun.launch_intent_id}）`
              + (bRun
                ? `；B 是修订 ${bRun.payload.draft_revision} 的冻结运行（确认 ${bRun.launch_intent_id}）。`
                : '；不含 B。')}
          </p>}

          {aState?.kind === 'unavailable' && <p className="notice error">{`运行 A：${aState.note}`}</p>}
          {aState?.kind === 'loading' && <p className="muted">正在读取运行 A 的状态与正式报告……</p>}
          {bRun && bState?.kind === 'unavailable' && <p className="notice error">{`运行 B：${bState.note}`}</p>}
          {bRun && bState?.kind === 'loading' && <p className="muted">正在读取运行 B 的状态与正式报告……</p>}

          {bRun && <div className="evidence-compare">
            <h3>{`B − A：修订 ${aRun?.payload.draft_revision} → 修订 ${bRun.payload.draft_revision}`}</h3>
            {bothReady
              ? <dl className="evidence-metrics">
                  {rows.map(([label, value]) => <div key={label}>
                    <dt>{label}</dt><dd>{value}</dd>
                  </div>)}
                </dl>
              : <p className="muted">
                  两次运行的正式报告没有同时可读，因此这里不显示任何 A/B 差值，也不会把缺失的值当作零。
                </p>}
            <p className="muted">
              这些数字都来自各自的正式报告，是两次运行之间的事实差异，不是因果解释，也不是谁更好的证明。
            </p>
            <h3>冻结参数差异</h3>
            {parameterPaths.length === 0
              ? <p className="muted">两次运行的冻结参数逐项相同。</p>
              : <ul className="path-list">
                  {parameterPaths.map(path => <li key={path}>
                    {`parameters.${path}：${aRun?.payload.parameters[path] ?? '未记录'} → ${bRun.payload.parameters[path] ?? '未记录'}`}
                  </li>)}
                </ul>}
            <h3>冻结假设差异</h3>
            {comparability.length === 0
              ? <p className="muted">两次运行的冻结假设逐块相同。</p>
              : <dl className="summary-list">
                  {comparability.map(block => <div key={block.key}>
                    <dt>{block.label}</dt>
                    <dd>{`A ${blockText(aRun ? blocksOf(aRun)[block.key] : null) ?? '未记录'} · B ${blockText(blocksOf(bRun)[block.key]) ?? '未记录'}`}</dd>
                  </div>)}
                </dl>}
            {comparability.length === 0
              ? <p className="muted">
                  两次运行的假设一致，但它们的差异仍然只是两次运行之间的差异：一次运行不构成因果或稳健性证明。
                </p>
              : <p className="notice">
                  {`这两次运行在${comparability.map(block => block.label).join('、')}上不同，因此它们的差异首先是一个可比性问题：`
                    + '这些差别让「哪一次更好」无法从这两次运行读出来，这里也不排名、不选优。'}
                </p>}
            {aRun?.job_id && bRun.job_id && <p>
              <Link className="button-link"
                to={`/backtests/compare/${encodeURIComponent(aRun.job_id)}/${encodeURIComponent(bRun.job_id)}`}>
                在既有比较页面打开这两次运行
              </Link>
            </p>}
          </div>}

          <details className="provenance">
            <summary>所选运行的身份与摘要</summary>
            <dl className="summary-list">
              {[aRun, bRun].filter((item): item is UserStrategyExperiment => Boolean(item))
                .map((item, index) => <div key={item.launch_intent_id}>
                  <dt>{index === 0 ? '运行 A' : '运行 B'}</dt>
                  <dd>{`修订 ${item.payload.draft_revision} · 确认 ${item.launch_intent_id} · 运行 ${item.job_id}`
                    + ` · 输入 ${item.payload.input_sha256?.slice(0, 16) ?? '未记录'}`}</dd>
                </div>)}
            </dl>
          </details>

          <label className="check" htmlFor={`${idBase}-approve`}>
            <input id={`${idBase}-approve`} type="checkbox" checked={approved}
              disabled={busy || !contextReady}
              onChange={event => { setApprovedFor(event.target.checked ? requestKey : null); failed('') }} />
            批准一次模型调用
          </label>
          <div className="actions">
            <button type="button" onClick={() => { void send('explain') }}
              disabled={busy || !approved || !contextReady}>解释所选证据</button>
            <button type="button" className="primary" onClick={() => { void send('next') }}
              disabled={busy || !approved || !contextReady}>提出下一实验</button>
          </div>
          <p className="muted">
            这两个动作各需要一次新的批准，各自只允许一次模型调用、不允许任何回测。它们把选中的运行
            作为证据发给既有对话：解释只说报告里的读数与模型的推断，「提出下一实验」只是针对当前草稿
            的有类型修改建议。两者都不会预览、启动或应用任何东西。
          </p>
          <p className="muted">
            「提出下一实验」允许诚实地回答「这次没有修改」：如果所选运行支撑不起任何值得检验的、
            有类型的改动，说清还缺哪一类证据、准则或数据，以及下一步能做什么，比提出一个可有可无的
            改动更有用。这不改变发送的门槛：每一次发送仍然各需要一次新的批准，而且只允许一次模型调用、
            不允许任何回测。
          </p>
        </>}
  </section>
}
