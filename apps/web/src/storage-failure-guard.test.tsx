/** Direct keyboard, cancellation and navigation-replay regressions; no API or financial data. */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { MouseEvent } from 'react'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { SourceLink } from './workspace-navigation'
import { StorageFailureGuard } from './storage-failure-guard'

function LocationProbe() {
  const location = useLocation()
  const state = location.state as { mark?: string } | null
  return <output aria-label="location">{location.pathname}{location.search}·{state?.mark ?? ''}</output>
}

function Links({ showFirst = true }: { showFirst?: boolean }) {
  const stay = (event: MouseEvent<HTMLAnchorElement>) => event.preventDefault()
  return <>
    {showFirst && <SourceLink to="/next?tab=1" state={{ mark: 'kept' }} replace>first link</SourceLink>}
    <SourceLink to="/other">second link</SourceLink>
    <a href="#section" onClick={stay}>same-page anchor</a>
    <a href="/download.csv" download onClick={stay}>download link</a>
    <a href="/api/backtests/job-a/artifacts/report.json" onClick={stay}>artifact link</a>
    <a href="/blank" target="_blank" onClick={stay}>blank link</a>
    <a href="/plain" onClick={stay}>plain link</a>
  </>
}

const renderGuard = (active: boolean, message = '未保存输入在本机无法保留。') =>
  render(<BrowserRouter><Links /><StorageFailureGuard active={active} message={message} /><LocationProbe /></BrowserRouter>)
const location = () => screen.getByLabelText('location').textContent
afterEach(() => { cleanup(); window.history.replaceState({}, '', '/') })

it('coalesces simultaneous uncached inputs into one cancellable leave decision', async () => {
  window.history.replaceState({}, '', '/start')
  render(<BrowserRouter><Links /><StorageFailureGuard active message="草稿无法缓冲" />
    <StorageFailureGuard active message="消息无法缓冲" /><LocationProbe /></BrowserRouter>)
  fireEvent.click(screen.getByText('first link'))
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
  expect(location()).toBe('/start·')
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.click(screen.getByText('first link'))
  fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
  await waitFor(() => expect(location()).toBe('/next?tab=1·kept'))
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.click(screen.getByText('second link'))
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
})

it('asks in-app, traps focus, and both cancel paths keep the page and restore the link', () => {
  window.history.replaceState({}, '', '/start?tab=old')
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  try {
    renderGuard(true)
    const link = screen.getByText('first link')
    fireEvent.click(link)
    expect(confirm).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: '离开此页？' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-describedby')).toBeTruthy()
    expect(within(dialog).getByText('未保存输入在本机无法保留。')).toBeTruthy()
    const cancel = within(dialog).getByRole('button', { name: '继续编辑' })
    const leave = within(dialog).getByRole('button', { name: '放弃并离开' })
    expect(document.activeElement).toBe(cancel)

    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(leave)
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(link)
    expect(location()).toBe('/start?tab=old·')

    fireEvent.click(link)
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(link)
    expect(location()).toBe('/start?tab=old·')
  } finally { confirm.mockRestore() }
})

it('confirm replays the first destination exactly once with router state, query and replace', async () => {
  window.history.replaceState({}, '', '/start?tab=old')
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const push = vi.spyOn(window.history, 'pushState')
  const replace = vi.spyOn(window.history, 'replaceState')
  try {
    renderGuard(true)
    fireEvent.click(screen.getByText('first link'))
    fireEvent.click(screen.getByText('second link'))
    expect(screen.getByRole('dialog', { name: '离开此页？' })).toBeTruthy()
    expect(location()).toBe('/start?tab=old·')

    const pushesBefore = push.mock.calls.length
    const replacesBefore = replace.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    await waitFor(() => expect(location()).toBe('/next?tab=1·kept'))
    expect(confirm).not.toHaveBeenCalled()
    expect(replace.mock.calls.length).toBe(replacesBefore + 1)
    expect(push.mock.calls.length).toBe(pushesBefore)

    // The one-shot replay exemption is gone: a later leaving click asks again.
    fireEvent.click(screen.getByText('second link'))
    expect(screen.getByRole('dialog', { name: '离开此页？' })).toBeTruthy()
    expect(location()).toBe('/next?tab=1·kept')
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  } finally { confirm.mockRestore(); push.mockRestore(); replace.mockRestore() }
})

it('leaves modified, non-primary, download, new-window and hash clicks alone', () => {
  window.history.replaceState({}, '', '/start')
  renderGuard(true)
  fireEvent.click(screen.getByText('same-page anchor'))
  fireEvent.click(screen.getByText('download link'))
  fireEvent.click(screen.getByText('artifact link'))
  fireEvent.click(screen.getByText('blank link'))
  fireEvent.click(screen.getByText('plain link'), { button: 1 })
  fireEvent.click(screen.getByText('plain link'), { ctrlKey: true })
  fireEvent.click(screen.getByText('plain link'), { metaKey: true })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(location()).toMatch(/^\/start/)
})

it('keeps beforeunload, closes on inactive, and leaves no listener after unmount', () => {
  window.history.replaceState({}, '', '/start')
  const view = render(<BrowserRouter><Links /><StorageFailureGuard active message="x" /><LocationProbe /></BrowserRouter>)
  const blocked = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(blocked)
  expect(blocked.defaultPrevented).toBe(true)

  fireEvent.click(screen.getByText('first link'))
  expect(screen.getByRole('dialog')).toBeTruthy()
  view.rerender(<BrowserRouter><Links /><StorageFailureGuard active={false} message="x" /><LocationProbe /></BrowserRouter>)
  expect(screen.queryByRole('dialog')).toBeNull()
  const allowed = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(allowed)
  expect(allowed.defaultPrevented).toBe(false)

  fireEvent.click(screen.getByText('second link'))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(location()).toBe('/other·')

  view.unmount()
  const afterUnmount = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(afterUnmount)
  expect(afterUnmount.defaultPrevented).toBe(false)
})

it('closes safely when the triggering link was detached before confirmation', () => {
  window.history.replaceState({}, '', '/start')
  const view = render(<BrowserRouter><Links /><StorageFailureGuard active message="x" /><LocationProbe /></BrowserRouter>)
  fireEvent.click(screen.getByText('first link'))
  view.rerender(<BrowserRouter><Links showFirst={false} /><StorageFailureGuard active message="x" /><LocationProbe /></BrowserRouter>)
  fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(location()).toBe('/start·')
})
