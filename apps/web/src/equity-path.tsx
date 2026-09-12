import { compareCanonicalDecimal, exactDelta } from './decimal'

export type EquityPath = {
  schema: 'ea.backtest-equity-path.v1' | 'ea.backtest-equity-path.v2'; run_id: string; report_sha256: string; currency: string
  point_count: number; display_sampling: string
  display_points: { index: number; time: string; equity: string }[]
  max_drawdown: { amount: string; ratio: string; peak_index: number; peak_time: string; peak_equity: string; trough_index: number; trough_time: string; trough_equity: string }
}
export const pathPercent = (ratio: string) => `${exactDelta('0', ratio, 2).replace(/^\+/, '')}%`

export function EquityCurve({ analysis }: { analysis: EquityPath }) {
  const points = analysis.display_points
  const low = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) < 0 ? a : b).equity
  const high = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) > 0 ? a : b).equity
  // Floating point is used only for SVG coordinates; all financial values come from Python.
  const span = Number(exactDelta(low, high))
  const coordinates = points.map(p => `${70 + 650 * p.index / Math.max(1, analysis.point_count - 1)},${span === 0 ? 120 : 210 - 180 * Number(exactDelta(low, p.equity)) / span}`).join(' ')
  const dd = analysis.max_drawdown
  return <section className="panel equity-path"><header><h2>Equity Curve</h2><span>{analysis.currency}</span></header>
    <svg viewBox="0 0 750 260" role="img" aria-label="Equity curve">
      <title>Equity through admitted market observations</title>
      <path d="M70 25V215H725" fill="none" stroke="currentColor" opacity="0.3" />
      <polyline points={coordinates} fill="none" stroke="#4f8cff" strokeWidth="2" />
      <text x="5" y="30" fontSize="11" fill="currentColor">{high}</text>
      <text x="5" y="215" fontSize="11" fill="currentColor">{low}</text>
      <text x="70" y="245" fontSize="11" fill="currentColor">{points[0]?.time}</text>
      <text x="720" y="245" textAnchor="end" fontSize="11" fill="currentColor">{points.at(-1)?.time}</text>
    </svg>
    <p className="muted">{points.length < analysis.point_count ? `${points.length} sampled display points from ${analysis.point_count} observations.` : `${analysis.point_count} equity observations.`} Maximum drawdown uses every observation, including initial funding.</p>
    <h3>Maximum Drawdown</h3><dl className="summary-list">
      <div><dt>Amount</dt><dd>{dd.amount} {analysis.currency}</dd></div>
      <div><dt>Percentage / ratio</dt><dd>{pathPercent(dd.ratio)} / {dd.ratio}</dd></div>
      <div><dt>Peak → trough (UTC)</dt><dd>{dd.peak_time} → {dd.trough_time}</dd></div>
      <div><dt>Peak equity → trough equity</dt><dd>{dd.peak_equity} → {dd.trough_equity} {analysis.currency}</dd></div>
    </dl>
  </section>
}
