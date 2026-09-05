import { expect, test } from '@playwright/test'
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

async function runScenario(page: import('@playwright/test').Page, name: string): Promise<{ runId: string; url: string }> {
  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption(name)
  await page.getByRole('button', { name: 'Validate input' }).click()
  await expect(page.getByText('Validated input')).toBeVisible()
  await page.getByRole('button', { name: 'Run new backtest' }).click()
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]+$/)
  await expect(page.locator('.status')).toHaveText('succeeded', { timeout: 15_000 })
  const runId = await page.locator('dt', { hasText: 'Engine run_id' }).locator('..').locator('dd').innerText()
  return { runId, url: page.url() }
}

test('installed browser completes the bounded local Web backtest loop', async ({ page }, testInfo) => {
  const flat = await runScenario(page, 'flat.yaml')
  await expect(page.getByText('10000 USD')).toHaveCount(3)
  await expect(page.getByText('Orders 0')).toBeVisible()
  await expect(page.getByText('Fills 0')).toBeVisible()

  const bounded = await runScenario(page, 'bounded-long.yaml')
  expect(bounded.runId).not.toBe(flat.runId)
  await expect(page.getByText('9797 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('10017 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('17 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('0.0017', { exact: true })).toBeVisible()
  await expect(page.getByText('101.5', { exact: true })).toBeVisible()
  await expect(page.getByText('110', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('bounded-long-success.png'), fullPage: true })

  await page.reload()
  await expect(page.getByText(bounded.runId)).toBeVisible()
  const reportDownload = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download report.json' }).click()
  expect((await reportDownload).suggestedFilename()).toBe('report.json')

  const changed = await runScenario(page, 'one.yaml')
  expect(changed.runId).not.toBe(bounded.runId)
  await expect(page.getByText('9898.5 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('10008.5 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('8.5 USD', { exact: true })).toBeVisible()
  await expect(page.getByText('0.00085', { exact: true })).toBeVisible()

  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption('invalid.yaml')
  await page.getByRole('button', { name: 'Validate input' }).click()
  await expect(page.getByText(/invalid field 'data\.fingerprint\.sha256'/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run new backtest' })).toBeDisabled()

  const boundary = await page.evaluate(async () => {
    const headers = { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }
    const jobsBefore = await fetch('/api/backtests').then((response) => response.json())
    const invalid = await fetch('/api/scenarios/invalid.yaml/validate', { method: 'POST', headers, body: '{}' })
    const validation = await fetch('/api/scenarios/bounded-long.yaml/validate', { method: 'POST', headers, body: '{}' }).then((response) => response.json())
    const request = { scenario_id: 'bounded-long.yaml', input_identity: validation.input_identity, request_id: 'playwright-idempotency-0001' }
    const first = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const firstBody = await first.json()
    const second = await fetch('/api/backtests', { method: 'POST', headers, body: JSON.stringify(request) })
    const secondBody = await second.json()
    return { before: jobsBefore.jobs.length, invalidStatus: invalid.status, firstStatus: first.status, secondStatus: second.status, firstJob: firstBody.job_id, secondJob: secondBody.job_id }
  })
  expect(boundary.invalidStatus).toBe(422)
  expect(boundary.firstStatus).toBe(202)
  expect(boundary.secondStatus).toBe(200)
  expect(boundary.secondJob).toBe(boundary.firstJob)

  await page.goto(`/backtests/${boundary.firstJob}`)
  await expect(page.locator('.status')).toHaveText('succeeded', { timeout: 15_000 })
  const jobsAfter = await page.evaluate(() => fetch('/api/backtests').then((response) => response.json()))
  expect(jobsAfter.jobs).toHaveLength(boundary.before + 1)

  await page.goto('/backtests')
  await page.getByLabel('Scenario').selectOption('low-cash.yaml')
  await page.getByRole('button', { name: 'Validate input' }).click()
  await page.getByRole('button', { name: 'Run new backtest' }).click()
  await expect(page.locator('.status')).toHaveText('failed', { timeout: 15_000 })
  await expect(page.getByText('No success report is available.')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('low-cash-failure.png'), fullPage: true })

  await page.goto(bounded.url)
  await expect(page.getByText(bounded.runId)).toBeVisible()
  const serverPid = Number(process.env.EA_WEB_SERVER_PID)
  expect(Number.isSafeInteger(serverPid)).toBe(true)
  process.kill(serverPid, 'SIGTERM')
  await waitForProcessExit(serverPid)
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByText(/Local service is unreachable/)).toBeVisible()

  const executable = process.env.EA_WEB_BIN
  const args = JSON.parse(process.env.EA_WEB_ARGS ?? '[]') as string[]
  expect(executable).toBeTruthy()
  let restarted: ChildProcess | undefined
  try {
    restarted = spawn(executable!, args, { stdio: 'ignore' })
    await waitForHealth()
    await page.reload()
    await expect(page.getByText(bounded.runId)).toBeVisible()
    await expect(page.locator('.status')).toHaveText('succeeded')
  } finally {
    if (restarted?.pid) process.kill(restarted.pid, 'SIGTERM')
  }
})
