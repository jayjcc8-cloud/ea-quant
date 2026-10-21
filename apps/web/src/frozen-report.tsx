import type { BacktestJob, BacktestReport } from './app'
import type { EquityPath } from './equity-path'
import { pathPercent } from './equity-path'
import { TradeAnalyticsPanel, type TradeAnalytics } from './trade-analytics'

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function frozenSource(job: BacktestJob) {
  const version = job.dataset_version
  const source = object(version?.source)
  const policy = object(version?.availability_policy)
  const synthetic = source.provenance === 'builtin-synthetic-example' && source.declared_by === 'host-example' && source.asserted === false
  const attribution = source.asserted === true ? '用户声明' : '冻结记录'
  const text = (value: unknown) => typeof value === 'string' && value ? value : '未记录'
  return { version, source, policy, synthetic, attribution, text }
}

/** Presentation rounding only; exact decimal strings remain available below. */
function displayDecimal(value: string, places: number): string {
  const negative = value.startsWith('-')
  const [integer, fraction = ''] = value.replace(/^[+-]/, '').split('.')
  const scale = 10n ** BigInt(places)
  let rounded = BigInt(integer) * scale + BigInt(fraction.slice(0, places).padEnd(places, '0'))
  if (Number(fraction[places] ?? '0') >= 5) rounded += 1n
  const sign = negative ? '−' : ''
  if (rounded === 0n && /[1-9]/.test(value)) return `${sign}<0.${'0'.repeat(places - 1)}1`
  const digits = rounded.toString().padStart(places + 1, '0')
  return `${sign}${digits.slice(0, -places)}.${digits.slice(-places)}`
}

export function FrozenReportMetrics({ report, path }: { report: BacktestReport; path: EquityPath | null }) {
  const e = report.economics
  const money = (value: { amount: string; currency?: string } | undefined) => value
    ? `${displayDecimal(value.amount, 2)} ${value.currency ?? e.currency ?? '币种未提供'}` : '未提供'
  const percent = (value: string) => `${displayDecimal(pathPercent(value).slice(0, -1), 4)}%`
  return <section className="report-key-results" aria-label="正式报告关键结果"><dl>
    <div><dt>总收益</dt><dd>{percent(e.total_return.value)}</dd></div>
    <div><dt>净盈亏</dt><dd>{money(e.net_pnl)}</dd></div>
    <div><dt>最大回撤</dt><dd>{path ? percent(path.max_drawdown.ratio) : '未提供'}</dd>{path && <small>{money({ amount: path.max_drawdown.amount, currency: path.currency })}</small>}</div>
    <div><dt>期末权益</dt><dd>{money(e.equity)}</dd></div>
  </dl><div className="report-supporting-results"><span>初始资金 <strong>{money(e.initial_funding)}</strong></span><span>手续费 <strong>{money(e.fees)}</strong></span><span>完成回合数 <strong>{'completed_round_trips' in e ? e.completed_round_trips : '未提供'}</strong></span><span>订单 / 成交 <strong>{e.counts.orders} / {e.counts.fills}</strong></span></div></section>
}

export function FrozenReportFacts({ job, report, revision }: { job: BacktestJob; report: BacktestReport; revision?: number | null }) {
  const evidence = frozenSource(job)
  const input = job.input_snapshot?.scenario
  const window = input?.data
  const timeLabel = window ? window.start_utc.slice(0,10) === window.end_utc.slice(0,10) ? `${window.start_utc.slice(0,10)} · ${window.start_utc.slice(11,19)} – ${window.end_utc.slice(11,19)}` : `${window.start_utc.slice(0,19).replace('T',' ')} – ${window.end_utc.slice(0,19).replace('T',' ')}` : '未提供'
  const instrument = input?.instrument ?? report.source?.instrument ?? report.scenario?.instrument
  return <div className="report-source-line" aria-label="冻结报告来源">
    <strong>{instrument ? `${instrument.venue}:${instrument.symbol}` : '标的未提供'}</strong>
    <span>{revision == null ? '冻结修订未知' : `冻结 v${revision}`}</span>
    <span>{job.input_identity.record_count ?? '未提供'} 条 · {evidence.synthetic ? '合成示例 · 非真实市场数据' : '性质未声明'}</span>
    {evidence.version && <span>数据 {evidence.version.dataset_id} · v{evidence.version.version ?? '未知'}</span>}
    {typeof evidence.source.reference === 'string' && <span>来源：{evidence.source.reference}（{evidence.attribution}）</span>}
    <span title={window ? `${window.start_utc} → ${window.end_utc}` : undefined}>{timeLabel} UTC</span>
  </div>
}

export function FrozenReportDetails({ job, report, path }: { job: BacktestJob; report: BacktestReport; path: EquityPath | null }) {
  const input = job.input_snapshot?.scenario
  const evidence = frozenSource(job)
  const e = report.economics
  const money = (value: { amount: string; currency?: string } | undefined) => value ? `${value.amount} ${value.currency ?? e.currency ?? '币种未提供'}` : '未提供'
  return <details className="provenance report-raw-values"><summary>原始指标与冻结输入</summary>
    <p>历史冻结报告 · 一次确定性回测。{job.input_identity.record_count <= 6 ? '短样本仅验证流程。' : ''}展示金额保留两位、比例保留四位小数；下列原值未舍入。缺失值不按零处理。</p>
    <dl className="summary-list">
      <div><dt>期末权益 · 原值</dt><dd>{money(e.equity)}</dd></div>
      <div><dt>净盈亏 · 原值</dt><dd>{money(e.net_pnl)}</dd></div>
      <div><dt>总收益 · 原值</dt><dd>{pathPercent(e.total_return.value)} / {e.total_return.value}</dd></div>
      <div><dt>最大回撤 · 正式路径原值</dt><dd>{path ? `${path.max_drawdown.amount} ${path.currency} · ${pathPercent(path.max_drawdown.ratio)} / ${path.max_drawdown.ratio}` : '未提供'}</dd></div>
      <div><dt>手续费 · 原值</dt><dd>{money(e.fees)}</dd></div>
      <div><dt>订单 / 成交</dt><dd>{e.counts.orders} / {e.counts.fills}</dd></div>
      <div><dt>交易所归属 / 市场类型</dt><dd>{input?.instrument.venue ?? report.source?.instrument.venue ?? report.scenario?.instrument.venue ?? '未记录'} / 市场类型未记录</dd></div>
      <div><dt>初始资金</dt><dd>{input?.funding ? `${input.funding.initial_cash} ${input.funding.currency}` : money(e.initial_funding)}</dd></div>
      <div><dt>参数</dt><dd>{Object.entries(input?.strategy.parameters ?? job.parameters ?? {}).map(([key, value]) => `${key}: ${value}`).join(' · ') || '未提供'}</dd></div>
      <div><dt>执行成本、滑点与延迟</dt><dd>{input?.execution ? <pre>{JSON.stringify(input.execution, null, 2)}</pre> : '未提供'}</dd></div>
      <div><dt>数据版本</dt><dd>{evidence.version ? `${evidence.version.dataset_id} · v${evidence.version.version ?? '未知'}` : '注册场景内冻结数据；独立版本未记录'}</dd></div>
      <div><dt>样本性质</dt><dd>{evidence.synthetic ? '合成示例 · 非真实市场数据' : '性质未声明；不由名称或可用时间策略推断'}</dd></div>
      <div><dt>数据来源</dt><dd>{evidence.synthetic ? 'QUINE 内置合成示例 · 冻结记录' : `${evidence.text(evidence.source.reference)} · ${evidence.attribution}`}</dd></div>
      <div><dt>使用许可</dt><dd>{evidence.text(evidence.source.license_reference)} · {evidence.attribution}</dd></div>
      <div><dt>可用时间声明</dt><dd>{evidence.text(evidence.policy.id)} · {evidence.text(evidence.policy.evidence)} · {evidence.policy.pit_verified === true ? '冻结记录标记 PIT 已验证' : 'PIT 未验证'}</dd></div>
      <div><dt>冻结数据指纹</dt><dd>{evidence.version?.data_sha256 ?? job.input_identity.data_sha256 ?? '未记录'}</dd></div>
      <div><dt>证据范围</dt><dd>只读取本次运行冻结的版本与声明；来源和许可声明不等于法律核验或市场真实性核验。</dd></div>
    </dl>
  </details>
}

export function ReportTrades({ report, analytics, view }: { report: BacktestReport; analytics: TradeAnalytics | null; view: 'complete' | 'orders' }) {
  const money = report.economics.currency ?? report.economics.equity.currency ?? '币种未提供'
  const open = report.economics.ending_positions
  return <section className="report-trade-section">
    {view === 'complete' ? <><h3>完整交易</h3>
      {report.schema === 'ea.backtest-report.v3' ? <><p>已完成 {report.economics.completed_round_trips} 回合 · 已平仓交易的扣费后事实</p><div className="table-scroll"><table><thead><tr><th>回合</th><th>数量</th><th>进场金额（{money}）</th><th>出场金额（{money}）</th><th>进 / 出场费用</th><th>净盈亏（{money}）</th><th>持有秒数</th></tr></thead><tbody>{report.economics.trades.map((trade, i) => <tr key={i}><td>{i + 1}</td><td>{trade.quantity}</td><td>{trade.entry_settled_notional}</td><td>{trade.exit_settled_notional}</td><td>{trade.entry_fee} / {trade.exit_fee}</td><td>{trade.realized_pnl}</td><td>{analytics?.trades[i]?.holding_seconds ?? '未提供'}</td></tr>)}</tbody></table></div>{!report.economics.trades.length && <p>没有已完成的交易回合。</p>}</>
        : report.schema === 'ea.backtest-report.v2' ? <p>{report.economics.position_outcome === 'CLOSED' ? `已完成 1 回合 · 净盈亏 ${report.economics.realized_pnl.amount} ${money}` : '没有已完成的交易回合。'}</p>
          : <p>此旧版报告未提供完整交易口径；成交笔数不能替代完整交易数。</p>}
      <TradeAnalyticsPanel analytics={analytics} />
    </> : <><h3>订单与逐笔成交</h3><p>正式报告记录 {report.economics.counts.orders} 个订单 / {report.economics.counts.fills} 笔成交。</p>
      <div className="table-scroll"><table><thead><tr><th>方向 / 执行腿</th><th>成交数量</th><th>成交价（{money}）</th><th>成交时间</th><th>费用（{money}）</th></tr></thead><tbody>
        {report.schema === 'ea.backtest-report.v1' ? report.economics.execution.fill && <tr><td>{report.economics.execution.fill.side}</td><td>{report.economics.execution.fill.quantity}</td><td>{report.economics.execution.fill.price}</td><td>未提供</td><td>{report.economics.fees?.amount ?? '未提供'}</td></tr>
          : report.economics.execution_legs.map((leg, i) => <tr key={i}><td>{leg.role === 'entry' ? '进场' : '出场'} · {leg.outcome}</td><td>{leg.fill?.quantity ?? '未成交'}</td><td>{leg.fill?.price ?? '未提供'}</td><td>{leg.fill?.occurred_at ?? '未提供'}</td><td>{leg.fill?.fees.map(item => item.amount).join(' / ') || '未提供'}</td></tr>)}
      </tbody></table></div><p className="muted">历史报告不是活动委托列表；未提供委托细节时不推算状态。</p></>}
    <h3>期末未平仓</h3>{open.length ? <div className="table-scroll"><table><thead><tr><th>标的</th><th>数量</th><th>估值价格（{money}）</th></tr></thead><tbody>{open.map(position => <tr key={`${position.venue}:${position.symbol}`}><td>{position.venue}:{position.symbol}</td><td>{position.quantity}</td><td>{report.economics.valuation.price}</td></tr>)}</tbody></table></div> : <p>正式报告记录无期末持仓。</p>}
  </section>
}
