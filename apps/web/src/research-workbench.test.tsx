import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { App } from './app'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ResearchSummary } from './research-workbench-api'
const row: ResearchSummary = { user_strategy_id: 'idea', name: '新的问题', intent: '研究市场', exchange_id: 'binance', updated_at: '2026-10-23', instrument: null, draft: {kind:'intent', revision:1, pending_proposals:0}, experiment_count:0, latest_experiment:null, next_step:'draft' }
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/strategies') })
afterEach(cleanup)
it('renders the product collection without fetching or auto-selecting a full research document', async () => {
  const get = vi.fn()
  render(<App api={{...scriptedAdapter(), listResearch: async () => ({items:[row], total:1, matched:1}), getUserStrategy:get}} />)
  await screen.findByRole('link', {name:row.name})
  expect(get).not.toHaveBeenCalled()
  expect(within(screen.getByRole('region',{name:'AI 协作'})).getByText('研究工作区')).toBeTruthy()
  expect(screen.getByRole('columnheader',{name:'草稿状态'})).toBeTruthy()
  expect(screen.getByRole('columnheader',{name:'实验'})).toBeTruthy()
})
it('sends search and exchange filtering to the collection owner', async () => {
  const listResearch = vi.fn(async () => ({items:[row], total:1, matched:1}))
  const user = userEvent.setup()
  render(<App api={{...scriptedAdapter(), listResearch}} />)
  await screen.findByRole('link', {name:row.name})
  await user.click(screen.getByRole('button',{name:'Binance 未接入'}))
  await user.click(screen.getByRole('button',{name:'当前交易所'}))
  await user.type(screen.getByRole('searchbox',{name:'搜索研究'}),'均线')
  await waitFor(() => expect(listResearch).toHaveBeenLastCalledWith({q:'均线', exchange_id:'binance', sort:'updated'}))
})
it('clears AI context when a server filter excludes the explicit selection and ignores late reads', async () => {
  const { researchCollection } = await import('../preview/research-fixtures')
  const full = {schema:'ea.user-strategy.v1' as const, user_strategy_id:row.user_strategy_id, name:row.name, intent:row.intent, created_at:row.updated_at, updated_at:row.updated_at, exchange_id:row.exchange_id, draft:{kind:'intent' as const, revision:1,scenario_id:null}, history:[],messages:[],calls:[],proposals:[],changes:[],experiments:[]}
  let finish!: (value: typeof full) => void
  const get = vi.fn(() => new Promise<typeof full>(resolve => { finish = resolve }))
  const user = userEvent.setup()
  render(<App api={{...scriptedAdapter(), listResearch:async query => researchCollection([full],query), getUserStrategy:get}} />)
  await user.click(await screen.findByRole('button',{name:`选择研究 ${row.name}`}))
  expect(await screen.findByText('正在读取这条策略的对话…')).toBeTruthy()
  await user.type(screen.getByRole('searchbox',{name:'搜索研究'}),'不存在')
  await screen.findByText('没有匹配的研究')
  finish(full)
  await waitFor(() => expect(within(screen.getByRole('region',{name:'AI 协作'})).getByText('研究工作区')).toBeTruthy())
  expect(screen.queryByLabelText('消息')).toBeNull()
})
it('keeps an exchange refusal beside its row and only retries on an explicit save', async () => {
  const assign = vi.fn().mockRejectedValue(new Error('research changed; refresh before assigning its exchange'))
  const user = userEvent.setup()
  render(<App api={{...scriptedAdapter(),listResearch:async () => ({items:[row],total:1,matched:1}),assignResearchExchange:assign}} />)
  await user.click(await screen.findByRole('button',{name:`整理研究 ${row.name}`}))
  await user.selectOptions(screen.getByLabelText('研究归属'),'okx')
  await user.click(screen.getByRole('button',{name:'保存归属'}))
  const alert = await screen.findByRole('alert')
  expect(alert.closest('tr')?.textContent).toContain(row.name)
  expect(alert.textContent).toContain('research changed')
  expect(assign).toHaveBeenCalledExactlyOnceWith(row.user_strategy_id,'okx',row.updated_at)
})
it('organizing another row leaves the selected research conversation mounted', async () => {
  const { researchCollection } = await import('../preview/research-fixtures')
  const a = {schema:'ea.user-strategy.v1' as const,user_strategy_id:row.user_strategy_id,name:row.name,intent:row.intent,created_at:row.updated_at,updated_at:row.updated_at,draft:{kind:'intent' as const,revision:1,scenario_id:null},history:[],messages:[],calls:[],proposals:[],changes:[],experiments:[]}
  const b = {...a,user_strategy_id:'other',name:'另一项研究'}
  const user = userEvent.setup()
  render(<App api={{...scriptedAdapter(),listResearch:async query=>researchCollection([a,b],query),getUserStrategy:async id=>id===a.user_strategy_id?a:b,assignResearchExchange:async ()=>({...b,exchange_id:'okx'})}} />)
  await user.click(await screen.findByRole('button',{name:`选择研究 ${a.name}`}))
  const composer = await screen.findByLabelText('消息')
  await user.click(screen.getByRole('button',{name:`整理研究 ${b.name}`}))
  await user.selectOptions(screen.getByLabelText('研究归属'),'okx')
  await user.click(screen.getByRole('button',{name:'保存归属'}))
  await waitFor(()=>expect(screen.queryByRole('button',{name:'保存归属'})).toBeNull())
  expect(screen.getByLabelText('消息')).toBe(composer)
  expect(within(screen.getByRole('region',{name:'AI 协作'})).getByText(a.name,{exact:true})).toBeTruthy()
})
