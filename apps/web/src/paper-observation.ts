import type { PaperInstance } from './paper-api'
import type { PaperHealth, PaperOverview } from './radian-api'

export const paperStateLabels: Record<string, string> = { starting: '正在启动', running: '运行中', stopping: '正在停止', stopped: '已停止', failed: '运行失败', unknown: '状态未知', stop_requested: '已请求停止 · 尚未确认', start_failed: '启动失败 · 未创建实例' }
export const observationTime = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未提供'
const text = (value: unknown) => value === undefined || value === null || value === '' ? '未提供' : String(value)
const flag = (value: unknown) => value === true ? '是' : value === false ? '否' : '未提供'
const list = (value?: string[]) => Array.isArray(value) && value.length ? value.join('；') : '未提供'
const nonTerminal = new Set(['starting', 'running', 'stopping', 'stop_requested'])
const reasons: Record<string, string> = {
  'readiness.recovery': '恢复依赖未就绪', 'readiness.reconciliation': '对账未就绪',
  'readiness.market_freshness': '行情未就绪', 'readiness.broker_health': '模拟执行依赖未就绪',
  'readiness.strategy_heartbeat': '策略心跳未就绪', 'readiness.storage': '存储未就绪',
  'trade_permission.no_market_reference': '没有可用行情参考，无法取得操作性许可',
}
export type ObservationReading = { text: string; tone: 'good' | 'blocked' | 'neutral' }
export type PaperObservation = {
  overview?: PaperOverview; health: PaperHealth | null; current: boolean
  kind: 'current' | 'history' | 'stale' | 'disconnected' | 'unknown'
  stateLabel: string; context: string; process: ObservationReading; readiness: ObservationReading; permission: ObservationReading
  observedAt: string; healthAt: string; statusAt: string
  reasons: string[]; diagnostics: { label: string; value: string }[]
}

/** Presentation only. Freshness, readiness and permission remain backend judgments. */
export function paperObservation(row: PaperInstance, disconnected: boolean): PaperObservation {
  const overview = row.overview.available === true && row.overview.run_id === row.run_id ? row.overview : undefined
  const slot = overview?.health
  const health = slot && !('available' in slot) && slot.schema === 'ea.paper-health.v1' && slot.run_id === row.run_id ? slot : null
  const historical = row.terminal_confirmed === true
  const current = !historical && !disconnected && nonTerminal.has(row.state) && overview?.lease_held === true
    && health?.process_alive === true && health.projection_stale === false && observationTime(health.observed_at) !== '未提供'
  const kind = historical ? 'history' : disconnected ? 'disconnected' : current ? 'current' : health?.projection_stale === true ? 'stale' : 'unknown'
  const reading = (value: unknown, yes: string, no: string): ObservationReading => {
    if (!current) return { text: historical ? '历史观测' : '未知', tone: 'neutral' }
    return value === true ? { text: yes, tone: 'good' } : value === false ? { text: no, tone: 'blocked' } : { text: '未知', tone: 'neutral' }
  }
  const context = {
    history: '终态已确认 · 以下保留原实例历史，不作为当前就绪或许可。',
    disconnected: '连接中断 · 保留上次快照，当前就绪与许可未知。',
    stale: '观测陈旧 · 当前就绪与许可未知，请刷新确认。',
    unknown: '观测未确认 · 缺少有效投影，当前就绪与许可未知。',
    current: '以下为读取时观测；运行中不代表依赖就绪或操作性许可未阻断。',
  }[kind]
  const reasonCodes = Array.isArray(health?.reason_codes) ? health.reason_codes : []
  // Only publish codes/reasons already supplied by the authority; do not invent causes.
  const blocking = current && (health?.runtime_ready === false || health?.trade_permitted === false)
  const reportedReasons = blocking ? reasonCodes.map(code => reasons[code] ? `${reasons[code]}（${code}）` : code) : []
  if (blocking && health?.trade_permitted === false && (health.trade_blocking_guard || health.trade_reason)) {
    reportedReasons.push(`首个许可阻断：${text(health.trade_blocking_guard)} · ${text(health.trade_reason)}`)
  }
  if (blocking && !reportedReasons.length) reportedReasons.push('后端未提供具体原因。')
  return {
    overview: overview ? { ...overview, health } : undefined, health, current, kind,
    stateLabel: disconnected && !historical ? '断连 · 状态未知' : paperStateLabels[row.state] ?? '状态未知', context,
    process: disconnected || typeof overview?.lease_held !== 'boolean' ? { text: '未知', tone: 'neutral' }
      : { text: overview.lease_held ? '存活 · 租约占用' : '未存活 · 租约释放', tone: 'neutral' },
    readiness: reading(health?.runtime_ready, '读取时就绪', '读取时未就绪'),
    permission: reading(health?.trade_permitted, '读取时未阻断', '读取时受阻'),
    observedAt: observationTime(row.observed_at), healthAt: observationTime(health?.observed_at), statusAt: observationTime(overview?.updated_at),
    reasons: reportedReasons,
    diagnostics: [
      { label: '投影范围', value: current ? '读取时观测' : historical ? '历史观测；就绪值包含读取端修正' : '上次投影；不代表当前状态' },
      { label: '投影说明', value: slot && 'available' in slot ? text(slot.detail) : slot && !health ? '健康投影身份不匹配或格式不支持，已隔离' : !health ? '健康投影未提供' : 'ea.paper-health.v1' },
      { label: '依赖就绪（投影值）', value: flag(health?.runtime_ready) },
      { label: '操作性许可（投影值）', value: flag(health?.trade_permitted) },
      { label: '策略心跳', value: text(health?.strategy_heartbeat_state) },
      { label: '行情状态', value: text(health?.market_state) },
      { label: '本地对账', value: text(health?.reconciliation_state) },
      { label: '恢复依赖', value: text(health?.recovery_state) },
      { label: '模拟执行依赖', value: text(health?.broker_state) },
      { label: '存储', value: text(health?.storage_state) },
      { label: '人工暂停', value: flag(health?.operator_halt) },
      { label: '原因代码', value: list(health?.reason_codes) },
      { label: '首个许可阻断项', value: text(health?.trade_blocking_guard) },
      { label: '许可原因', value: text(health?.trade_reason) },
      { label: '许可探测依据', value: text(health?.trade_permission_basis) },
      { label: '未覆盖的检查', value: list(health?.trade_permission_unexercised_guards) },
    ],
  }
}
