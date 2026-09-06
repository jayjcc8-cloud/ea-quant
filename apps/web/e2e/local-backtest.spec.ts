import { expect, test, type Page } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'

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
  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption('bounded-long.yaml')
  await expect(page.getByLabel('Initial cash')).toHaveValue('10000')
  await expect(page.getByLabel('Quantity')).toHaveValue('2')
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
  expect(baselineEvidence.input_snapshot.scenario.instrument.symbol).toBe('AAPL')
  expect(baselineEvidence.input_snapshot.identity.scenario_sha256).toMatch(/^[0-9a-f]{64}$/)
  expect(baselineEvidence.input_sha256).toMatch(/^[0-9a-f]{64}$/)

  await useParameters(page, baseline.jobId)
  await expect(page.getByLabel('Initial cash')).toHaveValue('10000')
  await expect(page.getByLabel('Quantity')).toHaveValue('2')
  await page.getByLabel('Quantity').fill('4')
  await expect(page.getByText('Validated input')).toHaveCount(0)
  const changed = await validateAndRun(page)
  expect(changed.runId).not.toBe(baseline.runId)
  await expect(page.getByText('9594 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('10034 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('34 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('0.0034', { exact: true })).toBeVisible()

  await page.goto('/backtests')
  await expect(page.locator('.job-card').first()).toContainText(changed.runId)
  await page.getByLabel(`Select ${baseline.jobId} for comparison`).check()
  await page.getByLabel(`Select ${changed.jobId} for comparison`).check()
  await page.getByRole('button', { name: 'Compare selected runs' }).click()
  await expect(page).toHaveURL(`/backtests/compare/${baseline.jobId}/${changed.jobId}`)
  await expect(page.getByRole('row', { name: /Quantity 2 4 Changed/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10034 USD \+17 USD/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Net P&L 17 USD 34 USD \+17 USD/i })).toBeVisible()
  await expect(page.getByRole('row', { name: /Return 0.17% 0.34% \+0.17 pp/i })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('success-success-comparison.png'), fullPage: true })
  const successComparisonURL = page.url()
  await page.reload()
  await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10034 USD \+17 USD/i })).toBeVisible()

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
      method: 'POST', headers, body: JSON.stringify({ parameters: { initial_cash: '10000', quantity: '2' } }),
    }).then((response) => response.json())
    const freeText = await fetch('/api/backtests', {
      method: 'POST', headers,
      body: JSON.stringify({
        scenario_id: 'bounded-long.yaml', input_identity: validation.input_identity,
        parameters: { initial_cash: '10000', quantity: '2', symbol: 'MSFT' },
        request_id: 'playwright-free-text-0001',
      }),
    })
    const request = {
      scenario_id: 'bounded-long.yaml', input_identity: validation.input_identity,
      parameters: { initial_cash: '10000', quantity: '2' }, request_id: 'playwright-idempotency-0001',
    }
    const first = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const firstBody = await first.json()
    const second = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const secondBody = await second.json()
    return { freeTextStatus: freeText.status, firstStatus: first.status, secondStatus: second.status, firstJob: firstBody.job_id, secondJob: secondBody.job_id }
  })
  expect(boundary.freeTextStatus).toBe(422)
  expect(boundary.firstStatus).toBe(202)
  expect(boundary.secondStatus).toBe(200)
  expect(boundary.secondJob).toBe(boundary.firstJob)
  await page.goto(`/backtests/${boundary.firstJob}`)
  await expect(page.locator('.status')).toHaveText('succeeded', { timeout: 15_000 })

  const reportDownload = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download report.json' }).click()
  expect((await reportDownload).suggestedFilename()).toBe('report.json')

  await page.goto(successComparisonURL)
  await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10034 USD \+17 USD/i })).toBeVisible()
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
    await expect(page.getByRole('row', { name: /Final Equity 10017 USD 10034 USD \+17 USD/i })).toBeVisible()
  } finally {
    if (restarted?.pid) process.kill(restarted.pid, 'SIGTERM')
  }
})
