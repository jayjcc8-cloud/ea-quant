/**
 * One user strategy, end to end, against a running workspace.
 *
 * This spec needs a server that is actually running, whose workspace registers a
 * runnable scenario and whose data root holds the data behind it. It scripts
 * nothing: the draft is created through the browser, the experiment is confirmed
 * from a preview read out of the live workspace, and the run it links to is the
 * engine's own backtest job with the engine's own report. No provider is needed,
 * because creating a strategy and confirming an experiment call no model.
 *
 *   EA_WEB_BASE_URL=http://127.0.0.1:8765 \
 *   npx playwright test e2e/user-strategies.spec.ts
 *
 * What this accepts is the behaviour of the workspace that is running here —
 * served from editable source or from an installed build. It is not, by itself,
 * evidence that a packaged distribution was installed.
 *
 * The webServer/installed-runner wiring is the controller's; this file only reads
 * the base URL, exactly like the other installed specs.
 */
import { expect, test, type Page } from '@playwright/test'

const baseURL = process.env.EA_WEB_BASE_URL ?? 'http://127.0.0.1:8765'

const sameOrigin = { Origin: baseURL, 'X-EA-Web-Request': '1', 'Content-Type': 'application/json' }

type StrategyDocument = {
  user_strategy_id: string
  name: string
  draft: { revision: number; strategy_id: string; scenario_id: string; initial_cash: string; parameters: Record<string, string | number>; criterion: { schema: string; field: string; expectation: string } | null }
  experiments: {
    launch_intent_id: string
    request_id: string
    task_id: string | null
    job_id: string | null
    status: string
    payload: {
      draft_revision: number
      scenario_id: string
      parameters: Record<string, string | number>
      preview_identity?: string
      input_sha256?: string | null
      approved_budget?: { max_model_calls: number; max_backtests: number }
    }
  }[]
}

type Preview = {
  draft_revision: number
  scenario_id: string
  initial_cash: string
  parameters: Record<string, string | number>
  data: unknown
  input_identity?: unknown
  input_sha256?: string | null
  preview_identity: string
  normalized: Record<string, unknown>
  commission: unknown
  slippage: unknown
  latency: unknown
  criterion: { schema: string; field: string; expectation: string } | null
  budget: { max_model_calls: number; max_backtests: number }
}

/**
 * One cost assumption exactly as the page states it, so the expectation below is
 * the page's own rule: what the scenario declares, or that it declares nothing.
 */
function declared(value: unknown): string {
  if (value === null || value === undefined) return 'not declared in this scenario'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([key, item]) => `${key} ${String(item)}`).join(', ') : 'declared empty'
  }
  return String(value)
}

async function waitForHealth(): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`${baseURL}/api/health`)
      if (response.ok) return
    } catch {
      // The installed server may still be starting; retry within the bounded loop.
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error('service did not become available')
}

/** One bounded wait on the real job index; never a wall-clock guess. */
async function waitForTerminalJob(page: Page, jobId: string): Promise<{ status: string; message: string | null }> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const job = await page.request.get(`/api/backtests/${jobId}`).then((response) => response.json())
    if (job.status !== 'accepted' && job.status !== 'running') return job
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`job ${jobId} never reached a terminal state`)
}

const read = (page: Page, id: string): Promise<StrategyDocument> =>
  page.request.get(`/api/user-strategies/${id}`).then((response) => response.json())

test('a confirmed strategy experiment freezes one real run and two strategies stay independent', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  await waitForHealth()

  const catalog = await page.request.get('/api/scenarios').then((response) => response.json())
  const scenario = catalog.scenarios.find(
    (item: { valid: boolean; strategy_parameters?: unknown[] }) => item.valid && (item.strategy_parameters?.length ?? 0) > 0,
  )
  expect(scenario, 'the isolated workspace must register one runnable scenario').toBeTruthy()

  // The default entry is the strategy workspace; the legacy pages stay reachable.
  await page.goto('/')
  await expect(page).toHaveURL(/\/strategies$/)
  await expect(page.getByRole('heading', { name: 'Strategies' })).toBeVisible()
  await expect(page.getByText('No strategies yet.')).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Research' })).toBeVisible()

  // One strategy, created through the browser: a save, with a stated criterion.
  await page.getByLabel('Strategy name').fill('Trend follow')
  await page.getByLabel('Intent').fill('Check whether a delayed entry still pays for its own costs on the registered data')
  await page.getByLabel('Scenario', { exact: true }).selectOption(scenario.scenario_id)
  await page.getByLabel('Criterion field').selectOption('economics.net_pnl.amount')
  await page.getByLabel('Expected sign').selectOption('positive')
  await page.getByRole('button', { name: 'Create strategy' }).click()
  await expect(page).toHaveURL(/\/strategies\/[0-9a-f-]{36}$/)
  const firstId = new URL(page.url()).pathname.split('/').at(-1)!
  const first = await read(page, firstId)
  expect(first.draft.revision).toBe(1)
  expect(first.draft.scenario_id).toBe(scenario.scenario_id)
  expect(first.experiments).toEqual([])
  // The host owns the criterion vocabulary: the browser sent a field and a sign,
  // and the stored draft carries the report's own schema.
  expect(first.draft.criterion).toEqual({
    schema: 'radian.report-criterion.v1',
    field: 'economics.net_pnl.amount',
    expectation: 'positive',
  })
  // With a strategy open the save form gives way to the page, and one obvious
  // entry point to creating another stays on screen.
  await expect(page.getByRole('region', { name: 'New strategy' })).toBeHidden()
  await expect(page.getByRole('link', { name: 'New strategy' })).toBeVisible()
  const draftPanel = page.getByRole('region', { name: 'Draft' })
  await expect(draftPanel.getByText('Draft revision 1')).toBeVisible()
  await expect(draftPanel.getByText(`Initial cash ${first.draft.initial_cash} USD`)).toBeVisible()
  await expect(draftPanel.getByText('Criterion: Net P&L expected positive')).toBeVisible()

  // A second strategy on the SAME registered strategy is a separate object with
  // its own draft, revision and experiment list.
  const secondResponse = await page.request.post('/api/user-strategies', {
    headers: sameOrigin,
    data: { name: 'Trend follow, no criterion', intent: 'The same idea, stated without a criterion', scenario_id: scenario.scenario_id },
  })
  expect(secondResponse.status()).toBe(201)
  const second = (await secondResponse.json()) as StrategyDocument
  expect(second.user_strategy_id).not.toBe(firstId)
  expect(second.draft.strategy_id).toBe(first.draft.strategy_id)
  expect(second.draft.revision).toBe(1)
  expect(second.draft.criterion).toBeNull()
  expect(second.experiments).toEqual([])

  await page.goto('/strategies')
  const list = page.getByRole('navigation', { name: 'Strategies' })
  await expect(list.getByRole('link', { name: 'Trend follow', exact: true })).toBeVisible()
  await expect(list.getByRole('link', { name: 'Trend follow, no criterion', exact: true })).toBeVisible()

  // The second strategy's preview is its own, and it says honestly that no
  // criterion was stated.
  await list.getByRole('link', { name: 'Trend follow, no criterion', exact: true }).click()
  await page.getByRole('button', { name: 'Preview experiment' }).click()
  const secondFacts = page.locator('dl.preview-facts')
  await expect(secondFacts.getByText('Draft revision 1')).toBeVisible()
  await expect(secondFacts.getByText('Criterion None was stated, so this run cannot be concluded for or against the idea')).toBeVisible()
  // Previewing starts nothing.
  expect((await read(page, second.user_strategy_id)).experiments).toEqual([])

  // The first strategy's experiment: preview shows the live data identity and the
  // scenario's actual cost assumptions, then an explicit confirmation starts it.
  const preview = (await page.request
    .get(`/api/user-strategies/${firstId}/experiment/preview`)
    .then((response) => response.json())) as Preview
  expect(preview.draft_revision).toBe(1)
  expect(preview.criterion).toEqual(first.draft.criterion)
  expect(preview.budget.max_backtests).toBeGreaterThanOrEqual(1)
  // The preview carries the admitted snapshot's own identity, and nothing about
  // reading it creates anything.
  expect(preview.preview_identity).toMatch(/^sha256:[0-9a-f]{64}$/)
  expect(preview.input_identity).toBeTruthy()
  expect(preview.input_sha256).toBeTruthy()
  expect(preview.normalized.execution).toBeTruthy()
  expect((await read(page, firstId)).experiments).toEqual([])

  await page.goto(`/strategies/${firstId}`)
  await page.getByRole('button', { name: 'Preview experiment' }).click()
  const facts = page.locator('dl.preview-facts')
  await expect(facts).toContainText(`Draft revision ${preview.draft_revision}`)
  await expect(facts).toContainText(`Initial cash ${preview.initial_cash} USD`)
  await expect(facts).toContainText('Criterion radian.report-criterion.v1 economics.net_pnl.amount positive')
  await expect(facts).toContainText(
    `commission ${declared(preview.commission)} · slippage ${declared(preview.slippage)} · latency ${declared(preview.latency)}`,
  )
  // The identity the confirmation will carry back is readable before the click.
  await expect(facts).toContainText(preview.preview_identity.slice(0, 19))
  const fingerprint = (preview.data as { fingerprint?: { sha256?: string } })?.fingerprint?.sha256
  if (typeof fingerprint === 'string') {
    await expect(facts).toContainText(fingerprint.slice(0, 8))
  }

  await page.getByRole('button', { name: 'Start experiment' }).click()
  await expect(page.getByText('Started experiment li-1 from revision 1')).toBeVisible()
  const launched = await read(page, firstId)
  const experiment = launched.experiments[0]
  expect(experiment.status).toBe('created')
  expect(experiment.job_id).toBeTruthy()
  expect(experiment.task_id).toBeTruthy()
  // The confirmed payload is retained on the record, not merely hashed: the
  // revision it froze, the input identity this confirmation named, and the budget
  // the workspace approved for it — one backtest and no model call.
  expect(experiment.payload.draft_revision).toBe(1)
  expect(experiment.payload.scenario_id).toBe(scenario.scenario_id)
  expect(experiment.payload.parameters).toEqual(first.draft.parameters)
  expect(experiment.payload.preview_identity).toBe(preview.preview_identity)
  expect(experiment.payload.input_sha256).toBe(preview.input_sha256)
  expect(experiment.payload.approved_budget).toEqual({ max_model_calls: 0, max_backtests: 1 })
  expect(launched.draft.revision).toBe(1)
  // One started run is not a finished piece of research, and this page says where
  // the rest of it belongs.
  const experimentPanel = page.getByRole('region', { name: 'Experiment' })
  await expect(experimentPanel.locator('p.muted', { hasText: /One run is one frozen result, not research that is finished/ })).toBeVisible()
  await expect(experimentPanel.getByRole('link', { name: 'holdout' })).toHaveAttribute('href', '/holdouts')
  await expect(experimentPanel.getByRole('link', { name: 'research' })).toHaveAttribute('href', '/research')

  // Follow the real run and its formal report on the existing backtest page.
  await page.getByRole('link', { name: 'Open the run to check its status and report' }).click()
  await expect(page).toHaveURL(new RegExp(`/backtests/${experiment.job_id}$`))
  const job = await waitForTerminalJob(page, experiment.job_id!)
  expect(job.status, job.message ?? 'the confirmed experiment must run').toBe('succeeded')
  await expect(page.locator('.page-title .status')).toHaveText('succeeded')
  await expect(page.getByRole('link', { name: 'Download report.json' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('link', { name: 'Download report.json' })).toBeVisible()

  // The run executed the confirmed draft, not today's anything.
  const jobDocument = await page.request.get(`/api/backtests/${experiment.job_id}`).then((response) => response.json())
  for (const [name, value] of Object.entries(first.draft.parameters)) {
    expect(String(jobDocument.input_snapshot.scenario.strategy[name])).toBe(String(value))
  }
  expect(jobDocument.input_snapshot.scenario.funding.initial_cash).toBe(first.draft.initial_cash)
  const report = await page.request.get(`/api/backtests/${experiment.job_id}/report`).then((response) => response.json())
  expect(String(report.schema)).toMatch(/^ea\.backtest-report/)
  await testInfo.attach('strategy-experiment-evidence', {
    body: JSON.stringify({ first, second, preview, experiment, job, report }, null, 2),
    contentType: 'application/json',
  })

  // Refreshing the strategy shows the same frozen experiment, on the same run,
  // while the second strategy is still untouched.
  await page.goto(`/strategies/${firstId}`)
  await expect(page.getByText('Started experiment li-1 from revision 1')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open the run to check its status and report' }))
    .toHaveAttribute('href', `/backtests/${experiment.job_id}`)
  const reread = await read(page, firstId)
  expect(reread.experiments).toEqual(launched.experiments)
  expect(reread.draft.revision).toBe(1)
  expect((await read(page, second.user_strategy_id)).draft.revision).toBe(1)
  expect((await read(page, second.user_strategy_id)).experiments).toEqual([])

  // The frozen child stays read-only on the legacy research surface: its run is
  // already the record, so re-confirming it is refused as a frozen child.
  const refusal = await page.request.post(`/api/research/tasks/${experiment.task_id}/confirm`, {
    headers: sameOrigin,
    data: { auto_run: true },
  })
  expect(refusal.status()).toBe(409)
  expect(JSON.stringify(await refusal.json())).toContain('frozen experiment of a user strategy')

  // The list carries the frozen run on the strategy's own line, at the revision
  // that ran rather than at today's head.
  await page.goto('/strategies')
  await expect(
    page.getByRole('navigation', { name: 'Strategies' })
      .getByText('Frozen run from revision 1 · run started'),
  ).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('strategy-workspace.png'), fullPage: true })

  // A narrow viewport with the real run and the admitted input on screen: the
  // workspace stays inside the viewport instead of scrolling sideways.
  await page.setViewportSize({ width: 420, height: 900 })
  await page.goto(`/strategies/${firstId}`)
  await expect(page.getByText('Started experiment li-1 from revision 1')).toBeVisible()
  await page.getByRole('button', { name: 'Preview experiment' }).click()
  await expect(page.locator('dl.preview-facts')).toContainText(`Draft revision ${preview.draft_revision}`)
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow, 'the workspace must not scroll sideways at a narrow width').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('strategy-workspace-narrow.png'), fullPage: true })
})
