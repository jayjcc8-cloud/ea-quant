import { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  act(() => resize?.({ matches: false }))
  await waitFor(() => expect(screen.getByRole('complementary', { name: '研究助手面板' })).toBeTruthy())
  expect(screen.queryByRole('dialog')).toBeNull()
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('仍待发送')
  expect(mounted).toHaveBeenCalledTimes(1)
  act(() => resize?.({ matches: true }))
  await waitFor(() => expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull())
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('仍待发送')
  expect(mounted).toHaveBeenCalledTimes(1)
})

it('collapses the desktop assistant without interrupting its session and restores focus with the unsent input', async () => {
  narrow = false
  const mounted = vi.fn(), unmounted = vi.fn()
  function Assistant({ answer }: { answer: string }) {
    useEffect(() => { mounted(); return unmounted }, [])
    return <><label>消息<textarea defaultValue="保留这个问题" /></label><p>{answer}</p></>
  }
  const view = render(<DetailSurface assistant={<Assistant answer="正在回答" />}><p>报告正文</p></DetailSurface>)
  const panel = screen.getByRole('complementary', { name: '研究助手面板' })
  fireEvent.change(within(panel).getByLabelText('消息'), { target: { value: '尚未发送的新问题' } })
  await userEvent.click(within(panel).getByRole('button', { name: '收起研究助手' }))
  const opener = screen.getByRole('button', { name: '打开研究助手' })
  expect(screen.queryByRole('complementary', { name: '研究助手面板' })).toBeNull()
  expect(document.activeElement).toBe(opener)
  expect(opener.getAttribute('aria-expanded')).toBe('false')
  view.rerender(<DetailSurface assistant={<Assistant answer="回答已经更新" />}><p>报告正文</p></DetailSurface>)
  await userEvent.click(opener)
  const input = screen.getByRole('textbox', { name: '消息' }) as HTMLTextAreaElement
  expect(input.value).toBe('尚未发送的新问题')
  expect(document.activeElement).toBe(input)
  expect(screen.getByText('回答已经更新')).toBeTruthy()
  expect(document.querySelectorAll('.persistent-agent')).toHaveLength(1)
  expect(mounted).toHaveBeenCalledTimes(1)
  expect(unmounted).not.toHaveBeenCalled()
})

it('keeps research content primary until the user opens the optional assistant', async () => {
  narrow = false
  const mounted = vi.fn()
  function Assistant() { useEffect(mounted, []); return <textarea aria-label="消息" /> }
  render(<DetailSurface defaultCollapsed assistant={<Assistant />}><h1>准备第一次回测</h1></DetailSurface>)
  expect(screen.queryByRole('complementary', { name: '研究助手面板' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  expect(screen.getByRole('textbox', { name: '消息' })).toBeTruthy()
  expect(mounted).toHaveBeenCalledTimes(1)
})

it('keeps desktop collapse separate from opening a narrow modal and returns focus when that modal disappears', async () => {
  narrow = false
  render(<DetailSurface assistant={<textarea aria-label="消息" defaultValue="研究问题" />}><p>正文</p></DetailSurface>)
  await userEvent.click(screen.getByRole('button', { name: '收起研究助手' }))
  act(() => resize?.({ matches: true }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  const input = screen.getByRole('textbox', { name: '消息' })
  input.focus()
  act(() => resize?.({ matches: false }))
  await waitFor(() => expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull())
  expect(document.activeElement).toBe(screen.getByRole('button', { name: '打开研究助手' }))
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  expect((screen.getByRole('textbox', { name: '消息' }) as HTMLTextAreaElement).value).toBe('研究问题')
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('preserves the offline preview callback instead of opening a second assistant', async () => {
  const openAgent = vi.fn()
  render(<DetailSurface openAgent={openAgent} assistant={<p>预览助手</p>}><p>预览正文</p></DetailSurface>)
  await userEvent.click(screen.getByRole('button', { name: '打开研究助手' }))
  expect(openAgent).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getAllByText('预览助手')).toHaveLength(1)
})

it('does not move focus from the report when a wider window reveals the assistant', async () => {
  render(<DetailSurface assistant={<textarea aria-label="消息" />}><button>报告操作</button></DetailSurface>)
  const reportAction = screen.getByRole('button', { name: '报告操作' })
  reportAction.focus()
  act(() => resize?.({ matches: false }))
  await waitFor(() => expect(screen.getByRole('complementary', { name: '研究助手面板' })).toBeTruthy())
  expect(document.activeElement).toBe(reportAction)
})
