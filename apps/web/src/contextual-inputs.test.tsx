import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './app'
import { scriptedAdapter } from '../preview/first-use-fixtures'

afterEach(() => { cleanup(); sessionStorage.clear() })

it('keeps the collection free of aggregate preparation and unnecessary data reads', async () => {
  const listDatasets = vi.fn(async () => [])
  const listScenarios = vi.fn(async () => [])
  const settingsStatus = vi.fn(scriptedAdapter().settingsStatus)
  window.history.replaceState({}, '', '/strategies')
  render(<App api={{ ...scriptedAdapter(), listDatasets, listScenarios, settingsStatus }} />)
  await screen.findByRole('heading', { name: '研究工作台' })
  expect(screen.queryByText(/^研究准备/)).toBeNull()
  expect(listDatasets).not.toHaveBeenCalled()
  expect(listScenarios).not.toHaveBeenCalled()
  expect(settingsStatus).not.toHaveBeenCalled()
})

it('keeps resource category navigation available on model connections', async () => {
  window.history.replaceState({}, '', '/resources/models')
  render(<App api={scriptedAdapter()} />)
  const navigation = screen.getByRole('navigation', { name: '资源分类' })
  expect(within(navigation).getByRole('link', { name: '模型连接' }).getAttribute('aria-current')).toBe('page')
  await userEvent.setup().click(within(navigation).getByRole('link', { name: '数据' }))
  expect(window.location.pathname).toBe('/resources/data')
})

it('stages an assistant idea without creating research and retains it when the form is closed', async () => {
  const createUserStrategy = vi.fn()
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies')
  render(<App api={{ ...scriptedAdapter(), createUserStrategy }} />)
  await user.type(screen.getByRole('textbox', { name: '研究想法' }), '观察不同波动阶段的表现')
  await user.click(screen.getByRole('button', { name: '起草研究' }))
  const form = screen.getByRole('region', { name: '新建研究' })
  expect((within(form).getByRole('textbox', { name: '意图' }) as HTMLTextAreaElement).value).toBe('观察不同波动阶段的表现')
  expect(createUserStrategy).not.toHaveBeenCalled()
  await user.click(within(form).getByRole('button', { name: '收起（保留输入）' }))
  await user.click(screen.getByRole('button', { name: /新建研究/ }))
  expect((screen.getByRole('textbox', { name: '意图' }) as HTMLTextAreaElement).value).toBe('观察不同波动阶段的表现')
  expect(createUserStrategy).not.toHaveBeenCalled()
})

it('recovers a failed template directory in place without creating research', async () => {
  const listScenarios = vi.fn().mockRejectedValueOnce(new Error('目录暂不可读')).mockResolvedValueOnce([])
  const createUserStrategy = vi.fn()
  window.history.replaceState({}, '', '/resources/extensions')
  render(<App api={{ ...scriptedAdapter(), listScenarios, createUserStrategy, localStrategySlot: undefined, inspectLocalStrategy: undefined }} />)
  expect((await screen.findByRole('alert')).textContent).toContain('目录暂不可读')
  await userEvent.click(screen.getByRole('button', { name: '刷新策略目录' }))
  await screen.findByText('暂无可用策略。可刷新目录，或检查本地策略包。')
  expect(listScenarios).toHaveBeenCalledTimes(2)
  expect(createUserStrategy).not.toHaveBeenCalled()
})
