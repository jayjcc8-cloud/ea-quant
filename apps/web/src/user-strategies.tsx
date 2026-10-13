import { ConversationInput } from './conversation-input'
import { DraftEditor } from './draft-editor'
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
 * the run it was read for. Which run it is about is the address's own selection —
 * the same one the research panel below reads — so the top card and that panel can
 * never be about two different runs. Nothing here computes a metric the engine did
 * not write.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { MultiRoundTripExecution, RoundTripExecution } from './app'
import { CandidateCreate } from './candidates'
import { TradeAnalyticsPanel, type TradeAnalytics } from './trade-analytics'
import type { ApiAdapter, BacktestJob, BacktestReport, ScenarioSummary } from './app'
import type { ModelConfigurationStatus, ReportCriterion } from './research-api'
import { EquityCurve, type EquityPath } from './equity-path'
import { exactDelta } from './decimal'
import { evidenceAddress, readEvidenceSelection, selectableRuns, selectedRuns } from './evidence-selection'
import {
  UserStrategyRequestError,
  isIntentDraft,
  userStrategiesApi,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyConversation,
  type UserStrategyConversationEvent,
  type UserStrategyConversationPending,
  type UserStrategyDraftDocument,
  type UserStrategyExperiment,
  type UserStrategyPreview,
  type UserStrategyProposal,
  type UserStrategyProposalStatus,
  type UserStrategyResumeRequest,
} from './user-strategies-api'
import { StrategyEvidenceResearch, useRunRead } from './user-strategy-research'

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
  // The owner's own states. Each one is the core's answer, not a local guess.
  confirmed: '运行的所有者已经确认了这次输入，但还没有开始运行它。',
  running: '运行的所有者正在运行这次确认的冻结输入。',
  stopping: '停止请求已被接受，所有者正在把这次运行自己的进程组收干净；在它记录之前，停止还不是已证实的。',
  cancelled: '这次运行已被取消，所有者没有为它登记任何结果。',
  unconfirmed: '所有者没有这次确认的记录：它没有被重发，也没有创建任何运行。同一次确认是唯一可以再次询问的对象。',
}

const EXPERIMENT_STATUS_LABELS: Record<UserStrategyExperiment['status'], string> = {
  pending: '已记录，未派发',
  // The record says a dispatch was created, not that the run has started or
  // finished: the run's own state lives on its backtest page.
  created: '运行已创建',
  failed: '派发前被拒绝',
  unknown: '结果未知',
  interrupted_before_dispatch: '从未派发',
  confirmed: '已确认，待运行',
  running: '运行中',
  stopping: '正在停止',
  cancelled: '已取消',
  unconfirmed: '收据未确认',
}

/** The owner's own states a run can still move out of on its own. */
const LIVE_EXPERIMENT_STATUSES: UserStrategyExperiment['status'][] = [
  'confirmed', 'running', 'stopping',
]
/**
 * A confirmation this page has not resolved yet. While one is open, the only
 * request that may be repeated is that same confirmation: a new launch intent
 * would be a second run, and the owner's answer to the first is still unknown.
 */
const UNRESOLVED_EXPERIMENT_STATUSES: UserStrategyExperiment['status'][] = [
  'pending', 'unconfirmed', 'confirmed', 'running', 'stopping', 'unknown',
]
/**
 * The stop request this panel uses. It is optional on the shared adapter, so a
 * workspace that serves none renders the honest state; when the adapter does not
 * provide it, the API module is asked directly rather than the action being dropped.
 */
type ExperimentOwnerApi = ApiAdapter & {
  cancelUserStrategyExperiment?: (
    userStrategyId: string,
    launchIntentId: string,
  ) => Promise<UserStrategy>
}

/**
 * The run's own state, as the job itself reports it. Nothing here is read from
 * `experiment.created`, so a dispatch that was created is never shown as a run
 * that succeeded, and no local Paper or simulation claim is made.
 */
const JOB_STATUS_LABELS: Record<BacktestJob['status'], string> = {
  accepted: '已受理',
  running: '运行中',
  stopping: '正在停止',
  stopped: '已停止',
  succeeded: '成功',
  failed: '失败',
  interrupted: '已中断',
}

const explain = (error: unknown) => {
  if (error instanceof UserStrategyRequestError) {
    const explanation = REQUEST_EXPLANATIONS[error.code]
    return explanation ? `${explanation} · 服务端：${error.message}` : `服务端：${error.message}`
  }
  return error instanceof Error ? error.message : '这次请求失败了，什么都没有改变。'
}

/**
 * A refused apply, said the way it can actually be recovered from. The proposal was
 * written against a revision that has since moved on, so reloading and pressing
 * Apply again cannot make this same change valid: the next step is a new proposal on
 * the current draft, or a rejection. Only this action reads it this way; every other
 * refusal keeps the ordinary explanation, and the host's own message is unchanged.
 */
const applyFailure = (error: unknown) => {
  if (error instanceof UserStrategyRequestError && error.code === 'stale_base_revision') {
    return '草稿修订已前进，这次修改没有被应用。请基于当前草稿重新提出建议，或拒绝此建议。'
      + ` · 服务端：${error.message}`
  }
  return explain(error)
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
const changeLabel = (kind: 'apply' | 'undo' | 'bind' | 'select-dataset' | 'manual') =>
  kind === 'manual' ? '手工保存' : kind === 'apply'
    ? '应用'
    : kind === 'undo'
      ? '撤销'
      : kind === 'bind'
        ? '绑定场景'
        : '选择数据集'

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
 * Every registered parameter the preview admitted, exactly as it was admitted. The
 * list is the scenario's own: a name this page has never heard of is printed rather
 * than dropped, and a zero is a value rather than a missing one.
 */
const parameterLine = (preview: UserStrategyPreview) => {
  const entries = Object.entries(preview.parameters).map(([key, value]) => `${key}: ${value}`)
  return entries.join(' · ') || '没有注册参数'
}

/**
 * The admitted data window, read from the data block itself and marked UTC. An
 * endpoint the data does not declare is said to be missing; no instant is invented
 * to stand in for it.
 */
const windowLine = (preview: UserStrategyPreview) => {
  const data = preview.data as { start_utc?: unknown; end_utc?: unknown } | undefined
  const endpoint = (value: unknown) =>
    typeof value === 'string' && value ? `${value} UTC` : '未记录'
  return `${endpoint(data?.start_utc)} → ${endpoint(data?.end_utc)}`
}

/**
 * One cost assumption exactly as the scenario declares it. A scenario that
 * declares none says so; nothing is filled in, guessed or priced here.
 */
const factLine = (value: unknown): string => {
  if (value === null || value === undefined) return '该场景未声明'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([key, item]) => `${key} ${factLine(item)}`).join(', ') : '声明为空'
  }
  return String(value)
}

const assumptionSummary = (name: string, value: unknown): string => {
  if (!value || typeof value !== 'object') return factLine(value)
  const block = value as Record<string, unknown>
  if (name === 'data') {
    const fingerprint = block.fingerprint as Record<string, unknown> | undefined
    return `${String(block.start_utc ?? '起点未记录')} → ${String(block.end_utc ?? '终点未记录')} · ${String(fingerprint?.record_count ?? block.record_count ?? '未记录')} 条记录`
  }
  const labels: Record<string, string> = name === 'risk'
    ? { max_notional: '最大名义金额', max_order_quantity: '最大委托数量', max_position_quantity: '最大持仓数量' }
    : { policy: '撮合策略', commission: '手续费', slippage: '滑点', latency: '执行延迟' }
  return Object.entries(block).map(([key, item]) => `${labels[key] ?? key} ${factLine(item)}`).join(' · ')
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

type TabKey = 'strategy' | 'experiments' | 'compare'

const DETAIL_TABS: { key: TabKey; label: string }[] = [
  { key: 'strategy', label: '策略' },
  { key: 'experiments', label: '实验' },
  { key: 'compare', label: '比较' },
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
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return
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
            {item.dataset_id && <p className="muted">{`选择的数据集：${item.dataset_id}`}</p>}
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


type EvidenceState =
  | { kind: 'empty' }
  | { kind: 'loading' }
  | { kind: 'unavailable'; note: string }
  | { kind: 'ready'; job: BacktestJob; report: BacktestReport; path: EquityPath | null; pathNote: string | null; analytics: TradeAnalytics | null }

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
export function EvidenceSection({ api, strategy }: { api: ApiAdapter; strategy: UserStrategy }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const visit = useLocation().key
  const runs = useMemo(() => selectableRuns(strategy), [strategy])
  // This card and the research panel below are two views of one selection, so both
  // read the address instead of keeping a choice of their own: the run a return link
  // names is the run shown here, a plain address is the latest frozen run, and an
  // address that cannot be verified shows no run at all rather than the latest one.
  const selection = useMemo(() => readEvidenceSelection(searchParams, runs), [searchParams, runs])
  const invalid = selection.kind === 'invalid'
  const { a: experiment, b: selectedB } = selectedRuns(selection, runs)
  const jobId = experiment?.job_id ?? null
  // Explicit navigation re-reads this selected report, including the same job.
  // Ordinary renders do not refresh, and nothing is dispatched by this read.
  const readKey = `${strategy.user_strategy_id}|${jobId ?? ""}|${visit}`
  // What was read is kept with the run it was read for. Nothing of another run is
  // ever shown as this one's — not even for the moment a new read is in flight —
  // so changing the selected frozen run shows that run's own load, never the
  // previous run's ready report.
  const [loaded, setLoaded] = useState<{ key: string; state: EvidenceState } | null>(null)
  const state: EvidenceState = jobId === null
    ? { kind: 'empty' }
    : loaded && loaded.key === readKey
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
      if (job.job_id !== jobId) throw new Error('读取到的任务与所选实验不一致')
      if (job.status !== 'succeeded' || !job.report_ready) {
        setLoaded({
          key: readKey,
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
          key: readKey,
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
      let analytics: TradeAnalytics | null = null
      try {
        const value = await api.getTradeAnalytics?.(jobId)
        if (value?.schema === 'ea.trade-analytics.v1' && value.run_id === job.engine_run_id
          && value.report_sha256 === job.report_sha256) analytics = value
      } catch { /* Unavailable stays absent. */ }
      if (!active) return
      setLoaded({ key: readKey, state: { kind: 'ready', job, report, path, pathNote, analytics } })
    }
    load().catch(error => {
      if (active) setLoaded({ key: readKey, state: { kind: 'unavailable', note: explain(error) } })
    })
    return () => { active = false }
  }, [api, jobId, readKey])

  // The criterion of the revision this run froze, read from that revision's own
  // document when it is still retained. A revision this page cannot read is said
  // to be unreadable rather than replaced by today's draft criterion.
  const frozenRevision = experiment?.payload.draft_revision ?? null
  const frozenDocument = frozenRevision === null
    ? undefined
    : [strategy.draft, ...strategy.history].find(item => item.revision === frozenRevision)
  const frozenCriterion = frozenRevision === null
    ? '这次运行没有记录冻结修订，因此这里不判断它的准则'
    : !frozenDocument
      ? `冻结修订 ${frozenRevision} 不在保留的修订记录里，它的准则在这里不可读`
      : isIntentDraft(frozenDocument)
        ? `冻结修订 ${frozenRevision} 是一份意图，没有声明准则`
        : frozenDocument.criterion === null
          ? `冻结修订 ${frozenRevision} 没有声明准则，这次运行无法支持或否定该想法`
          : `冻结修订 ${frozenRevision} 的准则 ${criterionLine(frozenDocument.criterion)}`
  // The sample size is the engine's own admitted record count on the run's job; a
  // job that does not carry one is named unreadable, never guessed, and no
  // sufficiency threshold is invented for it.
  const records = state.kind === 'ready'
    ? (state.job.input_identity as { record_count?: unknown }).record_count
    : undefined
  const sample = typeof records === 'number'
    ? `该运行输入 ${records} 条记录`
    : '该运行的输入记录数在这里不可读'

  return <section aria-label="证据" className="strategy-panel strategy-evidence">
    <header className="panel-head">
      <h2>运行 A · 正式报告</h2>
      {!invalid && runs.length > 1 && <label className="inline-field">运行 A
        <select aria-label="选择冻结运行 A" value={experiment?.launch_intent_id ?? ''}
          onChange={event => {
            const nextA = runs.find(item => item.launch_intent_id === event.target.value)
            if (!nextA) return
            // Choosing here is choosing A for both surfaces: the address is the
            // selection, so the research panel below follows this choice, a B that is
            // a different run stays, and a B that is the run chosen as A is dropped.
            setSearchParams(evidenceAddress(searchParams, nextA, selectedB))
          }}>
          {runs.map(item => <option key={item.launch_intent_id} value={item.launch_intent_id}>
            {`修订 ${item.payload.draft_revision} · ${item.launch_intent_id}`}
          </option>)}
        </select>
      </label>}
    </header>
    {invalid && <p className="notice error">
      地址里的证据选择无法核对，这里不显示任何运行或报告。
      <button type="button" onClick={() => setSearchParams(evidenceAddress(searchParams, null, null), { replace: true })}>清除无效的证据选择</button>
    </p>}
    {!invalid && state.kind === 'empty' && <p className="muted">
      还没有冻结运行。在「实验」里确认一次实验之后，这里会显示那次运行自己的正式报告——只有运行成功，报告才存在。
    </p>}
    {!invalid && state.kind === 'loading' && <p className="muted">正在读取该运行的状态与正式报告……</p>}
    {!invalid && state.kind === 'unavailable' && <p className="notice error">{state.note}</p>}
    {!invalid && state.kind === 'ready' && <>
      <p className="muted">{state.job.input_snapshot?.scenario.data
        ? `${state.job.input_snapshot.scenario.data.start_utc} → ${state.job.input_snapshot.scenario.data.end_utc} · UTC`
        : '数据窗口未记录'} · 冻结修订 {frozenRevision ?? '未记录'}</p>
      <p className="evidence-origin">
        <span className="chip">事实 · 引擎</span>
        <span className="muted">{`正式报告由引擎产出，不是模型输出 · 运行状态 ${state.job.status}`}</span>
      </p>
      <EvidenceMetrics report={state.report} />
      {state.path
        ? <div className="evidence-curve"><EquityCurve analysis={state.path} /></div>
        : <p className="muted">{state.pathNote ?? '该运行没有可读的权益曲线。'}</p>}
      {/* The limits stand beside the figures they qualify: one window and one frozen
          revision, and only the sample size the run's own job recorded. What is
          claimed is what this run cannot do on its own, not an inventory of things
          this page did not read. */}
      <p className="muted evidence-limits">
        {`${frozenCriterion} · ${sample}。这是一次单窗口的冻结运行：成功只表示执行完成，不能证明研究有效或稳健。同一数据上的重复运行和行情条数不等于独立验证样本。`}
      </p>
      <details className="provenance report-assumptions">
        <summary>数据与假设</summary>
        <dl className="summary-list">{Object.entries(experiment?.payload.normalized ?? {}).filter(([name]) => ['data', 'execution', 'risk'].includes(name)).map(([name, value]) =>
          <div key={name}><dt>{{ data: '数据', execution: '成本与执行', risk: '风险限制' }[name]}</dt><dd>{assumptionSummary(name, value)}</dd></div>)}</dl>
        <details><summary>完整来源与身份</summary><pre>{JSON.stringify(experiment?.payload.normalized ?? {}, null, 2)}</pre></details>
        <p className="muted">{sample} · 参数：{Object.entries(experiment?.payload.parameters ?? {}).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p>
      </details>
      <details className="provenance report-trades"><summary>成交与完整报告</summary>
        <dl className="summary-list">
          <div><dt>期末现金</dt><dd>{state.report.economics.ending_cash.map(item => `${item.amount} ${item.currency ?? state.report.economics.currency ?? '币种未记录'}`).join(' · ')}</dd></div>
          <div><dt>期末头寸</dt><dd>{state.report.economics.ending_positions.map(item => `${item.venue}:${item.symbol} ${item.quantity}`).join(' · ') || '无'}</dd></div>
          {state.report.schema === 'ea.backtest-report.v3' ? <MultiRoundTripExecution economics={state.report.economics} />
            : state.report.schema === 'ea.backtest-report.v2' ? <RoundTripExecution economics={state.report.economics} />
            : <><div><dt>订单</dt><dd>{state.report.economics.execution.order ? `${state.report.economics.execution.order.side} ${state.report.economics.execution.order.quantity}` : '无'}</dd></div>
                <div><dt>成交</dt><dd>{state.report.economics.execution.fill ? `${state.report.economics.execution.fill.quantity} @ ${state.report.economics.execution.fill.price}` : '无'}</dd></div></>}
        </dl>
        <TradeAnalyticsPanel analytics={state.analytics} />
        <div className="artifact-links"><a href={api.artifactUrl(state.job.job_id, 'report.json')}>下载正式报告</a><Link to={`/backtests/${encodeURIComponent(state.job.job_id)}`}>独立报告链接</Link></div>
      </details>
      <details className="provenance"><summary>留出验证与候选</summary>
        <p className="muted">使用本实验的冻结输入开展独立验证，再记录候选决定。</p>
        <Link to={`/holdouts/new/${encodeURIComponent(state.job.job_id)}`}>创建本实验的留出验证</Link>
        <CandidateCreate key={state.job.job_id} api={api} jobId={state.job.job_id} />
      </details>
      <details className="provenance">
        <summary>技术细节与来源</summary>
        <dl className="summary-list">
          <div><dt>Web 任务</dt><dd>{state.job.job_id}</dd></div>
          <div><dt>Engine 运行</dt><dd>{state.job.engine_run_id ?? '未记录'}</dd></div>
          <div><dt>报告 schema</dt><dd>{state.report.schema}</dd></div>
          <div><dt>报告 SHA-256</dt><dd>{state.job.report_sha256 ?? '未记录'}</dd></div>
          <div><dt>输入 SHA-256</dt><dd>{state.job.input_sha256 ?? '未记录'}</dd></div>
        </dl>
        <p className="muted">原始产物下载（与上面这份报告同一身份）：</p>
        <div className="artifact-links">
          <a href={api.artifactUrl(state.job.job_id, 'report.json')}>下载 report.json</a>
          <a href={api.artifactUrl(state.job.job_id, 'summary.txt')}>下载 summary.txt</a>
          {state.path && <a href={api.artifactUrl(state.job.job_id, 'equity-path.json')}>下载 equity-path.json</a>}
        </div>
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
function BindScenario({ api, strategy, scenarios, catalogReady, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  catalogReady: boolean
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
      {!catalogReady && <small>场景目录尚未读到，请在上方重新检查。</small>}
      {catalogReady && !usable.length && <small>当前没有可用的有效注册场景。请按上方「准备场景与数据」完成配置，再重新检查。</small>}
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

/** One catalog dataset this panel may offer, exactly as the host lists it. */
type SelectableDataset = {
  dataset_id: string
  valid: boolean
  origin?: 'registered' | 'upload'
  venue?: string
  symbol?: string
  record_count?: number
  replay_start_utc?: string
  replay_end_utc?: string
}

/**
 * One explicit dataset choice for the draft on screen.
 *
 * The user picks an identity from the workspace's own dataset catalog — never a
 * path and never a digest — and the host resolves that identity through its
 * registered read-only root and its controlled upload store, binds the published
 * version it finds there, and validates the whole resulting draft against that
 * data before it writes anything. Choosing again is a new revision, and that is
 * what makes a preview taken before the choice no longer confirmable: a
 * confirmation carries the admitted input's own identity back, so a stale one is
 * refused instead of silently run. This panel writes no field itself, calls no
 * model and starts no run.
 */
function SelectDataset({ api, strategy, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const location = useLocation()
  const importSearch = new URLSearchParams({ research: strategy.user_strategy_id, revision: String(strategy.draft.revision), return: location.pathname + location.search })
  const list = api.listDatasets
  // A workspace that serves no dataset catalog has nothing to offer, which is a
  // state this panel starts in rather than one it writes from inside an effect.
  const [items, setItems] = useState<SelectableDataset[] | null>(list ? null : [])
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState(false)
  const attempts = useRef(0)
  const inFlight = useRef(false)
  const visit = useRef(0)
  const pendingKey = `quine.dataset-selection.${strategy.user_strategy_id}.${strategy.draft.revision}`
  const [unknown, setUnknown] = useState(() => sessionStorage.getItem(pendingKey) !== null)
  useEffect(() => { visit.current += 1; return () => { visit.current += 1 } }, [strategy.user_strategy_id, strategy.draft.revision])
  const draft = strategy.draft
  const current = isIntentDraft(draft) ? null : draft.dataset_id
  const revision = draft.revision

  useEffect(() => {
    if (!list) return
    let active = true
    list()
      .then(value => { if (active) setItems(value.filter(item => item.valid)) })
      .catch(() => { if (active) setItems([]) })
    return () => { active = false }
  }, [list])

  const usable = items ?? []
  const candidate = usable.find(item => item.dataset_id === selected) ?? null

  const choose = async () => {
    if (inFlight.current || unknown) return
    const ticket = visit.current
    if (!candidate) {
      failed('请先选择一个数据集：没有被选中的数据集就不会被绑定。')
      return
    }
    const selectDataset = api.selectUserStrategyDataset
    if (!selectDataset) {
      failed('这个工作区不提供数据集选择。')
      return
    }
    inFlight.current = true
    setBusy(true)
    failed('')
    const attempt = attempts.current
    const operation = `dataset-${crypto.randomUUID()}`
    try {
      // One explicit choice, one operation id: a retry of this click replays the
      // revision it already produced instead of selecting a second time.
      sessionStorage.setItem(pendingKey, operation)
      const updated = await selectDataset(strategy.user_strategy_id, {
        operation_id: operation,
        base_revision: revision,
        dataset_id: candidate.dataset_id,
      })
      sessionStorage.removeItem(pendingKey)
      if (ticket !== visit.current) return
      attempts.current = attempt + 1
      setSelected('')
      changed(updated)
    } catch (error) {
      if (ticket !== visit.current) return
      if (error instanceof UserStrategyRequestError && ['stale_base_revision', 'operation_conflict', 'user_strategy_invalid'].includes(error.code)) sessionStorage.removeItem(pendingKey)
      else setUnknown(true)
      failed(explain(error))
    } finally {
      inFlight.current = false
      if (ticket === visit.current) setBusy(false)
    }
  }

  return <section aria-label="选择数据集" className="strategy-panel">
    <header className="panel-head">
      <h2>选择数据集</h2><Link to={`/data?${importSearch}#data-upload`}>导入 CSV 用于此研究</Link>
      <span className="chip">草稿数据</span>
    </header>
    <p className="muted">
      选择已发布版本，后端兼容性校验通过后生成新修订。
    </p>
    <div className="field">
      <label htmlFor="select-dataset">数据集</label>
      <select id="select-dataset" value={selected} disabled={busy}
        onChange={event => { setSelected(event.target.value); failed('') }}>
        <option value="">选择一个数据集</option>
        {usable.map(item => <option key={item.dataset_id} value={item.dataset_id}>
          {`${item.dataset_id} · ${item.origin === 'upload' ? '已上传版本' : '已注册'}`
            + (item.venue && item.symbol ? ` · ${item.venue}:${item.symbol}` : '')}
        </option>)}
      </select>
      {items === null
        ? <small>正在读取数据集目录……</small>
        : usable.length === 0
          && <small>当前没有可用的数据集，因此现在没有可以选择的数据来源。</small>}
    </div>
    {candidate && <dl className="summary-list">
      <div><dt>来源</dt><dd>{candidate.origin === 'upload' ? '已上传版本' : '已注册文件'}</dd></div>
      {candidate.venue && candidate.symbol
        && <div><dt>标的</dt><dd>{`${candidate.venue}:${candidate.symbol}`}</dd></div>}
      {candidate.record_count !== undefined
        && <div><dt>记录数</dt><dd>{candidate.record_count}</dd></div>}
    </dl>}
    <p className="muted">
      {current
        ? `当前草稿的数据来源：${current}。再次选择会写下新的修订；在那之前预览过的实验不再可确认。`
        : '当前草稿使用它已注册场景自己的数据来源；选择一次之后，草稿会改用你选中的数据集。'}
    </p>
    <div className="actions">
      <button type="button" className="primary" onClick={choose}
        disabled={busy || unknown || !candidate}>选择这个数据集</button>
    </div>
    {unknown && <p role="alert">选择结果未知，请重新加载研究核对当前数据。不会自动重发。</p>}
    <p className="muted">选择不会调用模型，也不会启动任何运行。</p>
  </section>
}

/**
 * The next step of the selected strategy, in one compact strip: the most recent
 * revision that was actually frozen and the real state of the job it dispatched,
 * read from that job itself through the same cell the table uses. The revision that
 * is open is not repeated here — the title chip and the facts already name it — so
 * the strip carries only what is frozen and what is not. A draft no frozen run
 * covers yet is offered one explicit way to the experiment tab; that button only
 * opens the tab, it reads no preview and starts no run.
 */
function NextStep({ api, strategy, openExperiments }: {
  api: ApiAdapter
  strategy: UserStrategy
  openExperiments: () => void
}) {
  const latest = strategy.experiments.at(-1)
  const draft = strategy.draft
  // A preview is still wanted while no created run covers the revision on screen.
  const covered = strategy.experiments.some(item =>
    item.status === 'created' && item.payload.draft_revision === draft.revision)
  const needsPreview = !isIntentDraft(draft) && !covered
  // A created experiment has a real job; its state is that job's own, never the
  // dispatch record's. An outcome this page has not read is not claimed here.
  const jobId = latest?.status === 'created' ? latest.job_id : null
  return <section aria-label="下一步" className="next-step">
    <dl>
      <div><dt>最近冻结</dt><dd>{latest
        ? <>
            {`v${latest.payload.draft_revision}`}
            {jobId
              ? <> · <JobStatusCell key={jobId} api={api} jobId={jobId} strategyId={strategy.user_strategy_id} /></>
              : ` · ${EXPERIMENT_STATUS_LABELS[latest.status]}`}
          </>
        : isIntentDraft(draft) ? '尚未绑定场景' : '还没有冻结运行'}</dd></div>
    </dl>
    {needsPreview && <>
      <p className="muted">本草稿尚未冻结为实验。</p>
      <button type="button" onClick={openExperiments}>去预览这个草稿</button>
    </>}
  </section>
}

/** A section of the selected strategy: the tab bar and the section it opens. */
function StrategyDetail({ api, strategy, scenarios, catalogReady, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  catalogReady: boolean
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab')
  const tab: TabKey = requested === 'strategy' || requested === 'experiments' || requested === 'compare'
    ? requested : params.has('evidence_b') ? 'compare' : hasRun ? 'experiments' : 'strategy'
  const selectTab = (next: TabKey) => {
    const search = new URLSearchParams(params)
    search.set('tab', next)
    setParams(search)
  }
  const idBase = `detail-${strategy.user_strategy_id}`
  const pending = strategy.proposals.filter(item => item.status === 'open').length
  return <section className="strategy-detail" id="strategy-detail" tabIndex={-1} aria-label="策略详情">
    <header className="strategy-title">
      <h1>{strategy.name}</h1>
      <span className="chip chip-draft">{revisionChip(strategy.draft)}</span>
      <a className="jump-link jump-to-ai" href="#strategy-message">讨论这个研究</a>
      <Link className="strategy-open" to="/strategies">返回研究列表</Link>
    </header>
    <p className="research-context">{isIntentDraft(strategy.draft) ? '尚未绑定场景' : `${strategy.draft.venue}:${strategy.draft.symbol} · ${strategy.draft.scenario_id}`} · 历史回测</p>
    <div className="research-status-line">
      <NextStep api={api} strategy={strategy} openExperiments={() => selectTab('experiments')} />
      <a className={`pending-changes${pending ? ' has-pending' : ''}`} href="#strategy-ai-column">{pending ? `待处理：${pending} 项策略修改` : '暂无待处理修改'}</a>
    </div>
    <DetailTabs active={tab} select={selectTab} idBase={idBase} />
    <div className="strategy-tabpanel" role="tabpanel" id={`${idBase}-panel-${tab}`}
      aria-labelledby={`${idBase}-tab-${tab}`} tabIndex={0}>
      {tab === 'strategy' && <>
        <DraftFacts draft={strategy.draft} />
        <IntentSection strategy={strategy} />
        {isIntentDraft(strategy.draft) ? <BindScenario api={api} strategy={strategy} scenarios={scenarios}
          catalogReady={catalogReady} changed={changed} failed={failed} />
          : <><DraftEditor key={strategy.user_strategy_id} api={api} strategy={strategy} changed={changed} /><details className="provenance"><summary>场景规则与来源</summary><RulesSection draft={strategy.draft} /></details><SelectDataset api={api} strategy={strategy} changed={changed} failed={failed} /></>}
        <details className="provenance"><summary>修订记录</summary><RevisionsSection strategy={strategy} /></details>
      </>}
      {tab === 'experiments' && <>
        <EvidenceSection api={api} strategy={strategy} />
        <details className="experiment-controls" open={!hasRun || undefined}>
          <summary>新实验与运行记录</summary>
          <ExperimentPanel key={`${strategy.user_strategy_id}-${strategy.draft.revision}`}
            api={api} strategy={strategy} changed={changed} failed={failed} />
        </details>
      </>}
      {tab === 'compare' && <StrategyEvidenceResearch api={api} strategy={strategy} changed={changed} failed={failed} />}
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
 * The current turn and proposal share one conversation scroller. The composer
 * stays below it and always names the selected draft and frozen A/B evidence.
 * A refused or unknown call never borrows an older answer. The previous turns
 * remain inspectable, and a new route visit requires its own approval.
 */
const TURN_CALL_CEILING = 4
const OBSERVATION_INTERVAL_MS = 4000

/** What one tool call of the running turn is doing, in the turn's own order. */
type LiveTool = { id: string; tool: string; state: 'running' | 'done' | 'failed'; detail: string }

const TOOL_LABELS: Record<string, string> = {
  describe_data: '读取数据说明',
  experiment_status: '读取实验状态与报告',
  propose_change: '提出字段修改',
}

/**
 * One turn's request identity. A fresh uuid-like name per attempt, so two attempts
 * are two turns and a retry of one click is one, and never anything derived from
 * how long the conversation has grown — the turn itself may add calls to it.
 */
function newTurnId(): string {
  const raw = globalThis.crypto?.randomUUID?.()
  const body = (raw ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`)
    .replace(/[^A-Za-z0-9]/g, '')
  return `turn-${body.slice(0, 32)}`
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/**
 * The frozen input one confirmation would run, exactly as the host admitted it.
 *
 * Nothing here is derived: the revision, the window, the costs and the parameters
 * are the preview's own fields, and the identity the confirm button carries back is
 * the one the host froze. The card grants no permission by itself — a run starts
 * only from the explicit click below, and never from the model call approval.
 */
function ConversationConfirmation({ pending, preview, identity, busy, error, confirm, decline }: {
  pending: UserStrategyConversationPending
  preview: UserStrategyPreview
  /** The exact preview identity the host froze, carried back unchanged. */
  identity: string
  busy: boolean
  error: string
  confirm: () => void
  decline: () => void
}) {
  return <section aria-label="实验确认" className="strategy-panel conversation-confirmation">
    <header className="panel-head"><h2>确认运行这次实验</h2></header>
    <p>修改已经应用到这个策略。下面是此刻冻结的输入；只有你按下确认，它才会运行一次回测。</p>
    <dl className="summary-list">
      <div><dt>冻结修订</dt><dd>{preview.draft_revision}</dd></div>
      <div><dt>注册参数</dt><dd>{parameterLine(preview)}</dd></div>
      <div><dt>数据</dt><dd>{dataLine(preview)}</dd></div>
      <div><dt>数据窗口</dt><dd>{windowLine(preview)}</dd></div>
      <div><dt>成本假设</dt><dd>{costLine(preview)}</dd></div>
    </dl>
    <p className="muted">{`本次确认消耗 1 次回测；批准过的模型调用不是运行许可。`}</p>
    <p className="muted">{`确认对象：意图 ${pending.launch_intent_id} · 修订 ${pending.base_revision} · 预览 ${identity.slice(0, 16)}…`}</p>
    <div className="actions">
      <button type="button" className="primary" onClick={confirm} disabled={busy}>确认并运行一次回测</button>
      <button type="button" onClick={decline} disabled={busy}>暂不运行</button>
    </div>
    {error && <p role="alert" className="notice error">{error}</p>}
  </section>
}

/**
 * One confirmed run this conversation is watching.
 *
 * Reading its status again is the only thing the polite resume does: it queries the
 * very intent the user confirmed, so a run that is still going is never started
 * again, and a reply that was lost is never a reason for a second confirmation.
 */
function ConversationObservation({ pending, runStatus, busy, error, refresh }: {
  pending: UserStrategyConversationPending
  runStatus: string
  busy: boolean
  error: string
  refresh: () => void
}) {
  return <section aria-label="实验观察" className="strategy-panel conversation-observation">
    <header className="panel-head"><h2>实验运行中</h2></header>
    <p>{`这次对话在等待它自己确认的那次运行：${pending.job_id ?? '尚未登记运行'} · ${JOB_STATUS_LABELS[runStatus as BacktestJob['status']] ?? runStatus}`}</p>
    <p className="muted">页面会自动查询同一次运行的进度；这不是新的运行，也不会重新批准任何模型调用。</p>
    <div className="actions"><button type="button" onClick={refresh} disabled={busy}>立即刷新运行状态</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
  </section>
}

function ChatPanel({ api, strategy, strategies, changed, proposalSlot, modelSendBlock }: {
  api: ApiAdapter
  strategy: UserStrategy
  strategies: UserStrategy[]
  changed: (strategy: UserStrategy) => void
  /** Null only after the current settings read reports configured=true. */
  modelSendBlock: string | null
  /** The current proposal, mounted after the turn it belongs to and before history. */
  proposalSlot?: ReactNode
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const [params] = useSearchParams()
  const runs = useMemo(() => selectableRuns(strategy), [strategy])
  const selection = useMemo(() => readEvidenceSelection(params, runs), [params, runs])
  const { a, b } = selectedRuns(selection, runs)
  const aState = useRunRead(api, a?.job_id ?? null)
  const bState = useRunRead(api, b?.job_id ?? null)
  const evidenceReady = selection.kind !== 'invalid' && (!a || aState?.kind === 'ready') && (!b || bState?.kind === 'ready')
  const requestKey = `${strategy.user_strategy_id}|${strategy.draft.revision}|${a?.job_id ?? ''}|${b?.job_id ?? ''}`
  const at = `${location.key}|${requestKey}`
  const [visit, setVisit] = useState({ at, id: 0 })
  const visitKey = `${visit.at === at ? visit.id : visit.id + 1}|${at}`
  const shown = useRef(visitKey)
  const mounted = useRef(false)
  const inFlight = useRef(false)
  useLayoutEffect(() => { shown.current = visitKey })
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [message, setMessage] = useState('')
  const [approvedAt, setApprovedAt] = useState<string | null>(null)
  const approved = approvedAt === visitKey
  const setApproved = (value: boolean) => setApprovedAt(value ? visitKey : null)
  if (visit.at !== at) {
    setVisit({ at, id: visit.id + 1 })
    setApprovedAt(null)
  }
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  // The conversation's own durable state, read from the host: what it is waiting
  // for and what it may still spend. A reload restores exactly this, so the panel
  // never depends on having watched the stream that produced it.
  const [conversation, setConversation] = useState<UserStrategyConversation | null>(null)
  const [conversationNote, setConversationNote] = useState('')
  // The turn being watched right now: deltas the provider really sent, and the
  // tools it really asked for. It is cleared when the turn ends, because the
  // canonical record is what is shown from then on.
  const [live, setLive] = useState<{ text: string; tools: LiveTool[] } | null>(null)
  const [runStatus, setRunStatus] = useState('')
  const current = useRef<AbortController | null>(null)
  const decided = useRef<Set<string>>(new Set())
  const latest = strategy.calls.at(-1)
  // One turn is one root call and every call that names it, so a turn that spent
  // several model calls shows its own prompt, its own answer and its own history
  // once, and never borrows a neighbouring turn's.
  const turnId = latest ? (latest.turn_id ?? latest.call_id) : null
  const turnCalls = turnId
    ? strategy.calls.filter(call => (call.turn_id ?? call.call_id) === turnId)
    : []
  const turnCallIds = new Set(turnCalls.map(call => call.call_id))
  const turnMessages = strategy.messages.filter(item => turnCallIds.has(item.call_id))
  const prompt = turnMessages.filter(item => item.role === 'user').at(-1)
  const answers = turnMessages.filter(item => item.role === 'model')
  const answer = answers.at(-1)
  const earlier = strategy.messages.filter(item => !turnCallIds.has(item.call_id))
  const root = turnCalls.find(call => call.call_id === turnId) ?? latest
  const unresolved = strategy.calls.some(call =>
    call.status === 'pending' || call.status === 'dispatched')
  const streaming = Boolean(api.streamUserStrategyMessage)

  // Each explicit send approves a bounded turn; accumulated usage never disables it.
  const turnLimit = conversation?.budget.max_model_calls_per_turn ?? null
  const turnBound = turnLimit === null ? 0 : Math.max(0, Math.min(TURN_CALL_CEILING, turnLimit))
  const turnBudget: ApprovedBudget = streaming && turnLimit !== null
    ? { max_model_calls: turnBound, max_backtests: 0 }
    : CALL_APPROVAL
  const awaitingRunDecision = conversation?.pending?.phase === 'awaiting_confirmation'
    || conversation?.pending?.phase === 'awaiting_observation'
  const canSend = (!streaming || turnBound > 0) && !awaitingRunDecision

  // The frozen confirmation this turn is waiting on, exactly as the host recorded
  // it. All three identities are required: a card that cannot name what it would
  // run is not shown, and its confirm button can therefore never name a guess.
  const confirmation = conversation?.pending?.phase === 'awaiting_confirmation'
    && conversation.preview && conversation.pending.preview_identity
    ? {
        pending: conversation.pending,
        preview: conversation.preview,
        identity: conversation.pending.preview_identity,
      }
    : null

  const active = () => mounted.current && shown.current === visitKey

  const refresh = useCallback(async () => {
    const read = api.userStrategyConversation
    if (!read) return
    try {
      const value = await read(strategy.user_strategy_id)
      if (!mounted.current || value.strategy_id !== strategy.user_strategy_id) return
      setConversation(value)
      setConversationNote('')
    } catch (error) {
      if (mounted.current) setConversationNote(explain(error))
    }
  }, [api, strategy.user_strategy_id])

  // Read the waiting decision on mount and whenever this strategy's own record
  // moves: after an apply the revision changes and this panel remounts, so the
  // resume below is driven by the durable records and never by a stale click.
  const readConversation = api.userStrategyConversation
  useEffect(() => {
    if (!readConversation) return
    let open = true
    readConversation(strategy.user_strategy_id)
      .then(value => {
        if (!open || value.strategy_id !== strategy.user_strategy_id) return
        setConversation(value)
        setConversationNote('')
      })
      .catch(error => { if (open) setConversationNote(explain(error)) })
    return () => { open = false }
  }, [readConversation, strategy.user_strategy_id, strategy.updated_at, strategy.draft.revision])

  useEffect(() => () => { current.current?.abort() }, [])

  const apply = (event: UserStrategyConversationEvent) => {
    if (event.kind === 'text_delta') {
      setLive(state => ({ text: (state?.text ?? '') + event.text, tools: state?.tools ?? [] }))
      return
    }
    if (event.kind === 'tool_start') {
      setLive(state => ({
        text: state?.text ?? '',
        tools: [...(state?.tools ?? []), { id: event.tool_id, tool: event.tool, state: 'running', detail: '' }],
      }))
      return
    }
    if (event.kind === 'tool_end' || event.kind === 'tool_error') {
      const failed = event.kind === 'tool_error'
      setLive(state => ({
        text: state?.text ?? '',
        tools: (state?.tools ?? []).map(item => item.id === event.tool_id
          ? { ...item, state: failed ? 'failed' : 'done', detail: failed ? event.error : event.text }
          : item),
      }))
      // A tool that failed is reported as a failure; it is never read as success.
      if (failed) setNote(event.error)
      return
    }
    if (event.kind === 'state') {
      if (event.strategy?.user_strategy_id === strategy.user_strategy_id) changed(event.strategy)
      if (event.run_status) setRunStatus(event.run_status)
      return
    }
    if (event.kind === 'error') {
      setNote(event.error)
      return
    }
    if (event.kind === 'interrupt' || event.kind === 'done') {
      if (event.strategy.user_strategy_id !== strategy.user_strategy_id) return
      changed(event.strategy)
      setLive(null)
      setRunStatus('')
      void refresh()
      return
    }
  }

  const runStream = async (
    source: (signal: AbortSignal) => Promise<AsyncGenerator<UserStrategyConversationEvent>>,
  ) => {
    // Observation and explicit clicks share one request slot. A timer must never
    // abort a model response (the provider may already have charged that call).
    if (current.current) return
    const controller = new AbortController()
    current.current = controller
    setBusy(true)
    setNote('')
    try {
      const events = await source(controller.signal)
      for await (const event of events) {
        if (!active()) return
        apply(event)
      }
    } catch (error) {
      if (active() && !isAbort(error)) {
        setNote(explain(error))
        // The page never guesses what happened: it re-reads the durable record.
        void refresh()
        const read = api.getUserStrategy
        if (read) {
          try {
            const value = await read(strategy.user_strategy_id)
            if (active() && value.user_strategy_id === strategy.user_strategy_id) changed(value)
          } catch { /* the notice above already says the read failed */ }
        }
      }
    } finally {
      if (current.current === controller) {
        current.current = null
        if (mounted.current) setBusy(false)
      }
    }
  }

  const decide = (input: UserStrategyResumeRequest) => {
    const resume = api.resumeUserStrategyConversation
    if (!resume) {
      setNote('这个工作区不提供对话续跑。')
      return
    }
    void runStream(signal => resume(strategy.user_strategy_id, input, signal))
  }

  // A decision the user already made is carried to the turn that is waiting for
  // it, once per turn and once per decision. Applying or rejecting happened through
  // the proposal's own operation first; this starts nothing by itself.
  const pendingProposal = conversation?.pending?.phase === 'awaiting_proposal'
    ? conversation.pending
    : null
  const recorded = pendingProposal
    ? strategy.proposals.find(item => item.proposal_id === pendingProposal.proposal_id)
    : undefined
  useEffect(() => {
    if (!api.resumeUserStrategyConversation || !pendingProposal || !recorded) return
    if (recorded.status !== 'applied' && recorded.status !== 'rejected') return
    const key = `${pendingProposal.turn_id}:${recorded.status}`
    if (decided.current.has(key)) return
    decided.current.add(key)
    decide({
      turn_id: pendingProposal.turn_id,
      action: recorded.status === 'applied' ? 'applied' : 'rejected',
      proposal_id: pendingProposal.proposal_id,
    })
    // decide is derived from the values above; the guard makes one attempt per turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api.resumeUserStrategyConversation, pendingProposal?.turn_id, pendingProposal?.proposal_id, recorded?.status])

  // A run this conversation confirmed is watched by asking about the very same
  // intent, one query at a time, while this strategy stays open.
  const observationTurn = conversation?.pending?.phase === 'awaiting_observation'
    ? conversation.pending.turn_id
    : null
  useEffect(() => {
    if (!observationTurn || !api.resumeUserStrategyConversation) return
    let stopped = false
    let handle: ReturnType<typeof setTimeout> | null = null
    const tick = () => {
      handle = setTimeout(async () => {
        if (stopped) return
        await runStream(signal => api.resumeUserStrategyConversation!(
          strategy.user_strategy_id, { turn_id: observationTurn, action: 'observe' }, signal))
        if (!stopped) tick()
      }, OBSERVATION_INTERVAL_MS)
    }
    tick()
    return () => { stopped = true; if (handle) clearTimeout(handle) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [observationTurn, api.resumeUserStrategyConversation, strategy.user_strategy_id])

  const ask = async () => {
    // UI readiness is only a sending precondition; the backend still owns the
    // actual configuration, approval and provider execution checks.
    if (inFlight.current || current.current || unresolved || !evidenceReady || awaitingRunDecision) return
    if (modelSendBlock !== null) {
      setNote(modelSendBlock)
      return
    }
    if (!approved) {
      setNote('在询问模型之前，需要先批准这次模型调用。')
      return
    }
    const evidence = a ? { evidence_job_ids: [a.job_id!, ...(b ? [b.job_id!] : [])] } : {}
    const text = message.trim()
    const revision = strategy.draft.revision
    inFlight.current = true
    setLive(streaming ? { text: '', tools: [] } : null)
    setNote('')
    try {
      if (streaming) {
        const send = api.streamUserStrategyMessage!
        if (!canSend) {
          setNote('请等待本轮权限读取完成，并处理当前待确认的实验。')
          return
        }
        await runStream(async signal => {
          const events = await send(strategy.user_strategy_id, {
            call_id: newTurnId(),
            base_revision: revision,
            text,
            approved_budget: turnBudget,
            ...evidence,
          }, signal)
          return events
        })
      } else {
        const send = api.sendUserStrategyMessage
        if (!send) {
          setNote('这个工作区不提供模型协作。')
          return
        }
        const updated = await send(strategy.user_strategy_id, {
          call_id: newTurnId(),
          base_revision: revision,
          text,
          approved_budget: CALL_APPROVAL,
          ...evidence,
        })
        if (!active() || updated.user_strategy_id !== strategy.user_strategy_id) return
        changed(updated)
      }
      if (active()) {
        setMessage('')
        setApproved(false)
      }
    } catch (error) {
      if (active()) setNote(explain(error))
    } finally {
      inFlight.current = false
      if (mounted.current) setBusy(false)
    }
  }

  return <section aria-label="AI 协作" className="strategy-panel strategy-chat">
    <header className="chat-head">
      <h2><span aria-hidden="true">✦ </span>AI 研究助手</h2>
      {strategies.length > 0 && <label className="inline-field">共同编辑
        <select aria-label="当前策略" value={strategy.user_strategy_id}
          onChange={event => navigate(`/strategies/${encodeURIComponent(event.target.value)}`)}>
          {strategies.map(item => <option key={item.user_strategy_id} value={item.user_strategy_id}>
            {item.name}
          </option>)}
        </select>
      </label>}
    </header>
    <p className="ai-context" aria-label="AI 当前上下文">{strategy.name} · 草稿 v{strategy.draft.revision}
      {selection.kind === 'invalid' ? ' · 证据选择无效，不能发送' : a
        ? ` · A：修订 ${a.payload.draft_revision}${b ? ` / B：修订 ${b.payload.draft_revision}` : ''}`
        : ' · 当前草稿，尚无实验报告'}
    </p>
    {!evidenceReady && <p className="notice" role="status">所选证据尚未就绪，请检查实验或比较标签。</p>}
    <div className="chat-body">
    {!latest && <div className="chat-empty-guidance">
      <p>从当前草稿和所选实验开始讨论。</p>
      <p className="muted">先选一个问题，也可以直接输入。发送前需要批准这次模型调用。</p>
      <div className="chat-suggestions">
        <button type="button" onClick={() => setMessage(a ? '请解读当前所选实验的结果、成本和局限。' : '请帮我明确当前策略想要验证的假设。')}>{a ? '解读所选实验' : '明确研究假设'}</button>
        <button type="button" onClick={() => setMessage(b ? '请比较当前 A/B 实验，指出差异、风险和仍缺少的证据。' : '当前研究还缺少哪些风险和成本证据？')}>{b ? '比较 A/B 证据' : '检查风险与成本'}</button>
      </div>
    </div>}
    {isIntentDraft(strategy.draft) && <p className="notice">
      这条策略还没有绑定场景：这次对话只能澄清这个想法，模型不会提出任何字段修改，也不会运行任何东西。
      绑定场景之后，它的建议才会变成可以应用的修改。
    </p>}
    {unresolved && <p className="notice">
      还有一次已记录的调用没有结论。在它落实之前不会再问下一次，因此不会覆盖任何东西。
    </p>}
    {(latest || live) && <section aria-label="当前一轮" className="turn">
      {/* A turn that has not recorded its first call yet is still a turn: it says
          that it is asking the model rather than showing nothing at all. */}
      <p className="turn-head">
        {latest && !live
          ? <span className={`status status-${latest.status}`}>{CALL_STATUS_LABELS[latest.status]}</span>
          : <span className="status status-dispatched">正在请求模型</span>}
        <span className="muted">{root && !live
          ? `草稿修订 ${root.base_revision}`
          : `草稿修订 ${strategy.draft.revision} · 本轮刚开始`}</span>
      </p>
      {!live && turnCalls.length > 1 && <details className="provenance">
        <summary>{`本轮共 ${turnCalls.length} 次模型调用`}</summary>
        {turnCalls.map(call => <p key={call.call_id}>{`${call.call_id} · ${CALL_STATUS_LABELS[call.status]} · ${usageLine(call)}`}</p>)}
      </details>}
      {/* The prompt that produced this turn, kept behind a disclosure so the answer
          is what is read first; the exact instant stays on the time element. */}
      {!live && prompt && <details className="provenance turn-prompt">
        <summary>{`本轮问题 · ${utcClock(prompt.at)}`}</summary>
        <p>{prompt.text}</p>
      </details>}
      {/* While the turn is really running, its own deltas are shown as they arrive.
          They are never a finished answer cut into pieces: a turn that produced no
          delta shows none, and the canonical answer replaces this when it lands. */}
      {live && <div className="turn-answer live-answer" role="status" aria-live="polite">
        <div className="answer-scroll" role="group" aria-label="正在生成的回答" tabIndex={0}>
          <p>{live.text || '正在等待模型…'}</p>
        </div>
        <p className="muted">模型推断 — 不是正式证据。以上内容正在生成，尚未完成。</p>
      </div>}
      {live && live.tools.length > 0 && <ul className="tool-list" aria-label="本轮工具调用">
        {live.tools.map(item => <li key={item.id} className={`tool tool-${item.state}`}>
          <strong>{TOOL_LABELS[item.tool] ?? item.tool}</strong>
          {item.state === 'running' ? ' · 读取中…'
            : item.state === 'done' ? ' · 已完成'
              : ` · 失败：${item.detail}`}
        </li>)}
      </ul>}
      {!live && latest && (answer
        ? <div className="turn-answer">
            {/* The full answer shares the conversation scroller. The composer
                remains outside it, while keyboard users can focus this answer. */}
            <div className="answer-scroll" role="group" aria-label="本轮回答" tabIndex={0}>
              {answers.map(item => <p key={item.message_id}>{item.text}</p>)}
            </div>
            <p className="muted">模型推断 — 不是正式证据。</p>
            <p className="muted">
              {`草稿修订 ${answer.base_revision} · `}
              <time dateTime={answer.at} title={answer.at}>{utcClock(answer.at)}</time>
            </p>
          </div>
        // A refusal, an unknown outcome or an unconfigured call answers nothing:
        // this turn says so rather than showing an older answer as its own.
        : <p className="muted">这一轮没有被观测到的模型回答；下面历史里的内容属于更早的调用，不是对它的回答。</p>)}
      {latest?.message && <p className="notice">{latest.message}</p>}
      {latest && <CallNextStep call={latest} />}
      {/* Usage, cost and the route are details of this call; an unavailable cost
          still says so in the summary, so it is never silently dropped. */}
      {!live && root && <details className="provenance turn-details">
        <summary>{root.cost && !root.cost.available ? '用量、成本与来源 · 成本不可用' : '用量、成本与来源'}</summary>
        <p className="muted">{usageLine(root)}</p>
        {root.cost
          ? <p className="muted">{root.cost.available
              ? `成本：${root.cost.amount ?? '金额未记录'}`
              : `成本不可用：${root.cost.reason}。`}</p>
          : <p className="muted">这次调用没有成本记录，因此这里不写任何金额。</p>}
        <dl className="summary-list">
          <div><dt>提供方</dt><dd>{root.route.provider ?? '未记录'}</dd></div>
          <div><dt>模型</dt><dd>{root.model ?? root.route.model ?? '未记录'}</dd></div>
          <div><dt>端点</dt><dd>{root.route.endpoint ?? '未记录'}</dd></div>
          <div><dt>本次批准</dt><dd>{`${root.approved_budget.max_model_calls} 次模型调用 · ${root.approved_budget.max_backtests} 次回测`}</dd></div>
        </dl>
      </details>}
      {!live && turnCalls.filter(call => call.evidence?.length).map(call => <CallEvidence key={call.call_id} call={call} />)}
    </section>}
    {/* The current change sits between the answer it follows and the earlier
        conversation: an open one is right there to decide on, a closed one is a
        single collapsed line. */}
    {proposalSlot}
    {conversation?.pending?.phase === 'awaiting_proposal' && recorded?.status === 'open' &&
      <p className="notice" role="status">
        这次对话正在等待你对上面这条建议做出决定：应用或拒绝之后，它才会继续。
      </p>}
    {confirmation &&
      <ConversationConfirmation
        pending={confirmation.pending}
        preview={confirmation.preview}
        identity={confirmation.identity}
        busy={busy}
        error={note}
        // The exact intent, revision and preview the host froze, carried back
        // unchanged: this click is the only thing that starts a run.
        confirm={() => decide({
          turn_id: confirmation.pending.turn_id,
          action: 'confirm',
          launch_intent_id: confirmation.pending.launch_intent_id,
          base_revision: confirmation.pending.base_revision,
          preview_identity: confirmation.identity,
        })}
        decline={() => decide({ turn_id: confirmation.pending.turn_id, action: 'declined' })}
      />}
    {conversation?.pending?.phase === 'awaiting_observation' &&
      <ConversationObservation
        pending={conversation.pending}
        runStatus={runStatus}
        busy={busy}
        error={note}
        refresh={() => decide({ turn_id: conversation.pending!.turn_id, action: 'observe' })}
      />}
    {conversationNote && <p role="alert" className="notice error">{conversationNote}</p>}
    {earlier.length > 0 &&
      // Earlier messages stay inspectable inside the shared conversation scroller.
      <details className="provenance turn-history">
        <summary>{`更早的对话（${earlier.length}）`}</summary>
        <div className="message-scroll" role="group" aria-label="对话记录" tabIndex={0}>
          <ol className="message-list">
            {earlier.map(item => <li key={item.message_id} className={`message message-${item.role}`}>
              <p className="message-role">
                {item.role === 'model' ? 'QUINE' : '你'}{' '}
                <time className="muted" dateTime={item.at} title={item.at}>{utcClock(item.at)}</time>
              </p>
              <p>{item.text}</p>
              {item.role === 'model' && <p className="muted">模型推断 — 不是正式证据。</p>}
            </li>)}
          </ol>
        </div>
      </details>}
    </div>
    <div className={`composer${awaitingRunDecision ? ' composer-paused' : ''}`}>
      <ConversationInput id="strategy-message" label="消息" value={message} onChange={setMessage}
        placeholder="描述你想验证的问题…" />
      <label className="check" htmlFor="strategy-approve">
        <input id="strategy-approve" type="checkbox" checked={approved}
          onChange={event => { setApproved(event.target.checked); setNote('') }} />
        {streaming ? `批准本轮最多 ${turnBudget.max_model_calls} 次模型调用` : '批准一次模型调用'}
      </label>
      <div className="composer-actions">
        <p className="muted">{streaming ? '仅讨论与提案；运行需单独确认。' : isIntentDraft(strategy.draft)
          ? `${streaming ? `本轮最多 ${turnBudget.max_model_calls} 次模型调用` : '一次模型调用'} · 澄清当前想法。`
          : `${streaming ? `本轮最多 ${turnBudget.max_model_calls} 次模型调用` : '一次模型调用'} · 不应用修改，不启动实验。`}</p>
        {streaming && conversation && <p className="muted" title="包含结果未知的预留；这是累计记录，不是模型服务余额，也不是运行许可。">
          {`工作区累计预留 ${conversation.budget.reserved_model_calls} 次模型调用`}
        </p>}
        <button type="button" className="primary" onClick={ask} disabled={busy || unresolved || !approved || !message.trim() || modelSendBlock !== null || !evidenceReady || !canSend}>询问模型</button>
      </div>
      {streaming && turnLimit === null && <p className="notice" role="status">正在读取本轮调用权限…</p>}
      {awaitingRunDecision && <p className="notice" role="status">先确认或暂不运行当前实验；运行完成后可在这里继续追问。</p>}
      {modelSendBlock !== null && <p className="notice" role="status">
        {modelSendBlock} <Link to="/settings">检查模型设置</Link>，完成后在<a href="#research-preparation">研究准备</a>中重新检查。
      </p>}
      {conversation?.pending?.phase !== 'awaiting_confirmation' && note && <p role="alert" className="notice error">{note}</p>}
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
    <header className="chat-head"><h2><span aria-hidden="true">✦ </span>AI 研究助手</h2></header>
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
 * selection, a stale approval, a stale refusal or a stale undo target across.
 */
function ProposalPanel({ api, strategy, proposal, changed }: {
  api: ApiAdapter
  strategy: UserStrategy
  proposal: UserStrategyProposal
  changed: (strategy: UserStrategy) => void
}) {
  const [checked, setChecked] = useState<string[]>(proposal.items.map(item => item.path))
  const [reviseMessage, setReviseMessage] = useState('')
  const [reviseApproved, setReviseApproved] = useState(false)
  const attempts = useRef(0)
  const [busy, setBusy] = useState(false)
  // The refusal of the action just taken, held here rather than in the page's own
  // notice area: these controls live in the AI column, which on a narrow screen is
  // far below whatever the page says at its top, so the answer to a click has to be
  // where the click was made. It belongs to this panel instance — a later attempt
  // replaces it, a decision or a remount forgets it — so it never leaks across a
  // strategy or a proposal.
  const [actionError, setActionError] = useState('')

  const open = proposal.status === 'open'
  const selected = proposal.items.map(item => item.path).filter(path => checked.includes(path))

  const toggle = (path: string, on: boolean) =>
    setChecked(current => on ? [...current, path] : current.filter(item => item !== path))

  const apply = async () => {
    if (!selected.length) {
      setActionError('至少选择一个字段再应用，或者改为拒绝这条建议。')
      return
    }
    const applyChange = api.applyUserStrategyProposal
    if (!applyChange) {
      setActionError('这个工作区不提供应用修改。')
      return
    }
    setBusy(true)
    setActionError('')
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
      // A refusal answers the click that is still on screen; a refusal is not a
      // reason to ask the host again on its own.
      setActionError(applyFailure(error))
    } finally {
      setBusy(false)
    }
  }

  const reject = async () => {
    const rejectChange = api.rejectUserStrategyProposal
    if (!rejectChange) return
    setBusy(true)
    setActionError('')
    try {
      changed(await rejectChange(strategy.user_strategy_id, proposal.proposal_id))
    } catch (error) {
      setActionError(explain(error))
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
      setActionError('当前修订上没有可撤销的已应用修改。')
      return
    }
    setBusy(true)
    setActionError('')
    try {
      changed(await undoChange(strategy.user_strategy_id, {
        operation_id: `undo-${target.operation_id}-${strategy.draft.revision}`.slice(0, 64),
        head_revision: strategy.draft.revision,
        target_operation_id: target.operation_id,
      }))
    } catch (error) {
      setActionError(explain(error))
    } finally {
      setBusy(false)
    }
  }

  const revise = async () => {
    if (!reviseApproved) {
      setActionError('在修改这条建议之前，需要先批准一次模型调用。')
      return
    }
    const reviseChange = api.reviseUserStrategyProposal
    if (!reviseChange) {
      setActionError('这个工作区不提供修改建议。')
      return
    }
    setBusy(true)
    setActionError('')
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
      setActionError(explain(error))
    } finally {
      setBusy(false)
    }
  }

  // The application this proposal really wrote, and whether an undo names it. The
  // proposal record itself is never rewritten — what the ledger says happened is
  // shown beside it, so a change that was applied and then reversed is not read as
  // the result the current draft stands on.
  const application = [...strategy.changes].reverse()
    .find(item => item.kind === 'apply' && item.proposal_id === proposal.proposal_id)
  const reversal = application
    ? strategy.changes.find(item =>
        item.kind === 'undo' && item.target_operation_id === application.operation_id)
    : undefined
  const undone = strategy.changes.filter(item => item.kind === 'apply').at(-1)
  const canUndo = Boolean(undone && undone.to_revision === strategy.draft.revision)
  const stale = proposal.stale_paths.length > 0

  /** The change itself: every field with both values, its reason and its sources. */
  const changeDetail = <>
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
        {/* Both values are labelled, so the two sides of the change are told apart
            without relying on position or colour. */}
        <p className="change"><span className="change-before">{`之前 ${item.before ?? '无'}`}</span><span aria-hidden="true"> → </span><span className="change-after">{`之后 ${item.after}`}</span></p>
        <p className="reason">{item.reason}</p>
        <details className="provenance">
          <summary>来源与标识</summary>
          <p className="muted">{`提议者：${item.proposer} · 提案 ${proposal.proposal_id} · 调用 ${proposal.call_id}`}</p>
          {item.evidence.length > 0 && <p className="muted">
            {`模型引用：${item.evidence.join(', ')} — 这是它自己的声明，不是已验证的事实。`}
          </p>}
        </details>
      </li>)}
    </ul>
    <p className="muted">
      保存仅生成新修订；仍需单独检查实验预览并确认运行。
    </p>
    {open && actionError && <p role="alert" className="notice error">{actionError}</p>}
    {open && <div className="actions">
      <button type="button" className="primary" onClick={apply} disabled={busy}>应用所选</button>
      <button type="button" onClick={reject} disabled={busy}>拒绝建议</button>
      {canUndo && <button type="button" onClick={undo} disabled={busy}>撤销最近一次修改</button>}
    </div>}
    {open && <details className="provenance revise-disclosure">
      <summary>要求一个不同的修改（需要一次新的批准）</summary>
      <div className="revise">
        <div className="field">
          <label htmlFor={`${proposal.proposal_id}-revise`}>要求一个不同的修改</label>
          <textarea id={`${proposal.proposal_id}-revise`} rows={2} maxLength={4000}
            value={reviseMessage} onChange={event => setReviseMessage(event.target.value)} />
        </div>
        <label className="check" htmlFor={`${proposal.proposal_id}-revise-approve`}>
          <input id={`${proposal.proposal_id}-revise-approve`} type="checkbox" checked={reviseApproved}
            onChange={event => { setReviseApproved(event.target.checked); setActionError('') }} />
          为这个修订批准一次模型调用
        </label>
        <button type="button" onClick={revise} disabled={busy}>修改这条建议</button>
        <p className="muted">
          修改会把当前建议留作历史，并在当前修订上用这一次已批准的调用重新询问模型。
        </p>
      </div>
    </details>}
  </>

  // The proposal's own origin line, its grounded evidence and the recorded status
  // are the same facts in both states; only where they live changes.
  const origin = <p className="muted proposal-origin">
    {`针对草稿修订 ${proposal.base_revision} · 提案 ${proposal.proposal_id} · ${PROPOSAL_ORIGIN[proposal.status]}`}
  </p>
  const callEvidence = <CallEvidence call={strategy.calls.find(item => item.call_id === proposal.call_id)} />
  const recordedStatus = <span className={`chip chip-${proposal.status}`}>
    {PROPOSAL_STATUS_LABELS[proposal.status]}
  </span>
  const undoButton = <button type="button" onClick={undo} disabled={busy}>撤销最近一次修改</button>
  // One alert at most, and it is the answer to the click the reader just made. It is
  // placed with the controls in whichever state is on screen, so it is read where it
  // happened rather than at the far end of a long panel or at the top of the page.
  const actionAlert = actionError
    ? <p role="alert" className="notice error">{actionError}</p>
    : null
  // A closed proposal's own line names the effective state and nothing else: what
  // this record now amounts to on the ledger — 已撤销 when it was reversed, the
  // proposal's own status otherwise. The status it was recorded with stays inside
  // the disclosure, with the rest of the provenance, so the collapsed line never
  // signals two different states at once.
  const closedSummary = `${reversal ? '已撤销' : PROPOSAL_STATUS_LABELS[proposal.status]}`
    + `的修改与来源（${proposal.items.length} 个字段）`

  return <section aria-label="建议修改" className="strategy-panel proposal-panel">
    <header className="panel-head">
      <h2>建议修改</h2>
      {open && recordedStatus}
    </header>
    {open
      // A proposal waiting for a decision is the reader's to act on now: origin,
      // evidence, the typed diff and the live controls are all exposed directly.
      ? <>
          {origin}
          {callEvidence}
          {changeDetail}

        </>
      // A closed one is history: one native disclosure, collapsed until the reader
      // opens it, holds the origin and its UUID, the status it was recorded with,
      // the ledger's reversal, the evidence it was grounded in, every field and the
      // controls that can no longer act. Its summary already says what it amounts to.
      : <>
          <details className="provenance proposal-closed">
            <summary>{closedSummary}</summary>
            {origin}
            <p className="proposal-record">{recordedStatus}</p>
            {reversal && <p className="muted proposal-undone">
              {`修改记录显示：这次应用已在修订 ${reversal.from_revision} → ${reversal.to_revision} 上被撤销，当前草稿不是它的结果。`}
            </p>}
            {callEvidence}
            {changeDetail}
            <div className="actions">
              <button type="button" className="primary" onClick={apply} disabled>应用所选</button>
              <button type="button" onClick={reject} disabled>拒绝建议</button>
              {!canUndo && <button type="button" onClick={undo} disabled>撤销最近一次修改</button>}
            </div>
          </details>
          {/* The one action that can still change the draft stays on the first
              screen, outside the history it belongs to; it is not repeated inside.
              Its own refusal is announced there with it, not at the top of the
              page. */}
          {canUndo && <>
            {actionAlert}
            <div className="actions">{undoButton}</div>
          </>}
        </>}
  </section>
}

/**
 * An experiment preview and the one explicit confirmation that launches it.
 *
 * The preview is a local read of what the host would run, and the confirmation is a
 * single local consent for exactly that read. Confirming consumes it — before the
 * request, and on success and failure alike — so one preview is never a licence for
 * a second run and a refused launch cannot leave stale consent behind. Giving a
 * preview up is local too: it abandons the read, it does not claim the host stopped
 * anything, and an answer that arrives afterwards is not read as consent. A launch
 * that is already in flight is not cancellable here.
 *
 * Every operation belongs to this panel instance and to the strategy and revision
 * it is showing. The panel is mounted per revision, so an answer that arrives after
 * the page has moved on — a revision applied in the sibling panel, another tab,
 * another strategy, or this page being left — writes nothing into what is shown
 * now: no detail, no refusal, no phase. The request it answered was really sent and
 * is not recalled; a reload finds the work the host committed.
 */
function ExperimentPanel({ api, strategy, changed, failed }: {
  api: ExperimentOwnerApi
  strategy: UserStrategy
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  const [preview, setPreview] = useState<UserStrategyPreview | null>(null)
  const [phase, setPhase] = useState<'idle' | 'reading' | 'launching'>('idle')
  // Every local operation is numbered against this panel instance: the preview read
  // that is in flight and the launch it confirms. Leaving the screen — another tab,
  // another strategy, a revision this panel no longer shows — ends that belonging,
  // so a late answer writes nothing at all: not a preview, not a refusal, not a
  // phase, not a detail.
  const generation = useRef(0)
  // One confirmation is one dispatch: a second event in the same tick is refused
  // here, without waiting for a re-render to disable the button.
  const dispatching = useRef(false)
  // The stop this panel asked for, and the ticket that makes its answer its own.
  const [stopping, setStopping] = useState<string | null>(null)
  const stopTicket = useRef(0)
  // One progress read at a time, with the ticket of the read that started it.
  const pollTicket = useRef(0)
  useEffect(() => () => { generation.current += 1 }, [])

  // The preview a confirmation would really run: the one read against the revision
  // that is still the head. A preview whose revision has moved on is not offered as
  // confirmable; the host's own full-preview gate is unchanged.
  const live = preview && preview.draft_revision === strategy.draft.revision ? preview : null
  const cancellable = phase === 'reading' || preview !== null
  // Every run an owner holds, and whether one of them can still move on its own.
  const owned = strategy.experiments.filter(item => item.owner === 'quine')
  const activeRun = owned.find(item => LIVE_EXPERIMENT_STATUSES.includes(item.status)) ?? null
  // A repeat is only ever read from the admitted engine input: the preview's own
  // nonempty input digest against the digest a created experiment froze. Equal
  // digests mean this confirmation would repeat the same engine input; they are
  // not read as equivalence from the parameters, and nothing is disabled for it —
  // a deliberate reproduction is still the user's decision.
  const duplicate = live && live.input_sha256
    ? strategy.experiments.find(item =>
        item.status === 'created' && Boolean(item.payload.input_sha256)
        && item.payload.input_sha256 === live.input_sha256)
    : undefined

  // The owner reads its own run and this page follows it: while a run can still
  // move, the strategy is re-read on a timer until it reaches a state the owner
  // will not change on its own, or until this panel unmounts. One read is in
  // flight at a time, and an answer that belongs to a superseded read — or to a
  // panel that is gone — is dropped rather than written.
  useEffect(() => {
    if (!activeRun) return
    const read = api.getUserStrategy
    if (!read) return
    const strategyId = strategy.user_strategy_id
    let mounted = true
    let timer: number | undefined
    const poll = async () => {
      const at = ++pollTicket.current
      let stillActive: boolean
      try {
        const updated = await read(strategyId)
        if (!mounted || at !== pollTicket.current) return
        if (updated.user_strategy_id !== strategyId) return
        changed(updated)
        stillActive = updated.experiments.some(item =>
          item.owner === 'quine' && LIVE_EXPERIMENT_STATUSES.includes(item.status))
      } catch {
        // A read that failed is not an outcome: the last-read state stands and the
        // next tick asks the owner again.
        stillActive = true
      }
      if (mounted && at === pollTicket.current && stillActive) {
        timer = window.setTimeout(() => { void poll() }, 1200)
      }
    }
    timer = window.setTimeout(() => { void poll() }, 600)
    return () => {
      mounted = false
      pollTicket.current += 1
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [api, changed, activeRun, strategy.user_strategy_id])

  // The confirmation this click belongs to. While a previous confirmation of the
  // same previewed input is still unresolved, that confirmation is the one asked
  // about again: a new intent would be a second run while the first answer is
  // unknown. A different preview is a different confirmation and gets its own.
  const intentFor = (confirmed: UserStrategyPreview) => {
    const open = [...strategy.experiments].reverse().find(item =>
      item.owner === 'quine'
      && UNRESOLVED_EXPERIMENT_STATUSES.includes(item.status)
      && item.payload.preview_identity === confirmed.preview_identity)
    return open ? open.launch_intent_id : `li-${strategy.experiments.length + 1}`
  }

  const stopRun = async (item: UserStrategyExperiment) => {
    const cancel = api.cancelUserStrategyExperiment
      ?? ((userStrategyId: string, launchIntentId: string) =>
        userStrategiesApi.cancelExperiment(userStrategyId, launchIntentId))
    const ticket = ++stopTicket.current
    const mine = generation.current
    setStopping(item.launch_intent_id)
    failed('')
    try {
      const updated = await cancel(strategy.user_strategy_id, item.launch_intent_id)
      if (stopTicket.current !== ticket || generation.current !== mine) return
      if (updated.user_strategy_id !== strategy.user_strategy_id) return
      changed(updated)
    } catch (error) {
      if (stopTicket.current !== ticket || generation.current !== mine) return
      failed(explain(error))
    } finally {
      if (stopTicket.current === ticket) setStopping(null)
    }
  }

  const readPreview = async () => {
    const read = api.previewUserStrategyExperiment
    if (!read) return
    const mine = generation.current + 1
    generation.current = mine
    setPhase('reading')
    failed('')
    try {
      const value = await read(strategy.user_strategy_id)
      if (generation.current !== mine) return
      setPreview(value)
    } catch (error) {
      if (generation.current !== mine) return
      setPreview(null)
      failed(explain(error))
    } finally {
      if (generation.current === mine) setPhase('idle')
    }
  }

  const cancelPreview = () => {
    // Abandoning the preview is local: nothing is written, and the read it gives up
    // on cannot come back as consent.
    generation.current += 1
    setPreview(null)
    setPhase('idle')
    failed('')
  }

  const start = async () => {
    if (dispatching.current) return
    // The click confirms exactly the preview on screen. It is consumed here, before
    // anything is sent, and the values it was made of are held by this click.
    const confirmed = live
    if (!confirmed) {
      failed('请先预览实验：在你读过它将运行什么之前，不会启动任何东西。')
      return
    }
    const launch = api.launchUserStrategyExperiment
    if (!launch) {
      failed('这个工作区不提供启动实验。')
      return
    }
    // This dispatch belongs to the revision on screen now, and the answer is only
    // written back while that is still the panel showing it.
    const mine = generation.current
    dispatching.current = true
    setPreview(null)
    setPhase('launching')
    failed('')
    try {
      const updated = await launch(strategy.user_strategy_id, {
        // The intent is stable across an uncertain receipt: while a confirmation of
        // this same previewed input is unresolved, it is that confirmation which is
        // asked about again, and the owner answers from its own record.
        launch_intent_id: intentFor(confirmed),
        base_revision: confirmed.draft_revision,
        // The confirmation names the input it was made against, so a preview that
        // no longer describes what would run is refused instead of silently run.
        preview_identity: confirmed.preview_identity,
        approved_budget: RUN_APPROVAL,
      })
      if (generation.current !== mine) return
      changed(updated)
    } catch (error) {
      // A refused launch is not kept as consent: the next attempt is a new preview.
      if (generation.current !== mine) return
      failed(explain(error))
    } finally {
      dispatching.current = false
      if (generation.current === mine) setPhase('idle')
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
      <button type="button" onClick={readPreview} disabled={phase !== 'idle'}>预览实验</button>
      <button type="button" onClick={cancelPreview} disabled={!cancellable}>取消预览</button>
      <button type="button" className="primary" onClick={start}
        disabled={phase !== 'idle' || !live}>确认并运行一次</button>
    </div>
    {phase === 'reading' && <p className="muted" role="status" aria-live="polite">
      正在读取这次实验将执行什么……这次读取不会启动任何运行，取消只是放弃这次读取。
    </p>}
    {phase === 'launching' && <p className="muted" role="status" aria-live="polite">
      这次确认已经送出，正在等待宿主回答；这个页面不会撤回它，运行的状态在它自己的页面上。
    </p>}
    {/* The owner's own progress: this page follows the record the owner writes and
        claims nothing of its own about a run that is still moving. */}
    {activeRun && <p className="notice" role="status" aria-live="polite">
      {`实验 ${activeRun.launch_intent_id} 的所有者状态：${EXPERIMENT_STATUS_LABELS[activeRun.status]}`
        + (activeRun.phase ? ` · 阶段 ${activeRun.phase}` : '')}
      {'。这个页面会继续读取所有者自己的记录，直到它进入不会再自行改变的状态；'
        + '一次失败的读取不构成结果，也不会被当作停止或成功。'}
    </p>}
    {phase === 'idle' && !live && (hasRun
      ? <p className="muted">要再运行一次，先重新预览：一次确认只对应它自己读到的那个预览。</p>
      : <p className="muted">先预览这次实验将执行什么，再确认。预览只读；一次确认只允许一次回测，且不调用模型。</p>)}
    {live && <dl className="preview-facts">
      <div><dt>草稿</dt><dd>{revisionLabel(live.draft_revision)}</dd></div>
      <div><dt>策略</dt><dd>{`${live.strategy_id} · ${live.venue}:${live.symbol}`}</dd></div>
      <div><dt>场景</dt><dd>{live.scenario_id}</dd></div>
      <div><dt>资金</dt><dd>{`初始资金 ${live.initial_cash} USD`}</dd></div>
      {/* Every admitted parameter, as admitted: an unknown name is not dropped and a
          zero is not read as absent. */}
      <div><dt>注册参数</dt><dd>{parameterLine(live)}</dd></div>
      <div><dt>数据</dt><dd>{dataLine(live)}</dd></div>
      <div><dt>数据窗口</dt><dd>{windowLine(live)}</dd></div>
      <div><dt>成本假设</dt><dd>{costLine(live)}</dd></div>
      <div><dt>输入身份</dt><dd>{live.preview_identity.slice(0, 19)}</dd></div>
      <div><dt>准则</dt><dd>{criterionLine(live.criterion)}</dd></div>
      <div><dt>本工作区授予的预算</dt><dd>
        {`${live.budget.max_model_calls} 次模型调用 · ${live.budget.max_backtests} 次回测`}
      </dd></div>
      {/* What this one confirmation may spend, told apart from the ceiling above. */}
      <div><dt>本次确认</dt><dd>
        {`${RUN_APPROVAL.max_model_calls} 次模型调用 · ${RUN_APPROVAL.max_backtests} 次回测`}
      </dd></div>
    </dl>}
    {duplicate && <p className="notice" role="status">
      {`这次预览的引擎输入与已确认的冻结运行 ${duplicate.launch_intent_id}（修订 ${duplicate.payload.draft_revision}）完全相同：`
        + '确认它会重复同一个引擎输入，不构成新的证据。是否重跑仍由你决定——这里不会自动取消或禁用它。'}
    </p>}
    {strategy.experiments.length > 0 && <ul className="experiment-list">
      {strategy.experiments.map(item => <li key={item.launch_intent_id}>
        {item.status === 'created'
          ? <p>{`已从修订 ${item.payload.draft_revision} 启动实验 ${item.launch_intent_id}`}</p>
          : <p>{`实验 ${item.launch_intent_id} 来自修订 ${item.payload.draft_revision} — ${EXPERIMENT_STATUS_LABELS[item.status]}`}</p>}
        {/* What the owner itself is doing, read back from its record: the phase it
            recorded, the attempt it named and the frozen input it retained. */}
        {item.owner === 'quine' && <details className="provenance"><summary>运行诊断</summary><p className="muted">
          {`所有者 quine 核心`
            + (item.phase ? ` · 阶段 ${item.phase}` : '')
            + ` · 输入 ${item.payload.input_sha256?.slice(0, 16) ?? '未记录'}`
            + ` · 冻结 ${item.frozen_sha256?.slice(0, 16) ?? '未记录'}`
            + ` · 运行 ${item.attempt_id?.slice(0, 8) ?? '未记录'}`}
        </p></details>}
        {/* The run's own page: the owner's registered result is read through the
            same run, report, path and analytics views every other run uses, so a
            core-owned success keeps the mature report and comparison, not a
            second, thinner reading of it. */}
        {item.status === 'created' && item.job_id &&
          <Link to={`/strategies/${encodeURIComponent(strategy.user_strategy_id)}?tab=experiments&evidence_a=${encodeURIComponent(item.job_id)}`}>
            查看这次实验报告
          </Link>}
        {/* One explicit stop request. It asks the owner; the owner's own record is
            what says whether the run stopped. */}
        {item.owner === 'quine' && LIVE_EXPERIMENT_STATUSES.includes(item.status) && <p className="actions">
          <button type="button" onClick={() => { void stopRun(item) }}
            disabled={stopping !== null || item.status === 'stopping'}>
            {stopping === item.launch_intent_id ? '正在请求停止……' : '停止这次运行'}
          </button>
          <span className="muted">
            停止请求只是请求：在所有者自己记录之前，停止没有被证实，这里也不会替它宣布停止。
          </span>
        </p>}
        {item.status !== 'created' && <p className="notice">
          {EXPERIMENT_NOTES[item.status] ?? '这次实验没有已确认的运行。'}
          {item.message ? ` ${item.message}` : ''}
        </p>}
      </li>)}
    </ul>}
    <p className="muted">
      正式报告在实验完成后显示。一次运行只是一个冻结的结果，不是已经完成的研究。
      已确认的实验会保留它冻结的那个修订，即使草稿继续前进。
    </p>

  </section>
}

/**
 * One row's run cell: the real state of the job the latest created experiment
 * dispatched, read from the job itself on mount and whenever the job id changes.
 * The read is guarded, so an answer that arrives after the row moved on writes
 * nothing; there is no polling, and a job id that does not match the one asked
 * for is refused rather than shown as this row's run. A job this page cannot read
 * says so and still links to the run's own page.
 */
function JobStatusCell({ api, jobId, strategyId }: { api: ApiAdapter; jobId: string; strategyId?: string }) {
  type JobRead =
    | { kind: 'reading' }
    | { kind: 'ready'; job: BacktestJob }
    | { kind: 'unavailable'; note: string }
  // Kept with the job it was read for: while another job is being read the cell
  // shows that job's own load, never the previous job's status.
  const [loaded, setLoaded] = useState<{ key: string; read: JobRead } | null>(null)
  const state: JobRead = loaded && loaded.key === jobId ? loaded.read : { kind: 'reading' }

  useEffect(() => {
    let active = true
    api.getBacktest(jobId).then(job => {
      if (!active) return
      setLoaded({ key: jobId, read: job.job_id === jobId
        ? { kind: 'ready', job }
        : { kind: 'unavailable', note: '读取到的任务与这条运行不一致' } })
    }).catch(() => {
      if (active) setLoaded({ key: jobId, read: { kind: 'unavailable', note: '运行状态不可读' } })
    })
    return () => { active = false }
  }, [api, jobId])

  if (state.kind === 'reading') return <span className="table-muted">正在读取运行状态…</span>
  const link = <Link to={strategyId ? `/strategies/${encodeURIComponent(strategyId)}?tab=experiments&evidence_a=${encodeURIComponent(jobId)}` : `/backtests/${encodeURIComponent(jobId)}`}>
    {state.kind === 'ready'
      ? `${JOB_STATUS_LABELS[state.job.status]}${state.job.status === 'succeeded'
          ? state.job.report_ready ? ' · 报告可读' : ' · 报告不可读'
          : ''}`
      : '打开运行'}
  </Link>
  return state.kind === 'ready'
    ? link
    : <span className="table-muted">{`${state.note} · `}{link}</span>
}

/**
 * The workspace's strategy table: one compact row per strategy, in the columns the
 * decision is actually made in. The frozen evidence of a row is the revision its
 * latest confirmed experiment ran; the run column is that launch's own job, read
 * from the job itself — never a claim that a created dispatch succeeded.
 */
function StrategyTable({ api, strategies, selectedId }: {
  api: ApiAdapter
  strategies: UserStrategy[]
  selectedId?: string
}) {
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
                  // A created run has a real run page, whatever owner registered it:
                  // its state is read from that run itself, never from the record.
                  : latest.status === 'created' && latest.job_id
                    ? <JobStatusCell key={latest.job_id} api={api} jobId={latest.job_id} strategyId={item.user_strategy_id} />
                    : <span className="table-muted">{EXPERIMENT_STATUS_LABELS[latest.status]}</span>}</td>
              </tr>
            })}
          </tbody>
        </table>}
  </nav>
}

/**
 * The way back into the work that moved most recently, on the collection route
 * only. It names the strategy, its working revision and the revision of its latest
 * frozen attempt. A created dispatch is not a run outcome, so no dispatch-only
 * state is claimed here: the run's own state is read on the strategy's page. It is
 * a link — nothing redirects on its own and no approval of one strategy is carried
 * to another.
 */
function Continuation({ strategies }: { strategies: UserStrategy[] }) {
  const latest = strategies.reduce((best, item) => item.updated_at > best.updated_at ? item : best)
  const experiment = latest.experiments.at(-1)
  const frozen = experiment
    ? `最近冻结 v${experiment.payload.draft_revision}`
    : isIntentDraft(latest.draft) ? '尚未绑定场景' : '还没有冻结运行'
  return <section aria-label="继续最近的策略" className="continuation">
    <Link className="continuation-link"
      to={`/strategies/${encodeURIComponent(latest.user_strategy_id)}`}>
      {`继续最近更新的策略：${latest.name}`}
    </Link>
    <span className="muted">{`${revisionChip(latest.draft)} · ${frozen}`}</span>
    <p className="muted">点开它继续这条策略的下一步。</p>
  </section>
}

/** The workspace itself: the table, the selected strategy and its AI column. */
export function StrategiesWorkspace({ api }: { api: ApiAdapter }) {
  const { userStrategyId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const [strategies, setStrategies] = useState<UserStrategy[]>([])
  const [check, setCheck] = useState(0)
  const [catalogRead, setCatalogRead] = useState<{
    api: ApiAdapter; check: number; items: ScenarioSummary[]; error: string | null
  } | null>(null)
  const [modelRead, setModelRead] = useState<{
    api: ApiAdapter; check: number; value: ModelConfigurationStatus | null; error: string | null
  } | null>(null)
  // Tag each independent read with its adapter and request. While a new read is in
  // flight, neither old readiness nor old bind choices are offered as current.
  const catalog = catalogRead?.api === api && catalogRead.check === check ? catalogRead : null
  const model = modelRead?.api === api && modelRead.check === check ? modelRead : null
  const modelSendBlock = !model ? '正在读取模型配置，暂不能发送。'
    : model.error ? '模型配置状态未知，暂不能发送。'
      : model.value?.configured !== true ? '模型尚未配置就绪，暂不能发送。' : null
  const scenarios = catalog?.error === null ? catalog.items : []
  const usableCount = scenarios.filter(item => item.valid && item.summary && item.scenario_id).length
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
    const readCatalog = async () => {
      try {
        if (!api.listScenarios) throw new Error('本客户端无法读取场景目录。')
        const items = await api.listScenarios(true)
        if (active) setCatalogRead({ api, check, items, error: null })
      } catch (error) {
        if (active) setCatalogRead({ api, check, items: [], error: explain(error) })
      }
    }
    const readModel = async () => {
      try {
        if (!api.settingsStatus) throw new Error('本客户端无法读取模型配置状态。')
        const value = await api.settingsStatus()
        if (active) setModelRead({ api, check, value, error: null })
      } catch (error) {
        if (active) setModelRead({ api, check, value: null, error: explain(error) })
      }
    }
    void readCatalog()
    void readModel()
    return () => { active = false }
  }, [api, check])

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
      {!userStrategyId && <header className="strategy-heading">
        <h1>研究</h1>
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
      </header>}
      {!userStrategyId && <p className="strategy-subtitle">保存想法，验证实验，与 AI 一起读懂证据。</p>}

      {!userStrategyId && (listState === 'reading'
        ? <p className="muted" role="status" aria-live="polite">正在读取策略…</p>
        : listState === 'ready' && <StrategyTable api={api} strategies={strategies} selectedId={userStrategyId} />)}
      {listNote && <p role="alert" className="notice error">{listNote}</p>}
      {/* The collection route offers a way back into the work that moved last; it
          is a link the reader chooses, not a redirect. */}
      {!userStrategyId && listState === 'ready' && strategies.length > 0 &&
        <Continuation strategies={strategies} />}
      {!userStrategyId && <details className="provenance"><summary>历史记录</summary><div className="actions">
        <Link to="/research">旧研究任务</Link><Link to="/backtests">独立回测</Link><Link to="/holdouts">留出记录</Link><Link to="/candidates">候选记录</Link>
      </div></details>}
      {userStrategyId && <div className="workspace-breadcrumb"><Link to="/strategies">所有研究</Link><Link to="/strategies" state={{ createStrategy: true }}>新建策略</Link></div>}
      {showCreate && <CreateStrategy api={api} created={created} value={createDraft} update={setCreateDraft} />}
      {detailNote && detailNote.id === userStrategyId &&
        <p role="alert" className="notice error">{detailNote.text}</p>}
      {detailReading && <p className="muted" role="status" aria-live="polite">正在读取策略…</p>}
      {current && <>
        {actionNote && <p role="alert" className="notice error">{actionNote}</p>}
        <StrategyDetail
          key={current.user_strategy_id}
          api={api} strategy={current} scenarios={scenarios} catalogReady={catalog !== null && catalog.error === null} changed={changed}
          failed={failedFor(current.user_strategy_id)} />
      </>}
      <details id="research-preparation" className="strategy-panel first-use-preparation" open={(!current || modelSendBlock !== null || usableCount === 0) || undefined}>
        <summary>研究准备{modelSendBlock === null && usableCount > 0 ? " · 已就绪" : " · 有待完成"}</summary>
        <section aria-label="开始研究">
        <header className="panel-head"><h2>开始研究</h2>
          <button type="button" disabled={!catalog || !model} onClick={() => setCheck(value => value + 1)}>重新检查</button>
        </header>
        <p className="muted">先保存想法，再绑定场景与数据。AI 提出修改，由你应用；实验须预览后确认。</p>
        <div className="preparation-row"><strong>{!model ? '正在读取模型配置…'
          : model.error ? '模型状态未知' : model.value?.configured ? '模型已配置 · 连接未验证' : '模型未配置'}</strong>
          <Link to="/settings">配置模型</Link></div>
        {model?.error && <p className={api.settingsStatus ? "notice error" : "muted"} role={api.settingsStatus ? "alert" : undefined}>{model.error}</p>}
        <div className="preparation-row"><strong>{!catalog ? '正在读取场景目录…'
          : catalog.error ? '场景目录读取失败' : usableCount ? `${usableCount} 个可用场景` : '尚无可用场景'}</strong>
          <Link to={current ? `/data?${new URLSearchParams({ research: current.user_strategy_id, revision: String(current.draft.revision), return: location.pathname + location.search })}#data-upload` : "/data"}>导入 CSV 数据</Link></div>
        {catalog?.error && <p className="notice error" role="alert">{catalog.error}</p>}
        {catalog && !catalog.error && usableCount === 0 && <details className="disclosure">
          <summary>准备场景与数据</summary>
          <p>场景定义策略、标的、资金及数据。请把有效场景 YAML 和它引用的数据放入本地服务配置的 <code>--scenario-root</code> 目录，保持相对数据路径正确；需要改变目录时，以同一工作区重启服务，再重新检查。已保存的想法会保留。</p>
          <p>CSV 导入只发布数据版本，不创建场景。当前版本仍需通过本地服务准备场景；安装应用内的目录配置尚未提供。</p>
        </details>}
        <p className="muted preparation-note">缺少配置也可保存想法。配置可用不代表模型连通；场景可用不代表草稿已可运行，以实验预览为准。</p>
      </section></details>
    </div>
    <aside className="strategy-ai" id="strategy-ai-column" tabIndex={-1}>
      {current && <a className="jump-link jump-to-details" href="#strategy-detail">返回策略详情</a>}
      {current
        ? <ChatPanel
            key={`${current.user_strategy_id}-${current.draft.revision}`}
            api={api} strategy={current} strategies={strategies} changed={changed} modelSendBlock={modelSendBlock}
            // The proposal sits in the AI column between the conversation and the
            // composer: explanation, then the current change, then the way to ask
            // again. It is keyed per proposal and revision, so no stale selection,
            // approval or undo target is carried across a change.
            proposalSlot={proposal
              ? <ProposalPanel
                  key={`${current.user_strategy_id}-${proposal.proposal_id}-${proposal.base_revision}-${proposal.status}`}
                  api={api} strategy={current} proposal={proposal}
                  changed={changed} />
              : null} />
        : detailReading
          ? <EmptyChat note="正在读取这条策略的对话…" live />
          : detailFailed
            ? <EmptyChat note="这条策略没有被读到，因此这里没有对话。" />
            : <EmptyChat />}
    </aside>
  </div>
}
