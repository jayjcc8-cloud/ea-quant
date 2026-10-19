import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { DataWorkspace, ResearchSettings } from './research'
import { FrozenReportFacts, FrozenReportDetails } from './frozen-report'
import type { ApiAdapter, BacktestJob, BacktestReport } from './app'

const job = { scenario_id: 'synthetic-name-is-not-evidence', input_identity: { record_count: 12 }, input_snapshot: { scenario: { instrument: { venue: 'XNAS', symbol: 'AAPL' }, strategy: { parameters: {} } } } } as BacktestJob
const report = { economics: { counts: { orders: 0, fills: 0 }, total_return: { value: '0' } } } as BacktestReport
const show = (value: BacktestJob) => render(<><FrozenReportFacts job={value} report={report} /><FrozenReportDetails job={value} report={report} path={null} /></>)
afterEach(() => { cleanup(); sessionStorage.clear(); window.history.replaceState({}, '', '/') })
it('uses only each job frozen source and never infers sample nature from a name or availability', () => {
  const frozen = { ...job, dataset_version: { dataset_id: 'old-v1', version: 1, source: { reference: 'Provider capture 2026', asserted: true, declared_by: 'user', license_reference: 'Internal research permission' }, availability_policy: { id: 'simulated', pit_verified: false } } }
  const original = JSON.stringify(frozen)
  show(frozen)
  expect(screen.getByText('Provider capture 2026 · 用户声明')).toBeTruthy()
  expect(screen.getByText('Internal research permission · 用户声明')).toBeTruthy()
  expect(screen.getByText(/12 条 · 性质未声明/)).toBeTruthy()
  expect(screen.getByText(/PIT 未验证/)).toBeTruthy()
  expect(JSON.stringify(frozen)).toBe(original)
})
it('prominently identifies frozen host synthetic evidence while legacy stays unknown', () => {
  const view = show({ ...job, dataset_version: { dataset_id: 'exact-v2', version: 2, source: { provenance: 'builtin-synthetic-example', declared_by: 'host-example', asserted: false } } })
  expect(screen.getByText(/12 条 · 合成示例/)).toBeTruthy()
  view.unmount(); show(job)
  expect(screen.getByText(/12 条 · 性质未声明/)).toBeTruthy()
  expect(screen.queryByText(/12 条 · 合成示例/)).toBeNull()
  expect(screen.getByText('XNAS / 市场类型未记录')).toBeTruthy()
})
it('offers an explicit synthetic CSV example without publishing or claiming PIT', async () => {
  const publish = vi.fn()
  render(<BrowserRouter><DataWorkspace api={{ previewUpload: vi.fn(), publishUpload: publish, listDatasets: async () => [] } as unknown as ApiAdapter} /></BrowserRouter>)
  fireEvent.click(screen.getByRole('button', { name: '填入合成 CSV 示例' }))
  expect((screen.getByLabelText('CSV 内容') as HTMLTextAreaElement).value).toContain('XNAS,AAPL')
  expect((screen.getByLabelText('数据来源（用户声明）') as HTMLInputElement).value).toContain('合成')
  expect(screen.getByText(/仅本次会话暂存/)).toBeTruthy()
  expect(publish).not.toHaveBeenCalled()
  await screen.findByText('暂无独立数据版本。已有场景仍使用其注册数据。')
})
it('about diagnostics are an allowlist and omit paths, endpoints, names and keys', async () => {
  window.history.replaceState({}, '', '/settings?section=about')
  render(<BrowserRouter><ResearchSettings api={{ desktopSettings: async () => ({ workspace: '/private/alice/research', next_workspace: '/other', theme: 'system', startup_error: null, model: { api_key: 'secret', base_url: 'https://private.invalid', profiles: [{ name: 'private profile' }] }, application: { desktop_version: '0.1.0', engine_version: '0.2.0', build_id: 'candidate-test', source_revision: null, source_dirty: null } }) } as unknown as ApiAdapter} /></BrowserRouter>)
  const diagnostic = await screen.findByLabelText('可分享的脱敏诊断') as HTMLTextAreaElement
  expect(diagnostic.value).toContain('candidate-test')
  expect(diagnostic.value).not.toMatch(/alice|private|secret|other/)
  expect(screen.getByText('/private/alice/research')).toBeTruthy()
})

it('keeps sample and permission copy on-page without a download or overwriting a CSV draft', async () => {
  const api = { previewUpload: vi.fn(), publishUpload: vi.fn(), listDatasets: async () => [] } as unknown as ApiAdapter
  render(<BrowserRouter><DataWorkspace api={api} /></BrowserRouter>)
  await screen.findByText('暂无独立数据版本。已有场景仍使用其注册数据。')
  fireEvent.change(screen.getByLabelText('CSV 内容'), { target: { value: 'my existing CSV draft' } })
  fireEvent.click(screen.getByText('查看并复制合成示例与许可'))
  const example = screen.getByLabelText('合成示例 CSV（只读）') as HTMLTextAreaElement
  expect(example.readOnly).toBe(true)
  expect(example.value).toContain('XNAS,AAPL')
  fireEvent.focus(example)
  expect(example.selectionStart).toBe(0)
  expect(example.selectionEnd).toBe(example.value.length)
  const notice = screen.getByLabelText('示例来源与完整许可（只读）') as HTMLTextAreaElement
  expect(notice.value).toContain('无条件复制、修改和使用')
  for (const field of [example, notice]) {
    // WKWebView's mouse default action collapses the focus-time selection.
    // A click must select again, including when the field already has focus.
    field.setSelectionRange(10, 10)
    fireEvent.click(field)
    expect(field.selectionStart).toBe(0)
    expect(field.selectionEnd).toBe(field.value.length)
  }
  expect((screen.getByLabelText('CSV 内容') as HTMLTextAreaElement).value).toBe('my existing CSV draft')
  expect(document.querySelector('a[download]')).toBeNull()
  expect(api.publishUpload).not.toHaveBeenCalled()
})
