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
  isIntentDraft,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyDraftDocument,
  type UserStrategyExperiment,
  type UserStrategyPreview,
  type UserStrategyProposal,
  type UserStrategyProposalStatus,
} from './user-strategies-api'
import { StrategyEvidenceResearch } from './user-strategy-research'

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
  user_strategy_invalid: '这个操作对这条策略的当前状态无效，因此什么都没有改变。',
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

/**
 * Who proposed one change and where it stands. Only a proposal that is still open
 * is waiting for a decision; a closed one says what became of it instead of asking
 * again for a decision that has already been made.
 */
const PROPOSAL_ORIGIN: Record<UserStrategyProposalStatus, string> = {
  open: '由模型提出，等待你决定',
  applied: '由模型提出，已应用；修改记录已保留。',
  rejected: '由模型提出，已拒绝：它没有写入任何修订，只留作历史',
  superseded: '由模型提出，已被取代：它只留作历史，不再是这条策略当前的建议',
}

const EXPERIMENT_NOTES: Record<string, string> = {
  pending: '这次确认在运行存在之前就被记录了，什么都没有派发。',
  interrupted_before_dispatch: '这次确认从未派发过运行，也没有创建重复的运行。',
  unknown: '中断之前可能已经派发了一次运行。在找到它之前，它不会被重发、不会被计数、也不会被退还。',
  failed: '这次实验在运行存在之前就被拒绝了。',
}

const EXPERIMENT_STATUS_LABELS: Record<UserStrategyExperiment['status'], string> = {
  pending: '已记录，未派发',
  // The record says a dispatch was created, not that the run has started or
  // finished: the run's own state lives on its backtest page.
  created: '运行已创建',
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

/**
 * The clock time of one recorded instant, to the minute, in UTC. The exact value
 * stays on the element that shows it, so nothing here rounds a timestamp away.
 */
function utcClock(at: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(at)
  return match ? `${match[2]}:${match[3]} UTC` : at
}

/**
 * One revision's strategy and instrument. An intent has neither, and says so
 * instead of printing a blank where a strategy would be.
 */
const strategyLine = (draft: UserStrategyDraftDocument) =>
  isIntentDraft(draft)
    ? '尚未绑定场景，因此没有策略与标的'
    : `${draft.strategy_id} · ${draft.venue}:${draft.symbol}`

const revisionLabel = (revision: number) => `草稿修订 ${revision}`

/** One revision, named for what it is: an executable draft, or a saved intent. */
const revisionText = (draft: UserStrategyDraftDocument) =>
  isIntentDraft(draft) ? `意图修订 ${draft.revision}` : revisionLabel(draft.revision)

/** The short form of a revision, for the chips and table cells. */
const revisionChip = (draft: UserStrategyDraftDocument) =>
  isIntentDraft(draft) ? `v${draft.revision} 意图` : `v${draft.revision} 草稿`

/** One durable change's kind, in the user's words. */
const changeLabel = (kind: 'apply' | 'undo' | 'bind') =>
  kind === 'apply' ? '应用' : kind === 'undo' ? '撤销' : '绑定场景'

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

/** The name a save falls back to: the user's own words, never a scene or a guess. */
const TEMPORARY_NAME = '未命名想法'

/** One string's UTF-8 length, counted here rather than through a browser-only encoder. */
function utf8Length(text: string): number {
  let bytes = 0
  for (const character of text) {
    const point = character.codePointAt(0) ?? 0
    bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4
  }
  return bytes
}

/**
 * A visible temporary name taken from the user's own text, clipped on a character
 * boundary to the host's own 120-byte bound. It stands in for a name the user did
 * not give — it is never a scenario, a strategy or a default draft field.
 */
function temporaryName(intent: string): string {
  const text = intent.trim().replace(/\s+/g, ' ')
  if (!text) return TEMPORARY_NAME
  let name = ''
  let bytes = 0
  for (const character of text) {
    const size = utf8Length(character)
    if (bytes + size > 120) break
    name += character
    bytes += size
  }
  return name
}

/**
 * Create one strategy. This is a save of an idea: no scenario is chosen here, so
 * no catalog is needed and no draft field, model call or run exists yet. The form
 * is a compact disclosure rather than a page: creating one strategy selects it, and
 * the conversation for it opens immediately.
 *
 * What is typed lives above the form, so opening a strategy and coming back leaves
 * the half-written idea where the user left it.
 */
function CreateStrategy({ api, created, value, update }: {
  api: ApiAdapter
  created: (strategy: UserStrategy) => void
  value: { name: string; intent: string }
  update: (value: { name: string; intent: string }) => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const { name, intent } = value

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
      // No scenario is named, so this saves an intent: the host needs no catalog
      // for it and writes no executable field, and the scenario is chosen later,
      // explicitly, on the strategy itself.
      const strategy = await createStrategy({
        name: name.trim() || temporaryName(intent),
        intent: intent.trim(),
      })
      update({ name: '', intent: '' })
      created(strategy)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="新建策略" className="strategy-create">
    <form onSubmit={submit}>
      <div className="field">
        <label htmlFor="strategy-intent">意图</label>
        <textarea id="strategy-intent" value={intent} maxLength={2000} required rows={2}
          onChange={event => update({ name, intent: event.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="strategy-name">策略名称</label>
        <input id="strategy-name" value={name} maxLength={120}
          onChange={event => update({ name: event.target.value, intent })} />
        <small>{`可以留空；留空时用你自己的话作为临时名称：${temporaryName(intent)}`}</small>
      </div>
      <div className="create-actions">
        <button type="submit" className="primary" disabled={busy || !intent.trim()}>
          保存想法
        </button>
        <p className="muted">
          保存只写下这个想法：不选择场景，不调用模型，也不启动回测。场景由你在策略里明确绑定，
          在此之前这里没有策略、标的、资金或参数。
        </p>
      </div>
      {note && <p role="alert" className="notice error">{note}</p>}
    </form>
  </section>
}

/**
 * The head revision's own facts, always visible under the selected strategy: the
 * revision, the strategy and instrument, the funding and the criterion. The detail
 * sections below it are read against exactly this revision. An intent has none of
 * the executable facts yet, and states that instead of showing a blank or a guess.
 */
function DraftFacts({ draft }: { draft: UserStrategyDraftDocument }) {
  return <section aria-label="草稿" className="draft-facts">
    {isIntentDraft(draft)
      ? <dl>
          <div><dt>工作版本</dt><dd>{revisionText(draft)}</dd></div>
          <div><dt>场景</dt><dd>尚未绑定：没有策略、标的、资金、数据来源或注册参数</dd></div>
        </dl>
      : <dl>
          <div><dt>工作版本</dt><dd>{revisionLabel(draft.revision)}</dd></div>
          <div><dt>策略与标的</dt><dd>{strategyLine(draft)}</dd></div>
          <div><dt>初始资金</dt><dd>{`${draft.initial_cash} USD`}</dd></div>
          <div><dt>准则</dt><dd>{criterionLabel(draft.criterion)}</dd></div>
        </dl>}
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
    <p className="muted">保存只写下想法；一次批准只允许一次模型调用；一次确认只允许一次实验。</p>
  </section>
}

function RulesSection({ draft }: { draft: UserStrategyDraftDocument }) {
  if (isIntentDraft(draft)) {
    return <section aria-label="规则与假设" className="strategy-panel">
      <h2>规则与假设</h2>
      <p className="muted">
        这条策略还没有绑定场景，因此没有策略标识、标的、场景、资金、数据来源或注册参数可以显示，
        也没有任何成本假设可以读。绑定一个已注册场景之后，这里会显示那个场景自己的规则与假设。
      </p>
    </section>
  }
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
                {changeLabel(item.kind)}
              </span>
              <span className="muted">{`修订 ${item.from_revision} → ${item.to_revision} · ${item.at}`}</span>
            </p>
            {item.scenario_id && <p className="muted">{`绑定的场景：${item.scenario_id}`}</p>}
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
          <p>{revisionText(revision)}</p>
          {isIntentDraft(revision)
            ? <p className="muted">保存的想法，尚未绑定场景。</p>
            : <p className="muted">{`${strategyLine(revision)} · 初始资金 ${revision.initial_cash} USD · ${criterionLabel(revision.criterion)}`}</p>}
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

/**
 * The one explicit choice that gives a saved intent a scenario.
 *
 * Nothing is preselected and nothing is inferred: the list is the workspace's own
 * registry, each choice is named by its exact scenario id and by that scenario's
 * own summary so two similar names stay told apart, and the button stays inert
 * until the user ticks the confirmation. The host validates the scenario and forms
 * the executable draft — this panel writes no field itself and never guesses a
 * default. Binding is one revision of the same strategy and cannot be undone, so
 * the panel says so before the click, not after it.
 */
function BindScenario({ api, strategy, scenarios, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const [selected, setSelected] = useState('')
  const [field, setField] = useState('')
  const [expectation, setExpectation] = useState<string>('positive')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const attempts = useRef(0)

  const usable = scenarios.filter(item => item.valid && item.summary && item.scenario_id)
  const candidate = usable.find(item => item.scenario_id === selected) ?? null

  const bind = async () => {
    if (!candidate) {
      failed('请先选择一个已注册场景。没有被选中的场景就不会被绑定。')
      return
    }
    if (!confirmed) {
      failed('绑定需要一个明确的确认：勾选确认之后才会绑定。')
      return
    }
    const bindScenario = api.bindUserStrategyScenario
    if (!bindScenario) {
      failed('这个工作区不提供场景绑定。')
      return
    }
    setBusy(true)
    failed('')
    const attempt = attempts.current
    try {
      // One explicit choice, one operation id: a retry of this click replays it
      // instead of binding a second time.
      const updated = await bindScenario(strategy.user_strategy_id, {
        operation_id: `bind-${strategy.user_strategy_id}-${strategy.draft.revision}-${attempt}`.slice(0, 64),
        base_revision: strategy.draft.revision,
        scenario_id: candidate.scenario_id,
        // The host validates this against the report's own vocabulary: what the
        // user states is a field and a sign, and what is stored is the complete
        // validated criterion the host froze.
        ...(field ? { criterion: { field, expectation } } : {}),
      })
      attempts.current += 1
      setSelected('')
      setField('')
      setConfirmed(false)
      changed(updated)
    } catch (error) {
      failed(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="绑定场景" className="strategy-panel bind-panel">
    <header className="panel-head">
      <h2>绑定场景</h2>
      <span className="chip">尚未绑定</span>
    </header>
    <p className="muted">
      这条策略目前只是一个想法：没有策略、标的、资金或参数。绑定会把你选中的那个已注册场景
      变成这条策略的可执行草稿（下一个修订），并保留这个想法、对话、调用和修订记录。
      绑定只有一次，不能撤销，也不会调用模型或启动回测。
    </p>
    <div className="field">
      <label htmlFor="bind-scenario">场景</label>
      <select id="bind-scenario" value={selected} onChange={event => setSelected(event.target.value)}>
        <option value="">选择一个已注册场景</option>
        {usable.map(item => <option key={item.scenario_id} value={item.scenario_id}>
          {`${item.scenario_id} · ${item.summary?.strategy_id} · ${item.summary?.venue}:${item.summary?.symbol}`}
        </option>)}
      </select>
      {!usable.length && <small>当前没有可用的有效注册场景，因此现在没有可以绑定的东西。</small>}
    </div>
    {candidate?.summary && <dl className="summary-list">
      <div><dt>策略标识</dt><dd>{candidate.summary.strategy_id}</dd></div>
      <div><dt>标的</dt><dd>{`${candidate.summary.venue}:${candidate.summary.symbol}`}</dd></div>
      <div><dt>初始资金</dt><dd>{`${candidate.summary.initial_cash} USD`}</dd></div>
      <div><dt>记录数</dt><dd>{candidate.summary.record_count}</dd></div>
    </dl>}
    <div className="create-fields">
      <div className="field">
        <label htmlFor="bind-criterion-field">准则字段</label>
        <select id="bind-criterion-field" value={field}
          onChange={event => setField(event.target.value)}>
          <option value="">不声明准则</option>
          {CRITERION_FIELDS.map(item => <option key={item.field} value={item.field}>{item.label}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor="bind-criterion-expectation">预期方向</label>
        <select id="bind-criterion-expectation" value={expectation} disabled={!field}
          onChange={event => setExpectation(event.target.value)}>
          {EXPECTATIONS.map(item => <option key={item} value={item}>{EXPECTATION_LABELS[item]}</option>)}
        </select>
      </div>
    </div>
    <label className="check" htmlFor="bind-confirm">
      <input id="bind-confirm" type="checkbox" checked={confirmed}
        onChange={event => { setConfirmed(event.target.checked); failed('') }} />
      我确认把选中的这个场景绑定到这条策略
    </label>
    <div className="actions">
      <button type="button" className="primary" onClick={bind}
        disabled={busy || !candidate || !confirmed}>绑定场景</button>
    </div>
    <p className="muted">
      绑定是这条策略上的一次明确选择：它不会重发任何调用，也不会启动任何运行。
    </p>
  </section>
}

/** A section of the selected strategy: the tab bar and the section it opens. */
function StrategyDetail({ api, strategy, scenarios, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  // The detail opens on the evidence of a frozen run when there is one, and on the
  // intent when there is not: an honest empty state rather than a hidden form.
  const [tab, setTab] = useState<TabKey>(hasRun ? 'evidence' : 'intent')
  const idBase = `detail-${strategy.user_strategy_id}`
  // The strategy and the conversation are two columns on a wide screen and two
  // stacked blocks on a narrow one, where the jump links are the way between them.
  // Both targets are focusable, so a fragment jump really moves the reader there.
  return <section className="strategy-detail" id="strategy-detail" tabIndex={-1} aria-label="策略详情">
    <header className="strategy-title">
      <h2>{strategy.name}</h2>
      <span className="chip chip-draft">{revisionChip(strategy.draft)}</span>
      <a className="jump-link jump-to-ai" href="#strategy-ai-column">跳到 AI 协作</a>
      <Link className="strategy-open" to="/strategies">返回策略列表</Link>
    </header>
    <DraftFacts draft={strategy.draft} />
    {isIntentDraft(strategy.draft) && <BindScenario
      api={api} strategy={strategy} scenarios={scenarios}
      changed={changed} failed={failed} />}
    <DetailTabs active={tab} select={setTab} idBase={idBase} />
    <div className="strategy-tabpanel" role="tabpanel" id={`${idBase}-panel-${tab}`}
      aria-labelledby={`${idBase}-tab-${tab}`} tabIndex={0}>
      {tab === 'intent' && <IntentSection strategy={strategy} />}
      {tab === 'rules' && <RulesSection draft={strategy.draft} />}
      {tab === 'experiments' && <ExperimentPanel
        api={api} strategy={strategy} changed={changed} failed={failed} />}
      {tab === 'evidence' && <>
        <EvidenceSection api={api} strategy={strategy} />
        {/* An intent has no frozen run, so there is nothing to select and nothing
            to explain: the panel is not shown until a scenario is bound. */}
        {!isIntentDraft(strategy.draft) && <StrategyEvidenceResearch
          api={api} strategy={strategy} changed={changed} failed={failed} />}
      </>}
      {tab === 'revisions' && <RevisionsSection strategy={strategy} />}
      {tab === 'candidates' && <CandidatesSection strategy={strategy} />}
    </div>
  </section>
}

/**
 * The frozen runs one recorded call was grounded in, kept with that call.
 *
 * This is provenance and not authority: it says which runs one explanation was
 * allowed to see, which revisions they froze and which digests they were read
 * under, so that after a reload there is no ambiguity about what was explained.
 * A call that selected nothing has no list and shows nothing here.
 */
function CallEvidence({ call }: { call: UserStrategyCall | undefined }) {
  const evidence = call?.evidence ?? []
  if (!evidence.length) return null
  return <details className="provenance">
    <summary>{`这次调用所依据的冻结证据（${evidence.length}）`}</summary>
    <ul className="path-list">
      {evidence.map(item => <li key={item.job_id}>
        {`修订 ${item.draft_revision ?? '未记录'} · 确认 ${item.launch_intent_id ?? '未记录'}`
          + ` · 运行 ${item.job_id}`
          + ` · 输入 ${item.input_sha256?.slice(0, 16) ?? '未记录'}`
          + ` · 报告 ${item.report_sha256?.slice(0, 16) ?? '未记录'}`
          + (item.criterion
            ? ` · 冻结准则 ${item.criterion.field} ${item.criterion.expectation}`
            : ' · 该运行没有冻结准则')}
      </li>)}
    </ul>
    <p className="muted">
      这是这次调用被记录下来的证据来源：所选运行的冻结修订与摘要，不是新的权限，
      也不会被后来的运行改写。
    </p>
  </details>
}

/**
 * The AI conversation: approved calls, labelled inference, no implied authority.
 * The header names the strategy the conversation belongs to and switches between
 * the real strategies of this workspace.
 *
 * Only the history scrolls. The last call's own status, usage and unavailable cost
 * and the composer stay outside the scroller, so the control the user acts on is
 * never somewhere inside a list of past answers.
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
    {strategy.messages.length > 0 &&
      // The history is the one bounded surface on this page: it scrolls on its
      // own, by keyboard as well as by pointer, and every message stays in it.
      <div className="message-scroll" role="group" aria-label="对话记录" tabIndex={0}>
        <ol className="message-list">
          {strategy.messages.map(item => <li key={item.message_id} className={`message message-${item.role}`}>
            <p className="message-role">
              {item.role === 'model' ? 'QUINE' : '你'}{' '}
              <time className="muted" dateTime={item.at} title={item.at}>{utcClock(item.at)}</time>
            </p>
            <p>{item.text}</p>
            {item.role === 'model' && <p className="muted">模型推断 — 不是正式证据。</p>}
          </li>)}
        </ol>
      </div>}
    {latest && <div className="call-note">
      <p><span className={`status status-${latest.status}`}>{CALL_STATUS_LABELS[latest.status]}</span></p>
      <p className="muted">{usageLine(latest)}</p>
      {latest.cost && !latest.cost.available &&
        <p className="muted">{`成本不可用：${latest.cost.reason}。`}</p>}
      {latest.message && <p className="notice">{latest.message}</p>}
      <CallNextStep call={latest} />
      <CallEvidence call={latest} />
    </div>}
    {isIntentDraft(strategy.draft) && <p className="notice">
      这条策略还没有绑定场景：这次对话只能澄清这个想法，模型不会提出任何字段修改，也不会运行任何东西。
      绑定场景之后，它的建议才会变成可以应用的修改。
    </p>}
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
        <p className="muted">{isIntentDraft(strategy.draft)
          ? '一次批准只允许一次调用，这一次调用只用来澄清这个想法：它不提出字段修改，也不启动回测。'
          : '本次批准仅针对当前修订调用模型一次，解释或提出修改；不应用提案，也不运行回测。'}</p>
        <button type="button" className="primary" onClick={ask} disabled={busy || !message.trim()}>询问模型</button>
      </div>
      {note && <p role="alert" className="notice error">{note}</p>}
    </div>
  </section>
}

/**
 * The AI column before anything is open: it explains what it will hold. While a
 * selected strategy is still being read it says that instead, so a read in flight
 * is never shown as "nothing is selected", and a read that failed says so too.
 */
function EmptyChat({ note, live = false }: { note?: string; live?: boolean }) {
  return <section aria-label="AI 协作" className="strategy-panel strategy-chat strategy-chat-empty">
    <header className="chat-head"><h2>AI 协作</h2></header>
    {note
      ? <p className="muted" role={live ? 'status' : undefined} aria-live={live ? 'polite' : undefined}>{note}</p>
      : <>
          <p className="muted">AI 提出建议，证据做出判断。</p>
          <p>先创建或选择一条策略：这里会显示它自己的对话、模型针对它提出的有类型修改，以及每次修改的应用记录。整个对话始终属于当前打开的那一条策略。</p>
        </>}
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
    <p className="muted proposal-origin">
      {`针对草稿修订 ${proposal.base_revision} · 提案 ${proposal.proposal_id} · ${PROPOSAL_ORIGIN[proposal.status]}`}
    </p>
    <CallEvidence call={strategy.calls.find(item => item.call_id === proposal.call_id)} />
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

  if (isIntentDraft(strategy.draft)) {
    // No scenario, so nothing to preview and nothing to confirm: this says so
    // rather than offering a button whose only outcome is a host refusal.
    return <section aria-label="实验" className="strategy-panel">
      <h2>实验</h2>
      <p className="muted">
        这条策略还没有绑定场景，因此没有可以预览或运行的草稿：这里不显示任何输入、成本假设或预算，
        因为一个都还不存在。
      </p>
      <p className="muted">
        在「绑定场景」里明确选择一个已注册场景之后，这里才会显示那次运行将执行什么。
        宿主也会自己把关：未绑定场景的策略不会被预览，也不会启动任何运行。
      </p>
    </section>
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
              // A saved intent has no scenario, so it has no frozen run to report
              // and nothing that could be started: its row says that plainly.
              const unbound = isIntentDraft(item.draft)
              return <tr key={item.user_strategy_id} className={selected ? 'selected' : undefined}>
                <th scope="row">
                  <Link className="strategy-name" aria-current={selected ? 'page' : undefined}
                    to={`/strategies/${encodeURIComponent(item.user_strategy_id)}`}>{item.name}</Link>
                  <span className="strategy-instrument">{strategyLine(item.draft)}</span>
                </th>
                <td data-label="工作版本"><span className="chip chip-draft">{revisionChip(item.draft)}</span></td>
                <td data-label="最近证据">{latest
                  ? <span className="strategy-evidence">{`冻结 v${latest.payload.draft_revision}`}</span>
                  : <span className="table-muted">{unbound ? '待绑定场景' : '待实验'}</span>}</td>
                <td data-label="运行">{!latest
                  ? <span className="table-muted">{unbound ? '不可运行' : '未启动'}</span>
                  : latest.status === 'created' && latest.job_id
                    ? <Link to={`/backtests/${encodeURIComponent(latest.job_id)}`}>{EXPERIMENT_STATUS_LABELS.created}</Link>
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
  // The list read is a state of its own: until it answers, an empty array is not an
  // empty workspace, and the count, the empty claim and the create form wait for it.
  // It is kept with the adapter it was read from, so "still reading" is derived
  // rather than written when the effect starts, and neither another adapter's answer
  // nor a late one from an adapter this workspace has left is read as this one's.
  const [listRead, setListRead] = useState<{ api: ApiAdapter; ok: boolean; note: string } | null>(null)
  const [creating, setCreating] = useState(false)
  // The half-written idea is kept above the form, so leaving the collection route
  // and coming back does not throw away what the user had typed.
  const [createDraft, setCreateDraft] = useState({ name: '', intent: '' })
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
        if (active) setListRead({ api, ok: false, note: '这个工作区不提供用户策略。' })
        return
      }
      try {
        const items = await list()
        if (active) {
          setStrategies(items)
          setListRead({ api, ok: true, note: '' })
        }
      } catch (error) {
        if (active) setListRead({ api, ok: false, note: explain(error) })
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
    if (!userStrategyId) return
    const read = api.getUserStrategy
    let active = true
    if (!read) {
      // Settled asynchronously for the same reason as the list above: this note is
      // never written by a read the workspace has already left.
      void Promise.resolve().then(() => {
        if (active) setDetailNote({ id: userStrategyId, text: '这个工作区不提供读取单条策略。' })
      })
      return () => { active = false }
    }
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

  // Loading, ready and failed, read from the adapter this list state belongs to.
  const listSettled = listRead && listRead.api === api ? listRead : null
  const listState: 'reading' | 'ready' | 'failed' =
    !listSettled ? 'reading' : listSettled.ok ? 'ready' : 'failed'
  const listNote = listSettled?.note ?? ''

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
  // nothing to select at all — or when the header action was followed from a
  // selected strategy's own route, which opens the form here in that one click. It
  // is offered on the collection route only: a strategy that is open is not a place
  // to create another one above it.
  const wantsCreate = (location.state as { createStrategy?: boolean } | null)?.createStrategy === true
  const showCreate = !userStrategyId
    && (creating || wantsCreate || (listState === 'ready' && strategies.length === 0))
  const actionNote = current ? notes[current.user_strategy_id] ?? '' : ''
  // A selected strategy that has neither answered nor failed is still being read;
  // that is a state to say out loud, not an empty selection and not an empty page.
  const detailFailed = Boolean(userStrategyId) && detailNote?.id === userStrategyId
  const detailReading = Boolean(userStrategyId) && current === null && !detailFailed

  return <div className="strategy-workspace">
    <div className="strategy-column">
      <header className="strategy-heading">
        <h1>策略</h1>
        {listState === 'ready' && <span className="strategy-count">{`${strategies.length} 个策略`}</span>}
        {/* The control follows the routed id, not what has been read: a selected
            strategy whose read is still in flight or has failed is still a selected
            route, where the form is not offered. There the one click goes to the
            collection route and opens the form, rather than toggling a disclosure
            that this route can never show. */}
        {userStrategyId
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
      {listState === 'reading'
        ? <p className="muted" role="status" aria-live="polite">正在读取策略…</p>
        : listState === 'ready' && <StrategyTable strategies={strategies} selectedId={userStrategyId} />}
      {listNote && <p role="alert" className="notice error">{listNote}</p>}
      {showCreate && <CreateStrategy api={api} created={created} value={createDraft} update={setCreateDraft} />}
      {detailNote && detailNote.id === userStrategyId &&
        <p role="alert" className="notice error">{detailNote.text}</p>}
      {detailReading && <p className="muted" role="status" aria-live="polite">正在读取策略…</p>}
      {current && <>
        {actionNote && <p role="alert" className="notice error">{actionNote}</p>}
        <StrategyDetail
          key={current.user_strategy_id}
          api={api} strategy={current} scenarios={scenarios} changed={changed}
          failed={failedFor(current.user_strategy_id)} />
      </>}
    </div>
    <aside className="strategy-ai" id="strategy-ai-column" tabIndex={-1}>
      {current && <a className="jump-link jump-to-details" href="#strategy-detail">返回策略详情</a>}
      {current
        ? <ChatPanel
            key={`${current.user_strategy_id}-${current.draft.revision}`}
            api={api} strategy={current} strategies={strategies} changed={changed} />
        : detailReading
          ? <EmptyChat note="正在读取这条策略的对话…" live />
          : detailFailed
            ? <EmptyChat note="这条策略没有被读到，因此这里没有对话。" />
            : <EmptyChat />}
      {current && proposal && <ProposalPanel
        key={`${current.user_strategy_id}-${proposal.proposal_id}-${proposal.base_revision}-${proposal.status}`}
        api={api} strategy={current} proposal={proposal}
        changed={changed} failed={failedFor(current.user_strategy_id)} />}
    </aside>
  </div>
}
