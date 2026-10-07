/**
 * QUINE strategy workspace: bounded browser acceptance of the visual states.
 *
 *   EA_WEB_BASE_URL=http://127.0.0.1:8917 \
 *   npx playwright test e2e/quine-visual.spec.ts
 *
 * Everything this spec reads from `/api/user-strategies**` is a scripted network
 * fixture written in this file — a hand-written document, not an engine result, not
 * a model output and not a provider call. No provider is contacted and no model is
 * configured. The fixture is used only to put the workspace into the AI proposal,
 * conflict and error states this pass has to look right in; the run state and the
 * evidence refusal it shows are read from a fixture job that is deliberately NOT a
 * formal engine report, and the page must say so rather than print a figure.
 *
 * The real browser → API → engine → report path is accepted in
 * e2e/user-strategies.spec.ts against a running workspace; this file accepts the
 * visual and interaction states, at 1400x1000 and at 420x900.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

const CRITERION = {
  schema: 'radian.report-criterion.v1',
  field: 'economics.net_pnl.amount',
  expectation: 'positive',
}

const FIELDS = { target_quantity: '2', entry_delay_bars: 0 }

/** Strategy A: a frozen run and one open, reviewable change proposed against it. */
const FIRST = {
  schema: 'ea.user-strategy.v1',
  user_strategy_id: 'fixture-us-1',
  created_at: '2026-10-06T10:00:00.000000Z',
  updated_at: '2026-10-06T10:02:00.000000Z',
  name: '点差假设收紧',
  intent: '检验一次延迟入场是否仍能覆盖自身的成本',
  draft: {
    revision: 4, strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL',
    scenario_id: 'bounded-long.yaml', dataset_id: null, source_sha256: null,
    initial_cash: '10000', parameters: { target_quantity: '3', entry_delay_bars: 0 }, criterion: CRITERION,
  },
  history: [],
  messages: [],
  calls: [],
  proposals: [{
    proposal_id: 'fixture-prop-1',
    call_id: 'fixture-call-1',
    base_revision: 4,
    status: 'open',
    created_at: '2026-10-06T10:02:00.000000Z',
    items: [
      { path: 'parameters.target_quantity', before: '3', after: '2', reason: '更小的仓位让这段样本的成本更可读。', evidence: ['fixture: 成本敏感性'], proposer: 'model' },
      { path: 'parameters.entry_delay_bars', before: 0, after: 1, reason: '跳过第一根 bar。', evidence: [], proposer: 'model' },
    ],
    applied_paths: [],
    stale_paths: [],
  }],
  changes: [{
    operation_id: 'fixture-op-1', kind: 'apply', proposal_id: 'fixture-prop-0', target_operation_id: null,
    from_revision: 3, to_revision: 4, applied_paths: ['parameters.target_quantity'], stale_paths: [],
    items: [], evidence: [], proposer: 'model', at: '2026-10-06T10:01:00.000000Z',
  }],
  experiments: [{
    launch_intent_id: 'li-1', request_id: 'fixture-request-1', payload_sha256: 'f'.repeat(64),
    payload: { draft_revision: 3, scenario_id: 'bounded-long.yaml', parameters: FIELDS },
    task_id: 'fixture-task-1', job_id: 'fixture-job-1', status: 'created',
    created_at: '2026-10-06T10:01:30.000000Z',
  }],
}

/** Strategy B: the same registered strategy, a separate object with its own draft. */
const SECOND = {
  ...FIRST,
  user_strategy_id: 'fixture-us-2',
  name: '黄金突破观察',
  intent: '同一个想法，换一个标的重新开始',
  draft: { ...FIRST.draft, revision: 1, symbol: 'XAUUSD', parameters: FIELDS },
  proposals: [],
  changes: [],
  experiments: [],
}

/** A fixture job that is deliberately not a formal engine report. */
const FIXTURE_JOB = {
  schema: 'ea.local-web-job.v5', job_id: 'fixture-job-1', request_id: 'fixture-request-1',
  scenario_id: 'bounded-long.yaml',
  input_identity: { scenario_sha256: 'b'.repeat(64), data_sha256: 'a'.repeat(64), record_count: 4 },
  status: 'running', engine_run_id: null, report_sha256: null, summary_sha256: null,
  error_code: null, message: null, report_ready: false,
}

/** The one scripted workspace: every response below is fixture data. */
async function scriptWorkspace(page: Page, strategies: unknown[]) {
  const documents: Record<string, unknown> = Object.fromEntries(
    (strategies as { user_strategy_id: string }[]).map(item => [item.user_strategy_id, item]),
  )
  await page.route('**/api/user-strategies**', async (route: Route) => {
    const url = new URL(route.request().url())
    const rest = url.pathname.replace('/api/user-strategies', '')
    if (route.request().method() === 'GET' && rest === '') {
      await route.fulfill({ json: { strategies } })
      return
    }
    const id = rest.split('/').filter(Boolean)[0]
    if (rest === `/${id}`) {
      await route.fulfill({ json: documents[id] ?? FIRST })
      return
    }
    // Anything that would write: the workspace refuses it, so the refusal is
    // visible on screen. Nothing here is dispatched anywhere.
    await route.fulfill({
      status: 409,
      json: { error: { code: 'stale_base_revision', message: 'the draft revision moved on' } },
    })
  })
  await page.route('**/api/backtests/fixture-job-1', async (route: Route) => {
    await route.fulfill({ json: FIXTURE_JOB })
  })
  await page.route('**/api/scenarios**', async (route: Route) => {
    await route.fulfill({ json: { scenarios: [] } })
  })
}

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
}

test('the empty workspace keeps the AI column visible and creates inline', async ({ page }, testInfo) => {
  await scriptWorkspace(page, [])
  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.goto('/strategies')

  await expect(page.getByRole('heading', { name: '策略' })).toBeVisible()
  await expect(page.getByText('0 个策略')).toBeVisible()
  await expect(page.getByText('还没有策略。')).toBeVisible()
  // The create flow is directly completable, and the AI column is on screen before
  // anything exists to talk about.
  await expect(page.getByRole('region', { name: '新建策略' })).toBeVisible()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await expect(page.getByLabel('场景')).toBeVisible()
  const ai = page.getByRole('region', { name: 'AI 协作' })
  await expect(ai).toBeVisible()
  await expect(ai.getByText(/先创建或选择一条策略/)).toBeVisible()
  // Legacy pages stay reachable from the QUINE navigation.
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('link', { name: '运行' })).toHaveAttribute('href', '/backtests')
  await expect(nav.getByRole('link', { name: '研究' })).toHaveAttribute('href', '/research')
  await page.screenshot({ path: testInfo.outputPath('quine-empty-1400.png'), fullPage: true })

  await page.setViewportSize({ width: 420, height: 900 })
  await expect(ai).toBeVisible()
  await expect(page.getByRole('region', { name: '新建策略' })).toBeVisible()
  expect(await horizontalOverflow(page), 'the empty workspace must not scroll sideways').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('quine-empty-420.png'), fullPage: true })
})

test('the selected strategy shows its diff, its evidence entry and its refusal', async ({ page }, testInfo) => {
  await scriptWorkspace(page, [FIRST, SECOND])
  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.goto('/strategies')

  const table = page.getByRole('navigation', { name: '策略列表' })
  await table.getByRole('link', { name: '点差假设收紧' }).click()

  // The selected strategy: the compact table, the frozen draft's own facts and
  // the sections behind the workspace's tabs.
  await expect(page.getByRole('heading', { name: '点差假设收紧' })).toBeVisible()
  await expect(table.getByText('冻结 v3')).toBeVisible()
  await expect(table.getByRole('link', { name: '运行已启动' })).toHaveAttribute('href', '/backtests/fixture-job-1')
  const facts = page.getByRole('region', { name: '草稿' })
  await expect(facts.getByText('草稿修订 4')).toBeVisible()
  await expect(facts.getByText('bounded-long-v1 · XNAS:AAPL')).toBeVisible()
  await expect(facts.getByText('10000 USD')).toBeVisible()
  await expect(page.getByRole('tab', { name: '意图' })).toBeVisible()
  await expect(page.getByRole('tab', { name: '规则与假设' })).toBeVisible()

  // The typed change is reviewable in the right column: path, before → after,
  // reason, proposer, and the caveat that a model's citation is a claim.
  const review = page.getByRole('region', { name: '建议修改' })
  await expect(review.getByText('parameters.target_quantity')).toBeVisible()
  await expect(review.getByText('3 → 2')).toBeVisible()
  await expect(review.getByText('更小的仓位让这段样本的成本更可读。')).toBeVisible()
  await expect(review.getByText('提议者：model').first()).toBeVisible()
  await expect(review.getByText(/这是它自己的声明，不是已验证的事实/)).toBeVisible()
  await expect(review.getByRole('button', { name: '应用所选' })).toBeVisible()
  await expect(review.getByRole('button', { name: '撤销最近一次修改' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('quine-detail-1400.png'), fullPage: true })

  // A refused write is reported, with the host's own message kept visible, and the
  // proposal is not shown as applied.
  await review.getByRole('button', { name: '应用所选' }).click()
  const refusal = page.getByRole('alert')
  await expect(refusal).toContainText('草稿修订已前进')
  await expect(refusal).toContainText('the draft revision moved on')
  await expect(review.getByText('待处理')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('quine-refusal-1400.png'), fullPage: true })

  // The evidence entry on a real run of this strategy: the fixture job is not a
  // formal report, and the page says so instead of printing a figure.
  await page.getByRole('tab', { name: '证据' }).click()
  const evidence = page.getByRole('region', { name: '证据' })
  await expect(evidence.getByText(/正式报告只在运行成功之后才存在/)).toBeVisible()
  await expect(evidence.getByText('期末权益')).toHaveCount(0)
  await expect(page.getByRole('link', { name: '打开运行页面' })).toHaveCount(0)

  // Switching the open strategy switches every panel with it: the other strategy's
  // draft, and no change of this one.
  await page.getByLabel('当前策略').selectOption('fixture-us-2')
  await expect(page.getByRole('heading', { name: '黄金突破观察' })).toBeVisible()
  // The refusal belonged to the strategy it was made on: the strategy that is now
  // open shows its own state, not the other one's error.
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '建议修改' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: '草稿' }).getByText('草稿修订 1')).toBeVisible()
  await expect(page.getByRole('region', { name: '草稿' }).getByText('bounded-long-v1 · XNAS:XAUUSD')).toBeVisible()
  await expect(page.getByRole('tab', { name: '意图' })).toHaveAttribute('aria-selected', 'true')

  // The telephone layout: stacked rows, readable sections, the AI column still
  // present, and no sideways document scroll.
  await page.setViewportSize({ width: 420, height: 900 })
  await expect(page.getByRole('heading', { name: '黄金突破观察' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'AI 协作' })).toBeVisible()
  expect(await horizontalOverflow(page), 'the workspace must not scroll sideways at 420px').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('quine-detail-420.png'), fullPage: true })
})
