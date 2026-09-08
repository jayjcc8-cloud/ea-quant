import { expect, test, type Page } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { rmSync } from 'node:fs'
import { join } from 'node:path'

const baseURL = process.env.EA_WEB_BASE_URL ?? 'http://127.0.0.1:8765'

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

async function waitForProcessExit(pid: number): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      process.kill(pid, 0)
    } catch {
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error('installed local Web service did not exit')
}

function jobId(page: Page): string {
  const match = new URL(page.url()).pathname.match(/^\/backtests\/([0-9a-f-]+)$/)
  if (!match) throw new Error(`current URL is not a backtest detail: ${page.url()}`)
  return match[1]
}

async function validateAndRun(page: Page): Promise<{ jobId: string; runId: string; url: string }> {
  await page.getByRole('button', { name: 'Validate input' }).click()
  await expect(page.getByText('Validated input')).toBeVisible()
  await page.getByRole('button', { name: 'Run new backtest' }).click()
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]+$/)
  await expect(page.locator('.status')).toHaveText('succeeded', { timeout: 15_000 })
  const runId = await page.locator('dt', { hasText: 'Engine run_id' }).locator('..').locator('dd').innerText()
  return { jobId: jobId(page), runId, url: page.url() }
}

async function useParameters(page: Page, sourceJobId: string): Promise<void> {
  await page.goto('/backtests')
  await page.getByRole('button', { name: `Use parameters for ${sourceJobId}` }).click()
  await expect(page.getByLabel('Symbol')).toHaveValue('AAPL')
  await expect(page.getByLabel('Symbol')).toHaveAttribute('readonly', '')
}

test('installed browser completes the bounded local Web research loop', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption('bounded-long.yaml')
  await expect(page.getByLabel('Initial cash')).toHaveValue('10000')
  await expect(page.getByLabel('target_quantity')).toHaveValue('2')
  await expect(page.getByLabel('entry_delay_bars')).toHaveValue('0')
  await expect(page.getByLabel('entry_delay_bars')).toHaveAttribute('max', '2')
  const baseline = await validateAndRun(page)
  await expect(page.getByText('9797 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('10017 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('17 USD', { exact: true })).toBeVisible()

  const baselineEvidence = await page.evaluate(async (id) => {
    const response = await fetch(`/api/backtests/${id}`)
    return response.json()
  }, baseline.jobId)
  expect(baselineEvidence.schema).toBe('ea.local-web-job.v2')
  expect(baselineEvidence.input_snapshot.scenario.funding.initial_cash).toBe('10000')
  expect(baselineEvidence.input_snapshot.scenario.strategy.target_quantity).toBe('2')
  expect(baselineEvidence.input_snapshot.scenario.strategy.entry_delay_bars).toBe(0)
  expect(baselineEvidence.input_snapshot.scenario.instrument.symbol).toBe('AAPL')
  expect(baselineEvidence.input_snapshot.identity.scenario_sha256).toMatch(/^[0-9a-f]{64}$/)
  expect(baselineEvidence.input_sha256).toMatch(/^[0-9a-f]{64}$/)

  await useParameters(page, baseline.jobId)
  await expect(page.getByLabel('Initial cash')).toHaveValue('10000')
  await expect(page.getByLabel('target_quantity')).toHaveValue('2')
  await expect(page.getByLabel('entry_delay_bars')).toHaveValue('0')
  await page.getByLabel('target_quantity').fill('4')
  await page.getByLabel('entry_delay_bars').fill('2')
  await expect(page.getByText('Validated input')).toHaveCount(0)
  const changed = await validateAndRun(page)
  expect(changed.runId).not.toBe(baseline.runId)
  await expect(page.locator('.metric-card').filter({ hasText: 'Ending cash' })).toContainText('9560 USD')
  await expect(page.locator('.metric-card').filter({ hasText: 'Equity' })).toContainText('10000 USD')
  await expect(page.locator('.metric-card').filter({ hasText: 'Net P&L' })).toContainText('0 USD')
  await expect(page.locator('.metric-card').filter({ hasText: 'Total return' })).toContainText('0')

  await page.goto('/backtests')
  await expect(page.locator('.job-card').first()).toContainText(changed.runId)
  await page.getByLabel(`Select ${baseline.jobId} for comparison`).check()
  await page.getByLabel(`Select ${changed.jobId} for comparison`).check()
  await page.getByRole('button', { name: 'Compare selected runs' }).click()
  await expect(page).toHaveURL(`/backtests/compare/${baseline.jobId}/${changed.jobId}`)
  await expect(page.getByRole('row', { name: /target_quantity 2 4 \+2/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /entry_delay_bars 0 2 \+2/i })).toBeVisible()
    await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10000 USD -17 USD/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Net P&L 17 USD 0 USD -17 USD/i })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('success-success-comparison.png'), fullPage: true })
  const successComparisonURL = page.url()
  await page.reload()
  await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10000 USD -17 USD/i })).toBeVisible()

  await page.goto('/batches/new')
  await expect(page.getByRole('heading', { name: 'New Experiment Batch' })).toBeVisible()
  await page.getByLabel('Scenario').selectOption('bounded-long.yaml')
  await expect(page.getByLabel('Run 1 entry_delay_bars')).toHaveAttribute('max', '2')
  await page.getByLabel('Run 2 target_quantity').fill('4')
  await page.getByLabel('Run 2 entry_delay_bars').fill('2')
  await page.getByRole('button', { name: 'Add configuration' }).click()
  await page.getByLabel('Run 3 target_quantity').fill('100')
  await page.getByRole('button', { name: 'Run batch' }).click()
  await expect(page).toHaveURL(/\/batches\/[0-9a-f-]+$/)
  const batchURL = page.url()
  await expect(page.locator('.page-title .status')).toHaveText('complete', { timeout: 20_000 })
  await expect(page.getByRole('row', { name: /Run 1 2 0 succeeded 10017 USD 17 USD 0.17% View/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Run 2 4 2 succeeded 10000 USD 0 USD 0% View/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Run 3 100 0 succeeded/i })).toBeVisible()
  const batchEvidence = await page.evaluate(async () => {
    const response = await fetch(new URL(window.location.href).pathname.replace('/batches/', '/api/batches/'))
    return response.json()
  })
  expect(batchEvidence.schema).toBe('ea.local-web-batch.v1')
  expect(batchEvidence.member_count).toBe(3)
  expect(batchEvidence.members.map((member: { status: string }) => member.status)).toEqual(['succeeded', 'succeeded', 'succeeded'])
  expect(batchEvidence.members.map((member: { input_snapshot: { scenario: { strategy: unknown } } }) => member.input_snapshot.scenario.strategy)).toEqual([
    { id: 'bounded-long-v1', target_quantity: '2', entry_delay_bars: 0 },
    { id: 'bounded-long-v1', target_quantity: '4', entry_delay_bars: 2 },
    { id: 'bounded-long-v1', target_quantity: '100', entry_delay_bars: 0 },
  ])
  const workspace = process.env.EA_WEB_WORKSPACE
  expect(workspace).toBeTruthy()
  rmSync(join(workspace!, 'reports', batchEvidence.member_job_ids[2], 'report.json'))
  await page.reload()
  await expect(page.getByRole('row', { name: /Run 3 100 0 succeeded No report View/i })).toBeVisible()
  await expect(page.locator('dd[data-summary="reports"]')).toHaveText('2')
  await page.getByLabel('Sort by').selectOption('net_pnl')
  await page.getByLabel('Direction').selectOption('ascending')
  await expect(page.locator('tbody tr')).toHaveText([/Run 2/, /Run 1/, /Run 3/])
  await page.getByLabel('Direction').selectOption('descending')
  await expect(page.locator('tbody tr')).toHaveText([/Run 1/, /Run 2/, /Run 3/])
  await page.getByLabel('Status filter').selectOption('failed')
  await expect(page.locator('tbody tr')).toHaveCount(0)
  await page.getByLabel('Status filter').selectOption('succeeded')
  await expect(page.locator('tbody tr')).toHaveCount(3)
  await page.getByLabel('Status filter').selectOption('all')
  await page.getByLabel(`Select ${batchEvidence.member_job_ids[0]} for comparison`).check()
  await page.getByLabel(`Select ${batchEvidence.member_job_ids[1]} for comparison`).check()
  await page.getByRole('button', { name: 'Compare selected runs' }).click()
  await expect(page.getByRole('row', { name: /target_quantity 2 4 \+2/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /entry_delay_bars 0 2 \+2/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Net P&L 17 USD 0 USD -17 USD/i })).toBeVisible()

  await useParameters(page, baseline.jobId)
  await page.getByLabel('Initial cash').fill('50')
  await page.getByRole('button', { name: 'Validate input' }).click()
  await expect(page.getByText('Validated input')).toBeVisible()
  await page.getByRole('button', { name: 'Run new backtest' }).click()
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]+$/)
  await expect(page.locator('.page-title .status')).toHaveText('failed', { timeout: 15_000 })
  const rejectedJobId = jobId(page)
  await expect(page.getByText('risk.rejected')).toBeVisible()
  await expect(page.getByText('No success report is available.')).toBeVisible()

  await page.goto(`/backtests/compare/${baseline.jobId}/${rejectedJobId}`)
  await expect(page.getByText('risk.rejected')).toBeVisible()
  await expect(page.getByRole('row', { name: /Final Equity 10017 USD No report —/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Net P&L 17 USD No report —/i })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('success-risk-rejected-comparison.png'), fullPage: true })

  const boundary = await page.evaluate(async () => {
    const headers = { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }
    const validation = await fetch('/api/scenarios/bounded-long.yaml/validate', {
      method: 'POST', headers, body: JSON.stringify({ parameters: {
        initial_cash: '10000', strategy_parameters: { target_quantity: '2', entry_delay_bars: 0 },
      } }),
    }).then((response) => response.json())
    const freeText = await fetch('/api/backtests', {
      method: 'POST', headers,
      body: JSON.stringify({
        scenario_id: 'bounded-long.yaml', input_identity: validation.input_identity,
        parameters: { initial_cash: '10000', quantity: '2', symbol: 'MSFT' },
        request_id: 'playwright-free-text-0001',
      }),
    })
    const unknownStrategyParameter = await fetch('/api/scenarios/bounded-long.yaml/validate', {
      method: 'POST', headers, body: JSON.stringify({ parameters: {
        initial_cash: '10000',
        strategy_parameters: { target_quantity: '2', entry_delay_bars: 0, unknown: 1 },
      } }),
    })
    const request = {
      scenario_id: 'bounded-long.yaml', input_identity: validation.input_identity,
      parameters: {
        initial_cash: '10000', strategy_parameters: { target_quantity: '2', entry_delay_bars: 0 },
      },
      request_id: 'playwright-idempotency-0001',
    }
    const first = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const firstBody = await first.json()
    const second = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const secondBody = await second.json()
    return { freeTextStatus: freeText.status, unknownStrategyStatus: unknownStrategyParameter.status, firstStatus: first.status, secondStatus: second.status, firstJob: firstBody.job_id, secondJob: secondBody.job_id }
  })
  expect(boundary.freeTextStatus).toBe(422)
  expect(boundary.unknownStrategyStatus).toBe(422)
  expect(boundary.firstStatus).toBe(202)
  expect(boundary.secondStatus).toBe(200)
  expect(boundary.secondJob).toBe(boundary.firstJob)
  await page.goto(`/backtests/${boundary.firstJob}`)
  await expect(page.locator('.status')).toHaveText('succeeded', { timeout: 15_000 })

  const reportDownload = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download report.json' }).click()
  expect((await reportDownload).suggestedFilename()).toBe('report.json')

  await page.goto(batchURL)
  await expect(page.getByRole('row', { name: /Run 2 4 2 succeeded 10000 USD 0 USD 0% View/i })).toBeVisible()
  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption('bounded-long-commission.yaml')
  await expect(page.getByText('deterministic-commission-v1 · 100 bps', { exact: true })).toBeVisible()
  const commissioned = await validateAndRun(page)
  await expect(page.getByText('2.03 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('9794.97 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('14.97 USD', { exact: true })).toBeVisible()
  const commissionedReport = await page.request.get(`/api/backtests/${commissioned.jobId}/report`).then(r => r.json())
  expect(commissionedReport.economics.fees.amount).toBe('2.03')
  await page.screenshot({ path: testInfo.outputPath('commission-result.png'), fullPage: true })
  await testInfo.attach('commission-report', { body: JSON.stringify(commissionedReport, null, 2), contentType: 'application/json' })
  const commissionComparison = `/backtests/compare/${baseline.jobId}/${commissioned.jobId}`
  await page.goto(commissionComparison)
  await expect(page.getByRole('row', { name: /Net P&L 17 USD 14.97 USD -2.03 USD/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Fees 0 USD 2.03 USD \+2.03 USD/i })).toBeVisible()
  await page.goto('/batches/new')
  await page.getByLabel('Scenario').selectOption('bounded-long-commission.yaml')
  await page.getByLabel('Run 2 entry_delay_bars').fill('2')
  await page.getByRole('button', { name: 'Run batch' }).click()
  await expect(page.locator('.page-title .status')).toHaveText('complete', { timeout: 20_000 })
  const commissionBatchURL = page.url()
  await page.getByLabel('Sort by').selectOption('net_pnl')
  await page.getByLabel('Direction').selectOption('ascending')
  await expect(page.locator('tbody tr')).toHaveText([/Run 2.*-2.2 USD/, /Run 1.*14.97 USD/])
  await page.screenshot({ path: testInfo.outputPath('commission-batch.png'), fullPage: true })
  const sourceBatchId = new URL(commissionBatchURL).pathname.split('/').at(-1)!
  const sourceBatchBefore = await page.request.get(`/api/batches/${sourceBatchId}`).then(r => r.json())
  const selectedSource = sourceBatchBefore.member_job_ids[0]
  await page.getByRole('row', { name: /Run 1 2 0 succeeded/ }).getByRole('link', { name: 'View' }).click()
  await page.getByRole('link', { name: 'Evaluate chronological holdout' }).click()
  await expect(page.getByText('Frozen from source')).toBeVisible()
  await expect(page.getByRole('spinbutton')).toHaveCount(0)
  await expect(page.locator('dt', { hasText: /^target_quantity$/ }).locator('..').locator('dd')).toHaveText('2')
  await page.getByLabel('Holdout scenario').selectOption('chronological-holdout.yaml')
  await page.screenshot({ path: testInfo.outputPath('holdout-frozen.png'), fullPage: true })
  const holdoutRequest = page.waitForRequest(request => request.url().endsWith('/api/holdouts') && request.method() === 'POST')
  await page.getByRole('button', { name: 'Run chronological holdout' }).click()
  expect((await holdoutRequest).postDataJSON()).toEqual({ source_job_id: selectedSource, scenario_id: 'chronological-holdout.yaml' })
  await expect(page).toHaveURL(/\/holdouts\/[0-9a-f-]+$/)
  const holdoutURL = page.url()
  const validationId = new URL(holdoutURL).pathname.split('/').at(-1)!
  const relation = await page.request.get(`/api/holdouts/${validationId}`).then(r => r.json())
  const oosPanel = page.locator('section').filter({ has: page.getByRole('heading', { name: 'OOS', exact: true }) })
  await expect(oosPanel.getByText('succeeded', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(oosPanel.getByText('2.03 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('Parameter Delta', { exact: true })).toHaveCount(0)
  const sourceJob = await page.request.get(`/api/backtests/${selectedSource}`).then(r => r.json())
  const oosJob = await page.request.get(`/api/backtests/${relation.holdout_job_id}`).then(r => r.json())
  expect(oosJob.input_snapshot.scenario.strategy).toEqual(sourceJob.input_snapshot.scenario.strategy)
  expect(oosJob.attempt_id).not.toBe(sourceJob.attempt_id)
  const oosReport = await page.request.get(`/api/backtests/${relation.holdout_job_id}/report`).then(r => r.json())
  expect(oosReport.economics.fees.amount).toBe('2.03')
  expect(oosReport.economics.net_pnl.amount).toBe('14.97')
  expect(await page.request.get(`/api/batches/${sourceBatchId}`).then(r => r.json())).toEqual(sourceBatchBefore)
  await page.screenshot({ path: testInfo.outputPath('holdout-detail.png'), fullPage: true })
  await testInfo.attach('chronological-holdout-evidence', { body: JSON.stringify({ relation, sourceJob, oosJob, oosReport }, null, 2), contentType: 'application/json' })
  await page.getByRole('link', { name: 'Open IS formal run' }).click()
  await expect(page.getByRole('heading', { name: 'Backtest Result' })).toBeVisible()
  await expect(page.getByText('2.03 USD', { exact: true })).toBeVisible()
  await page.goto(holdoutURL)
  await page.getByRole('link', { name: 'Open OOS formal run' }).click()
  await expect(page.getByRole('heading', { name: 'Backtest Result' })).toBeVisible()
  await expect(page.getByText('14.97 USD', { exact: true })).toBeVisible()
  await page.goto(batchURL)
  await page.goto('/backtests')
  await expect(page.getByLabel('Scenario').locator('option[value="flat.yaml"]')).toHaveCount(0)
  await page.getByLabel('Scenario').selectOption('moving-average-entry.yaml')
  await expect(page.getByLabel('fast_window')).toHaveValue('1')
  await expect(page.getByLabel('slow_window')).toHaveAttribute('max', '5')
  await page.getByLabel('fast_window').fill('2')
  await page.getByLabel('slow_window').fill('3')
  await page.getByLabel('target_quantity').fill('3')
  await page.screenshot({ path: testInfo.outputPath('ma-schema-controls.png'), fullPage: true })
  const ma = await validateAndRun(page)
  await useParameters(page, ma.jobId)
  await expect(page.getByLabel('fast_window')).toHaveValue('2')
  await expect(page.getByLabel('slow_window')).toHaveValue('3')
  await expect(page.getByLabel('target_quantity')).toHaveValue('3')
  const maReplay = await validateAndRun(page)
  expect(maReplay.runId).not.toBe(ma.runId)
  await page.goto('/batches/new')
  await page.getByLabel('Scenario').selectOption('moving-average-entry.yaml')
  for (const member of [1, 2]) {
    await page.getByLabel(`Run ${member} fast_window`).fill('2')
    await page.getByLabel(`Run ${member} slow_window`).fill('3')
    await page.getByLabel(`Run ${member} target_quantity`).fill(String(member + 2))
  }
  await page.getByRole('button', { name: 'Run batch' }).click()
  await expect(page.locator('.page-title .status')).toHaveText('complete', { timeout: 20_000 })
  const maBatchURL = page.url()
  await expect(page.getByRole('columnheader', { name: 'fast_window', exact: true })).toBeVisible()
  await page.getByLabel('Sort by').selectOption('parameter:slow_window')
  const maBatchId = new URL(maBatchURL).pathname.split('/').at(-1)!
  const maBatch = await page.request.get(`/api/batches/${maBatchId}`).then(r => r.json())
  await page.screenshot({ path: testInfo.outputPath('ma-batch-analysis.png'), fullPage: true })
  await page.getByRole('row', { name: /^Run 1 / }).getByRole('link', { name: 'View' }).click()
  await page.getByRole('link', { name: 'Evaluate chronological holdout' }).click()
  await expect(page.locator('dt', { hasText: /^slow_window$/ }).locator('..').locator('dd')).toHaveText('3')
  await page.getByLabel('Holdout scenario').selectOption('moving-average-holdout.yaml')
  await page.getByRole('button', { name: 'Run chronological holdout' }).click()
  await expect(page).toHaveURL(/\/holdouts\/[0-9a-f-]+$/)
  const maHoldoutURL = page.url()
  const maValidationId = new URL(maHoldoutURL).pathname.split('/').at(-1)!
  const maOosPanel = page.locator('section').filter({ has: page.getByRole('heading', { name: 'OOS', exact: true }) })
  await expect(maOosPanel.getByText('succeeded', { exact: true })).toBeVisible({ timeout: 20_000 })
  const maRelation = await page.request.get(`/api/holdouts/${maValidationId}`).then(r => r.json())
  const maOos = await page.request.get(`/api/backtests/${maRelation.holdout_job_id}`).then(r => r.json())
  expect(maOos.parameters).toEqual({ fast_window: 2, slow_window: 3, target_quantity: '3' })
  expect(maOos.engine_run_id).not.toBe(maBatch.members[0].engine_run_id)
  const maReport = await page.request.get(`/api/backtests/${maRelation.holdout_job_id}/report`).then(r => r.json())
  expect(maReport.economics.fees.amount).toBe('3.09')
  await page.screenshot({ path: testInfo.outputPath('ma-holdout.png'), fullPage: true })
  await page.goto(`/backtests/compare/${ma.jobId}/${baseline.jobId}`)
  await expect(page.getByText(/NO_PARAMETER_DELTA/)).toBeVisible()
  await page.goto(batchURL)
  const serverPid = Number(process.env.EA_WEB_SERVER_PID)
  expect(Number.isSafeInteger(serverPid)).toBe(true)
  process.kill(serverPid, 'SIGTERM')
  await waitForProcessExit(serverPid)

  const executable = process.env.EA_WEB_BIN
  const args = JSON.parse(process.env.EA_WEB_ARGS ?? '[]') as string[]
  expect(executable).toBeTruthy()
  let restarted: ChildProcess | undefined
  try {
    restarted = spawn(executable!, args, { stdio: 'ignore' })
    await waitForHealth()
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Experiment Batch' })).toBeVisible()
    await expect(page.locator('.page-title .status')).toHaveText('complete')
    await expect(page.getByRole('row', { name: /Run 1 2 0 succeeded 10017 USD 17 USD 0.17% View/i })).toBeVisible()
    await expect(page.getByRole('row', { name: /Run 2 4 2 succeeded 10000 USD 0 USD 0% View/i })).toBeVisible()
    await expect(page.getByRole('row', { name: /Run 3 100 0 succeeded No report View/i })).toBeVisible()
    await expect(page.locator('dd[data-summary="reports"]')).toHaveText('2')
    await page.goto(successComparisonURL)
    await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10000 USD -17 USD/i })).toBeVisible()
    await page.goto(commissionBatchURL)
    await expect(page.getByRole('row', { name: /Run 1 2 0 succeeded 10014.97 USD 14.97 USD/i })).toBeVisible()
    await page.goto(commissioned.url)
    await expect(page.getByText('2.03 USD', { exact: true })).toBeVisible()
    expect(await page.request.get(`/api/backtests/${commissioned.jobId}/report`).then(r => r.json())).toEqual(commissionedReport)
    await page.goto(commissionComparison)
    await expect(page.getByRole('row', { name: /Net P&L 17 USD 14.97 USD -2.03 USD/i })).toBeVisible()
    await page.goto('/holdouts')
    await page.getByRole('link', { name: validationId, exact: true }).click()
    await expect(page).toHaveURL(holdoutURL)
    await expect(page.getByText('Frozen from source')).toBeVisible()
    await expect(page.locator('section').filter({ has: page.getByRole('heading', { name: 'OOS', exact: true }) }).getByText('2.03 USD', { exact: true })).toBeVisible()
    expect(await page.request.get(`/api/holdouts/${validationId}`).then(r => r.json())).toEqual(relation)
    expect(await page.request.get(`/api/backtests/${relation.holdout_job_id}/report`).then(r => r.json())).toEqual(oosReport)
    expect(await page.request.get(`/api/batches/${sourceBatchId}`).then(r => r.json())).toEqual(sourceBatchBefore)
    await page.goto(maHoldoutURL)
    await expect(page.getByText('Frozen from source')).toBeVisible()
    expect(await page.request.get(`/api/holdouts/${maValidationId}`).then(r => r.json())).toEqual(maRelation)
    expect(await page.request.get(`/api/backtests/${maRelation.holdout_job_id}/report`).then(r => r.json())).toEqual(maReport)
    expect(await page.request.get(`/api/batches/${maBatchId}`).then(r => r.json())).toEqual(maBatch)
    await page.screenshot({ path: testInfo.outputPath('ma-holdout-restarted.png'), fullPage: true })
    await testInfo.attach('ma-research-evidence', { body: JSON.stringify({ ma, maReplay, maBatch, maRelation, maOos, maReport }, null, 2), contentType: 'application/json' })
    await page.screenshot({ path: testInfo.outputPath('holdout-restarted.png'), fullPage: true })
  } finally {
    if (restarted?.pid) process.kill(restarted.pid, 'SIGTERM')
  }
})
