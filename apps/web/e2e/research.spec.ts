/**
 * Installed-engine research workspace flow.
 *
 * The model provider used here is a local scripted fixture that answers the
 * Messages protocol with one hand-written `propose_spec` call. It is NOT real
 * AI and it makes no market judgment; it exists so the real loop can be driven
 * end to end: the real orchestrator persists the task, the real installed
 * engine runs the confirmed spec, and the existing chronological-holdout and
 * candidate pages produce the evidence. Everything after the proposal is the
 * product's own behavior.
 *
 * This spec runs last (alphabetical order, one worker) and leaves no server
 * running; its temp settings file is removed in `finally`.
 */
import { expect, test, type Page } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { createServer, type Server } from 'node:http'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const baseURL = process.env.EA_WEB_BASE_URL ?? 'http://127.0.0.1:8765'
const SCRIPTED_MODEL = 'scripted-proposal-fixture-v1'

test.skip(!process.env.EA_WEB_BIN, 'the installed-engine e2e harness is not running')

/** The scripted proposal: a registered strategy, instrument and parameters. */
const PROPOSAL = {
  strategy_id: 'bounded-long-v1',
  // Match the registered later-window fixture's commission policy.
  scenario_id: 'bounded-long-commission.yaml',
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  initial_cash: '10000',
  rationale: 'Enter once with a two-unit target on the registered scenario window.',
  assumptions: ['The registered scenario bars are the only evidence considered.'],
  // Registered decimal parameters are canonical decimal strings, as in the catalog.
  parameters: { target_quantity: '2', entry_delay_bars: 0 },
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

type ScriptedProvider = {
  requests: { model: string; tool_names: string[] }[]
  settingsPath: string
  close: () => Promise<void>
}

/** A local Messages-protocol fixture plus the settings file that points at it. */
async function startScriptedProvider(): Promise<ScriptedProvider> {
  const requests: ScriptedProvider['requests'] = []
  const server: Server = createServer((request, response) => {
    if (request.method !== 'POST' || request.url !== '/v1/messages') {
      response.writeHead(404).end()
      return
    }
    let raw = ''
    request.on('data', (chunk) => { raw += chunk })
    request.on('end', () => {
      const body = JSON.parse(raw) as { model?: string; tools?: { name?: string }[] }
      requests.push({ model: String(body.model ?? ''), tool_names: (body.tools ?? []).map((tool) => String(tool.name)) })
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        id: 'msg-scripted-fixture',
        type: 'message',
        role: 'assistant',
        model: body.model,
        content: [{ type: 'tool_use', id: 'toolu-scripted-fixture', name: 'propose_spec', input: PROPOSAL }],
        stop_reason: 'tool_use',
        usage: { input_tokens: 1234, output_tokens: 321 },
      }))
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('scripted provider did not bind a port')
  const directory = mkdtempSync(join(tmpdir(), 'ea-research-e2e-'))
  const settingsPath = join(directory, 'settings.json')
  writeFileSync(settingsPath, JSON.stringify({
    schema: 'radian.settings.v1',
    model: {
      provider: 'anthropic',
      model: SCRIPTED_MODEL,
      base_url: `http://127.0.0.1:${address.port}`,
      api_key: 'local-fixture-key-not-a-credential',
    },
    research: { max_model_calls: 6, max_backtests: 3 },
  }))
  return {
    requests,
    settingsPath,
    close: async () => {
      await new Promise<void>((resolve) => server.close(() => resolve()))
      rmSync(directory, { recursive: true, force: true })
    },
  }
}

/** Replace the harness server with one that reads the scripted settings file. */
async function startServerWithProvider(settingsPath: string): Promise<ChildProcess> {
  const current = Number(process.env.EA_WEB_SERVER_PID)
  if (Number.isSafeInteger(current)) {
    try { process.kill(current, 'SIGTERM') } catch { /* already gone */ }
    await waitForProcessExit(current)
  }
  const restarted = spawn(process.env.EA_WEB_BIN!, JSON.parse(process.env.EA_WEB_ARGS ?? '[]') as string[], {
    stdio: 'ignore',
    env: { ...process.env, RADIAN_SETTINGS_FILE: settingsPath },
  })
  await waitForHealth()
  return restarted
}

async function stopServer(server: ChildProcess): Promise<void> {
  if (!server.pid) return
  try { process.kill(server.pid, 'SIGTERM') } catch { /* already gone */ }
  await waitForProcessExit(server.pid)
}

async function draftIdea(page: Page, idea: string): Promise<string> {
  await page.goto('/research')
  await page.getByLabel('Research idea').fill(idea)
  await page.getByRole('button', { name: 'Create draft' }).click()
  await expect(page).toHaveURL(/\/research\/[0-9a-f-]{36}$/)
  await expect(page.getByRole('heading', { name: 'Draft specification' })).toBeVisible()
  return page.url()
}

test('research workspace reaches an evaluated candidate from a scripted proposal', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const provider = await startScriptedProvider()
  let server = await startServerWithProvider(provider.settingsPath)
  try {
    const taskURL = await draftIdea(page, 'Test a two-unit entry on the registered AAPL scenario')
    await expect(page.locator('.page-title .status')).toHaveText('awaiting confirmation')
    // One scripted proposal, and it was the only provider call: drafting is explicit.
    expect(provider.requests).toHaveLength(1)
    expect(provider.requests[0].tool_names).toContain('propose_spec')

    const draft = page.locator('section[aria-label="Specification"]')
    await expect(draft.getByText('bounded-long-v1')).toBeVisible()
    await expect(draft.getByText('XNAS:AAPL')).toBeVisible()
    await expect(draft.getByText('Enter once with a two-unit target on the registered scenario window.')).toBeVisible()
    await expect(draft.getByText('The registered scenario bars are the only evidence considered.')).toBeVisible()
    await expect(draft.getByText('Unconfirmed draft')).toBeVisible()

    // Model configuration comes from the backend projection: protocol, endpoint and capabilities.
    const model = page.locator('section[aria-label="Model provider configuration"]')
    await expect(model.getByText('Configured', { exact: true })).toBeVisible()
    await expect(model.getByText('anthropic-messages')).toBeVisible()
    await expect(model.getByText(SCRIPTED_MODEL)).toBeVisible()
    await expect(model.getByText('tool_use · declared')).toBeVisible()

    // Frozen budget from the settings file, measured usage from the provider response.
    const usage = page.locator('section[aria-label="Frozen budget and measured usage"]')
    await expect(usage.getByText('1 of 6')).toBeVisible()
    await expect(usage.getByText('0 of 3')).toBeVisible()
    await expect(usage.getByText('1234')).toBeVisible()
    await expect(usage.getByText('321')).toBeVisible()
    await expect(usage.getByText(new RegExp(`${SCRIPTED_MODEL} · observed`))).toBeVisible()

    // A refresh reloads persisted state and never confirms, retries or re-dispatches.
    await page.reload()
    await expect(page.locator('.page-title .status')).toHaveText('awaiting confirmation')
    await expect(page.getByRole('button', { name: 'Confirm spec and start real backtest' })).toBeVisible()
    expect(provider.requests).toHaveLength(1)

    // Returning from the list also reloads the same persisted draft.
    await page.goto('/research')
    await page.goto(taskURL)
    await expect(page.locator('.page-title .status')).toHaveText('awaiting confirmation')
    const duplicate = await page.request.post(`${baseURL}/api/research/tasks`, {
      headers: { Origin: baseURL, 'X-EA-Web-Request': '1' },
      data: { idea: 'Test a two-unit entry on the registered AAPL scenario' },
    })
    expect(duplicate.status()).toBe(409)
    expect(provider.requests).toHaveLength(1)

    await page.getByRole('button', { name: 'Confirm spec and start real backtest' }).click()
    await expect(page.locator('.page-title .status')).toHaveText('backtest succeeded', { timeout: 30_000 })
    await expect(page.getByRole('heading', { name: 'Confirmed specification' })).toBeVisible()

    const jobCard = page.locator('section[aria-label="Research backtests"] .job-card')
    await expect(jobCard.locator('.status')).toHaveText('succeeded')
    const jobId = (await jobCard.getByRole('link', { name: 'Open job and report' }).getAttribute('href'))!.split('/').pop()!
    await expect(jobCard.getByRole('link', { name: 'Download report.json' })).toHaveAttribute('href', `/api/backtests/${jobId}/artifacts/report.json`)
    await expect(jobCard.getByRole('link', { name: 'Evaluate chronological holdout' })).toHaveAttribute('href', `/holdouts/new/${jobId}`)

    // Existing chronological holdout page, real later-window scenario.
    await jobCard.getByRole('link', { name: 'Evaluate chronological holdout' }).click()
    await expect(page).toHaveURL(new RegExp(`/holdouts/new/${jobId}$`))
    await page.getByLabel('Holdout scenario').selectOption('chronological-holdout.yaml')
    await page.getByRole('button', { name: 'Run chronological holdout' }).click()
    await expect(page).toHaveURL(/\/holdouts\/[0-9a-f-]+$/)
    await expect(page.getByText('Frozen from source')).toBeVisible()
    const validationId = page.url().split('/').pop()!
    const oos = page.locator('section').filter({ has: page.getByRole('heading', { name: 'OOS', exact: true }) })
    await expect(oos.locator('dt', { hasText: 'Final equity' }).locator('..').locator('dd')).toHaveText(/USD/, { timeout: 30_000 })

    // The research task now points at that holdout from persisted state only.
    await page.goto(taskURL)
    await expect(jobCard.getByRole('link', { name: `Open holdout ${validationId}` })).toBeVisible()
    await expect(jobCard.getByRole('link', { name: 'Create evaluated candidate' })).toHaveAttribute('href', `/backtests/${jobId}`)
    await expect(page.getByText(/Candidate acceptance records a research judgment only/)).toBeVisible()

    // Existing candidate page: creation verifies both runs and stays a human decision.
    await page.goto(`/backtests/${jobId}`)
    await page.getByLabel('Candidate Holdout').selectOption(validationId)
    await page.getByRole('button', { name: 'Create evaluated candidate' }).click()
    await expect(page).toHaveURL(/\/candidates\/[0-9a-f-]+$/)
    const candidateURL = page.url()
    await expect(page.getByRole('heading', { name: 'Research decision' })).toBeVisible()
    await expect(page.getByText('EVALUATED', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Accept candidate' })).toBeDisabled()
    await expect(page.getByText(/ACCEPTED retains a research judgment and grants no execution permission/)).toBeVisible()

    // Actually restart the source service, then reopen the same evidence.
    await stopServer(server)
    server = await startServerWithProvider(provider.settingsPath)
    await page.goto(candidateURL)
    await expect(page.getByText('EVALUATED', { exact: true })).toBeVisible()
    await page.goto(taskURL)
    await expect(page.locator('.page-title .status')).toHaveText('backtest succeeded')
    await expect(jobCard.locator('.status')).toHaveText('succeeded')
    await expect(usage.getByText('1 of 6')).toBeVisible()
    await expect(usage.getByText('1 of 3')).toBeVisible()
    expect(provider.requests).toHaveLength(1)
    await testInfo.attach('research-workspace-evidence', {
      body: JSON.stringify({ task_id: taskURL.split('/').pop(), job_id: jobId, validation_id: validationId, candidate_id: candidateURL.split('/').pop(), model_validation: 'scripted Messages response only', computation: 'real local source backtest and holdout', provider_calls: provider.requests }, null, 2),
      contentType: 'application/json',
    })
    await page.screenshot({ path: testInfo.outputPath('research-workspace.png'), fullPage: true })
  } finally {
    await stopServer(server)
    await provider.close()
  }
})

test('cancel is final and claims no rollback', async ({ page }) => {
  const provider = await startScriptedProvider()
  const server = await startServerWithProvider(provider.settingsPath)
  try {
    const taskURL = await draftIdea(page, 'Test a cancelled plan that must stop before any backtest')
    await page.getByRole('button', { name: 'Cancel task' }).click()

    await expect(page.locator('.page-title .status')).toHaveText('cancelled')
    await expect(page.getByText(/This task is cancelled and final/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Confirm spec and start real backtest' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Cancel task' })).toHaveCount(0)
    await expect(page.locator('section[aria-label="Research backtests"]')).toHaveCount(0)

    await page.goto(taskURL)
    await expect(page.locator('.page-title .status')).toHaveText('cancelled')
    await expect(page.getByRole('button', { name: 'Retry failed step' })).toHaveCount(0)
    expect(provider.requests).toHaveLength(1)

    await page.goto('/research')
    const card = page.locator('.job-card').filter({ hasText: 'Test a cancelled plan that must stop before any backtest' })
    await expect(card.locator('.status')).toHaveText('cancelled')
    await expect(card.getByText(/jobs 0/)).toBeVisible()
  } finally {
    await stopServer(server)
    await provider.close()
  }
})
