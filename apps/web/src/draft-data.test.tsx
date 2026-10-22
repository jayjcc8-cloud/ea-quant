/** Deterministic UI fixtures only; no product model or financial result evidence. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { DraftEditor } from './draft-editor'
import { DataWorkspace } from './research'
import { ResearchUploadBinding, importTarget } from './research-upload-binding'
import { SourceLink } from './workspace-navigation'
import type { ApiAdapter } from './app'
import { UserStrategyRequestError, type DraftContract, type SaveDraftRequest, type UserStrategy } from './user-strategies-api'
import type { UploadVersion } from './research-api'
const draft = { revision: 1, strategy_id: 'bounded-long-v1', venue: 'XNAS', symbol: 'AAPL', scenario_id: 'sample.yaml', dataset_id: null, source_sha256: null, initial_cash: '10000', parameters: { entry_delay_bars: 0 }, criterion: null }
const strategy = { schema: 'ea.user-strategy.v1', user_strategy_id: 'a', name: 'Research A', draft, changes: [], calls: [], messages: [], proposals: [], experiments: [], history: [] } as unknown as UserStrategy
const contract: DraftContract = { draft_revision: 1, scenario_id: 'sample.yaml', source: 'registered', fields: [
  { path: 'initial_cash', kind: 'positive decimal', json_type: 'string', current: '10000', minimum: null, maximum: null, editable: true },
  { path: 'parameters.entry_delay_bars', kind: 'integer', json_type: 'integer', current: 0, minimum: 0, maximum: 5, editable: true },
] }
const target = { id: 'a', revision: 1, returnTo: '/strategies/a?tab=strategy' }
const makeApi = (extra: Partial<ApiAdapter> = {}) => ({ userStrategyDraftContract: async () => contract, getUserStrategy: async () => strategy, ...extra }) as ApiAdapter
const saved = (request: SaveDraftRequest): UserStrategy => ({ ...strategy, draft: { ...draft, revision: 2 }, changes: [{ kind: 'manual', operation_id: request.operation_id }] as UserStrategy['changes'] })
function LocationProbe() {
  const location = useLocation()
  const state = location.state as { mark?: string } | null
  return <output aria-label="location">{location.pathname}{location.search}·{state?.mark ?? ''}</output>
}
afterEach(() => { cleanup(); sessionStorage.clear(); window.history.replaceState({}, '', '/') })

it('manual cancel writes nothing, save carries typed values and exact revision only once', async () => {
  let resolve!: (value: UserStrategy) => void
  let request!: SaveDraftRequest
  const save = vi.fn((_id: string, input: SaveDraftRequest) => { request = input; return new Promise<UserStrategy>(done => { resolve = done }) })
  const changed = vi.fn()
  render(<DraftEditor api={makeApi({ saveUserStrategyDraft: save })} strategy={strategy} changed={changed} />)
  const cash = await screen.findByLabelText(/初始资金/)
  fireEvent.change(cash, { target: { value: '12000' } })
  fireEvent.click(screen.getByText('取消编辑'))
  expect(save).not.toHaveBeenCalled(); expect((cash as HTMLInputElement).value).toBe('10000')
  fireEvent.change(await screen.findByLabelText(/parameters.entry_delay_bars/), { target: { value: '1' } })
  fireEvent.click(screen.getByText('保存草稿')); fireEvent.click(screen.getByText('保存草稿'))
  expect(save).toHaveBeenCalledTimes(1)
  expect(request.expected_revision).toBe(1); expect(request.values).toEqual({ 'parameters.entry_delay_bars': 1 })
  resolve(saved(request)); await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
})

it('manual buffer survives remount and refuses to overwrite a newer revision', async () => {
  const save = vi.fn()
  const api = makeApi({ saveUserStrategyDraft: save })
  const view = render(<DraftEditor api={api} strategy={strategy} changed={() => {}} />)
  fireEvent.change(await screen.findByLabelText(/初始资金/), { target: { value: '13500' } })
  view.unmount()
  const again = render(<DraftEditor api={api} strategy={strategy} changed={() => {}} />)
  expect((await screen.findByLabelText(/初始资金/) as HTMLInputElement).value).toBe('13500')
  again.rerender(<DraftEditor api={api} strategy={{ ...strategy, draft: { ...draft, revision: 2 } }} changed={() => {}} />)
  expect(screen.getByRole('alert').textContent).toContain('草稿已有新修订')
  fireEvent.click(screen.getByText('保存草稿')); expect(save).not.toHaveBeenCalled()
})

it('unknown manual submission stays blocked across remount and is resolved by read only', async () => {
  let request!: SaveDraftRequest
  const save = vi.fn(async (_id: string, input: SaveDraftRequest) => { request = input; throw new Error('connection lost') })
  const api = makeApi({ saveUserStrategyDraft: save, getUserStrategy: async () => saved(request) })
  const view = render(<DraftEditor api={api} strategy={strategy} changed={() => {}} />)
  fireEvent.change(await screen.findByLabelText(/初始资金/), { target: { value: '12000' } })
  fireEvent.click(screen.getByText('保存草稿')); await screen.findByText(/保存结果未知/)
  view.unmount()
  render(<DraftEditor api={api} strategy={strategy} changed={() => {}} />)
  await screen.findByLabelText(/初始资金/)
  expect((screen.getByText('保存草稿') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByText('核对保存结果')); await screen.findByText(/已保存为修订 2/)
  expect(save).toHaveBeenCalledTimes(1)
})

it('storage failure before sending keeps values editable and cancel available without an API call', async () => {
  const save = vi.fn()
  render(<DraftEditor api={makeApi({ saveUserStrategyDraft: save })} strategy={strategy} changed={() => {}} />)
  const cash = await screen.findByLabelText(/初始资金/) as HTMLInputElement
  fireEvent.change(cash, { target: { value: '12000' } })
  const write = vi.spyOn(Object.getPrototypeOf(sessionStorage) as Storage, 'setItem')
    .mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
  try {
    fireEvent.click(screen.getByText('保存草稿'))
    await screen.findByText(/本次未发送/)
    expect(write).toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
    expect(cash.value).toBe('12000')
    expect((screen.getByText('取消编辑') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.change(cash, { target: { value: '12100' } })
    expect(cash.value).toBe('12100')
    fireEvent.click(screen.getByText('取消编辑'))
    expect(cash.value).toBe('10000')
    expect(save).not.toHaveBeenCalled()
  } finally { write.mockRestore() }
})

it('draft without a retained buffer guards same-window links in-app and leaves only once', async () => {
  window.history.replaceState({}, '', '/strategies/a?tab=strategy')
  const save = vi.fn()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const push = vi.spyOn(window.history, 'pushState')
  const replace = vi.spyOn(window.history, 'replaceState')
  try {
    render(<BrowserRouter>
      <SourceLink to="/research?tab=data" state={{ mark: 'kept' }} replace>离开草稿</SourceLink>
      <DraftEditor api={makeApi({ saveUserStrategyDraft: save })} strategy={strategy} changed={() => {}} />
      <LocationProbe />
    </BrowserRouter>)
    const cash = await screen.findByLabelText(/初始资金/) as HTMLInputElement
    fireEvent.change(cash, { target: { value: '12000' } })
    const write = vi.spyOn(Object.getPrototypeOf(sessionStorage) as Storage, 'setItem')
      .mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    try {
      fireEvent.click(screen.getByText('保存草稿'))
      await screen.findByText(/本次未发送/)
      expect(write).toHaveBeenCalled()
    } finally { write.mockRestore() }
    expect(save).not.toHaveBeenCalled()

    const link = screen.getByText('离开草稿')
    fireEvent.click(link)
    expect(confirm).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: '离开此页？' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.textContent).toContain('本地缓冲不可用')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '继续编辑' }))
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '放弃并离开' }))
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '继续编辑' }))

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(cash.value).toBe('12000')
    expect(document.activeElement).toBe(link)
    expect(screen.getByLabelText('location').textContent).toBe('/strategies/a?tab=strategy·')

    fireEvent.click(link)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(cash.value).toBe('12000')
    expect(screen.getByLabelText('location').textContent).toBe('/strategies/a?tab=strategy·')

    fireEvent.click(link)
    const pushesBefore = push.mock.calls.length
    const replacesBefore = replace.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    await waitFor(() => expect(screen.getByLabelText('location').textContent).toBe('/research?tab=data·kept'))
    expect(confirm).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
    expect(replace.mock.calls.length).toBe(replacesBefore + 1)
    expect(push.mock.calls.length).toBe(pushesBefore)
  } finally { confirm.mockRestore(); push.mockRestore(); replace.mockRestore() }
})

it('CSV without a retained buffer uses the same in-app guard and publishes nothing', async () => {
  window.history.replaceState({}, '', '/data?research=a&revision=1')
  const publish = vi.fn()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const write = vi.spyOn(Object.getPrototypeOf(sessionStorage) as Storage, 'setItem')
    .mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
  try {
    render(<BrowserRouter>
      <DataWorkspace api={makeApi({ previewUpload: vi.fn(), publishUpload: publish })} />
      <LocationProbe />
    </BrowserRouter>)
    fireEvent.change(screen.getByLabelText('CSV 内容'), { target: { value: 'retained csv bytes' } })
    await screen.findByText(/无法完整缓冲/)
    expect(write).toHaveBeenCalled()

    fireEvent.click(screen.getByText('返回当前研究（保留导入草稿）'))
    expect(confirm).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: '离开此页？' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.textContent).toContain('CSV 输入')
    fireEvent.click(screen.getByRole('button', { name: '放弃并离开' }))
    await waitFor(() => expect(screen.getByLabelText('location').textContent).toContain('/strategies/a?tab=strategy'))
    expect(confirm).not.toHaveBeenCalled()
    expect(publish).not.toHaveBeenCalled()
  } finally { write.mockRestore(); confirm.mockRestore() }
})

it('restored storage lets the user explicitly save the preserved draft exactly once', async () => {
  let request!: SaveDraftRequest
  const save = vi.fn(async (_id: string, input: SaveDraftRequest) => { request = input; return saved(input) })
  const changed = vi.fn()
  render(<DraftEditor api={makeApi({ saveUserStrategyDraft: save })} strategy={strategy} changed={changed} />)
  fireEvent.change(await screen.findByLabelText(/初始资金/), { target: { value: '12000' } })
  const write = vi.spyOn(Object.getPrototypeOf(sessionStorage) as Storage, 'setItem')
    .mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
  try {
    fireEvent.click(screen.getByText('保存草稿'))
    await screen.findByText(/本次未发送/)
    expect(write).toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  } finally { write.mockRestore() }
  fireEvent.click(screen.getByText('保存草稿'))
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
  expect(save).toHaveBeenCalledTimes(1)
  expect(request.values).toEqual({ initial_cash: '12000' })
  expect(screen.getByText(/已保存为修订 2/)).toBeTruthy()
  expect(sessionStorage.getItem('quine.manual-draft.a')).toBeNull()
})

it('manual late response does not call the previous page owner', async () => {
  let resolve!: (value: UserStrategy) => void; let request!: SaveDraftRequest
  const changed = vi.fn()
  const view = render(<DraftEditor api={makeApi({ saveUserStrategyDraft: (_id, input) => { request = input; return new Promise(done => { resolve = done }) } })} strategy={strategy} changed={changed} />)
  fireEvent.change(await screen.findByLabelText(/初始资金/), { target: { value: '12000' } })
  fireEvent.click(screen.getByText('保存草稿')); view.unmount(); resolve(saved(request))
  await Promise.resolve(); expect(changed).not.toHaveBeenCalled()
})

it('CSV binding uses exact target revision/version, deduplicates clicks, and never guesses context', async () => {
  let resolve!: (value: UserStrategy) => void; let operation = ''
  const select = vi.fn((_id: string, input: { operation_id: string; base_revision: number; dataset_id: string }) => { operation = input.operation_id; return new Promise<UserStrategy>(done => { resolve = done }) })
  render(<BrowserRouter><ResearchUploadBinding api={makeApi({ selectUserStrategyDataset: select })} target={target} datasetId="upload-exact-v2" /></BrowserRouter>)
  await screen.findByText(/目标研究：Research A/)
  fireEvent.click(screen.getByRole('button', { name: '用于当前研究' })); fireEvent.click(screen.getByRole('button', { name: '用于当前研究' }))
  expect(select).toHaveBeenCalledTimes(1); expect(select.mock.calls[0][0]).toBe('a')
  expect(select.mock.calls[0][1]).toEqual({ operation_id: operation, base_revision: 1, dataset_id: 'upload-exact-v2' })
  resolve({ ...strategy, draft: { ...draft, revision: 2, dataset_id: 'upload-exact-v2' }, dataset: { operation_id: operation, dataset_id: 'upload-exact-v2' } as UserStrategy['dataset'] })
  await screen.findByText(/已用于当前研究/)
  cleanup()
  render(<BrowserRouter><ResearchUploadBinding api={makeApi({ selectUserStrategyDataset: select })} target={null} datasetId="upload-exact-v2" /></BrowserRouter>)
  expect(screen.queryByRole('button', { name: '用于当前研究' })).toBeNull()
  expect(importTarget('?return=https://bad.example')).toBeNull()
})

it('CSV compatibility refusal retains published version and never calls another research', async () => {
  const select = vi.fn(async () => { throw new UserStrategyRequestError('user_strategy_invalid', 'instrument mismatch') })
  render(<BrowserRouter><ResearchUploadBinding api={makeApi({ selectUserStrategyDataset: select })} target={target} datasetId="version-a" /></BrowserRouter>)
  await screen.findByText(/目标研究：Research A/)
  fireEvent.click(screen.getByRole('button', { name: '用于当前研究' }))
  await screen.findByText(/已发布版本与导入输入已保留/)
  expect(select).toHaveBeenCalledTimes(1)
})

it('CSV inputs survive settings/unmount and publication alone performs no binding', async () => {
  window.history.replaceState({}, '', '/data?research=a&revision=1')
  const select = vi.fn()
  const version = { dataset_id: 'version-exact', version: 1, source: {}, availability_policy: {}, instrument: {}, quality: {}, record_count: 8, limitations: [] } as unknown as UploadVersion
  const publish = vi.fn(async () => ({ ...version, datasets: [] }))
  const api = makeApi({ previewUpload: vi.fn(), publishUpload: publish, selectUserStrategyDataset: select })
  const view = render(<BrowserRouter><DataWorkspace api={api} /></BrowserRouter>)
  fireEvent.change(screen.getByLabelText('CSV 内容'), { target: { value: 'fixture bytes' } })
  fireEvent.change(screen.getByLabelText('可用时间策略'), { target: { value: 'ea.availability.provided-in-capture.v1' } })
  view.unmount()
  render(<BrowserRouter><DataWorkspace api={api} /></BrowserRouter>)
  expect((screen.getByLabelText('CSV 内容') as HTMLTextAreaElement).value).toBe('fixture bytes')
  fireEvent.click(screen.getByText('发布数据版本')); await screen.findByText('已发布 · 8 条记录')
  expect(publish).toHaveBeenCalledTimes(1); expect(select).not.toHaveBeenCalled()
  expect(screen.getByText('版本身份、文件哈希与声明')).toBeTruthy()
})

it('manual conflict preserves input without retrying', async () => {
  const save = vi.fn(async () => { throw new UserStrategyRequestError('stale_base_revision', 'revision moved') })
  render(<DraftEditor api={makeApi({ saveUserStrategyDraft: save })} strategy={strategy} changed={() => {}} />)
  const cash = await screen.findByLabelText(/初始资金/)
  fireEvent.change(cash, { target: { value: '12500' } }); fireEvent.click(screen.getByText('保存草稿'))
  await screen.findByText(/revision moved/)
  expect((cash as HTMLInputElement).value).toBe('12500'); expect(save).toHaveBeenCalledTimes(1)
})

it('late CSV binding result cannot appear in the next research', async () => {
  let resolve!: (value: UserStrategy) => void; let operation = ''
  const api = makeApi({ selectUserStrategyDataset: (_id, request) => { operation = request.operation_id; return new Promise(done => { resolve = done }) } })
  const view = render(<BrowserRouter><ResearchUploadBinding key="a" api={api} target={target} datasetId="csv-a" /></BrowserRouter>)
  await screen.findByText(/目标研究：Research A/); fireEvent.click(screen.getByRole('button', { name: '用于当前研究' }))
  view.rerender(<BrowserRouter><ResearchUploadBinding key="none" api={api} target={null} datasetId="csv-b" /></BrowserRouter>)
  resolve({ ...strategy, draft: { ...draft, revision: 2, dataset_id: 'csv-a' }, dataset: { operation_id: operation, dataset_id: 'csv-a' } as UserStrategy['dataset'] })
  await Promise.resolve()
  expect(screen.queryByText(/已用于当前研究/)).toBeNull(); expect((screen.getByLabelText('目标研究') as HTMLSelectElement).value).toBe(''); expect(screen.queryByRole('button', { name: '用于当前研究' })).toBeNull()
})

it('pending or failed file read cannot publish previous bytes under a new filename', async () => {
  let reject!: (reason: Error) => void
  const publish = vi.fn()
  render(<BrowserRouter><DataWorkspace api={makeApi({ previewUpload: vi.fn(), publishUpload: publish })} /></BrowserRouter>)
  fireEvent.change(screen.getByLabelText('CSV 内容'), { target: { value: 'retained input' } })
  fireEvent.change(screen.getByLabelText('可用时间策略'), { target: { value: 'ea.availability.provided-in-capture.v1' } })
  fireEvent.change(screen.getByLabelText('选择 CSV 文件'), { target: { files: [{ name: 'unread.csv', arrayBuffer: () => new Promise((_resolve, fail) => { reject = fail }) }] } })
  expect((screen.getByText('发布数据版本') as HTMLButtonElement).disabled).toBe(true)
  reject(new Error('file inaccessible')); await screen.findByText(/Previous inputs retained/)
  expect((screen.getByLabelText('CSV 内容') as HTMLTextAreaElement).value).toBe('retained input')
  expect(publish).not.toHaveBeenCalled()
})

it('a CSV larger than the browser buffer can still publish with a small pending marker', async () => {
  const original = Storage.prototype.setItem
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (key.startsWith('quine.csv-input.') && value.length > 100) throw new DOMException('quota', 'QuotaExceededError')
    original.call(this, key, value)
  })
  try {
    const version = { dataset_id: 'large-version', version: 1, source: {}, availability_policy: {}, instrument: {}, quality: {}, record_count: 8, limitations: [] } as unknown as UploadVersion
    const publish = vi.fn(async () => ({ ...version, datasets: [] }))
    render(<BrowserRouter><DataWorkspace api={makeApi({ previewUpload: vi.fn(), publishUpload: publish })} /></BrowserRouter>)
    fireEvent.change(screen.getByLabelText('CSV 内容'), { target: { value: 'large fixture '.repeat(20) } })
    fireEvent.change(screen.getByLabelText('可用时间策略'), { target: { value: 'ea.availability.provided-in-capture.v1' } })
    fireEvent.click(screen.getByText('发布数据版本'))
    await screen.findByText('已发布 · 8 条记录')
    expect(publish).toHaveBeenCalledTimes(1)
  } finally { storage.mockRestore() }
})
