import { exactDelta } from './decimal'

export type TradeAnalytics = {
  schema: 'ea.trade-analytics.v1'; run_id: string; report_sha256: string; currency: string
  closed_trades: number; wins: number; losses: number; breakeven: number
  win_rate: string | null; average_win: string | null; average_loss: string | null
  payoff_ratio: string | null; average_holding_seconds: string | null; has_open_position: boolean
  trades: { realized_pnl: string; holding_seconds: string }[]
}

export function TradeAnalyticsPanel({ analytics }: { analytics: TradeAnalytics | null }) {
  const shown = (value: string | null | undefined, unit = '') => value == null ? 'Unavailable' : `${value}${unit}`
  return <section className="panel" aria-label="Trade analytics">
    <header><h2>Trade analytics</h2><span>Closed trades · after fees</span></header>
    {!analytics ? <p>Complete-trade analytics unavailable for this report.</p> : <>
      <p className="muted">Open positions are excluded. Missing denominators are shown as unavailable.</p>
      <dl className="summary-list">
        <div><dt>Closed trades</dt><dd>{analytics.closed_trades}</dd></div>
        <div><dt>Wins / losses / breakeven</dt><dd>{analytics.wins} / {analytics.losses} / {analytics.breakeven}</dd></div>
        <div><dt>Win rate</dt><dd>{analytics.win_rate === null ? 'Unavailable' : `${exactDelta('0', analytics.win_rate, 2).replace(/^\+/, '')}%`}</dd></div>
        <div><dt>Average win</dt><dd>{shown(analytics.average_win, ` ${analytics.currency}`)}</dd></div>
        <div><dt>Average loss</dt><dd>{shown(analytics.average_loss, ` ${analytics.currency}`)}</dd></div>
        <div><dt>Payoff ratio</dt><dd>{shown(analytics.payoff_ratio)}</dd></div>
        <div><dt>Average holding time</dt><dd>{shown(analytics.average_holding_seconds, ' seconds')}</dd></div>
        <div><dt>Open position</dt><dd>{analytics.has_open_position ? 'Present · excluded from statistics' : 'None'}</dd></div>
      </dl>
      <div className="trade-history"><table><thead><tr><th scope="col">Trade</th><th scope="col">After-fee P&amp;L</th><th scope="col">Holding time (seconds)</th></tr></thead><tbody>
        {analytics.trades.map((trade, i) => <tr key={i}><th scope="row">{i + 1}</th><td>{trade.realized_pnl} {analytics.currency}</td><td>{trade.holding_seconds}</td></tr>)}
      </tbody></table></div>
    </>}
  </section>
}
