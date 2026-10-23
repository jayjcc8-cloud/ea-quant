import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './app'
import { scriptedAdapter } from '../preview/first-use-fixtures'

afterEach(() => { cleanup(); sessionStorage.clear() })

it('organizes the shell by research, local simulation and reusable resources', async () => {
  window.history.pushState({}, '', '/strategies')
  render(<App api={scriptedAdapter()} />)
  const nav = within(screen.getByRole('navigation', { name: '主导航' }))
  expect(nav.getByRole('link', { name: '研究' }).getAttribute('href')).toBe('/strategies')
  expect(nav.getByRole('link', { name: '模拟运行' }).getAttribute('href')).toBe('/runs')
  expect(screen.getByRole('link', { name: '资源' }).getAttribute('href')).toBe('/resources/data')
  expect(nav.queryByRole('link', { name: '数据' })).toBeNull()
  expect(nav.queryByRole('link', { name: '扩展' })).toBeNull()
  await screen.findByRole('heading', { name: '研究工作台' })
})

it('keeps a legacy import deep link bound to the same research and revision', async () => {
  const query = '?research=us-1&revision=3&return=%2Fstrategies%2Fus-1'
  window.history.pushState({}, '', `/data${query}#data-upload`)
  const api = scriptedAdapter()
  const publish = vi.fn(api.publishUpload)
  render(<App api={{ ...api, publishUpload: publish }} />)
  await screen.findByRole('heading', { name: '资源' })
  expect(window.location.pathname).toBe('/resources/data')
  expect(window.location.search).toBe(query)
  expect(window.location.hash).toBe('#data-upload')
  expect(screen.getByRole('navigation', { name: '资源分类' })).toBeTruthy()
  expect(publish).not.toHaveBeenCalled()
})

it('opens existing research history without presenting a competing new-idea form', async () => {
  window.history.pushState({}, '', '/research')
  const create = vi.fn()
  render(<App api={{ ...scriptedAdapter(), listResearchTasks: async () => [], createResearchTask: create }} />)
  await screen.findByRole('heading', { name: '历史研究任务' })
  expect(screen.queryByRole('button', { name: 'Create draft' })).toBeNull()
  expect(screen.getByRole('link', { name: '新建研究' }).getAttribute('href')).toBe('/strategies')
  expect(create).not.toHaveBeenCalled()
})

it('redirects the old research upload anchor with its original query intact', async () => {
  const query = '?research=us-1&revision=3&return=%2Fstrategies%2Fus-1'
  window.history.pushState({}, '', `/research${query}#data-upload`)
  render(<App api={{ ...scriptedAdapter(), listResearchTasks: async () => [] }} />)
  await screen.findByRole('heading', { name: '资源' })
  expect(window.location.pathname).toBe('/resources/data')
  expect(window.location.search).toBe(query)
  expect(document.getElementById('data-upload')).not.toBeNull()
})


it('returns from model management to the exact research evidence after a remount', async () => {
  const user = userEvent.setup()
  const source = '/strategies/us-1?tab=experiments&evidence_a=job-1'
  window.history.replaceState({}, '', source)
  const api = scriptedAdapter()
  const view = render(<App api={api} />)
  await user.click(await screen.findByRole('link', { name: '配置模型' }))
  await screen.findByRole('heading', { name: '模型连接' })
  view.unmount()
  render(<App api={api} />)
  const back = await screen.findByRole('link', { name: '返回当前研究' })
  expect(back.getAttribute('href')).toBe(source)
  await user.click(back)
  expect(window.location.pathname + window.location.search).toBe(source)
})

it('keeps import origin when switching resource categories', async () => {
  const user = userEvent.setup()
  const query = '?research=us-1&revision=3&return=%2Fstrategies%2Fus-1%3Ftab%3Dstrategy'
  window.history.replaceState({}, '', `/resources/data${query}`)
  render(<App api={scriptedAdapter()} />)
  await user.click(screen.getByRole('link', { name: '策略与扩展' }))
  expect(window.location.search).toBe(query)
  expect(screen.getByRole('link', { name: '返回当前研究' }).getAttribute('href')).toBe('/strategies/us-1?tab=strategy')
})

it('retains a new research idea through cross-section navigation without saving it', async () => {
  const user = userEvent.setup()
  window.history.replaceState({}, '', '/strategies')
  const api = scriptedAdapter()
  const create = vi.fn(api.createUserStrategy)
  render(<App api={{ ...api, createUserStrategy: create }} />)
  await user.click((await screen.findAllByRole('button', { name: '新建研究' }))[0])
  await user.type(screen.getByLabelText('意图'), '尚未保存的研究想法')
  await user.click(screen.getByRole('link', { name: '资源' }))
  await user.click(screen.getByRole('link', { name: '返回研究' }))
  await user.click((await screen.findAllByRole('button', { name: '新建研究' }))[0])
  expect((screen.getByLabelText('意图') as HTMLTextAreaElement).value).toBe('尚未保存的研究想法')
  expect(create).not.toHaveBeenCalled()
})


it('keeps model editing in resources and application preferences in settings', async () => {
  window.history.replaceState({}, '', '/settings')
  const view = render(<App api={scriptedAdapter()} />)
  await screen.findByRole('heading', { name: '设置' })
  expect(screen.queryByLabelText('新 API Key')).toBeNull()
  expect(screen.getByRole('link', { name: '管理模型连接' }).getAttribute('href')).toBe('/resources/models')
  view.unmount()
})


it('opens an unconnected exchange configuration without a business-navigation duplicate', async () => {
  window.history.replaceState({}, '', '/platform/binance')
  render(<App api={scriptedAdapter()} />)
  expect(screen.getByRole('heading', { name: 'Binance 交易所配置' })).toBeTruthy()
  expect(screen.queryByRole('navigation', { name: '主导航' })).toBeNull()
  expect(screen.getByRole('link', { name: '返回工作页' }).getAttribute('href')).toBe('/strategies')
})

it('selects primary areas directly without history arrows and retains list filters and unsaved input', async () => {
  window.history.replaceState({}, '', '/strategies?scope=all&search=fixture&sort=name')
  const user = userEvent.setup()
  const create = vi.fn()
  render(<App api={{ ...scriptedAdapter(), createUserStrategy: create }} />)
  await user.click((await screen.findAllByRole('button', { name: '新建研究' }))[0])
  await user.type(screen.getByLabelText('意图'), '保留一级切换前的输入')
  for (const [name, path] of [['模拟运行', '/runs'], ['实盘运行', '/live'], ['回测记录', '/backtests'], ['研究', '/strategies']]) {
    const nav = within(screen.getByRole('navigation', { name: '主导航' }))
    await user.click(nav.getByRole('link', { name }))
    expect(window.location.pathname).toBe(path)
    expect(screen.queryByRole('button', { name: '后退' })).toBeNull()
    expect(screen.queryByRole('button', { name: '前进' })).toBeNull()
    expect(screen.queryByRole('navigation', { name: '页面导航' })).toBeNull()
  }
  expect((await screen.findByRole('searchbox', { name: '搜索研究' }) as HTMLInputElement).value).toBe('fixture')
  expect((screen.getByLabelText('研究排序') as HTMLSelectElement).value).toBe('name')
  await user.click(screen.getAllByRole('button', { name: '新建研究' })[0])
  expect((screen.getByLabelText('意图') as HTMLTextAreaElement).value).toBe('保留一级切换前的输入')
  expect(create).not.toHaveBeenCalled()
})
