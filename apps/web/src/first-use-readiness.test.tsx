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

async function recheck(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '重新读取模型配置' }))
}

describe('contextual research inputs and recovery', () => {
  it('keeps saving independent of model configuration', async () => {
    const user = userEvent.setup()
    const { api, ask, run } = host()
    open(api)
    await user.click(screen.getByRole('button', { name: /新建研究/ }))
    await user.type(await screen.findByLabelText('意图'), 'Compare an entry delay')
    await user.click(screen.getByRole('button', { name: '保存想法' }))
    expect(await screen.findByRole('heading', { name: idea.name })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 协作' })).toBeTruthy()
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('retries failed templates beside binding without selecting or losing the conversation draft', async () => {
    const user = userEvent.setup()
    let reads = 0
    const { api, ask, run } = host({ listResearch: async () => researchCollection([idea]), listScenarios: async () => {
      if (++reads === 1) throw new Error('fixture catalog unavailable')
      return [scenario]
    } })
    open(api, '/strategies/first-idea')
    const binding = within(await screen.findByRole('region', { name: '绑定场景' }))
    await binding.findByText('fixture catalog unavailable')
    await user.type(await screen.findByLabelText('消息'), 'Keep this question')
    await user.click(binding.getByRole('button', { name: '重新读取模板' }))
    await binding.findByRole('option', { name: /fixture.yaml/ })
    expect((screen.getByLabelText('场景') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('Keep this question')
    expect(window.location.pathname).toBe('/strategies/first-idea')
    expect(ask).not.toHaveBeenCalled(); expect(run).not.toHaveBeenCalled()
  })

  it('ignores late template and model reads from a replaced adapter', async () => {
    const stale = deferred<ScenarioSummary[]>(), model = deferred<ModelConfigurationStatus>()
    const view = open(host({ listScenarios: () => stale.promise, settingsStatus: () => model.promise }).api, '/strategies/first-idea')
    view.rerender(<App api={host({ listScenarios: async () => [scenario], settingsStatus: async () => ready }).api} />)
    await screen.findByRole('option', { name: /fixture.yaml/ })
    await screen.findByText('已配置 · 连接未验证')
    await act(async () => { stale.resolve([]); model.resolve(absent) })
    expect(screen.getByRole('option', { name: /fixture.yaml/ })).toBeTruthy()
    expect(screen.queryByText('模型尚未配置就绪，暂不能发送。')).toBeNull()
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
    await screen.findByText('模型配置状态未知，暂不能发送。')
    expect(button.disabled).toBe(true)
    await user.click(button)
    expect(ask).not.toHaveBeenCalled()
  })
})

describe('history navigation', () => {
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
