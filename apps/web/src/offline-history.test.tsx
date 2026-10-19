/**
 * The offline import seam: a pasted response, or the host's own labelled example,
 * is previewed and then published as exactly the identity that preview verified.
 *
 * Everything else about the upload surfaces is covered elsewhere and must keep
 * passing unchanged; this file only drives the offline panel.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter } from './app'
import {
  AVAILABILITY_SIMULATED,
  type OfflineHistoryDeclaration,
  type OfflineHistoryExample,
  type OfflineHistoryPreview,
  type OfflineHistoryVersion,
} from './research-api'

const EXAMPLE_BODY =
  '[["1704067200000","100.0","101.0","99.0","100.5","1.0","1704070799999","1.0",1,"1.0","1.0","0"]]'
const USER_BODY =
  '[["1704067200000","200.0","201.0","199.0","200.5","2.0","1704070799999","2.0",2,"2.0","2.0","0"]]'
const PREVIEW_DIGEST = 'sha256:' + 'e'.repeat(64)

const EXAMPLE: OfflineHistoryExample = {
  schema: 'ea.offline-history-example.v1',
  example_id: 'ea.offline-history.synthetic-klines-1h.v1',
  synthetic: true,
  label: 'Built-in synthetic example — not market data',
  note: 'A fixture this host ships.',
  adapter: { provider: {}, normalizer: {} },
  media_type: 'application/json',
  record_field_count: 12,
  documented_fields: ['open_time_ms'],
  venue: 'BINANCE',
  symbol: 'BTCUSDT',
  interval: '1h',
  start_ms: 1704067200000,
  end_ms: 1704153600000,
  latency_seconds: 0,
  record_count: 24,
  response_body: EXAMPLE_BODY,
  response_sha256: 'a'.repeat(64),
  limitations: ['Built-in synthetic example. These bytes are a fixture the host ships.'],
}

function previewDocument(overrides: Partial<OfflineHistoryPreview> = {}): OfflineHistoryPreview {
  return {
    schema: 'ea.offline-history-preview.v1',
    mode: 'offline-history',
    valid: true,
    first_error: null,
    synthetic: false,
    provenance: 'user-pasted-offline-response',
    byte_count: 96,
    response_sha256: 'b'.repeat(64),
    envelope_sha256: 'f'.repeat(64),
    capture: { sha256: 'c'.repeat(64), byte_count: 200, data_sha256: 'd'.repeat(64), record_count: 1 },
    instrument: { venue: 'BINANCE', symbol: 'BTCUSDT' },
    availability_policy: {
      id: AVAILABILITY_SIMULATED,
      version: 1,
      latency_seconds: 0,
      pit_verified: false,
      evidence: 'declared_by_user',
    },
    quality: {
      bar_count: 1,
      revision_count: 1,
      bar_durations_seconds: ['3600'],
      observed_gap_count: 0,
      largest_gap_seconds: null,
      bar_start_utc: '2024-01-01T00:00:00.000000Z',
      bar_end_utc: '2024-01-01T01:00:00.000000Z',
    },
    preview: {
      rows: [{ record_number: 2, close: '100.5' }],
      row_count_sampled: 1,
      truncated: false,
      valid: true,
    },
    declarations: { source: 'my export', rights: 'public data', intended_use: 'local research' },
    preview_sha256: PREVIEW_DIGEST,
    limitations: ['Offline import only. The response bytes were pasted by the user.'],
    ...overrides,
  }
}

const VERSION = {
  schema: 'ea.local-dataset-version.v4',
  dataset_id: 'offline-1.csv',
  kind: 'ohlcv',
  profile: 'ea-phase1-ohlcv-csv-v1',
  version: 1,
  published_at: '2024-01-03T00:00:00.000000Z',
  source: {
    declared_by: 'user',
    asserted: true,
    reference: 'my export',
    file_name: null,
    license_reference: 'public data',
    intended_use: 'local research',
    provenance: 'user-pasted-offline-response',
  },
  availability_policy: {
    id: AVAILABILITY_SIMULATED,
    version: 1,
    latency_seconds: 0,
    pit_verified: false,
    evidence: 'declared_by_user',
  },
  source_bytes_sha256: 'f'.repeat(64),
  source_byte_count: 300,
  raw_retained: true,
  capture_sha256: 'c'.repeat(64),
  capture_byte_count: 200,
  data_sha256: 'd'.repeat(64),
  record_count: 1,
  instrument: { venue: 'BINANCE', symbol: 'BTCUSDT' },
  quality: previewDocument().quality,
  limitations: ['Offline import only.'],
  manifest_sha256: '1'.repeat(64),
} as unknown as OfflineHistoryVersion

type Calls = {
  example: number
  previews: number
  publishes: number
  declaration?: OfflineHistoryDeclaration
  digest?: string
}

function adapter(calls: Calls, overrides: Record<string, unknown> = {}) {
  return {
    listResearchTasks: async () => [],
    offlineHistoryExample: async () => {
      calls.example += 1
      return EXAMPLE
    },
    previewOfflineHistory: async (_content: string, declaration: OfflineHistoryDeclaration) => {
      calls.previews += 1
      calls.declaration = declaration
      return previewDocument()
    },
    publishOfflineHistory: async (
      _content: string,
      declaration: OfflineHistoryDeclaration,
      digest: string,
    ) => {
      calls.publishes += 1
      calls.declaration = declaration
      calls.digest = digest
      return VERSION
    },
    ...overrides,
  } as unknown as ApiAdapter
}

function openPanel() {
  return screen.findByRole('button', { name: 'Open offline history import' })
}

function pasteOwnResponse() {
  fireEvent.change(screen.getByLabelText('Offline response (JSON, exactly as received)'), {
    target: { value: USER_BODY },
  })
  fireEvent.change(screen.getByLabelText('Offline venue'), { target: { value: 'BINANCE' } })
  fireEvent.change(screen.getByLabelText('Offline symbol'), { target: { value: 'BTCUSDT' } })
  fireEvent.change(screen.getByLabelText('Offline interval'), { target: { value: '1h' } })
  fireEvent.change(screen.getByLabelText('Offline query start (epoch ms)'), {
    target: { value: '1704067200000' },
  })
  fireEvent.change(screen.getByLabelText('Offline query end (epoch ms, exclusive)'), {
    target: { value: '1704070800000' },
  })
  fireEvent.change(screen.getByLabelText('Simulated availability latency (seconds)'), {
    target: { value: '0' },
  })
  fireEvent.change(screen.getByLabelText('Declared source (your assertion)'), {
    target: { value: 'my export' },
  })
  fireEvent.change(screen.getByLabelText('Declared rights/licence (your assertion)'), {
    target: { value: 'public data' },
  })
  fireEvent.change(screen.getByLabelText('Declared intended use (your assertion)'), {
    target: { value: 'local research' },
  })
}

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('offline history import', () => {
  it('loads the built-in example as synthetic and clears that mark when the response is edited', async () => {
    const user = userEvent.setup()
    const calls: Calls = { example: 0, previews: 0, publishes: 0 }
    window.history.pushState({}, '', '/research/new')
    render(<App api={adapter(calls)} />)

    await user.click(await openPanel())
    // The panel states plainly what this path is before anything is sent.
    expect(screen.getByText(/API NOT connected/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Load built-in synthetic example' }))
    await waitFor(() => expect(calls.example).toBe(1))

    expect(screen.getByText(/Synthetic: these are the host/)).toBeTruthy()
    expect((screen.getByLabelText('Offline response (JSON, exactly as received)') as HTMLTextAreaElement).value).toBe(
      EXAMPLE_BODY,
    )
    // The example is the host's fixture, so it takes no assertion of yours.
    expect((screen.getByLabelText('Declared source (your assertion)') as HTMLInputElement).disabled).toBe(true)

    // Editing the response makes it the user's own bytes, and the synthetic
    // designation never survives that edit.
    fireEvent.change(screen.getByLabelText('Offline response (JSON, exactly as received)'), {
      target: { value: USER_BODY },
    })
    expect(
      screen.getByText(/^Your own response\. It is never recorded as synthetic/),
    ).toBeTruthy()
    expect(screen.getByText(/cannot be requested for a paste/)).toBeTruthy()
    expect((screen.getByLabelText('Declared source (your assertion)') as HTMLInputElement).disabled).toBe(false)
  })

  it('publishes only the exact bytes and declarations a preview verified', async () => {
    const user = userEvent.setup()
    const calls: Calls = { example: 0, previews: 0, publishes: 0 }
    window.history.pushState({}, '', '/research/new')
    render(<App api={adapter(calls)} />)

    await user.click(await openPanel())
    pasteOwnResponse()

    const publish = () => screen.getByRole('button', { name: 'Publish offline version' }) as HTMLButtonElement
    expect(publish().disabled).toBe(true)
    expect(calls.publishes).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Preview offline import' }))
    await waitFor(() => expect(calls.previews).toBe(1))
    // The declaration reaches the host whole, with the user's own assertions.
    expect(calls.declaration).toMatchObject({
      venue: 'BINANCE',
      symbol: 'BTCUSDT',
      interval: '1h',
      start_ms: 1704067200000,
      end_ms: 1704070800000,
      latency_seconds: 0,
      synthetic: false,
      declared_source: 'my export',
      declared_rights: 'public data',
      intended_use: 'local research',
    })

    const preview = await screen.findByRole('region', { name: 'Offline history import preview' })
    expect(within(preview).getByText(/point-in-time verified: false/)).toBeTruthy()
    expect(within(preview).getAllByText(new RegExp(PREVIEW_DIGEST)).length).toBeGreaterThan(0)
    expect(within(preview).getByRole('cell', { name: '100.5' })).toBeTruthy()
    expect(publish().disabled).toBe(false)

    // Editing a declaration makes this a different import, so the preview stops
    // authorizing a publication.
    fireEvent.change(screen.getByLabelText('Declared intended use (your assertion)'), {
      target: { value: 'something else' },
    })
    expect(publish().disabled).toBe(true)
    fireEvent.click(publish())
    expect(calls.publishes).toBe(0)

    // Restoring the exact declaration and previewing again authorizes it, and the
    // publication names the digest the preview handed out.
    fireEvent.change(screen.getByLabelText('Declared intended use (your assertion)'), {
      target: { value: 'local research' },
    })
    await user.click(screen.getByRole('button', { name: 'Preview offline import' }))
    await waitFor(() => expect(calls.previews).toBe(2))

    // Two clicks in the same tick, before React can disable anything, are one
    // publication: the guard that refuses the second is synchronous.
    const button = publish()
    await act(async () => {
      button.click()
      button.click()
    })
    await waitFor(() => expect(calls.publishes).toBe(1))
    expect(calls.digest).toBe(PREVIEW_DIGEST)
    expect(await screen.findByText(/keeps your exact pasted bytes/)).toBeTruthy()
  })

  it('never publishes from a preview the host refused', async () => {
    const user = userEvent.setup()
    const calls: Calls = { example: 0, previews: 0, publishes: 0 }
    const refused = adapter(calls, {
      previewOfflineHistory: async () => {
        calls.previews += 1
        return previewDocument({
          valid: false,
          preview_sha256: null,
          first_error: {
            code: 'offline_response_invalid',
            message: 'the pasted response is not the documented record shape',
          },
        })
      },
    })
    window.history.pushState({}, '', '/research/new')
    render(<App api={refused} />)

    await user.click(await openPanel())
    pasteOwnResponse()
    await user.click(screen.getByRole('button', { name: 'Preview offline import' }))
    await waitFor(() => expect(calls.previews).toBe(1))

    const publish = screen.getByRole('button', { name: 'Publish offline version' }) as HTMLButtonElement
    expect(publish.disabled).toBe(true)
    fireEvent.click(publish)
    expect(calls.publishes).toBe(0)
    expect(screen.getByText(/a refused preview is never published/)).toBeTruthy()
  })

  it('drops a preview reply that arrives after the panel was closed', async () => {
    const user = userEvent.setup()
    const calls: Calls = { example: 0, previews: 0, publishes: 0 }
    let release: (value: OfflineHistoryPreview) => void = () => {}
    const delayed = adapter(calls, {
      previewOfflineHistory: async () => {
        calls.previews += 1
        return await new Promise<OfflineHistoryPreview>(resolve => {
          release = resolve
        })
      },
    })
    window.history.pushState({}, '', '/research/new')
    render(<App api={delayed} />)

    await user.click(await openPanel())
    pasteOwnResponse()
    await user.click(screen.getByRole('button', { name: 'Preview offline import' }))
    await waitFor(() => expect(calls.previews).toBe(1))

    // Closing is itself an invalidation: the late reply is dropped unread, and
    // reopening shows no preview and offers no way to publish.
    await user.click(screen.getByRole('button', { name: 'Close offline history import' }))
    release(previewDocument())
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Open offline history import' })).toBeTruthy(),
    )
    expect(screen.queryByRole('region', { name: 'Offline history import preview' })).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Open offline history import' }))
    expect(screen.queryByRole('region', { name: 'Offline history import preview' })).toBeNull()
    expect((screen.getByRole('button', { name: 'Publish offline version' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
