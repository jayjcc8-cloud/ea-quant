import { useId } from 'react'
import { compareCanonicalDecimal, exactDelta } from './decimal'

export type EquityPath = {
  schema: 'ea.backtest-equity-path.v1' | 'ea.backtest-equity-path.v2' | 'ea.backtest-equity-path.v3'; run_id: string; report_sha256: string; currency: string
  point_count: number; display_sampling: string
  display_points: { index: number; time: string; equity: string }[]
  max_drawdown: { amount: string; ratio: string; peak_index: number; peak_time: string; peak_equity: string; trough_index: number; trough_time: string; trough_equity: string }
}
export const pathPercent = (ratio: string) => `${exactDelta('0', ratio, 2).replace(/^\+/, '')}%`

/**
 * An ISO instant as a short UTC label, or the recorded string unchanged.
 *
 * The label is a bounded view of the same recorded value; the full instant stays
 * in the accessible description and in the drawdown facts below.
 */
function shortTime(value: string | undefined): string {
  if (!value) return 'Not recorded'
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(value)
  return match ? `${match[1]} ${match[2]}Z` : value
}

export function EquityCurve({ analysis }: { analysis: EquityPath }) {
  const points = analysis.display_points
  const low = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) < 0 ? a : b).equity
  const high = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) > 0 ? a : b).equity
  // Floating point is used only for SVG coordinates; all financial values come from Python.
  const span = Number(exactDelta(low, high))
  const coordinates = points.map(p => `${70 + 650 * p.index / Math.max(1, analysis.point_count - 1)},${span === 0 ? 120 : 210 - 180 * Number(exactDelta(low, p.equity)) / span}`).join(' ')
  const dd = analysis.max_drawdown
  const first = points[0]
  const last = points.at(-1)
  // The axis labels are HTML at the reading size rather than SVG text scaled by
  // the 750x260 viewBox, which is unreadable in a narrow column. The path, its
  // points and every extrema/drawdown fact are unchanged; the description below
  // carries the same context the axis labels did.
  const descriptionId = useId()
  const gradientId = useId()
  return <section className="panel equity-path"><header><h2>Equity Curve</h2><span>{analysis.currency}</span></header>
    <div className="curve-axis"><span>High {high} {analysis.currency}</span><span>Low {low} {analysis.currency}</span></div>
    <svg viewBox="0 0 750 260" role="img" aria-label="Equity curve" aria-describedby={descriptionId}>
      <title>Equity through admitted market observations</title>
      <path d="M70 25V215H725" fill="none" stroke="currentColor" opacity="0.3" />
      <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity=".18" /><stop offset="100%" stopColor="var(--accent)" stopOpacity=".02" /></linearGradient></defs>
      {[30, 75, 120, 165, 210].map(y => <line key={y} x1="70" x2="720" y1={y} y2={y} stroke="currentColor" opacity=".12" />)}
      <polygon points={`70,215 ${coordinates} 720,215`} fill={`url(#${gradientId})`} />
      <polyline points={coordinates} fill="none" stroke="var(--accent)" strokeWidth="2.5" />
    </svg>
    <div className="curve-axis"><span>First {shortTime(first?.time)}</span><span>Last {shortTime(last?.time)}</span></div>
    <details className="provenance"><summary>曲线数据与回撤明细</summary><p className="muted" id={descriptionId}>Equity in {analysis.currency} over the displayed observations: first {first?.time ?? 'not recorded'} at {first?.equity ?? 'not recorded'}, last {last?.time ?? 'not recorded'} at {last?.equity ?? 'not recorded'}, lowest {low}, highest {high}. {points.length < analysis.point_count ? `${points.length} sampled display points from ${analysis.point_count} observations.` : `${analysis.point_count} equity observations.`} Maximum drawdown uses every observation, including initial funding.</p>
    <h3>Maximum Drawdown</h3><dl className="summary-list">
      <div><dt>Amount</dt><dd>{dd.amount} {analysis.currency}</dd></div>
      <div><dt>Percentage / ratio</dt><dd>{pathPercent(dd.ratio)} / {dd.ratio}</dd></div>
      <div><dt>Peak → trough (UTC)</dt><dd>{dd.peak_time} → {dd.trough_time}</dd></div>
      <div><dt>Peak equity → trough equity</dt><dd>{dd.peak_equity} → {dd.trough_equity} {analysis.currency}</dd></div>
    </dl></details>
    <p className="curve-drawdown">最大回撤 {dd.amount} {analysis.currency} · {pathPercent(dd.ratio)}</p>
  </section>
}
