/**
 * The one seam this file covers: an explicitly mapped source may only be
 * published from a preview of the exact declaration on screen.
 *
 * Everything else about the upload panel is covered from the strict side in
 * research-plugins.test.tsx, which must keep passing unchanged.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter } from './app'
import {
  AVAILABILITY_PROVIDED,
  type LocalStrategyCapability,
  type LocalStrategyRegisterRequest,
  type LocalStrategySlot,
  type MappedPreview,
  type UploadDeclaration,
  type UploadPreviewResponse,
  type UploadVersion,
} from './research-api'

const DIGEST = 'b7d4e0a1'.repeat(8)

const mappedPreview: MappedPreview = {
  schema: 'ea.local-csv-mapping-preview.v1',
  mode: 'explicit-mapping',
  valid: true,
  first_error: null,
  byte_count: 412,
  profile: 'ea-phase1-ohlcv-csv-v1',
  source: { byte_count: 96, sha256: DIGEST, record_count: 4, stored_here: false },
  capture: { byte_count: 412, sha256: 'c'.repeat(64), data_sha256: 'd'.repeat(64), record_count: 4 },
  mapping: { columns: { close: 'c' }, constants: {} },
  time: { timezone: 'UTC', formats: { interval_start: 'utc-microseconds' }, target_format: 'utc-microseconds' },
  quality: {
    bar_count: 4,
    revision_count: 4,
    bar_durations_seconds: ['60'],
    observed_gap_count: 0,
    largest_gap_seconds: null,
    bar_start_utc: '2024-01-02T00:00:00.000000Z',
    bar_end_utc: '2024-01-02T00:03:00.000000Z',
  },
  preview: { rows: [{ record_number: 1, close: '100.5' }], row_count_sampled: 1, truncated: false, valid: true },
  mapping_evidence_sha256: DIGEST,
  limitations: ['A preview reads a bounded sample; a publication is only accepted after the whole source is verified.'],
}

const mappedVersion = {
  schema: 'ea.local-dataset-version.v2',
  dataset_id: 'upload-1',
  kind: 'ohlcv-csv',
  profile: 'ea-phase1-ohlcv-csv-v1',
  version: 1,
  published_at: '2024-01-02T00:00:00Z',
  source: { declared_by: 'uploader', asserted: true, reference: null, file_name: null, license_reference: null },
  availability_policy: { id: AVAILABILITY_PROVIDED, version: 1, latency_seconds: null, pit_verified: false, evidence: 'declared_by_uploader' },
  source_bytes_sha256: DIGEST,
  source_byte_count: 96,
  raw_retained: true,
  capture_sha256: 'c'.repeat(64),
  capture_byte_count: 412,
  data_sha256: 'd'.repeat(64),
  record_count: 4,
  instrument: { venue: 'XNAS', symbol: 'AAPL' },
  quality: mappedPreview.quality,
  limitations: mappedPreview.limitations,
  manifest_sha256: 'e'.repeat(64),
  mapping_evidence_sha256: DIGEST,
} as unknown as UploadVersion & { datasets: [] }

const MAPPING = '{"columns":{"close":"c"},"constants":{}}'
const TIME = '{"timezone":"UTC","formats":{"interval_start":"utc-microseconds"}}'

const ARTIFACT = 'a'.repeat(64)
const inspected: LocalStrategyCapability = {
  schema: 'ea.plugin-local-strategy.v1',
  warning: 'This package runs in this process with no sandbox.',
  static_only: true,
  trust_established: false,
  plugin_id: 'ea.local.strategy.example.probe',
  package_version: '1.0.0',
  manifest_digest: 'sha256:' + 'b'.repeat(64),
  entrypoint: 'ea.host.adapter.local-strategy',
  effects: ['local_compute'],
  identity: { package_id: 'example.probe' },
  package: {
    package_id: 'example.probe',
    schema_version: 1,
    artifact_sha256: ARTIFACT,
    source: { kind: 'local-package', package_id: 'example.probe', artifact_sha256: ARTIFACT },
  },
  descriptor: {
    strategy_id: 'local-probe-entry-v1',
    strategy_version: 1,
    display_name: 'Local probe',
    parameters: [{ name: 'target_quantity', type: 'decimal', default: '2' }],
  },
  manifest: {},
}

const emptySlot: LocalStrategySlot = {
  schema: 'ea.local-strategy-slot.v1',
  registered: false,
  trusted: false,
  revoked: false,
  scenario_id: 'local-package.yaml',
  capability: null,
  grant: null,
  warning: inspected.warning,
  limitations: ['One local package slot per workspace.'],
}

const trustedSlot: LocalStrategySlot = {
  ...emptySlot,
  registered: true,
  trusted: true,
  base_scenario_id: 'bounded-long.yaml',
  capability: inspected,
  grant: {
    schema: 'ea.plugin-local-strategy-grant.v1',
    trusted: true,
    plugin_id: inspected.plugin_id,
    package_version: inspected.package_version,
    manifest_digest: inspected.manifest_digest,
    implementation_digest: 'sha256:' + ARTIFACT,
    effects: ['local_compute'],
  },
}

function adapter(overrides: Partial<ApiAdapter> = {}) {
  const calls = {
    previews: 0,
    publishes: 0,
    lastDeclaration: undefined as UploadDeclaration | undefined,
    publishRequest: undefined as Record<string, unknown> | undefined,
  }
  const api: ApiAdapter = {
    holdoutScenarios: async () => [],
    listResearchTasks: async () => [],
    getResearchTask: async () => { throw new Error('this fixture opens no task') },
    pluginCatalog: async () => { throw new Error('this fixture reads no catalog') },
    resolveRecipe: async () => { throw new Error('this fixture resolves no recipe') },
    listDeclarations: async () => { throw new Error('this fixture lists no declarations') },
    validateDeclaration: async () => { throw new Error('this fixture validates no declaration') },
    registerDeclaration: async () => { throw new Error('this fixture registers no declaration') },
    previewUpload: async (_content: string, _fileName?: string, declaration?: UploadDeclaration) => {
      calls.previews += 1
      calls.lastDeclaration = declaration
      return mappedPreview as UploadPreviewResponse
    },
    publishUpload: async (request) => {
      calls.publishes += 1
      calls.publishRequest = request as unknown as Record<string, unknown>
      return mappedVersion
    },
    createResearchTask: async () => { throw new Error('this fixture creates no task') },
    editResearchSpec: async () => { throw new Error('this fixture edits no spec') },
    listScenarios: async () => [],
    listBacktests: async () => [],
    listHoldouts: async () => [],
    artifactUrl: (jobId, name) => `/api/backtests/${jobId}/artifacts/${name}`,
    createBacktest: async () => { throw new Error('this fixture submits no backtest') },
    createBatch: async () => { throw new Error('this fixture submits no batch') },
    getBatch: async () => { throw new Error('this fixture holds no batch') },
    getBacktest: async () => { throw new Error('this fixture holds no backtest') },
    getReport: async () => { throw new Error('this fixture holds no report') },
    getEquityPath: async () => { throw new Error('this fixture holds no equity path') },
    validateScenario: async () => { throw new Error('this fixture validates no scenario') },
    createHoldout: async () => { throw new Error('this fixture creates no holdout') },
    getHoldout: async () => { throw new Error('this fixture holds no holdout') },
    ...overrides,
  }
  return { api, calls }
}

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('explicitly mapped upload', () => {
  it('requires a preview of the exact declaration and drops it when the declaration changes', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter()
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    const content = await screen.findByLabelText('CSV content')
    fireEvent.change(content, { target: { value: 'seq,c\n1,100.5' } })
    await user.selectOptions(screen.getByLabelText('Upload mode'), 'explicit-mapping')
    // The guided controls are the default editor; this file is about the
    // declaration written as JSON, so it chooses that editor explicitly.
    await user.click(screen.getByLabelText('Advanced JSON'))
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), { target: { value: MAPPING } })
    fireEvent.change(screen.getByLabelText(/^Time declaration/), { target: { value: TIME } })
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)

    // A declaration and a policy are not a verification: without a preview the
    // panel offers no way to publish the mapping.
    const publish = screen.getByRole('button', { name: 'Publish version' }) as HTMLButtonElement
    expect(publish.disabled).toBe(true)
    expect(screen.getByText(/published only from a preview of this exact declaration/)).toBeTruthy()
    expect(calls.publishes).toBe(0)

    await user.click(screen.getByRole('button', { name: 'Preview sample' }))
    await waitFor(() => expect(calls.previews).toBe(1))
    // The declaration reaches the host whole; the panel computes no evidence of
    // its own and sends no digest.
    expect(calls.lastDeclaration).toMatchObject({
      mode: 'explicit-mapping',
      availability_policy: AVAILABILITY_PROVIDED,
    })
    expect(calls.lastDeclaration?.mapping).toEqual({ columns: { close: 'c' }, constants: {} })
    expect(calls.lastDeclaration?.time_declaration).toEqual({
      timezone: 'UTC',
      formats: { interval_start: 'utc-microseconds' },
    })

    // The preview reports the mapper's own verdict and the exact evidence digest
    // the publication would seal.
    const verifiedPreview = await screen.findByRole('region', { name: 'Mapped upload preview' })
    expect(within(verifiedPreview).getByText(/Mapping evidence SHA-256/)).toBeTruthy()
    expect(within(verifiedPreview).getAllByText(new RegExp(DIGEST)).length).toBeGreaterThan(0)
    expect(within(verifiedPreview).getByRole('cell', { name: '100.5' })).toBeTruthy()

    const fresh = screen.getByRole('button', { name: 'Publish version' }) as HTMLButtonElement
    expect(fresh.disabled).toBe(false)

    // Editing the mapping makes the previewed declaration a different one, so the
    // preview stops authorizing a publication.
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), {
      target: { value: '{"columns":{"close":"not_a_column"},"constants":{}}' },
    })
    expect((screen.getByRole('button', { name: 'Publish version' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Publish version' }))
    expect(calls.publishes).toBe(0)

    // Restoring the exact declaration and previewing it again authorizes it.
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), { target: { value: MAPPING } })
    await user.click(screen.getByRole('button', { name: 'Preview sample' }))
    await waitFor(() => expect(calls.previews).toBe(2))
    await user.click(screen.getByRole('button', { name: 'Publish version' }))
    await waitFor(() => expect(calls.publishes).toBe(1))
    expect(calls.publishRequest).toMatchObject({
      content: 'seq,c\n1,100.5',
      mode: 'explicit-mapping',
      availability_policy: AVAILABILITY_PROVIDED,
      mapping: { columns: { close: 'c' }, constants: {} },
      time_declaration: { timezone: 'UTC', formats: { interval_start: 'utc-microseconds' } },
    })
    // The evidence digest the preview named is what the published version reports.
    expect(await screen.findByText(/the declaration is sealed with the version/)).toBeTruthy()
  })

  it('never publishes from a preview the host refused', async () => {
    const user = userEvent.setup()
    const { api, calls } = adapter({
      previewUpload: async () => {
        calls.previews += 1
        return {
          ...mappedPreview,
          valid: false,
          preview: undefined,
          source: undefined,
          capture: undefined,
          quality: undefined,
          mapping_evidence_sha256: null,
          first_error: {
            code: 'mapping_column_missing',
            message: 'the declaration names a column this file does not have',
            record_number: 1,
            field_name: 'close',
          },
        }
      },
    })
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    fireEvent.change(await screen.findByLabelText('CSV content'), { target: { value: 'seq,c\n1,100.5' } })
    await user.selectOptions(screen.getByLabelText('Upload mode'), 'explicit-mapping')
    await user.click(screen.getByLabelText('Advanced JSON'))
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), { target: { value: MAPPING } })
    fireEvent.change(screen.getByLabelText(/^Time declaration/), { target: { value: TIME } })
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)

    await user.click(screen.getByRole('button', { name: 'Preview sample' }))
    await waitFor(() => expect(calls.previews).toBe(1))

    // The refusal is shown, and a fresh preview of a refused declaration is still
    // no authorization: the host would refuse the publication anyway.
    const refusedPreview = await screen.findByRole('region', { name: 'Mapped upload preview' })
    expect(within(refusedPreview).getByText(/refused/)).toBeTruthy()
    expect(await screen.findByText(/mapping_column_missing/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Publish version' }))
    expect(calls.publishes).toBe(0)
    expect(screen.getByText(/a refused preview is never published/)).toBeTruthy()
  })
})

describe('local strategy package', () => {
  it('trusts only the identity it inspected, and withdraws that trust explicitly', async () => {
    const user = userEvent.setup()
    const calls = {
      inspect: 0,
      register: undefined as LocalStrategyRegisterRequest | undefined,
      revoke: 0,
    }
    const { api } = adapter({
      localStrategySlot: async () => emptySlot,
      inspectLocalStrategy: async () => { calls.inspect += 1; return inspected },
      registerLocalStrategy: async (request) => { calls.register = request; return trustedSlot },
      revokeLocalStrategy: async () => { calls.revoke += 1; return { ...trustedSlot, trusted: false, revoked: true } },
      listScenarios: async () => [
        { scenario_id: 'bounded-long.yaml', name: 'bounded-long', valid: true },
        { scenario_id: 'invalid.yaml', name: 'invalid', valid: false },
      ],
    })
    window.history.pushState({}, '', '/research')
    render(<App api={api} />)

    // A File-shaped object: the panel's own read path is exercised, and the
    // browser's own File carries the same arrayBuffer method.
    const file = {
      name: 'probe.eastrategy',
      arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
    } as unknown as File
    fireEvent.change(await screen.findByLabelText('Package file (.eastrategy)'), {
      target: { files: [file] },
    })
    fireEvent.change(screen.getByLabelText(/Capability manifest/), {
      target: { value: '{"schema_version":1,"plugin_id":"ea.local.strategy.example.probe"}' },
    })
    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Inspect package' }) as HTMLButtonElement).disabled).toBe(false),
    )
    await user.click(screen.getByRole('button', { name: 'Inspect package' }))
    await waitFor(() => expect(calls.inspect).toBe(1))

    const panel = await screen.findByRole('region', { name: 'Package inspection' })
    expect(within(panel).getByText(/local-probe-entry-v1/)).toBeTruthy()
    expect(within(panel).getByText(new RegExp(`sha256:${ARTIFACT}`))).toBeTruthy()

    // Inspection alone is not trust: the decision needs a base scenario, the
    // explicit checkbox and the local_compute permission.
    const trustButton = screen.getByRole('button', { name: 'Trust and register' }) as HTMLButtonElement
    expect(trustButton.disabled).toBe(true)
    await user.click(screen.getByLabelText(/I have read these digests and trust this exact identity/))
    // Only a valid registered scenario is offered as a base.
    const offered = Array.from((screen.getByLabelText(/Base scenario/) as HTMLSelectElement).options, option => option.value)
    expect(offered).toContain('bounded-long.yaml')
    expect(offered).not.toContain('invalid.yaml')
    await user.selectOptions(screen.getByLabelText(/Base scenario/), 'bounded-long.yaml')
    expect(trustButton.disabled).toBe(true)
    await user.click(screen.getByLabelText(/Grant local_compute/))
    expect(trustButton.disabled).toBe(false)

    await user.click(trustButton)
    await waitFor(() => expect(calls.register).toBeDefined())
    // Every expected value is the digest the operator just read, not a guess.
    expect(calls.register).toMatchObject({
      trusted: true,
      effects: ['local_compute'],
      base_scenario_id: 'bounded-long.yaml',
      expected_plugin_id: inspected.plugin_id,
      expected_package_version: inspected.package_version,
      expected_manifest_digest: inspected.manifest_digest,
      expected_implementation_digest: `sha256:${ARTIFACT}`,
    })
    expect(await screen.findByText(/research now offers the scenario local-package.yaml/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'Revoke trust' }))
    await waitFor(() => expect(calls.revoke).toBe(1))
    expect(await screen.findByText(/no later run can start it/)).toBeTruthy()
  })
})
