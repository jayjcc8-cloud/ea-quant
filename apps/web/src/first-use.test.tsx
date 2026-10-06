/**
 * First-use path 2: the guided upload controls.
 *
 * The strict upload profile has fifteen fixed destinations. Until now a user who
 * owns a vendor export had to write two JSON declarations by hand before the
 * panel would even preview their file. These tests describe the outcome that
 * replaces that: choose a destination per control, choose a column from the
 * file's own header, state the zone and the exact timestamp form, and preview —
 * with no JSON typed at all. The JSON declarations stay available as the advanced
 * path and are covered, unchanged, by research-integration.test.tsx. The two are
 * not two declarations: the guided controls are read from the JSON text and write
 * back into it, so a declaration the user may have edited as JSON is the one the
 * controls show, and a declaration the controls cannot hold stays in the JSON with
 * the reason instead of being dropped.
 *
 * Every source here is a synthetic fixture from `../preview/first-use-fixtures`,
 * and every host answer is one of that module's *scripted* documents, selected by
 * name. No test asserts that this file can judge a declaration — it cannot and
 * does not try. What is asserted is what the panel sends and what it renders:
 * the declaration contract it submits, the bytes it keeps as the payload, and the
 * host's own refusal text when the host refuses.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { App, type ApiAdapter } from './app'
import {
  AVAILABILITY_PROVIDED,
  AVAILABILITY_SIMULATED,
  TIME_FORMAT_IDS,
  type MappedPreview,
  type UploadDeclaration,
  type UploadPublishRequest,
} from './research-api'
import {
  MAPPED_PREVIEW_OK,
  REFUSALS,
  SYNTHETIC_CSV,
  SYNTHETIC_CSV_AFTER_QUOTE,
  SYNTHETIC_CSV_ALTERNATE,
  SYNTHETIC_CSV_BYTE_ORDER_MARK,
  SYNTHETIC_CSV_DUPLICATE_HEADER,
  SYNTHETIC_CSV_EMPTY_HEADER_NAME,
  SYNTHETIC_CSV_QUOTE_IN_NAME,
  SYNTHETIC_FILE_NAME,
  decodeBase64,
  scriptedAdapter,
} from '../preview/first-use-fixtures'

type User = ReturnType<typeof userEvent.setup>

/**
 * The scripted host, plus what the panel actually submitted.
 *
 * `answers` is a finite list of scripted host documents used in order, the last
 * one repeating. Nothing is computed from the request: a refusal is a fixture the
 * test chose by name. Both preview routes are recorded, because the payload a
 * selected file is previewed as is not the payload a paste is previewed as.
 */
function host(answers: MappedPreview[] = [MAPPED_PREVIEW_OK]) {
  const script = answers.length > 0 ? answers : [MAPPED_PREVIEW_OK]
  const calls = {
    previews: 0,
    publishes: 0,
    lastDeclaration: undefined as UploadDeclaration | undefined,
    lastPublish: undefined as UploadPublishRequest | undefined,
    lastFileBase64: undefined as string | undefined,
  }
  const answer = () => script[Math.min(calls.previews - 1, script.length - 1)]
  const api: ApiAdapter = {
    ...scriptedAdapter(),
    previewUpload: async (_content: string, _fileName?: string, declaration?: UploadDeclaration) => {
      calls.previews += 1
      calls.lastDeclaration = declaration
      return answer()
    },
    previewUploadFile: async (contentBase64: string, declaration?: UploadDeclaration) => {
      calls.previews += 1
      calls.lastDeclaration = declaration
      calls.lastFileBase64 = contentBase64
      return answer()
    },
    publishUpload: async (request: UploadPublishRequest) => {
      calls.publishes += 1
      calls.lastPublish = request
      const base = scriptedAdapter()
      if (!base.publishUpload) throw new Error('the scripted host publishes nothing')
      return base.publishUpload(request)
    },
  }
  return { api, calls }
}

function workspace(api: ApiAdapter) {
  window.history.pushState({}, '', '/research')
  render(<App api={api} />)
}

/** The guided controls, which is where every destination choice is made. */
const guided = () => within(screen.getByRole('group', { name: /guided mapping/i }))

/** The column names one destination is offered, as its own control lists them. */
const columnOptions = (field: string): string[] =>
  Array.from((guided().getByLabelText(field) as HTMLSelectElement).options, option => option.value)
    .filter(value => value !== '')

const columnSelect = (field: string) => guided().getByLabelText(field) as HTMLSelectElement

const declareColumn = (field: string, column: string) => {
  const select = columnSelect(field)
  expect(columnOptions(field)).toContain(column)
  fireEvent.change(select, { target: { value: column } })
  expect(select.value).toBe(column)
}

/** The constant choice is found by its own wording, never by a value invented here. */
const declareConstant = (field: string, value: string) => {
  const select = columnSelect(field)
  const constant = Array.from(select.options).find(option => /constant/i.test(option.textContent ?? ''))
  expect(constant, `${field} offers no constant choice`).toBeTruthy()
  fireEvent.change(select, { target: { value: constant?.value } })
  fireEvent.change(guided().getByLabelText(`${field} constant value`), { target: { value } })
}

const declareFormat = (field: string, format: string) => {
  fireEvent.change(guided().getByLabelText(`${field} time format`), { target: { value: format } })
}

const EXPECTED_MAPPING = {
  columns: {
    venue: 'mic',
    symbol: 'tick,er',
    interval_start: 'bar_start',
    interval_end: 'bar_end',
    open: 'o',
    high: 'h',
    low: 'l',
    close: 'c',
    volume: 'v',
    source_sequence: 'seq',
    available_at: 'say "hi"',
  },
  constants: { schema_version: '1', adjustment: 'raw', source: 'vendor.a', revision: '0' },
}

const EXPECTED_TIME = {
  timezone: 'Asia/Shanghai',
  formats: {
    interval_start: 'naive-seconds',
    interval_end: 'naive-seconds',
    available_at: 'naive-seconds',
  },
}

/** Open the mapped panel over a synthetic export, without touching any JSON field. */
async function openMappedPanel(user: User, content: string = SYNTHETIC_CSV) {
  fireEvent.change(await screen.findByLabelText('CSV content'), { target: { value: content } })
  await user.selectOptions(screen.getByLabelText('Upload mode'), 'explicit-mapping')
}

/** Every strict destination of the synthetic export, chosen by control only. */
function declareSyntheticMapping() {
  declareConstant('schema_version', '1')
  declareColumn('venue', 'mic')
  declareColumn('symbol', 'tick,er')
  declareColumn('interval_start', 'bar_start')
  declareColumn('interval_end', 'bar_end')
  declareConstant('adjustment', 'raw')
  declareColumn('open', 'o')
  declareColumn('high', 'h')
  declareColumn('low', 'l')
  declareColumn('close', 'c')
  declareColumn('volume', 'v')
  declareConstant('source', 'vendor.a')
  declareColumn('source_sequence', 'seq')
  declareConstant('revision', '0')
  declareColumn('available_at', 'say "hi"')
  fireEvent.change(guided().getByLabelText('Time zone'), { target: { value: 'Asia/Shanghai' } })
  declareFormat('interval_start', 'naive-seconds')
  declareFormat('interval_end', 'naive-seconds')
  declareFormat('available_at', 'naive-seconds')
}

const publishButton = () => screen.getByRole('button', { name: 'Publish version' }) as HTMLButtonElement
const previewButton = () => screen.getByRole('button', { name: 'Preview sample' }) as HTMLButtonElement

afterEach(() => {
  cleanup()
  window.history.pushState({}, '', '/backtests')
})

describe('guided mapping', () => {
  it('reads the file’s own header and publishes exactly the declaration it previewed', async () => {
    const user = userEvent.setup()
    const { api, calls } = host()
    workspace(api)
    await openMappedPanel(user)

    // The options are the file's own header, read the way the file writes it: one
    // name for a quoted comma, one name for an escaped quote, and never a value
    // that only appears in a data row.
    const offered = columnOptions('close')
    expect(offered).toContain('c')
    expect(offered).toContain('tick,er')
    expect(offered).toContain('say "hi"')
    expect(offered).not.toContain('tick')
    expect(offered).not.toContain('er')
    expect(offered).not.toContain('"tick,er"')
    expect(offered).not.toContain('say ""hi""')
    // `AAPL` and `XNAS` appear in this file only inside data rows; a reader that
    // swept the whole file instead of its header would offer them.
    expect(offered).not.toContain('AAPL')
    expect(offered).not.toContain('XNAS')

    // Nothing is pre-filled: the zone is stated by the user, never guessed, and a
    // declaration and a policy are not a verification.
    expect((guided().getByLabelText('Time zone') as HTMLInputElement).value).toBe('')
    expect(publishButton().disabled).toBe(true)

    declareSyntheticMapping()

    // The timestamp forms are the mapper's own closed allowlist, and nothing else.
    const formats = Array.from(
      (guided().getByLabelText('interval_start time format') as HTMLSelectElement).options,
      option => option.value,
    ).filter(value => value !== '')
    expect(formats).toEqual([...TIME_FORMAT_IDS])

    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())

    await waitFor(() => expect(calls.previews).toBe(1))
    expect(calls.lastDeclaration?.mode).toBe('explicit-mapping')
    expect(calls.lastDeclaration?.mapping).toEqual(EXPECTED_MAPPING)
    expect(calls.lastDeclaration?.time_declaration).toEqual(EXPECTED_TIME)
    expect(calls.lastDeclaration?.availability_policy).toBe(AVAILABILITY_PROVIDED)
    expect(publishButton().disabled).toBe(false)

    // Publishing sends the declaration this exact preview verified, the policy that
    // was stated, and the source as the text on screen — no second declaration and
    // no derived value. A paste is not byte-exact: a textarea's value is the edited
    // text, so this fixture's CRLF endings are LF there, and that edited text is
    // what the panel sends. A file selected as its own bytes is where the original
    // bytes are kept, and that is a separate case below.
    await user.click(publishButton())
    await waitFor(() => expect(calls.publishes).toBe(1))
    const published = calls.lastPublish
    const edited = (screen.getByLabelText('CSV content') as HTMLTextAreaElement).value
    expect(edited).toBe(SYNTHETIC_CSV.replace(/\r\n/g, '\n'))
    expect(published?.content).toBe(edited)
    expect(published?.content_base64).toBeUndefined()
    expect(published?.mode).toBe('explicit-mapping')
    expect(published?.mapping).toEqual(EXPECTED_MAPPING)
    expect(published?.time_declaration).toEqual(EXPECTED_TIME)
    expect(published?.availability_policy).toBe(AVAILABILITY_PROVIDED)
    expect(published?.latency_seconds).toBeUndefined()
    // The published version is shown as the host's own document, and an upload is
    // never reported as verified point-in-time availability.
    const shown = within(await screen.findByRole('region', { name: 'Published dataset version' }))
    expect(shown.getByText(/point-in-time verified: false/)).toBeTruthy()
  })

  it('previews and publishes a selected file as its own bytes, CRLF and quoted header included', async () => {
    const user = userEvent.setup()
    const { api, calls } = host()
    workspace(api)

    // A real File, read the way the browser holds it. This is the case where the
    // payload is the file's own bytes rather than the text on screen, so the CRLF
    // endings and the quoted comma in the header reach the host as written.
    const file = new File([SYNTHETIC_CSV], SYNTHETIC_FILE_NAME, { type: 'text/csv' })
    await user.upload(await screen.findByLabelText('CSV file'), file)
    await waitFor(() => expect(screen.getByText(/sent as its own bytes/)).toBeTruthy())

    await user.selectOptions(screen.getByLabelText('Upload mode'), 'explicit-mapping')
    declareSyntheticMapping()
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    // The preview was of these bytes, and of the declaration the controls describe.
    expect(decodeBase64(calls.lastFileBase64 ?? '')).toBe(SYNTHETIC_CSV)
    expect(calls.lastDeclaration?.mapping).toEqual(EXPECTED_MAPPING)

    await user.click(publishButton())
    await waitFor(() => expect(calls.publishes).toBe(1))
    const published = calls.lastPublish
    // A selected file is published as bytes, never through the text view, and it
    // carries the name it was selected under.
    expect(published?.content).toBeUndefined()
    expect(decodeBase64(published?.content_base64 ?? '')).toBe(SYNTHETIC_CSV)
    expect(published?.file_name).toBe(SYNTHETIC_FILE_NAME)
  })

  it('drops the preview as soon as a guided choice changes', async () => {
    const user = userEvent.setup()
    const { api, calls } = host()
    workspace(api)
    await openMappedPanel(user)
    declareSyntheticMapping()
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    expect(publishButton().disabled).toBe(false)

    // The verified declaration is gone the moment one of its choices changes.
    declareColumn('close', 'v')
    expect(publishButton().disabled).toBe(true)
    fireEvent.click(publishButton())
    expect(calls.publishes).toBe(0)
  })

  it('keeps one declaration when the JSON editor and the guided controls are both used', async () => {
    const user = userEvent.setup()
    const { api, calls } = host()
    workspace(api)
    await openMappedPanel(user)
    declareColumn('close', 'c')
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    expect(publishButton().disabled).toBe(false)

    // The JSON editor is an explicit choice, and it edits the one declaration the
    // controls show rather than a second copy of it.
    await user.click(screen.getByLabelText('Advanced JSON'))
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), {
      target: { value: '{"columns":{"close":"o"},"constants":{}}' },
    })
    // That is a different declaration from the one the preview verified.
    expect(publishButton().disabled).toBe(true)

    // Going back to the controls shows what the JSON says, not the earlier choice.
    await user.click(screen.getByLabelText('Guided controls'))
    expect(columnSelect('close').value).toBe('o')

    // A guided change writes that same declaration back, so it neither reverts the
    // column the JSON editor stated nor keeps a choice of its own beside it.
    fireEvent.change(guided().getByLabelText('Time zone'), { target: { value: 'UTC' } })
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(2))
    expect(calls.lastDeclaration?.mapping).toEqual({ columns: { close: 'o' }, constants: {} })
    expect(calls.lastDeclaration?.time_declaration).toEqual({ timezone: 'UTC', formats: {} })
    expect(publishButton().disabled).toBe(false)
  })

  it('leaves a declaration the controls cannot hold in the JSON editor, with the reason', async () => {
    const user = userEvent.setup()
    const { api, calls } = host()
    workspace(api)
    await openMappedPanel(user)

    // The guided controls are the default editor; choosing the JSON editor is what
    // makes this declaration one the user typed.
    await user.click(screen.getByLabelText('Advanced JSON'))
    const typed = '{"columns":{"close":"c","volume":"v"},"constants":{"close":"1"}}'
    fireEvent.change(screen.getByLabelText(/^Mapping declaration/), { target: { value: typed } })

    // `close` cannot be a column and a constant at once, so no control can hold
    // this declaration. It stays exactly as typed, the reason is on screen, and no
    // control is offered that would drop one of the two values or re-point it.
    const controls = screen.getByLabelText('Guided controls') as HTMLInputElement
    expect(controls.disabled).toBe(true)
    expect((screen.getByLabelText(/^Mapping declaration/) as HTMLTextAreaElement).value).toBe(typed)
    expect(screen.getByText(/both a column and a constant/)).toBeTruthy()
    expect(screen.queryByRole('group', { name: /guided mapping/i })).toBeNull()
    fireEvent.click(publishButton())
    expect(calls.publishes).toBe(0)
    expect(calls.previews).toBe(0)
  })

  it('keeps a destination chosen for a renamed column visible instead of re-pointing it', async () => {
    const user = userEvent.setup()
    const { api, calls } = host([MAPPED_PREVIEW_OK, REFUSALS.mapping_column_missing])
    workspace(api)
    await openMappedPanel(user)
    declareSyntheticMapping()
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    expect(publishButton().disabled).toBe(false)

    // The next file from the same vendor renamed that column.
    fireEvent.change(screen.getByLabelText('CSV content'), { target: { value: SYNTHETIC_CSV_ALTERNATE } })

    // The earlier preview is not authorization for a different file...
    expect(publishButton().disabled).toBe(true)
    // ...and the choice is neither silently re-pointed at another column nor
    // silently accepted: it stays as the user made it, and says it is not here.
    const select = columnSelect('available_at')
    expect(select.value).toBe('say "hi"')
    expect(select.options[select.selectedIndex]?.textContent ?? '').toMatch(/not in this file/)
    expect(columnOptions('available_at')).toContain('seen')

    // The declaration is still the one the user made, so the host is what refuses
    // it, and the refusal is the host's own sentence.
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(2))
    expect(calls.lastDeclaration?.mapping.columns.available_at).toBe('say "hi"')
    expect(await screen.findByText(/mapping_column_missing/)).toBeTruthy()
    expect(publishButton().disabled).toBe(true)
    expect(calls.publishes).toBe(0)
  })

  it('invents no column name from a header that cannot name its columns', async () => {
    const user = userEvent.setup()
    const { api, calls } = host([REFUSALS.invalid_encoding])
    workspace(api)
    await openMappedPanel(user, SYNTHETIC_CSV_DUPLICATE_HEADER)

    // A repeated name is ambiguous and an unnamed column names nothing, so there
    // is no column to choose — and the reason is on screen, not implied.
    expect(columnOptions('close')).toEqual([])
    expect(columnOptions('interval_start')).toEqual([])
    expect(
      screen.getAllByRole('alert').some(node => /header/i.test(node.textContent ?? '')),
    ).toBe(true)
    expect(publishButton().disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('CSV content'), { target: { value: SYNTHETIC_CSV_EMPTY_HEADER_NAME } })
    expect(columnOptions('close')).toEqual([])
    expect(publishButton().disabled).toBe(true)
    expect(calls.publishes).toBe(0)

    // A quote inside an unquoted name, and text right after a closing quote, are
    // not names either: `clo"se"` is not the column `close`, and `"close"x` is not
    // the column `closex`. Reading either one as a whole name would offer a column
    // this file does not have, so the header is refused instead.
    for (const [source, problem] of [
      [SYNTHETIC_CSV_QUOTE_IN_NAME, /quote inside the column name/i],
      [SYNTHETIC_CSV_AFTER_QUOTE, /right after a quoted column name/i],
    ] as const) {
      fireEvent.change(screen.getByLabelText('CSV content'), { target: { value: source } })
      expect(columnOptions('close')).toEqual([])
      expect(columnOptions('interval_start')).toEqual([])
      expect(
        screen.getAllByRole('alert').some(node => problem.test(node.textContent ?? '')),
      ).toBe(true)
      expect(publishButton().disabled).toBe(true)
    }

    // A byte-order mark is not part of a name either. The names are the file's
    // real ones, the mark is stated rather than repaired, and the source itself
    // is the host's to refuse — which it does.
    fireEvent.change(screen.getByLabelText('CSV content'), { target: { value: SYNTHETIC_CSV_BYTE_ORDER_MARK } })
    const markedNames = columnOptions('interval_start')
    expect(markedNames).toContain('bar_start')
    expect(markedNames).not.toContain('﻿bar_start')
    expect(
      screen.getAllByRole('alert').some(node => /byte-order mark/i.test(node.textContent ?? '')),
    ).toBe(true)
    declareSyntheticMapping()
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    // The payload keeps the mark: nothing here silently strips it.
    expect(await screen.findByText(/invalid_encoding/)).toBeTruthy()
    expect(publishButton().disabled).toBe(true)
    expect(calls.publishes).toBe(0)
  })

  it('keeps availability explicit and declares no availability column for a derived policy', async () => {
    const user = userEvent.setup()
    const { api, calls } = host([MAPPED_PREVIEW_OK, MAPPED_PREVIEW_OK])
    workspace(api)
    await openMappedPanel(user)
    declareSyntheticMapping()
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(1))
    expect(calls.lastDeclaration?.mapping.columns.available_at).toBe('say "hi"')
    expect(calls.lastDeclaration?.time_declaration.formats.available_at).toBe('naive-seconds')

    // Under a derived policy the host computes availability itself, so declaring a
    // column for it would be a contradiction the host refuses. The panel asks for
    // the latency instead, and declares no availability column or form at all.
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_SIMULATED)
    expect(guided().queryByLabelText('available_at time format')).toBeNull()
    fireEvent.change(screen.getByLabelText('Simulated latency (seconds)'), { target: { value: '45' } })
    await user.click(previewButton())
    await waitFor(() => expect(calls.previews).toBe(2))
    const declaration = calls.lastDeclaration
    expect(declaration?.mapping.columns).not.toHaveProperty('available_at')
    expect(declaration?.mapping.constants).not.toHaveProperty('available_at')
    expect(declaration?.time_declaration.formats).not.toHaveProperty('available_at')
    expect(declaration?.time_declaration.formats).toEqual({
      interval_start: 'naive-seconds',
      interval_end: 'naive-seconds',
    })
    expect(declaration?.availability_policy).toBe(AVAILABILITY_SIMULATED)
    expect(declaration?.latency_seconds).toBe(45)
  })

  it('shows the host’s refusal of an incomplete declaration inline and publishes nothing', async () => {
    const user = userEvent.setup()
    const { api, calls } = host([REFUSALS.mapping_declaration])
    workspace(api)
    await openMappedPanel(user)

    // One destination is not a declaration, and this panel does not pretend to
    // judge that: it sends what the controls describe and shows the host's answer.
    declareColumn('close', 'c')
    fireEvent.change(guided().getByLabelText('Time zone'), { target: { value: 'UTC' } })
    await user.selectOptions(screen.getByLabelText('Availability policy'), AVAILABILITY_PROVIDED)
    expect(calls.lastDeclaration).toBeUndefined()
    await user.click(previewButton())

    await waitFor(() => expect(calls.previews).toBe(1))
    expect(calls.lastDeclaration?.mapping).toEqual({ columns: { close: 'c' }, constants: {} })
    const refusal = await screen.findByText(/mapping_declaration/)
    expect(refusal.closest('[role="alert"]')).toBeTruthy()
    expect(publishButton().disabled).toBe(true)
    expect(calls.publishes).toBe(0)
  })
})
