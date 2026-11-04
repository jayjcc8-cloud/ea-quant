import type { ApiAdapter, BacktestJob } from './app'
import type { TradeAnalytics } from './trade-analytics'
import type { EquityPath } from './equity-path'

export const jobParameters = (job: BacktestJob) => job.parameters ?? job.input_snapshot?.scenario.strategy.parameters ?? {}

export async function loadTradeAnalytics(api: ApiAdapter, job: BacktestJob): Promise<TradeAnalytics | null> {
  if (!api.getTradeAnalytics || job.status !== 'succeeded' || !job.report_ready) return null
  try {
    const value = await api.getTradeAnalytics(job.job_id)
    if (value.schema !== 'ea.trade-analytics.v1' || value.run_id !== job.engine_run_id || value.report_sha256 !== job.report_sha256) return null
    return value
  } catch { return null }
}


export async function loadPath(api: ApiAdapter, job: BacktestJob): Promise<EquityPath | null> {
  if (job.status !== 'succeeded' || !job.equity_path_sha256) return null
  try {
    const path = await api.getEquityPath(job.job_id)
    if (path.schema !== (job.schema === 'ea.local-web-job.v5' ? 'ea.backtest-equity-path.v3' : job.schema === 'ea.local-web-job.v4' ? 'ea.backtest-equity-path.v2' : 'ea.backtest-equity-path.v1') || path.run_id !== job.engine_run_id || path.report_sha256 !== job.report_sha256) return null
    return path
  } catch { return null }
}
