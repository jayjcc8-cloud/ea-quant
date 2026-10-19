import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { radianApi } from './radian-api'
import { DesktopSettings, type DesktopStatus } from './desktop-settings'
import type { ApiAdapter } from './app'

const status = { workspace: '/fixture/current', next_workspace: '/fixture/current', theme: 'system', startup_error: null,
  model: { provider: 'deepseek', model: 'fake-model', base_url: null, credential_available: true, configured: true, request_validation: 'not_verified' } } as DesktopStatus
const setup = (save = vi.fn(async () => status)) => {
  const api = { desktopSettings: async () => status, saveDesktopSettings: save } as unknown as ApiAdapter
  render(<DesktopSettings api={api} onSaved={() => {}} />)
  return save
}
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals() })
describe('desktop settings explicit saves', () => {
  it('never fills the existing key; cancel and edits do not save', async () => {
    const save = setup()
    const key = await screen.findByLabelText('新 API Key') as HTMLInputElement
    expect(key.value).toBe('')
    fireEvent.change(key, { target: { value: 'fake-new-key' } })
    fireEvent.change(screen.getByLabelText('工作区绝对路径'), { target: { value: '/fixture/other' } })
    fireEvent.click(screen.getByText('取消未保存的修改'))
    expect(key.value).toBe('')
    expect((screen.getByLabelText('工作区绝对路径') as HTMLInputElement).value).toBe('/fixture/current')
    expect(save).not.toHaveBeenCalled()
  })
  it('saves once, clears the input immediately and reports local-only verification', async () => {
    let finish!: (value: DesktopStatus) => void
    const save = setup(vi.fn(() => new Promise<DesktopStatus>(resolve => { finish = resolve })))
    const key = await screen.findByLabelText('新 API Key') as HTMLInputElement
    fireEvent.change(key, { target: { value: 'fake-new-key' } })
    const button = screen.getByText('保存模型设置')
    fireEvent.click(button); fireEvent.click(button)
    expect(save).toHaveBeenCalledTimes(1)
    expect(key.value).toBe('')
    finish(status)
    await screen.findByText('模型设置已保存；真实请求认证仍未验证。')
  })
  it('keeps the saved workspace on failure and permits a retry', async () => {
    const save = setup(vi.fn(async () => { throw new Error('未保存，目录占用') }))
    await screen.findByLabelText('工作区绝对路径')
    fireEvent.change(screen.getByLabelText('工作区绝对路径'), { target: { value: '/fixture/other' } })
    fireEvent.click(screen.getByText('保存工作区位置'))
    await screen.findByRole('alert')
    expect(screen.getAllByText('/fixture/current').length).toBe(1)
    await waitFor(() => expect((screen.getByText('保存工作区位置') as HTMLButtonElement).disabled).toBe(false))
    expect(save).toHaveBeenCalledTimes(1)
  })
})

it('editing or saving a named connection never silently changes the workspace default', async () => {
  const profiles = [
    { ...status.model, id: 'legacy', name: '原有连接', revision: 1, route_version: 'old' },
    { ...status.model, id: 'second', name: '第二连接', model: 'second-model', revision: 1, route_version: 'second' },
  ]
  const value = { ...status, model: { ...status.model, profiles, default_profile_id: 'legacy' } }
  const save = vi.fn(async (...args: [object]) => { void args; return value })
  const api = { desktopSettings: async () => value, saveDesktopSettings: save } as unknown as ApiAdapter
  render(<DesktopSettings api={api} onSaved={() => {}} />)
  fireEvent.click(await screen.findByText('编辑 第二连接'))
  expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('second-model')
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('保存模型设置'))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  expect(save.mock.calls[0][0]).toEqual({ model_profile: { id: 'second', name: '第二连接', provider: 'deepseek', model: 'second-model', base_url: '', api_key: '', clear_key: false } })
  await screen.findByText('模型设置已保存；真实请求认证仍未验证。')
  fireEvent.click(screen.getByText('设为默认 第二连接'))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
  expect(save.mock.calls[1][0]).toEqual({ default_model_profile: 'second' })
})

it('opens the actual named default when no legacy connection exists', async () => {
  const profile = { ...status.model, id: 'named', name: '唯一连接', revision: 1, route_version: 'one' }
  const value = { ...status, model: { ...status.model, profiles: [profile], default_profile_id: 'named' } }
  const save = vi.fn(async (...args: [object]) => { void args; return value })
  const api = { desktopSettings: async () => value, saveDesktopSettings: save } as unknown as ApiAdapter
  render(<DesktopSettings api={api} onSaved={() => {}} />)
  const name = await screen.findByLabelText('连接名称') as HTMLInputElement
  expect(name.value).toBe('唯一连接')
  fireEvent.click(screen.getByText('保存模型设置'))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  expect(save.mock.calls[0][0]).toHaveProperty('model_profile.id', 'named')
})


it('retains ordinary draft across navigation while clearing secrets and removal approval; cancel restores saved input', async () => {
  const save = vi.fn(async () => status)
  const api = { desktopSettings: async () => status, saveDesktopSettings: save } as unknown as ApiAdapter
  const first = render(<DesktopSettings api={api} section="model" onSaved={() => {}} />)
  fireEvent.change(await screen.findByLabelText('模型 ID'), { target: { value: 'unsaved-model' } })
  fireEvent.change(screen.getByLabelText('新 API Key'), { target: { value: 'test-only-secret' } })
  fireEvent.click(screen.getByLabelText('清除已保存凭据'))
  first.unmount()
  render(<DesktopSettings api={api} section="model" onSaved={() => {}} />)
  expect((await screen.findByLabelText('模型 ID') as HTMLInputElement).value).toBe('unsaved-model')
  expect((screen.getByLabelText('新 API Key') as HTMLInputElement).value).toBe('')
  expect((screen.getByLabelText('清除已保存凭据') as HTMLInputElement).checked).toBe(false)
  expect(JSON.stringify(sessionStorage)).not.toContain('test-only-secret')
  fireEvent.click(screen.getByText('取消未保存的修改'))
  expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('fake-model')
  expect(save).not.toHaveBeenCalled()
})

 it('rejects malformed endpoints, accepts a valid URL and retains failed ordinary input', async () => {
  const save = setup(vi.fn(async () => { throw new Error('保存失败') }))
  const endpoint = await screen.findByLabelText('Base URL')
  fireEvent.change(endpoint, { target: { value: 'localhost:1234' } })
  expect((screen.getByText('保存模型设置') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(endpoint, { target: { value: 'https://api.example.test/v1' } })
  expect((screen.getByText('保存模型设置') as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByText('保存模型设置'))
  await screen.findByText('保存失败')
  expect((endpoint as HTMLInputElement).value).toBe('https://api.example.test/v1')
  expect(screen.getByText('未保存草稿')).toBeTruthy()
  expect(save).toHaveBeenCalledTimes(1)
})

it('keeps an unsaved workspace path after a different settings section is saved', async () => {
  let value = status
  const api = { desktopSettings: async () => value } as unknown as ApiAdapter
  const first = render(<DesktopSettings api={api} section="workspace" onSaved={() => {}} />)
  fireEvent.change(await screen.findByLabelText('工作区绝对路径'), { target: { value: '/fixture/pending-draft' } })
  first.unmount()
  value = { ...status, model: { ...status.model, model: 'another-saved-model' } }
  render(<DesktopSettings api={api} section="workspace" onSaved={() => {}} />)
  expect((await screen.findByLabelText('工作区绝对路径') as HTMLInputElement).value).toBe('/fixture/pending-draft')
  expect(screen.getByText('未保存草稿')).toBeTruthy()
})

const genericSaveFailure = '未保存。请检查格式、目录占用和权限；更改提供方/端点需重新输入或清除凭据。原设置保留。'
it('projects only generic workspace failures, retaining input and the saved location', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: { code: 'settings_not_saved', message: genericSaveFailure } }), { status: 409 })))
  const save = vi.fn(radianApi.saveDesktopSettings)
  const api = { desktopSettings: async () => status, saveDesktopSettings: save } as unknown as ApiAdapter
  const view = render(<DesktopSettings api={api} section="workspace" onSaved={() => {}} />)
  fireEvent.change(await screen.findByLabelText('工作区绝对路径'), { target: { value: 'relative/path' } })
  fireEvent.click(screen.getByText('保存工作区位置'))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('工作区未保存')
  expect(alert.textContent).toContain('绝对路径')
  expect(alert.textContent).toContain('目录占用和读写权限')
  expect(alert.textContent).not.toContain('凭据')
  expect((screen.getByLabelText('工作区绝对路径') as HTMLInputElement).value).toBe('relative/path')
  expect(screen.getAllByText('/fixture/current')).toHaveLength(1)
  view.unmount()
  render(<DesktopSettings api={api} section="workspace" onSaved={() => {}} />)
  expect((await screen.findByLabelText('工作区绝对路径') as HTMLInputElement).value).toBe('relative/path')
  expect(save).toHaveBeenCalledTimes(1)
})
it('preserves stop-unconfirmed workspace errors and model credential guidance', async () => {
  const message = '本地模拟停止尚未确认；工作区未切换，请查看运行状态。'
  const response = vi.fn(async () => new Response(JSON.stringify({ error: { code: 'paper_stop_unconfirmed', message } }), { status: 409 }))
  vi.stubGlobal('fetch', response)
  const save = vi.fn(radianApi.saveDesktopSettings)
  render(<DesktopSettings api={{ desktopSettings: async () => status, saveDesktopSettings: save } as unknown as ApiAdapter} onSaved={() => {}} />)
  fireEvent.click(await screen.findByText('保存工作区位置'))
  expect((await screen.findByRole('alert')).textContent).toBe(message)
  response.mockImplementation(async () => new Response(JSON.stringify({ error: { code: 'settings_not_saved', message: genericSaveFailure } }), { status: 409 }))
  fireEvent.click(screen.getByText('保存模型设置'))
  expect((await screen.findByText(genericSaveFailure)).textContent).toContain('凭据')
})
