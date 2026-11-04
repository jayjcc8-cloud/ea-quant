import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ApiAdapter } from './app'
import type { DesktopStatus } from './desktop-settings'
import type { ModelConfigurationStatus } from './research-api'
import type { UserStrategy } from './user-strategies-api'
import { Assistant } from './studio-assistant'
import { ResearchModelControl } from './studio-model-setup'

const strategy: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'current-research', name: '成本研究', intent: '比较成本', created_at: '2026-11-04', updated_at: '2026-11-04',
  draft: { kind: 'intent', revision: 4, scenario_id: null }, history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const emptyModel: ModelConfigurationStatus = {
  schema: 'radian.settings-status.v1', settings_file: '/fixture/settings.json', configured: false, provider: 'deepseek', model: '', base_url: null,
  api_key_env: null, credential_available: false, request_validation: 'not_verified', default_profile_id: 'legacy', profiles: [],
}
function setup(overrides: Partial<ApiAdapter> = {}) {
  let model = structuredClone(emptyModel)
  const desktop = (): DesktopStatus => ({ workspace: '/fixture/workspace', next_workspace: '/fixture/workspace', theme: 'system', startup_error: null, model })
  const save = vi.fn<NonNullable<ApiAdapter['saveDesktopSettings']>>(async body => {
    const input = body as { model_profile?: { id: string; name: string; model: string }; default_model_profile?: string }
    if (input.model_profile) {
      // The real settings response never contains an API key.
      const profile = { ...emptyModel, id: input.model_profile.id, name: input.model_profile.name, model: input.model_profile.model, profiles: undefined,
        revision: 1, route_version: input.model_profile.id, configured: true, credential_available: true }
      model = { ...model, profiles: [profile] }
    }
    if (input.default_model_profile) model = { ...model, ...model.profiles![0], profiles: model.profiles, default_profile_id: input.default_model_profile }
    return desktop()
  })
  const send = vi.fn(), launch = vi.fn(), saved = vi.fn()
  const api = { desktopSettings: async () => desktop(), settingsStatus: async () => model, saveDesktopSettings: save, sendUserStrategyMessage: send, launchUserStrategyExperiment: launch, ...overrides } as ApiAdapter
  function Research() {
    const [open, setOpen] = useState(false), [version, setVersion] = useState(0), [doc, changed] = useState(strategy)
    return <>
      <ResearchModelControl api={api} strategy={doc} open={open} onOpen={() => setOpen(true)} onClose={() => setOpen(false)} modelVersion={version}
        onSaved={() => { saved(); setVersion(value => value + 1) }} />
      <label>未保存参数<input defaultValue="12" /></label>
      <Assistant api={api} strategy={doc} changed={changed} modelVersion={version} onConfigureModel={() => setOpen(true)} />
    </>
  }
  render(<MemoryRouter><Research /></MemoryRouter>)
  return { save, saved, send, launch, desktop }
}
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear()
  HTMLDialogElement.prototype.showModal = function () { this.open = true; this.querySelector<HTMLElement>('button, input')?.focus() }
  HTMLDialogElement.prototype.close = function () { this.open = false }
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('saves and selects a default in place, then explicitly returns with the original research and question still mounted', async () => {
  const { save, saved, send, launch } = setup()
  const question = await screen.findByLabelText('消息') as HTMLTextAreaElement
  const parameter = screen.getByLabelText('未保存参数') as HTMLInputElement
  fireEvent.change(question, { target: { value: '先保留这个问题' } })
  fireEvent.change(parameter, { target: { value: '29' } })
  const opener = await screen.findByRole('button', { name: '配置模型连接' })
  await userEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: '模型连接' })
  await userEvent.click(await within(dialog).findByRole('button', { name: '添加连接' }))
  fireEvent.change(within(dialog).getByLabelText('连接名称'), { target: { value: '研究连接' } })
  fireEvent.change(within(dialog).getByLabelText('模型 ID'), { target: { value: 'test-model' } })
  fireEvent.change(within(dialog).getByLabelText('新 API Key'), { target: { value: 'synthetic-secret' } })
  await userEvent.click(within(dialog).getByRole('button', { name: '保存模型设置' }))
  await within(dialog).findByText('模型设置已保存；真实请求认证仍未验证。')
  expect(saved).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('dialog', { name: '模型连接' })).toBe(dialog)
  expect((within(dialog).getByLabelText('新 API Key') as HTMLInputElement).value).toBe('')
  expect(JSON.stringify(sessionStorage)).not.toContain('synthetic-secret')
  await userEvent.click(within(dialog).getByRole('button', { name: '设为默认 研究连接' }))
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(2))
  expect(screen.getByRole('dialog', { name: '模型连接' })).toBe(dialog)
  expect(save).toHaveBeenCalledTimes(2)
  await userEvent.click(within(dialog).getByRole('button', { name: '返回研究' }))
  expect(screen.queryByRole('dialog', { name: '模型连接' })).toBeNull()
  expect(screen.getByLabelText('消息')).toBe(question)
  expect(screen.getByLabelText('未保存参数')).toBe(parameter)
  expect(question.value).toBe('先保留这个问题'); expect(parameter.value).toBe('29')
  expect(document.activeElement).toBe(opener)
  await waitFor(() => expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false))
  expect(screen.queryByText('连接已验证')).toBeNull()
  expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('keeps a failed save in settings with ordinary inputs intact and never reports success or sends', async () => {
  const save = vi.fn().mockRejectedValue(new Error('保存失败，请重试。'))
  const { saved, send, launch } = setup({ saveDesktopSettings: save })
  await userEvent.click(await screen.findByRole('button', { name: '配置模型连接' }))
  const dialog = screen.getByRole('dialog', { name: '模型连接' })
  fireEvent.change(await within(dialog).findByLabelText('模型 ID'), { target: { value: 'keep-this-model' } })
  fireEvent.change(within(dialog).getByLabelText('新 API Key'), { target: { value: 'synthetic-secret' } })
  await userEvent.click(within(dialog).getByRole('button', { name: '保存模型设置' }))
  expect((await within(dialog).findByRole('alert')).textContent).toBe('保存失败，请重试。')
  expect(screen.getByRole('dialog', { name: '模型连接' })).toBe(dialog)
  expect((within(dialog).getByLabelText('模型 ID') as HTMLInputElement).value).toBe('keep-this-model')
  expect((within(dialog).getByLabelText('新 API Key') as HTMLInputElement).value).toBe('')
  expect(saved).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('cancels settings edits and returns to the same question through the assistant entry', async () => {
  const { save, saved, send } = setup()
  const question = await screen.findByLabelText('消息') as HTMLTextAreaElement
  fireEvent.change(question, { target: { value: '保留未发送的问题' } })
  await userEvent.click(screen.getByText('选择模型连接'))
  const opener = screen.getByRole('button', { name: '管理模型连接' })
  await userEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: '模型连接' })
  fireEvent.change(await within(dialog).findByLabelText('模型 ID'), { target: { value: 'discard-model' } })
  fireEvent.change(within(dialog).getByLabelText('新 API Key'), { target: { value: 'discard-secret' } })
  await userEvent.click(within(dialog).getByRole('button', { name: '取消未保存的修改' }))
  expect((within(dialog).getByLabelText('模型 ID') as HTMLInputElement).value).toBe('')
  fireEvent.keyDown(dialog, { key: 'Escape' })
  expect(screen.queryByRole('dialog', { name: '模型连接' })).toBeNull()
  expect(screen.getByLabelText('消息')).toBe(question)
  expect(question.value).toBe('保留未发送的问题')
  expect(document.activeElement).toBe(opener)
  expect(save).not.toHaveBeenCalled(); expect(saved).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled()
})

it('waits for a pending save before allowing return so its saved callback cannot be lost', async () => {
  let finish!: (value: DesktopStatus) => void
  const save = vi.fn(() => new Promise<DesktopStatus>(resolve => { finish = resolve }))
  const { desktop, saved } = setup({ saveDesktopSettings: save })
  await userEvent.click(await screen.findByRole('button', { name: '配置模型连接' }))
  const dialog = screen.getByRole('dialog', { name: '模型连接' })
  fireEvent.change(await within(dialog).findByLabelText('模型 ID'), { target: { value: 'pending-model' } })
  await userEvent.click(within(dialog).getByRole('button', { name: '保存模型设置' }))
  expect((within(dialog).getByRole('button', { name: '返回研究' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.click(within(dialog).getByRole('button', { name: '关闭模型连接' }))
  fireEvent.keyDown(dialog, { key: 'Escape' })
  expect(screen.getByRole('dialog', { name: '模型连接' })).toBe(dialog)
  await act(async () => finish(desktop()))
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1))
  await userEvent.click(within(dialog).getByRole('button', { name: '返回研究' }))
  expect(screen.queryByRole('dialog', { name: '模型连接' })).toBeNull()
})
