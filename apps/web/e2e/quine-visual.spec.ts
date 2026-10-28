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
import { researchCollection } from '../preview/research-fixtures'
import type { UserStrategy } from '../src/user-strategies-api'
import type { ResearchQuery } from '../src/research-workbench-api'

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
  await page.route('**/api/research?**', async route => {
    const query = Object.fromEntries(new URL(route.request().url()).searchParams) as ResearchQuery
    const result = researchCollection(strategies as UserStrategy[], query)
    for (const row of result.items) if (row.latest_experiment?.job_id === FIXTURE_JOB.job_id) {
      row.latest_experiment.job_status = FIXTURE_JOB.status
      row.next_step = 'inspect'
    }
    await route.fulfill({ json: result })
  })
  await page.route('**/api/user-strategies**', async (route: Route) => {
    const url = new URL(route.request().url())
    const rest = url.pathname.replace('/api/user-strategies', '')
    if (route.request().method() === 'GET' && rest === '') {
      await route.fulfill({ status: 405, json: { error: 'retired collection route' } })
      return
    }
    const id = rest.split('/').filter(Boolean)[0]
    if (rest === `/${id}`) {
      await route.fulfill({ json: documents[id] ?? FIRST })
      return
    }
    if (route.request().method() === 'GET' && rest === `/${id}/draft/contract`) {
      const document = documents[id] as typeof FIRST
      await route.fulfill({ json: {
        draft_revision: document.draft.revision,
        scenario_id: document.draft.scenario_id,
        source: 'scripted visual fixture',
        fields: [
          { path: 'initial_cash', kind: 'funding', json_type: 'decimal_string', current: document.draft.initial_cash, minimum: null, maximum: null, editable: true },
          { path: 'parameters.target_quantity', kind: 'parameter', json_type: 'decimal_string', current: document.draft.parameters.target_quantity, minimum: '0', maximum: null, editable: true },
          { path: 'parameters.entry_delay_bars', kind: 'parameter', json_type: 'integer', current: document.draft.parameters.entry_delay_bars, minimum: 0, maximum: 2, editable: true },
        ],
      } })
      return
    }
    if (route.request().method() === 'GET' && rest === `/${id}/conversation`) {
      // Reading the selected conversation's state, not a refused write: the page
      // restores what it is waiting for and what it may still spend. The budget
      // below is an explicitly scripted, unused grant — no provider is configured
      // and no model is called by this fixture.
      const document = documents[id] as typeof FIRST
      await route.fulfill({ json: {
        strategy_id: document.user_strategy_id,
        revision: document.draft.revision,
        pending: null,
        preview: null,
        open_proposals: document.proposals
          .filter(proposal => proposal.status === 'open')
          .map(proposal => proposal.proposal_id),
        budget: {
          max_model_calls_per_turn: 4,
          reserved_model_calls: 0,
          max_backtests: 1,
        },
      } })
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

  await expect(page.getByRole('heading', { name: '研究工作台', exact: true })).toBeVisible()
  await expect(page.getByText('0 项研究')).toBeVisible()
  await expect(page.getByText('还没有策略。')).toBeVisible()
  // The create flow is directly completable, and the AI column is on screen before
  // anything exists to talk about. The form asks for the idea alone: a scenario
  // belongs to the one explicit binding on the strategy itself.
  await page.getByRole('button', { name: '新建研究', exact: true }).click()
  await expect(page.getByRole('region', { name: '新建研究' })).toBeVisible()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await expect(page.getByLabel('意图')).toBeVisible()
  await expect(page.getByRole('region', { name: '新建研究' }).getByLabel('场景')).toHaveCount(0)
  const ai = page.getByRole('region', { name: 'AI 协作' })
  await expect(ai).toBeVisible()
  await expect(ai.getByText(/选择或新建研究，继续它自己的对话/)).toBeVisible()
  // The shell and the single history entry match the desktop workspace routes.
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('link', { name: '研究' })).toHaveAttribute('href', '/strategies')
  await expect(nav.getByRole('link', { name: '模拟运行' })).toHaveAttribute('href', '/runs')
  await expect(page.getByRole('complementary', { name: '工作区侧栏' }).getByRole('link', { name: '数据与模型' })).toHaveAttribute('href', '/resources/data')
  await expect(nav.getByRole('link', { name: '回测记录' })).toHaveAttribute('href', '/backtests')
  await expect(page.getByRole('link', { name: '历史记录' })).toHaveAttribute('href', '/backtests#history-records')
  await expect(page.getByRole('link', { name: '历史记录' })).toHaveCount(1)
  await page.screenshot({ path: testInfo.outputPath('quine-empty-1400.png'), fullPage: true })

  await page.setViewportSize({ width: 420, height: 900 })
  await expect(page.getByRole('region', { name: '新建研究' })).toBeVisible()
  await page.getByRole('group', { name: '工作区视图' }).getByRole('button', { name: 'AI 协作', exact: true }).click()
  await expect(ai).toBeVisible()
  await expect(page.getByRole('region', { name: '新建研究' })).toBeHidden()
  expect(await horizontalOverflow(page), 'the empty workspace must not scroll sideways').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('quine-empty-420.png'), fullPage: true })
})

test('the collection shows every strategy\'s frozen evidence, and the selected strategy shows its diff, its evidence entry and its refusal', async ({ page }, testInfo) => {
  await scriptWorkspace(page, [FIRST, SECOND])
  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.goto('/strategies')

  // The collection route owns the table, and the table carries each strategy's own
  // frozen revision and the run its latest confirmation dispatched — a real page
  // read from that job, never a claim that a created dispatch succeeded.
  await expect(page.getByRole('heading', { name: '研究工作台', exact: true })).toBeVisible()
  const table = page.getByRole('navigation', { name: '策略列表' })
  const firstRow = table.locator('tr').filter({ hasText: '点差假设收紧' })
  await expect(firstRow.getByText(/v4 · 1 项建议待审阅/)).toBeVisible()
  await expect(firstRow.getByText('冻结 v3')).toBeVisible()
  await expect(firstRow.getByText(/XNAS:AAPL/)).toBeVisible()
  await expect(firstRow.getByRole('link', { name: '查看运行' }))
    .toHaveAttribute('href', '/backtests/fixture-job-1?research=fixture-us-1')
  const secondRow = table.locator('tr').filter({ hasText: '黄金突破观察' })
  await expect(secondRow.getByText('v1', { exact: true })).toBeVisible()
  await expect(secondRow.getByText(/冻结/)).toHaveCount(0)
  await expect(secondRow.getByText('尚未实验')).toBeVisible()

  // The selected strategy opens by its exact identity: the collection table gives
  // way to that strategy's own detail.
  await table.getByRole('link', { name: '点差假设收紧' }).click()
  await expect(page).toHaveURL(/\/strategies\/fixture-us-1$/)
  await expect(page.getByRole('heading', { name: '点差假设收紧' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: '策略列表' })).toHaveCount(0)

  // The evidence entry on a run of this strategy: the fixture job is not a formal
  // report, and the page says so instead of printing a figure. The run is live, so
  // the detail opens on the experiment whose report does not exist yet.
  const evidence = page.getByRole('region', { name: '证据' })
  await expect(evidence).toBeVisible()
  await expect(page.getByRole('tab', { name: '实验' })).toHaveAttribute('aria-selected', 'true')
  await expect(evidence.getByText(/正式报告只在运行成功之后才存在/)).toBeVisible()
  await expect(evidence.getByText('期末权益')).toHaveCount(0)
  await expect(page.getByRole('link', { name: '打开运行页面' })).toHaveCount(0)

  // The strategy tab: the frozen draft's own facts, and the sections the old tabs
  // named. A rule section is progressive detail behind the shipped disclosure.
  await page.getByRole('tab', { name: '策略' }).click()
  await expect(page.getByRole('tab', { name: '策略' })).toHaveAttribute('aria-selected', 'true')
  const facts = page.getByRole('region', { name: '草稿' })
  await expect(facts.getByText('草稿修订 4', { exact: true })).toBeVisible()
  await expect(facts.getByText('bounded-long-v1 · XNAS:AAPL')).toBeVisible()
  await expect(facts.getByText('10000 USD')).toBeVisible()
  await expect(page.getByRole('region', { name: '意图' })).toBeVisible()
  const rules = page.locator('details.provenance')
    .filter({ has: page.locator('summary', { hasText: '场景规则与来源' }) }).first()
  await expect(rules).not.toHaveAttribute('open', '')
  await rules.locator('summary').first().click()
  await expect(page.getByRole('region', { name: '规则与假设' })).toBeVisible()

  // The typed change is reviewable in the right column: path, both labelled values,
  // reason, proposer, and the caveat that a model's citation is a claim.
  const review = page.getByRole('region', { name: '建议修改' })
  await expect(review.getByText('parameters.target_quantity')).toBeVisible()
  await expect(review.getByText('之前 3')).toBeVisible()
  await expect(review.getByText('之后 2')).toBeVisible()
  await expect(review.getByText('更小的仓位让这段样本的成本更可读。')).toBeVisible()
  await expect(review.getByText('待处理')).toBeVisible()
  await expect(review.getByRole('button', { name: '应用所选' })).toBeVisible()
  // This open proposal has not been applied; another proposal's undo must not act here.
  await expect(review.getByRole('button', { name: '撤销最近一次修改' })).toHaveCount(0)
  const proposalOrigin = review.locator('details.provenance')
    .filter({ has: page.locator('summary', { hasText: '来源与标识' }) }).first()
  await expect(proposalOrigin).not.toHaveAttribute('open', '')
  await proposalOrigin.locator('summary').first().click()
  await expect(proposalOrigin.getByText('提议者：model').first()).toBeVisible()
  await expect(proposalOrigin.getByText(/这是它自己的声明，不是已验证的事实/)).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('quine-detail-1400.png'), fullPage: true })

  // A refused write is reported, with the host's own message kept visible, and the
  // proposal is not shown as applied.
  await review.getByRole('button', { name: '应用所选' }).click()
  const refusal = page.getByRole('alert')
  await expect(refusal).toContainText('草稿修订已前进')
  await expect(refusal).toContainText('the draft revision moved on')
  await expect(review.getByText('待处理')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('quine-refusal-1400.png'), fullPage: true })

  // Switching the open strategy switches every panel with it: the collection's own
  // breadcrumb leads back to the table, and the other strategy's draft, evidence
  // and conversation are the ones on screen after that.
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '研究', exact: true }).click()
  await expect(page).toHaveURL(/\/strategies$/)
  await table.getByRole('link', { name: '黄金突破观察' }).click()
  await expect(page).toHaveURL(/\/strategies\/fixture-us-2$/)
  await expect(page.getByRole('heading', { name: '黄金突破观察' })).toBeVisible()
  // The refusal belonged to the strategy it was made on: the strategy that is now
  // open shows its own state, not the other one's error. A strategy with no
  // proposal has no proposal panel at all.
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '建议修改' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: '草稿' }).getByText('草稿修订 1', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: '草稿' }).getByText('bounded-long-v1 · XNAS:XAUUSD')).toBeVisible()
  // A strategy with no frozen run opens on its own draft, so its facts are the
  // first thing on screen.
  await expect(page.getByRole('tab', { name: '策略' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('region', { name: 'AI 协作' })).toBeVisible()

  // The narrow layout exposes each panel through the workspace switch, without overflow.
  await page.setViewportSize({ width: 420, height: 900 })
  await expect(page.getByRole('heading', { name: '黄金突破观察' })).toBeVisible()
  await page.getByRole('group', { name: '工作区视图' }).getByRole('button', { name: 'AI 协作', exact: true }).click()
  await expect(page.getByRole('region', { name: 'AI 协作' })).toBeVisible()
  expect(await horizontalOverflow(page), 'the workspace must not scroll sideways at 420px').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('quine-detail-420.png'), fullPage: true })
})
