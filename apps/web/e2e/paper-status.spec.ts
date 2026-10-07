/**
 * QUINE 运行 — the observation page against the real, already-running local Paper run.
 *
 * There is no `page.route` interception here and no second, independently fetched
 * expectation: every displayed value is compared against the exact
 * `/api/paper/overview` and `/api/paper/events` documents this page received, captured
 * with a response listener armed before the navigation. The run's own timestamps,
 * event count and age move with the run, so a separately fetched snapshot would only
 * prove the two reads disagree.
 *
 * The host only requests the log when the overview named a readable run, so the capture
 * treats an absent log document as "unavailable" instead of waiting for a request that
 * was never going to be made.
 */
import { expect, test, type Page, type Response } from '@playwright/test'

type HealthDoc = {
  run_id?: string
  observed_at?: string | null
  process_alive?: boolean
  runtime_ready?: boolean
  projection_stale?: boolean
  trade_permitted?: boolean
  trade_reason?: string | null
  trade_permission_basis?: string | null
  trade_blocking_guard?: string | null
  trade_permission_unexercised_guards?: string[]
  age_seconds?: number
  reason_codes?: string[]
}

type OverviewDoc = {
  available: boolean
  reason?: string | null
  run_id?: string
  state?: string
  detail?: string
  candidate_id?: string
  account_id?: string
  symbol?: string
  currency?: string
  cash?: string
  equity?: string
  position?: string
  orders?: number
  fills?: number
  observed_fills?: number
  reconciliation?: string
  incomplete?: boolean
  risk_halted?: boolean
  kill_switch_halted?: boolean
  started_at?: string
  updated_at?: string
  ended_at?: string
  terminal_durable?: boolean
  lease_held?: boolean
  health?: HealthDoc | { available: false; detail: string } | null
}

type EventsDoc = {
  available: boolean
  reason?: string
  run_id?: string
  events?: { event?: string; operation?: string; outcome?: string; run_id?: string }[]
  count?: number
  total_lines?: number
  truncated?: boolean
}

type Read = { overview: OverviewDoc; events: EventsDoc | null }

const NON_TERMINAL = ['starting', 'running', 'stopping']
const path = (url: string) => new URL(url).pathname

/** The display rules, restated once so the page is read back the way it renders. */
const text = (value: unknown) =>
  value === null || value === undefined || value === '' ? '未提供' : String(value)
const flag = (value: unknown) => value === true ? '是' : value === false ? '否' : '未提供'

/**
 * Collects the read documents for one read. Arm this *before* the navigation or the
 * refresh click, then await the returned function. The listener is removed once the
 * capture resolves, so nothing leaks into the next read.
 */
function captureRead(page: Page) {
  const overviewDocs: OverviewDoc[] = []
  const eventsDocs: EventsDoc[] = []
  const parsing: Promise<void>[] = []
  const collect = (response: Response) => {
    if (response.request().method() !== 'GET') return
    const pathname = path(response.url())
    if (pathname === '/api/paper/overview') {
      parsing.push(response.json().then(doc => { overviewDocs.push(doc as OverviewDoc) }, () => {}))
    } else if (pathname === '/api/paper/events') {
      parsing.push(response.json().then(doc => { eventsDocs.push(doc as EventsDoc) }, () => {}))
    }
  }
  page.on('response', collect)
  // Registered before the read is triggered, so the overview is always awaited; the
  // log may legitimately never be requested, so it is collected by the listener only.
  const overviewArrived = page.waitForResponse(
    response => response.request().method() === 'GET' && path(response.url()) === '/api/paper/overview',
  )

  return async (): Promise<Read> => {
    await overviewArrived
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(300)
    page.off('response', collect)
    await Promise.all([...parsing])

    const overview = overviewDocs.at(-1)
    if (!overview) throw new Error('the page read /api/paper/overview but its document was not captured')
    const events = eventsDocs.at(-1) ?? null
    if (overview.available && overview.run_id && !events) {
      throw new Error('the page read an available run but no /api/paper/events document was captured')
    }
    return { overview, events }
  }
}

function record(page: Page) {
  const requests: { method: string; url: string }[] = []
  const errors: string[] = []
  page.on('request', request => requests.push({ method: request.method(), url: request.url() }))
  page.on('pageerror', error => errors.push(error.message))
  return {
    methods: () => requests.map(entry => entry.method),
    paths: () => requests.map(entry => path(entry.url)),
    urls: () => requests.map(entry => entry.url),
    errors: () => [...errors],
  }
}

const horizontalOverflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)

/** A real projection, not the explicit unavailable marker. */
const projectionOf = (overview: OverviewDoc): HealthDoc | null => {
  const slot = overview.health
  if (!slot || typeof slot !== 'object' || (slot as { available?: unknown }).available === false) return null
  return slot as HealthDoc
}

test('the page shows the run its own /api documents describe, field for field', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1400, height: 1000 })
  const traffic = record(page)

  const mount = captureRead(page)
  await page.goto('/runs')
  const first = await mount()

  // This spec observes the supervised run that is currently readable; the sibling test
  // asserts the honest unavailable surface when there is none.
  test.skip(!first.overview.available, 'no supervised Paper run is currently readable')
  const runId = String(first.overview.run_id ?? '')
  expect(runId).not.toBe('')

  // Identity first: the run this page is about is the run the document named.
  await expect(page.locator('[data-paper="run_id"]')).toHaveText(runId)
  await expect(page.locator('[data-paper="state"]')).toHaveText(text(first.overview.state))
  await expect(page.locator('.paper-status h1')).toHaveText('运行')
  await expect(page.locator('.paper-snapshot-notice')).toContainText('不会自动更新')

  // An explicit refresh is one read, so its documents are unambiguously the ones on
  // screen — the economic facts are then compared value for value.
  const refresh = captureRead(page)
  await page.getByRole('button', { name: '刷新' }).click()
  const read = await refresh()
  expect(read.overview.run_id).toBe(runId)

  const facts = page.getByRole('region', { name: '运行经济事实' })
  for (const name of ['cash', 'equity', 'position', 'currency'] as const) {
    await expect(facts.locator(`[data-paper="${name}"]`)).toHaveText(text(read.overview[name]))
  }
  await expect(facts.locator('[data-paper="fills"]')).toHaveText(text(read.overview.fills))
  await expect(facts.locator('[data-paper="observed_fills"]')).toHaveText(text(read.overview.observed_fills))
  await expect(facts.locator('[data-paper="orders"]')).toHaveText(text(read.overview.orders))
  await expect(facts.locator('[data-paper="reconciliation"]')).toHaveText(text(read.overview.reconciliation))
  await expect(facts.locator('[data-paper="incomplete"]')).toHaveText(flag(read.overview.incomplete))
  await expect(facts.locator('[data-paper="risk_halted"]')).toHaveText(flag(read.overview.risk_halted))
  await expect(facts.locator('[data-paper="kill_switch_halted"]')).toHaveText(flag(read.overview.kill_switch_halted))

  const observation = page.getByRole('region', { name: '运行观测' })
  await expect(observation.locator('[data-paper="started_at"]')).toHaveText(text(read.overview.started_at))
  await expect(observation.locator('[data-paper="updated_at"]')).toHaveText(text(read.overview.updated_at))
  await expect(observation.locator('[data-paper="lease_held"]')).toHaveText(flag(read.overview.lease_held))
  // The page stamps its own read time, so no state is claimed timelessly.
  await expect(observation.locator('[data-paper="fetched_at"]')).toHaveText(/^\d{4}-\d{2}-\d{2}T/)

  // A projection naming another run is quarantined: it is never read as this run's.
  const projection = projectionOf(read.overview)
  const sameRun = projection !== null && projection.run_id === runId
  const healthRegion = page.getByRole('region', { name: '健康观测' })
  if (sameRun && projection) {
    await expect(healthRegion.locator('[data-paper="process_alive"]')).toHaveText(flag(projection.process_alive))
    await expect(healthRegion.locator('[data-paper="runtime_ready"]')).toHaveText(flag(projection.runtime_ready))
    await expect(healthRegion.locator('[data-paper="projection_stale"]')).toHaveText(flag(projection.projection_stale))
    await expect(healthRegion.locator('[data-paper="health_observed_at"]')).toHaveText(text(projection.observed_at))
    await expect(healthRegion.locator('[data-paper="health_age"]')).toHaveText(text(projection.age_seconds))
    if (projection.projection_stale === true) {
      await expect(healthRegion.locator('[data-paper="stale-warning"]')).toContainText(text(projection.age_seconds))
    }
  } else {
    await expect(healthRegion).toContainText(/未提供|不可用/)
  }

  // Permission is a snapshot: it is reported as allowed or denied only for a coherent
  // same-run, alive, explicitly fresh, non-terminal observation with a recorded
  // permission boolean. Its basis and limits are always shown as reported evidence.
  const permission = page.getByRole('region', { name: '交易许可' })
  const qualified = sameRun
    && read.overview.lease_held === true
    && projection?.process_alive === true
    && projection.projection_stale === false
    && typeof projection.trade_permitted === 'boolean'
    && NON_TERMINAL.includes(String(read.overview.state))
  await expect(permission.locator('[data-paper="trade_permitted"]')).toHaveText(flag(sameRun ? projection?.trade_permitted : undefined))
  await expect(permission.locator('[data-paper="trade_permission_basis"]')).toHaveText(text(sameRun ? projection?.trade_permission_basis : undefined))
  await expect(permission.locator('[data-paper="trade_blocking_guard"]')).toHaveText(text(sameRun ? projection?.trade_blocking_guard : undefined))
  await expect(permission.locator('[data-paper="permission-limits"]')).toHaveText(/\S/)
  await expect(permission.locator('[data-paper="permission-captured-at"]')).toHaveText(/^\d{4}-\d{2}-\d{2}T/)
  const chip = await permission.locator('[data-paper="permission"]').textContent()
  expect(chip?.trim()).toBe(
    !sameRun
      ? '不可用'
      : qualified
        ? (projection?.trade_permitted === true ? '读取时报告允许' : '读取时报告不允许')
        : '历史观测',
  )
  if (sameRun && projection?.trade_permission_basis === 'zero_effect_probe') {
    // A zero-effect probe is a narrow basis, never a blanket order approval.
    await expect(permission).toContainText('未执行的守卫')
    await expect(permission).toContainText(text(projection.trade_permission_unexercised_guards?.length
      ? projection.trade_permission_unexercised_guards.join(' · ')
      : undefined))
  }

  // The log is paired with the exact observed run, or it is not shown at all.
  const events = page.getByRole('region', { name: '运行事件' })
  const rows = events.locator('[data-paper-event]')
  const log = read.events
  const logged = log?.events ?? []
  if (log?.available && log.run_id === runId
    && logged.every(event => event.run_id === undefined || event.run_id === runId)) {
    await expect(events.locator('[data-paper="event-count"]')).toHaveText(text(log.count ?? logged.length))
    await expect(events.locator('[data-paper="event-total-lines"]')).toHaveText(text(log.total_lines))
    await expect(rows).toHaveCount(logged.length)
    expect(await events.locator('.paper-event-op').allTextContents()).toEqual(logged.map(event => text(event.operation)))
    expect(await events.locator('.paper-event-name').allTextContents()).toEqual(logged.map(event => text(event.event)))
    if (log.truncated === true) await expect(events.locator('[data-paper="events-truncated"]')).toBeVisible()
    // Every returned row stays reachable, but inside a bounded scroll region: a full
    // 100-row answer must not stretch the document.
    const logRegion = page.getByRole('region', { name: '操作日志' })
    await expect(logRegion).toBeVisible()
    const box = await logRegion.boundingBox()
    expect(box?.height ?? 0).toBeLessThanOrEqual(360)
  } else {
    // Unavailable log: the page states the reason, and shows no rows and no count —
    // an absent log is never rendered as an empty log of zero events.
    await expect(events).toContainText('不可用')
    await expect(rows).toHaveCount(0)
    await expect(events.locator('[data-paper="event-count"]')).toHaveCount(0)
    await expect(events.locator('[data-paper="event-total-lines"]')).toHaveCount(0)
    if (log && log.available === false && (log.run_id === undefined || log.run_id === runId)) {
      await expect(events.locator('[data-paper="events-reason"]')).toHaveText(text(log.reason))
    }
  }

  // The only control is the explicit refresh: no run id or path field, no control that
  // acts on the run, no Live surface.
  await expect(page.locator('.paper-status button')).toHaveCount(1)
  await expect(page.locator('.paper-status input, .paper-status select, .paper-status textarea')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /启动|停止|重置|下单|Live/ })).toHaveCount(0)

  // Model interaction lives in the strategy workspace, and this page says so.
  await expect(page.getByRole('link', { name: /AI/ })).toHaveAttribute('href', '/strategies')
  await expect(page.locator('.paper-status-model')).toContainText('不调用模型')

  // While observing, the page only reads: GET only, these two endpoints only.
  expect([...new Set(traffic.paths().filter(entry => entry.startsWith('/api/')))].sort())
    .toEqual(['/api/paper/events', '/api/paper/overview'])
  expect(traffic.methods().filter(method => method !== 'GET')).toEqual([])
  expect(traffic.urls().filter(url => /model|provider|openai|anthropic|completion/i.test(url))).toEqual([])
  expect(traffic.errors()).toEqual([])
  expect(await horizontalOverflow(page), 'the observation page must not scroll sideways').toBeLessThanOrEqual(1)

  await testInfo.attach('paper-overview.json', { body: JSON.stringify(read.overview, null, 2), contentType: 'application/json' })
  if (log) await testInfo.attach('paper-events.json', { body: JSON.stringify(log, null, 2), contentType: 'application/json' })
  await page.screenshot({ path: testInfo.outputPath('paper-status-1400.png'), fullPage: true })
})

test('with no readable run the page states the reason instead of inventing facts', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1400, height: 1000 })
  const traffic = record(page)
  const mount = captureRead(page)
  await page.goto('/runs')
  const read = await mount()

  // No real observed source is readable in this environment, so the observed-source
  // acceptance for this batch is pending. This test asserts the honest surface only;
  // it is not evidence that a real run was read.
  test.skip(read.overview.available, 'a supervised run is readable; its observation is asserted by the sibling test')

  await expect(page.locator('.paper-status h1')).toHaveText('运行')
  await expect(page.locator('.paper-unavailable code')).toHaveText(text(read.overview.reason))
  await expect(page.getByRole('region', { name: '运行经济事实' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: '运行标识' })).toHaveCount(0)
  // Without an observed run there is no run_id to pair a log with, so none is fetched.
  expect(traffic.paths()).not.toContain('/api/paper/events')
  expect(traffic.methods().filter(method => method !== 'GET')).toEqual([])
  expect(traffic.errors()).toEqual([])
  expect(await horizontalOverflow(page), 'the observation page must not scroll sideways').toBeLessThanOrEqual(1)

  await testInfo.attach('paper-overview-unavailable.json', { body: JSON.stringify(read.overview, null, 2), contentType: 'application/json' })
  await page.screenshot({ path: testInfo.outputPath('paper-status-unavailable-1400.png'), fullPage: true })
})

test('the observation page stays readable at 420 without scrolling sideways', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 420, height: 900 })
  const traffic = record(page)
  const mount = captureRead(page)
  await page.goto('/runs')
  const read = await mount()

  test.skip(!read.overview.available, 'no supervised Paper run is currently readable')
  await expect(page.locator('.paper-status h1')).toHaveText('运行')
  await expect(page.getByRole('region', { name: '运行标识' })).toBeVisible()
  await expect(page.getByRole('region', { name: '运行经济事实' })).toBeVisible()
  await expect(page.getByRole('region', { name: '运行事件' })).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新' })).toBeVisible()
  expect(await horizontalOverflow(page), 'the narrow observation page must not scroll sideways').toBeLessThanOrEqual(1)
  expect(traffic.errors()).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('paper-status-420.png'), fullPage: true })
})

test('运行 and 回测 are two independent entries, and the backtest entry only reads', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 1000 })
  const traffic = record(page)
  await page.goto('/runs')

  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('link', { name: '运行' })).toHaveAttribute('href', '/runs')
  await expect(nav.getByRole('link', { name: '回测' })).toHaveAttribute('href', '/backtests')

  // The backtest area is a separate destination; visiting it creates nothing.
  await nav.getByRole('link', { name: '回测' }).click()
  await expect(page).toHaveURL(/\/backtests$/)
  await expect(page.getByRole('heading', { name: 'Offline Backtests' })).toBeVisible()
  expect(traffic.methods().filter(method => method !== 'GET')).toEqual([])

  await nav.getByRole('link', { name: '运行' }).click()
  await expect(page).toHaveURL(/\/runs$/)
  await expect(page.locator('.paper-status h1')).toHaveText('运行')
  expect(traffic.methods().filter(method => method !== 'GET')).toEqual([])
  expect(traffic.errors()).toEqual([])
})
