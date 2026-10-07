/**
 * QUINE 运行 — a read-only observation of the local supervised Paper run.
 *
 * The contract lives in `paper-status.test.tsx`. In short:
 * - One read on mount plus one per explicit 刷新 click. No polling, no run id or
 *   path field, no control that acts on the run, no Live surface.
 * - Every field is the value the read project published, or 未提供. A missing
 *   number is never shown as 0, and a recorded `false` is never shown as missing.
 * - Trade permission is reported as a snapshot: 读取时报告允许 / 读取时报告不允许 only
 *   for a coherent same-run, alive, explicitly fresh, non-terminal observation with a
 *   recorded permission boolean. Everything else is 历史观测 or 不可用. The reported
 *   reason, basis, blocking guard and its limits are shown as evidence, not authority.
 * - A projection naming another run is quarantined instead of being read as this run.
 * - The newest read owns the page across both fetch stages; older responses and
 *   responses arriving after unmount are discarded.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  radianApi,
  type HealthUnavailable,
  type PaperEvent,
  type PaperEvents,
  type PaperHealth,
  type PaperOverview,
} from './radian-api'
import './paper-status.css'

export type PaperStatusApi = {
  paperOverview(): Promise<PaperOverview>
  paperEvents(runId?: string, limit?: number): Promise<PaperEvents>
}

const MISSING = '未提供'
const UNAVAILABLE = '不可用'
/** States a run has not left yet; terminal and interrupted records are history. */
const NON_TERMINAL = new Set(['starting', 'running', 'stopping'])

const text = (value: unknown): string =>
  value === null || value === undefined || value === '' ? MISSING : String(value)

const flag = (value: unknown): string =>
  value === true ? '是' : value === false ? '否' : MISSING

/** Why the read projection has no run to publish. */
const UNAVAILABLE_REASONS: Record<string, string> = {
  runtime_root_not_configured: '没有配置受监督的本地 Paper 运行根目录，所以没有可读取的运行。',
  no_supervised_run_directories: '运行根目录下还没有任何受监督的运行目录。',
  run_state_unreadable: '这个运行目录存在，但它的状态无法被读取，因此不作为事实。',
  operational_log_missing: '这次运行没有可读取的操作日志。',
}

const isUnavailableMarker = (slot: PaperOverview['health']): slot is HealthUnavailable =>
  typeof slot === 'object' && slot !== null && (slot as { available?: unknown }).available === false

/** The health projection, or null when there is none to read. */
const healthProjection = (overview: PaperOverview): PaperHealth | null => {
  const slot = overview.health
  if (!slot || typeof slot !== 'object' || isUnavailableMarker(slot)) return null
  return slot
}

type Permission = { kind: 'current' | 'historical' | 'unavailable'; permitted?: boolean }

/**
 * A permission qualifies as an observation of this run only when the projection is
 * alive, explicitly fresh and about this run, the run has not reached a terminal
 * record, and the host actually recorded a permission boolean. An unrecorded
 * freshness or permission flag stays unknown: it never becomes a fresh permission.
 */
function permissionOf(overview: PaperOverview, projection: PaperHealth | null): Permission {
  if (!projection) return { kind: 'unavailable' }
  const live = overview.lease_held === true
    && projection.process_alive === true
    && projection.projection_stale === false
    && typeof projection.trade_permitted === 'boolean'
    && NON_TERMINAL.has(String(overview.state ?? ''))
  return live
    ? { kind: 'current', permitted: projection.trade_permitted === true }
    : { kind: 'historical', permitted: projection.trade_permitted }
}

const permissionLabel = (permission: Permission): string => {
  if (permission.kind === 'unavailable') return UNAVAILABLE
  // Even a qualified observation is a snapshot, so it is named as what was reported
  // when the page read, never as a state that is still true now.
  if (permission.kind === 'historical') return '历史观测'
  return permission.permitted ? '读取时报告允许' : '读取时报告不允许'
}

function Row({ name, label, children }: { name: string; label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd data-paper={name}>{children}</dd>
    </div>
  )
}

export function PaperStatus({ api = radianApi }: { readonly api?: PaperStatusApi }) {
  const [overview, setOverview] = useState<PaperOverview | null>(null)
  const [log, setLog] = useState<PaperEvents | null>(null)
  const [eventsFailure, setEventsFailure] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  // Only the newest read may write; a stale response from either fetch stage is dropped.
  const generation = useRef(0)

  const load = useCallback(async () => {
    const current = ++generation.current
    // The mount effect launches this read. Yielding once keeps every state write out
    // of the effect body — no synchronous write, no cascading render — without
    // changing what is read or how late responses are discarded.
    await Promise.resolve()
    if (current !== generation.current) return
    setReading(true)
    try {
      const read = await api.paperOverview()
      if (current !== generation.current) return
      setOverview(read)
      setLog(null)
      setEventsFailure(null)
      setFailure(null)
      setFetchedAt(new Date().toISOString())
      const runId = read.available ? read.run_id : undefined
      if (!runId) return
      try {
        const events = await api.paperEvents(runId, 100)
        if (current !== generation.current) return
        const rows = Array.isArray(events.events) ? events.events : []
        // An absent run_id is the host saying "no log", not a foreign log; a stated
        // run_id, or one foreign row, makes the whole document untrustworthy here.
        if ((events.run_id !== undefined && events.run_id !== runId)
          || rows.some(event => carriesAnotherRun(event, runId))) {
          setEventsFailure('返回的日志不属于这次运行，整份已丢弃。')
          return
        }
        // The published document is normalized to the rows this page actually read,
        // keeping an unavailable document's own reason readable.
        setLog({ ...events, events: rows })
      } catch (caught) {
        if (current !== generation.current) return
        setEventsFailure(caught instanceof Error ? caught.message : '操作日志读取失败。')
      }
    } catch (caught) {
      if (current !== generation.current) return
      // The latest read failed: the earlier success is demoted, not left standing.
      setOverview(null)
      setLog(null)
      setEventsFailure(null)
      setFetchedAt(null)
      setFailure(caught instanceof Error ? caught.message : '无法读取运行状态。')
    } finally {
      if (current === generation.current) setReading(false)
    }
  }, [api])

  useEffect(() => {
    let cancelled = false
    // The read is launched asynchronously, so the effect body itself writes no state.
    // Cleanup cancels a launch that has not started and drops any read still in flight.
    void Promise.resolve().then(() => { if (!cancelled) return load() })
    return () => { cancelled = true; generation.current += 1 }
  }, [load])

  const slot = overview ? healthProjection(overview) : null
  // A projection that names another run is quarantined: none of its readings are
  // shown as this run's facts, and it can never qualify a permission.
  const projection = slot && overview?.run_id && slot.run_id === overview.run_id ? slot : null
  const foreignProjection = Boolean(slot) && !projection
  const marker = overview && isUnavailableMarker(overview.health) ? overview.health : null
  const permission = overview ? permissionOf(overview, projection) : null
  const reason = overview && !overview.available ? String(overview.reason ?? '') : ''
  // The host omits `events` entirely when it has no log, so it is never assumed.
  const eventRows: PaperEvent[] = log?.events ?? []

  return (
    <div className="paper-status">
      <header className="paper-status-head">
        <div>
          <h1>运行</h1>
          <p className="muted">
            只读观测：本地生成行情，进程内 Paper 撮合。没有实盘，也没有启动、停止或重置控件。
          </p>
        </div>
        <button type="button" onClick={() => { void load() }} aria-busy={reading}>刷新</button>
      </header>

      <p className="paper-status-model">
        <Link to="/strategies">AI 协作在策略工作区</Link>
        <span>模型交互在策略工作区进行；本页只读观测，不调用模型，也不显示模型输出。</span>
      </p>

      <p data-paper="snapshot-notice" className="paper-snapshot-notice">
        本页只在打开时和手动刷新时各读取一次，不会自动更新；每个读数都是读取那一刻的观测，不是持续状态。
      </p>

      {failure ? <p role="alert" className="notice error">{failure}</p> : null}
      {!overview && !failure ? <p className="muted">正在读取这次运行…</p> : null}

      {overview && !overview.available ? (
        <section className="paper-panel paper-panel-wide">
          <p className="paper-unavailable">
            <span>{UNAVAILABLE_REASONS[reason] ?? '无法读取这次运行的状态。'}</span>{' '}
            <code>{text(reason)}</code>
          </p>
          {overview.run_id || overview.detail ? (
            <dl className="summary-list">
              {overview.run_id ? <Row name="unreadable_run" label="运行目录">{text(overview.run_id)}</Row> : null}
              {overview.detail ? <Row name="unreadable_detail" label="读取错误">{text(overview.detail)}</Row> : null}
            </dl>
          ) : null}
        </section>
      ) : null}

      {overview?.available ? (
        <div className="paper-panels">
          <section aria-label="运行标识" className="paper-panel">
            <h2>运行标识</h2>
            <dl className="summary-list">
              <Row name="run_id" label="运行标识">{text(overview.run_id)}</Row>
              <Row name="state" label="状态">{text(overview.state)}</Row>
              <Row name="reason" label="状态原因">{text(overview.reason)}</Row>
              <Row name="candidate" label="候选">{text(overview.candidate_id)}</Row>
              <Row name="account" label="账户">{text(overview.account_id)}</Row>
              <Row name="symbol" label="标的">{text(overview.symbol)}</Row>
            </dl>
          </section>

          <section aria-label="运行观测" className="paper-panel">
            <h2>运行观测</h2>
            <dl className="summary-list">
              <Row name="started_at" label="运行开始">{text(overview.started_at)}</Row>
              <Row name="ended_at" label="运行结束">{text(overview.ended_at)}</Row>
              <Row name="updated_at" label="运行更新">{text(overview.updated_at)}</Row>
              <Row name="lease_held" label="写入租约">{flag(overview.lease_held)}</Row>
              <Row name="terminal_durable" label="终态已落盘">{flag(overview.terminal_durable)}</Row>
              <Row name="fetched_at" label="本页读取时间">{text(fetchedAt)}</Row>
            </dl>
          </section>

          <section aria-label="运行经济事实" className="paper-panel">
            <h2>运行经济事实</h2>
            <dl className="summary-list">
              <Row name="cash" label="现金">{text(overview.cash)}</Row>
              <Row name="equity" label="权益">{text(overview.equity)}</Row>
              <Row name="position" label="持仓">{text(overview.position)}</Row>
              <Row name="currency" label="币种">{text(overview.currency)}</Row>
              <Row name="fills" label="记录成交">{text(overview.fills)}</Row>
              <Row name="observed_fills" label="观测成交">{text(overview.observed_fills)}</Row>
              <Row name="orders" label="委托">{text(overview.orders)}</Row>
              <Row name="reconciliation" label="对账">{text(overview.reconciliation)}</Row>
              <Row name="incomplete" label="未完成">{flag(overview.incomplete)}</Row>
              <Row name="risk_halted" label="风险熔断">{flag(overview.risk_halted)}</Row>
              <Row name="kill_switch_halted" label="总开关熔断">{flag(overview.kill_switch_halted)}</Row>
              <Row name="valuation_price" label="估值价">{text(overview.valuation_price)}</Row>
            </dl>
            <p className="muted">以上是这次运行自己记录的事实，本页不做任何推算。</p>
          </section>

          <section aria-label="健康观测" className="paper-panel">
            <h2>健康观测</h2>
            {projection ? (
              <>
                <dl className="summary-list">
                  <Row name="process_alive" label="进程存活">{flag(projection.process_alive)}</Row>
                  <Row name="runtime_ready" label="运行时就绪">{flag(projection.runtime_ready)}</Row>
                  <Row name="projection_stale" label="投影过期">{flag(projection.projection_stale)}</Row>
                  <Row name="health_observed_at" label="健康观测时间">{text(projection.observed_at)}</Row>
                  <Row name="health_age" label="健康观测年龄（秒）">{text(projection.age_seconds)}</Row>
                  <Row name="trade_permitted" label="运行记录的许可">{flag(projection.trade_permitted)}</Row>
                </dl>
                {projection.reason_codes?.length ? (
                  <p className="paper-reason-codes">原因码：{projection.reason_codes.join(' · ')}</p>
                ) : null}
                {projection.projection_stale === true ? (
                  <p data-paper="stale-warning" className="paper-stale">
                    健康投影已过期：观测于 {text(projection.observed_at)}，年龄 {text(projection.age_seconds)} 秒。
                    这些读数不是实时结论。
                  </p>
                ) : null}
              </>
            ) : foreignProjection ? (
              <p className="muted">
                未提供属于这次运行的健康投影：报告里的 run_id 与这次运行不一致，那些读数不作为这次运行的事实。
              </p>
            ) : (
              <p className="muted">
                {marker ? `健康投影不可用：${text(marker.detail)}` : '未提供这次运行的健康投影。'}
              </p>
            )}
          </section>

          <section aria-label="交易许可" className="paper-panel">
            <h2>交易许可</h2>
            <p className="paper-permission-head">
              <span
                data-paper="permission"
                className={`paper-chip paper-chip-${permission?.kind ?? 'unavailable'}`}
              >
                {permission ? permissionLabel(permission) : UNAVAILABLE}
              </span>
              <span className="muted">
                {permission?.kind === 'current'
                  ? '这是这次读取那一刻、同源、存活、显式未过期且未终态的观测。'
                  : permission?.kind === 'historical'
                    ? '运行已结束、观测已过期，或这次投影没有明确记录新鲜度与许可，只作为历史记录显示。'
                    : '没有属于这次运行的可读健康观测，许可不可读。'}
              </span>
            </p>
            <p className="muted">
              读取时间：
              <span data-paper="permission-captured-at">{text(fetchedAt)}</span>
              。许可不会随着时间自动重算，本页也不会轮询。
            </p>
            <dl className="summary-list">
              <Row name="trade_permitted" label="运行记录的许可">{flag(projection?.trade_permitted)}</Row>
              <Row name="trade_reason" label="许可原因">{text(projection?.trade_reason)}</Row>
              <Row name="trade_permission_basis" label="许可依据">{text(projection?.trade_permission_basis)}</Row>
              <Row name="trade_blocking_guard" label="阻塞守卫">{text(projection?.trade_blocking_guard)}</Row>
              <Row name="trade_permission_unexercised_guards" label="未执行的守卫">
                {projection?.trade_permission_unexercised_guards?.length
                  ? projection.trade_permission_unexercised_guards.join(' · ')
                  : MISSING}
              </Row>
            </dl>
            <p data-paper="permission-limits" className="muted">
              这是运行自己报告的许可证据，不是授权：许可依据只覆盖探针真正跑过的守卫，未执行的守卫不在其中；
              本页不重算经济健康，也不判断某笔委托会不会被接受。
            </p>
          </section>

          <section aria-label="运行事件" className="paper-panel paper-panel-wide">
            <h2>运行事件</h2>
            {!overview.run_id ? (
              <p className="notice">运行事件不可用：这次观测没有 run_id，无法把日志对上。</p>
            ) : eventsFailure ? (
              <p role="alert" className="notice error">运行事件不可用：{eventsFailure}</p>
            ) : !log ? (
              <p className="muted">正在读取这次运行的操作日志…</p>
            ) : log.available === false ? (
              // The host answered with a reason and no rows: that is unavailable, not a
              // log of zero events, so no count is shown at all.
              <p className="notice">
                <span>运行事件不可用：{UNAVAILABLE_REASONS[String(log.reason ?? '')] ?? '这次运行没有可读取的操作日志。'}</span>{' '}
                <code data-paper="events-reason">{text(log.reason)}</code>
              </p>
            ) : (
              <>
                <dl className="summary-list">
                  <Row name="event-count" label="事件条数">{text(log.count ?? eventRows.length)}</Row>
                  <Row name="event-total-lines" label="日志总行数">{text(log.total_lines)}</Row>
                </dl>
                {log.truncated === true ? (
                  <p data-paper="events-truncated" className="muted">
                    日志已按上限截断，只显示最近的 {text(log.count ?? eventRows.length)} 条。
                  </p>
                ) : null}
                {eventRows.length ? (
                  // The host returns up to the requested limit, so the list is bounded and
                  // scrollable inside the panel instead of stretching the page.
                  <div className="paper-event-scroll" role="region" aria-label="操作日志" tabIndex={0}>
                    <ol className="paper-event-list">
                      {eventRows.map((event, index) => (
                        <li key={index} data-paper-event>
                          <span className="paper-event-name">{text(event.event)}</span>
                          <span className="paper-event-op">{text(event.operation)}</span>
                          <span className="paper-event-outcome">{text(event.outcome)}</span>
                          <span className="paper-event-at">
                            {text(event.occurred_at ?? event.timestamp ?? event.at ?? event.sequence)}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : (
                  <p className="muted">这次运行还没有操作日志。</p>
                )}
              </>
            )}
          </section>
        </div>
      ) : null}
    </div>
  )
}

/** An event row is only usable when it either omits run_id or names this run. */
function carriesAnotherRun(event: PaperEvent, runId: string): boolean {
  return typeof event.run_id === 'string' && event.run_id !== runId
}
