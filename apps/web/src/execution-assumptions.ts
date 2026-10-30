export type Commission = { policy: string; commission_bps: string }
export type Slippage = { policy: string; slippage_bps: string }
export type Latency = { policy: string; latency_ms: number }
export const latencyLabel = (latency?: Latency | null) => latency
  ? `${latency.latency_ms} ms execution latency`
  : 'Legacy latency omitted · effective 0 ms'
export const slippageLabel = (slippage?: Slippage | null) => slippage ? `${slippage.slippage_bps} bps adverse slippage` : '0 bps slippage'
export const commissionLabel = (commission?: Commission | null) => commission ? `${commission.policy} · ${commission.commission_bps} bps` : 'Legacy zero commission · 0 bps'

