import { researchCollection } from '../preview/research-fixtures'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App, type ApiAdapter, type ScenarioSummary } from './app'
import type { ModelConfigurationStatus } from './research-api'
import type { UserStrategy } from './user-strategies-api'
import { scriptedAdapter } from '../preview/first-use-fixtures'

const absent: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1', settings_file: '/fixture/settings.json',
  configured: false, configuration_present: false, credential_available: false,
  provider: null, model: null, base_url: null, api_key_env: null,
}
const ready = { ...absent, configured: true, configuration_present: true, credential_available: true, provider: 'deepseek', model: 'fixture-model' }
const scenario: ScenarioSummary = {
  scenario_id: 'fixture.yaml', name: 'Fixture', valid: true,
  summary: { strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { target_quantity: '2', entry_delay_bars: 0 }, record_count: 4 },
}
const idea: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'first-idea', name: 'My saved idea', intent: 'Compare an entry delay',
  created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z',
  draft: { kind: 'intent', revision: 1, scenario_id: null }, binding: null,
  history: [], messages: [], calls: [], proposals: [], changes: [], experiments: [],
}
function host(overrides: Partial<ApiAdapter> = {}) {
  const ask = vi.fn(async () => { throw new Error('no model calls permitted') })
  const run = vi.fn(async () => { throw new Error('no run permitted') })
  const api: ApiAdapter = {
    ...scriptedAdapter(), listResearch: async () => researchCollection([]), getUserStrategy: async () => idea,
    createUserStrategy: async () => idea, settingsStatus: async () => absent, listDatasets: async () => [],
    sendUserStrategyMessage: ask, createBacktest: run, ...overrides,
  }
  return { api, ask, run }
}
function open(api: ApiAdapter, path = '/strategies') {
  window.history.replaceState({}, '', path)
  return render(<App api={api} />)
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
afterEach(() => { cleanup(); sessionStorage.clear(); window.history.replaceState({}, '', '/strategies') })

async function preparation(user: ReturnType<typeof userEvent.setup>) {
  const summary = await screen.findByText(/^研究准备/)
  if (!(summary.closest('details') as HTMLDetailsElement).open) await user.click(summary)
  return within(screen.getByRole('region', { name: '研究准备状态' }))
}

async function recheck(user: ReturnType<typeof userEvent.setup>) {
  const prep = await preparation(user)
  const retry = prep.queryByRole('button', { name: '重试检查' })
  if (retry) { await user.click(retry); return }
  const help = prep.getByText('准备说明')
  if (!(help.closest('details') as HTMLDetailsElement).open) await user.click(help)
  await user.click(prep.getByRole('button', { name: '重新读取配置' }))
}

describe('first-use preparation and recovery', () => {
  it('names missing prerequisites while preserving save and the AI entry', async () => {
    const user = userEvent.setup()
    const { api, ask, run } = host()
    open(api)
    const prep = await preparation(user)
    await prep.findByText('未配置')
    await prep.findByText('暂无可用模板，请查看准备说明。')
    expect(prep.getByRole('link', { name: '导入 CSV 数据' }).getAttribute('href')).toBe('/data#data-upload')
    await user.click(screen.getByRole('button', { name: /新建研究/ }))
    await user.type(await screen.findByLabelText('意图'), 'Compare an entry delay')
    await user.click(screen.getByRole('button', { name: '保存想法' }))
    expect(await screen.findByRole('heading', { name: idea.name })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('distinguishes a failed catalog from empty, retries without selecting or losing the current idea', async () => {
    const user = userEvent.setup()
    let reads = 0
    const { api, ask, run } = host({ listResearch: async () => researchCollection([idea]), listScenarios: async () => {
      if (++reads === 1) throw new Error('fixture catalog unavailable')
      return [scenario]
    } })
    open(api, '/strategies/first-idea')
    const prep = await preparation(user)
    await prep.findByText(/fixture catalog unavailable/)
    expect(screen.queryByText('暂无可用模板，请查看准备说明。')).toBeNull()
    expect(screen.queryByText(/当前没有可用的有效注册场景/)).toBeNull()
    await user.type(await screen.findByLabelText('消息'), 'Keep this question')
    await recheck(user)
    await prep.findByText('1 个有效注册场景；可在绑定场景时选择。')
    expect((screen.getByLabelText('场景') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('Keep this question')
    expect(screen.getByText('意图修订 1')).toBeTruthy()
    expect(window.location.pathname).toBe('/strategies/first-idea')
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('keeps typed unsaved intent through a read-only recheck', async () => {
    const user = userEvent.setup()
    const { api } = host()
    open(api)
    await screen.findByText('未配置')
    await user.click(screen.getByRole('button', { name: /新建研究/ }))
    await user.type(await screen.findByLabelText('意图'), 'Not saved yet')
    await recheck(user)
    await screen.findByText('暂无可用模板，请查看准备说明。')
    expect((screen.getByLabelText('意图') as HTMLTextAreaElement).value).toBe('Not saved yet')
  })

  it('ignores late reads from a replaced adapter', async () => {
    const stale = deferred<ScenarioSummary[]>(), model = deferred<ModelConfigurationStatus>()
    const old = host({ listScenarios: () => stale.promise, settingsStatus: () => model.promise })
    const view = open(old.api)
    const fresh = host({ listScenarios: async () => [scenario], settingsStatus: async () => ready })
    view.rerender(<App api={fresh.api} />)
    await screen.findByText('1 个有效注册场景；可在绑定场景时选择。')
    await act(async () => { stale.resolve([]); model.resolve(absent) })
    expect(screen.getByText('1 个有效注册场景；可在绑定场景时选择。')).toBeTruthy()
    expect(screen.queryByText('未配置')).toBeNull()
    expect(screen.queryByText('暂无可用模板，请查看准备说明。')).toBeNull()
  })

  it('recovers settings GET failure without provider calls and exposes an inert setup recipe', async () => {
    const user = userEvent.setup()
    let reads = 0
    const { api, ask, run } = host({ settingsStatus: async () => {
      if (++reads === 1) throw new Error('fixture settings unavailable')
      return ready
    } })
    open(api, '/resources/models')
    await user.click(screen.getByText('模型连接诊断'))
    await screen.findByText('fixture settings unavailable')
    await user.click(screen.getByRole('button', { name: '重新检查模型配置' }))
    await screen.findByText('fixture-model')
    expect(screen.queryByText('fixture settings unavailable')).toBeNull()
    await user.click(screen.getByText('如何配置模型'))
    expect(screen.getByText(/YOUR_MODEL_ID/)).toBeTruthy()
    expect(screen.getByText(/QUINE_MODEL_API_KEY/)).toBeTruthy()
    expect(screen.getByText(/本页未发送验证请求/)).toBeTruthy()
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('opens the existing upload controls via the first-use link and returns to saved work', async () => {
    const user = userEvent.setup()
    const { api } = host({ listResearch: async () => researchCollection([idea]) })
    open(api)
    await user.click((await preparation(user)).getByRole('link', { name: '导入 CSV 数据' }))
    const disclosure = document.getElementById('data-upload') as HTMLDetailsElement
    expect(disclosure?.open).toBe(true)
    expect(window.location.pathname + window.location.hash).toBe('/resources/data#data-upload')
    expect(screen.getByLabelText('CSV 内容')).toBeTruthy()
    await user.click(screen.getByRole('link', { name: '返回研究' }))
    expect(await screen.findByRole('link', { name: idea.name })).toBeTruthy()
  })

  it('does not let late settings from a previous adapter replace the current status', async () => {
    const late = deferred<ModelConfigurationStatus>()
    const view = open(host({ settingsStatus: () => late.promise }).api, '/resources/models')
    await userEvent.setup().click(screen.getByText('模型连接诊断'))
    view.rerender(<App api={host({ settingsStatus: async () => ready }).api} />)
    await screen.findByText('fixture-model')
    await act(async () => late.resolve(absent))
    await waitFor(() => expect(screen.getByText('fixture-model')).toBeTruthy())
  })
})


describe('model send readiness blocker', () => {
  it.each(['unconfigured', 'error', 'unavailable', 'loading'] as const)('blocks %s readiness with retained input and no model request', async state => {
    const user = userEvent.setup()
    const pending = deferred<ModelConfigurationStatus>()
    const settingsStatus = state === 'unavailable' ? undefined
      : state === 'loading' ? () => pending.promise
        : state === 'error' ? async () => { throw new Error('fixture status failed') }
          : async () => absent
    const { api, ask } = host({ listResearch: async () => researchCollection([idea]), settingsStatus })
    open(api, '/strategies/first-idea')
    await user.type(await screen.findByLabelText('消息'), 'Keep my question')
    const send = screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    await user.click(send)
    expect(ask).not.toHaveBeenCalled()
    const chat = within(screen.getByRole('region', { name: 'AI 协作' }))
    expect(chat.getByRole('link', { name: '设置连接' })).toBeTruthy()
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('Keep my question')
  })

  it('recovers to ready with retained input and sends once only after the send click', async () => {
    const user = userEvent.setup()
    let configured = false
    const send = vi.fn(async () => idea)
    const { api } = host({ listResearch: async () => researchCollection([idea]),
      settingsStatus: async () => configured ? ready : absent, sendUserStrategyMessage: send })
    open(api, '/strategies/first-idea')
    await user.type(await screen.findByLabelText('消息'), 'Keep my question')
    expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(true)
    configured = true
    await recheck(user)
    await screen.findByText('已配置 · 连接未验证')
    const button = screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(send).not.toHaveBeenCalled()
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('Keep my question')
    await user.click(button)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send.mock.calls[0]).toEqual(['first-idea', expect.objectContaining({ text: 'Keep my question', base_revision: 1 })])
  })

  it('blocks a previously ready composer throughout recheck and after a failed status read', async () => {
    const user = userEvent.setup()
    const pending = deferred<ModelConfigurationStatus>()
    let reads = 0
    const { api, ask } = host({ listResearch: async () => researchCollection([idea]), settingsStatus: async () => {
      reads += 1
      if (reads === 1) return ready
      if (reads === 2) return pending.promise
      throw new Error('fixture status failed')
    } })
    open(api, '/strategies/first-idea')
    await user.type(await screen.findByLabelText('消息'), 'Question after a status change')
    const button = screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    await recheck(user)
    expect(button.disabled).toBe(true)
    await act(async () => pending.resolve(ready))
    expect(button.disabled).toBe(false)
    await recheck(user)
    await screen.findByText('状态未知')
    expect(button.disabled).toBe(true)
    await user.click(button)
    expect(ask).not.toHaveBeenCalled()
  })
})

describe('compact preparation and existing history navigation', () => {
  it('uses actual data facts, removes repeated onboarding, and keeps manual refresh in details', async () => {
    const user = userEvent.setup()
    open(host({ listScenarios: async () => [scenario] }).api)
    const prep = await preparation(user)
    await prep.findByText('尚未导入')
    expect(prep.getByText('未配置')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: '开始研究' })).toBeNull()
    expect(screen.queryByText(/先保存想法，再绑定场景与数据/)).toBeNull()
    expect((prep.getByRole('button', { name: '重新读取配置' }).closest('details') as HTMLDetailsElement).open).toBe(false)
    expect(screen.queryByRole('link', { name: '旧研究任务' })).toBeNull()
    expect(screen.getByRole('link', { name: '历史记录' }).getAttribute('href')).toBe('/backtests#history-records')
    await user.click(prep.getByText('准备说明'))
    expect(prep.getByRole('button', { name: '重新读取配置' })).toBeTruthy()
    expect(prep.getByText(/模型配置不代表连接成功/)).toBeTruthy()
    expect(prep.getByText(/已登记数据不代表与当前模板兼容/)).toBeTruthy()
  })

  it('collapses complete configuration without claiming a working model or runnable experiment', async () => {
    const user = userEvent.setup()
    open(host({ settingsStatus: async () => ready, listScenarios: async () => [scenario], listDatasets: async () => [{ dataset_id: 'fixture.csv', valid: true }] }).api)
    const badge = await screen.findByText('配置已齐')
    expect((badge.closest('details') as HTMLDetailsElement).open).toBe(false)
    const prep = await preparation(user)
    expect(prep.getByText('已配置 · 连接未验证')).toBeTruthy()
    expect(prep.getByText('1 项已登记')).toBeTruthy()
    expect(prep.getByRole('link', { name: '查看配置' })).toBeTruthy()
    expect(screen.queryByText('已就绪')).toBeNull()
  })

  it('distinguishes unknown data from empty and retries without losing a draft', async () => {
    const user = userEvent.setup()
    let failed = true
    const { api, ask, run } = host({ listScenarios: async () => [scenario], listDatasets: async () => {
      if (failed) throw new Error('fixture data unavailable')
      return [{ dataset_id: 'bad.csv', valid: false }]
    } })
    open(api)
    const prep = await preparation(user)
    await prep.findByText('数据目录读取失败')
    expect(prep.queryByText('尚未导入')).toBeNull()
    await user.click(screen.getByRole('button', { name: /新建研究/ }))
    await user.type(await screen.findByLabelText('意图'), 'Keep my draft')
    failed = false
    await recheck(user)
    await prep.findByText('1 项已登记 · 1 项需修正')
    expect(screen.queryByText('配置已齐')).toBeNull()
    expect((screen.getByLabelText('意图') as HTMLTextAreaElement).value).toBe('Keep my draft')
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('ignores late data from a replaced adapter and labels unsupported reads unknown', async () => {
    const late = deferred<{ dataset_id: string; valid: boolean }[]>()
    const view = open(host({ listDatasets: () => late.promise }).api)
    view.rerender(<App api={host({ listDatasets: undefined }).api} />)
    await screen.findByText('数据目录读取失败')
    await act(async () => late.resolve([{ dataset_id: 'stale.csv', valid: true }]))
    expect(screen.queryByText('1 项已登记')).toBeNull()
    expect(screen.queryByText('尚未导入')).toBeNull()
  })

  it('keeps all history routes reachable through the existing records page with source returns', async () => {
    const user = userEvent.setup()
    const old = vi.fn(async () => []), holdouts = vi.fn(async () => []), candidates = vi.fn(async () => [])
    const { api, ask, run } = host({ listBacktests: async () => [], listResearchTasks: old, listHoldouts: holdouts, listCandidates: candidates })
    open(api, '/strategies?search=remember')
    await user.click(await screen.findByRole('link', { name: '历史记录' }))
    expect(window.location.pathname + window.location.hash).toBe('/backtests#history-records')
    expect(await screen.findByRole('heading', { name: '回测记录' })).toBeTruthy()
    for (const [label, path, read] of [['旧研究任务', '/research', old], ['留出记录', '/holdouts', holdouts], ['候选记录', '/candidates', candidates]] as const) {
      await user.click(within(screen.getByRole('navigation', { name: '历史记录分类' })).getByRole('link', { name: label }))
      expect(window.location.pathname).toBe(path)
      await waitFor(() => expect(read).toHaveBeenCalled())
      await user.click(screen.getByRole('link', { name: '返回回测列表' }))
      expect(window.location.pathname + window.location.hash).toBe('/backtests#history-records')
    }
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })
})
