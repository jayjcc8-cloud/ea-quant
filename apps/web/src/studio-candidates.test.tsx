import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StudioRoutes } from './studio-real'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter, BacktestJob, BacktestReport, ChronologicalHoldout } from './app'
import type { Candidate } from './candidates'

const relation: ChronologicalHoldout = { schema: 'ea.chronological-holdout.v1', validation_id: 'relation-1', created_at: '2026-10-29T07:00:00Z', source_job_id: 'source', holdout_job_id: 'later' }
const evidence = { job_id: 'source', run_id: 'run-source', report_sha256: 'report-source', equity_path_sha256: 'path-source', input_sha256: 'input-source', scenario_sha256: 'scenario-source', data_sha256: 'data-source', record_count: 3 }
const evaluated: Candidate = { candidate_id: 'candidate-1', status: 'EVALUATED', fingerprint: 'f'.repeat(64), decision: null,
  projection: { strategy: { id: 'bounded-long-v1', version: 1, parameters: { target_quantity: 2 } }, source: evidence, holdout: { ...evidence, job_id: 'later', run_id: 'run-later' }, relationship: { validation_id: 'relation-1', sha256: 'relationship-hash' } } }
function job(id: string): BacktestJob {
  return { schema: 'ea.local-web-job.v3', job_id: id, request_id: id, scenario_id: `${id}.yaml`, input_identity: { scenario_sha256: `${id}-scenario`, data_sha256: `${id}-data`, record_count: 3 }, status: 'succeeded', engine_run_id: `run-${id}`, report_ready: true, report_sha256: `report-${id}`, summary_sha256: null, error_code: null, message: null }
}
function report(id: string): BacktestReport {
  return { schema: 'ea.backtest-report.v1', run_id: `run-${id}`, source: { strategy: { id: 'bounded-long-v1' }, instrument: { venue: 'XNAS', symbol: 'AAPL' } }, economics: { currency: 'USD', initial_funding: { amount: '10000' }, ending_cash: [], ending_positions: [], valuation: { price: '110', position_value: '220' }, equity: { amount: '10018' }, net_pnl: { amount: '18' }, total_return: { value: '0.0018' }, counts: { orders: 1, fills: 1 }, execution: { order: null, fill: null } } }
}
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }), researchReferences: async () => [],
    getBacktest: async id => job(id), getReport: async id => report(id), getEquityPath: async () => { throw new Error('curve unavailable') },
    listHoldouts: async () => [relation], getHoldout: async () => relation, listCandidates: async () => [evaluated], getCandidate: async () => evaluated,
    createCandidate: vi.fn(async () => evaluated), decideCandidate: vi.fn(), prepareSimulation: vi.fn(), ...overrides }
}
function open(api: ApiAdapter, path = '/holdout/relation-1') {
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it('creates only the explicitly opened completed relationship and enters EVALUATED without execution', async () => {
  let finish!: (value: Candidate) => void
  const create = vi.fn(() => new Promise<Candidate>(resolve => { finish = resolve }))
  const api = adapter({ createCandidate: create })
  open(api)
  const button = await screen.findByRole('button', { name: '创建 EVALUATED 候选' })
  await waitFor(() => expect(button).toHaveProperty('disabled', false))
  expect(create).not.toHaveBeenCalled()
  fireEvent.click(button); fireEvent.click(button)
  expect(create).toHaveBeenCalledExactlyOnceWith('relation-1')
  finish(evaluated)
  await screen.findByRole('heading', { name: '研究候选' })
  expect(await screen.findByText('EVALUATED')).toBeTruthy()
  expect(api.decideCandidate).not.toHaveBeenCalled()
  expect(api.prepareSimulation).not.toHaveBeenCalled()
  expect(screen.queryByText('Local simulation preparation')).toBeNull()
})

it.each(['running', 'missing', 'mismatch'])('does not create from %s holdout evidence', async mode => {
  const api = adapter({ getBacktest: async id => id === 'source' ? job(id) : { ...job(id), status: mode === 'running' ? 'running' : 'succeeded', report_ready: mode !== 'missing' }, getReport: async id => ({ ...report(id), run_id: mode === 'mismatch' && id === 'later' ? 'foreign' : `run-${id}` }) })
  open(api)
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: '创建 EVALUATED 候选' })).toHaveProperty('disabled', true)
  expect(api.createCandidate).not.toHaveBeenCalled()
})

it('opens history and frozen detail with both v3 report links and no simulation preparation', async () => {
  const api = adapter()
  open(api, '/records')
  await userEvent.click(await screen.findByRole('link', { name: '研究候选历史' }))
  await userEvent.click(await screen.findByRole('link', { name: 'candidate-1' }))
  expect((await screen.findByRole('link', { name: '来源报告 · source' })).getAttribute('href')).toBe('/report/source/overview')
  expect(screen.getByRole('link', { name: '留出报告 · later' }).getAttribute('href')).toBe('/report/later/overview')
  expect(screen.getByRole('link', { name: '留出验证关系' }).getAttribute('href')).toBe('/holdout/relation-1')
  expect(screen.getByText('target_quantity: 2')).toBeTruthy()
  expect(screen.getByText(evaluated.fingerprint!)).toBeTruthy()
  expect(api.prepareSimulation).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('link', { name: '来源报告 · source' }))
  await screen.findByRole('heading', { name: '冻结报告' })
})

it.each(['ACCEPTED', 'REJECTED'] as const)('requires a human reason for %s and reopens the terminal record', async outcome => {
  let saved = evaluated
  const decide = vi.fn(async (id: string, value: string, reason: string): Promise<Candidate> => {
    saved = { ...evaluated, candidate_id: id, status: value as typeof outcome, decision: { outcome: value, reason, decided_at: '2026-10-29T08:00:00Z' } }; return saved
  })
  const api = adapter({ getCandidate: async () => saved, decideCandidate: decide })
  const view = open(api, '/candidates/candidate-1')
  const accept = await screen.findByRole('button', { name: '接受候选' })
  expect(accept).toHaveProperty('disabled', true)
  expect(screen.getByRole('button', { name: '拒绝候选' })).toHaveProperty('disabled', true)
  await userEvent.type(screen.getByLabelText('决定理由'), '已核对两份合成证据，仅验证流程。')
  await userEvent.click(screen.getByRole('button', { name: outcome === 'ACCEPTED' ? '接受候选' : '拒绝候选' }))
  await screen.findByText(outcome)
  expect(decide).toHaveBeenCalledExactlyOnceWith('candidate-1', outcome, '已核对两份合成证据，仅验证流程。')
  view.unmount(); open(api, '/candidates/candidate-1')
  await screen.findByText('已核对两份合成证据，仅验证流程。')
  expect(screen.queryByRole('button', { name: '接受候选' })).toBeNull()
  expect(screen.queryByRole('button', { name: '拒绝候选' })).toBeNull()
  expect(decide).toHaveBeenCalledTimes(1)
  expect(api.prepareSimulation).not.toHaveBeenCalled()
})

it('retains unknown creation across remount and queries history without resending', async () => {
  const api = adapter({ createCandidate: vi.fn().mockRejectedValue(new Error('reply lost')) })
  const view = open(api)
  const button = await screen.findByRole('button', { name: '创建 EVALUATED 候选' })
  await waitFor(() => expect(button).toHaveProperty('disabled', false))
  await userEvent.click(button)
  await screen.findByText(/创建结果未确认/)
  view.unmount(); open(api)
  await userEvent.click(await screen.findByRole('link', { name: '查询此留出的候选' }))
  await userEvent.click(await screen.findByRole('link', { name: 'candidate-1' }))
  await screen.findByText('EVALUATED')
  expect(api.createCandidate).toHaveBeenCalledTimes(1)
})

it('reads back an uncertain decision without replaying it', async () => {
  let saved = evaluated
  const decide = vi.fn(async () => { saved = { ...evaluated, status: 'ACCEPTED', decision: { outcome: 'ACCEPTED', reason: '人工判断', decided_at: '2026-10-29T08:00:00Z' } }; throw new Error('reply lost') })
  const api = adapter({ getCandidate: async () => saved, decideCandidate: decide })
  open(api, '/candidates/candidate-1')
  await userEvent.type(await screen.findByLabelText('决定理由'), '人工判断')
  await userEvent.click(screen.getByRole('button', { name: '接受候选' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: '接受候选' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '重新读取候选' }))
  await screen.findByText('ACCEPTED')
  expect(decide).toHaveBeenCalledTimes(1)
})

it('shows unavailable history independently and refuses decisions after an evidence read failure', async () => {
  const api = adapter({ listCandidates: async () => [evaluated, { candidate_id: 'broken', status: 'UNAVAILABLE' }], getCandidate: async () => { throw new Error('evidence mismatch') } })
  open(api, '/candidates')
  await screen.findByText('UNAVAILABLE')
  await userEvent.click(screen.getByRole('link', { name: 'candidate-1' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: '接受候选' })).toBeNull()
  expect(api.decideCandidate).not.toHaveBeenCalled()
})
