/**
 * QUINE 模拟观测 page: the read-only contract of the observed local Paper run.
 *
 * Every document below is a hand-written fixture served by a scripted double.
 * Nothing here is an engine result, a model output or a provider call: the double
 * only proves the product rules — the page reads one overview and then that run's
 * own operational log, it never invents a zero or a false, it never shows a stale,
 * foreign or terminal observation as current permission, and it never polls.
 *
 * The `data-paper` anchors are the stable read targets. Values, states and reason
 * codes are asserted through them (or through short, distinctive phrases) instead
 * of through the surrounding Chinese prose, so a wording change cannot silently
 * weaken a claim about a number, a state or a permission.
 *
 * The anchors this file fixes, all inside `<section aria-label="...">` regions:
 *
 *   运行标识      run_id · candidate · account · symbol · state · reason
 *   运行观测      started_at · ended_at · updated_at · lease_held · fetched_at
 *   运行经济事实  cash · equity · position · currency · fills · observed_fills ·
 *                orders · reconciliation · incomplete · risk_halted ·
 *                kill_switch_halted
 *   健康观测      process_alive · runtime_ready · projection_stale ·
 *                health_observed_at · health_age · stale-warning
 *   交易许可      permission (读取时报告允许 / 读取时报告不允许 / 历史观测 / 不可用) ·
 *                permission-captured-at · trade_permitted · trade_reason ·
 *                trade_permission_basis · trade_blocking_guard · permission-limits
 *   运行事件      event-count · event-total-lines · events-truncated · events-reason ·
 *                one data-paper-event per rendered event row
 *
 * A value the host did not record renders as 未提供; a recorded boolean renders
 * as 是 or 否; the read is triggered by mount and by the 刷新 button only.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { PaperStatus, type PaperStatusApi } from './paper-status'
import type { HealthUnavailable, PaperEvents, PaperHealth, PaperOverview } from './radian-api'

const RUN = '3f1c8d2a-9b6e-4f0a-8c7d-1e2b3a4c5d6e'

/**
 * Keys the served document really carries. They are not on the published
 * TypeScript mirror yet, so they are added here rather than assumed away: the
 * page has to bind the field the host sends, not a fallback that happens to look
 * the same.
 */
type ObservedHealth = PaperHealth & { age_seconds?: number }
type ObservedOverview = PaperOverview & { observed_fills?: number }

function health(overrides: Partial<ObservedHealth> = {}): ObservedHealth {
  return {
    schema: 'ea.paper-health.v1',
    run_id: RUN,
    observed_at: '2026-10-06T02:00:00.000000Z',
    process_alive: true,
    runtime_state: 'running',
    runtime_ready: true,
    storage_state: 'available',
    broker_state: 'available',
    market_state: 'fresh',
    strategy_heartbeat_state: 'fresh',
    kill_switch_state: 'not_halted',
    reconciliation_state: 'matched',
    recovery_state: 'ready',
    projection_stale: false,
    operator_halt: false,
    trade_permitted: true,
    trade_reason: null,
    trade_permission_basis: 'zero_effect_probe',
    trade_blocking_guard: null,
    reason_codes: [],
    candidate_id: 'cand-1',
    age_seconds: 0.4,
    ...overrides,
  }
}

function overview(overrides: Partial<ObservedOverview> = {}): ObservedOverview {
  return {
    available: true,
    run_id: RUN,
    state: 'running',
    candidate_id: 'cand-1',
    account_id: 'paper-local',
    symbol: 'AAPL',
    currency: 'USD',
    cash: '986.8',
    equity: '1002.350',
    position: '0',
    valuation_price: '110',
    valuation_time: '2026-10-06T02:00:00.000000Z',
    orders: 5,
    fills: 5,
    observed_fills: 6,
    market_events: 12,
    fact_events: 6,
    ledger_sequence: 9,
    reconciliation: 'matched',
    risk_halted: false,
    kill_switch_halted: false,
    started_at: '2026-10-06T01:00:00.000000Z',
    updated_at: '2026-10-06T02:00:00.000000Z',
    terminal_durable: false,
    incomplete: false,
    lease_held: true,
    health: health(),
    ...overrides,
  }
}

function eventLog(overrides: Partial<PaperEvents> = {}): PaperEvents {
  return {
    available: true,
    run_id: RUN,
    events: [
      { event: 'fill_recorded', operation: 'submit_order', outcome: 'accepted', run_id: RUN, account_id: 'paper-local' },
      { event: 'ledger_updated', operation: 'settle_fill', outcome: 'recorded', run_id: RUN, account_id: 'paper-local' },
    ],
    count: 2,
    total_lines: 2,
    truncated: false,
    ...overrides,
  }
}

type Calls = { overview: number; events: [string | undefined, number | undefined][] }

function stubApi(script: {
  overview: () => Promise<PaperOverview>
  events: (runId?: string, limit?: number) => Promise<PaperEvents>
}): { api: PaperStatusApi; calls: Calls } {
  const calls: Calls = { overview: 0, events: [] }
  const api: PaperStatusApi = {
    paperOverview: () => { calls.overview += 1; return script.overview() },
    paperEvents: (runId, limit) => { calls.events.push([runId, limit]); return script.events(runId, limit) },
  }
  return { api, calls }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const show = (api: PaperStatusApi) => render(<MemoryRouter><PaperStatus api={api} /></MemoryRouter>)
const findRegion = (name: string) => screen.findByRole('region', { name })
/** The synchronous lookup: a missing region throws, so `waitFor` retries it. */
const region = (name: string) => screen.getByRole('region', { name })
const value = (root: HTMLElement, name: string) => root.querySelector(`[data-paper="${name}"]`)?.textContent ?? null
const eventsIn = (root: HTMLElement) => [...root.querySelectorAll('[data-paper-event]')].map(node => node.textContent ?? '')
const permissionChip = () => screen.getByRole('region', { name: '交易许可' }).querySelector('[data-paper="permission"]')?.textContent ?? null

afterEach(() => { cleanup() })

describe('QUINE 模拟观测 page', () => {
  it('names each unavailable reason distinctly and shows no run facts', async () => {
    const reasons = ['runtime_root_not_configured', 'no_supervised_run_directories', 'run_state_unreadable'] as const
    // The reason code and its explanation are one statement, so the sentence that
    // carries the code is the sentence a reader is given.
    const statements: { reason: string; text: string }[] = []
    for (const reason of reasons) {
      const { api, calls } = stubApi({
        overview: async () => ({ available: false, reason }),
        events: async () => { throw new Error('an unavailable overview must not fetch events') },
      })
      show(api)
      const node = (await screen.findAllByText(new RegExp(reason)))[0]
      const statement = node.closest('p, li, section, article, div') ?? node
      statements.push({ reason, text: statement.textContent ?? '' })
      // An unavailable observation is stated as such and never dressed as facts.
      expect(screen.queryByRole('region', { name: '运行经济事实' })).toBeNull()
      expect(screen.queryByRole('region', { name: '运行标识' })).toBeNull()
      // With no simulation configured, the page still points at the existing
      // research evidence routes instead of leaving the reader at a dead end.
      if (reason === 'runtime_root_not_configured') {
        expect(screen.getByRole('link', { name: '回测实验' }).getAttribute('href')).toBe('/backtests')
        expect(screen.getByRole('link', { name: '策略' }).getAttribute('href')).toBe('/strategies')
      }
      expect(calls.overview).toBe(1)
      expect(calls.events).toEqual([])
      cleanup()
    }
    const explanations = statements.map(({ text }) => reasons
      .reduce((rest, code) => rest.split(code).join(''), text)
      .trim())
    expect(explanations.every(text => text.length > 0)).toBe(true)
    expect(new Set(explanations).size).toBe(reasons.length)
  })

  it('keeps an unreadable run directory named without inventing facts', async () => {
    const { api } = stubApi({
      overview: async () => ({ available: false, reason: 'run_state_unreadable', run_id: 'run-7', detail: 'PaperSessionError' }),
      events: async () => { throw new Error('unused') },
    })
    show(api)
    await screen.findByText(/run_state_unreadable/)
    expect(screen.getByText(/run-7/)).toBeTruthy()
    expect(screen.getByText(/PaperSessionError/)).toBeTruthy()
    expect(screen.queryByRole('region', { name: '运行经济事实' })).toBeNull()
  })

  it('clears a previously successful observation when the latest overview request fails', async () => {
    const user = userEvent.setup()
    let fail = false
    const { api } = stubApi({
      overview: async () => {
        if (fail) throw new Error('paper_overview_unavailable')
        return overview()
      },
      events: async () => eventLog(),
    })
    show(api)
    await waitFor(() => expect(value(screen.getByRole('region', { name: '运行经济事实' }), 'cash')).toBe('986.8'))

    fail = true
    await user.click(screen.getByRole('button', { name: '刷新' }))

    expect((await screen.findByRole('alert')).textContent).toContain('paper_overview_unavailable')
    // The previous success is demoted: nothing of that run stays on screen as current.
    expect(screen.queryByText('986.8')).toBeNull()
    expect(screen.queryByRole('region', { name: '运行经济事实' })).toBeNull()
    expect(screen.queryByRole('region', { name: '运行标识' })).toBeNull()
    expect(screen.queryByText(RUN)).toBeNull()
    expect(screen.queryByText('submit_order')).toBeNull()
  })

  it('keeps an events failure explicit while the run observation stays readable', async () => {
    const { api } = stubApi({
      overview: async () => overview(),
      events: async () => { throw new Error('paper_events_unavailable') },
    })
    show(api)
    await waitFor(() => expect(value(region('运行经济事实'), 'cash')).toBe('986.8'))
    const events = await findRegion('运行事件')
    await waitFor(() => expect(within(events).getByRole('alert').textContent).toContain('paper_events_unavailable'))
    expect(eventsIn(events)).toHaveLength(0)
    expect(screen.getByRole('region', { name: '运行经济事实' })).toBeTruthy()
  })

  it('marks the correlated log unavailable when the observed run carries no run_id', async () => {
    const { api, calls } = stubApi({
      overview: async () => overview({ run_id: undefined }),
      events: async () => { throw new Error('events must not be fetched without an observed run') },
    })
    show(api)
    const events = await findRegion('运行事件')
    await waitFor(() => expect(events.textContent).toContain('不可用'))
    expect(calls.events).toEqual([])
    expect(eventsIn(events)).toHaveLength(0)
  })

  it('pairs the operational log with the exact observed run and reports the bounded count', async () => {
    const { api, calls } = stubApi({
      overview: async () => overview(),
      events: async () => eventLog({ count: 2, total_lines: 240, truncated: true }),
    })
    show(api)
    const events = await findRegion('运行事件')
    await waitFor(() => expect(eventsIn(events)).toHaveLength(2))
    expect(calls.events).toEqual([[RUN, 100]])
    expect(value(events, 'event-count')).toBe('2')
    expect(value(events, 'event-total-lines')).toBe('240')
    expect(events.querySelector('[data-paper="events-truncated"]')).toBeTruthy()
    const rows = eventsIn(events)
    expect(rows.some(text => text.includes('fill_recorded'))).toBe(true)
    expect(rows.some(text => text.includes('submit_order'))).toBe(true)
  })

  it('rejects an events document or a single event that belongs to another run', async () => {
    const foreign = stubApi({
      overview: async () => overview(),
      events: async () => eventLog({
        run_id: 'other-run',
        events: [{ event: 'ledger_updated', operation: 'foreign-operation', run_id: 'other-run' }],
        count: 1, total_lines: 1,
      }),
    })
    show(foreign.api)
    const mismatched = await findRegion('运行事件')
    await waitFor(() => expect(mismatched.textContent).toContain('不可用'))
    expect(eventsIn(mismatched)).toHaveLength(0)
    expect(screen.queryByText('foreign-operation')).toBeNull()
    cleanup()

    const mixed = stubApi({
      overview: async () => overview(),
      events: async () => eventLog({
        events: [
          { event: 'fill_recorded', operation: 'submit_order', run_id: RUN },
          { event: 'ledger_updated', operation: 'foreign-operation', run_id: 'other-run' },
        ],
        count: 2, total_lines: 2,
      }),
    })
    show(mixed.api)
    const contaminated = await findRegion('运行事件')
    await waitFor(() => expect(contaminated.textContent).toContain('不可用'))
    expect(eventsIn(contaminated)).toHaveLength(0)
    expect(screen.queryByText('foreign-operation')).toBeNull()
    expect(screen.queryByText('submit_order')).toBeNull()
  })

  it('reports an unavailable log by its reason instead of an empty log of zero events', async () => {
    // The real document for a run with no operational log carries the reason and no
    // `events` key at all, so nothing here may be read as "zero events".
    const { api } = stubApi({
      overview: async () => overview(),
      events: async () => ({ available: false, run_id: RUN, reason: 'operational_log_missing' }),
    })
    show(api)
    const events = await findRegion('运行事件')
    await waitFor(() => expect(value(events, 'events-reason')).toBe('operational_log_missing'))
    expect(events.textContent).toContain('不可用')
    expect(eventsIn(events)).toHaveLength(0)
    expect(events.querySelector('[data-paper="event-count"]')).toBeNull()
    expect(events.querySelector('[data-paper="event-total-lines"]')).toBeNull()
    expect(events.querySelector('[data-paper="events-truncated"]')).toBeNull()
    // No count, no zero: an unavailable log has no number to show.
    expect(events.textContent).not.toMatch(/\d/)
  })

  it('shows the observed run identity and every recorded timestamp it was read with', async () => {
    const { api } = stubApi({ overview: async () => overview(), events: async () => eventLog() })
    show(api)
    const identity = await findRegion('运行标识')
    await waitFor(() => expect(value(identity, 'run_id')).toBe(RUN))
    expect(value(identity, 'candidate')).toBe('cand-1')
    expect(value(identity, 'account')).toBe('paper-local')
    expect(value(identity, 'symbol')).toBe('AAPL')
    const observation = await findRegion('运行观测')
    expect(value(observation, 'started_at')).toBe('2026-10-06T01:00:00.000000Z')
    expect(value(observation, 'updated_at')).toBe('2026-10-06T02:00:00.000000Z')
    expect(value(observation, 'lease_held')).toBe('是')
    // Liveness and readiness are the projection's own observation, with its own
    // timestamp and age, not the moment this page was read.
    const healthObservation = await findRegion('健康观测')
    expect(value(healthObservation, 'health_observed_at')).toBe('2026-10-06T02:00:00.000000Z')
    expect(value(healthObservation, 'health_age')).toBe('0.4')
  })

  it('renders the observed economic fields exactly and never replaces a missing one with a zero', async () => {
    const present = stubApi({ overview: async () => overview(), events: async () => eventLog() })
    show(present.api)
    const facts = await findRegion('运行经济事实')
    await waitFor(() => expect(value(facts, 'cash')).toBe('986.8'))
    expect(value(facts, 'equity')).toBe('1002.350')
    expect(value(facts, 'position')).toBe('0')
    expect(value(facts, 'currency')).toBe('USD')
    expect(value(facts, 'fills')).toBe('5')
    expect(value(facts, 'observed_fills')).toBe('6')
    // No return, P&L or derived figure is added to the observed facts.
    expect(facts.textContent).not.toContain('P&L')
    expect(screen.queryByText(/回报率|收益率/)).toBeNull()
    cleanup()

    const missing = stubApi({
      overview: async () => overview({
        cash: undefined, equity: undefined, position: undefined, currency: undefined,
        fills: undefined, observed_fills: undefined, orders: undefined, reconciliation: undefined,
      }),
      events: async () => eventLog(),
    })
    show(missing.api)
    const empty = await findRegion('运行经济事实')
    await waitFor(() => expect(value(empty, 'cash')).toBe('未提供'))
    expect(value(empty, 'equity')).toBe('未提供')
    expect(value(empty, 'position')).toBe('未提供')
    expect(value(empty, 'currency')).toBe('未提供')
    expect(value(empty, 'fills')).toBe('未提供')
    expect(value(empty, 'observed_fills')).toBe('未提供')
    expect(value(empty, 'reconciliation')).toBe('未提供')
  })

  it('distinguishes a recorded false from an unrecorded boolean', async () => {
    const recorded = stubApi({
      overview: async () => overview({ incomplete: false, risk_halted: false, kill_switch_halted: false, reconciliation: 'matched' }),
      events: async () => eventLog(),
    })
    show(recorded.api)
    const facts = await findRegion('运行经济事实')
    await waitFor(() => expect(value(facts, 'risk_halted')).toBe('否'))
    expect(value(facts, 'incomplete')).toBe('否')
    expect(value(facts, 'kill_switch_halted')).toBe('否')
    expect(value(facts, 'reconciliation')).toBe('matched')
    cleanup()

    const unrecorded = stubApi({
      overview: async () => overview({ incomplete: undefined, risk_halted: undefined, kill_switch_halted: undefined }),
      events: async () => eventLog(),
    })
    show(unrecorded.api)
    const absent = await findRegion('运行经济事实')
    await waitFor(() => expect(value(absent, 'risk_halted')).toBe('未提供'))
    expect(value(absent, 'incomplete')).toBe('未提供')
    expect(value(absent, 'kill_switch_halted')).toBe('未提供')
  })

  it('separates a missing, an unavailable and a stale health projection', async () => {
    const missing = stubApi({ overview: async () => overview({ health: null }), events: async () => eventLog() })
    show(missing.api)
    const absent = await findRegion('健康观测')
    await waitFor(() => expect(absent.textContent).toContain('未提供'))
    await waitFor(() => expect(permissionChip()).toBe('不可用'))
    cleanup()

    const unavailableMarker: HealthUnavailable = { schema: 'ea.paper-health-unavailable.v1', available: false, detail: 'health projection could not be observed' }
    const unavailable = stubApi({ overview: async () => overview({ health: unavailableMarker }), events: async () => eventLog() })
    show(unavailable.api)
    const marker = await findRegion('健康观测')
    await waitFor(() => expect(marker.textContent).toContain('health projection could not be observed'))
    await waitFor(() => expect(permissionChip()).toBe('不可用'))
    cleanup()

    const stale = stubApi({
      overview: async () => overview({
        health: health({
          process_alive: false, runtime_ready: false, projection_stale: true,
          trade_permitted: true, age_seconds: 120.5,
          reason_codes: ['liveness.not_alive', 'projection.stale'],
        }),
      }),
      events: async () => eventLog(),
    })
    show(stale.api)
    const aged = await findRegion('健康观测')
    await waitFor(() => expect(value(aged, 'projection_stale')).toBe('是'))
    expect(aged.textContent).toContain('projection.stale')
    expect(aged.querySelector('[data-paper="stale-warning"]')?.textContent).toContain('120.5')
    // A stale projection is never a current permission, whatever it recorded.
    await waitFor(() => expect(permissionChip()).toBe('历史观测'))
    expect(screen.getByRole('region', { name: '交易许可' }).textContent).not.toContain('当前')
  })

  it('keeps an unrecorded freshness or permission flag unknown instead of qualifying it', async () => {
    const unknownFreshness = stubApi({
      overview: async () => overview({ health: health({ projection_stale: undefined }) }),
      events: async () => eventLog(),
    })
    show(unknownFreshness.api)
    await waitFor(() => expect(permissionChip()).toBe('历史观测'))
    expect(value(region('健康观测'), 'projection_stale')).toBe('未提供')
    cleanup()

    const unknownPermission = stubApi({
      overview: async () => overview({ health: health({ trade_permitted: undefined }) }),
      events: async () => eventLog(),
    })
    show(unknownPermission.api)
    await waitFor(() => expect(permissionChip()).toBe('历史观测'))
    const permission = region('交易许可')
    expect(value(permission, 'trade_permitted')).toBe('未提供')
    expect(permission.textContent).not.toContain('读取时报告')
  })

  it('quarantines a health projection that belongs to another run', async () => {
    const { api } = stubApi({
      overview: async () => overview({
        health: health({ run_id: 'other-run', process_alive: true, trade_permitted: true }),
      }),
      events: async () => eventLog(),
    })
    show(api)
    const healthRegion = await findRegion('健康观测')
    await waitFor(() => expect(healthRegion.textContent).toContain('未提供'))
    // Another run's liveness and permission never become this run's facts.
    expect(healthRegion.querySelector('[data-paper="process_alive"]')).toBeNull()
    expect(healthRegion.querySelector('[data-paper="trade_permitted"]')).toBeNull()
    await waitFor(() => expect(permissionChip()).toBe('不可用'))
  })

  it('reports trade permission as a snapshot only for a live, fresh, same-run, non-terminal observation', async () => {
    const live = stubApi({ overview: async () => overview(), events: async () => eventLog() })
    show(live.api)
    await waitFor(() => expect(permissionChip()).toBe('读取时报告允许'))
    const permitted = screen.getByRole('region', { name: '交易许可' })
    expect(value(permitted, 'trade_permitted')).toBe('是')
    expect(value(permitted, 'trade_permission_basis')).toBe('zero_effect_probe')
    expect(permitted.querySelector('[data-paper="permission-limits"]')?.textContent).toBeTruthy()
    // A qualified permission is still a snapshot, so it carries the time it was read.
    expect(value(permitted, 'permission-captured-at')).toMatch(/^\d{4}-\d{2}-\d{2}T/)
    cleanup()

    const denied = stubApi({
      overview: async () => overview({
        health: health({
          process_alive: true, runtime_ready: false, trade_permitted: false,
          trade_reason: 'daily loss limit reached', trade_blocking_guard: 'daily_loss',
        }),
      }),
      events: async () => eventLog(),
    })
    show(denied.api)
    await waitFor(() => expect(permissionChip()).toBe('读取时报告不允许'))
    const blocked = screen.getByRole('region', { name: '交易许可' })
    expect(value(blocked, 'trade_blocking_guard')).toBe('daily_loss')
    expect(value(blocked, 'trade_reason')).toBe('daily loss limit reached')
    // Liveness, readiness and permission are three separate observations.
    const observation = screen.getByRole('region', { name: '健康观测' })
    expect(value(observation, 'process_alive')).toBe('是')
    expect(value(observation, 'runtime_ready')).toBe('否')
    expect(value(blocked, 'trade_permitted')).toBe('否')
    cleanup()

    const foreign = stubApi({
      overview: async () => overview({ health: health({ run_id: 'other-run' }) }),
      events: async () => eventLog(),
    })
    show(foreign.api)
    await waitFor(() => expect(permissionChip()).toBe('不可用'))
    expect(screen.getByRole('region', { name: '交易许可' }).textContent).not.toContain('当前')
  })

  it('shows an interrupted or terminal run as an observation, never as a running badge', async () => {
    const interrupted = stubApi({
      overview: async () => overview({
        state: 'interrupted', reason: 'writer_lease_missing', lease_held: false,
        health: health({ process_alive: false, projection_stale: true, trade_permitted: true }),
      }),
      events: async () => eventLog(),
    })
    show(interrupted.api)
    const identity = await findRegion('运行标识')
    await waitFor(() => expect(value(identity, 'state')).toBe('interrupted'))
    expect(value(identity, 'reason')).toBe('writer_lease_missing')
    expect(value(await findRegion('运行观测'), 'lease_held')).toBe('否')
    await waitFor(() => expect(permissionChip()).toBe('历史观测'))
    cleanup()

    const stopped = stubApi({
      overview: async () => overview({
        state: 'stopped', ended_at: '2026-10-06T03:00:00.000000Z', terminal_durable: true, lease_held: false,
        health: health({ process_alive: false, runtime_ready: false, trade_permitted: false, trade_reason: 'run stopped' }),
      }),
      events: async () => eventLog(),
    })
    show(stopped.api)
    const finished = await findRegion('运行观测')
    await waitFor(() => expect(value(finished, 'ended_at')).toBe('2026-10-06T03:00:00.000000Z'))
    await waitFor(() => expect(value(region('运行标识'), 'state')).toBe('stopped'))
    await waitFor(() => expect(permissionChip()).toBe('历史观测'))
    // The read is timestamped, so the page never claims a timeless running state.
    expect(value(finished, 'fetched_at')).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })

  it('reads once on mount and again only on an explicit refresh', async () => {
    const user = userEvent.setup()
    const { api, calls } = stubApi({ overview: async () => overview(), events: async () => eventLog() })
    const view = show(api)
    await waitFor(() => expect(value(screen.getByRole('region', { name: '运行经济事实' }), 'cash')).toBe('986.8'))
    expect(calls.overview).toBe(1)
    expect(calls.events).toEqual([[RUN, 100]])
    // There is no run id or path field to type into, and no control that acts on the run.
    expect(view.container.querySelectorAll('input, select, textarea')).toHaveLength(0)
    // The page states out loud that it only reads on open and on manual refresh.
    expect(screen.getByText(/不会自动更新/)).toBeTruthy()

    await new Promise(resolve => setTimeout(resolve, 700))
    expect(calls.overview).toBe(1)
    expect(calls.events).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(calls.overview).toBe(2))
    await waitFor(() => expect(calls.events).toHaveLength(2))
    expect(calls.events[1]).toEqual([RUN, 100])
  })

  it('keeps only the newest read across both fetch stages', async () => {
    const user = userEvent.setup()
    const first = deferred<PaperOverview>()
    const second = deferred<PaperOverview>()
    const firstEvents = deferred<PaperEvents>()
    const secondEvents = deferred<PaperEvents>()
    const overviews = [first, second]
    let index = 0
    const api: PaperStatusApi = {
      paperOverview: () => overviews[index++].promise,
      paperEvents: runId => runId === 'run-new' ? secondEvents.promise : firstEvents.promise,
    }
    show(api)
    // A second read starts while the first one is still in flight.
    await user.click(screen.getByRole('button', { name: '刷新' }))

    second.resolve(overview({ run_id: 'run-new', cash: '222.2' }))
    secondEvents.resolve(eventLog({ run_id: 'run-new', events: [{ event: 'fill_recorded', operation: 'fresh-operation', run_id: 'run-new' }], count: 1, total_lines: 1 }))
    await waitFor(() => expect(value(screen.getByRole('region', { name: '运行经济事实' }), 'cash')).toBe('222.2'))

    first.resolve(overview({ run_id: 'run-old', cash: '111.1' }))
    firstEvents.resolve(eventLog({ run_id: 'run-old', events: [{ event: 'fill_recorded', operation: 'stale-operation', run_id: 'run-old' }], count: 1, total_lines: 1 }))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(value(screen.getByRole('region', { name: '运行经济事实' }), 'cash')).toBe('222.2')
    expect(screen.queryByText('111.1')).toBeNull()
    expect(screen.getByText('fresh-operation')).toBeTruthy()
    expect(screen.queryByText('stale-operation')).toBeNull()
  })

  it('discards a late events response from an older stage-two read', async () => {
    const user = userEvent.setup()
    const slow = deferred<PaperEvents>()
    const one = eventLog({ events: [{ event: 'fill_recorded', operation: 'operation-one', run_id: RUN }], count: 1, total_lines: 1 })
    const three = eventLog({ events: [{ event: 'fill_recorded', operation: 'operation-three', run_id: RUN }], count: 1, total_lines: 1 })
    const scripts: (PaperEvents | Promise<PaperEvents>)[] = [one, slow.promise, three]
    let call = 0
    const api: PaperStatusApi = {
      paperOverview: async () => overview(),
      paperEvents: async () => scripts[Math.min(call++, scripts.length - 1)],
    }
    show(api)
    await waitFor(() => expect(screen.getByText('operation-one')).toBeTruthy())
    await user.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(call).toBe(2))
    await user.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(screen.getByText('operation-three')).toBeTruthy())

    slow.resolve(eventLog({ events: [{ event: 'fill_recorded', operation: 'operation-two', run_id: RUN }], count: 1, total_lines: 1 }))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(screen.getByText('operation-three')).toBeTruthy()
    expect(screen.queryByText('operation-two')).toBeNull()
  })

  it('discards an in-flight read after unmount without a late write or an escaping rejection', async () => {
    // React 19 no longer warns about a state write on an unmounted component, so
    // the observable claim here is narrow on purpose: resolving both stages after
    // unmount neither throws nor logs. The discard guarantee itself is asserted by
    // the newest-read tests above.
    const pending = deferred<PaperOverview>()
    const { api, calls } = stubApi({ overview: () => pending.promise, events: async () => eventLog() })
    const errors: unknown[][] = []
    const original = console.error
    console.error = (...args: unknown[]) => { errors.push(args) }
    try {
      const view = show(api)
      // The read has to be genuinely in flight when the component goes away.
      await waitFor(() => expect(calls.overview).toBe(1))
      view.unmount()
      pending.resolve(overview())
      await pending.promise
      await new Promise(resolve => setTimeout(resolve, 0))
    } finally {
      console.error = original
    }
    expect(errors).toEqual([])
  })

  it('links to the strategy workspace for model work without calling a model', async () => {
    const { api, calls } = stubApi({ overview: async () => overview(), events: async () => eventLog() })
    show(api)
    await findRegion('运行经济事实')
    expect(screen.getByRole('link', { name: /AI/ }).getAttribute('href')).toBe('/strategies')
    // The page names the strategy workspace as where model work happens, and its
    // adapter exposes the two Paper reads only, so no model call can be made here.
    expect(await screen.findByText(/模型交互请进入策略工作区/)).toBeTruthy()
    expect(calls.overview).toBe(1)
    expect(calls.events).toHaveLength(1)
  })
})
