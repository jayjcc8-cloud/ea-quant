import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DesktopSettings, type DesktopStatus } from './desktop-settings'
import type { ApiAdapter } from './app'

const status = { workspace: '/fixture/current', next_workspace: '/fixture/current', theme: 'system', startup_error: null,
  model: { provider: 'deepseek', model: 'fake-model', base_url: null, credential_available: true, configured: true, request_validation: 'not_verified' } } as DesktopStatus
const setup = (save = vi.fn(async () => status)) => {
  const api = { desktopSettings: async () => status, saveDesktopSettings: save } as unknown as ApiAdapter
  render(<DesktopSettings api={api} onSaved={() => {}} />)
  return save
}
afterEach(cleanup)
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
    fireEvent.click(screen.getByText('打开此工作区（重启生效）'))
    await screen.findByRole('alert')
    expect(screen.getAllByText('/fixture/current').length).toBe(2)
    await waitFor(() => expect((screen.getByText('打开此工作区（重启生效）') as HTMLButtonElement).disabled).toBe(false))
    expect(save).toHaveBeenCalledTimes(1)
  })
})
