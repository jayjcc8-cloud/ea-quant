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
    ...scriptedAdapter(), listUserStrategies: async () => [], getUserStrategy: async () => idea,
    createUserStrategy: async () => idea, settingsStatus: async () => absent,
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
afterEach(() => { cleanup(); window.history.replaceState({}, '', '/strategies') })

describe('first-use preparation and recovery', () => {
  it('names missing prerequisites while preserving save and the AI entry', async () => {
    const user = userEvent.setup()
    const { api, ask, run } = host()
    open(api)
    const prep = within(await screen.findByRole('region', { name: '开始研究' }))
    await prep.findByText('模型未配置')
    await prep.findByText('尚无可用场景')
    expect(prep.getByRole('link', { name: '导入 CSV 数据' }).getAttribute('href')).toBe('/research#data-upload')
    await user.type(await screen.findByLabelText('意图'), 'Compare an entry delay')
    await user.click(screen.getByRole('button', { name: '保存想法' }))
    expect(await screen.findByRole('heading', { name: idea.name })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('distinguishes a failed catalog from empty, retries without selecting or losing the current idea', async () => {
    const user = userEvent.setup()
    let reads = 0
    const { api, ask, run } = host({ listUserStrategies: async () => [idea], listScenarios: async () => {
      if (++reads === 1) throw new Error('fixture catalog unavailable')
      return [scenario]
    } })
    open(api, '/strategies/first-idea')
    const prep = within(await screen.findByRole('region', { name: '开始研究' }))
    await prep.findByText(/fixture catalog unavailable/)
    expect(screen.queryByText('尚无可用场景')).toBeNull()
    expect(screen.queryByText(/当前没有可用的有效注册场景/)).toBeNull()
    await user.type(await screen.findByLabelText('消息'), 'Keep this question')
    await user.click(prep.getByRole('button', { name: '重新检查' }))
    await prep.findByText('1 个可用场景')
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
    await screen.findByText('模型未配置')
    await user.type(await screen.findByLabelText('意图'), 'Not saved yet')
    await user.click(screen.getByRole('button', { name: '重新检查' }))
    await screen.findByText('尚无可用场景')
    expect((screen.getByLabelText('意图') as HTMLTextAreaElement).value).toBe('Not saved yet')
  })

  it('ignores late reads from a replaced adapter', async () => {
    const stale = deferred<ScenarioSummary[]>(), model = deferred<ModelConfigurationStatus>()
    const old = host({ listScenarios: () => stale.promise, settingsStatus: () => model.promise })
    const view = open(old.api)
    const fresh = host({ listScenarios: async () => [scenario], settingsStatus: async () => ready })
    view.rerender(<App api={fresh.api} />)
    await screen.findByText('1 个可用场景')
    await act(async () => { stale.resolve([]); model.resolve(absent) })
    expect(screen.getByText('1 个可用场景')).toBeTruthy()
    expect(screen.queryByText('模型未配置')).toBeNull()
    expect(screen.queryByText('尚无可用场景')).toBeNull()
  })

  it('recovers settings GET failure without provider calls and exposes an inert setup recipe', async () => {
    const user = userEvent.setup()
    let reads = 0
    const { api, ask, run } = host({ settingsStatus: async () => {
      if (++reads === 1) throw new Error('fixture settings unavailable')
      return ready
    } })
    open(api, '/settings')
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
    const { api } = host({ listUserStrategies: async () => [idea] })
    open(api)
    await user.click(await screen.findByRole('link', { name: '导入 CSV 数据' }))
    const disclosure = document.getElementById('data-upload') as HTMLDetailsElement
    expect(disclosure?.open).toBe(true)
    expect(screen.getByLabelText('CSV content')).toBeTruthy()
    await user.click(screen.getByRole('link', { name: '返回策略' }))
    expect(await screen.findByRole('link', { name: idea.name })).toBeTruthy()
  })

  it('does not let late settings from a previous adapter replace the current status', async () => {
    const late = deferred<ModelConfigurationStatus>()
    const view = open(host({ settingsStatus: () => late.promise }).api, '/settings')
    view.rerender(<App api={host({ settingsStatus: async () => ready }).api} />)
    await screen.findByText('fixture-model')
    await act(async () => late.resolve(absent))
    await waitFor(() => expect(screen.getByText('fixture-model')).toBeTruthy())
  })
})
