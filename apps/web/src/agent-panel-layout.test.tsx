import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { AgentFeed, ResearchSplitLayout } from './agent-panel-layout'

afterEach(() => { cleanup(); localStorage.clear() })

it('adjusts and restores the split width without replacing either workspace', () => {
  const view = render(<ResearchSplitLayout><input aria-label="manual draft" defaultValue="work" /><input aria-label="AI draft" defaultValue="message" /></ResearchSplitLayout>)
  const splitter = screen.getByRole('separator', { name: '调整 AI 面板宽度' })
  fireEvent.keyDown(splitter, { key: 'ArrowLeft' })
  expect(splitter.getAttribute('aria-valuenow')).toBe('34')
  fireEvent.click(screen.getByRole('button', { name: 'AI 协作' }))
  expect(screen.getByLabelText('AI draft').closest('.strategy-workspace')?.getAttribute('data-workspace-view')).toBe('ai')
  expect((screen.getByLabelText('manual draft') as HTMLInputElement).value).toBe('work')
  view.unmount()
  render(<ResearchSplitLayout><div /><div /></ResearchSplitLayout>)
  expect(screen.getByRole('separator').getAttribute('aria-valuenow')).toBe('34')
})

it('does not move a reader in history on new output or a revision remount, and isolates another research', () => {
  const page = (identity: string, revision: number) => <ResearchSplitLayout>
    <div />
    <AgentFeed key={`${identity}-${revision}`} identity={identity} version={String(revision)}><p>Recorded output {revision}</p></AgentFeed>
  </ResearchSplitLayout>
  const view = render(page('first', 1))
  const feed = screen.getByRole('group', { name: '对话与操作记录' })
  Object.defineProperties(feed, { scrollHeight: { configurable: true, value: 2000 }, clientHeight: { configurable: true, value: 300 } })
  feed.scrollTop = 240
  fireEvent.scroll(feed)
  view.rerender(page('first', 2))
  const next = screen.getByRole('group', { name: '对话与操作记录' })
  Object.defineProperties(next, { scrollHeight: { value: 2500 }, clientHeight: { value: 300 } })
  // Trigger a content update without another remount.
  view.rerender(<ResearchSplitLayout><div /><AgentFeed key="first-2" identity="first" version="3"><p>More output</p></AgentFeed></ResearchSplitLayout>)
  expect(next.scrollTop).toBe(240)
  fireEvent.click(screen.getByRole('button', { name: '回到最新 ↓' }))
  expect(next.scrollTop).toBe(2500)
  expect(screen.queryByRole('button', { name: '回到最新 ↓' })).toBeNull()
  view.rerender(page('second', 1))
  expect(screen.queryByRole('button', { name: '回到最新 ↓' })).toBeNull()
})


it('temporarily collapses and expands AI without unmounting either draft', () => {
  render(<ResearchSplitLayout><input aria-label="manual draft" defaultValue="work" /><input aria-label="AI draft" defaultValue="message" /></ResearchSplitLayout>)
  fireEvent.click(screen.getByRole('button', { name: '收起 AI 面板' }))
  expect(document.querySelector('.strategy-workspace')?.getAttribute('data-ai-dock')).toBe('closed')
  fireEvent.click(screen.getByRole('button', { name: '展开 AI 面板' }))
  fireEvent.click(screen.getByRole('button', { name: '放大 AI 面板' }))
  expect(document.querySelector('.strategy-workspace')?.getAttribute('data-ai-dock')).toBe('expanded')
  expect((screen.getByLabelText('AI draft') as HTMLInputElement).value).toBe('message')
  fireEvent.click(screen.getByRole('button', { name: '恢复双栏' }))
  expect((screen.getByLabelText('manual draft') as HTMLInputElement).value).toBe('work')
})


it('keeps one pane reachable when reopening narrow AI or narrowing an expanded desktop panel', () => {
  render(<ResearchSplitLayout><section className="strategy-column"><input aria-label="manual draft" defaultValue="work" /></section><aside className="strategy-ai"><input aria-label="AI draft" defaultValue="message" /></aside></ResearchSplitLayout>)
  const layout = document.querySelector('.strategy-workspace')!
  fireEvent.click(screen.getByRole('button', { name: '收起 AI 面板' }))
  fireEvent.click(screen.getByRole('button', { name: 'AI 协作' }))
  expect(layout.getAttribute('data-ai-dock')).toBe('split')
  expect(layout.getAttribute('data-workspace-view')).toBe('ai')
  fireEvent.click(screen.getByRole('button', { name: '主内容' }))
  fireEvent.click(screen.getByRole('button', { name: '放大 AI 面板' }))
  expect(layout.getAttribute('data-workspace-view')).toBe('ai')
  expect(layout.getAttribute('data-ai-dock')).toBe('expanded')
  fireEvent.click(screen.getByRole('button', { name: '主内容' }))
  expect(layout.getAttribute('data-ai-dock')).toBe('split')
  expect((screen.getByLabelText('manual draft') as HTMLInputElement).value).toBe('work')
  expect((screen.getByLabelText('AI draft') as HTMLInputElement).value).toBe('message')
})
