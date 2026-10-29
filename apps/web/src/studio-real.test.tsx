import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { StrictMode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StudioRoutes } from './studio-real'
import { scriptedAdapter } from '../preview/first-use-fixtures'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'
import { UserStrategyRequestError, type UserStrategy, type UserStrategyPreview } from './user-strategies-api'

const strategy: UserStrategy = {
  schema: 'ea.user-strategy.v1', user_strategy_id: 'real-idea', name: '明确保存的研究', intent: '检验成本',
  created_at: '2026-10-28', updated_at: '2026-10-28',
  draft: { revision: 1, scenario_id: 'sample.yaml', strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', initial_cash: '10000', parameters: { target_quantity: 1 }, dataset_id: null, source_sha256: null, criterion: null },
  history: [], experiments: [], calls: [], messages: [], proposals: [], changes: [],
}
const preview: UserStrategyPreview = {
  ...strategy.draft as Exclude<UserStrategy['draft'], { kind: 'intent' }>, draft_revision: 1, owner: 'quine',
  record_count: 12, data: {}, input_identity: {}, input_sha256: 'input', preview_identity: 'verified-preview',
  normalized: { execution: { commission_bps: '1' } }, commission: null, slippage: null, latency: null,
  budget: { max_model_calls: 0, max_backtests: 1 },
}
function adapter(overrides: Partial<ApiAdapter> = {}): ApiAdapter {
  return { ...scriptedAdapter(), listResearch: async () => ({ items: [], total: 0, matched: 0 }),
    getUserStrategy: async () => structuredClone(strategy), listScenarios: async () => [], listDatasets: async () => [],
    previewUserStrategyExperiment: async () => preview,
    userStrategyDraftContract: async () => ({ draft_revision: 1, scenario_id: 'sample.yaml', source: 'registered', fields: [] }),
    ...overrides }
}
function open(api: ApiAdapter, path: string) {
  return render(<MemoryRouter initialEntries={[path]}><StudioRoutes api={api} /></MemoryRouter>)
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)

it.each(['概览', '实验'])('guards uncached draft edits before leaving through the %s tab', async label => {
  const save = vi.fn(), launch = vi.fn()
  const api = adapter({ saveUserStrategyDraft: save, launchUserStrategyExperiment: launch,
    userStrategyDraftContract: async () => ({ draft_revision: 1, scenario_id: 'sample.yaml', source: 'registered',
      fields: [{ path: 'parameters.target_quantity', kind: 'integer', json_type: 'integer', current: 1, minimum: 0, maximum: 100, editable: true }] }) })
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
  try {
    open(api, '/research/real-idea/draft')
    fireEvent.change(await screen.findByLabelText('目标持仓数量'), { target: { value: '17' } })
    await screen.findByText('本地缓冲不可用，离开前请保存或取消。')
    const tab = () => within(screen.getByRole('navigation', { name: '页面标签' })).getByText(label)
    await userEvent.click(tab())
    expect(screen.getByRole('dialog', { name: '离开此页？' })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect((screen.getByLabelText('目标持仓数量') as HTMLInputElement).value).toBe('17')
    await userEvent.click(tab())
    await userEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    await screen.findByRole('heading', { name: label === '实验' ? '实验记录' : '研究问题' })
    expect(screen.queryByLabelText('目标持仓数量')).toBeNull()
    expect(save).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
  } finally { write.mockRestore() }
})

it('restores an unsent assistant message after experiment preparation without sending it', async () => {
  const send = vi.fn(), launch = vi.fn()
  const api = adapter({ sendUserStrategyMessage: send, launchUserStrategyExperiment: launch })
  const view = open(api, '/research/real-idea/overview')
  await userEvent.type(await screen.findByLabelText('消息'), '比较费用变化后再决定实验')
  await userEvent.click(screen.getByRole('link', { name: '准备一次实验' }))
  await screen.findByRole('heading', { name: '确认一次实验' })
  await userEvent.click(screen.getByRole('link', { name: '返回修改' }))
  expect((await screen.findByLabelText('消息') as HTMLTextAreaElement).value).toBe('比较费用变化后再决定实验')
  view.unmount()
  const reopened = open(api, '/research/real-idea/overview')
  expect((await screen.findByLabelText('消息') as HTMLTextAreaElement).value).toBe('比较费用变化后再决定实验')
  reopened.unmount()
  open(adapter({ getUserStrategy: async () => ({ ...strategy, user_strategy_id: 'other-idea' }) }), '/research/other-idea/overview')
  expect((await screen.findByLabelText('消息') as HTMLTextAreaElement).value).toBe('')
  expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('keeps newer unsent typing when an earlier model receipt arrives', async () => {
  let finish!: (value: UserStrategy) => void
  const send = vi.fn<NonNullable<ApiAdapter['sendUserStrategyMessage']>>(() => new Promise<UserStrategy>(resolve => { finish = resolve }))
  const api = adapter({ sendUserStrategyMessage: send, settingsStatus: configuredModel })
  const view = open(api, '/research/real-idea/overview')
  const input = await screen.findByLabelText('消息')
  fireEvent.change(input, { target: { value: '第一个问题' } })
  await waitFor(() => expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false))
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  fireEvent.change(input, { target: { value: '尚未发送的下一个问题' } })
  finish({ ...strategy, calls: [{ call_id: send.mock.calls[0][1].call_id, status: 'observed', base_revision: 1, route: {}, usage_available: false } as UserStrategy['calls'][number]] })
  await waitFor(() => expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('尚未发送的下一个问题')
  view.unmount()
  open(api, '/research/real-idea/overview')
  expect((await screen.findByLabelText('消息') as HTMLTextAreaElement).value).toBe('尚未发送的下一个问题')
  expect(send).toHaveBeenCalledTimes(1)
})

it('asks before losing an assistant message when its session buffer cannot be written', async () => {
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
  try {
    open(adapter(), '/research/real-idea/overview')
    await userEvent.type(await screen.findByLabelText('消息'), '保留这个问题')
    await userEvent.click(screen.getByRole('link', { name: '准备一次实验' }))
    expect(screen.getByRole('dialog', { name: '离开此页？' })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('保留这个问题')
    await userEvent.click(screen.getByRole('link', { name: '准备一次实验' }))
    await userEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    await screen.findByRole('heading', { name: '确认一次实验' })
  } finally { write.mockRestore() }
})

it('opens workspace settings from the real navigation and saves only an explicit next-launch selection', async () => {
  const status = { workspace: '/fixture/current', next_workspace: '/fixture/current', theme: 'system' as const, startup_error: null,
    model: { schema: 'radian.settings-status.v1' as const, settings_file: '/fixture/current/settings.json', api_key_env: null, provider: null, model: null, base_url: null, credential_available: false, configured: false, request_validation: 'not_verified' as const } }
  const save = vi.fn(async () => ({ ...status, next_workspace: '/fixture/new' })), launch = vi.fn()
  open(adapter({ desktopSettings: async () => status, saveDesktopSettings: save, launchUserStrategyExperiment: launch }), '/')
  await userEvent.click(screen.getByRole('link', { name: '工作区位置' }))
  const path = await screen.findByLabelText('工作区绝对路径')
  await userEvent.clear(path); await userEvent.type(path, '/fixture/new')
  expect(save).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '保存工作区位置' }))
  await screen.findByText('已保存下次启动位置；当前工作区未切换。请等待工作结束后退出并重开。')
  expect(save).toHaveBeenCalledExactlyOnceWith({ next_workspace: '/fixture/new' })
  expect(screen.getByText('/fixture/current')).toBeTruthy()
  expect(screen.queryByLabelText('新 API Key')).toBeNull()
  expect(launch).not.toHaveBeenCalled()
})

it('saves an idea through the real contract without a model or experiment call', async () => {
  const create = vi.fn(async () => strategy), launch = vi.fn(), message = vi.fn()
  open(adapter({ createUserStrategy: create, launchUserStrategyExperiment: launch, sendUserStrategyMessage: message }), '/')
  await userEvent.type(screen.getByLabelText('研究目标'), '检验成本')
  await userEvent.click(screen.getByRole('button', { name: '保存研究并继续' }))
  await screen.findByRole('heading', { name: strategy.name })
  expect(create).toHaveBeenCalledExactlyOnceWith({ name: '检验成本', intent: '检验成本' })
  expect(launch).not.toHaveBeenCalled(); expect(message).not.toHaveBeenCalled()
})

it('consumes one preview once and retains a lost receipt as query-only across remount', async () => {
  const launch = vi.fn().mockRejectedValue(new Error('receipt lost'))
  const api = adapter({ launchUserStrategyExperiment: launch })
  const view = open(api, '/research/real-idea/confirm')
  const confirm = await screen.findByRole('button', { name: '确认并运行一次' })
  fireEvent.click(confirm); fireEvent.click(confirm)
  await screen.findByText(/确认结果未知/)
  expect(launch).toHaveBeenCalledTimes(1)
  expect(launch.mock.calls[0][1]).toMatchObject({ base_revision: 1, preview_identity: 'verified-preview', approved_budget: { max_model_calls: 0, max_backtests: 1 } })
  view.unmount()
  open(api, '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '查询原请求' }))
  await screen.findByText(/尚未读到原请求/)
  expect(screen.queryByRole('button', { name: '确认并运行一次' })).toBeNull()
  expect(launch).toHaveBeenCalledTimes(1)
})

it('reads a server-owned unknown on reopen without offering a new confirmation', async () => {
  const launch = vi.fn(), readPreview = vi.fn()
  const unknown = { launch_intent_id: 'original', status: 'unknown', owner: 'quine', payload: { draft_revision: 1 }, job_id: null }
  const api = adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [unknown] } as UserStrategy), launchUserStrategyExperiment: launch, previewUserStrategyExperiment: readPreview })
  open(api, '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '查询原请求' }))
  expect(screen.getByRole('alert').textContent).toContain('原请求 original：unknown')
  expect(launch).not.toHaveBeenCalled(); expect(readPreview).not.toHaveBeenCalled()
})

it('refuses mismatched report identity instead of presenting success', async () => {
  open(adapter({ getBacktest: async () => ({ job_id: 'job', engine_run_id: 'run', status: 'succeeded', report_ready: true } as BacktestJob),
    getReport: async () => ({ run_id: 'foreign' } as BacktestReport) }), '/report/job/overview')
  await screen.findByText(/报告的身份与该运行/)
  expect(screen.queryByRole('region', { name: '正式报告关键结果' })).toBeNull()
})

it('shows unsupported routes as unavailable and never loads synthetic scenario state', async () => {
  const list = vi.fn()
  open(adapter({ listResearch: list }), '/simulations')
  await screen.findByRole('heading', { name: '此候选尚未接入' })
  await waitFor(() => expect(list).not.toHaveBeenCalled())
  expect(localStorage.length).toBe(0)
})

it('shows an unreadable real preview as a refusal with no fabricated result', async () => {
  const launch = vi.fn()
  open(adapter({ previewUserStrategyExperiment: async () => { throw new Error('dataset missing') }, launchUserStrategyExperiment: launch }), '/research/real-idea/confirm')
  await screen.findByText('dataset missing')
  expect(screen.queryByRole('button', { name: '确认并运行一次' })).toBeNull()
  expect(launch).not.toHaveBeenCalled()
})

it('uses one explicit scripted model call and applies its proposal without running', async () => {
  const proposal = { proposal_id: 'p', call_id: 'script', base_revision: 1, status: 'open' as const, created_at: '2026-10-28', applied_paths: [], stale_paths: [], items: [{ path: 'parameters.target_quantity', before: 1, after: 2, reason: 'scripted interaction', evidence: [], proposer: 'script' }] }
  const proposed = { ...strategy, proposals: [proposal] }
  const send = vi.fn(async (_id: string, input: { call_id: string }) => ({ ...proposed, calls: [{ call_id: input.call_id, status: 'observed', base_revision: 1, route: {}, usage_available: false } as UserStrategy['calls'][number]] }))
  const apply = vi.fn(async () => ({ ...proposed, draft: { ...strategy.draft, revision: 2 }, proposals: [{ ...proposal, status: 'applied' as const }] }))
  const launch = vi.fn()
  const api = adapter({ sendUserStrategyMessage: send, applyUserStrategyProposal: apply, launchUserStrategyExperiment: launch,
    settingsStatus: async () => ({ configured: true, provider: 'scripted', model: 'no-network', credential_available: false }) as Awaited<ReturnType<NonNullable<ApiAdapter['settingsStatus']>>> })
  render(<StrictMode><MemoryRouter initialEntries={['/research/real-idea/overview']}><StudioRoutes api={api} /></MemoryRouter></StrictMode>)
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await userEvent.click(await screen.findByRole('link', { name: '审阅修改提案' }))
  await screen.findByRole('heading', { name: '审阅修改提案' })
  expect(apply).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
  await userEvent.click(await screen.findByRole('button', { name: '应用修改到草稿' }))
  await screen.findByText('草稿 r2 · XNAS:AAPL')
  expect(send).toHaveBeenCalledTimes(1)
  expect(send).toHaveBeenCalledWith('real-idea', expect.objectContaining({ approved_budget: { max_model_calls: 1, max_backtests: 0 } }))
  expect(apply).toHaveBeenCalledTimes(1)
  expect(launch).not.toHaveBeenCalled()
})


it('clears a definite pre-dispatch rejection and requires a fresh preview and click', async () => {
  const launch = vi.fn().mockRejectedValue(new UserStrategyRequestError('stale_base_revision', 'preview changed'))
  const readPreview = vi.fn(async () => preview)
  open(adapter({ launchUserStrategyExperiment: launch, previewUserStrategyExperiment: readPreview }), '/research/real-idea/confirm')
  await userEvent.click(await screen.findByRole('button', { name: '确认并运行一次' }))
  await screen.findByText(/本次未派发/)
  await userEvent.click(screen.getByRole('button', { name: '重新读取并预览' }))
  await screen.findByRole('button', { name: '确认并运行一次' })
  expect(sessionStorage.getItem('quine.studio.confirm.real-idea')).toBeNull()
  expect(readPreview).toHaveBeenCalledTimes(2)
  expect(launch).toHaveBeenCalledTimes(1)
})

it('allows a fresh preview after the owner released an unknown slot without claiming success', async () => {
  const released = { launch_intent_id: 'released-original', status: 'unknown', owner: 'quine', slot_held: false, recovery: { status: 'released' }, payload: { draft_revision: 1 } }
  open(adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [released] } as UserStrategy) }), '/research/real-idea/confirm')
  await screen.findByRole('button', { name: '确认并运行一次' })
  expect(screen.queryByText(/原请求.*released-original/)).toBeNull()
})

const configuredModel = async () => ({ configured: true, provider: 'scripted', model: 'no-network', credential_available: false }) as Awaited<ReturnType<NonNullable<ApiAdapter['settingsStatus']>>>
it('shows a server unknown model call and identity after reopening and permits a separate question', async () => {
  let doc = structuredClone(strategy)
  const send = vi.fn(async (_id: string, input: { call_id: string }) => {
    doc = { ...doc, calls: [{ call_id: input.call_id, status: 'unknown', base_revision: 1, route: {}, message: 'receipt missing', usage_available: false } as UserStrategy['calls'][number]] }
    return doc
  })
  const api = adapter({ getUserStrategy: async () => doc, sendUserStrategyMessage: send, settingsStatus: configuredModel })
  const view = open(api, '/research/real-idea/overview')
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await screen.findByText(/结果与用量未知/)
  const identity = send.mock.calls[0][1].call_id
  view.unmount()
  open(api, '/research/real-idea/overview')
  await screen.findByText(identity)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  expect(send).toHaveBeenCalledTimes(1)
  await userEvent.type(screen.getByLabelText('消息'), '另一个独立问题')
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(false)
})

it('retains transport-unknown model identity across remount and queries without resending', async () => {
  const send = vi.fn().mockRejectedValue(new Error('transport lost'))
  const api = adapter({ sendUserStrategyMessage: send, settingsStatus: configuredModel })
  const view = open(api, '/research/real-idea/overview')
  await userEvent.type(await screen.findByLabelText('消息'), '检查参数')
  await userEvent.click(screen.getByRole('button', { name: '发送一次模型请求' }))
  await screen.findByText(/transport lost/)
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  const identity = send.mock.calls[0][1].call_id
  view.unmount()
  open(api, '/research/real-idea/overview')
  await screen.findByText(identity)
  await userEvent.click(screen.getByRole('button', { name: '查询模型请求' }))
  await screen.findByText(/尚未读到此调用/)
  expect(send).toHaveBeenCalledTimes(1)
})

it('opens the existing CSV deep link and returns to the same real draft without running', async () => {
  const launch = vi.fn()
  open(adapter({ launchUserStrategyExperiment: launch }), '/data?research=real-idea&revision=1&return=%2Fresearch%2Freal-idea%2Fdraft#data-upload')
  await screen.findByRole('heading', { name: '数据资源' })
  await userEvent.click(await screen.findByRole('link', { name: '返回当前研究（保留导入草稿）' }))
  await screen.findByRole('heading', { name: '策略草稿 · r1' })
  expect(launch).not.toHaveBeenCalled()
})

it('reads core-owned reports from persisted research references after reopen even when the legacy job list is empty', async () => {
  const launch = vi.fn(), legacy = vi.fn(async () => [])
  open(adapter({ listBacktests: legacy, launchUserStrategyExperiment: launch,
    researchReferences: async () => [{ user_strategy_id: 'real-idea', name: '已有研究', draft: { revision: 4 }, experiments: [{ job_id: 'persisted-core-job', launch_intent_id: 'original' }] }],
    getBacktest: async () => ({ job_id: 'persisted-core-job', status: 'succeeded', report_ready: true } as BacktestJob),
  }), '/records')
  await screen.findByRole('link', { name: /persisted-core-job.*报告可读/ })
  expect(screen.queryByText('暂无实验记录。')).toBeNull()
  expect(legacy).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

const modelProfile = (id: string, name: string) => ({
  schema: 'radian.settings-status.v1' as const, settings_file: '/fixture/settings.json', api_key_env: null,
  id, name, revision: 1, route_version: id, provider: 'openai-chat-completions-compatible', model: `${id}-model`,
  base_url: 'http://127.0.0.1:9999/v1', configured: true, credential_available: true,
})

it('edits connections from v3, cancels without saving and retains ordinary inputs on save failure', async () => {
  const first = modelProfile('first', '本地连接')
  const status = { workspace: '/fixture/current', next_workspace: '/fixture/current', theme: 'system' as const, startup_error: null,
    model: { ...first, default_profile_id: 'first', profiles: [first] } }
  const save = vi.fn().mockRejectedValue(new Error('保存失败；原设置保留。')), send = vi.fn(), launch = vi.fn()
  open(adapter({ desktopSettings: async () => status, saveDesktopSettings: save, sendUserStrategyMessage: send, launchUserStrategyExperiment: launch }), '/')
  await userEvent.click(screen.getByRole('link', { name: '模型连接' }))
  const key = await screen.findByLabelText('新 API Key') as HTMLInputElement
  expect(key.value).toBe('')
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'cancelled-model' } })
  fireEvent.change(key, { target: { value: 'synthetic-test-only' } })
  await userEvent.click(screen.getByRole('button', { name: '取消未保存的修改' }))
  expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('first-model')
  expect(key.value).toBe('')
  expect(save).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '添加连接' }))
  fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '新连接' } })
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'new-model' } })
  fireEvent.change(key, { target: { value: 'synthetic-test-only' } })
  expect(JSON.stringify(sessionStorage)).not.toContain('synthetic-test-only')
  expect(JSON.stringify(localStorage)).not.toContain('synthetic-test-only')
  await userEvent.click(screen.getByRole('button', { name: '保存模型设置' }))
  await screen.findByText('保存失败；原设置保留。')
  expect((screen.getByLabelText('连接名称') as HTMLInputElement).value).toBe('新连接')
  expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('new-model')
  expect(key.value).toBe('')
  expect(save).toHaveBeenCalledTimes(1)
  expect(save.mock.calls[0][0].model_profile).toMatchObject({ name: '新连接', model: 'new-model', api_key: 'synthetic-test-only' })
  expect(send).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('configures a fresh workspace through v3 and explicitly selects its connection before one send', async () => {
  const first = modelProfile('first', '本地连接')
  let model = { ...first, configured: false, default_profile_id: 'legacy', profiles: [] as ReturnType<typeof modelProfile>[] }
  const desktop = () => ({ workspace: '/fixture/fresh', next_workspace: '/fixture/fresh', theme: 'system' as const, startup_error: null, model })
  let doc = { ...strategy, model_profile_id: 'legacy' }
  const save = vi.fn(async (body: { model_profile?: { id: string; name: string }; default_model_profile?: string }) => {
    if (body.model_profile) model = { ...model, profiles: [{ ...first, ...body.model_profile }] }
    if (body.default_model_profile) model = { ...model, configured: true, default_profile_id: body.default_model_profile }
    return desktop()
  })
  const select = vi.fn(async (_id: string, profileId: string | null) => { doc = { ...doc, model_profile_id: profileId ?? '' }; return doc })
  const send = vi.fn(async (_id: string, body: { call_id: string }) => ({ ...doc, calls: [{ call_id: body.call_id, status: 'observed', base_revision: 1, route: {}, usage_available: false } as UserStrategy['calls'][number]] }))
  const api = adapter({ getUserStrategy: async () => doc, desktopSettings: async () => desktop(), saveDesktopSettings: save,
    settingsStatus: async () => model, selectUserStrategyModelProfile: select, sendUserStrategyMessage: send })
  open(api, '/research/real-idea/overview')
  fireEvent.change(await screen.findByLabelText('消息'), { target: { value: '检查草稿' } })
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.click(screen.getByRole('link', { name: '管理模型连接' }))
  await userEvent.click(await screen.findByRole('button', { name: '添加连接' }))
  fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '本地连接' } })
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'first-model' } })
  await userEvent.click(screen.getByRole('button', { name: '保存模型设置' }))
  await userEvent.click(await screen.findByRole('button', { name: '设为默认 本地连接' }))
  await screen.findByText('工作区默认已更改；历史调用和已批准轮次保持原配置。')
  expect(send).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('link', { name: '新建研究' }))
  // Re-open the persisted research with the existing direct route contract.
  cleanup()
  open(api, '/research/real-idea/overview')
  const picker = await screen.findByLabelText('本研究模型连接')
  expect((screen.getByRole('button', { name: '发送一次模型请求' }) as HTMLButtonElement).disabled).toBe(true)
  await userEvent.selectOptions(picker, model.profiles[0].id)
  expect(select).toHaveBeenCalledExactlyOnceWith('real-idea', model.profiles[0].id)
  const button = screen.getByRole('button', { name: '发送一次模型请求' })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(button); fireEvent.click(button)
  await screen.findByRole('heading', { name: '调用记录' })
  expect(send).toHaveBeenCalledTimes(1)
  expect(send.mock.calls[0][1]).toMatchObject({ approved_budget: { max_model_calls: 1, max_backtests: 0 } })
})

it('keeps the research selection and historical call identity when the workspace default changes', async () => {
  const first = modelProfile('first', '原连接'), second = modelProfile('second', '新默认')
  const historical = { call_id: 'old', status: 'observed', base_revision: 1, route: {}, usage_available: false,
    model_profile: { id: 'first', name: '历史连接名称', revision: 1, route_version: 'old' } } as UserStrategy['calls'][number]
  let doc = { ...strategy, model_profile_id: 'first' as string | null, calls: [historical] }
  const select = vi.fn(async (_id: string, profileId: string | null) => { doc = { ...doc, model_profile_id: profileId }; return doc })
  open(adapter({ getUserStrategy: async () => doc, settingsStatus: async () => ({ ...second, default_profile_id: 'second', profiles: [first, second] }), selectUserStrategyModelProfile: select }), '/research/real-idea/overview')
  const picker = await screen.findByLabelText('本研究模型连接') as HTMLSelectElement
  expect(picker.value).toBe('first')
  expect(screen.getByText('当次连接：历史连接名称 · v1')).toBeTruthy()
  await userEvent.selectOptions(picker, '')
  expect(select).toHaveBeenCalledExactlyOnceWith('real-idea', null)
  expect(picker.value).toBe('')
  expect(screen.getByText('当次连接：历史连接名称 · v1')).toBeTruthy()
})

// These are owner projections, not simulated engine or OS-stop evidence.
function controlRecord(overrides: Partial<UserStrategy['experiments'][number]> = {}): UserStrategy['experiments'][number] {
  return {
    launch_intent_id: 'original-control', request_id: 'request-control', payload_sha256: 'frozen',
    payload: { draft_revision: 1, scenario_id: 'sample.yaml', parameters: { target_quantity: 1 },
      preview_identity: 'original-preview', approved_budget: { max_model_calls: 0, max_backtests: 1 } },
    task_id: null, job_id: null, status: 'running', created_at: '2026-10-29', owner: 'quine',
    attempt_id: 'attempt-original', core_revision: 4, slot_held: true, ...overrides,
  }
}

it('requests stop once and renders stopping until an owner terminal receipt is read', async () => {
  let current = { ...strategy, experiments: [controlRecord()] }
  let finish!: (value: UserStrategy) => void
  const cancel = vi.fn(() => new Promise<UserStrategy>(resolve => { finish = resolve })), launch = vi.fn()
  open(adapter({ getUserStrategy: async () => current, cancelUserStrategyExperiment: cancel, launchUserStrategyExperiment: launch }), '/research/real-idea/experiments')
  const stop = await screen.findByRole('button', { name: '停止这次运行' })
  fireEvent.click(stop); fireEvent.click(stop)
  expect(cancel).toHaveBeenCalledExactlyOnceWith('real-idea', 'original-control')
  current = { ...strategy, experiments: [controlRecord({ status: 'stopping' })] }
  finish(current)
  await screen.findByText(/停止请求已受理.*尚未证实停止/)
  expect((screen.getByRole('button', { name: '停止这次运行' }) as HTMLButtonElement).disabled).toBe(true)
  current = { ...strategy, experiments: [controlRecord({ status: 'cancelled', slot_held: false })] }
  await userEvent.click(screen.getByRole('button', { name: '重新读取研究' }))
  await screen.findByText(/冻结 r1 · cancelled/)
  expect(screen.queryByRole('button', { name: '停止这次运行' })).toBeNull()
  expect(current.experiments[0].payload).toEqual(controlRecord().payload)
  expect(launch).not.toHaveBeenCalled()
})

it('keeps the last owner state and exposes a refused stop without retry or redispatch', async () => {
  const cancel = vi.fn().mockRejectedValue(new UserStrategyRequestError('late_cancel_refused', 'owner already committed')), launch = vi.fn()
  open(adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [controlRecord()] }), cancelUserStrategyExperiment: cancel, launchUserStrategyExperiment: launch }), '/research/real-idea/experiments')
  await userEvent.click(await screen.findByRole('button', { name: '停止这次运行' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('owner already committed')
  expect(screen.getByText(/冻结 r1 · running/)).toBeTruthy()
  expect(cancel).toHaveBeenCalledTimes(1); expect(launch).not.toHaveBeenCalled()
})

it('prepares recovery explicitly, queries across reopen, then releases only a ready owner receipt', async () => {
  const recovery = { status: 'prepare_required' as const, preparation_id: null, prepared_at: null, released_at: null }
  const original = controlRecord({ status: 'unknown', recovery })
  let current: UserStrategy = { ...strategy, experiments: [original] }
  const recover = vi.fn(async (_id: string, _intent: string, input: { action: string; preparation_id: string }) => {
    current = { ...current, experiments: [{ ...current.experiments[0], core_revision: input.action === 'prepare' ? 5 : 7,
      slot_held: input.action !== 'release', recovery: { ...recovery, status: input.action === 'prepare' ? 'restart_required' : 'released', preparation_id: input.preparation_id } }] }
    return current
  })
  const launch = vi.fn(), readPreview = vi.fn(async () => preview)
  const api = adapter({ getUserStrategy: async () => current, recoverUserStrategyExperiment: recover,
    launchUserStrategyExperiment: launch, previewUserStrategyExperiment: readPreview })
  const view = open(api, '/research/real-idea/experiments')
  const prepare = await screen.findByRole('button', { name: '记录恢复准备（需重启 Mac）' })
  expect(recover).not.toHaveBeenCalled()
  fireEvent.click(prepare); fireEvent.click(prepare)
  await screen.findByText(/恢复准备已保存/)
  expect(recover).toHaveBeenCalledExactlyOnceWith('real-idea', 'original-control', {
    action: 'prepare', attempt_id: 'attempt-original', expected_revision: 4, preparation_id: expect.any(String),
  })
  expect(screen.queryByRole('button', { name: '保留未知结果并释放运行名额' })).toBeNull()
  view.unmount()
  open(api, '/research/real-idea/experiments')
  await screen.findByText(/恢复准备已保存/)
  expect(recover).toHaveBeenCalledTimes(1)
  current = { ...current, experiments: [{ ...current.experiments[0], core_revision: 6, recovery: { ...current.experiments[0].recovery!, status: 'ready' } }] }
  await userEvent.click(screen.getByRole('button', { name: '重新读取研究' }))
  await userEvent.click(await screen.findByRole('button', { name: '保留未知结果并释放运行名额' }))
  await screen.findByText(/结果仍未知；核心已验证旧计算停止并释放运行名额/)
  expect(recover).toHaveBeenLastCalledWith('real-idea', 'original-control', {
    action: 'release', attempt_id: 'attempt-original', expected_revision: 6, preparation_id: recover.mock.calls[0][2].preparation_id,
  })
  expect(current.experiments[0].status).toBe('unknown')
  expect(current.experiments[0].payload).toEqual(original.payload)
  expect(launch).not.toHaveBeenCalled(); expect(readPreview).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('link', { name: '准备一次实验' }))
  await screen.findByRole('button', { name: '确认并运行一次' })
  expect(readPreview).toHaveBeenCalledExactlyOnceWith('real-idea'); expect(launch).not.toHaveBeenCalled()
})

it.each([
  { recovery: null },
  { recovery: { status: 'unavailable' as const, preparation_id: null, prepared_at: null, released_at: null } },
  { slot_held: null }, { attempt_id: null }, { core_revision: null },
])('does not invent recovery authority from an unknown record: %j', async missing => {
  const recover = vi.fn(), launch = vi.fn()
  const item = controlRecord({ status: 'unknown', recovery: { status: 'ready', preparation_id: 'prep', prepared_at: 1, released_at: null }, ...missing })
  open(adapter({ getUserStrategy: async () => ({ ...strategy, experiments: [item] }), recoverUserStrategyExperiment: recover, launchUserStrategyExperiment: launch }), '/research/real-idea/experiments')
  await screen.findByLabelText('未知运行恢复')
  expect(screen.queryByRole('button', { name: '保留未知结果并释放运行名额' })).toBeNull()
  expect(screen.queryByRole('button', { name: '记录恢复准备（需重启 Mac）' })).toBeNull()
  expect(recover).not.toHaveBeenCalled(); expect(launch).not.toHaveBeenCalled()
})

it('retains unknown after a failed recovery and rereads the same request on reopen', async () => {
  const recover = vi.fn().mockRejectedValue(new Error('receipt lost')), launch = vi.fn()
  const current = { ...strategy, experiments: [controlRecord({ status: 'unknown', recovery: { status: 'prepare_required' as const, preparation_id: null, prepared_at: null, released_at: null } })] }
  const api = adapter({ getUserStrategy: async () => current, recoverUserStrategyExperiment: recover, launchUserStrategyExperiment: launch })
  const view = open(api, '/research/real-idea/experiments')
  await userEvent.click(await screen.findByRole('button', { name: '记录恢复准备（需重启 Mac）' }))
  expect((await screen.findByRole('alert')).textContent).toContain('不会自动重试或重跑')
  view.unmount(); open(api, '/research/real-idea/experiments')
  await userEvent.click(await screen.findByRole('button', { name: '重新读取研究' }))
  await screen.findByText('original-control')
  expect(recover).toHaveBeenCalledTimes(1); expect(launch).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('link', { name: '查看脱敏诊断与备份说明' }))
  await screen.findByRole('heading', { name: '关于 QUINE' })
  expect(screen.getByText('手动备份、恢复与升级')).toBeTruthy()
})
