/**
 * One user strategy, end to end, against a running workspace.
 *
 * This spec needs a server that is actually running, whose workspace registers a
 * runnable scenario and whose data root holds the data behind it. It scripts
 * nothing: the idea is saved through the browser first, then bound to one
 * registered scenario by one explicit confirmed choice, the experiment is
 * confirmed from a preview read out of the live workspace, and the run it links to
 * is the engine's own backtest job with the engine's own report. No provider is
 * needed, because saving an idea, binding a scenario and confirming an experiment
 * call no model.
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
  binding?: { scenario_id: string; from_revision: number; to_revision: number } | null
  messages: { message_id: string; role: 'user' | 'model'; text: string; call_id: string }[]
  calls: { call_id: string }[]
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

/**
 * The same record while it is still only a saved idea: revision 1, no scenario and
 * no executable field at all. It is read with its own type so an assertion about an
 * intent cannot accidentally reach for a draft field that does not exist yet.
 */
type IntentDocument = {
  user_strategy_id: string
  draft: { kind: 'intent'; revision: number; scenario_id: null }
  calls: unknown[]
  experiments: unknown[]
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
  if (value === null || value === undefined) return '该场景未声明'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([key, item]) => `${key} ${String(item)}`).join(', ') : '声明为空'
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

const readIntent = (page: Page, id: string): Promise<IntentDocument> =>
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
  await expect(page.getByRole('heading', { name: '研究工作台', exact: true })).toBeVisible()
  await expect(page.getByText('还没有策略。')).toBeVisible()
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '研究' })).toBeVisible()

  // One idea, saved through the browser first: the entry point asks for the idea,
  // not for a scenario, so a save needs no catalog and writes no executable field.
  await page.getByRole('button', { name: '新建研究', exact: true }).click()
  await page.getByLabel('策略名称').fill('Trend follow')
  await page.getByLabel('意图').fill('Check whether a delayed entry still pays for its own costs on the registered data')
  await page.getByRole('button', { name: '保存想法' }).click()
  await expect(page).toHaveURL(/\/strategies\/[0-9a-f-]{36}$/)
  const firstId = new URL(page.url()).pathname.split('/').at(-1)!
  const savedIntent = await readIntent(page, firstId)
  // The saved idea is revision 1 of the same durable strategy: no scenario, no
  // strategy, symbol, funding or parameter, and nothing was called or run for it.
  expect(savedIntent.draft).toEqual({ kind: 'intent', revision: 1, scenario_id: null })
  expect(savedIntent.experiments).toEqual([])
  expect(savedIntent.calls).toEqual([])
  // The page says the same: it shows no strategy, instrument, funding or criterion
  // where none exists, and it survives a reload of the same id.
  const intentPanel = page.getByRole('region', { name: '草稿' })
  await expect(intentPanel.getByText('意图修订 1')).toBeVisible()
  await expect(intentPanel.getByText('尚未绑定：没有策略、标的、资金、数据来源或注册参数')).toBeVisible()
  await page.reload()
  const bindPanel = page.getByRole('region', { name: '绑定场景' })
  await expect(bindPanel).toBeVisible()
  expect((await readIntent(page, firstId)).draft).toEqual({ kind: 'intent', revision: 1, scenario_id: null })

  // The one explicit choice that gives it a scenario. Nothing is preselected, the
  // registered scenario is named by its exact id beside its own summary, and the
  // button stays inert until the confirmation is ticked — so a reloaded intent
  // cannot be bound by a stray click.
  await expect(bindPanel.getByLabel('场景', { exact: true })).toHaveValue('')
  expect(await bindPanel.getByRole('button', { name: '绑定场景' }).isDisabled()).toBe(true)
  await bindPanel.getByLabel('场景', { exact: true }).selectOption(scenario.scenario_id)
  // Selecting a scenario still binds nothing: only the confirmed click does.
  expect((await readIntent(page, firstId)).draft).toEqual({ kind: 'intent', revision: 1, scenario_id: null })
  await bindPanel.getByLabel('准则字段').selectOption('economics.net_pnl.amount')
  await bindPanel.getByLabel('预期方向').selectOption('positive')
  await bindPanel.getByLabel('我确认把选中的这个场景绑定到这条策略').check()
  await bindPanel.getByRole('button', { name: '绑定场景' }).click()
  await expect(bindPanel).toHaveCount(0)

  const first = await read(page, firstId)
  // One binding, one revision: the same strategy, the same id, the same messages —
  // and the executable draft the host formed from the chosen scenario.
  const boundRevision = 2
  expect(first.user_strategy_id).toBe(firstId)
  expect(first.draft.revision).toBe(boundRevision)
  expect(first.draft.scenario_id).toBe(scenario.scenario_id)
  expect(first.binding?.scenario_id).toBe(scenario.scenario_id)
  expect(first.experiments).toEqual([])
  expect(first.calls).toEqual([])
  // The host owns the criterion vocabulary: the browser sent a field and a sign,
  // and the stored draft carries the report's own schema.
  expect(first.draft.criterion).toEqual({
    schema: 'radian.report-criterion.v1',
    field: 'economics.net_pnl.amount',
    expectation: 'positive',
  })
  // With a strategy open the save form gives way to the page, and one obvious
  // entry point to creating another stays on screen.
  await expect(page.getByRole('region', { name: '新建研究' })).toBeHidden()
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '研究', exact: true })).toBeVisible()
  const draftPanel = page.getByRole('region', { name: '草稿' })
  await expect(draftPanel.getByText(`草稿修订 ${boundRevision}`, { exact: true })).toBeVisible()
  await expect(draftPanel.getByText(`${first.draft.initial_cash} USD`)).toBeVisible()
  await expect(draftPanel.getByText('净盈亏 预期为正')).toBeVisible()

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
  const list = page.getByRole('navigation', { name: '策略列表' })
  await expect(list.getByRole('link', { name: 'Trend follow', exact: true })).toBeVisible()
  await expect(list.getByRole('link', { name: 'Trend follow, no criterion', exact: true })).toBeVisible()

  // The second strategy's preview is its own, and it says honestly that no
  // criterion was stated.
  await list.getByRole('link', { name: 'Trend follow, no criterion', exact: true }).click()
  await page.getByRole('tab', { name: '实验' }).click()
  await page.getByRole('button', { name: '预览实验' }).click()
  const secondFacts = page.locator('dl.preview-facts')
  await expect(secondFacts.getByText('草稿修订 1', { exact: true })).toBeVisible()
  await expect(secondFacts.getByText('未声明准则，因此这次运行无法支持或否定该想法')).toBeVisible()
  // Previewing starts nothing.
  expect((await read(page, second.user_strategy_id)).experiments).toEqual([])

  // The first strategy's experiment: preview shows the live data identity and the
  // scenario's actual cost assumptions, then an explicit confirmation starts it.
  const preview = (await page.request
    .get(`/api/user-strategies/${firstId}/experiment/preview`)
    .then((response) => response.json())) as Preview
  expect(preview.draft_revision).toBe(boundRevision)
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
  await page.getByRole('tab', { name: '实验' }).click()
  await page.getByRole('button', { name: '预览实验' }).click()
  const facts = page.locator('dl.preview-facts')
  await expect(facts).toContainText(`草稿修订 ${preview.draft_revision}`)
  await expect(facts).toContainText(`初始资金 ${preview.initial_cash} USD`)
  await expect(facts).toContainText('radian.report-criterion.v1 economics.net_pnl.amount positive')
  await expect(facts).toContainText(
    `手续费 ${declared(preview.commission)} · 滑点 ${declared(preview.slippage)} · 延迟 ${declared(preview.latency)}`,
  )
  // The identity the confirmation will carry back is readable before the click.
  await expect(facts).toContainText(preview.preview_identity.slice(0, 19))
  const fingerprint = (preview.data as { fingerprint?: { sha256?: string } })?.fingerprint?.sha256
  if (typeof fingerprint === 'string') {
    await expect(facts).toContainText(fingerprint.slice(0, 8))
  }

  const confirmed = page.waitForResponse(response => new URL(response.url()).pathname === `/api/user-strategies/${firstId}/experiments` && response.request().method() === 'POST')
  await page.getByRole('button', { name: '确认并运行一次' }).click()
  expect((await confirmed).ok()).toBe(true)
  const launched = await read(page, firstId)
  const experiment = launched.experiments[0]
  expect(experiment.status).toBe('created')
  expect(experiment.job_id).toBeTruthy()
  expect(experiment.task_id).toBeTruthy()
  // The confirmed payload is retained on the record, not merely hashed: the
  // revision it froze, the input identity this confirmation named, and the budget
  // the workspace approved for it — one backtest and no model call.
  expect(experiment.payload.draft_revision).toBe(boundRevision)
  expect(experiment.payload.scenario_id).toBe(scenario.scenario_id)
  expect(experiment.payload.parameters).toEqual(first.draft.parameters)
  expect(experiment.payload.preview_identity).toBe(preview.preview_identity)
  expect(experiment.payload.input_sha256).toBe(preview.input_sha256)
  expect(experiment.payload.approved_budget).toEqual({ max_model_calls: 0, max_backtests: 1 })
  expect(launched.draft.revision).toBe(boundRevision)
  // One started run is not a finished piece of research, and this page says where
  // the rest of it belongs. The controls that hold the launch ship folded away
  // once a run exists, so they are opened before they are read.
  const experimentPanel = page.getByRole('region', { name: '实验' })
  const experimentControls = page.locator('details.experiment-controls').first()
  if (await experimentControls.getAttribute('open') === null) await experimentControls.locator('summary').first().click()
  await expect(experimentControls).toHaveAttribute('open', '')
  await expect(page.getByText(`已从修订 ${boundRevision} 启动实验 li-1`)).toBeVisible()
  await expect(experimentPanel.locator('p.muted', { hasText: /一次运行只是一个冻结的结果，不是已经完成的研究/ })).toBeVisible()
  await expect(experimentPanel.getByRole('link', { name: '查看这次实验报告' }))
    .toHaveAttribute('href', `/strategies/${firstId}?tab=experiments&evidence_a=${experiment.job_id}`)

  // The run's own outcome is the run's own, read from its persisted record until
  // it reaches a state its owner will not move on its own.
  const job = await waitForTerminalJob(page, experiment.job_id!)
  expect(job.status, job.message ?? 'the confirmed experiment must run').toBe('succeeded')

  // Follow the current inline evidence entry and its own independent report link.
  await experimentPanel.getByRole('link', { name: '查看这次实验报告' }).click()
  await page.getByRole('region', { name: '证据' }).getByRole('link', { name: '独立报告链接' }).click()
  await expect(page).toHaveURL(new RegExp(`/backtests/${experiment.job_id}$`))
  await expect(page.locator('.page-title .status')).toHaveText('已完成')
  await page.locator('summary').filter({ hasText: '原始执行字段' }).click()
  await expect(page.getByRole('link', { name: 'Download report.json' })).toBeVisible()
  await page.reload()
  await page.locator('summary').filter({ hasText: '原始执行字段' }).click()
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

  // The workspace's own view of that same run, still with no model anywhere: it
  // reads the job, the formal report and the equity path through the existing
  // adapter and prints the report's own figures. Nothing below is a fixture and
  // nothing is recomputed here.
  const fee = report.economics.fees
  const netPnl = report.economics.net_pnl
  expect(fee, 'the engine report must record its own fee total').toBeTruthy()
  // The report the page may show is only ever the report of the run it was read for.
  expect(report.run_id).toBe(jobDocument.engine_run_id)

  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.goto(`/strategies/${firstId}`)
  // A strategy with a frozen run opens on the tab that holds it.
  await expect(page.getByRole('tab', { name: '实验' })).toHaveAttribute('aria-selected', 'true')
  const evidence = page.getByRole('region', { name: '证据' })
  const metrics = evidence.locator('.report-raw-values')
  await metrics.locator('summary').click()
  await expect(metrics).toContainText(`${netPnl.amount} ${netPnl.currency ?? report.economics.currency}`)
  await expect(metrics).toContainText(`${fee.amount} ${fee.currency ?? report.economics.currency}`)
  // The identity this report was read under stays on screen: the web job, the
  // engine run and the report digest are the persisted ones. It is deliberate
  // detail behind its own summary, so that summary is opened to read it.
  expect(jobDocument.engine_run_id, 'the engine run must have its own id').toBeTruthy()
  await evidence.getByRole('navigation', { name: '报告视图' }).getByRole('button', { name: '输入与来源', exact: true }).click()
  const provenance = evidence.locator('details.provenance')
    .filter({ has: page.locator('summary', { hasText: '技术细节与来源' }) }).first()
  await expect(provenance).not.toHaveAttribute('open', '')
  await provenance.locator('summary').first().click()
  await expect(provenance).toContainText(jobDocument.job_id)
  await expect(provenance).toContainText(jobDocument.engine_run_id)
  await expect(provenance).toContainText(jobDocument.report_sha256)
  // The run itself, and its report, are one click from here — and the digest the
  // link carries is the digest of this run's own persisted report.
  await expect(evidence.getByRole('link', { name: '独立报告链接' }))
    .toHaveAttribute('href', `/backtests/${experiment.job_id}`)
  await expect(evidence.getByRole('link', { name: '下载正式报告', exact: true }))
    .toHaveAttribute('href', `/api/backtests/${experiment.job_id}/artifacts/report.json`)
  // And where the rest of the research belongs: one explicit holdout entry for
  // this exact frozen run.
  const holdoutEntry = evidence.locator('details.provenance')
    .filter({ has: page.locator('summary', { hasText: '留出验证与候选' }) }).first()
  await expect(holdoutEntry).not.toHaveAttribute('open', '')
  await holdoutEntry.locator('summary').first().click()
  await expect(holdoutEntry.getByRole('link', { name: '创建本实验的留出验证' }))
    .toHaveAttribute('href', `/holdouts/new/${experiment.job_id}`)
  // The curve is shown only because the equity path's own identity is this run's
  // and this report's.
  await expect(evidence.locator('.evidence-curve svg')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('strategy-evidence-1400.png'), fullPage: true })

  await page.setViewportSize({ width: 420, height: 900 })
  await expect(evidence.locator('.evidence-curve svg')).toBeVisible()
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth),
    'the evidence view must not scroll sideways at a narrow width',
  ).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('strategy-evidence-420.png'), fullPage: true })

  // Return through the main research entry and open the create action. The form
  // asks for the idea alone; opening it neither binds a scenario nor creates data.
  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '研究', exact: true }).click()
  await page.getByRole('button', { name: '新建研究', exact: true }).click()
  await expect(page).toHaveURL(/\/strategies$/)
  const createForm = page.getByRole('region', { name: '新建研究' })
  await expect(createForm).toBeVisible()
  await expect(createForm.getByLabel('策略名称')).toBeVisible()
  await expect(createForm.getByLabel('意图')).toBeVisible()
  await expect(createForm.getByRole('button', { name: '保存想法' })).toBeVisible()
  // The form chooses no scenario and states no criterion: both belong to the one
  // explicit binding, on the strategy itself.
  await expect(createForm.getByLabel('场景', { exact: true })).toHaveCount(0)
  await expect(createForm.getByLabel('准则字段')).toHaveCount(0)
  expect((await read(page, firstId)).experiments).toEqual(launched.experiments)

  // Refreshing the strategy shows the same frozen experiment, on the same run,
  // while the second strategy is still untouched.
  await page.goto(`/strategies/${firstId}`)
  await page.getByRole('tab', { name: '实验' }).click()
  const rereadControls = page.locator('details.experiment-controls').first()
  if (await rereadControls.getAttribute('open') === null) await rereadControls.locator('summary').first().click()
  await expect(rereadControls).toHaveAttribute('open', '')
  await expect(page.getByText(`已从修订 ${boundRevision} 启动实验 li-1`)).toBeVisible()
  await expect(page.getByRole('region', { name: '实验' }).getByRole('link', { name: '查看这次实验报告' }))
    .toHaveAttribute('href', `/strategies/${firstId}?tab=experiments&evidence_a=${experiment.job_id}`)
  const reread = await read(page, firstId)
  expect(reread.experiments).toEqual(launched.experiments)
  expect(reread.draft.revision).toBe(boundRevision)
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

  // The table carries the frozen run on the strategy's own row, at the revision
  // that ran rather than at today's head. The run column is the job's own state,
  // read from that job, and it leads back to this strategy's evidence for it.
  await page.goto('/strategies')
  const table = page.getByRole('navigation', { name: '策略列表' })
  await expect(table.getByText(`冻结 v${boundRevision}`)).toBeVisible()
  const firstRow = table.locator('tr').filter({ has: page.getByRole('link', { name: 'Trend follow', exact: true }) })
  await expect(firstRow.getByRole('link', { name: '查看正式报告' }))
    .toHaveAttribute('href', `/backtests/${experiment.job_id}?research=${firstId}`)
  // The one history entry leads to the existing read-only classifications.
  await page.getByRole('link', { name: '历史记录' }).click()
  const history = page.getByRole('navigation', { name: '历史记录分类' })
  await expect(history.getByRole('link', { name: '旧研究任务' })).toHaveAttribute('href', '/research')
  await page.screenshot({ path: testInfo.outputPath('strategy-workspace.png'), fullPage: true })

  // A narrow viewport with the real run and the admitted input on screen: the
  // workspace stays inside the viewport instead of scrolling sideways.
  await page.setViewportSize({ width: 420, height: 900 })
  await page.goto(`/strategies/${firstId}`)
  await page.getByRole('tab', { name: '实验' }).click()
  const narrowControls = page.locator('details.experiment-controls').first()
  if (await narrowControls.getAttribute('open') === null) await narrowControls.locator('summary').first().click()
  await expect(narrowControls).toHaveAttribute('open', '')
  await expect(page.getByText(`已从修订 ${boundRevision} 启动实验 li-1`)).toBeVisible()
  await page.getByRole('button', { name: '预览实验' }).click()
  await expect(page.locator('dl.preview-facts')).toContainText(`草稿修订 ${preview.draft_revision}`)
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow, 'the workspace must not scroll sideways at a narrow width').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('strategy-workspace-narrow.png'), fullPage: true })
})

/**
 * The way back from an already-existing formal report to the persisted strategy
 * that froze it.
 *
 * On a workspace that already holds the sample, the three identifiers come from
 * the environment and this test scripts nothing: it reads the real workspace with
 * GET only, follows the link the page itself renders, and re-reads the same
 * documents afterwards.
 *
 *   EA_REPORT_JOB_ID=<persisted experiment job> \
 *   EA_REPORT_STRATEGY_ID=<the strategy that froze it> \
 *   EA_REPORT_OTHER_STRATEGY_ID=<a strategy on the same engine strategy> \
 *   EA_WEB_BASE_URL=http://127.0.0.1:8765 \
 *   npx playwright test e2e/user-strategies.spec.ts -g 'existing report returns'
 *
 * With none of them set — the clean installed harness — this test first creates
 * its own bounded sample through the product's own local API: two distinct named
 * strategies on one registered scenario, then one real experiment of the first
 * one, previewed and explicitly confirmed against exactly that preview, awaited
 * until its persisted job is terminal and its report readable. The seed runs
 * *before* the no-write watchers and the baselines are installed, so what the
 * read-only journey is measured against is the state the seed itself left. No
 * model is called, no provider is configured, no persisted JSON is edited
 * directly and no report is fabricated. A partial environment is a mistake and
 * fails clearly here rather than being guessed at.
 *
 * Either way the recorded call counts, the experiment lists and the two strategy
 * documents are compared with their own baseline before the page is opened, so
 * the claim "the return writes nothing" is checked against this workspace's real
 * state and not against a number this test assumed.
 */
type ReportSample = { jobId: string; strategyId: string; otherStrategyId: string }

/**
 * One deterministic sample, created through the product's own local API.
 *
 * Two distinct named strategies on one registered scenario, then one real
 * experiment of the first one: a preview of exactly what would run, one explicit
 * confirmation of that preview, and a bounded read of the persisted job until it
 * reaches a terminal state. Every identifier this returns is an answer the API
 * itself gave, and the report it leaves behind is the engine's own.
 */
async function seedReportSample(page: Page): Promise<ReportSample> {
  const catalog = await page.request.get('/api/scenarios').then((response) => response.json())
  const scenario = catalog.scenarios.find(
    (item: { valid: boolean; strategy_parameters?: unknown[] }) =>
      item.valid && (item.strategy_parameters?.length ?? 0) > 0,
  )
  expect(scenario, 'the isolated workspace must register one runnable scenario').toBeTruthy()

  const create = async (name: string, intent: string): Promise<StrategyDocument> => {
    const response = await page.request.post('/api/user-strategies', {
      headers: sameOrigin,
      data: { name, intent, scenario_id: scenario.scenario_id },
    })
    expect(response.status(), `creating "${name}" must succeed`).toBe(201)
    return await response.json() as StrategyDocument
  }
  const strategy = await create('回归样本策略', '为返回路径冻结一份真实的正式报告')
  const other = await create('回归样本对照策略', '同一引擎策略上的另一条策略，不拥有这次运行')

  const preview = await page.request
    .get(`/api/user-strategies/${strategy.user_strategy_id}/experiment/preview`)
    .then((response) => response.json()) as Preview
  expect(preview.preview_identity, 'the sample preview must carry the identity a confirmation returns')
    .toMatch(/^sha256:[0-9a-f]{64}$/)
  const launched = await page.request.post(`/api/user-strategies/${strategy.user_strategy_id}/experiments`, {
    headers: sameOrigin,
    data: {
      launch_intent_id: 'sample-report-return',
      base_revision: preview.draft_revision,
      preview_identity: preview.preview_identity,
      approved_budget: { max_model_calls: 0, max_backtests: 1 },
    },
  })
  expect(launched.status(), 'the explicit confirmation must be accepted').toBe(200)
  const experiment = (await read(page, strategy.user_strategy_id)).experiments.at(-1)
  expect(experiment?.job_id, 'the confirmed experiment must have dispatched its run').toBeTruthy()
  const job = await waitForTerminalJob(page, experiment!.job_id!)
  expect(job.status, job.message ?? 'the sample experiment must run').toBe('succeeded')
  const ready = await page.request.get(`/api/backtests/${experiment!.job_id}`).then((response) => response.json())
  expect(ready.report_ready, 'the sample must leave a readable formal report').toBe(true)
  return {
    jobId: experiment!.job_id!,
    strategyId: strategy.user_strategy_id,
    otherStrategyId: other.user_strategy_id,
  }
}

test('existing report returns to its persisted strategy without writes', async ({ page, context }, testInfo) => {
  test.setTimeout(180_000)
  await waitForHealth()
  const suppliedJobId = process.env.EA_REPORT_JOB_ID ?? ''
  const suppliedStrategyId = process.env.EA_REPORT_STRATEGY_ID ?? ''
  const suppliedOtherStrategyId = process.env.EA_REPORT_OTHER_STRATEGY_ID ?? ''
  const supplied = [suppliedJobId, suppliedStrategyId, suppliedOtherStrategyId].filter((value) => value !== '').length
  expect(supplied === 0 || supplied === 3,
    'EA_REPORT_JOB_ID, EA_REPORT_STRATEGY_ID and EA_REPORT_OTHER_STRATEGY_ID must be supplied together or not at all')
    .toBe(true)
  if (supplied === 0) {
    expect(process.env.EA_WEB_BIN, 'automatic fixtures require the isolated installed harness').toBeTruthy()
    expect(process.env.EA_WEB_WORKSPACE, 'automatic fixtures require an isolated workspace').toBeTruthy()
  }
  const sample: ReportSample = supplied === 3
    ? { jobId: suppliedJobId, strategyId: suppliedStrategyId, otherStrategyId: suppliedOtherStrategyId }
    : await seedReportSample(page)
  const { jobId, strategyId, otherStrategyId } = sample
  testInfo.annotations.push({
    type: 'sample',
    description: supplied === 3
      ? `supplied by the environment: job ${jobId}, strategy ${strategyId}`
      : `created through the local API before the read-only journey: job ${jobId}, strategy ${strategyId}`,
  })

  // Every non-GET the pages themselves attempt, on both pages this test opens.
  const nonGet: string[] = []
  const recordNonGet = (target: Page) => target.on('request', (request) => {
    const url = new URL(request.url())
    if (url.origin === new URL(baseURL).origin && url.pathname.startsWith('/api/') && request.method() !== 'GET') {
      nonGet.push(`${request.method()} ${url.pathname}`)
    }
  })
  recordNonGet(page)

  // The records this page must agree with, read before anything is opened: the
  // strategy that froze the job, the strategy that did not, the job and its report.
  const strategy = await read(page, strategyId)
  const other = await read(page, otherStrategyId)
  const job = await page.request.get(`/api/backtests/${jobId}`).then((response) => response.json())
  const report = await page.request.get(`/api/backtests/${jobId}/report`).then((response) => response.json())
  expect(strategy.draft.strategy_id, 'the other strategy must be on the same engine strategy').toBe(other.draft.strategy_id)
  expect(other.experiments.some((item) => item.job_id === jobId), 'the other strategy must not own this job').toBe(false)
  expect(strategy.experiments.length, 'the sample must carry at least one frozen experiment').toBeGreaterThanOrEqual(1)
  const matching = strategy.experiments.filter((item) => item.job_id === jobId)
  expect(matching, 'the supplied sample must freeze this job exactly once').toHaveLength(1)
  const frozen = matching[0].payload.draft_revision
  expect(Number.isInteger(frozen) && frozen > 0, 'the frozen revision must be a real revision').toBe(true)
  expect(job.status, job.message ?? 'the supplied job must have succeeded').toBe('succeeded')
  expect(job.report_ready).toBe(true)
  expect(report.run_id).toBe(job.engine_run_id)

  // The appearance this browser already had, then an explicit dark/compact choice
  // through the existing Settings controls, which must survive the whole journey.
  await page.goto('/settings')
  const appearanceBefore = await page.evaluate(() => window.localStorage.getItem('ea.quine.appearance.v1'))
  const appearanceSettings = page.getByRole('region', { name: '本地外观' })
  await expect(appearanceSettings).toBeVisible()
  await appearanceSettings.getByRole('radio', { name: '深色' }).check()
  await appearanceSettings.getByRole('radio', { name: '紧凑' }).check()
  const appearanceInForce = () => page.evaluate(
    () => [document.documentElement.dataset.theme, document.documentElement.dataset.density],
  )
  expect(await appearanceInForce()).toEqual(['dark', 'compact'])
  await page.reload()
  expect(await appearanceInForce(), 'the appearance choice must survive a reload').toEqual(['dark', 'compact'])
  const appearanceAfter = await page.evaluate(() => window.localStorage.getItem('ea.quine.appearance.v1'))
  const storedAppearance = JSON.parse(appearanceAfter ?? '{}') as { theme?: string; density?: string }
  expect(storedAppearance.theme).toBe('dark')
  expect(storedAppearance.density).toBe('compact')

  // The formal report, opened directly, at the desktop width.
  const association = page.getByRole('region', { name: '策略关联' })
  const returnLink = association.getByRole('link', { name: '返回此策略继续研究' })
  // The way back names the frozen run this report is about, so the strategy opens
  // on that run instead of resetting its evidence to the latest one.
  const returnHref = `/strategies/${strategyId}?evidence_a=${jobId}`
  await page.setViewportSize({ width: 1400, height: 1000 })
  await page.goto(`/backtests/${jobId}`)
  await expect(page.locator('.page-title .status')).toHaveText('已完成')
  await expect(association.getByText(strategy.name, { exact: true })).toBeVisible()
  await expect(association.getByText(`冻结 v${frozen}`)).toBeVisible()
  if (other.name === strategy.name) {
    // Two strategies that share a display name must still be told apart by id.
    expect(otherStrategyId).not.toBe(strategyId)
  } else {
    await expect(association.getByText(other.name, { exact: true })).toHaveCount(0)
  }
  await expect(returnLink).toHaveAttribute('href', returnHref)
  // The formal report stays the report: its own evidence and downloads are intact.
  await page.locator('summary').filter({ hasText: '原始执行字段' }).click()
  await expect(page.getByRole('link', { name: 'Download report.json' })).toBeVisible()
  expect(await appearanceInForce(), 'opening the report must not disturb the appearance').toEqual(['dark', 'compact'])
  await page.screenshot({ path: testInfo.outputPath('report-return-1400.png'), fullPage: true })

  // Following it lands on the very strategy that froze the run, with its own
  // conversation, evidence and AI column intact and the other one never selected.
  const origin = new URL(baseURL).origin
  await returnLink.click()
  await expect(page).toHaveURL(`${origin}${returnHref}`)
  await expect(page.getByRole('region', { name: 'AI 协作' })).toBeVisible()
  await expect(page.getByRole('region', { name: '证据' })).toBeVisible()
  // The conversation on screen is this strategy's own: the newest turn keeps its
  // own prompt and answer in the turn above, and the turns before it stay in the
  // shared message list. Nothing of the other strategy's conversation is here.
  const earlierMessages = strategy.messages.filter(
    (message) => message.call_id !== strategy.calls.at(-1)?.call_id)
  await expect(page.locator('ol.message-list li')).toHaveCount(earlierMessages.length)
  if (earlierMessages.length > 0) {
    await expect(page.locator('ol.message-list li').first()).toContainText(earlierMessages[0].text)
  }
  const contextLabel = page.getByLabel('AI 当前上下文')
  await expect(page.getByRole('region', { name: 'AI 协作' }).locator('.agent-object')).toContainText(strategy.name)
  await expect(contextLabel).toContainText(`草稿 v${strategy.draft.revision}`)
  await expect(contextLabel.getByRole('link', { name: `草稿 v${strategy.draft.revision}`, exact: true })).toHaveAttribute('href', new RegExp(`/strategies/${strategyId}[?]`))
  await expect(contextLabel.locator(`a[href*='${otherStrategyId}']`)).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('report-return-strategy-1400.png'), fullPage: true })

  // A reload of the report shows the same association, read again from the records.
  await page.goto(`/backtests/${jobId}`)
  await page.reload()
  await expect(association.getByText(`冻结 v${frozen}`)).toBeVisible()
  await expect(returnLink).toHaveAttribute('href', returnHref)

  // The narrow width: the same return, still clickable, with no sideways scroll.
  await page.setViewportSize({ width: 420, height: 900 })
  await page.goto(`/backtests/${jobId}`)
  await expect(returnLink).toBeVisible()
  const narrowOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(narrowOverflow, 'the report with its association must not scroll sideways at 420 px').toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('report-return-420.png'), fullPage: true })
  await returnLink.click()
  await expect(page).toHaveURL(`${origin}${returnHref}`)
  await page.getByRole('group', { name: '工作区视图' }).getByRole('button', { name: 'AI 协作', exact: true }).click()
  await expect(page.getByRole('region', { name: 'AI 协作' })).toBeVisible()
  expect(await appearanceInForce(), 'the appearance must survive the navigation').toEqual(['dark', 'compact'])

  // One independent page, never navigated by this test, reaching the same report
  // directly: the association is the records' own, not a carried-over page state.
  const independent = await context.newPage()
  recordNonGet(independent)
  await independent.setViewportSize({ width: 420, height: 900 })
  await independent.goto(`${baseURL}/backtests/${jobId}`)
  const independentAssociation = independent.getByRole('region', { name: '策略关联' })
  await expect(independentAssociation.getByText(strategy.name, { exact: true })).toBeVisible()
  await expect(independentAssociation.getByText(`冻结 v${frozen}`)).toBeVisible()
  await expect(independentAssociation.getByRole('link', { name: '返回此策略继续研究' }))
    .toHaveAttribute('href', returnHref)
  expect(
    await independent.evaluate(() => [document.documentElement.dataset.theme, document.documentElement.dataset.density]),
    'this browser keeps the appearance it saved',
  ).toEqual(['dark', 'compact'])
  await independent.screenshot({ path: testInfo.outputPath('report-return-independent-420.png'), fullPage: true })
  await independent.getByRole('link', { name: '返回此策略继续研究' }).click()
  await expect(independent).toHaveURL(`${origin}${returnHref}`)
  await independent.getByRole('group', { name: '工作区视图' }).getByRole('button', { name: 'AI 协作', exact: true }).click()
  await expect(independent.getByRole('region', { name: 'AI 协作' })).toBeVisible()
  await independent.close()

  // Nothing was written on the way: both documents and both counts are exactly
  // what they were before the report was opened, and no non-GET was attempted.
  const after = await read(page, strategyId)
  const afterOther = await read(page, otherStrategyId)
  expect(after, 'following the report back must not change the strategy').toEqual(strategy)
  expect(afterOther, 'the other strategy must stay untouched').toEqual(other)
  expect(after.experiments, 'the frozen record set must be unchanged').toEqual(strategy.experiments)
  expect(nonGet, 'the return path must issue no write of any kind').toEqual([])
  await testInfo.attach('report-return-evidence', {
    body: JSON.stringify({
      jobId, strategyId, otherStrategyId, frozenRevision: frozen,
      strategy, other, job, report,
      appearanceBefore, appearanceAfter, nonGetRequests: nonGet,
    }, null, 2),
    contentType: 'application/json',
  })
})
