import { useEffect, useRef, useState } from 'react'
import { Link as RouterLink, useParams } from 'react-router-dom'
import { SourceLink } from './workspace-navigation'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'
import type { TradeAnalytics } from './trade-analytics'
import type { EquityPath } from './equity-path'
import { exactDelta, percent } from './decimal'
import { jobParameters, loadPath, loadTradeAnalytics } from './report-readers'
import { commissionLabel, slippageLabel, latencyLabel } from './execution-assumptions'
import { experimentRecordsForJob, frozenRevisionText, type StrategyAssociationRecord } from './research-association'
import { PageHeader } from '../studio/src/layout'

function ComparisonTitle({ title, subtitle }: { title: string; subtitle: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div></header>
}

export type ComparedRun = { analytics?: TradeAnalytics | null; job: BacktestJob; report: BacktestReport | null; path?: EquityPath | null }


/** One row of the formal comparison. `key` is the stable internal identity the
 * arithmetic and the availability rules read; `label` is only what the reader
 * sees, so translating a heading can never change which rule runs. */
type CompareMetric = {
  key: string
  label: string
  before?: string
  after?: string
  shift: number
  suffix: string
  monetary: boolean
  leftCurrency?: string
  rightCurrency?: string
}

/** Trade analytics are the secondary evidence: an unavailable denominator is
 * named, never folded into the primary comparison as zero. */
const ANALYTICS_KEYS = ['closed_trades', 'wins', 'losses', 'breakeven', 'win_rate', 'average_win', 'average_loss', 'payoff_ratio', 'average_holding_seconds'] as const
const ANALYTICS_LABELS: Record<(typeof ANALYTICS_KEYS)[number], string> = {
  closed_trades: 'Closed trades', wins: 'Winning trades', losses: 'Losing trades', breakeven: 'Breakeven trades',
  win_rate: 'Win rate (ratio)', average_win: 'Average win', average_loss: 'Average loss', payoff_ratio: 'Payoff ratio',
  average_holding_seconds: 'Average holding time (seconds)',
}
const metricUnavailable = (key: string) =>
  key.startsWith('max_drawdown') || (ANALYTICS_KEYS as readonly string[]).includes(key)
function compareCell(row: CompareMetric, side: 'before' | 'after'): string {
  const value = side === 'before' ? row.before : row.after
  const currency = side === 'before' ? row.leftCurrency : row.rightCurrency
  if (value === undefined) return metricUnavailable(row.key) ? 'Unavailable' : 'No report'
  if (row.key === 'return') return percent(value)
  return `${value}${currency ? ` ${currency}` : ''}`
}
function compareDelta(row: CompareMetric): string {
  const { before, after } = row
  if (before === undefined || after === undefined) return '—'
  if (row.monetary && (!row.leftCurrency || !row.rightCurrency)) return 'Currency unavailable'
  if (row.monetary && row.leftCurrency !== row.rightCurrency) return 'Different currencies'
  return `${exactDelta(before, after, row.shift)}${row.suffix}${row.monetary ? ` ${row.leftCurrency}` : ''}`
}

/**
 * The comparison route. The instance is keyed on the exact route pair, the way the
 * report route is keyed on its job: naming another pair mounts a fresh comparison
 * instead of leaving the previous pair's runs, reports, owner or errors on screen
 * while the new one is still being read. Nothing of the old pair can be rendered
 * under the new pair's ids, and a late answer for the old pair lands in a
 * component that no longer exists.
 */
export function CompareBacktestsRoute({ api, studio = false }: { api: ApiAdapter; studio?: boolean }) {
  const { leftId = '', rightId = '' } = useParams()
  return <CompareBacktests key={`${leftId}:${rightId}`} api={api} leftId={leftId} rightId={rightId} studio={studio} />
}

function CompareBacktests({ api, leftId, rightId, studio }: { api: ApiAdapter; leftId: string; rightId: string; studio: boolean }) {
  const Link = studio ? RouterLink : SourceLink
  const reportPath = (id: string) => studio ? `/report/${encodeURIComponent(id)}/overview` : `/backtests/${encodeURIComponent(id)}`
  const PageTitle = studio ? PageHeader : ComparisonTitle
  const [runs, setRuns] = useState<[ComparedRun, ComparedRun] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Two identical route ids are not two distinct experiments, so this page never
  // presents them as a comparison and never claims a common owner for them. That is
  // derived from the route instead of stored by the effect, so the page states it in
  // the same render that decides to read nothing.
  const identicalPairError = leftId === rightId ? '同一个运行不能与它自己对比：请选择两个不同的已持久化运行。' : null

  useEffect(() => {
    let active = true
    // A pair the route does not really name is not read at all.
    if (leftId === rightId) return () => { active = false }
    const load = async (jobId: string): Promise<ComparedRun> => {
      const job = await api.getBacktest(jobId)
      // The route names the run this page may show. A host that answers with some
      // other job is an identity conflict, not a comparison.
      if (job.job_id !== jobId) throw new Error('Loaded run identity does not match the requested run')
      if (job.status !== 'succeeded' || !job.report_ready) return { job, report: null }
      let report: BacktestReport
      try {
        report = await api.getReport(jobId)
      } catch (caught) {
        if (typeof caught === 'object' && caught !== null && 'code' in caught && caught.code === 'report_unavailable') return { job, report: null }
        throw caught
      }
      if (!job.engine_run_id || report.run_id !== job.engine_run_id) {
        throw new Error('Verified report identity does not match this job')
      }
      return { job, report, path: await loadPath(api, job), analytics: await loadTradeAnalytics(api, job) }
    }
    Promise.all([load(leftId), load(rightId)])
      .then(([left, right]) => { if (active) setRuns([left, right]) })
      .catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : 'Comparison failed') })
    return () => { active = false }
  }, [api, leftId, rightId])

  // The way back is part of every state of this route, and each persisted run
  // keeps a direct link to its own formal report — visible, not behind a
  // disclosure — so the comparison is never a dead end.
  const toolbar = <div className="result-toolbar">
    <Link to={studio ? "/records" : "/backtests"}>{studio ? "回测记录" : "回测列表"}</Link>
    {runs && <span className="compare-report-links">
      <Link to={reportPath(runs[0].job.job_id)}>运行 A 正式报告</Link>
      <Link to={reportPath(runs[1].job.job_id)}>运行 B 正式报告</Link>
    </span>}
  </div>

  if (!runs) return <div className={studio ? "page compare-page" : "compare-page"}>
    <PageTitle title="回测对比" subtitle="读取两个已持久化运行及其正式报告的只读视图。" />
    {toolbar}
    {(error ?? identicalPairError) && <section className="notice error" role="alert">{error ?? identicalPairError}</section>}
  </div>
  const [left, right] = runs
  const leftInput = left.job.input_snapshot?.scenario
  const rightInput = right.job.input_snapshot?.scenario
  if (!leftInput || !rightInput) return <div className={studio ? "page compare-page" : "compare-page"}>
    <PageTitle title="回测对比" subtitle="两两对比需要已持久化的 v2 输入快照。" />
    {toolbar}
    <section className="notice error">输入快照不可用</section>
  </div>
  const inputRows = [
    ['Initial Cash', leftInput.funding.initial_cash, rightInput.funding.initial_cash],
    ['Action contract', String(leftInput.strategy.action_contract ?? 'V1'), String(rightInput.strategy.action_contract ?? 'V1')],
    ['Position lifecycle', String(leftInput.strategy.position_lifecycle ?? 'single-entry'), String(rightInput.strategy.position_lifecycle ?? 'single-entry')],
    ['Max round trips', String(leftInput.strategy.max_round_trips ?? '—'), String(rightInput.strategy.max_round_trips ?? '—')],
    ['Report version', left.report?.schema ?? '—', right.report?.schema ?? '—'],
    ['Symbol', leftInput.instrument.symbol, rightInput.instrument.symbol],
    ['Commission', commissionLabel(leftInput.execution?.commission), commissionLabel(rightInput.execution?.commission)],
    ['Slippage', slippageLabel(leftInput.execution?.slippage), slippageLabel(rightInput.execution?.slippage)],
    ['Execution latency', latencyLabel(leftInput.execution?.latency), latencyLabel(rightInput.execution?.latency)],
  ]
  const sameImplementation = (leftInput.strategy.source?.kind ?? 'builtin') === (rightInput.strategy.source?.kind ?? 'builtin') && leftInput.strategy.source?.artifact_sha256 === rightInput.strategy.source?.artifact_sha256
  const sameStrategy = sameImplementation && leftInput.strategy.id === rightInput.strategy.id && (leftInput.strategy.version ?? 1) === (rightInput.strategy.version ?? 1)
  const parameterRows = sameStrategy
    ? (left.job.strategy_descriptor?.parameters.map(p => p.name) ?? Object.keys(jobParameters(left.job)))
        .filter((name, index, all) => all.indexOf(name) === index)
        .map(name => ({ label: name, before: String(jobParameters(left.job)[name]), after: String(jobParameters(right.job)[name]) }))
        // Changed parameters lead: the reader came for what moved, and an
        // unchanged row is context that follows it.
        .sort((a, b) => Number(a.before === a.after) - Number(b.before === b.after))
    : []
  // The four figures a comparison is read for lead; every other recorded number
  // stays in its own native disclosure below them, so none is dropped and none
  // competes with these.
  const primaryMetricRows: CompareMetric[] = [
    { key: 'equity', label: '期末权益', before: left.report?.economics.equity.amount, after: right.report?.economics.equity.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.equity.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.equity.currency ?? right.report?.economics.currency },
    { key: 'net_pnl', label: '净盈亏', before: left.report?.economics.net_pnl.amount, after: right.report?.economics.net_pnl.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.net_pnl.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.net_pnl.currency ?? right.report?.economics.currency },
    { key: 'fees', label: '手续费', before: left.report?.economics.fees?.amount, after: right.report?.economics.fees?.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.report?.economics.fees?.currency ?? left.report?.economics.currency, rightCurrency: right.report?.economics.fees?.currency ?? right.report?.economics.currency },
    { key: 'return', label: '收益率', before: left.report?.economics.total_return.value, after: right.report?.economics.total_return.value, shift: 2, suffix: ' pp', monetary: false },
  ]
  const secondaryMetricRows: CompareMetric[] = [
    { key: 'max_drawdown_amount', label: '最大回撤金额', before: left.path?.max_drawdown.amount, after: right.path?.max_drawdown.amount, shift: 0, suffix: '', monetary: true, leftCurrency: left.path?.currency, rightCurrency: right.path?.currency },
    { key: 'max_drawdown_ratio', label: '最大回撤比率', before: left.path?.max_drawdown.ratio, after: right.path?.max_drawdown.ratio, shift: 0, suffix: '', monetary: false },
    { key: 'orders', label: '订单数', before: left.report ? String(left.report.economics.counts.orders) : undefined, after: right.report ? String(right.report.economics.counts.orders) : undefined, shift: 0, suffix: '', monetary: false },
    { key: 'fills', label: '成交数', before: left.report ? String(left.report.economics.counts.fills) : undefined, after: right.report ? String(right.report.economics.counts.fills) : undefined, shift: 0, suffix: '', monetary: false },
  ]
  // Trade analytics are the secondary evidence: a metric the verified report does
  // not carry is named unavailable and stays in its own disclosure, never folded
  // into the primary comparison as a zero.
  const analyticsRows: CompareMetric[] = ANALYTICS_KEYS.map(key => {
    // An analytics value this workspace never served stays absent — the cell says
    // so — instead of becoming a zero that would read as a measured result.
    const before = left.analytics ? left.analytics[key] : null
    const after = right.analytics ? right.analytics[key] : null
    const monetary = key === 'average_win' || key === 'average_loss'
    return {
      key, label: ANALYTICS_LABELS[key],
      before: before == null ? undefined : String(before),
      after: after == null ? undefined : String(after),
      shift: 0, suffix: '', monetary,
      leftCurrency: monetary ? left.analytics?.currency : undefined,
      rightCurrency: monetary ? right.analytics?.currency : undefined,
    }
  })
  return <div className={studio ? "page compare-page" : "compare-page"}>
    <PageTitle title="回测对比" subtitle="基于两个已持久化运行及其正式报告的只读对比。" />
    {toolbar}
    {/* The association sits directly under the toolbar, so which saved strategy
        these two runs belong to is read before the differences, not after them.
        The whole comparison is keyed on the route pair by CompareBacktestsRoute,
        which is what keeps another pair's owner off this page. */}
    <StrategyComparisonAssociation api={api} leftJobId={left.job.job_id} rightJobId={right.job.job_id} studio={studio} />
    <section className="panel comparison-panel" aria-label="参数差异">
      <header><h2>参数差异</h2><span>已持久化的规范化策略输入</span></header>
      {!sameImplementation ? <p>Strategy implementation changed</p> : !sameStrategy && <p>NO_PARAMETER_DELTA · Different strategy ID/version</p>}
      <table><thead><tr><th>参数</th><th>运行 A</th><th>运行 B</th><th>差异</th></tr></thead><tbody>{parameterRows.map(row => <tr className={row.before === row.after ? '' : 'changed'} key={row.label}><th>{row.label}</th><td>{row.before}</td><td>{row.after}</td><td>{exactDelta(row.before, row.after)}</td></tr>)}</tbody></table>
    </section>
    <section className="panel comparison-panel" aria-label="结果对比">
      <header><h2>结果对比</h2><span>已核验的正式报告与路径证据</span></header>
      {/* What the reader has to know before reading a single number: how small each
          window is, which way the delta runs, and what the P&L does not mean. The
          digests themselves stay in the technical disclosure below. */}
      <div className="compare-limits">
        <dl className="summary-list">
          {[left, right].map((run, index) => <div key={`count-${run.job.job_id}`}><dt>运行 {index === 0 ? 'A' : 'B'} 记录数</dt><dd>{run.job.input_identity.record_count} 条记录{run.job.input_identity.record_count <= 6 ? ' · 小样本：仅用于流程验证，不作稳健性结论' : ''}</dd></div>)}
          <div><dt>差异方向</dt><dd>运行 B − 运行 A</dd></div>
        </dl>
        <p className="muted">差异仅作描述：数据、成本与运行条件都可能不同，本页不排序、不推荐。单一窗口不足以确立稳健性，正式报告是每个数字的唯一权威。</p>
        <p className="muted">净盈亏包含窗口结束时仍持有头寸的估值，不是已实现利润。</p>
      </div>
      <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{primaryMetricRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      <details className="disclosure"><summary>其他结果指标</summary>
        <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{secondaryMetricRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      </details>
      <details className="disclosure"><summary>分析指标明细</summary>
        <table><thead><tr><th>指标</th><th>运行 A</th><th>运行 B</th><th>Δ</th></tr></thead><tbody>{analyticsRows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{compareCell(row, 'before')}</td><td>{compareCell(row, 'after')}</td><td>{compareDelta(row)}</td></tr>)}</tbody></table>
      </details>
    </section>
    <details className="disclosure"><summary>规范化输入</summary>
      <table><thead><tr><th>参数</th><th>运行 A</th><th>运行 B</th><th>差异</th></tr></thead><tbody>{inputRows.map(([label, before, after]) => <tr className={before === after ? '' : 'changed'} key={label}><th>{label}</th><td>{before}</td><td>{after}</td><td>{before === after ? 'Unchanged' : 'Changed'}</td></tr>)}</tbody></table>
    </details>
    <details className="disclosure"><summary>运行身份与技术细节</summary>
      <div className="compare-run-detail">
        {[left, right].map((run, index) => <section className="panel" key={run.job.job_id}><header><h2>运行 {index === 0 ? 'A' : 'B'}</h2><span className={`status status-${run.job.status}`}>{run.job.status}</span></header>
          <dl className="summary-list">
            <div><dt>Web job</dt><dd>{run.job.job_id}</dd></div>
            <div><dt>Engine run</dt><dd>{run.job.engine_run_id ?? 'not available'}</dd></div>
            <div><dt>Input SHA-256</dt><dd>{run.job.input_sha256 ?? 'not available'}</dd></div>
            <div><dt>Scenario SHA-256</dt><dd>{run.job.input_identity.scenario_sha256}</dd></div>
            <div><dt>Data SHA-256</dt><dd>{run.job.input_identity.data_sha256}</dd></div>
            <div><dt>Outcome</dt><dd>{run.report ? '正式报告' : run.job.error_code ?? 'No report'}</dd></div>
          </dl>
          <div className="downloads"><Link to={reportPath(run.job.job_id)}>打开正式报告</Link><a href={api.artifactUrl(run.job.job_id, 'report.json')}>Download report.json</a><a href={api.artifactUrl(run.job.job_id, 'summary.txt')}>Download summary.txt</a></div>
        </section>)}
      </div>
    </details>
  </div>
}

/**
 * The read-only association of a two-run comparison with one saved strategy.
 *
 * A comparison has one honest answer only when each of the two distinct route
 * jobs names exactly one frozen record, and both records belong to the same
 * saved strategy. Anything else — two different owners, no record for a run, a
 * run frozen twice, a workspace with no strategy list, a refused read — is named
 * as such and falls back to the strategy list; this panel never guesses a common
 * owner and never prints a revision it could not read. Sharing a registered
 * engine strategy is not sharing an owner, so nothing here is matched by
 * strategy id, name or the current draft.
 *
 * The read is generation-guarded and the caller keys this component on the route
 * pair, so a late answer for one pair can never name an owner on another. A
 * failed or missing association read stays local: the formal comparison above it
 * keeps rendering.
 */
function StrategyComparisonAssociation({ api, leftJobId, rightJobId, studio }: { api: ApiAdapter; leftJobId: string; rightJobId: string; studio: boolean }) {
  const Link = studio ? RouterLink : SourceLink
  type Pair =
    | { kind: 'reading' }
    | { kind: 'none'; note: string }
    | { kind: 'owner'; record: StrategyAssociationRecord; leftRevision: number | null; rightRevision: number | null }
  const [pair, setPair] = useState<Pair>({ kind: 'reading' })
  const generation = useRef(0)

  useEffect(() => {
    const read = ++generation.current
    const list = api.researchReferences
    if (!list) {
      void Promise.resolve().then(() => {
        if (generation.current === read) setPair({ kind: 'none', note: '这个工作区不提供已保存的用户策略。' })
      })
      return () => { generation.current += 1 }
    }
    const settled = (next: Pair) => { if (generation.current === read) setPair(next) }
    list().then(strategies => {
      const left = experimentRecordsForJob(strategies, leftJobId)
      const right = experimentRecordsForJob(strategies, rightJobId)
      if (left.length > 1 || right.length > 1) {
        settled({ kind: 'none', note: '其中一个运行匹配到多条冻结记录，无法确定唯一来源，这里不替你选择，也不显示冻结修订。' })
      } else if (left.length === 0 || right.length === 0) {
        settled({ kind: 'none', note: '没有同一个已保存用户策略同时冻结了这两个运行。' })
      } else if (left[0].userStrategyId !== right[0].userStrategyId) {
        settled({ kind: 'none', note: '这两个运行分别由不同的已保存用户策略冻结，没有共同来源策略。' })
      } else {
        settled({ kind: 'owner', record: left[0], leftRevision: left[0].revision, rightRevision: right[0].revision })
      }
    }).catch(error => {
      settled({
        kind: 'none',
        note: `${error instanceof Error ? error.message : '策略关联读取失败。'} · 这次关联读取失败了，上面的正式对比不受影响。`,
      })
    })
    return () => { generation.current += 1 }
  }, [api, leftJobId, rightJobId])

  return <section className="panel association-panel" aria-label="对比策略关联">
    <header><h2>对比策略关联</h2><span>只读 · 不启动任何运行</span></header>
    {pair.kind === 'reading' && <p className="muted">正在读取冻结了这两个运行的已保存策略……</p>}
    {pair.kind === 'none' && <>
      <p className="muted">{pair.note}</p>
      <p><Link to={studio ? "/" : "/strategies"}>{studio ? "打开研究列表" : "打开策略列表"}</Link></p>
    </>}
    {pair.kind === 'owner' && <>
      <p><strong>{pair.record.name}</strong></p>
      <p><span>{`运行 A · ${frozenRevisionText(pair.leftRevision)}`}</span> · <span>{`运行 B · ${frozenRevisionText(pair.rightRevision)}`}</span></p>
      {/* The way back names both runs, in the order this comparison shows them, so
          the strategy reopens on A then B instead of resetting the evidence. */}
      <p><Link to={studio ? `/research/${encodeURIComponent(pair.record.userStrategyId)}/experiments` : `/strategies/${encodeURIComponent(pair.record.userStrategyId)}?evidence_a=${encodeURIComponent(leftJobId)}&evidence_b=${encodeURIComponent(rightJobId)}`}>返回此策略继续研究</Link></p>
    </>}
  </section>
}
