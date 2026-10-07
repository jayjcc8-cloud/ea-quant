/**
 * QUINE: one durable user strategy, the AI work the user approved, and its frozen runs.
 *
 * The page is two areas and one owner. The centre is every strategy in this
 * workspace — the compact table, then the selected strategy's own detail with its
 * facts strip and its sections; the right column is the AI conversation, which is
 * always the conversation of the strategy that is open, and the typed change the
 * model proposed against it.
 *
 * Three rules this file exists to keep visible:
 *
 * * creating a strategy is a save — it calls no model and starts no backtest;
 * * one model call happens only after an explicit approval click, and everything a
 *   model says is labelled inference, never evidence;
 * * an experiment starts only from an explicit confirmation of facts the user just
 *   read, and the run itself lives on the existing backtest and report pages.
 *
 * The evidence section reads the run's own job, report and equity path through the
 * existing ApiAdapter and refuses to show a report whose identity does not match
 * the run it was read for. Nothing here computes a metric the engine did not write.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, BacktestJob, BacktestReport, ScenarioSummary } from './app'
import type { ReportCriterion } from './research-api'
import { EquityCurve, pathPercent, type EquityPath } from './equity-path'
import { exactDelta } from './decimal'
import {
  UserStrategyRequestError,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyDraft,
  type UserStrategyExperiment,
  type UserStrategyPreview,
  type UserStrategyProposal,
  type UserStrategyProposalStatus,
} from './user-strategies-api'

/**
 * The allowlisted criterion fields, in the report's own paths. This list is
 * presentation only: the host owns the vocabulary and refuses a field it does not
 * know, so a field this map has never heard of is shown exactly as it was written.
 */
const CRITERION_FIELDS: { field: string; label: string }[] = [
  { field: 'economics.net_pnl.amount', label: '净盈亏' },
  { field: 'economics.realized_pnl.amount', label: '已实现盈亏' },
  { field: 'economics.fees.amount', label: '手续费' },
  { field: 'economics.total_return.value', label: '总收益' },
  { field: 'economics.counts.fills', label: '成交笔数' },
  { field: 'economics.counts.orders', label: '订单数' },
  { field: 'economics.completed_round_trips', label: '完成回合数' },
]
const EXPECTATIONS = ['positive', 'negative', 'zero'] as const
const EXPECTATION_LABELS: Record<string, string> = { positive: '为正', negative: '为负', zero: '为零' }

/** One approval allows exactly one model call, and no experiment. */
const CALL_APPROVAL: ApprovedBudget = { max_model_calls: 1, max_backtests: 0 }
/** One confirmation allows exactly one experiment, and no model call. */
const RUN_APPROVAL: ApprovedBudget = { max_model_calls: 0, max_backtests: 1 }

/** What the host's own refusal means, in the user's words; the host's message stays visible. */
const REQUEST_EXPLANATIONS: Record<string, string> = {
  stale_base_revision: '草稿修订已前进，这次修改没有被应用。请重新加载该策略后再试。',
  operation_conflict: '该操作编号已绑定到另一个修改，因此什么都没有应用。',
  approval_required: '这里需要一次明确的批准，而且它必须在该工作区的预算之内。',
  user_strategy_not_found: '该策略不在这个工作区里。请重新加载策略列表。',
  request_failed: '本地服务拒绝了这次请求，什么都没有改变。',
}

const CALL_STATUS_LABELS: Record<UserStrategyCall['status'], string> = {
  pending: '已记录，尚未发送',
  dispatched: '已询问，结果尚未观测',
  observed: '已观测',
  refused: '被拒绝',
  unconfigured: '未配置',
  unknown: '结果未知',
  interrupted_before_dispatch: '发送前已中断',
}

const PROPOSAL_STATUS_LABELS: Record<UserStrategyProposalStatus, string> = {
  open: '待处理',
  applied: '已应用',
  rejected: '已拒绝',
  superseded: '已被取代',
}

const EXPERIMENT_NOTES: Record<string, string> = {
  pending: '这次确认在运行存在之前就被记录了，什么都没有派发。',
  interrupted_before_dispatch: '这次确认从未派发过运行，也没有创建重复的运行。',
  unknown: '中断之前可能已经派发了一次运行。在找到它之前，它不会被重发、不会被计数、也不会被退还。',
  failed: '这次实验在运行存在之前就被拒绝了。',
}

const EXPERIMENT_STATUS_LABELS: Record<UserStrategyExperiment['status'], string> = {
  pending: '已记录，未派发',
  created: '运行已启动',
  failed: '派发前被拒绝',
  unknown: '结果未知',
  interrupted_before_dispatch: '从未派发',
}

const explain = (error: unknown) => {
  if (error instanceof UserStrategyRequestError) {
    const explanation = REQUEST_EXPLANATIONS[error.code]
    return explanation ? `${explanation} · 服务端：${error.message}` : `服务端：${error.message}`
  }
  return error instanceof Error ? error.message : '这次请求失败了，什么都没有改变。'
}

const strategyLine = (draft: UserStrategyDraft) =>
  `${draft.strategy_id} · ${draft.venue}:${draft.symbol}`

const revisionLabel = (revision: number) => `草稿修订 ${revision}`

/** The criterion exactly as the report names it: schema, path, expectation. */
const criterionLine = (criterion: ReportCriterion | null) =>
  criterion === null
    ? '未声明准则，因此这次运行无法支持或否定该想法'
    : `${criterion.schema} ${criterion.field} ${criterion.expectation}`

const criterionLabel = (criterion: ReportCriterion | null) => {
  if (criterion === null) return '未声明准则'
  const known = CRITERION_FIELDS.find(item => item.field === criterion.field)
  const name = known ? known.label : criterion.field
  const sign = EXPECTATION_LABELS[criterion.expectation]
  return sign ? `${name} 预期${sign}` : `${name} 预期 ${criterion.expectation}`
}

const fingerprintOf = (data: Record<string, unknown>) => {
  const fingerprint = data?.fingerprint as { sha256?: string } | undefined
  return typeof fingerprint?.sha256 === 'string' ? fingerprint.sha256 : null
}

const dataLine = (preview: UserStrategyPreview) => {
  const sha = fingerprintOf(preview.data)
  if (preview.record_count === null && sha === null) return '数据身份不可用'
  return `${preview.record_count ?? '未知'} 条记录${sha ? ` · ${sha.slice(0, 8)}` : ''}`
}

/**
 * One cost assumption exactly as the scenario declares it. A scenario that
 * declares none says so; nothing is filled in, guessed or priced here.
 */
const factLine = (value: unknown) => {
  if (value === null || value === undefined) return '该场景未声明'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([key, item]) => `${key} ${String(item)}`).join(', ') : '声明为空'
  }
  return String(value)
}

const costLine = (preview: UserStrategyPreview) =>
  `手续费 ${factLine(preview.commission)} · 滑点 ${factLine(preview.slippage)} · 延迟 ${factLine(preview.latency)}`

const usageLine = (call: UserStrategyCall) =>
  call.usage_available
    ? `输入 ${call.input_tokens ?? 0} tokens · 输出 ${call.output_tokens ?? 0} tokens`
    : '这次调用没有上报 token 用量，因此不会按零计费'

/** A report percentage, read from the report's own canonical decimal. */
const percent = (value: string) => `${exactDelta('0', value, 2).replace(/^\+/, '')}%`

/**
 * What to do next when a call did not produce an answer. Unconfigured and unknown
 * are different states: one has a provider to set up, the other may already have
 * reached one and is never re-sent silently.
 */
function CallNextStep({ call }: { call: UserStrategyCall }) {
  if (call.status === 'unconfigured') {
    return <p className="muted">
      这里没有配置模型提供方，因此这次调用只被记录、从未发送。请在
      <Link to="/settings"> 设置</Link> 中配置提供方后再问一次——那是一次新的调用，需要新的批准。
    </p>
  }
  if (call.status === 'unknown') {
    return <p className="muted">
      这次调用可能在中断之前已经到达提供方，因此不会被重发，也不会被退还。如果你想要一个答案，
      请明确批准一次新的调用。如果提供方不是你预期的那个，请检查
      <Link to="/settings"> 设置</Link>。
    </p>
  }
  return null
}

/**
 * Create one strategy. This is a save: the scenario is read to freeze a valid
 * starting draft, and no provider, job or child experiment is touched. The form is
 * a compact disclosure rather than a page: creating one strategy selects it, and
 * the conversation for it opens immediately.
 */
function CreateStrategy({ api, scenarios, created }: {
  api: ApiAdapter
  scenarios: ScenarioSummary[]
  created: (strategy: UserStrategy) => void
}) {
  const [name, setName] = useState('')
  const [intent, setIntent] = useState('')
  const [scenarioId, setScenarioId] = useState('')
  const [field, setField] = useState('')
  const [expectation, setExpectation] = useState<string>('positive')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const usable = scenarios.filter(item => item.valid && item.summary && item.scenario_id)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    // Captured once: the approved action is the one this click was made with.
    const createStrategy = api.createUserStrategy
    if (!createStrategy) {
      setNote('这个工作区不提供创建策略。')
      return
    }
    setBusy(true)
    setNote('')
    try {
      const strategy = await createStrategy({
        name: name.trim(),
        intent: intent.trim(),
        scenario_id: scenarioId,
        // The host validates this against the report's own vocabulary: what the
        // creator states is a field and a sign, and what is stored is the complete
        // validated criterion the host froze.
        ...(field ? { criterion: { field, expectation } } : {}),
      })
      setName('')
      setIntent('')
      setField('')
      created(strategy)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="新建策略" className="strategy-create">
    <form onSubmit={submit}>
      <div className="create-fields">
        <div className="field">
          <label htmlFor="strategy-name">策略名称</label>
          <input id="strategy-name" value={name} maxLength={120} required
            onChange={event => setName(event.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="strategy-scenario">场景</label>
          <select id="strategy-scenario" value={scenarioId} required
            onChange={event => setScenarioId(event.target.value)}>
            <option value="">选择一个已注册场景</option>
            {usable.map(item => <option key={item.scenario_id} value={item.scenario_id}>
              {`${item.summary?.strategy_id} · ${item.summary?.venue}:${item.summary?.symbol}`}
            </option>)}
          </select>
          {!usable.length && <small>当前没有可用的有效注册场景。</small>}
        </div>
      </div>
      <div className="field">
        <label htmlFor="strategy-intent">意图</label>
        <textarea id="strategy-intent" value={intent} maxLength={2000} required rows={2}
          onChange={event => setIntent(event.target.value)} />
      </div>
      <div className="create-fields">
        <div className="field">
          <label htmlFor="strategy-criterion-field">准则字段</label>
          <select id="strategy-criterion-field" value={field}
            onChange={event => setField(event.target.value)}>
            <option value="">不声明准则</option>
            {CRITERION_FIELDS.map(item => <option key={item.field} value={item.field}>{item.label}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="strategy-criterion-expectation">预期方向</label>
          <select id="strategy-criterion-expectation" value={expectation} disabled={!field}
            onChange={event => setExpectation(event.target.value)}>
            {EXPECTATIONS.map(item => <option key={item} value={item}>{EXPECTATION_LABELS[item]}</option>)}
          </select>
        </div>
      </div>
      <div className="create-actions">
        <button type="submit" className="primary" disabled={busy || !name.trim() || !intent.trim() || !scenarioId}>
          创建策略
        </button>
        <p className="muted">创建策略只保存草稿：不调用模型，也不启动回测。</p>
      </div>
      {note && <p role="alert" className="notice error">{note}</p>}
    </form>
  </section>
}

/**
 * The frozen draft's own facts, always visible under the selected strategy: the
 * revision, the strategy and instrument, the funding and the criterion. The detail
 * sections below it are read against exactly this draft.
 */
function DraftFacts({ draft }: { draft: UserStrategyDraft }) {
  return <section aria-label="草稿" className="draft-facts">
    <dl>
      <div><dt>工作版本</dt><dd>{revisionLabel(draft.revision)}</dd></div>
      <div><dt>策略与标的</dt><dd>{strategyLine(draft)}</dd></div>
      <div><dt>初始资金</dt><dd>{`${draft.initial_cash} USD`}</dd></div>
      <div><dt>准则</dt><dd>{criterionLabel(draft.criterion)}</dd></div>
    </dl>
  </section>
}

type TabKey = 'intent' | 'rules' | 'experiments' | 'evidence' | 'revisions' | 'candidates'

const DETAIL_TABS: { key: TabKey; label: string }[] = [
  { key: 'intent', label: '意图' },
  { key: 'rules', label: '规则与假设' },
  { key: 'experiments', label: '实验' },
  { key: 'evidence', label: '证据' },
  { key: 'revisions', label: '修订' },
  { key: 'candidates', label: '候选' },
]

function DetailTabs({ active, select, idBase }: {
  active: TabKey
  select: (key: TabKey) => void
  idBase: string
}) {
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = DETAIL_TABS.findIndex(item => item.key === active)
    const next = event.key === 'ArrowRight' ? index + 1
      : event.key === 'ArrowLeft' ? index - 1
        : event.key === 'Home' ? 0
          : event.key === 'End' ? DETAIL_TABS.length - 1
            : -1
    if (next < 0) return
    event.preventDefault()
    const key = DETAIL_TABS[(next + DETAIL_TABS.length) % DETAIL_TABS.length].key
    select(key)
    document.getElementById(`${idBase}-tab-${key}`)?.focus()
  }
  return <div className="detail-tabs" role="tablist" aria-label="策略详情" onKeyDown={move}>
    {DETAIL_TABS.map(item => <button key={item.key} type="button" role="tab"
      id={`${idBase}-tab-${item.key}`}
      aria-selected={item.key === active}
      aria-controls={`${idBase}-panel-${item.key}`}
      tabIndex={item.key === active ? 0 : -1}
      onClick={() => select(item.key)}>{item.label}</button>)}
  </div>
}

function IntentSection({ strategy }: { strategy: UserStrategy }) {
  const latest = strategy.experiments.at(-1)
  return <section aria-label="意图" className="strategy-panel">
    <h2>意图</h2>
    <p className="strategy-intent">{strategy.intent}</p>
    <p className="muted">{`创建于 ${strategy.created_at} · 最后更新 ${strategy.updated_at}`}</p>
    {latest
      ? <p className="muted">
          {'最近一次确认的实验：'}
          {latest.job_id
            ? <Link to={`/backtests/${encodeURIComponent(latest.job_id)}`}>
                {`修订 ${latest.payload.draft_revision} · ${EXPERIMENT_STATUS_LABELS[latest.status]}`}
              </Link>
            : `修订 ${latest.payload.draft_revision} · ${EXPERIMENT_STATUS_LABELS[latest.status]}`}
        </p>
      : <p className="muted">还没有确认过的实验。实验在「实验」里预览并确认，一次确认只允许一次运行。</p>}
    <p className="muted">创建只保存草稿；一次批准只允许一次模型调用；一次确认只允许一次实验。</p>
  </section>
}

function RulesSection({ draft }: { draft: UserStrategyDraft }) {
  return <section aria-label="规则与假设" className="strategy-panel">
    <h2>规则与假设</h2>
    <p>{`准则 ${criterionLine(draft.criterion)}`}</p>
    <p className="muted">准则由宿主按报告的字段词表冻结；一次运行只对着它自己冻结的修订被解读。</p>
    <dl className="summary-list">
      <div><dt>策略标识</dt><dd>{draft.strategy_id}</dd></div>
      <div><dt>标的</dt><dd>{`${draft.venue}:${draft.symbol}`}</dd></div>
      <div><dt>场景</dt><dd>{draft.scenario_id}</dd></div>
      <div><dt>初始资金</dt><dd>{`${draft.initial_cash} USD`}</dd></div>
      <div><dt>数据来源</dt><dd>{
        draft.dataset_id
          ? `${draft.dataset_id}${draft.source_sha256 ? ` · ${draft.source_sha256.slice(0, 12)}` : ''}`
          : '本地注册场景文件，没有上传来源'
      }</dd></div>
      <div><dt>注册参数</dt><dd>{
        Object.entries(draft.parameters).map(([key, value]) => `${key}: ${value}`).join(' · ') || '没有注册参数'
      }</dd></div>
    </dl>
    <p className="muted">成本假设是场景自己的声明，只在实验预览里按它声明的方式读出，这里不补写任何价格。</p>
  </section>
}

function RevisionsSection({ strategy }: { strategy: UserStrategy }) {
  return <section aria-label="修订" className="strategy-panel">
    <h2>修订</h2>
    {strategy.changes.length === 0
      ? <p className="muted">还没有已应用的修改。被拒绝的建议不会写入修订。</p>
      : <ol className="change-list">
          {strategy.changes.map(item => <li key={item.operation_id}>
            <p className="change-head">
              <span className={item.kind === 'apply' ? 'chip chip-draft' : 'chip'}>
                {item.kind === 'apply' ? '应用' : '撤销'}
              </span>
              <span className="muted">{`修订 ${item.from_revision} → ${item.to_revision} · ${item.at}`}</span>
            </p>
            {item.applied_paths.length > 0 && <ul className="path-list">
              {item.applied_paths.map(path => <li key={path}>{path}</li>)}
            </ul>}
            {item.stale_paths.length > 0 && <p className="muted">{`未生效的字段：${item.stale_paths.join(', ')}`}</p>}
            <p className="muted">{`提议者：${item.proposer}`}</p>
            {item.evidence.length > 0 && <p className="muted">
              {`模型引用（声明，非已验证事实）：${item.evidence.join(', ')}`}
            </p>}
          </li>)}
        </ol>}
    {strategy.history.length > 0 && <details className="provenance">
      <summary>{`历史修订（${strategy.history.length}）`}</summary>
      <ul className="history-list">
        {strategy.history.map(revision => <li key={revision.revision}>
          <p>{revisionLabel(revision.revision)}</p>
          <p className="muted">{`${strategyLine(revision)} · 初始资金 ${revision.initial_cash} USD · ${criterionLabel(revision.criterion)}`}</p>
        </li>)}
      </ul>
    </details>}
  </section>
}

function CandidatesSection({ strategy }: { strategy: UserStrategy }) {
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  return <section aria-label="候选" className="strategy-panel">
    <h2>候选</h2>
    <p className="muted">候选与决策在既有页面上完成：一次运行的结果可以在那里被提升为候选，并记录你的决定。</p>
    <p><Link to="/candidates">打开候选页面</Link></p>
    {!hasRun && <p className="muted">这条策略还没有冻结运行，因此还没有可提升的证据。</p>}
  </section>
}

type EvidenceState =
  | { kind: 'empty' }
  | { kind: 'loading' }
  | { kind: 'unavailable'; note: string }
  | { kind: 'ready'; job: BacktestJob; report: BacktestReport; path: EquityPath | null; pathNote: string | null }

/** The report's own figures, printed as the report wrote them. */
function EvidenceMetrics({ report }: { report: BacktestReport }) {
  const economics = report.economics
  const unit = (value: { amount: string; currency?: string } | undefined) => {
    if (!value) return '本次报告未记录'
    const currency = value.currency ?? economics.currency
    return currency ? `${value.amount} ${currency}` : `${value.amount} · 报告未记录币种`
  }
  const rows: [string, string][] = [
    ['期末权益', unit(economics.equity)],
    ['净盈亏', unit(economics.net_pnl)],
    ['总收益', percent(economics.total_return.value)],
    ['手续费', unit(economics.fees)],
    ['订单 / 成交', `${economics.counts.orders} / ${economics.counts.fills}`],
  ]
  if ('completed_round_trips' in economics) rows.push(['完成回合数', String(economics.completed_round_trips)])
  return <dl className="evidence-metrics">
    {rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
  </dl>
}

/**
 * The evidence of one frozen experiment: its real job, its formal report and its
 * equity path, read through the existing adapter. The report is only shown when its
 * run id is the run it was read for; a missing, refused or mismatched report is
 * said out loud rather than replaced by a figure this page would have to invent.
 */
function EvidenceSection({ api, strategy }: { api: ApiAdapter; strategy: UserStrategy }) {
  const runs = useMemo(
    () => strategy.experiments.filter(item => item.status === 'created' && item.job_id),
    [strategy],
  )
  const [intent, setIntent] = useState(runs.at(-1)?.launch_intent_id ?? '')
  const experiment = runs.find(item => item.launch_intent_id === intent) ?? runs.at(-1)
  const jobId = experiment?.job_id ?? null
  // What was read is kept with the run it was read for. Nothing of another run is
  // ever shown as this one's — not even for the moment a new read is in flight —
  // so changing the selected frozen run shows that run's own load, never the
  // previous run's ready report.
  const [loaded, setLoaded] = useState<{ key: string; state: EvidenceState } | null>(null)
  const state: EvidenceState = jobId === null
    ? { kind: 'empty' }
    : loaded && loaded.key === jobId
      ? loaded.state
      : { kind: 'loading' }

  useEffect(() => {
    // Every update below happens after an awaited read, so a read that is no longer
    // current — because the run changed or the section went away — writes nothing.
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
              : `该运行当前的状态是 ${job.status}；正式报告只在运行成功之后才存在，启动运行不等于有报告。`,
          },
        })
        return
      }
      const report = await api.getReport(jobId)
      if (!active) return
      // The report is the authority only for the run it belongs to.
      if (!job.engine_run_id || report.run_id !== job.engine_run_id) {
        setLoaded({
          key: jobId,
          state: { kind: 'unavailable', note: '报告的身份与该运行的 engine run id 不一致，因此这里不显示任何结果。' },
        })
        return
      }
      let path: EquityPath | null = null
      let pathNote: string | null = null
      try {
        const loadedPath = await api.getEquityPath(jobId)
        // The path is shown only when its own identity is this run's and this report's.
        if (loadedPath.run_id === job.engine_run_id && loadedPath.report_sha256 === job.report_sha256) path = loadedPath
        else pathNote = '权益曲线的身份与该报告不一致，因此不显示曲线。'
      } catch {
        pathNote = '该运行没有可读的权益曲线。'
      }
      if (!active) return
      setLoaded({ key: jobId, state: { kind: 'ready', job, report, path, pathNote } })
    }
    load().catch(error => {
      if (active) setLoaded({ key: jobId, state: { kind: 'unavailable', note: explain(error) } })
    })
    return () => { active = false }
  }, [api, jobId])

  return <section aria-label="证据" className="strategy-panel strategy-evidence">
    <header className="panel-head">
      <h2>证据</h2>
      {runs.length > 1 && <label className="inline-field">冻结运行
        <select aria-label="选择冻结运行" value={intent} onChange={event => setIntent(event.target.value)}>
          {runs.map(item => <option key={item.launch_intent_id} value={item.launch_intent_id}>
            {`修订 ${item.payload.draft_revision} · ${item.launch_intent_id}`}
          </option>)}
        </select>
      </label>}
    </header>
    {state.kind === 'empty' && <p className="muted">
      还没有冻结运行。在「实验」里确认一次实验之后，这里会显示那次运行自己的正式报告——只有运行成功，报告才存在。
    </p>}
    {state.kind === 'loading' && <p className="muted">正在读取该运行的状态与正式报告……</p>}
    {state.kind === 'unavailable' && <p className="notice error">{state.note}</p>}
    {state.kind === 'ready' && <>
      <p className="evidence-origin">
        <span className="chip">事实 · 引擎</span>
        <span className="muted">{`正式报告由引擎产出，不是模型输出 · 运行状态 ${state.job.status}`}</span>
      </p>
      <EvidenceMetrics report={state.report} />
      {state.path
        ? <div className="evidence-curve"><EquityCurve analysis={state.path} /></div>
        : <p className="muted">{state.pathNote ?? '该运行没有可读的权益曲线。'}</p>}
      <div className="actions">
        <Link className="button-link" to={`/backtests/${encodeURIComponent(state.job.job_id)}`}>打开运行页面</Link>
        <a className="button-link" href={api.artifactUrl(state.job.job_id, 'report.json')}>下载 report.json</a>
        <a className="button-link" href={api.artifactUrl(state.job.job_id, 'summary.txt')}>下载 summary.txt</a>
        {state.path && <a className="button-link" href={api.artifactUrl(state.job.job_id, 'equity-path.json')}>下载 equity-path.json</a>}
      </div>
      <details className="provenance">
        <summary>技术细节与来源</summary>
        <dl className="summary-list">
          <div><dt>Web 任务</dt><dd>{state.job.job_id}</dd></div>
          <div><dt>Engine 运行</dt><dd>{state.job.engine_run_id ?? '未记录'}</dd></div>
          <div><dt>报告 schema</dt><dd>{state.report.schema}</dd></div>
          <div><dt>报告 SHA-256</dt><dd>{state.job.report_sha256 ?? '未记录'}</dd></div>
          <div><dt>输入 SHA-256</dt><dd>{state.job.input_sha256 ?? '未记录'}</dd></div>
          <div><dt>最大回撤（曲线）</dt><dd>{state.path ? `${state.path.max_drawdown.amount} ${state.path.currency} / ${pathPercent(state.path.max_drawdown.ratio)}` : '不可用'}</dd></div>
        </dl>
      </details>
    </>}
  </section>
}

/** A section of the selected strategy: the tab bar and the section it opens. */
function StrategyDetail({ api, strategy, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  // The detail opens on the evidence of a frozen run when there is one, and on the
  // intent when there is not: an honest empty state rather than a hidden form.
  const [tab, setTab] = useState<TabKey>(hasRun ? 'evidence' : 'intent')
  const idBase = `detail-${strategy.user_strategy_id}`
  return <section className="strategy-detail" aria-label="策略详情">
    <header className="strategy-title">
      <h2>{strategy.name}</h2>
      <span className="chip chip-draft">{`v${strategy.draft.revision} 草稿`}</span>
      <Link className="strategy-open" to={`/strategies/${encodeURIComponent(strategy.user_strategy_id)}`}>
        打开策略
      </Link>
    </header>
    <DraftFacts draft={strategy.draft} />
    <DetailTabs active={tab} select={setTab} idBase={idBase} />
    <div className="strategy-tabpanel" role="tabpanel" id={`${idBase}-panel-${tab}`}
      aria-labelledby={`${idBase}-tab-${tab}`} tabIndex={0}>
      {tab === 'intent' && <IntentSection strategy={strategy} />}
      {tab === 'rules' && <RulesSection draft={strategy.draft} />}
      {tab === 'experiments' && <ExperimentPanel
        api={api} strategy={strategy} changed={changed} failed={failed} />}
      {tab === 'evidence' && <EvidenceSection api={api} strategy={strategy} />}
      {tab === 'revisions' && <RevisionsSection strategy={strategy} />}
      {tab === 'candidates' && <CandidatesSection strategy={strategy} />}
    </div>
  </section>
}

/**
 * The AI conversation: approved calls, labelled inference, no implied authority.
 * The header names the strategy the conversation belongs to and switches between
 * the real strategies of this workspace.
 */
function ChatPanel({ api, strategy, strategies, changed }: {
  api: ApiAdapter
  strategy: UserStrategy
  strategies: UserStrategy[]
  changed: (strategy: UserStrategy) => void
}) {
  const navigate = useNavigate()
  const [message, setMessage] = useState('')
  const [approved, setApproved] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const latest = strategy.calls.at(-1)
  const unresolved = strategy.calls.some(call =>
    call.status === 'pending' || call.status === 'dispatched')

  const ask = async () => {
    if (!approved) {
      setNote('在询问模型之前，需要先批准一次模型调用。')
      return
    }
    const send = api.sendUserStrategyMessage
    if (!send) {
      setNote('这个工作区不提供模型协作。')
      return
    }
    setBusy(true)
    setNote('')
    try {
      const updated = await send(strategy.user_strategy_id, {
        call_id: `call-${strategy.calls.length + 1}`,
        base_revision: strategy.draft.revision,
        text: message.trim(),
        approved_budget: CALL_APPROVAL,
      })
      changed(updated)
      setMessage('')
      setApproved(false)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="AI 协作" className="strategy-panel strategy-chat">
    <header className="chat-head">
      <h2>AI 协作</h2>
      {strategies.length > 0 && <label className="inline-field">共同编辑
        <select aria-label="当前策略" value={strategy.user_strategy_id}
          onChange={event => navigate(`/strategies/${encodeURIComponent(event.target.value)}`)}>
          {strategies.map(item => <option key={item.user_strategy_id} value={item.user_strategy_id}>
            {item.name}
          </option>)}
        </select>
      </label>}
    </header>
    {strategy.messages.length > 0 && <ol className="message-list">
      {strategy.messages.map(item => <li key={item.message_id} className={`message message-${item.role}`}>
        <p className="message-role">{item.role === 'model' ? 'QUINE' : '你'} <span className="muted">{item.at}</span></p>
        <p>{item.text}</p>
        {item.role === 'model' && <p className="muted">模型推断 — 不是正式证据。</p>}
      </li>)}
    </ol>}
    {latest && <div className="call-note">
      <p><span className={`status status-${latest.status}`}>{CALL_STATUS_LABELS[latest.status]}</span></p>
      <p className="muted">{usageLine(latest)}</p>
      {latest.cost && !latest.cost.available &&
        <p className="muted">{`成本不可用：${latest.cost.reason}。`}</p>}
      {latest.message && <p className="notice">{latest.message}</p>}
      <CallNextStep call={latest} />
    </div>}
    {unresolved && <p className="notice">
      还有一次已记录的调用没有结论。在它落实之前不会再问下一次，因此不会覆盖任何东西。
    </p>}
    <div className="composer">
      <div className="field">
        <label htmlFor="strategy-message">消息</label>
        <textarea id="strategy-message" rows={3} value={message} maxLength={4000}
          onChange={event => setMessage(event.target.value)} />
      </div>
      <label className="check" htmlFor="strategy-approve">
        <input id="strategy-approve" type="checkbox" checked={approved}
          onChange={event => { setApproved(event.target.checked); setNote('') }} />
        批准一次模型调用
      </label>
      <div className="composer-actions">
        <p className="muted">一次批准只允许针对这个草稿修订的一次调用。模型可以解释，也可以提出一次有类型的修改，但它不运行任何东西，也不决定结果。</p>
        <button type="button" className="primary" onClick={ask} disabled={busy || !message.trim()}>询问模型</button>
      </div>
      {note && <p role="alert" className="notice error">{note}</p>}
    </div>
  </section>
}

/** The AI column before anything is open: it explains what it will hold. */
function EmptyChat() {
  return <section aria-label="AI 协作" className="strategy-panel strategy-chat strategy-chat-empty">
    <header className="chat-head"><h2>AI 协作</h2></header>
    <p className="muted">AI 提出建议，证据做出判断。</p>
    <p>先创建或选择一条策略。这里会显示该策略自己的对话、模型针对它提出的有类型修改，以及每次修改的应用记录。</p>
    <p className="muted">创建策略只保存草稿：不调用模型，也不启动回测。整个对话始终属于当前打开的那一条策略。</p>
  </section>
}

/**
 * One typed proposal: the checked subset is exactly what Apply sends.
 *
 * This panel is mounted per proposal identity and per revision, so the checked
 * set and the approval below always belong to the proposal on screen: switching
 * strategy or revision mounts a fresh panel instead of carrying a stale
 * selection, a stale approval or a stale undo target across.
 */
function ProposalPanel({ api, strategy, proposal, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  proposal: UserStrategyProposal
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const [checked, setChecked] = useState<string[]>(proposal.items.map(item => item.path))
  const [reviseMessage, setReviseMessage] = useState('')
  const [reviseApproved, setReviseApproved] = useState(false)
  const attempts = useRef(0)
  const [busy, setBusy] = useState(false)

  const open = proposal.status === 'open'
  const selected = proposal.items.map(item => item.path).filter(path => checked.includes(path))

  const toggle = (path: string, on: boolean) =>
    setChecked(current => on ? [...current, path] : current.filter(item => item !== path))

  const apply = async () => {
    if (!selected.length) {
      failed('至少选择一个字段再应用，或者改为拒绝这条建议。')
      return
    }
    const applyChange = api.applyUserStrategyProposal
    if (!applyChange) {
      failed('这个工作区不提供应用修改。')
      return
    }
    setBusy(true)
    failed('')
    const attempt = attempts.current
    try {
      // One intent, one operation id: a retry of this click replays it instead of
      // applying a second change.
      const updated = await applyChange(
        strategy.user_strategy_id,
        proposal.proposal_id,
        { operation_id: `op-${proposal.proposal_id}-${proposal.base_revision}-${attempt}`.slice(0, 64), paths: selected },
      )
      attempts.current += 1
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const reject = async () => {
    const rejectChange = api.rejectUserStrategyProposal
    if (!rejectChange) return
    setBusy(true)
    failed('')
    try {
      changed(await rejectChange(strategy.user_strategy_id, proposal.proposal_id))
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const undo = async () => {
    const undoChange = api.undoUserStrategyChange
    if (!undoChange) return
    // The change this click means, named exactly: the latest application of the
    // revision on screen. A stale page cannot undo whatever happens to be newest.
    const target = strategy.changes.filter(item => item.kind === 'apply').at(-1)
    if (!target || target.to_revision !== strategy.draft.revision) {
      failed('当前修订上没有可撤销的已应用修改。')
      return
    }
    setBusy(true)
    failed('')
    try {
      changed(await undoChange(strategy.user_strategy_id, {
        operation_id: `undo-${target.operation_id}-${strategy.draft.revision}`.slice(0, 64),
        head_revision: strategy.draft.revision,
        target_operation_id: target.operation_id,
      }))
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const revise = async () => {
    if (!reviseApproved) {
      failed('在修改这条建议之前，需要先批准一次模型调用。')
      return
    }
    const reviseChange = api.reviseUserStrategyProposal
    if (!reviseChange) {
      failed('这个工作区不提供修改建议。')
      return
    }
    setBusy(true)
    failed('')
    try {
      // A fresh call with a fresh approval: the old proposal is kept as history and
      // this one is superseded by whatever the model proposes on the current base.
      const updated = await reviseChange(strategy.user_strategy_id, proposal.proposal_id, {
        call_id: `revise-${proposal.proposal_id}-${strategy.draft.revision}`.slice(0, 64),
        text: reviseMessage.trim() || '请修改这条建议',
        approved_budget: CALL_APPROVAL,
      })
      setReviseApproved(false)
      setReviseMessage('')
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const undone = strategy.changes.filter(item => item.kind === 'apply').at(-1)
  const canUndo = Boolean(undone && undone.to_revision === strategy.draft.revision)
  const stale = proposal.stale_paths.length > 0

  return <section aria-label="建议修改" className="strategy-panel proposal-panel">
    <header className="panel-head">
      <h2>建议修改</h2>
      <span className={`chip chip-${proposal.status}`}>{PROPOSAL_STATUS_LABELS[proposal.status]}</span>
    </header>
    <p className="muted">
      {`针对草稿修订 ${proposal.base_revision} · 提案 ${proposal.proposal_id} · 由模型提出，等待你决定`}
    </p>
    {stale && <p className="notice error">
      {`这条建议的部分字段是在更早的修订上写的，应用时会被宿主拒绝：${proposal.stale_paths.join(', ')}`}
    </p>}
    <ul className="proposal-list">
      {proposal.items.map(item => <li key={item.path}>
        <label className="check" htmlFor={`${proposal.proposal_id}-${item.path}`}>
          <input id={`${proposal.proposal_id}-${item.path}`} type="checkbox"
            checked={checked.includes(item.path)} disabled={!open}
            onChange={event => toggle(item.path, event.target.checked)} />
          <span className="proposal-path">{item.path}</span>
        </label>
        <p className="change"><span>{item.before ?? '无'} → {item.after}</span></p>
        <p className="reason">{item.reason}</p>
        <p className="muted">{`提议者：${item.proposer}`}</p>
        {item.evidence.length > 0 && <p className="muted">
          {`模型引用：${item.evidence.join(', ')} — 这是它自己的声明，不是已验证的事实。`}
        </p>}
      </li>)}
    </ul>
    <div className="actions">
      <button type="button" className="primary" onClick={apply} disabled={busy || !open}>应用所选</button>
      <button type="button" onClick={reject} disabled={busy || !open}>拒绝建议</button>
      <button type="button" onClick={undo} disabled={busy || !canUndo}>撤销最近一次修改</button>
    </div>
    <p className="muted">
      应用会用整份已校验的草稿写出一个新修订：选中的字段如果无法同时成立，就什么都不会应用。
      未勾选的字段留在旧建议里。应用本身不是运行——它不调用模型，也不启动回测。
    </p>
    {open && <div className="revise">
      <div className="field">
        <label htmlFor={`${proposal.proposal_id}-revise`}>要求一个不同的修改</label>
        <textarea id={`${proposal.proposal_id}-revise`} rows={2} maxLength={4000}
          value={reviseMessage} onChange={event => setReviseMessage(event.target.value)} />
      </div>
      <label className="check" htmlFor={`${proposal.proposal_id}-revise-approve`}>
        <input id={`${proposal.proposal_id}-revise-approve`} type="checkbox" checked={reviseApproved}
          onChange={event => { setReviseApproved(event.target.checked); failed('') }} />
        为这个修订批准一次模型调用
      </label>
      <button type="button" onClick={revise} disabled={busy}>修改这条建议</button>
      <p className="muted">
        修改会把当前建议留作历史，并在当前修订上用这一次已批准的调用重新询问模型。
      </p>
    </div>}
  </section>
}

/** An experiment preview and the one explicit confirmation that launches it. */
function ExperimentPanel({ api, strategy, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const [preview, setPreview] = useState<UserStrategyPreview | null>(null)
  const [busy, setBusy] = useState(false)

  const previewExperiment = async () => {
    const readPreview = api.previewUserStrategyExperiment
    if (!readPreview) return
    setBusy(true)
    failed('')
    try {
      setPreview(await readPreview(strategy.user_strategy_id))
    } catch (error) {
      setPreview(null)
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    if (!preview) {
      failed('请先预览实验：在你读过它将运行什么之前，不会启动任何东西。')
      return
    }
    const launch = api.launchUserStrategyExperiment
    if (!launch) {
      failed('这个工作区不提供启动实验。')
      return
    }
    setBusy(true)
    failed('')
    const intent = `li-${strategy.experiments.length + 1}`
    try {
      const updated = await launch(strategy.user_strategy_id, {
        launch_intent_id: intent,
        base_revision: preview.draft_revision,
        // The confirmation names the input it was made against, so a preview that
        // no longer describes what would run is refused instead of silently run.
        preview_identity: preview.preview_identity,
        approved_budget: RUN_APPROVAL,
      })
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="实验" className="strategy-panel">
    <h2>实验</h2>
    <div className="actions">
      <button type="button" onClick={previewExperiment} disabled={busy}>预览实验</button>
      <button type="button" className="primary" onClick={start} disabled={busy || !preview}>开始实验</button>
    </div>
    {preview && <dl className="preview-facts">
      <div><dt>草稿</dt><dd>{revisionLabel(preview.draft_revision)}</dd></div>
      <div><dt>策略</dt><dd>{`${preview.strategy_id} · ${preview.venue}:${preview.symbol}`}</dd></div>
      <div><dt>资金</dt><dd>{`初始资金 ${preview.initial_cash} USD`}</dd></div>
      <div><dt>数据</dt><dd>{dataLine(preview)}</dd></div>
      <div><dt>成本假设</dt><dd>{costLine(preview)}</dd></div>
      <div><dt>输入身份</dt><dd>{preview.preview_identity.slice(0, 19)}</dd></div>
      <div><dt>准则</dt><dd>{criterionLine(preview.criterion)}</dd></div>
      <div><dt>本工作区授予的预算</dt><dd>
        {`${preview.budget.max_model_calls} 次模型调用 · ${preview.budget.max_backtests} 次回测`}
      </dd></div>
    </dl>}
    {strategy.experiments.length > 0 && <ul className="experiment-list">
      {strategy.experiments.map(item => <li key={item.launch_intent_id}>
        {item.status === 'created'
          ? <p>{`已从修订 ${item.payload.draft_revision} 启动实验 ${item.launch_intent_id}`}</p>
          : <p>{`实验 ${item.launch_intent_id} 来自修订 ${item.payload.draft_revision} — ${EXPERIMENT_STATUS_LABELS[item.status]}`}</p>}
        {item.status === 'created' && item.job_id &&
          <Link to={`/backtests/${encodeURIComponent(item.job_id)}`}>
            打开运行以查看它的状态和报告
          </Link>}
        {item.status !== 'created' && <p className="notice">
          {EXPERIMENT_NOTES[item.status] ?? '这次实验没有已确认的运行。'}
          {item.message ? ` ${item.message}` : ''}
        </p>}
      </li>)}
    </ul>}
    <p className="muted">
      运行和它的状态在回测页面上，报告只在运行成功之后才存在——启动运行不等于有报告。
      已确认的实验会保留它冻结的那个修订，即使草稿继续前进。
    </p>
    {strategy.experiments.some(item => item.status === 'created') && <p className="muted">
      一次运行只是一个冻结的结果，不是已经完成的研究：留出验证、下一个问题和结论属于
      <Link to="/holdouts">留出验证</Link> 和
      <Link to="/research"> 研究</Link> 页面。
    </p>}
  </section>
}

/**
 * The workspace's strategy table: one compact row per strategy, in the columns the
 * decision is actually made in. The frozen evidence of a row is the revision its
 * latest confirmed experiment ran, and the run column is the state of that launch —
 * never a claim that a started run succeeded.
 */
function StrategyTable({ strategies, selectedId }: { strategies: UserStrategy[]; selectedId?: string }) {
  return <nav aria-label="策略列表" className="strategy-list">
    {strategies.length === 0
      ? <p className="strategy-empty">还没有策略。</p>
      : <table className="strategy-table">
          <thead><tr>
            <th scope="col">策略</th>
            <th scope="col">工作版本</th>
            <th scope="col">最近证据</th>
            <th scope="col">运行</th>
          </tr></thead>
          <tbody>
            {strategies.map(item => {
              const latest = item.experiments.at(-1)
              const selected = item.user_strategy_id === selectedId
              return <tr key={item.user_strategy_id} className={selected ? 'selected' : undefined}>
                <th scope="row">
                  <Link className="strategy-name" aria-current={selected ? 'page' : undefined}
                    to={`/strategies/${encodeURIComponent(item.user_strategy_id)}`}>{item.name}</Link>
                  <span className="strategy-instrument">{strategyLine(item.draft)}</span>
                </th>
                <td data-label="工作版本"><span className="chip chip-draft">{`v${item.draft.revision} 草稿`}</span></td>
                <td data-label="最近证据">{latest
                  ? <span className="strategy-evidence">{`冻结 v${latest.payload.draft_revision}`}</span>
                  : <span className="table-muted">待实验</span>}</td>
                <td data-label="运行">{!latest
                  ? <span className="table-muted">未启动</span>
                  : latest.status === 'created' && latest.job_id
                    ? <Link to={`/backtests/${encodeURIComponent(latest.job_id)}`}>运行已启动</Link>
                    : <span className="table-muted">{EXPERIMENT_STATUS_LABELS[latest.status]}</span>}</td>
              </tr>
            })}
          </tbody>
        </table>}
  </nav>
}

/** The workspace itself: the table, the selected strategy and its AI column. */
export function StrategiesWorkspace({ api }: { api: ApiAdapter }) {
  const { userStrategyId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const [strategies, setStrategies] = useState<UserStrategy[]>([])
  const [scenarios, setScenarios] = useState<ScenarioSummary[]>([])
  const [detail, setDetail] = useState<{ id: string; value: UserStrategy } | null>(null)
  const [creating, setCreating] = useState(false)
  const [listNote, setListNote] = useState('')
  const [detailNote, setDetailNote] = useState<{ id: string; text: string } | null>(null)
  // Every refusal is kept under the strategy it was made on. A note is written and
  // read for one strategy only, so a late answer for a strategy that is no longer
  // open can neither show over the open one nor replace the open one's note.
  const [notes, setNotes] = useState<Record<string, string>>({})

  useEffect(() => {
    let active = true
    const load = async () => {
      const list = api.listUserStrategies
      if (!list) {
        // The optional method is settled asynchronously, so a workspace that
        // unmounts or swaps its api never writes this note from a stale load.
        await Promise.resolve()
        if (active) setListNote('这个工作区不提供用户策略。')
        return
      }
      try {
        const items = await list()
        if (active) {
          setStrategies(items)
          setListNote('')
        }
      } catch (error) {
        if (active) setListNote(explain(error))
      }
    }
    void load()
    return () => { active = false }
  }, [api])

  useEffect(() => {
    let active = true
    api.listScenarios?.().then(items => { if (active) setScenarios(items) }).catch(() => { if (active) setScenarios([]) })
    return () => { active = false }
  }, [api])

  useEffect(() => {
    const read = api.getUserStrategy
    if (!userStrategyId || !read) return
    let active = true
    read(userStrategyId)
      .then(value => { if (active) { setDetail({ id: value.user_strategy_id, value }); setDetailNote(null) } })
      // The failure names the strategy it was read for, so it is not shown over
      // another strategy's page.
      .catch(error => { if (active) setDetailNote({ id: userStrategyId, text: explain(error) }) })
    return () => { active = false }
  }, [api, userStrategyId])

  // The render is derived from the routed id, never from whatever was read last:
  // a strategy that is no longer the open one is never rendered as if it were, so
  // a slow answer or a stale callback cannot put another object's editable draft,
  // preview or approval under this route.
  const current = detail && detail.id === userStrategyId ? detail.value : null

  /** Write (or clear) the refusal of exactly one strategy, and of no other. */
  const note = (id: string, message: string) => setNotes(currentNotes => {
    if (!message && !(id in currentNotes)) return currentNotes
    const next = { ...currentNotes }
    if (message) next[id] = message
    else delete next[id]
    return next
  })
  /** The refusal channel of one strategy: a panel can only write its own. */
  const failedFor = (id: string) => (message: string) => note(id, message)

  const changed = useCallback((value: UserStrategy) => {
    setDetail({ id: value.user_strategy_id, value })
    setNotes(currentNotes => {
      if (!(value.user_strategy_id in currentNotes)) return currentNotes
      const next = { ...currentNotes }
      delete next[value.user_strategy_id]
      return next
    })
    setStrategies(items => items.some(item => item.user_strategy_id === value.user_strategy_id)
      ? items.map(item => item.user_strategy_id === value.user_strategy_id ? value : item)
      : [...items, value])
  }, [])

  const created = useCallback((value: UserStrategy) => {
    setStrategies(items => items.some(item => item.user_strategy_id === value.user_strategy_id)
      ? items
      : [...items, value])
    setCreating(false)
    // One create selects the strategy it just saved: its own conversation opens
    // immediately, with no second creation step.
    setDetail({ id: value.user_strategy_id, value })
    navigate(`/strategies/${encodeURIComponent(value.user_strategy_id)}`)
  }, [navigate])

  const proposal = current && current.proposals.length > 0
    ? current.proposals[current.proposals.length - 1]
    : null

  // The create flow is a disclosure, open by itself only while this workspace has
  // nothing to select at all — or when the header action was followed from an open
  // strategy, which opens the form here in that one click.
  const wantsCreate = (location.state as { createStrategy?: boolean } | null)?.createStrategy === true
  const showCreate = creating || wantsCreate || (current === null && strategies.length === 0 && !userStrategyId)
  const actionNote = current ? notes[current.user_strategy_id] ?? '' : ''

  return <div className="strategy-workspace">
    <div className="strategy-column">
      <header className="strategy-heading">
        <h1>策略</h1>
        <span className="strategy-count">{`${strategies.length} 个策略`}</span>
        {current
          ? <Link className="create-cta" to="/strategies" state={{ createStrategy: true }}>
              <span aria-hidden="true">+</span>新建策略
            </Link>
          : <button type="button" className="create-cta" aria-expanded={showCreate}
              onClick={() => {
                // Following that request leaves it behind: this control still opens
                // and closes the form itself.
                if (wantsCreate) { navigate('/strategies', { replace: true, state: null }); return }
                setCreating(value => !value)
              }}>
              <span aria-hidden="true">+</span>新建策略
            </button>}
      </header>
      <p className="strategy-subtitle">每个想法一份草稿。在你确认之前，这里不会运行任何东西。</p>
      <StrategyTable strategies={strategies} selectedId={userStrategyId} />
      {listNote && <p role="alert" className="notice error">{listNote}</p>}
      {showCreate && <CreateStrategy api={api} scenarios={scenarios} created={created} />}
      {detailNote && detailNote.id === userStrategyId &&
        <p role="alert" className="notice error">{detailNote.text}</p>}
      {current && <>
        {actionNote && <p role="alert" className="notice error">{actionNote}</p>}
        <StrategyDetail
          key={current.user_strategy_id}
          api={api} strategy={current} changed={changed}
          failed={failedFor(current.user_strategy_id)} />
      </>}
    </div>
    <aside className="strategy-ai">
      {current
        ? <ChatPanel
            key={`${current.user_strategy_id}-${current.draft.revision}`}
            api={api} strategy={current} strategies={strategies} changed={changed} />
        : <EmptyChat />}
      {current && proposal && <ProposalPanel
        key={`${current.user_strategy_id}-${proposal.proposal_id}-${proposal.base_revision}-${proposal.status}`}
        api={api} strategy={current} proposal={proposal}
        changed={changed} failed={failedFor(current.user_strategy_id)} />}
    </aside>
  </div>
}
