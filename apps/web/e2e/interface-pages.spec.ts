import { expect, test } from '@playwright/test'

// Presentation fixtures only: no model, runtime, account or experiment is started.
// The installed-engine journeys remain in user-strategies and local-backtest.
const id = 'interface-fixture'
const at = '2026-10-23T08:00:00Z'
const model = { provider: 'deepseek', model: 'display-fixture', base_url: null, credential_available: false, configured: false, request_validation: 'not_verified' }
const instance = {
  run_id: id, state: 'stopped', terminal_confirmed: true, stop_requested: true,
  source_job_id: id, created_at: at, observed_at: at,
  request: { request_id: id, candidate_id: id, fingerprint: 'a'.repeat(64), name: '界面验收 · 已停止实例', prices: [100], interval_seconds: 1 },
  overview: { available: true, run_id: id, state: 'stopped', updated_at: at, cash: '10000', equity: '10000', currency: 'USD', position: '0', fills: 0, lease_held: false },
}
const routes = [
  ['/strategies', 'research'], ['/runs', 'paper-list'], ['/runs?view=new', 'paper-new'],
  [`/runs?instance=${id}`, 'paper-overview'], [`/runs?instance=${id}&tab=position`, 'paper-position'], [`/runs?instance=${id}&tab=events`, 'paper-events'],
  ['/live', 'live-list'], ['/live?view=start', 'live-start'], ['/live?view=detail', 'live-detail'],
  ['/backtests', 'backtests'], ['/resources/data', 'data'], ['/resources/extensions', 'templates'], ['/resources/models', 'models'],
  ['/settings?section=appearance', 'appearance'], ['/settings?section=workspace', 'workspace'], ['/settings?section=diagnostics', 'diagnostics'], ['/settings?section=about', 'about'],
  ['/platform', 'platform'], ['/platform/binance', 'binance'], ['/research', 'history'], ['/candidates', 'candidates'], ['/holdouts', 'holdouts'], ['/events', 'events'],
] as const

for (const [theme, width] of [['light', 1400], ['dark', 1400], ['light', 420], ['dark', 420]] as const) {
  test(`secondary interface matrix ${theme} ${width}`, async ({ page }, info) => {
    test.setTimeout(120_000)
    const errors: string[] = []
    const writes: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      if (route.request().method() !== 'GET') { writes.push(route.request().url()); await route.abort(); return }
      await route.fallback()
    })
    await page.route('**/api/desktop/settings', route => route.fulfill({ json: {
      workspace: '/fixture/quine', next_workspace: '/fixture/quine', theme: 'system', startup_error: null,
      model: { ...model, profiles: [{ ...model, id: 'legacy', name: '界面测试连接', revision: 1 }], default_profile_id: 'legacy' },
    } }))
    await page.route('**/api/paper/instances', route => route.fulfill({ json: { available: true, instances: [instance] } }))
    await page.route('**/api/paper/instances/*/conversation', route => route.fulfill({ status: 503, json: { error: '界面测试未配置模型' } }))
    await page.route('**/api/paper/events?*', route => route.fulfill({ json: { available: true, run_id: id, events: [] } }))
    await page.emulateMedia({ colorScheme: theme })
    await page.setViewportSize({ width, height: 1000 })
    for (const [path, name] of routes) {
      await page.goto(path)
      await expect(page.locator('h1').first(), path).toBeVisible()
      await expect(page.getByRole('navigation', { name: '主导航' }), path).toBeVisible()
      if (name === 'models') await expect(page.getByLabel('模型 ID', { exact: true })).toHaveValue('display-fixture')
      if (name === 'templates') await expect(page.getByRole('button', { name: '刷新策略目录' })).toBeEnabled()
      if (name.startsWith('paper-') && !['paper-new', 'paper-list'].includes(name)) await expect(page.locator('h1')).toHaveText(instance.request.name)
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), { message: path }).toBeLessThanOrEqual(1)
      await page.screenshot({ path: info.outputPath(`${name}.png`), fullPage: true })
    }
    expect(errors).toEqual([])
    expect(writes).toEqual([])
  })
}
