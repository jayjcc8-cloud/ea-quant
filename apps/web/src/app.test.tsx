import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App } from './app'

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/')
})

describe('EA Quant mock terminal', () => {
  it('presents the complete deterministic mock overview', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Overview' })).toBeTruthy()
    expect(screen.getByText('MOCK DATA')).toBeTruthy()
    ;[
      'Total Equity',
      'Daily P&L',
      'Unrealized P&L',
      'Available Margin',
      'Open Positions',
      'Active Orders',
      'Equity Curve',
      'Asset Allocation',
      'Recent Executions',
      'System Health',
      'Environment',
      'Last Sync',
    ].forEach((label) => expect(screen.getAllByText(label).length).toBeGreaterThan(0))
  })

  it('navigates to every read-only operational skeleton', async () => {
    const user = userEvent.setup()
    render(<App />)

    for (const page of [
      'Orders',
      'Executions',
      'Positions',
      'Cash',
      'Risk',
      'Recovery',
      'Audit',
      'System Health',
    ]) {
      await user.click(screen.getByRole('link', { name: page }))
      expect(screen.getByRole('heading', { name: page })).toBeTruthy()
      expect(screen.getByText('Mock workspace · read-only skeleton')).toBeTruthy()
    }
  })

  it('renders deterministic loading, empty, and error mock states', () => {
    window.history.pushState({}, '', '/?state=loading')
    const { rerender } = render(<App />)
    expect(screen.getByText('Loading mock market workspace…')).toBeTruthy()

    window.history.pushState({}, '', '/?state=empty')
    rerender(<App />)
    expect(screen.getByText('No mock positions in this workspace')).toBeTruthy()

    window.history.pushState({}, '', '/?state=error')
    rerender(<App />)
    expect(screen.getByText('Mock data is temporarily unavailable')).toBeTruthy()
  })

  it('does not present any trading or recovery mutation controls', () => {
    render(<App />)

    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(/place order|cancel order|recover now/i)).toBeNull()
  })
})
