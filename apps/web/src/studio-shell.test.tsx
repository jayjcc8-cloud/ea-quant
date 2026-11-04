import { useEffect } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { WorkspaceNav } from './studio-navigation'
import { DetailSurface } from '../studio/src/layout'
import type { ApiAdapter } from './app'
import type { ResearchSummary } from './research-workbench-api'

let narrow = true
let resize: ((event: { matches: boolean }) => void) | undefined
beforeEach(() => {
  narrow = true
  resize = undefined
  vi.stubGlobal('matchMedia', () => ({ matches: narrow, addEventListener: (_: string, listener: typeof resize) => { resize = listener }, removeEventListener() {} }))
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true
    this.querySelector<HTMLElement>('input, button, a[href], textarea')?.focus()
  }
  HTMLDialogElement.prototype.show = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

const items: ResearchSummary[] = Array.from({ length: 8 }, (_, i) => ({
  user_strategy_id: `research-${i}`, name: `成本研究 ${i}`, intent: '检查交易成本', exchange_id: null,
  updated_at: '2026-11-04', instrument: null, draft: { kind: 'configured', revision: 2, pending_proposals: 0 },
  experiment_count: 0, latest_experiment: null, next_step: 'experiment',
}))
function Location() { return <output aria-label="当前位置">{useLocation().pathname}</output> }
function navigation() {
  const listResearch = vi.fn<NonNullable<ApiAdapter['listResearch']>>(async () => ({ items, total: 8, matched: 8 }))
  render(<MemoryRouter><WorkspaceNav api={{ listResearch } as unknown as ApiAdapter} /><Location /></MemoryRouter>)
  return listResearch
}

it('opens an accessible search from the collapsed sidebar and shows matches beyond recent five', async () => {
  const list = navigation()
  const opener = screen.getByRole('button', { name: '搜索研究' })
  await userEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: '搜索研究' })
  const input = within(dialog).getByRole('searchbox', { name: '搜索研究' })
  expect(document.activeElement).toBe(input)
  await userEvent.type(input, '成本')
  await waitFor(() => expect(list).toHaveBeenLastCalledWith({ q: '成本', sort: 'updated' }))
  const last = await within(dialog).findByRole('link', { name: /成本研究 7/ })
  await userEvent.click(last)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByLabelText('当前位置').textContent).toBe('/research/research-7/overview')
})

it('opens search with the desktop shortcut and returns focus on Escape without accepting IME shortcuts', async () => {
  navigation()
  const opener = screen.getByRole('button', { name: '搜索研究' })
  opener.focus()
  fireEvent.keyDown(document, { key: 'k', metaKey: true, isComposing: true })
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.keyDown(document, { key: 'k', metaKey: true })
  const dialog = screen.getByRole('dialog', { name: '搜索研究' })
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(opener)
})

it('keeps resource navigation reachable from the menu and removes unavailable Live from the primary navigation', async () => {
  navigation()
  const primary = screen.getByRole('navigation', { name: '主要导航' })
  expect(within(primary).queryByRole('link', { name: /实盘/ })).toBeNull()
  expect(within(primary).getByRole('link', { name: '模拟运行' }).getAttribute('href')).toBe('/simulations')
  await userEvent.click(screen.getByRole('button', { name: '打开工作区导航' }))
  const dialog = screen.getByRole('dialog', { name: '工作区导航' })
  await userEvent.click(within(dialog).getByRole('link', { name: '模型连接' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByLabelText('当前位置').textContent).toBe('/resources/models')
})

it('opens and closes the assistant with Escape while preserving its mounted session and unsent input', async () => {
  const mounted = vi.fn(), unmounted = vi.fn()
  function Assistant() {
    useEffect(() => { mounted(); return unmounted }, [])
    return <label>消息<textarea defaultValue="保留此问题" /></label>
  }
  render(<DetailSurface assistant={<Assistant />}><button>正文操作</button></DetailSurface>)
  const opener = screen.getByRole('button', { name: '打开研究助手' })
  await userEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: '研究助手面板' })
  const bounds = vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
  const first = within(dialog).getByRole('button', { name: '收起研究助手' }), last = within(dialog).getByLabelText('消息')
  first.focus(); fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(last)
  fireEvent.keyDown(last, { key: 'Tab' })
  expect(document.activeElement).toBe(first)
  bounds.mockRestore()
  fireEvent.change(within(dialog).getByLabelText('消息'), { target: { value: '新的未发送问题' } })
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(opener)
  await userEvent.click(opener)
  expect((within(screen.getByRole('dialog')).getByLabelText('消息') as HTMLTextAreaElement).value).toBe('新的未发送问题')
  expect(mounted).toHaveBeenCalledTimes(1)
  expect(unmounted).not.toHaveBeenCalled()
})

it('switches between modal and inline assistant without remounting or leaving a modal open', async () => {
  const mounted = vi.fn()
  function Assistant() { useEffect(mounted, []); return <textarea aria-label="消息" defaultValue="待发送" /> }
  render(<DetailSurface assistant={<Assistant />}><p>研究正文</p></DetailSurface>)
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '仍待发送' } })
  resize?.({ matches: false })
  await waitFor(() => expect(screen.getByRole('complementary', { name: '研究助手面板' })).toBeTruthy())
  expect(screen.queryByRole('dialog')).toBeNull()
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('仍待发送')
  expect(mounted).toHaveBeenCalledTimes(1)
  resize?.({ matches: true })
  await waitFor(() => expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull())
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('仍待发送')
  expect(mounted).toHaveBeenCalledTimes(1)
})
