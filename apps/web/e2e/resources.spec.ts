/**
 * QUINE 资源中心 — the resource directory against a running workspace.
 *
 *   EA_WEB_BASE_URL=http://127.0.0.1:8765 \
 *   npx playwright test e2e/resources.spec.ts
 *
 * Nothing here is mocked. Every directory on the page is compared with the
 * response the workspace served for it, and the composition the page sends is
 * first put to the host's own resolve endpoint, so the assertion is
 * correspondence with the host rather than a guess about what this machine
 * should contain — and the two compositions this spec does know the answer for
 * (a declared builtin strategy, and a model layer with no configuration) are
 * asserted hard rather than accepted either way.
 *
 * The one write this page has is that explicit composition check, and this spec
 * guards everything else: a request made by the page to `/api/**` that is not a
 * GET, a HEAD, or a POST to `/api/plugins/recipe/resolve` is aborted and
 * recorded, and the spec closes by asserting nothing was ever recorded there.
 * The probes and the before/after reads use the test's own request context, so
 * what is counted is exactly what the page sent.
 *
 * The three reference sources are read before and after every choice, refresh,
 * verification and navigation. `GET /api/research/tasks` is not a pure read in
 * this source: it reconciles operations, which touches exactly three per-task
 * metadata fields — `operation_generation`, `operation_completion_generation`
 * and `updated_at`. Those three are therefore excluded from the comparison with
 * their before and after values recorded in the attachment; every other field of
 * every task — budget, usage, spent_calls, selection, pinned binding, confirmed
 * spec, jobs — and the whole strategy and job documents must come back
 * identical. So must a frozen reference's identity, before and after a
 * selection, a catalog refresh and a trip to the page a reference links to.
 *
 * The page states what it is: 本主机已声明资源的只读目录，以及组合授权校验。 It composes
 * nothing, saves nothing, applies nothing to a task and starts no run.
 *
 * What this accepts is the behaviour of the workspace running at that base URL,
 * served from editable source or from an installed build. It is not, by itself,
 * evidence that a packaged distribution was installed.
 */
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'

const baseURL = process.env.EA_WEB_BASE_URL ?? 'http://127.0.0.1:8765'

const sameOrigin = { Origin: baseURL, 'X-EA-Web-Request': '1', 'Content-Type': 'application/json' }

/** The one endpoint this page may write to: a verdict, which dispatches nothing. */
const RECIPE_PATH = '/api/plugins/recipe/resolve'

/** The appearance document the workspace already keeps; see src/appearance.ts. */
const APPEARANCE_KEY = 'ea.quine.appearance.v1'

/** The five layer keys, the label each one carries, and the declared kinds it binds. */
const LAYERS: { key: string; label: string; kinds: string[] }[] = [
  { key: 'model', label: '模型层', kinds: ['model'] },
  { key: 'strategy', label: '策略层', kinds: ['strategy'] },
  { key: 'data', label: '数据层', kinds: ['data'] },
  { key: 'validator', label: '校验层', kinds: ['validator'] },
  { key: 'backtest', label: '回测层', kinds: ['backtest-method', 'backtest'] },
]

type CatalogEntry = {
  plugin_id: string
  kind: string
  package_version: string
  usable: boolean
  artifact_digest?: string
  identity?: Record<string, unknown>
  reason?: { code: string; message: string } | null
}

type Selection = Record<string, { plugin_id: string; package_version: string }>

type Binding = { binding_sha256: string; components: { layer: string; plugin_id: string; package_version: string }[] }

type Resolution = {
  usable: boolean
  code: string | null
  message: string | null
  selection: Selection | null
  binding: Binding | null
}

type Settings = {
  configured?: boolean
  credential_available?: boolean
  provider?: string | null
  protocol?: string | null
}

type TaskPayload = { tasks?: Record<string, unknown>[] }

type Sources = {
  scenarios: { scenario_id: string }[]
  datasets: { dataset_id: string }[]
  plugins: CatalogEntry[]
  settings: Settings
  tasks: TaskPayload
  strategies: unknown
  jobs: unknown
}

/** The three per-task fields a reconcile may touch, and nothing else. */
const RECONCILE_METADATA = ['operation_generation', 'operation_completion_generation', 'updated_at'] as const

async function waitForHealth(): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`${baseURL}/api/health`)
      if (response.ok) return
    } catch {
      // The server may still be starting; retry within the bounded loop.
    }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error('service did not become available')
}

/**
 * Refuses every write by the page but the one composition verification. A GET or
 * a HEAD is passed through untouched, so the page reads the real workspace.
 */
async function guardApi(page: Page) {
  const refused: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    const method = request.method()
    if (method === 'GET' || method === 'HEAD' || (method === 'POST' && pathname === RECIPE_PATH)) {
      await route.continue()
      return
    }
    refused.push(`${method} ${pathname}`)
    await route.abort('blockedbyclient')
  })
  return () => refused
}

/** Every API request the page made, with the body it sent. */
function watchApi(page: Page) {
  const seen: { method: string; path: string; body: string | null }[] = []
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith('/api/')) return
    seen.push({ method: request.method(), path: pathname, body: request.postData() })
  })
  return seen
}

const writesOnly = (seen: { method: string; path: string }[]) =>
  seen.filter((entry) => entry.method !== 'GET' && entry.method !== 'HEAD')

const writeList = (seen: { method: string; path: string }[]) =>
  writesOnly(seen).map((entry) => `${entry.method} ${entry.path}`)

const json = (request: APIRequestContext, path: string) => request.get(path).then((response) => response.json())

const readSources = async (request: APIRequestContext): Promise<Sources> => ({
  scenarios: (await json(request, '/api/scenarios')).scenarios ?? [],
  datasets: (await json(request, '/api/datasets')).datasets ?? [],
  plugins: (await json(request, '/api/plugins/catalog')).plugins ?? [],
  settings: await json(request, '/api/settings/status'),
  tasks: await json(request, '/api/research/tasks'),
  strategies: await json(request, '/api/user-strategies'),
  jobs: await json(request, '/api/backtests'),
})

/** The host's own verdict for one composition, which is what the page must repeat. */
const probe = (request: APIRequestContext, selection: Selection): Promise<Resolution> =>
  request
    .post(RECIPE_PATH, { headers: sameOrigin, data: { selection } })
    .then((response) => response.json() as Promise<Resolution>)

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)

/**
 * The tasks as served, split into the part that must be identical and the three
 * metadata fields a reconcile is allowed to move, recorded on their own so the
 * difference is reported rather than hidden behind an all-or-nothing comparison.
 */
function splitTasks(payload: TaskPayload | undefined) {
  const list = payload?.tasks ?? []
  return {
    reduced: list.map((task) => {
      const kept: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(task)) {
        if ((RECONCILE_METADATA as readonly string[]).includes(key)) continue
        kept[key] = value
      }
      return kept
    }),
    metadata: list.map((task) => ({
      task_id: task.task_id ?? null,
      operation_generation: task.operation_generation ?? null,
      operation_completion_generation: task.operation_completion_generation ?? null,
      updated_at: task.updated_at ?? null,
    })),
  }
}

const metadataChanges = (
  before: ReturnType<typeof splitTasks>['metadata'],
  after: ReturnType<typeof splitTasks>['metadata'],
) => {
  const byId = new Map(after.map((entry) => [String(entry.task_id), entry]))
  return before
    .map((entry) => ({ before: entry, after: byId.get(String(entry.task_id)) ?? null }))
    .filter((pair) => JSON.stringify(pair.before) !== JSON.stringify(pair.after))
}

/** Poll a plain condition, so a keyboard activation can be observed rather than assumed. */
async function pollTrue(check: () => boolean, timeout = 3000): Promise<boolean> {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (check()) return true
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  return check()
}

/**
 * Choose one layer the way a reader with only a keyboard would: focus the native
 * select, open its menu, walk it to the option and commit. The popup is drawn by
 * the platform, so the value it actually left behind is what is asserted — and
 * when a headless run withholds the popup, that is recorded and the choice is
 * made through the select API instead, with the keyboard claim never made for it.
 */
async function chooseLayer(page: Page, testInfo: TestInfo, label: string, wanted: string): Promise<void> {
  const select = page.getByLabel(label)
  await select.focus()
  const index = await select.evaluate(
    (node, value) => [...(node as HTMLSelectElement).options].findIndex((option) => option.value === value),
    wanted,
  )
  await page.keyboard.press('Space')
  await page.keyboard.press('Home')
  for (let step = 0; step < Math.max(index, 0); step += 1) await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  const landed = await select.inputValue()
  if (landed === wanted) {
    testInfo.annotations.push({
      type: 'keyboard',
      description: `${label}: Space, Home, ${Math.max(index, 0)}×ArrowDown, Enter selected ${wanted}`,
    })
    return
  }
  testInfo.annotations.push({
    type: 'keyboard-limitation',
    description: `${label}: the native select popup did not commit in this headless run ` +
      `(Space, Home, ${Math.max(index, 0)}×ArrowDown, Enter left "${landed}"); the native control is kept and the choice is made through selectOption`,
  })
  await select.selectOption(wanted)
  expect(await select.inputValue(), 'the control must carry the value the request will send').toBe(wanted)
}

/**
 * Activate the page's one action from the keyboard, and report honestly whether
 * the key press is what did it.
 */
async function pressVerify(page: Page, testInfo: TestInfo, seen: { method: string; path: string }[]): Promise<void> {
  const before = writeList(seen).length
  const button = page.getByRole('button', { name: '校验组合' })
  await button.focus()
  await page.keyboard.press('Enter')
  if (await pollTrue(() => writeList(seen).length > before)) {
    testInfo.annotations.push({ type: 'keyboard', description: '校验组合 was activated with Enter on the focused button' })
    return
  }
  testInfo.annotations.push({
    type: 'keyboard-limitation',
    description: 'Enter on the focused 校验组合 button produced no request in this run; the button was clicked instead',
  })
  await button.click()
}

test('资源中心 repeats the real workspace, composes only on the explicit action, and writes nothing else', async ({ page, request }, testInfo) => {
  test.setTimeout(180_000)
  await waitForHealth()
  const refusedWrites = await guardApi(page)
  const seen = watchApi(page)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => { pageErrors.push(String(error)) })
  const evidence: Record<string, unknown> = {}

  try {
    const before = await readSources(request)
    const tasksBefore = splitTasks(before.tasks)
    const modelPlugins = before.plugins.filter((entry) => entry.kind === 'model')
    const otherPlugins = before.plugins.filter((entry) => entry.kind !== 'model')
    const optionsFor = (kinds: string[]) => before.plugins.filter((entry) => kinds.includes(entry.kind))

    await page.emulateMedia({ colorScheme: 'light' })
    await page.setViewportSize({ width: 1400, height: 1000 })
    await page.goto('/resources/diagnostics')
    await expect(page.getByRole('heading', { name: '资源中心' })).toBeVisible()

    // What the page is: a read of the workspace with one compatibility check. It
    // claims no read-only guarantee beyond that, because reading the task index
    // reconciles operations in this source.
    await expect(page.locator('.resources-page')).toContainText('本主机已声明资源的只读目录，以及组合授权校验。')
    await expect(page.locator('.resources-page')).not.toContainText('全部只读')

    // Every directory on the page is the response the workspace just served it:
    // the same entries, in the same order, under the same identifiers.
    await expect(page.locator('[data-resource-entry="scenario"]')).toHaveCount(before.scenarios.length)
    await expect(page.locator('[data-resource-entry="dataset"]')).toHaveCount(before.datasets.length)
    await expect(page.locator('[data-resource-entry="model"]')).toHaveCount(modelPlugins.length)
    await expect(page.locator('[data-resource-entry="plugin"]')).toHaveCount(otherPlugins.length)
    if (before.scenarios.length) {
      await expect(page.locator('[data-resource-entry="scenario"]').first())
        .toHaveAttribute('data-id', before.scenarios[0].scenario_id)
    }
    if (before.datasets.length) {
      await expect(page.locator('[data-resource-entry="dataset"]').first())
        .toHaveAttribute('data-id', before.datasets[0].dataset_id)
    }
    if (modelPlugins.length) {
      await expect(page.locator('[data-resource-entry="model"]').first())
        .toHaveAttribute('data-id', modelPlugins[0].plugin_id)
    }

    // Host admissibility, configuration, credential presence and the two things
    // this page has never observed are five separate facts, and the first two are
    // the workspace's own answers rather than the page's opinion. A workspace that
    // reports no credential has not refused one: that stays unknown.
    const credentialFact = before.settings.credential_available === true
      ? '是'
      : before.settings.credential_available === false ? '否' : '未知'
    await expect(page.locator('[data-resource-fact="host-admissible"]'))
      .toHaveText(modelPlugins.some((entry) => entry.usable) ? '是' : '否')
    await expect(page.locator('[data-resource-fact="configured"]')).toHaveText(before.settings.configured ? '是' : '否')
    await expect(page.locator('[data-resource-fact="credential"]')).toHaveText(credentialFact)
    await expect(page.locator('[data-resource-fact="connection"]')).toHaveText('未验证')
    await expect(page.locator('[data-resource-fact="request-validation"]')).toHaveText('未验证')

    // A configured provider belongs to the wrappers whose own identity declares
    // it, and to no others: another provider's wrapper is not 已配置 here, and a
    // wrapper that names no provider cannot be attributed to the configured one.
    const providerBadges: Record<string, string> = {}
    for (const model of modelPlugins) {
      const chips = page.locator(`[data-resource-entry="model"][data-id="${model.plugin_id}"] .resource-chip`)
      const texts = await chips.allTextContents()
      const badge = texts.find((text) => text === '已配置' || text === '未配置' || text === '配置未知') ?? 'no-badge'
      providerBadges[model.plugin_id] = badge
      const declared = typeof model.identity?.provider === 'string' ? model.identity.provider : null
      if (before.settings.configured !== true) {
        expect(badge, `${model.plugin_id} must not claim a configuration this workspace does not have`).not.toBe('已配置')
      } else if (declared && before.settings.provider) {
        expect(badge, `${model.plugin_id} declares ${declared}; the workspace configures ${before.settings.provider}`)
          .toBe(declared === before.settings.provider ? '已配置' : '未配置')
      } else {
        expect(badge, `${model.plugin_id} names no provider and must stay unknown`).toBe('配置未知')
      }
    }
    evidence['provider_badges'] = providerBadges

    // No credential form, and no claim that anything is connected.
    await expect(page.locator('input[type="password"]')).toHaveCount(0)
    await expect(page.locator('.resources-page')).not.toContainText('已连接')
    await expect(page.locator('.resources-page')).not.toContainText('请求能力已验证')

    // The directory is compact rows: every entry's long evidence is folded away
    // until a reader asks for it, and asking is a keyboard action on a native
    // disclosure rather than something this page scripted.
    await expect(page.locator('.resources-page .resource-details[open]')).toHaveCount(0)
    const entryHeights = await page.locator('[data-resource-entry]').evaluateAll(
      (nodes) => nodes.map((node) => node.getBoundingClientRect().height))
    evidence['closed_page_height'] = await page.evaluate(() => document.documentElement.scrollHeight)
    evidence['max_closed_entry_height'] = entryHeights.length ? Math.max(...entryHeights) : null
    expect(entryHeights.length, 'the directory must render its entries').toBeGreaterThan(0)
    expect(Math.max(...entryHeights), 'one directory entry is a compact row, not a card').toBeLessThan(160)
    if (modelPlugins.length) {
      const entry = page.locator(`[data-resource-entry="model"][data-id="${modelPlugins[0].plugin_id}"]`)
      const summary = entry.locator('details > summary').first()
      const digest = entry.locator('dd', { hasText: modelPlugins[0].artifact_digest ?? '' }).first()
      if (modelPlugins[0].artifact_digest) {
        await expect(digest).toHaveCount(1)
        await expect(digest, 'the digest is folded away until asked for').toBeHidden()
        await summary.focus()
        await page.keyboard.press('Enter')
        await expect(entry.locator('details')).toHaveAttribute('open', '')
        await expect(digest, 'and visible when the reader asks').toBeVisible()
        await page.keyboard.press('Enter')
        await expect(entry.locator('details')).not.toHaveAttribute('open', '')
      }
    }

    // A frozen reference shows its kind, its scenario and its real links in the
    // open, and keeps the rest of its identity one disclosure down.
    const referenceNodes = page.locator('[data-resource="reference"]')
    if (await referenceNodes.count()) {
      const first = referenceNodes.first()
      await expect(first.locator('.resource-chip').first()).toBeVisible()
      await expect(first.locator('details')).not.toHaveAttribute('open', '')
    }

    // The reference sources are named as what this read did with each of them.
    for (const [reference, path] of [
      ['source-strategies', '/api/user-strategies'],
      ['source-tasks', '/api/research/tasks'],
      ['source-jobs', '/api/backtests'],
    ] as const) {
      expect((await request.get(path)).status(), `${path} must answer for this correspondence`).toBe(200)
      await expect(page.locator(`[data-ref="${reference}"]`)).toContainText('已读取')
    }

    // Reading the page asked the host for nothing but reads.
    expect(writeList(seen), 'loading the directory must not write anything').toEqual([])

    // The search narrows the directories it sits next to, typed on the keyboard,
    // and it must not reach the references, which come from records, not queries.
    const referencesBeforeSearch = await referenceNodes.count()
    const search = page.getByLabel('搜索资源')
    const realQuery = before.scenarios[0]?.scenario_id ?? before.plugins[0]?.plugin_id ?? ''
    if (realQuery) {
      await search.click()
      await page.keyboard.type(realQuery)
      const shown = await page.locator('[data-resource-entry]').evaluateAll(
        (nodes) => nodes.map((node) => node.getAttribute('data-id') ?? ''))
      expect(shown, 'the search must keep the entry it names').toContain(realQuery)
    }
    await search.fill('no-such-resource-in-this-workspace')
    for (const [region, count] of [
      ['模型与协议封装', modelPlugins.length],
      ['已声明能力', otherPlugins.length],
      ['数据集', before.datasets.length],
      ['场景', before.scenarios.length],
    ] as const) {
      if (!count) continue
      const section = page.getByRole('region', { name: region })
      await expect(section).toContainText('没有匹配当前搜索的资源')
      await expect(section.locator('[data-resource-entry]')).toHaveCount(0)
    }
    await expect(referenceNodes).toHaveCount(referencesBeforeSearch)
    await page.getByRole('button', { name: '清除搜索' }).click()
    await expect(page.locator('[data-resource-entry="scenario"]')).toHaveCount(before.scenarios.length)
    expect(writeList(seen), 'filtering must not write anything').toEqual([])

    // One frozen reference's identity, as the page reads it. Nothing the page
    // offers may change it: the reference comes from records, not from choices.
    // A workspace whose three reference sources are all empty has no such
    // reference, and that is asserted as its own case rather than waited for.
    const referenceSourcesEmpty = [
      (before.strategies as { strategies?: unknown[] } | null)?.strategies ?? [],
      before.tasks?.tasks ?? [],
      (before.jobs as { jobs?: unknown[] } | null)?.jobs ?? [],
    ].every((list) => list.length === 0)
    if (referenceSourcesEmpty) {
      await expect(referenceNodes, 'a workspace with no records has no reference to observe').toHaveCount(0)
      await expect(page.getByRole('region', { name: '可观察到的已知引用' }))
        .toContainText('已读取的来源中没有已知引用')
    }
    const frozen = referenceNodes.filter({ has: page.locator('a[href^="/backtests/"]') }).first()
    const frozenKey = (await frozen.count()) ? await frozen.getAttribute('data-key') : null
    const frozenIdentity = frozenKey ? await referenceIdentity(page, frozenKey) : null
    evidence['frozen_reference'] = { key: frozenKey, identity: frozenIdentity }

    // The pages a reference links to are the workspace's own pages: following one
    // reads it, and coming back leaves the frozen identity exactly as it was.
    const followed: string[] = []
    if (frozenKey) {
      for (const prefix of ['/backtests/', '/research/', '/strategies/']) {
        const link = page.locator(`[data-resource="reference"] a[href^="${prefix}"]`).first()
        if ((await link.count()) === 0) continue
        const href = await link.getAttribute('href')
        if (!href) continue
        await link.click()
        await expect.poll(() => new URL(page.url()).pathname).toBe(href)
        await expect(page.locator('h1, h2').first()).toBeVisible()
        await page.goBack()
        await expect(page.getByRole('heading', { name: '资源中心' })).toBeVisible()
        await expect.poll(() => referenceIdentity(page, frozenKey)).toBe(frozenIdentity)
        followed.push(href)
      }
    }
    evidence['followed_reference_links'] = followed

    // A catalog refresh is a re-read of the same directory, and it leaves the
    // frozen identity where it was.
    await page.getByRole('button', { name: '刷新能力目录' }).click()
    await expect(page.locator('[data-resource-entry="plugin"]')).toHaveCount(otherPlugins.length)
    expect(writeList(seen), 'a refresh must not write anything').toEqual([])
    if (frozenKey) await expect.poll(() => referenceIdentity(page, frozenKey)).toBe(frozenIdentity)

    // A model the settings do not configure keeps its place in the control, so the
    // host can refuse it explicitly, and the page's badge never claims otherwise.
    const modelWithoutConfiguration = modelPlugins.length > 0 && before.settings.configured !== true

    // One declared builtin strategy: this workspace's own host must authorize the
    // composition, and the page must repeat the host's own binding digest for it.
    const strategyLayer = LAYERS.find((layer) => layer.key === 'strategy')!
    const strategy = optionsFor(strategyLayer.kinds).find((entry) => entry.usable)
    let expectedWrites = 0
    let strategyResolution: Resolution | null = null
    if (strategy) {
      const value = `${strategy.plugin_id}@${strategy.package_version}`
      const selection: Selection = { strategy: { plugin_id: strategy.plugin_id, package_version: strategy.package_version } }
      await chooseLayer(page, testInfo, strategyLayer.label, value)
      expect(writeList(seen), 'choosing a layer must not write anything').toEqual([])
      await expect(page.getByRole('region', { name: '组合校验结果' })).toHaveCount(0)

      strategyResolution = await probe(request, selection)
      expect(strategyResolution.usable,
        `a declared builtin strategy (${value}) must be authorized by this host`).toBe(true)
      expect(strategyResolution.binding?.binding_sha256, 'an authorized composition names its binding')
        .toBeTruthy()

      await pressVerify(page, testInfo, seen)
      expectedWrites += 1
      const verdict = page.getByRole('region', { name: '组合校验结果' })
      await expect(verdict).toBeVisible()
      await expect(verdict).toContainText('组合授权检查')
      await expect(verdict).toContainText(strategyResolution.binding!.binding_sha256)
      for (const component of strategyResolution.binding!.components) {
        await expect(verdict).toContainText(`${component.layer}: ${component.plugin_id} ${component.package_version}`)
      }
      await expect(verdict).not.toContainText('已连接')
      await expect(verdict).not.toContainText('请求能力已验证')
      await expect(page.getByRole('region', { name: '组合选择' }).getByRole('alert')).toHaveCount(0)
      testInfo.annotations.push({
        type: 'composition',
        description: `strategy-only ${JSON.stringify(selection)} authorized with binding ${strategyResolution.binding!.binding_sha256}`,
      })
      // The one action sent exactly the selection that was on screen.
      const sent = await lastResolutionBody(seen)
      expect(sent.selection, 'the composition check states the selection that was shown').toEqual(selection)
      expect(JSON.stringify(sent), 'and carries no credential').not.toMatch(/api[_-]?key|password|secret|token/i)
    } else {
      testInfo.annotations.push({
        type: 'composition',
        description: 'this workspace declares no usable strategy capability, so the builtin-strategy authorization could not be checked',
      })
    }

    // The same action with a model layer the settings do not configure: the host
    // must refuse it, and the page must print the host's own code and sentence.
    if (modelWithoutConfiguration) {
      const modelLayer = LAYERS.find((layer) => layer.key === 'model')!
      const model = modelPlugins[0]
      const selection: Selection = { model: { plugin_id: model.plugin_id, package_version: model.package_version } }
      await page.getByLabel(strategyLayer.label).selectOption('')
      await chooseLayer(page, testInfo, modelLayer.label, `${model.plugin_id}@${model.package_version}`)
      await expect(page.getByRole('region', { name: '组合校验结果' })).toHaveCount(0)

      const modelVerdict = await probe(request, selection)
      expect(modelVerdict.usable, `a model layer with settings.configured=${String(before.settings.configured)} must be refused`)
        .toBe(false)
      expect(modelVerdict.code, 'a refusal names the host code for it').toBeTruthy()

      await pressVerify(page, testInfo, seen)
      expectedWrites += 1
      const alert = page.getByRole('region', { name: '组合选择' }).getByRole('alert')
      await expect(alert).toContainText(modelVerdict.code!)
      if (modelVerdict.message) await expect(alert).toContainText(modelVerdict.message)
      await expect(page.getByRole('region', { name: '组合校验结果' })).toHaveCount(0)
      testInfo.annotations.push({
        type: 'model-without-configuration',
        description: `the host refused the model layer with ${modelVerdict.code}: ${modelVerdict.message ?? ''}`.trim(),
      })
      await page.getByLabel(modelLayer.label).selectOption('')
      await expect(page.getByRole('region', { name: '组合选择' }).getByRole('alert')).toHaveCount(0)
    } else {
      testInfo.annotations.push({
        type: 'model-without-configuration',
        description: `not applicable: this workspace reports settings.configured=${String(before.settings.configured)}`,
      })
    }

    // Every write by the page was the explicit action, one per verification, and
    // the host's answer was the one that was on screen when it was sent.
    expect(writeList(seen), 'the explicit action is the only write here')
      .toEqual(Array.from({ length: expectedWrites }, () => `POST ${RECIPE_PATH}`))
    expect(refusedWrites(), 'the page must never write anywhere but the composition check').toEqual([])

    // The unsupported capabilities are stated, and stated without a control.
    const unsupported = page.getByRole('region', { name: '当前未支持' })
    await expect(unsupported).toContainText('多账户')
    await expect(unsupported).toContainText('数据库')
    await expect(unsupported).toContainText('多套执行配置')
    await expect(unsupported.getByRole('button')).toHaveCount(0)
    await expect(unsupported.locator('input, select, textarea')).toHaveCount(0)

    // Nothing above changed anything. The task index is compared with exactly the
    // three reconcile metadata fields excluded, and their before/after values are
    // recorded; the whole strategy and job documents are compared unchanged.
    const after = await readSources(request)
    const tasksAfter = splitTasks(after.tasks)
    expect(tasksAfter.reduced, 'no task field but the reconcile metadata may change').toEqual(tasksBefore.reduced)
    expect(after.strategies).toEqual(before.strategies)
    expect(after.jobs).toEqual(before.jobs)
    evidence['task_metadata_changes'] = metadataChanges(tasksBefore.metadata, tasksAfter.metadata)
    evidence['task_metadata'] = { before: tasksBefore.metadata, after: tasksAfter.metadata }

    // The frozen identity survived the whole session, and the read state too.
    if (frozenKey) await expect.poll(() => referenceIdentity(page, frozenKey)).toBe(frozenIdentity)
    await expect(page.locator('[data-ref="source-tasks"]')).toContainText('已读取')

    evidence['source_counts'] = {
      scenarios: before.scenarios.length,
      datasets: before.datasets.length,
      plugins: before.plugins.length,
      model_plugins: modelPlugins.length,
      jobs: (before.jobs as { jobs?: unknown[] }).jobs?.length ?? null,
    }
    evidence['settings_status'] = before.settings
    evidence['strategy_resolution'] = strategyResolution
    expect(pageErrors, 'the page must not throw').toEqual([])
  } finally {
    // The evidence is attached whether or not an expectation above failed: what
    // the page actually sent is the first thing a failure needs.
    await testInfo.attach('resources-evidence.json', {
      body: JSON.stringify({
        ...evidence,
        api_requests: seen,
        refused_writes: refusedWrites(),
        page_errors: pageErrors,
        reference_sources_compared: 'tasks modulo operation_generation/operation_completion_generation/updated_at; strategies and jobs whole',
      }, null, 2),
      contentType: 'application/json',
    })
  }
})

/** One reference's whole visible identity, as the page derives it from records. */
async function referenceIdentity(page: Page, key: string): Promise<string | null> {
  const node = page.locator(`[data-resource="reference"][data-key="${key}"]`).first()
  if ((await node.count()) === 0) return null
  return node.textContent()
}

/** The body of the last composition check the page sent. */
async function lastResolutionBody(seen: { path: string; body: string | null }[]) {
  const sent = seen.filter((entry) => entry.path === RECIPE_PATH).pop()
  expect(sent, 'the page must have sent a composition check').toBeTruthy()
  return JSON.parse(sent!.body ?? '{}') as { selection?: Selection }
}

test('资源中心 在明暗两种主题与 1400/420 宽度下都不横向溢出，控制仍是 44px', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await waitForHealth()
  const refusedWrites = await guardApi(page)
  const seen = watchApi(page)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => { pageErrors.push(String(error)) })
  const measured: Record<string, unknown> = {}

  try {
    await page.emulateMedia({ colorScheme: 'light' })
    await page.setViewportSize({ width: 1400, height: 1000 })
    await page.goto('/resources/diagnostics')
    await expect(page.getByRole('heading', { name: '资源中心' })).toBeVisible()

    for (const [theme, width] of [
      ['light', 1400],
      ['light', 420],
      ['dark', 1400],
      ['dark', 420],
    ] as const) {
      await page.emulateMedia({ colorScheme: theme })
      await page.setViewportSize({ width, height: theme === 'dark' && width === 420 ? 900 : 1000 })
      await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme ?? null)).toBe(theme)
      await expect(page.getByRole('heading', { name: '资源中心' })).toBeVisible()
      measured[`overflow_${theme}_${width}`] = await overflow(page)
      expect(await overflow(page), `the directory must not scroll sideways in ${theme} at ${width}px`).toBeLessThanOrEqual(1)

      // The shell's own targets stay reachable at a phone width, where the phone
      // layout owns the padding.
      if (width === 420) {
        const verify = page.getByRole('button', { name: '校验组合' })
        await expect(verify).toBeVisible()
        const targets: Record<string, number> = { 校验组合: (await verify.boundingBox())?.height ?? 0 }
        // Every layer control a reader can choose from, and the disclosure that
        // holds a directory entry's evidence, are targets at a phone width too.
        for (const layer of LAYERS) {
          const control = page.getByLabel(layer.label)
          await expect(control, `${layer.label} must be offered on the phone`).toBeVisible()
          targets[layer.label] = (await control.boundingBox())?.height ?? 0
        }
        const summary = page.locator('.resources-page .resource-details > summary').first()
        if (await summary.count()) targets['证据摘要'] = (await summary.boundingBox())?.height ?? 0
        measured[`targets_${theme}_420`] = targets
        for (const [name, height] of Object.entries(targets)) {
          expect(height, `${name} stays a 44px target on the phone`).toBeGreaterThanOrEqual(44)
        }
      }

      await page.screenshot({ path: testInfo.outputPath(`resources-${theme}-${width}.png`), fullPage: true })
      await page.screenshot({ path: testInfo.outputPath(`resources-${theme}-${width}-viewport.png`) })
    }

    expect(writeList(seen), 'looking at the page must not write anything').toEqual([])
    expect(refusedWrites(), 'the page must never write anywhere but the composition check').toEqual([])
    expect(pageErrors, 'the page must not throw').toEqual([])
  } finally {
    await testInfo.attach('resources-visual.json', {
      body: JSON.stringify({ ...measured, api_requests: seen, refused_writes: refusedWrites(), page_errors: pageErrors }, null, 2),
      contentType: 'application/json',
    })
  }
})

test('资源中心 的紧凑密度来自既有的外观设置，两个浏览器都是隔离的', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  await waitForHealth()

  const open = async (density: 'normal' | 'compact') => {
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, colorScheme: 'light' })
    if (density === 'compact') {
      // The workspace's own appearance document, written into this browser's
      // storage before the app starts: no settings UI is driven and no production
      // configuration is touched.
      await context.addInitScript(([key, value]) => {
        try {
          window.localStorage.setItem(key, value)
        } catch {
          // Storage is unavailable before the document has an origin; the run
          // below then reports the default density rather than a false one.
        }
      }, [APPEARANCE_KEY, JSON.stringify({ theme: 'light', density: 'compact', aiWidth: 320 })] as const)
    }
    const page = await context.newPage()
    const refusedWrites = await guardApi(page)
    const seen = watchApi(page)
    const pageErrors: string[] = []
    page.on('pageerror', (error) => { pageErrors.push(String(error)) })
    await page.goto('/resources/diagnostics')
    await expect(page.getByRole('heading', { name: '资源中心' })).toBeVisible()
    const applied = await page.evaluate(() => document.documentElement.dataset.density ?? null)
    const panel = await page.getByRole('region', { name: '组合选择' }).boundingBox()
    const documentHeight = await page.evaluate(() => document.documentElement.scrollHeight)
    await page.screenshot({ path: testInfo.outputPath(`resources-density-${density}.png`), fullPage: true })
    return { context, page, applied, panelHeight: panel?.height ?? 0, documentHeight, refusedWrites, seen, pageErrors }
  }

  const normal = await open('normal')
  const compact = await open('compact')
  try {
    expect(normal.applied, 'an untouched browser keeps the default density').toBe('normal')
    expect(compact.applied, 'the stored appearance sets the density this page renders with').toBe('compact')
    expect(compact.panelHeight, 'compact density tightens the same panel').toBeLessThan(normal.panelHeight)
    for (const [name, run] of [['normal', normal], ['compact', compact]] as const) {
      expect(await overflow(run.page), `${name} density must not scroll sideways`).toBeLessThanOrEqual(1)
      expect(writeList(run.seen), `${name} density must not write anything`).toEqual([])
      expect(run.refusedWrites(), `${name} density must never write`).toEqual([])
      expect(run.pageErrors, `${name} density must not throw`).toEqual([])
    }
    await testInfo.attach('resources-density.json', {
      body: JSON.stringify({
        normal: { density: normal.applied, panel: normal.panelHeight, document: normal.documentHeight },
        compact: { density: compact.applied, panel: compact.panelHeight, document: compact.documentHeight },
      }, null, 2),
      contentType: 'application/json',
    })
  } finally {
    await normal.context.close()
    await compact.context.close()
  }
})
