import type { ResearchSummary, ResearchReference } from './research-workbench-api'
import type { PlatformId } from './platform-context'
import { FrozenReportFacts, FrozenReportMetrics, FrozenReportDetails, ReportTrades } from './frozen-report'
import { WorkbenchIcon } from './workbench-icon'
import { platformNames, usePlatform } from './platform-context'
import { SourceLink as Link } from './workspace-navigation'
import { ResearchComposer, type ComposerCommand } from './research-composer'
import { DraftEditor } from './draft-editor'
import { AgentFeed, ResearchSplitLayout } from './agent-panel-layout'
import { useWorkspaceView } from './agent-panel-view'
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
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { CandidateCreate } from './candidates'
import { type TradeAnalytics } from './trade-analytics'
import type { ApiAdapter, BacktestJob, BacktestReport, ScenarioSummary } from './app'
import type { ModelConfigurationStatus, ReportCriterion } from './research-api'
import { EquityCurve, type EquityPath } from './equity-path'
import { evidenceAddress, readEvidenceSelection, selectableRuns, selectedRuns } from './evidence-selection'
import {
  UserStrategyRequestError,
  isIntentDraft,
  userStrategiesApi,
  type ApprovedBudget,
  type UserStrategy,
  type UserStrategyCall,
  type UserStrategyMessage,
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
  request_failed: '无法确认请求结果。请重新读取原记录，不要重复提交。',
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

/**
 * What to do next when a call did not produce an answer. Unconfigured and unknown
 * are different states: one has a provider to set up, the other may already have
 * reached one and is never re-sent silently.
 */
function CallNextStep({ call }: { call: UserStrategyCall }) {
  if (call.status === 'unconfigured') {
    return <p className="muted">
      这里没有配置模型提供方，因此这次调用只被记录、从未发送。请在
      <Link to="/resources/models"> 设置</Link> 中配置提供方后，发送一条新消息继续。
    </p>
  }
  if (call.status === 'unknown') {
    return <p className="muted">
      这次请求可能已经到达提供方，结果与用量尚未确认，不会自动重发。你可以发送一条新消息继续。
      如果提供方不是你预期的那个，请检查
      <Link to="/resources/models"> 设置</Link>。
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
function CreateStrategy({ api, created, value, update, close }: {
  api: ApiAdapter
  created: (strategy: UserStrategy) => void
  value: { name: string; intent: string }
  update: (value: { name: string; intent: string }) => void
  close: () => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const { platform } = usePlatform()
  const [exchange, setExchange] = useState<PlatformId | null>(platform)
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
        exchange_id: exchange,
      })
      update({ name: '', intent: '' })
      created(strategy)
    } catch (error) {
      setNote(explain(error))
    } finally {
      setBusy(false)
    }
  }

  return <section aria-label="新建研究" className="strategy-create">
    <form onSubmit={submit}>
      <div className="field">
        <label htmlFor="strategy-intent">意图</label>
        <textarea id="strategy-intent" value={intent} maxLength={2000} required rows={2} autoFocus
          onChange={event => update({ name, intent: event.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="strategy-name">策略名称</label>
        <input id="strategy-name" value={name} maxLength={120}
          onChange={event => update({ name: event.target.value, intent })} />
        <small>{`可以留空；留空时用你自己的话作为临时名称：${temporaryName(intent)}`}</small>
      </div>
      <label className="field">研究归属<select aria-label="研究归属" value={exchange ?? ''} onChange={event => setExchange((event.target.value || null) as PlatformId | null)}><option value="">未指定交易所</option><option value="binance">Binance</option><option value="okx">OKX</option></select><small>仅用于整理和筛选研究，不连接账户或改变执行参数。</small></label>
      <div className="create-actions"><button type="button" disabled={busy} onClick={close}>收起（保留输入）</button>
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
    <h2>研究想法</h2>
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
/**
 * The evidence of one frozen experiment: its real job, its formal report and its
 * equity path, read through the existing adapter. The report is only shown when its
 * run id is the run it was read for; a missing, refused or mismatched report is
 * said out loud rather than replaced by a figure this page would have to invent.
 */
export function EvidenceSection({ api, strategy }: { api: ApiAdapter; strategy: UserStrategy }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const visit = useLocation().key
  const reportView = searchParams.get('report') ?? 'overview'
  const reportTab = ['overview', 'complete', 'trades', 'inputs'].includes(reportView) ? reportView : 'overview'
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
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      const job = await api.getBacktest(jobId)
      if (!active) return
      if (job.job_id !== jobId) throw new Error('读取到的任务与所选实验不一致')
      if (job.status === 'accepted' || job.status === 'running' || job.status === 'stopping') timer = setTimeout(() => {void load().catch(fail)}, OBSERVATION_INTERVAL_MS)
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
    const fail = (error: unknown) => {
      if (active) setLoaded({ key: readKey, state: { kind: 'unavailable', note: explain(error) } })
    }
    void load().catch(fail)
    return () => { active = false; clearTimeout(timer) }
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
      {selection.kind === 'draft' ? '当前选择的是草稿，未选择实验报告。用 /context 选择已冻结实验，或在运行记录中打开报告。'
        : strategy.experiments.some(item => item.owner === 'quine' && item.frozen_sha256)
          ? '已有冻结的实验，但还没有可读取的正式报告。请查看下方运行记录；未确认的结果不会被当作成功，也不会自动重跑。'
          : '还没有冻结运行。在「实验」里确认一次实验之后，这里会显示那次运行自己的正式报告——只有运行成功，报告才存在。'}
    </p>}
    {!invalid && state.kind === 'loading' && <p className="muted">正在读取该运行的状态与正式报告……</p>}
    {!invalid && state.kind === 'unavailable' && <p className="notice error">{state.note}</p>}
    {!invalid && state.kind === 'ready' && <>
      <FrozenReportFacts job={state.job} report={state.report} revision={frozenRevision} />

      <div className="report-overview">
      <FrozenReportMetrics report={state.report} path={state.path} />
      {state.path
        ? <div className="evidence-curve"><EquityCurve analysis={state.path} /></div>
        : <p className="muted">{state.pathNote ?? '该运行没有可读的权益曲线。'}</p>}
      {/* The limits stand beside the figures they qualify: one window and one frozen
          revision, and only the sample size the run's own job recorded. What is
          claimed is what this run cannot do on its own, not an inventory of things
          this page did not read. */}
      </div>
      <FrozenReportDetails job={state.job} report={state.report} path={state.path} />
      <nav className="report-tabs" aria-label="报告视图">{[['overview', '结果总览'], ['complete', '完整交易'], ['trades', '订单与成交'], ['inputs', '输入与来源']].map(([key, name]) => <button type="button" key={key} aria-pressed={reportTab === key} onClick={() => { const next = new URLSearchParams(searchParams); next.set('report', key); setSearchParams(next) }}>{name}</button>)}</nav>
      <div hidden={reportTab !== 'overview'}>
      <p className="muted evidence-limits">
        {`${frozenCriterion} · ${sample}。这是一次单窗口的冻结运行：成功只表示执行完成，不能证明研究有效或稳健。同一数据上的重复运行和行情条数不等于独立验证样本。`}
      </p>
      </div>
      <div hidden={reportTab !== "inputs"}>
      <details className="provenance report-assumptions" open>
        <summary>数据与假设</summary>
        <dl className="summary-list">{Object.entries(experiment?.payload.normalized ?? {}).filter(([name]) => ['data', 'execution', 'risk'].includes(name)).map(([name, value]) =>
          <div key={name}><dt>{{ data: '数据', execution: '成本与执行', risk: '风险限制' }[name]}</dt><dd>{assumptionSummary(name, value)}</dd></div>)}</dl>
        <details><summary>完整来源与身份</summary><pre>{JSON.stringify(experiment?.payload.normalized ?? {}, null, 2)}</pre></details>
        <p className="muted">{sample} · 参数：{Object.entries(experiment?.payload.parameters ?? {}).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p>
      </details>
      </div>
      {reportTab === 'complete' && <ReportTrades report={state.report} analytics={state.analytics} view="complete" />}
      {reportTab === 'trades' && <ReportTrades report={state.report} analytics={state.analytics} view="orders" />}
      <div className="artifact-links"><a href={api.artifactUrl(state.job.job_id, 'report.json')}>下载正式报告</a><Link to={`/backtests/${encodeURIComponent(state.job.job_id)}`}>独立报告链接</Link></div>
      <details className="provenance"><summary>留出验证与候选</summary>
        <p className="muted">使用本实验的冻结输入开展独立验证，再记录候选决定。</p>
        <Link to={`/holdouts/new/${encodeURIComponent(state.job.job_id)}`}>创建本实验的留出验证</Link>
        <CandidateCreate key={state.job.job_id} api={api} jobId={state.job.job_id} />
      </details>
      <details className="provenance" hidden={reportTab !== "inputs"}>
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
const TEMPLATE_LABELS: Record<string, string> = {
  venue: '交易所', symbol: '标的', settlement_currency: '结算币种', price_quantum: '价格步长',
  quantity_quantum: '数量步长', currency_quantum: '金额步长', contract_multiplier: '合约乘数',
  specification_id: '标的规格版本', specification_set_id: '规格集合版本',
  max_order_quantity: '单笔数量上限', max_position_quantity: '持仓数量上限', max_notional: '名义金额上限',
  policy: '撮合方式', commission: '佣金',
}

function BindScenario({ api, strategy, scenarios, catalogReady, changed, failed }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  catalogReady: boolean
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
}) {
  const location = useLocation()
  const [selected, setSelected] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [datasets, setDatasets] = useState<SelectableDataset[]>([])
  const [datasetError, setDatasetError] = useState('')
  useEffect(() => {
    let active = true
    if (api.listDatasets) void api.listDatasets().then(items => {
      if (active) setDatasets(items)
    }).catch(() => { if (active) setDatasetError('数据目录读取失败，请返回研究重新打开。') })
    return () => { active = false }
  }, [api])
  const [field, setField] = useState('')
  const [expectation, setExpectation] = useState<string>('positive')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const attempts = useRef(0)

  const usable = scenarios.filter(item => item.valid && item.summary && item.scenario_id)
  const candidate = usable.find(item => item.scenario_id === selected) ?? null
  const isTemplate = selected.startsWith('template:')
  const compatibleData = datasets.filter(item => item.valid && item.venue === candidate?.summary?.venue && item.symbol === candidate?.summary?.symbol)
  const importQuery = new URLSearchParams({ research: strategy.user_strategy_id, revision: String(strategy.draft.revision), return: location.pathname + location.search })

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
        ...(datasetId ? { dataset_id: datasetId } : {}),
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
    <p>无需编写 YAML 或插件：选择一个内置模板，导入并绑定兼容 CSV，再编辑资金与参数、预览并单独确认回测。AI 当前可检查输入、提出受支持参数的修改和解释报告；不能凭任意自然语言生成并执行新策略代码。</p>
    <div className="field">
      <label htmlFor="bind-scenario">场景</label>
      <select id="bind-scenario" value={selected} onChange={event => { setSelected(event.target.value); setDatasetId(''); setConfirmed(false) }}>
        <option value="">选择支持模板或已注册场景</option>
        {usable.map(item => <option key={item.scenario_id} value={item.scenario_id}>
          {`${item.scenario_id} · ${item.summary?.strategy_id} · ${item.summary?.venue}:${item.summary?.symbol}`}
        </option>)}
      </select>
      {!catalogReady && <small>场景目录尚未读到，请在「研究准备」重试检查。</small>}
      {catalogReady && !usable.length && <small>当前没有可用的有效注册场景。请查看「研究准备」中的准备说明。</small>}
    </div>
    {candidate && <div className="field">
      <label htmlFor="bind-dataset">研究数据版本</label>
      <select id="bind-dataset" value={datasetId} onChange={event => { setDatasetId(event.target.value); setConfirmed(false) }}>
        <option value="">{isTemplate ? '选择已发布的兼容 CSV 数据' : '使用场景原有数据'}</option>
        {compatibleData.map(item => <option value={item.dataset_id} key={item.dataset_id}>{item.dataset_id} · {item.record_count} 条</option>)}
      </select>
      <p className="muted">{isTemplate ? '模板只提供已支持的策略与固定标的规格，必须选择自己的数据；不使用随包样本运行本研究。' : '可保留原场景数据，或明确选择兼容的已发布版本。'}只列出标的一致的数据，后端还会完整校验。</p>
      <Link to={`/resources/data?${importQuery}#data-upload`}>导入 CSV，完成后返回此研究</Link>
      {datasetError && <p role="alert">{datasetError}</p>}
    </div>}
    {candidate?.summary && ['bounded-long-v1', 'moving-average-entry-v1'].includes(candidate.summary.strategy_id) && <section aria-label="策略行为说明">
      <h3>{candidate.summary.strategy_id === 'bounded-long-v1' ? '延迟后做多一次' : '快均线高于慢均线时做多一次'}</h3>
      <p>{candidate.summary.strategy_id === 'bounded-long-v1'
        ? '等待 entry_delay_bars 根可用行情后发出一次做多信号；0 表示首根即触发。参数单位是根数，不是天数。'
        : '收集 slow_window 根原始、无修订 K 线后，在快均线严格高于慢均线时发出一次做多信号；不是等待一次上穿。fast_window 和 slow_window 的单位是 K 线根数，快窗口必须小于慢窗口。'}</p>
      <p>target_quantity 是目标股数（整数股）；初始资金单位为 USD。两种模板均无自动卖出、止损或止盈规则，期末按最后价格估值，可能留有持仓；信号不保证成交，仍受资金、风险与后续行情约束。</p>
      <p>可改资金、已列参数及兼容数据；标的规格、风控与撮合成本沿用所选模板，以下和执行预览显示准确值。研究模板不承诺可晋级模拟。</p>
    </section>}
    {candidate?.template_assumptions && <details className="provenance" open>
      <summary>核对模板的标的规格、风险与执行假设</summary>
      <p>当前随包提供两个 XNAS:AAPL 示例模板，这是本批已验证的准备范围。绑定后可编辑策略参数和资金；其他假设沿用模板，并在实验预览中再次展示。</p>
      <dl className="summary-list">
        {Object.entries(candidate.template_assumptions.instrument).map(([key, value]) => <div key={key}><dt>{TEMPLATE_LABELS[key] ?? key}</dt><dd>{value}</dd></div>)}
        {Object.entries(candidate.template_assumptions.risk).map(([key, value]) => <div key={key}><dt>{TEMPLATE_LABELS[key] ?? key}</dt><dd>{value}</dd></div>)}
        {Object.entries(candidate.template_assumptions.execution).map(([key, value]) => <div key={key}><dt>{TEMPLATE_LABELS[key] ?? key}</dt><dd>{value === 'phase1.next-bar-close.v1' ? '下一根 K 线收盘撮合' : key === 'commission' && value && typeof value === 'object' && 'commission_bps' in value ? `${value.commission_bps} 基点` : String(value)}</dd></div>)}
      </dl>
    </details>}
    {candidate?.summary && <dl className="summary-list">
      <div><dt>策略标识</dt><dd>{candidate.summary.strategy_id}</dd></div>
      <div><dt>标的</dt><dd>{`${candidate.summary.venue}:${candidate.summary.symbol}`}</dd></div>
      <div><dt>初始资金</dt><dd>{`${candidate.summary.initial_cash} USD`}</dd></div>
      <div><dt>记录数</dt><dd>{datasetId ? compatibleData.find(item => item.dataset_id === datasetId)?.record_count ?? '未知' : isTemplate ? '尚未选择研究数据' : candidate.summary.record_count}</dd></div>
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
        disabled={busy || !candidate || !confirmed || (isTemplate && !datasetId)}>绑定场景</button>
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
function NextStep({ api, strategy, openExperiments, completedReads }: {
  api: ApiAdapter
  strategy: UserStrategy
  openExperiments: () => void
  completedReads: Record<string, string>
}) {
  const visit = useLocation().key
  const latest = strategy.experiments.at(-1)
  const draft = strategy.draft
  // A core-owned frozen input remains frozen even without a successful result.
  const covered = strategy.experiments.some(item =>
    (item.status === 'created' || (item.owner === 'quine' && Boolean(item.frozen_sha256)))
      && item.payload.draft_revision === draft.revision)
  const needsPreview = !isIntentDraft(draft) && !covered
  // A created experiment has a real job; its state is that job's own, never the
  // dispatch record's. An outcome this page has not read is not claimed here.
  const jobId = latest?.status === 'created' ? latest.job_id : null
  return <section aria-label="下一步" className="next-step">
    <dl>
      <div><dt>最近冻结</dt><dd>{latest
        ? <>
            {`v${latest.payload.draft_revision}`} · 记录于 <time dateTime={latest.created_at} title={latest.created_at}>{latest.created_at.slice(0, 10)} {utcClock(latest.created_at)}</time>
            {' · 关联实验状态：'}{jobId
              ? <JobStatusCell key={jobId} api={api} jobId={jobId} strategyId={strategy.user_strategy_id}
                  refreshKey={`${visit}|${completedReads[jobId] ?? ''}`} />
              : EXPERIMENT_STATUS_LABELS[latest.status]}
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
function StrategyDetail({ api, strategy, scenarios, catalogReady, changed, failed, completedReads }: {
  api: ApiAdapter
  strategy: UserStrategy
  scenarios: ScenarioSummary[]
  catalogReady: boolean
  changed: (strategy: UserStrategy) => void
  failed: (message: string) => void
  completedReads: Record<string, string>
}) {
  const showWorkspace = useWorkspaceView()
  const hasRun = strategy.experiments.some(item => item.status === 'created')
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab')
  const [editingDraft, setEditingDraft] = useState(false)
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
      <span className="chip">未指定交易所</span><span className="chip chip-draft">{revisionChip(strategy.draft)}</span>
      {!isIntentDraft(strategy.draft) && <button className="draft-edit-toggle" aria-expanded={editingDraft} onClick={() => { selectTab('strategy'); setEditingDraft(value => !value) }}>{editingDraft ? '返回概览' : '编辑草稿'}</button>}
      <a className="jump-link jump-to-ai" href="#strategy-message" onClick={() => showWorkspace('ai')}>讨论这个研究</a>
    </header>
    <p className="research-context">{isIntentDraft(strategy.draft) ? '尚未绑定场景' : `${strategy.draft.venue}:${strategy.draft.symbol} · ${strategy.draft.scenario_id}`} · 市场类型未记录 · 历史回测</p>
    <div className="research-status-line" hidden={tab === 'experiments'}>
      <NextStep api={api} strategy={strategy} completedReads={completedReads} openExperiments={() => selectTab('experiments')} />
      <a className={`pending-changes${pending ? ' has-pending' : ''}`} href="#strategy-ai-column" onClick={() => showWorkspace('ai')}>{pending ? `待处理：${pending} 项策略修改` : '暂无待处理修改'}</a>
    </div>
    <DetailTabs active={tab} select={selectTab} idBase={idBase} />
    <div className="strategy-tabpanel" role="tabpanel" id={`${idBase}-panel-${tab}`}
      aria-labelledby={`${idBase}-tab-${tab}`} tabIndex={0}>
      {tab === 'strategy' && <>
        <DraftFacts draft={strategy.draft} />
        <IntentSection strategy={strategy} />
        {isIntentDraft(strategy.draft) ? <BindScenario api={api} strategy={strategy} scenarios={scenarios}
          catalogReady={catalogReady} changed={changed} failed={failed} />
          : <><section className="draft-overview" hidden={editingDraft}><h2>关键信息</h2><dl className="inspector-facts"><div><dt>交易标的</dt><dd>{strategy.draft.venue}:{strategy.draft.symbol}</dd></div><div><dt>市场类型</dt><dd>未记录</dd></div><div><dt>数据来源</dt><dd>{strategy.draft.dataset_id ?? `注册场景 · ${strategy.draft.scenario_id}`}</dd></div>{Object.entries(strategy.draft.parameters).map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}<div><dt>初始资金（模拟）</dt><dd>{strategy.draft.initial_cash} USD</dd></div></dl></section><div hidden={!editingDraft}><DraftEditor key={strategy.user_strategy_id} api={api} strategy={strategy} changed={changed} /></div><details className="provenance"><summary>场景规则与来源</summary><RulesSection draft={strategy.draft} /></details><div hidden={!editingDraft}><SelectDataset api={api} strategy={strategy} changed={changed} failed={failed} /></div></>}
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
function CallEvidence({ call, strategyId }: { call: UserStrategyCall | undefined; strategyId: string }) {
  const showWorkspace = useWorkspaceView()
  const evidence = call?.evidence ?? []
  if (!evidence.length) return null
  return <details className="provenance">
    <summary>{`这次调用所依据的冻结证据（${evidence.length}）`}</summary>
    <ul className="path-list">
      {evidence.map(item => <li key={item.job_id}>
        <Link onClick={() => showWorkspace('work')}
          to={`/strategies/${encodeURIComponent(strategyId)}?tab=experiments&evidence_a=${encodeURIComponent(item.job_id)}`}>
          查看冻结实验 · {item.job_id}
        </Link><br />
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
 * Saved turns and their proposals share one chronological scroller. The composer
 * stays below it; the context names the draft, selected model and frozen evidence.
 * A refused or unknown call never borrows an older answer. A new route visit
 * requires its own approval.
 */
const OBSERVATION_INTERVAL_MS = 4000

/** What one tool call of the running turn is doing, in the turn's own order. */
type LiveTool = { id: string; tool: string; state: 'running' | 'done' | 'failed'; detail: string }

const TOOL_LABELS: Record<string, string> = {
  discover_resources: '发现策略与数据',
  inspect_research_input: '核查实验输入',
  read_experiments: '读取正式报告',
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

/** Persisted calls remain in their original turn, with their own evidence and decisions. */
function RecordedTurn({ calls: turnCalls, messages: turnMessages, strategyId, current, children }: {
  calls: UserStrategyCall[]; messages: UserStrategyMessage[]; strategyId: string; current: boolean; children?: ReactNode
}) {
  const latest = turnCalls.at(-1)!
  const root = turnCalls.find(call => call.call_id === (latest.turn_id ?? latest.call_id)) ?? turnCalls[0]
  const answers = turnMessages.filter(item => item.role === 'model')
  const answer = answers.at(-1)
  return <section aria-label={current ? '当前一轮' : `已保存轮次 ${root.call_id}`} className="turn">
      {(turnMessages.length > 0 || turnCalls.some(call => call.tool_results?.length)) &&
        <div role="group" aria-label={current ? '本轮回答' : '已保存的回答'} tabIndex={0}>
          {turnCalls.map(call => <div className="agent-call" key={call.call_id}>
            {turnMessages.filter(item => item.call_id === call.call_id).map(item =>
              <div key={item.message_id} className={item.role === 'user' ? 'message message-user' : 'turn-answer'}>
                <p className="message-role">{item.role === 'user' ? '你' : 'QUINE'} · <time dateTime={item.at} title={item.at}>{utcClock(item.at)}</time></p>
                <p>{item.text}</p>
                {item.role === 'model' && <span className="inference-label">模型推断</span>}
              </div>)}
            {call.tool_results?.map(tool =>
              <details className={`agent-tool-step tool-${tool.status}`} key={tool.tool_id}>
                <summary>{tool.status === 'done' ? '✓' : '!'} {TOOL_LABELS[tool.tool] ?? tool.tool} · {tool.status === 'done' ? '已完成' : '失败'}</summary>
                <pre>{tool.text}</pre>
              </details>)}
          </div>)}
        </div>}
      {!answer && <p className="muted">这一轮没有被观测到的模型回答。</p>}
      {latest?.message && <p className="notice">{latest.message}</p>}
      {latest && <CallNextStep call={latest} />}
      {children}
      {/* One provenance disclosure retains every call; equal evidence is shown once. */}
      {root && <details className="provenance turn-details">
        <summary>详情 · <span>{CALL_STATUS_LABELS[latest.status]}</span></summary>
        <p className="muted"><span>模型推断 — 不是正式证据。</span> 草稿修订 {root.base_revision}</p>
        {turnCalls.length > 1 && turnCalls.map(call => <p key={call.call_id}>{`${call.call_id} · ${CALL_STATUS_LABELS[call.status]} · ${usageLine(call)}`}</p>)}
        <p className="muted">{usageLine(root)}</p>
        {root.cost
          ? <p className="muted">{root.cost.available
              ? `成本：${root.cost.amount ?? '金额未记录'}`
              : `成本不可用：${root.cost.reason}。`}</p>
          : <p className="muted">这次调用没有成本记录，因此这里不写任何金额。</p>}
        <dl className="summary-list">
          {root.model_profile && <div><dt>当次连接</dt><dd>{root.model_profile.name} · v{root.model_profile.revision}</dd></div>}
          <div><dt>提供方</dt><dd>{root.route.provider ?? '未记录'}</dd></div>
          <div><dt>模型</dt><dd>{root.model ?? root.route.model ?? '未记录'}</dd></div>
          <div><dt>端点</dt><dd>{root.route.endpoint ?? '未记录'}</dd></div>
          {root.task?.mode !== "interactive" && <div><dt>历史授权</dt><dd>{`${root.task?.limit ?? root.approved_budget.max_model_calls} 次模型请求 · ${root.approved_budget.max_backtests} 次回测`}</dd></div>}
        </dl>
        {turnCalls.filter((call, index, all) => call.evidence?.length && all.findIndex(other => JSON.stringify(other.evidence) === JSON.stringify(call.evidence)) === index)
          .map(call => <CallEvidence key={call.call_id} call={call} strategyId={strategyId} />)}
      </details>}
  </section>
}

function ChatPanel({ api, strategy, strategies, changed, modelSendBlock, modelStatus, completedReads, completed, fixedEvidenceJobId }: {
  api: ApiAdapter
  strategy: UserStrategy
  strategies: { user_strategy_id: string; name: string }[]
  completedReads: Record<string, string>
  completed: (strategyId: string, jobId: string, turnId: string) => void
  changed: (strategy: UserStrategy) => void
  fixedEvidenceJobId?: string
  modelStatus?: ModelConfigurationStatus | null
  /** Null only after the current settings read reports configured=true. */
  modelSendBlock: string | null
}) {
  const navigate = useNavigate()
  const showWorkspace = useWorkspaceView()
  const location = useLocation()
  const [routeParams] = useSearchParams()
  const params = useMemo(() => fixedEvidenceJobId ? new URLSearchParams({ evidence_a: fixedEvidenceJobId }) : routeParams, [fixedEvidenceJobId, routeParams])
  const runs = useMemo(() => selectableRuns(strategy), [strategy])
  const selection = useMemo(() => readEvidenceSelection(params, runs), [params, runs])
  const { a, b } = selectedRuns(selection, runs)
  const aState = useRunRead(api, a?.job_id ?? null, `${location.key}|${a?.job_id ? completedReads[a.job_id] ?? '' : ''}`)
  const bState = useRunRead(api, b?.job_id ?? null, `${location.key}|${b?.job_id ? completedReads[b.job_id] ?? '' : ''}`)
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
  const messageKey = `quine.unsent.${strategy.user_strategy_id}.${strategy.draft.revision}`
  const [message, setMessage] = useState(() => { try { return sessionStorage.getItem(messageKey) ?? '' } catch { return '' } })
  useEffect(() => { try { if (message) sessionStorage.setItem(messageKey, message); else sessionStorage.removeItem(messageKey) } catch { /* optional unsent text buffer */ } }, [message, messageKey])
  const continuation = useRef<{ key: string; id: string } | null>(null)
  if (visit.at !== at) {
    setVisit({ at, id: visit.id + 1 })
  }
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  // The conversation's own durable state, read from the host: what it is waiting
  // for and what it may still spend. A reload restores exactly this, so the panel
  // never depends on having watched the stream that produced it.
  const [conversation, setConversation] = useState<UserStrategyConversation | null>(null)
  const [conversationNote, setConversationNote] = useState('')
  const [streamUnconfirmed, setStreamUnconfirmed] = useState(false)
  // The turn being watched right now: deltas the provider really sent, and the
  // tools it really asked for. It is cleared when the turn ends, because the
  // canonical record is what is shown from then on.
  const [live, setLive] = useState<{ text: string; tools: LiveTool[] } | null>(null)
  const [liveTurn, setLiveTurn] = useState<{ id: string; prompt: string } | null>(null)
  const [runStatus, setRunStatus] = useState('')
  const current = useRef<AbortController | null>(null)
  const decided = useRef<Set<string>>(new Set())
  const observedCalls = useRef<Set<string>>(new Set())
  const latest = strategy.calls.at(-1)
  const turns = useMemo(() => {
    const grouped = new Map<string, { id: string; calls: UserStrategyCall[]; messages: UserStrategyMessage[] }>()
    for (const call of strategy.calls) {
      const id = call.turn_id ?? call.call_id
      const turn = grouped.get(id) ?? { id, calls: [], messages: [] }
      turn.calls.push(call)
      grouped.set(id, turn)
    }
    for (const item of strategy.messages) {
      const call = strategy.calls.find(call => call.call_id === item.call_id)
      const turn = grouped.get(call?.turn_id ?? item.call_id)
      turn?.messages.push(item)
    }
    return [...grouped.values()]
  }, [strategy.calls, strategy.messages])
  const renderProposal = (proposal: UserStrategyProposal) => <ProposalPanel
    key={`${strategy.user_strategy_id}-${proposal.proposal_id}-${strategy.draft.revision}-${proposal.status}`}
    api={api} strategy={strategy} proposal={proposal} changed={changed} />
  const unresolved = strategy.calls.some(call =>
    call.status === 'pending' || call.status === 'dispatched')
  const streaming = Boolean(api.streamUserStrategyMessage)

  const awaitingRunDecision = Boolean(conversation?.pending)
  const canSend = (!streaming || conversation !== null) && !awaitingRunDecision && !streamUnconfirmed
  const task = conversation?.task
  const interactiveLimit = conversation?.budget.max_interactive_requests ?? 24
  const taskTurn = (busy ? liveTurn?.id : null) ?? task?.turn_id
  const taskActive = busy || Boolean(task && ['running', 'awaiting_budget', 'awaiting_decision'].includes(task.status))
  const stopLabels: Record<string, string> = {
    completed: '已完成', budget_exhausted: '旧任务已暂停，可继续研究', execution_limit: '连续执行已暂停，可继续研究',
    cancelled: '任务已取消，停止后续请求', unknown: '响应未确认，未自动重试',
    consecutive_failures: '连续两次工具失败，已停止', scope_decision: '需要调整任务范围，已停止',
    decision_required: '等待你的决定', declined: '已按你的决定结束',
    unconfigured: '模型未配置，未派发请求', interrupted: '执行中断，未自动重发',
    experiment_failed: '实验未成功，已停止',
  }

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

  const apply = (event: UserStrategyConversationEvent, observed?: UserStrategyConversationPending) => {
    if (event.kind === 'text_delta') {
      if (event.call_id && !observedCalls.current.has(event.call_id)) {
        observedCalls.current.add(event.call_id)
        void refresh()
      }
      setLive(state => ({ text: (state?.text ?? '') + event.text, tools: state?.tools ?? [] }))
      return
    }
    if (event.kind === 'tool_start') {
      void refresh()
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
      if (event.kind === 'done' && observed?.launch_intent_id) {
        // This stream belongs to one resumed turn. Resolve its frozen intent,
        // never the latest run or an unscoped job from a state event. A failed or
        // budget-exhausted turn also ends here; the read determines readiness.
        const matches = event.strategy.experiments.filter(item => item.launch_intent_id === observed.launch_intent_id)
        const jobId = matches.length === 1 ? matches[0].job_id : null
        if (jobId && (!observed.job_id || observed.job_id === jobId)) {
          completed(strategy.user_strategy_id, jobId, observed.turn_id)
        }
      }
      changed(event.strategy)
      setLive(null)
      setRunStatus('')
      void refresh()
      return
    }
  }

  const runStream = async (
    source: (signal: AbortSignal) => Promise<AsyncGenerator<UserStrategyConversationEvent>>,
    observed?: UserStrategyConversationPending,
  ) => {
    // Observation and explicit clicks share one request slot. A timer must never
    // abort a model response (the provider may already have charged that call).
    if (current.current) return
    const controller = new AbortController()
    current.current = controller
    setBusy(true)
    setNote('')
    setStreamUnconfirmed(false)
    try {
      const events = await source(controller.signal)
      for await (const event of events) {
        if (!active()) return
        apply(event, observed)
      }
    } catch (error) {
      if (active() && !isAbort(error)) {
        // A stale completed turn is not evidence for the disconnected request.
        // Keep received content, block a blind resend, and require reopening.
        setStreamUnconfirmed(true)
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
    setLiveTurn({ id: input.turn_id, prompt: strategy.messages.find(item => item.call_id === input.turn_id && item.role === 'user')?.text ?? '' })
    const observed = (input.action === 'confirm' || input.action === 'observe')
      && conversation?.pending?.turn_id === input.turn_id ? conversation.pending : undefined
    void runStream(signal => resume(strategy.user_strategy_id, input, signal), observed)
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
    ? conversation.pending
    : null
  useEffect(() => {
    if (!observationTurn || !api.resumeUserStrategyConversation) return
    let stopped = false
    let handle: ReturnType<typeof setTimeout> | null = null
    const tick = () => {
      handle = setTimeout(async () => {
        if (stopped) return
        await runStream(signal => api.resumeUserStrategyConversation!(
          strategy.user_strategy_id, { turn_id: observationTurn.turn_id, action: 'observe' }, signal), observationTurn)
        if (!stopped) tick()
      }, OBSERVATION_INTERVAL_MS)
    }
    tick()
    return () => { stopped = true; if (handle) clearTimeout(handle) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [observationTurn?.turn_id, observationTurn?.job_id, observationTurn?.launch_intent_id, api.resumeUserStrategyConversation, strategy.user_strategy_id, visitKey])

  const ask = async () => {
    // UI readiness is only a sending precondition; the backend still owns the
    // actual configuration, approval and provider execution checks.
    if (inFlight.current || current.current || unresolved || !evidenceReady || awaitingRunDecision) return
    if (modelSendBlock !== null) {
      setNote(modelSendBlock)
      return
    }
    if (message.trimStart().startsWith('/')) return
    const evidence = a ? { evidence_job_ids: [a.job_id!, ...(b ? [b.job_id!] : [])] } : {}
    const text = message.trim()
    const revision = strategy.draft.revision
    const callId = newTurnId()
    setLiveTurn({ id: callId, prompt: text })
    inFlight.current = true
    setBusy(true)
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
            call_id: callId,
            base_revision: revision,
            text,
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
          call_id: callId,
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
      }
    } catch (error) {
      if (active()) setNote(explain(error))
    } finally {
      inFlight.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const continueTask = () => {
    if (!task) return
    const key = `${task.turn_id}:${task.used}`
    if (continuation.current?.key !== key) continuation.current = {key, id: newTurnId()}
    decide({turn_id: task.turn_id, action: 'continue', grant_id: continuation.current.id})
  }
  const cancelTask = async () => {
    if (!taskTurn || !api.resumeUserStrategyConversation) return
    try {
      // Separate control request: do not abort or wait for the provider's stream.
      for await (const event of await api.resumeUserStrategyConversation(strategy.user_strategy_id,
        { turn_id: taskTurn, action: 'cancel' })) {
        if (active()) apply(event)
      }
    } catch (error) { if (active()) setNote(explain(error)) }
  }

  const modelName = modelStatus?.profiles?.find(item => item.id === (strategy.model_profile_id ?? modelStatus.default_profile_id))?.model ?? modelStatus?.model ?? '未配置模型'
  const contextName = a ? `证据 ${b ? 'A / B' : 'A'} · v${a.payload.draft_revision}` : `草稿 v${strategy.draft.revision}`
  const configLocked = taskActive || unresolved || awaitingRunDecision || streamUnconfirmed
  const commands: ComposerCommand[] = [
    ...(api.selectUserStrategyModelProfile && modelStatus?.profiles ? [{
      name:'model', label:'模型连接', scope:'本研究 · 下一轮生效，不改变已冻结任务',
      options:[{value:'',label:`跟随工作区默认 · ${modelStatus.profiles.find(p=>p.id===modelStatus.default_profile_id)?.name ?? '未设置'}`},
        ...modelStatus.profiles.map(p=>({value:p.id,label:`${p.name} · ${p.model}`}))],
      choose:async(value:string)=>{const updated=await api.selectUserStrategyModelProfile!(strategy.user_strategy_id,value||null);if(active())changed(updated)},
    }] : []),
    ...(!fixedEvidenceJobId ? [
    {name:'context',label:'证据上下文',scope:'本次对话 · 只选择已冻结记录，不改变输入或执行实验',
      options:[{value:'draft',label:`当前草稿 v${strategy.draft.revision}`},...runs.map(r=>({value:r.job_id!,label:`实验 v${r.payload.draft_revision} · ${r.created_at.slice(0,16)} · ${r.job_id!.slice(0,8)}`}))],
      choose:(value:string)=>{const next=evidenceAddress(params,runs.find(r=>r.job_id===value)??null,null);if(value==='draft')next.set('context','draft');navigate(`${location.pathname}?${next}`)},
    },
    ...(a && runs.length>1 ? [{name:'compare',label:'比较证据 B',scope:'本次对话 · 与当前 A 比较，不改变已冻结记录',
      options:[{value:'none',label:'仅证据 A'},...runs.filter(r=>r.job_id!==a.job_id).map(r=>({value:r.job_id!,label:`实验 v${r.payload.draft_revision} · ${r.job_id!.slice(0,8)}`}))],
      choose:(value:string)=>navigate(`${location.pathname}?${evidenceAddress(params,a,runs.find(r=>r.job_id===value)??null)}`),
    }] : []),
    ] : []),
    {name:'research',label:'当前研究',scope:'切换工作区 · 保留各研究的消息和草稿',
      options:strategies.map(r=>({value:r.user_strategy_id,label:r.name})),choose:(value:string)=>navigate(`/strategies/${encodeURIComponent(value)}`)},
  ]
  const executionState = streamUnconfirmed ? '响应未确认，请重新打开研究核对；未自动重试' : busy ? '正在处理…' : conversation?.pending?.phase === 'awaiting_confirmation' ? '等待实验确认'
    : conversation?.pending?.phase === 'awaiting_observation' ? '实验运行中'
    : conversation?.pending?.phase === 'awaiting_proposal' ? '等待修改决定'
    : task?.stop_reason ? stopLabels[task.stop_reason] ?? '已停止' : '准备就绪'
  return <section aria-label="AI 协作" className="strategy-panel strategy-chat">
    <header className="chat-head agent-identity">
      <span className="agent-mark"><WorkbenchIcon name="spark" /></span>
      <div><h2>QUINE <span>研究助手</span></h2><p>{modelName}</p></div>
    </header>
    <div className="agent-object"><WorkbenchIcon name="draft" /><div><small>当前研究</small><strong title={strategy.name}>{strategy.name}</strong></div><span>草稿 v{strategy.draft.revision}</span></div>
    <nav className="agent-context" aria-label="AI 当前上下文">
      <Link onClick={()=>showWorkspace('work')} to={`/strategies/${encodeURIComponent(strategy.user_strategy_id)}?${new URLSearchParams({...Object.fromEntries(params),tab:'strategy'})}`}>草稿 v{strategy.draft.revision}</Link>
      {a && <Link onClick={()=>showWorkspace('work')} to={`/strategies/${encodeURIComponent(strategy.user_strategy_id)}?tab=experiments&evidence_a=${encodeURIComponent(a.job_id!)}`}>证据 A · v{a.payload.draft_revision}</Link>}
      {b && <Link onClick={()=>showWorkspace('work')} to={`/strategies/${encodeURIComponent(strategy.user_strategy_id)}?tab=experiments&evidence_a=${encodeURIComponent(b.job_id!)}`}>证据 B · v{b.payload.draft_revision}</Link>}
    </nav>
    {!evidenceReady && <p className="agent-inline-note" role="status">所选证据尚未就绪 · {aState?.kind === 'unavailable' ? aState.note : bState?.kind === 'unavailable' ? bState.note : '正在读取实验状态'}</p>}
    <AgentFeed identity={strategy.user_strategy_id}
      version={`${strategy.updated_at}|${strategy.draft.revision}|${strategy.calls.length}|${strategy.messages.length}|${live?.text ?? ''}|${JSON.stringify(live?.tools)}|${conversation?.pending?.phase}|${runStatus}|${note}`}>

    {!latest && <div className="chat-empty-guidance">
      <div className="chat-guidance-heading"><div className="assistant-empty-mark" aria-hidden="true">···</div><div><h3>{fixedEvidenceJobId ? "解读这份冻结报告" : "继续你的研究"}</h3><p className="muted">{a ? `已选择实验 · 冻结 v${a.payload.draft_revision}` : `当前草稿 v${strategy.draft.revision}`}</p></div></div>
      <div className="chat-suggestions">
        <button type="button" aria-label={a ? '解读所选实验' : '查找策略与数据'} onClick={() => setMessage(a ? '请解读当前所选实验的结果、成本和局限。' : '请查找与我的研究想法匹配的支持策略和真实数据，并核查缺少哪些输入。')}>
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" /></svg>
          <span><strong>{a ? '解读所选实验' : '查找策略与数据'}</strong><small>{a ? '结果、费用与样本局限' : '支持策略、数据与缺少的输入'}</small></span>
        </button>
        <button type="button" aria-label={b ? '比较 A/B 证据' : '检查风险与成本'} onClick={() => setMessage(b ? '请比较当前 A/B 实验，指出差异、风险和仍缺少的证据。' : '当前研究还缺少哪些风险和成本证据？')}>
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M4 20V10h4v10M10 20V4h4v16M16 20v-7h4v7" /></svg>
          <span><strong>{b ? '比较 A/B 证据' : '检查风险与成本'}</strong><small>{b ? '核对差异与仍缺少的证据' : '核查假设、成本与缺失证据'}</small></span>
        </button>
      </div>
    </div>}
    {isIntentDraft(strategy.draft) && <p className="notice">
      尚未绑定场景。Agent 可以查找支持的策略与真实数据、核查输入；选择绑定后才能提出字段修改。
    </p>}
    {unresolved && <p className="notice">
      还有一次已记录的调用没有结论。在它落实之前不会再问下一次，因此不会覆盖任何东西。
    </p>}
    {strategy.messages.filter(item => !strategy.calls.some(call => call.call_id === item.call_id)).map(item =>
      <div className={`message message-${item.role}`} key={item.message_id}>
        <p className="message-role">{item.role === 'user' ? '你' : 'QUINE'} · <time dateTime={item.at} title={item.at}>{utcClock(item.at)}</time></p>
        <p>{item.text}</p><p className="muted">历史消息 · 对应调用记录不可用{item.role === 'model' ? ' · 模型推断，不是正式证据' : ''}</p>
      </div>)}
    {turns.map((turn, index) => <RecordedTurn key={turn.id} calls={turn.calls} messages={turn.messages}
      strategyId={strategy.user_strategy_id} current={!live && index === turns.length - 1}>
      {strategy.proposals.filter(item => turn.calls.some(call => call.call_id === item.call_id)).map(renderProposal)}
    </RecordedTurn>)}
    {live && <section aria-label="当前一轮" className="turn live-turn">
      {!turns.some(turn => turn.id === liveTurn?.id) && <div className="message message-user"><p className="message-role">你 · 草稿 v{strategy.draft.revision}</p><p>{liveTurn?.prompt}</p></div>}
      <p className="turn-head"><span className="status status-dispatched">{streamUnconfirmed ? '响应未确认' : '正在请求模型'}</span></p>
      <div className="turn-answer live-answer" role="status" aria-live="polite">
        <div className="answer-scroll" role="group" aria-label="正在生成的回答" tabIndex={0}>
          <p>{live.text || (streamUnconfirmed ? '连接已中断，请重新打开研究查看原请求。' : '正在等待模型响应…')}</p>
        </div>
        <span className="inference-label">{streamUnconfirmed ? '已收到的内容 · 结果未确认' : '生成中'}</span>
      </div>
      {live.tools.length > 0 && <ul className="tool-list" aria-label="本轮工具调用">
        {live.tools.map(item => <li key={item.id} className={`tool tool-${item.state}`}>
          <details><summary><strong>{TOOL_LABELS[item.tool] ?? item.tool}</strong>
            {item.state === 'running' ? ' · 执行中…' : item.state === 'done' ? ' · 已完成' : ' · 失败'}
          </summary><p>{item.detail || '已收到开始事件，尚无结果。'}</p></details>
        </li>)}
      </ul>}
    </section>}
    {strategy.proposals.filter(item => !strategy.calls.some(call => call.call_id === item.call_id)).map(renderProposal)}
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
    </AgentFeed>
    {conversation?.pending?.phase === 'awaiting_budget' && <div className="agent-pause" role="status">
      <span>{task?.stop_reason === 'execution_limit' ? '本次请求额度已用完，进度已保存。' : '这项旧任务已暂停，进度已保存。'}</span>
      <button type="button" onClick={continueTask} disabled={busy}>授权继续（最多再 {interactiveLimit} 次请求）</button>
    </div>}
    <ResearchComposer value={message} change={setMessage} send={()=>void ask()}
      stop={taskActive && taskTurn ? ()=>void cancelTask() : undefined} sending={busy}
      canSend={!busy&&!unresolved&&!!message.trim()&&modelSendBlock===null&&evidenceReady&&canSend}
      locked={configLocked} commands={commands} status={`${contextName} · ${executionState}`}>
      <p className="agent-inline-note">发送新消息即授权最多 {interactiveLimit} 次模型请求；费用取决于模型用量。</p>
      {task && <p className="agent-inline-note">当前任务累计授权 {task.limit} 次，已用 {task.used} 次，剩余 {task.remaining} 次。</p>}
      {!!message && <p className="agent-inline-note">未发送文字仅本次会话暂存；退出前请在输入框全选复制到本地文件。不要在消息中填写密钥。</p>}
      {streaming && conversation===null && <p className="agent-inline-note" role="status">正在恢复会话…</p>}
      {modelSendBlock!==null && <p className="agent-inline-note" role="status">模型未就绪 · <Link to="/resources/models">设置连接</Link></p>}
      {conversation?.pending?.phase!=='awaiting_confirmation' && note && <p role="alert" className="agent-inline-note error">{note}</p>}
    </ResearchComposer>

  </section>
}

/**
 * The AI column before anything is open: it explains what it will hold. While a
 * selected strategy is still being read it says that instead, so a read in flight
 * is never shown as "nothing is selected", and a read that failed says so too.
 */
function EmptyChat({ note, live = false }: { note?: string; live?: boolean }) {
  const show = useWorkspaceView()
  const reveal = () => {
    show('work')
    // Focus the real search after the narrow workspace switches back to content.
    requestAnimationFrame(() => document.querySelector<HTMLElement>('input[aria-label="搜索研究"]')?.focus())
  }
  return <section aria-label="AI 协作" className="strategy-panel strategy-chat strategy-chat-empty">
    <header className="chat-head agent-identity"><span className="agent-mark"><WorkbenchIcon name="spark" /></span><h2>QUINE <span>研究助手</span></h2></header>
    {note ? <p className="chat-empty-status" role={live ? 'status' : undefined} aria-live={live ? 'polite' : undefined}>{note}</p>
      : <>
        <div className="agent-object"><WorkbenchIcon name="layers" /><div><small>当前上下文</small><strong>研究工作区</strong></div><span>未选择研究</span></div>
        <div className="chat-empty-start">
          <span className="chat-empty-symbol"><WorkbenchIcon name="spark" /></span>
          <h3>从问题开始，<br />循证前进。</h3>
          <p>选择或新建研究，继续它自己的对话。让 AI 帮你梳理思路、审阅草稿和解读证据。</p>
          <div className="assistant-start-actions">
            <Link to="/strategies" state={{ createStrategy: true }} onClick={() => show('work')}><WorkbenchIcon name="idea" /><span><strong>梳理研究思路</strong><small>先保存想法，再逐步验证</small></span><WorkbenchIcon name="arrow" /></Link>
            <Link to="/resources/data"><WorkbenchIcon name="evidence" /><span><strong>查看数据与模型</strong><small>准备研究所需的输入</small></span><WorkbenchIcon name="arrow" /></Link>
          </div>
          <div className="chat-empty-actions"><button type="button" onClick={reveal}>选择研究</button><span>继续已有的对话</span></div>
        </div>
        <footer className="assistant-boundary"><WorkbenchIcon name="draft" /><p>AI 建议由你审阅。<br />每次实验，都需要你的确认。</p></footer>
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
  const canUndo = Boolean(undone && undone.proposal_id === proposal.proposal_id && undone.to_revision === strategy.draft.revision)
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
          修改会把当前建议留作历史，并在当前修订上用这一次已批准的调用生成新的修订建议。
        </p>
      </div>
    </details>}
  </>

  // The proposal's own origin line, its grounded evidence and the recorded status
  // are the same facts in both states; only where they live changes.
  const origin = <p className="muted proposal-origin">
    {`针对草稿修订 ${proposal.base_revision} · 提案 ${proposal.proposal_id} · ${PROPOSAL_ORIGIN[proposal.status]}`}
  </p>
  const callEvidence = <CallEvidence strategyId={strategy.user_strategy_id} call={strategy.calls.find(item => item.call_id === proposal.call_id)} />
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

  return <section aria-label="建议修改" className={`strategy-panel proposal-panel ${open ? 'proposal-active' : 'proposal-history'}`}>
    <header className="panel-head">
      {open && <h2>建议修改</h2>}
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
      && !(item.status === 'unknown' && item.slot_held === false && item.recovery?.status === 'released')
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
        {item.owner === 'quine' && item.status === 'unknown' && <UnknownRecoveryControls
          key={`${strategy.user_strategy_id}/${item.launch_intent_id}`}
          strategyId={strategy.user_strategy_id} item={item} changed={changed} />}

      </li>)}
    </ul>}
    <p className="muted">
      正式报告在实验完成后显示。一次运行只是一个冻结的结果，不是已经完成的研究。
      已确认的实验会保留它冻结的那个修订，即使草稿继续前进。
    </p>

  </section>
}

function UnknownRecoveryControls({ strategyId, item, changed }: {
  strategyId: string; item: UserStrategyExperiment; changed: (value: UserStrategy) => void
}) {
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const sending = useRef(false)
  const active = useRef(true)
  const preparation = useRef({ revision: item.core_revision, id: crypto.randomUUID() })
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const recovery = item.recovery
  const released = item.slot_held === false && recovery?.status === 'released'
  const action = item.slot_held === true && item.attempt_id && item.core_revision
    ? recovery?.status === 'prepare_required' ? 'prepare' : recovery?.status === 'ready' ? 'release' : null
    : null
  const act = async () => {
    if (!action || !item.attempt_id || !item.core_revision || sending.current) return
    sending.current = true; setBusy(true); setFailure('')
    if (action === 'prepare' && preparation.current.revision !== item.core_revision) {
      preparation.current = { revision: item.core_revision, id: crypto.randomUUID() }
    }
    try {
      const updated = await userStrategiesApi.recoverExperiment(strategyId, item.launch_intent_id, {
        action, attempt_id: item.attempt_id, expected_revision: item.core_revision,
        preparation_id: recovery?.preparation_id ?? preparation.current.id,
      })
      if (active.current && updated.user_strategy_id === strategyId) changed(updated)
    } catch (error) {
      if (active.current) setFailure(`${explain(error)} 请重新加载并核对核心回执；不会自动重试或重跑。`)
    } finally {
      sending.current = false
      if (active.current) setBusy(false)
    }
  }
  return <div className="notice" aria-label="未知运行恢复">
    {released ? <p>结果仍未知；核心已验证旧计算停止并释放运行名额。旧任务不会重跑，冻结输入和已扣预算保留。新实验仍需重新预览并确认。</p>
      : item.slot_held === true ? <p>核心确认这次未知运行仍占用本工作区唯一的运行名额，其他研究也暂时不能启动新实验。重新打开应用不会自动释放该名额。保留历史、冻结输入和已扣预算；恢复仅解除名额，不恢复计算或补报结果。</p>
      : <p>当前无法确认运行名额是否占用，不能仅凭“结果未知”判断运行名额是否占用。若核心确认是中断后保留的未知运行，须核对停止证据；若只是核心暂时不可达，请恢复连接后重新读取状态。</p>}
    {recovery?.status === 'prepare_required' && <p>先记录恢复准备，然后正常退出 QUINE，手动重启此 Mac。重新打开后，核心会核对执行机器、工作区和系统启动证据。</p>}
    {recovery?.status === 'restart_required' && <p>恢复准备已保存。请正常退出 QUINE 后手动重启此 Mac；仅重开应用不能证明旧计算停止。</p>}
    {recovery?.status === 'ready' && <p>核心已核对同一机器跨系统启动的停止证据。可显式释放名额，原结果继续保留为未知。</p>}
    {!released && (!recovery || recovery.status === 'unavailable') && <p>缺少可验证的运行归属或停止证据，暂不能释放。旧版记录可能缺少机器身份；不要删除记录或修改数据库来解锁。</p>}
    {action && <button type="button" onClick={() => { void act() }} disabled={busy}>
      {busy ? '正在核对核心回执…' : action === 'prepare' ? '记录恢复准备（需重启 Mac）' : '保留未知结果并释放运行名额'}
    </button>}
    {failure && <p role="alert">{failure}</p>}
    <p>已保存的草稿和报告仍可查看。<Link to="/settings?section=about">查看脱敏诊断与备份说明</Link></p>
  </div>
}

/**
 * One row's run cell: the real state of the job the latest created experiment
 * dispatched, read from the job itself on mount, job change or an explicit
 * completion/navigation invalidation. Frozen experiment inputs never change.
 * The read is guarded, so an answer that arrives after the row moved on writes
 * nothing; there is no polling, and a job id that does not match the one asked
 * for is refused rather than shown as this row's run. A job this page cannot read
 * says so and still links to the run's own page.
 */
function JobStatusCell({ api, jobId, strategyId, refreshKey = '' }: { api: ApiAdapter; jobId: string; strategyId?: string; refreshKey?: string }) {
  type JobRead =
    | { kind: 'reading' }
    | { kind: 'ready'; job: BacktestJob }
    | { kind: 'unavailable'; note: string }
  // Kept with the job it was read for: while another job is being read the cell
  // shows that job's own load, never the previous job's status.
  const key = JSON.stringify([strategyId, jobId, refreshKey])
  const [loaded, setLoaded] = useState<{ key: string; api: ApiAdapter; read: JobRead } | null>(null)
  const state: JobRead = loaded && loaded.key === key && loaded.api === api ? loaded.read : { kind: 'reading' }

  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = () => api.getBacktest(jobId).then(job => {
      if (!active) return
      if (job.job_id === jobId && (job.status === 'accepted' || job.status === 'running' || job.status === 'stopping')) timer = setTimeout(() => {void load()}, OBSERVATION_INTERVAL_MS)
      setLoaded({ key, api, read: job.job_id === jobId
        ? { kind: 'ready', job }
        : { kind: 'unavailable', note: '读取到的任务与这条运行不一致' } })
    }).catch(() => {
      if (active) setLoaded({ key, api, read: { kind: 'unavailable', note: '运行状态不可读' } })
    })
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [api, jobId, key])

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
function researchLocation(id: string) {
  const path = `/strategies/${encodeURIComponent(id)}`
  try { const saved = sessionStorage.getItem(`quine.research-location.${id}`); return saved === path || saved?.startsWith(path + '?') ? saved : path } catch { return path }
}

const nextStepNames = { draft: '完善草稿', review: '审阅修改', experiment: '预览实验', inspect: '查看实验状态', evidence: '查看证据' } as const
function researchNextLocation(item: ResearchSummary) {
  const path = `/strategies/${encodeURIComponent(item.user_strategy_id)}`
  return item.next_step === 'draft' ? path : item.next_step === 'review' ? `${path}#strategy-ai-column`
    : `${path}?tab=experiments${item.latest_experiment?.job_id ? `&evidence_a=${encodeURIComponent(item.latest_experiment.job_id)}` : ''}`
}
function ResearchRow({ api, item, selected, select, changed }: { api: ApiAdapter; item: ResearchSummary; selected: boolean; select: (id: string) => void; changed: (value: UserStrategy) => void }) {
  const [editing, setEditing] = useState(false)
  const [exchange, setExchange] = useState<PlatformId | null>(item.exchange_id)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const latest = item.latest_experiment
  const status = latest?.read_error ? '状态未知' : latest?.job_status ? JOB_STATUS_LABELS[latest.job_status as keyof typeof JOB_STATUS_LABELS] ?? latest.job_status
    : latest ? EXPERIMENT_STATUS_LABELS[latest.launch_status as keyof typeof EXPERIMENT_STATUS_LABELS] ?? latest.launch_status : '尚未实验'
  const assign = async () => {
    if (!api.assignResearchExchange || busy) return
    setBusy(true); setError('')
    try { changed(await api.assignResearchExchange(item.user_strategy_id, exchange, item.updated_at)); setEditing(false) }
    catch (caught) { setError(explain(caught)) }
    finally { setBusy(false) }
  }
  return <tr className={selected ? 'selected' : undefined}>
    <th scope="row"><div className="research-row-identity"><button type="button" className="research-row-select" aria-label={`选择研究 ${item.name}`} aria-pressed={selected} onClick={() => select(item.user_strategy_id)}><WorkbenchIcon name="draft" /></button><div>
      <Link className="strategy-name" aria-current={selected ? 'page' : undefined} to={researchLocation(item.user_strategy_id)}>{item.name}</Link>
      <span className="strategy-instrument">{item.instrument ? `${item.instrument.venue}:${item.instrument.symbol}` : '尚未绑定'} · {item.exchange_id ? platformNames[item.exchange_id] : '未指定交易所'}</span>
      <span className="research-intent-excerpt">{item.intent}</span>
    </div></div></th>
    <td data-label="草稿状态"><span className="chip chip-draft">{item.draft.kind === 'intent' ? '想法已保存' : '草稿已配置'}</span><small className="strategy-instrument">v{item.draft.revision}{item.draft.pending_proposals > 0 ? ` · ${item.draft.pending_proposals} 项建议待审阅` : ''}</small><Link className="research-next-step" to={researchNextLocation(item)}>{nextStepNames[item.next_step]}</Link></td>
    <td data-label="实验"><span className={`research-experiment-status ${latest?.read_error ? 'unknown' : latest?.job_status ?? ''}`}>{status}</span><small className="strategy-instrument">{item.experiment_count} 次确认{latest?.draft_revision ? ` · 冻结 v${latest.draft_revision}` : ''}</small>{latest?.job_id && <Link to={`/backtests/${encodeURIComponent(latest.job_id)}?research=${encodeURIComponent(item.user_strategy_id)}`}>{latest.report_ready ? '查看正式报告' : '查看运行'}</Link>}{latest?.read_error && <small className="strategy-instrument" role="status">{latest.read_error}</small>}</td>
    <td data-label="最近更新"><div className="research-row-updated"><time dateTime={item.updated_at}>{item.updated_at.slice(0, 10)}<small className="strategy-instrument">{utcClock(item.updated_at)}</small></time><button className="research-row-menu" aria-label={`整理研究 ${item.name}`} aria-expanded={editing} onClick={() => { setEditing(value => !value); setExchange(item.exchange_id); setError('') }}>•••</button></div>
      {editing && <div className="research-row-organize"><label>研究归属<select value={exchange ?? ''} onChange={event => setExchange((event.target.value || null) as PlatformId | null)}><option value="">未指定交易所</option><option value="binance">Binance</option><option value="okx">OKX</option></select></label><button disabled={busy || !api.assignResearchExchange} onClick={() => { void assign() }}>保存归属</button><small>仅用于分类，不改变草稿。</small>{error && <p role="alert">{error}</p>}</div>}
    </td>
  </tr>
}
function StrategyTable({ api, strategies, selectedId, select, changed, filtered = false }: {
  api: ApiAdapter; strategies: ResearchSummary[]; selectedId?: string; select: (id: string) => void; changed: (value: UserStrategy) => void; filtered?: boolean
}) {
  return <nav aria-label="策略列表" className="strategy-list">
    {strategies.length === 0
      ? <div className="research-empty-state"><WorkbenchIcon name={filtered ? 'search' : 'idea'} /><h2>{filtered ? '没有匹配的研究' : '给你的下一个想法，留一个位置。'}</h2><p className="strategy-empty">{filtered ? '当前范围没有研究。可调整筛选，或新建研究。' : '还没有策略。'}</p>{!filtered && <p>点击「新建研究」保存想法，再绑定数据、完善草稿。</p>}</div>
      : <table className="strategy-table research-collection-table"><thead><tr><th scope="col">研究</th><th scope="col">草稿状态</th><th scope="col">实验</th><th scope="col">最近更新</th></tr></thead><tbody>{strategies.map(item => <ResearchRow key={item.user_strategy_id} api={api} item={item} selected={item.user_strategy_id === selectedId} select={select} changed={changed} />)}</tbody></table>}
  </nav>
}

/** The workspace itself: the table, the selected strategy and its AI column. */
export function StrategiesWorkspace({ api }: { api: ApiAdapter }) {
  const { platform } = usePlatform()
  const [listParams, setListParams] = useSearchParams()
  // The history entry is authoritative, including absent query values on Back.
  const scope = listParams.get('scope') ?? 'all'
  const search = listParams.get('search') ?? ''
  const sort = listParams.get('sort') ?? 'updated'
  const filter = (patch: { scope?: string; search?: string; sort?: string }) => {
    setListParams(new URLSearchParams({ scope, search, sort, ...patch }))
  }
  const { userStrategyId } = useParams()
  const lastSelectedId = (() => { try { return sessionStorage.getItem('quine.research-selection') ?? undefined } catch { return undefined } })()
  const navigate = useNavigate()
  const location = useLocation()
  useEffect(() => {
    if (userStrategyId) { try { sessionStorage.setItem(`quine.research-location.${userStrategyId}`, location.pathname + location.search); sessionStorage.setItem('quine.research-selection', userStrategyId) } catch { /* presentation only */ } }
  }, [userStrategyId, location.pathname, location.search])
  const [strategies, setStrategies] = useState<ResearchSummary[]>([])
  const [total, setTotal] = useState(0)
  const [listVersion, setListVersion] = useState(0)
  const queryKey = JSON.stringify([scope, search, sort, platform])
  // Share only the existing view invalidation between the selected research's
  // two panels. This stores no job result and never alters frozen evidence.
  const [completedReads, setCompletedReads] = useState<Record<string, Record<string, string>>>({})
  const completed = useCallback((strategyId: string, jobId: string, turnId: string) => {
    setCompletedReads(previous => previous[strategyId]?.[jobId] === turnId ? previous : {
      ...previous, [strategyId]: { ...previous[strategyId], [jobId]: turnId },
    })
  }, [])
  const [check, setCheck] = useState(0)
  const [catalogRead, setCatalogRead] = useState<{
    api: ApiAdapter; check: number; items: ScenarioSummary[]; error: string | null
  } | null>(null)
  const [dataRead, setDataRead] = useState<{
    api: ApiAdapter; check: number; count: number; invalid: number; error: string | null
  } | null>(null)
  const data = dataRead?.api === api && dataRead.check === check ? dataRead : null
  const [modelRead, setModelRead] = useState<{
    api: ApiAdapter; check: number; value: ModelConfigurationStatus | null; error: string | null
  } | null>(null)
  // Tag each independent read with its adapter and request. While a new read is in
  // flight, neither old readiness nor old bind choices are offered as current.
  const catalog = catalogRead?.api === api && catalogRead.check === check ? catalogRead : null
  const model = modelRead?.api === api && modelRead.check === check ? modelRead : null
  const scenarios = catalog?.error === null ? catalog.items : []
  const usableCount = scenarios.filter(item => item.valid && item.summary && item.scenario_id).length
  const [detail, setDetail] = useState<{ id: string; value: UserStrategy } | null>(null)
  // The list read is a state of its own: until it answers, an empty array is not an
  // empty workspace, and the count, the empty claim and the create form wait for it.
  // It is kept with the adapter it was read from, so "still reading" is derived
  // rather than written when the effect starts, and neither another adapter's answer
  // nor a late one from an adapter this workspace has left is read as this one's.
  const [listRead, setListRead] = useState<{ api: ApiAdapter; key: string; ok: boolean; note: string } | null>(null)
  const [creating, setCreating] = useState(false)
  // The half-written idea is kept above the form, so leaving the collection route
  // and coming back does not throw away what the user had typed.
  const [createDraft, setCreateDraft] = useState(() => { try { const value = JSON.parse(sessionStorage.getItem('quine.new-research') ?? 'null'); if (typeof value?.name === 'string' && typeof value?.intent === 'string') return { name: value.name as string, intent: value.intent as string } } catch { /* unavailable local buffer */ } return { name: '', intent: '' } })
  useEffect(() => { try { sessionStorage.setItem('quine.new-research', JSON.stringify(createDraft)) } catch { /* optional local buffer */ } }, [createDraft])
  const [detailNote, setDetailNote] = useState<{ id: string; text: string } | null>(null)
  // Every refusal is kept under the strategy it was made on. A note is written and
  // read for one strategy only, so a late answer for a strategy that is no longer
  // open can neither show over the open one nor replace the open one's note.
  const [notes, setNotes] = useState<Record<string, string>>({})

  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      const list = api.listResearch
      if (!list) {
        // The optional method is settled asynchronously, so a workspace that
        // unmounts or swaps its api never writes this note from a stale load.
        await Promise.resolve()
        if (active) setListRead({ api, key: queryKey, ok: false, note: '这个工作区不提供研究集合。' })
        return
      }
      try {
        const collection = await list({ q: search, ...(scope === 'current' && platform ? { exchange_id: platform } : {}), sort: sort === 'name' ? 'name' : 'updated' })
        if (active) {
          setStrategies(collection.items); setTotal(collection.total)
          setListRead({ api, key: queryKey, ok: true, note: '' })
          if (collection.items.some(item => ['accepted','running','stopping'].includes(item.latest_experiment?.job_status ?? ''))) timer = setTimeout(() => setListVersion(value => value + 1), OBSERVATION_INTERVAL_MS)
        }
      } catch (error) {
        if (active) setListRead({ api, key: queryKey, ok: false, note: explain(error) })
      }
    }
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [api, queryKey, search, scope, platform, sort, listVersion])

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
    const readData = async () => {
      try {
        if (!api.listDatasets) throw new Error('本客户端无法读取数据目录。')
        const items = await api.listDatasets()
        if (active) setDataRead({ api, check, count: items.length, invalid: items.filter(item => !item.valid).length, error: null })
      } catch (error) {
        if (active) setDataRead({ api, check, count: 0, invalid: 0, error: explain(error) })
      }
    }
    void readCatalog()
    void readModel()
    void readData()
    return () => { active = false }
  }, [api, check])

  const selectedInList = !userStrategyId && listRead?.api === api && listRead.key === queryKey && listRead.ok
    ? strategies.find(item => item.user_strategy_id === (listParams.get('selected') ?? lastSelectedId)) : undefined
  const requestedId = userStrategyId ?? selectedInList?.user_strategy_id
  useEffect(() => {
    if (!requestedId) return
    const read = api.getUserStrategy
    let active = true
    if (!read) {
      // Settled asynchronously for the same reason as the list above: this note is
      // never written by a read the workspace has already left.
      void Promise.resolve().then(() => {
        if (active) setDetailNote({ id: requestedId, text: '这个工作区不提供读取单条策略。' })
      })
      return () => { active = false }
    }
    read(requestedId)
      .then(value => {
        if (value.user_strategy_id !== requestedId) throw new Error('读取到的研究与当前选择不一致。')
        if (active) { setDetail({ id: value.user_strategy_id, value }); setDetailNote(null) }
      })
      // The failure names the strategy it was read for, so it is not shown over
      // another strategy's page.
      .catch(error => { if (active) setDetailNote({ id: requestedId, text: explain(error) }) })
    return () => { active = false }
  }, [api, requestedId])

  // The render is derived from the routed id, never from whatever was read last:
  // a strategy that is no longer the open one is never rendered as if it were, so
  // a slow answer or a stale callback cannot put another object's editable draft,
  // preview or approval under this route.
  const current = detail && detail.id === userStrategyId ? detail.value : null
  const selectInList = (id: string) => {
    try { sessionStorage.setItem('quine.research-selection', id) } catch { /* URL keeps the selection */ }
    setListParams(new URLSearchParams({ ...Object.fromEntries(listParams), selected: id }))
  }
  const assistantStrategy = detail && detail.id === requestedId ? detail.value : null
  const selectedModel = assistantStrategy?.model_profile_id
    ? model?.value?.profiles?.find(item => item.id === assistantStrategy.model_profile_id)
    : model?.value
  const modelSendBlock = !model ? '正在读取模型配置，暂不能发送。'
    : model.error ? '模型配置状态未知，暂不能发送。'
      : selectedModel?.configured !== true ? '模型尚未配置就绪，暂不能发送。' : null

  const preparationReading = !model || !catalog || !data
  const preparationFailed = Boolean(model?.error || catalog?.error || data?.error)
  // These are configuration facts, never a model connection or runnable verdict.
  const preparationComplete = !preparationReading && !preparationFailed
    && modelSendBlock === null && usableCount > 0 && data.count > data.invalid

  // Loading, ready and failed, read from the adapter this list state belongs to.
  const listSettled = listRead && listRead.api === api && listRead.key === queryKey ? listRead : null
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
    setListVersion(value => value + 1)
  }, [setListVersion])

  const created = (value: UserStrategy) => {
    setListVersion(value => value + 1)
    setCreating(false)
    // One create selects the strategy it just saved: its own conversation opens
    // immediately, with no second creation step.
    setDetail({ id: value.user_strategy_id, value })
    navigate(`/strategies/${encodeURIComponent(value.user_strategy_id)}`)
  }

  // The create flow is a disclosure, open by itself only while this workspace has
  // nothing to select at all — or when the header action was followed from a
  // selected strategy's own route, which opens the form here in that one click. It
  // is offered on the collection route only: a strategy that is open is not a place
  // to create another one above it.
  const wantsCreate = (location.state as { createStrategy?: boolean } | null)?.createStrategy === true
  const showCreate = !userStrategyId
    && (creating || wantsCreate)
  const actionNote = current ? notes[current.user_strategy_id] ?? '' : ''
  // A selected strategy that has neither answered nor failed is still being read;
  // that is a state to say out loud, not an empty selection and not an empty page.
  const detailFailed = Boolean(requestedId) && detailNote?.id === requestedId
  const detailReading = Boolean(requestedId) && assistantStrategy === null && !detailFailed
  return <ResearchSplitLayout key={userStrategyId ?? 'list'} storageKey={userStrategyId ?? 'list'}>
    <div className={`strategy-column ${userStrategyId ? 'research-detail-canvas' : 'research-list-canvas'}`}>
      {!userStrategyId && <header className="strategy-heading">
        <h1>研究工作台</h1>
        {listState === 'ready' && <span className="strategy-count">{`${total} 项研究`}</span>}
        {/* The control follows the routed id, not what has been read: a selected
            strategy whose read is still in flight or has failed is still a selected
            route, where the form is not offered. There the one click goes to the
            collection route and opens the form, rather than toggling a disclosure
            that this route can never show. */}
        {userStrategyId
          ? <Link className="create-cta" to="/strategies" state={{ createStrategy: true }}>
              <span aria-hidden="true">+</span>新建研究
            </Link>
          : <button type="button" className="create-cta" aria-expanded={showCreate}
              onClick={() => {
                // Following that request leaves it behind: this control still opens
                // and closes the form itself.
                if (wantsCreate) { navigate('/strategies', { replace: true, state: null }); return }
                setCreating(value => !value)
              }}>
              <span aria-hidden="true">+</span>新建研究
            </button>}
      </header>}
      {!userStrategyId && <>
        <p className="workbench-intro">从一个想法，到一份可复核的证据。</p>
        <ol className="research-journey" aria-label="研究流程">
          {([
            ['idea', '想法', '提出问题与假设'],
            ['draft', '策略草稿', '把思路变成规则'],
            ['experiment', '实验', '确认输入后运行'],
            ['evidence', '证据', '复核结果与局限'],
          ] as const).map(([icon, title, description]) => <li key={icon}><WorkbenchIcon name={icon} /><div><strong>{title}</strong><span>{description}</span></div></li>)}
        </ol>
      </>}
      {!userStrategyId && <div className="research-list-controls"><div className="research-scope-row"><div className="scope-switch" role="group" aria-label="研究范围"><button aria-pressed={scope === 'all'} onClick={() => filter({ scope: 'all' })}>全部研究</button><button disabled={!platform} aria-pressed={scope === 'current'} onClick={() => filter({ scope: 'current' })}>当前交易所</button></div><p className="muted">{scope === 'current' ? platform ? `${platformNames[platform]} · 按研究归属筛选` : '请先从侧栏选择交易所。' : '切换交易所仅改变筛选'}</p></div><div className="list-search"><input type="search" aria-label="搜索研究" placeholder="搜索研究名称、标的或备注" value={search} onChange={e => filter({ search: e.target.value })} /><select aria-label="研究排序" value={sort} onChange={e => filter({ sort: e.target.value })}><option value="updated">最近更新</option><option value="name">名称</option></select></div></div>}
      {!userStrategyId && (listState === 'reading'
        ? <p className="muted" role="status" aria-live="polite">正在读取研究…</p>
        : listState === 'ready' && <StrategyTable api={api} strategies={strategies} selectedId={selectedInList?.user_strategy_id} select={selectInList} changed={changed} filtered={scope === 'current' || !!search} />)}
      {listNote && <p role="alert" className="notice error">{listNote}<button onClick={() => setListVersion(value => value + 1)}>重新读取研究</button></p>}
      {!userStrategyId && <div className="research-history-entry">
        <Link to="/backtests#history-records">历史记录 <span aria-hidden="true">→</span></Link>
      </div>}

      {showCreate && <CreateStrategy api={api} created={created} value={createDraft} update={setCreateDraft} close={() => { setCreating(false); navigate("/strategies", { replace: true, state: null }) }} />}
      {detailNote && detailNote.id === requestedId &&
        <p role="alert" className="notice error">{detailNote.text}</p>}
      {detailReading && <p className="muted" role="status" aria-live="polite">正在读取研究…</p>}
      {current && <>
        {actionNote && <p role="alert" className="notice error">{actionNote}</p>}
        <StrategyDetail
          key={current.user_strategy_id}
          api={api} strategy={current} scenarios={scenarios} catalogReady={catalog !== null && catalog.error === null} changed={changed}
          failed={failedFor(current.user_strategy_id)} completedReads={completedReads[current.user_strategy_id] ?? {}} />
      </>}
      <details id="research-preparation" className="first-use-preparation" open={!preparationComplete || undefined}>
        <summary>研究准备 <span className="preparation-summary">{preparationReading ? '检查中' : preparationFailed ? '检查失败' : preparationComplete ? '配置已齐' : '待配置'}</span></summary>
        <section aria-label="研究准备状态" className="preparation-content">
          <div className="preparation-row">
            <span className="preparation-label">模型</span>
            <span>{!model ? '读取中…' : model.error ? '状态未知' : selectedModel?.configured ? '已配置 · 连接未验证' : '未配置'}</span>
            <Link to="/resources/models">{selectedModel?.configured && !model?.error ? '查看配置' : '配置模型'}</Link>
          </div>
          <div className="preparation-row">
            <span className="preparation-label">数据</span>
            <span>{!data ? '读取中…' : data.error ? '状态未知' : data.count ? `${data.count} 项已登记${data.invalid ? ` · ${data.invalid} 项需修正` : ''}` : '尚未导入'}</span>
            <Link to={current ? `/data?${new URLSearchParams({ research: current.user_strategy_id, revision: String(current.draft.revision), return: location.pathname + location.search })}#data-upload` : '/data#data-upload'}>导入 CSV 数据</Link>
          </div>
          {preparationFailed && <div className="preparation-error" role="alert">
            <span>{[model?.error && '模型配置读取失败', data?.error && '数据目录读取失败', catalog?.error && '模板目录读取失败'].filter(Boolean).join('；')}</span>
            <button type="button" disabled={preparationReading} onClick={() => setCheck(value => value + 1)}>重试检查</button>
          </div>}
          {catalog && !catalog.error && usableCount === 0 && <p className="preparation-warning">暂无可用模板，请查看准备说明。</p>}
          <details className="preparation-help">
            <summary>准备说明</summary>
            <p>未配置模型或数据也可保存想法。模型配置不代表连接成功，本页不会调用模型。</p>
            <p>先保存研究，再绑定支持模板与已发布 CSV。已登记数据不代表与当前模板兼容，能否运行以实验预览为准。</p>
            <p>{!catalog ? '正在读取模板目录…' : catalog.error ? catalog.error : usableCount ? `${usableCount} 个有效注册场景；可在绑定场景时选择。` : '支持模板暂不可用。可重新读取；仍不可用时请检查应用安装。无需手写 YAML。'}</p>
            {model?.error && <p>{model.error}</p>}
            {data?.error && <p>{data.error}</p>}
            {!preparationFailed && <button type="button" disabled={preparationReading} onClick={() => setCheck(value => value + 1)}>重新读取配置</button>}
          </details>
        </section>
      </details>
    </div>
    <aside className="strategy-ai" id="strategy-ai-column" tabIndex={-1}>
      {current && <a className="jump-link jump-to-details" href="#strategy-detail">返回策略详情</a>}
      {assistantStrategy
        ? <ChatPanel
            key={`${assistantStrategy.user_strategy_id}-${assistantStrategy.draft.revision}-${assistantStrategy.model_profile_id ?? ''}`}
            api={api} strategy={assistantStrategy} strategies={strategies} changed={changed} modelSendBlock={modelSendBlock} modelStatus={model?.value}
            completedReads={completedReads[assistantStrategy.user_strategy_id] ?? {}} completed={completed}
            />
        : detailReading
          ? <EmptyChat note="正在读取这条策略的对话…" live />
          : detailFailed
            ? <EmptyChat note="这条策略没有被读到，因此这里没有对话。" />
            : <EmptyChat />}
    </aside>
  </ResearchSplitLayout>
}

/** A report uses its recorded research conversation and pins the exact frozen job. */
export function ReportAssistant({ api, jobId, sources, changed }: { api: ApiAdapter; jobId: string; sources: ResearchReference[] | null; changed: (next: UserStrategy) => void }) {
  const [model, setModel] = useState<ModelConfigurationStatus | null>(null)
  const [reads, setReads] = useState<Record<string, string>>({})
  useEffect(() => {
    let active = true
    void api.settingsStatus?.().then(value => { if (active) setModel(value) }).catch(() => { /* unavailable is not ready */ })
    return () => { active = false }
  }, [api])
  const records = (sources ?? []).flatMap(item => item.experiments.filter(experiment => experiment.job_id === jobId).map(() => item))
  const owner = records.length === 1 ? records[0].user_strategy_id : null
  const [loaded, setLoaded] = useState<{api: ApiAdapter; sources: ResearchReference[] | null; id: string; value: UserStrategy | null; error?: string} | null>(null)
  useEffect(() => {
    let active = true
    if (owner) void (api.getUserStrategy ? api.getUserStrategy(owner) : Promise.reject(new Error('本客户端无法读取研究会话。')))
      .then(value => {
        if (value.user_strategy_id !== owner) throw new Error('读取到的研究与报告来源不一致。')
        if (active) setLoaded({api,sources,id:owner,value})
      }).catch(error => { if (active) setLoaded({api,sources,id:owner,value:null,error:explain(error)}) })
    return () => { active = false }
  }, [api, owner, sources])
  const observed = loaded?.id === owner && loaded.api === api && loaded.sources === sources ? loaded : null
  const strategy = observed?.value ?? null
  const selected = strategy?.model_profile_id ? model?.profiles?.find(profile => profile.id === strategy.model_profile_id) : model
  return strategy ? <ChatPanel key={`${strategy.user_strategy_id}.${strategy.draft.revision}.${strategy.model_profile_id ?? ''}`} api={api} strategy={strategy} strategies={sources ?? []} changed={value => { setLoaded({api,sources,id:value.user_strategy_id,value}); changed(value) }} modelStatus={model} modelSendBlock={selected?.configured ? null : '模型未配置或状态不可读，请前往模型连接。'} completedReads={reads} completed={(_id, job, turn) => setReads(value => ({ ...value, [job]: turn }))} fixedEvidenceJobId={jobId} />
    : <div className="preview-assistant"><h2>QUINE 研究助手</h2><p>上下文：当前冻结报告</p><p>{!sources ? '正在读取报告的研究来源…' : owner ? observed?.error ? `研究会话读取失败：${observed.error}` : '正在读取来源研究的会话…' : '这份报告没有唯一可核对的研究会话。可独立阅读报告事实。'}</p><Link to="/strategies">前往研究</Link></div>
}
