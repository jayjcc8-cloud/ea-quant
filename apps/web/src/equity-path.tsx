import { useId, useState } from 'react'
import { compareCanonicalDecimal, exactDelta, percent as pathPercent } from './decimal'

export type EquityPath = {
  schema: 'ea.backtest-equity-path.v1' | 'ea.backtest-equity-path.v2' | 'ea.backtest-equity-path.v3'; run_id: string; report_sha256: string; currency: string
  point_count: number; display_sampling: string
  display_points: { index: number; time: string; equity: string }[]
  max_drawdown: { amount: string; ratio: string; peak_index: number; peak_time: string; peak_equity: string; trough_index: number; trough_time: string; trough_equity: string }
}
export { pathPercent }

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
  const [range, setRange] = useState('all')
  const [cursor, setCursor] = useState(0)
  const descriptionId = useId()
  const gradientId = useId()
  const points = range === 'all' ? analysis.display_points : analysis.display_points.slice(-Math.max(2, Math.ceil(analysis.display_points.length / 2)))
  if (!points.length) return <section className="panel equity-path"><h2>权益曲线</h2><p>没有已记录的曲线点。</p></section>
  const selected = points[Math.min(cursor, points.length - 1)]
  const firstIndex = points[0].index
  const indexSpan = Math.max(1, points[points.length - 1].index - firstIndex)
  const low = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) < 0 ? a : b).equity
  const high = points.reduce((a, b) => compareCanonicalDecimal(a.equity, b.equity) > 0 ? a : b).equity
  // Floating point is used only for SVG coordinates; all financial values come from Python.
  const span = Number(exactDelta(low, high))
  const coordinates = points.map(p => `${70 + 650 * (p.index - firstIndex) / indexSpan},${span === 0 ? 120 : 210 - 180 * Number(exactDelta(low, p.equity)) / span}`).join(' ')
  const dd = analysis.max_drawdown
  const first = points[0]
  const last = points.at(-1)
  // The axis labels are HTML at the reading size rather than SVG text scaled by
  // the 750x260 viewBox, which is unreadable in a narrow column. The path, its
  // points and every extrema/drawdown fact are unchanged; the description below
  // carries the same context the axis labels did.
  return <section className="panel equity-path"><header className="curve-header"><h2>权益曲线</h2><small>{analysis.currency}</small><div className="curve-controls"><label>显示区间 <select aria-label="曲线显示区间" value={range} onChange={event => { setRange(event.target.value); setCursor(0) }}><option value="all">全部观测</option><option value="recent">后半区间</option></select></label><span className="muted">仅缩放显示，指标始终来自完整报告</span></div></header>
    <div className="curve-plot"><div className="curve-scale"><span>最高 {high} {analysis.currency}</span><span>最低 {low} {analysis.currency}</span></div>
    <svg viewBox="60 0 675 260" preserveAspectRatio="none" role="img" aria-label="Equity curve" aria-describedby={descriptionId}>
      <title>Equity through admitted market observations</title>
      <path d="M70 25V215H725" fill="none" stroke="currentColor" opacity="0.3" />
      <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity=".18" /><stop offset="100%" stopColor="var(--accent)" stopOpacity=".02" /></linearGradient></defs>
      {[30, 75, 120, 165, 210].map(y => <line key={y} x1="70" x2="720" y1={y} y2={y} stroke="currentColor" opacity=".12" />)}
      <polygon points={`70,215 ${coordinates} 720,215`} fill={`url(#${gradientId})`} />
      <polyline points={coordinates} fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <line x1={70 + 650 * (selected.index - firstIndex) / indexSpan} x2={70 + 650 * (selected.index - firstIndex) / indexSpan} y1="25" y2="215" stroke="currentColor" strokeDasharray="4 4" opacity=".45" />
    </svg></div>
    <div className="curve-axis"><span>{shortTime(first?.time)}</span><span>{shortTime(last?.time)}</span></div>
    <div className="curve-inspect"><label className="curve-cursor">观测时间轴 <input type="range" aria-label="曲线观测点" aria-valuetext={`${selected.time} · ${selected.equity} ${analysis.currency}`} min={0} max={points.length - 1} value={Math.min(cursor, points.length - 1)} onChange={event => setCursor(Number(event.target.value))} /></label>
    <output className="curve-readout" aria-live="polite" title={selected.time}>{shortTime(selected.time)} · {selected.equity} {analysis.currency} · 观测 #{selected.index}</output></div>
    <details className="provenance"><summary>曲线数据与回撤原值</summary><p className="muted" id={descriptionId}>Equity in {analysis.currency} over the displayed observations: first {first?.time ?? 'not recorded'} at {first?.equity ?? 'not recorded'}, last {last?.time ?? 'not recorded'} at {last?.equity ?? 'not recorded'}, lowest {low}, highest {high}. {points.length < analysis.point_count ? `${points.length} sampled display points from ${analysis.point_count} observations.` : `${analysis.point_count} equity observations.`} Maximum drawdown uses every observation, including initial funding.</p>
    <h3>最大回撤</h3><dl className="summary-list">
      <div><dt>金额</dt><dd>{dd.amount} {analysis.currency}</dd></div>
      <div><dt>比例 / 原值</dt><dd>{pathPercent(dd.ratio)} / {dd.ratio}</dd></div>
      <div><dt>峰值 → 谷值（UTC）</dt><dd>{dd.peak_time} → {dd.trough_time}</dd></div>
      <div><dt>峰值权益 → 谷值权益</dt><dd>{dd.peak_equity} → {dd.trough_equity} {analysis.currency}</dd></div>
    </dl></details>
  </section>
}
