/**
 * The guided mapped-upload editor, and the declaration it reads.
 *
 * The strict Phase 1 profile has fifteen fixed destinations. Before this, a user
 * who owns a vendor export had to write two JSON declarations by hand. Here each
 * destination gets one control, the column choices come from the file's own header
 * row and nowhere else, and the timestamp forms come from the host's own closed
 * allowlist.
 *
 * There is one declaration, not two: the JSON text is the panel's only state, and
 * these controls are read from it on every render, so a guided choice and a JSON
 * edit can never disagree or silently overwrite one another. A declaration these
 * controls cannot represent stays in the JSON editor with the reason.
 *
 * What this module is not: a mapper, a validator or a guess. It does not read a
 * single data row, it never fills a field, it never rebinds a choice to a column
 * that appeared later, and it never decides whether a declaration is acceptable —
 * the host does that, and the panel shows the host's own refusal. A value this
 * module cannot establish (the zone, the exact timestamp form, every column and
 * every constant) stays empty until the user states it.
 */
import { useEffect, useMemo, useRef } from 'react'
import { AVAILABILITY_SIMULATED, TIME_FORMAT_IDS } from './research-api'

/** The strict Phase 1 OHLCV destinations, in the decoder's own order. */
const STRICT_DESTINATIONS: readonly string[] = [
  'schema_version',
  'venue',
  'symbol',
  'interval_start',
  'interval_end',
  'adjustment',
  'open',
  'high',
  'low',
  'close',
  'volume',
  'source',
  'source_sequence',
  'revision',
  'available_at',
]

/**
 * The destinations that describe the capture rather than carry an observation.
 *
 * These are the only ones the mapper accepts as a declared constant
 * (`ea.web.csv_mapping._METADATA_FIELDS`); every other destination is an
 * economic or sequence value and must come from a source column.
 */
const CONSTANT_DESTINATIONS: readonly string[] = [
  'schema_version',
  'venue',
  'symbol',
  'adjustment',
  'source',
  'revision',
]

/** The destinations whose values are timestamps with one exact declared form. */
const TIMESTAMP_DESTINATIONS: readonly string[] = [
  'interval_start',
  'interval_end',
  'available_at',
]

/** The one option value that means "declare this destination as a constant". */
const CONSTANT_OPTION = '\u0000constant'

/** What one destination currently holds: a source column, or a stated constant. */
export type GuidedChoice = { kind: 'column' | 'constant'; value: string }

/** The declaration as these controls describe it: one choice per destination. */
export type GuidedState = {
  choices: Record<string, GuidedChoice>
  timezone: string
  formats: Record<string, string>
}

/** The declaration on screen, either as choices or as the reason it is not. */
export type GuidedReading = { ok: true; state: GuidedState } | { ok: false; problem: string }

/**
 * One file's header row, or the reason it cannot name its columns.
 *
 * Only the first record is read: a data row is not a column name, so nothing
 * below the header is ever consulted. A quoted comma and a doubled quote are one
 * name each, a CRLF or LF ends the record, and a byte-order mark is dropped from
 * the names *with a notice* — the bytes sent to the host are still the file's own,
 * mark included, so the refusal that follows is the host's and not a silent
 * repair performed here.
 *
 * A name is either wholly quoted or wholly unquoted. A quote inside an unquoted
 * name, or text after a closing quote, is refused rather than read: a reader that
 * guessed where such a name ends would offer a column this file does not have.
 */
type HeaderReading =
  | { ok: true; names: string[]; notices: string[] }
  | { ok: false; problem: string }

function readHeader(content: string): HeaderReading {
  if (!content) return { ok: false, problem: 'This file is empty, so it has no header row to read.' }
  const notices: string[] = []
  let text = content
  if (text.startsWith('﻿')) {
    text = text.slice(1)
    notices.push(
      'This file starts with a byte-order mark. The column names below are read as if it were not there, so the mark never becomes part of a name — but the bytes sent to the host are this file’s own, mark included, and the host refuses a source that begins with one.',
    )
  }
  const names: string[] = []
  let field = ''
  let quoted = false
  let closed = false
  let index = 0
  for (; index < text.length; index += 1) {
    const character = text[index]
    if (quoted) {
      if (character !== '"') { field += character; continue }
      if (text[index + 1] === '"') { field += '"'; index += 1; continue }
      quoted = false
      closed = true
      continue
    }
    if (closed) {
      // A quoted name is the whole name. Text after the closing quote is a name
      // this reader cannot read, and looking for where it might end would invent
      // a column the file does not have.
      if (character === ',') { names.push(field); field = ''; closed = false; continue }
      if (character === '\n' || character === '\r') break
      const junk = text.slice(index).split(/[,\r\n]/, 1)[0]
      return {
        ok: false,
        problem: `The header has "${junk}" right after a quoted column name, so this file does not say where that name ends. A quoted name must be the whole name.`,
      }
    }
    if (character === '"') {
      if (field !== '') {
        return {
          ok: false,
          problem: `The header has a quote inside the column name it read as "${field}", so this file does not say where that name ends. A name containing a comma or a quote is written as one whole quoted name.`,
        }
      }
      quoted = true
      continue
    }
    if (character === ',') { names.push(field); field = ''; continue }
    if (character === '\n' || character === '\r') break
    field += character
  }
  if (quoted) {
    return {
      ok: false,
      problem: 'The header row ends inside a quoted column name, so this file does not name its own columns. Save the file with an intact header row and read it again.',
    }
  }
  names.push(field)
  const unnamed = names.findIndex(name => name.trim() === '')
  if (unnamed >= 0) {
    return {
      ok: false,
      problem: `Column ${unnamed + 1} of the header has no name. An unnamed column is never given a name here, so no destination can be pointed at a column of this file.`,
    }
  }
  const seen = new Set<string>()
  for (const name of names) {
    if (seen.has(name)) {
      return {
        ok: false,
        problem: `The header names ${name} more than once, so a destination pointed at ${name} would not say which column it means. Remove the repeated column from the file and read it again.`,
      }
    }
    seen.add(name)
  }
  return { ok: true, names, notices }
}

/**
 * The mapping declaration these choices describe, as the panel's own JSON text.
 *
 * A constant with no value typed yet is written as it stands: choosing to declare
 * a constant is a decision the JSON carries, and an empty string says "declared as
 * this constant, no value stated yet". Nothing a choice holds is dropped here, so
 * the text on screen is always the declaration the controls describe.
 */
function mappingOf(state: GuidedState, policy: string): string {
  const columns: Record<string, string> = {}
  const constants: Record<string, string> = {}
  for (const field of STRICT_DESTINATIONS) {
    // A derived availability policy computes the instant itself, so declaring a
    // column for it would contradict the policy the user chose.
    if (field === 'available_at' && policy === AVAILABILITY_SIMULATED) continue
    const choice = state.choices[field]
    if (!choice) continue
    if (choice.kind === 'constant') constants[field] = choice.value
    else if (choice.value) columns[field] = choice.value
  }
  return JSON.stringify({ columns, constants })
}

/** The time declaration these choices describe, as the panel's own JSON text. */
function timeOf(state: GuidedState, policy: string): string {
  const formats: Record<string, string> = {}
  for (const field of TIMESTAMP_DESTINATIONS) {
    if (field === 'available_at' && policy === AVAILABILITY_SIMULATED) continue
    const format = state.formats[field]
    if (format) formats[field] = format
  }
  return JSON.stringify({ timezone: state.timezone, formats })
}

/** The destinations still waiting for a choice, named so the user can act. */
function undeclared(state: GuidedState, policy: string): string[] {
  return STRICT_DESTINATIONS.filter(field => {
    if (field === 'available_at' && policy === AVAILABILITY_SIMULATED) return false
    return !state.choices[field]?.value
  })
}

/** One JSON object from the panel's own text; blank text is an empty declaration. */
function jsonObject(text: string, label: string): Record<string, unknown> | string {
  if (text.trim() === '') return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return `${label} is not valid JSON.`
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return `${label} must be one JSON object.`
  }
  return parsed as Record<string, unknown>
}

/** One named object inside a declaration, or the reason it is not one. */
function namedObject(
  document: Record<string, unknown>,
  key: string,
  label: string,
): Record<string, unknown> | string {
  const value = document[key]
  if (value === undefined) return {}
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return `${label} must give ${key} as one JSON object of names to text.`
  }
  return value as Record<string, unknown>
}

/**
 * Read the two declarations the panel holds back as the choices it shows.
 *
 * This is a reading, not a validator: it says whether the declaration on screen
 * can be shown as these controls. It never says whether the host will accept it —
 * a declaration read cleanly here is still the host's to refuse. Nothing is
 * repaired or re-pointed: a destination that is not one of the strict profile's
 * own, a constant on a field that carries an observation, a timestamp form outside
 * the host's allowlist and a value that is not text are each returned as the
 * reason this declaration stays in the JSON editor.
 *
 * The availability policy is not read here: under the derived policy the writing
 * side omits `available_at` altogether, so a declaration carrying it is still one
 * these controls can show, and the host refuses it if it is ever sent as it stands.
 */
export function readGuidedDeclaration(mappingText: string, timeText: string): GuidedReading {
  const mapping = jsonObject(mappingText, 'The mapping declaration')
  if (typeof mapping === 'string') return { ok: false, problem: mapping }
  const time = jsonObject(timeText, 'The time declaration')
  if (typeof time === 'string') return { ok: false, problem: time }

  const strays = [...Object.keys(mapping), ...Object.keys(time)].filter(
    key => !['columns', 'constants', 'timezone', 'formats'].includes(key),
  )
  if (strays.length > 0) {
    return {
      ok: false,
      problem: `The declaration carries ${strays.join(', ')}, which the strict profile does not read: a mapping is columns and constants, and a time declaration is timezone and formats.`,
    }
  }
  const columns = namedObject(mapping, 'columns', 'The mapping declaration')
  if (typeof columns === 'string') return { ok: false, problem: columns }
  const constants = namedObject(mapping, 'constants', 'The mapping declaration')
  if (typeof constants === 'string') return { ok: false, problem: constants }

  const choices: Record<string, GuidedChoice> = {}
  for (const [field, value] of Object.entries(columns)) {
    if (!STRICT_DESTINATIONS.includes(field)) {
      return {
        ok: false,
        problem: `The mapping declaration names ${field}, which is not one of the strict profile's destinations.`,
      }
    }
    if (typeof value !== 'string' || value === '') {
      return { ok: false, problem: `The mapping declaration gives ${field} no column name.` }
    }
    choices[field] = { kind: 'column', value }
  }
  for (const [field, value] of Object.entries(constants)) {
    if (choices[field]) {
      return {
        ok: false,
        problem: `The mapping declaration gives ${field} both a column and a constant; each destination is one or the other.`,
      }
    }
    if (!CONSTANT_DESTINATIONS.includes(field)) {
      return {
        ok: false,
        problem: `The mapping declaration makes ${field} a constant; only ${CONSTANT_DESTINATIONS.join(', ')} may be constants, because every other destination carries an observation or a sequence.`,
      }
    }
    if (typeof value !== 'string') {
      return {
        ok: false,
        problem: `The mapping declaration gives ${field} a constant that is not text.`,
      }
    }
    choices[field] = { kind: 'constant', value }
  }

  const timezone = time.timezone
  if (timezone !== undefined && typeof timezone !== 'string') {
    return { ok: false, problem: 'The time declaration gives a timezone that is not text.' }
  }
  const stated = namedObject(time, 'formats', 'The time declaration')
  if (typeof stated === 'string') return { ok: false, problem: stated }

  const formats: Record<string, string> = {}
  for (const [field, value] of Object.entries(stated)) {
    if (!TIMESTAMP_DESTINATIONS.includes(field)) {
      return {
        ok: false,
        problem: `The time declaration names ${field}, which is not a timestamp destination.`,
      }
    }
    if (typeof value !== 'string' || !(TIME_FORMAT_IDS as readonly string[]).includes(value)) {
      return {
        ok: false,
        problem: `The time declaration gives ${field} a form this host does not accept; it accepts ${TIME_FORMAT_IDS.join(', ')}.`,
      }
    }
    formats[field] = value
  }
  return { ok: true, state: { choices, timezone: timezone ?? '', formats } }
}

type GuidedMappingFieldsProps = {
  /** The source exactly as the panel holds it; only its header row is read. */
  content: string
  /** The availability policy already chosen, which decides whether `available_at` is used. */
  policy: string
  disabled: boolean
  /**
   * The declaration the panel holds, read back as choices. These controls keep no
   * copy of their own, so a guided choice and a JSON edit cannot disagree.
   */
  state: GuidedState
  /** The declaration the user's choices describe, written into the panel's own text. */
  onChange: (mapping: string, time: string) => void
}

/**
 * The guided controls. They write the declaration the panel already sends; they
 * add no request, no endpoint and no second path to the host.
 */
export function GuidedMappingFields({ content, policy, disabled, state, onChange }: GuidedMappingFieldsProps) {
  const header = useMemo(() => readHeader(content), [content])
  const simulated = policy === AVAILABILITY_SIMULATED

  // One reconciliation, and only while these controls are the editor on screen:
  // changing the policy changes which destinations a declaration may carry, so the
  // same choices are written again for it. Every other write here is a control the
  // user touched, written straight into the one declaration.
  const appliedPolicy = useRef(policy)
  useEffect(() => {
    if (appliedPolicy.current === policy) return
    appliedPolicy.current = policy
    onChange(mappingOf(state, policy), timeOf(state, policy))
  }, [policy, state, onChange])

  const choose = (field: string, choice: GuidedChoice | null) => {
    const choices = { ...state.choices }
    if (choice) choices[field] = choice
    else delete choices[field]
    onChange(mappingOf({ ...state, choices }, policy), timeOf(state, policy))
  }
  const setFormat = (field: string, value: string) => {
    const formats = { ...state.formats, [field]: value }
    onChange(mappingOf(state, policy), timeOf({ ...state, formats }, policy))
  }
  const setZone = (value: string) => {
    onChange(mappingOf(state, policy), timeOf({ ...state, timezone: value }, policy))
  }

  const names = header.ok ? header.names : []
  const waiting = undeclared(state, policy)

  return <fieldset className="guided-mapping">
    <legend>Guided mapping · choose each destination from your own file</legend>
    <p className="muted">
      Every one of the strict profile’s fifteen destinations is a choice you make here. The column list is this
      file’s own header row and nothing else — no data row is read and no value is inferred — and each timestamp
      form is one of {TIME_FORMAT_IDS.length} exact forms this host accepts. A choice you make stays as you made
      it, even when the file no longer has that column: the host then refuses the declaration rather than
      re-pointing it for you.
    </p>
    {!header.ok && <p className="notice error" role="alert">{header.problem}</p>}
    {header.ok && header.notices.map(notice => <p key={notice} className="notice error" role="alert">{notice}</p>)}
    <p className="muted">
      {header.ok
        ? `This file’s header row, read as names only: ${names.join(' · ')}`
        : 'No column can be chosen from this header, so no destination is offered a column.'}
    </p>
    <div className="parameter-grid">
      {STRICT_DESTINATIONS.map(field => {
        if (field === 'available_at' && simulated) {
          // Not a destination the user declares under this policy, and not an
          // editable value the panel would then ignore: the policy derives it.
          return <div key={field}>
            <label htmlFor={`guided-${field}`}>{field}</label>
            <select id={`guided-${field}`} disabled defaultValue="derived">
              <option value="derived">Derived by the policy (interval end + the latency you state)</option>
            </select>
            <small id={`guided-hint-${field}`}>
              Not declared here: the simulated policy computes this instant itself.
            </small>
          </div>
        }
        const choice = state.choices[field]
        const held = choice?.value ?? ''
        const stale = choice !== undefined && choice.kind === 'column' && !names.includes(choice.value)
        const mayBeConstant = CONSTANT_DESTINATIONS.includes(field)
        const selectValue = header.ok ? (choice ? (choice.kind === 'constant' ? CONSTANT_OPTION : held) : '') : ''
        return <div key={field}>
          <label htmlFor={`guided-${field}`}>{field}</label>
          <select
            id={`guided-${field}`}
            value={selectValue}
            disabled={disabled}
            aria-describedby={`guided-hint-${field}`}
            onChange={event => {
              const raw = event.target.value
              choose(field, raw === CONSTANT_OPTION
                ? { kind: 'constant', value: '' }
                : raw ? { kind: 'column', value: raw } : null)
            }}
          >
            <option value="">Not declared…</option>
            {stale && <option value={held}>{held} (not in this file)</option>}
            {names.map(name => <option key={name} value={name}>{name}</option>)}
            {mayBeConstant && <option value={CONSTANT_OPTION}>Constant value (metadata only)…</option>}
          </select>
          <small id={`guided-hint-${field}`}>
            {mayBeConstant ? 'a column of this file, or one constant you state' : 'a column of this file'}
          </small>
          {choice?.kind === 'constant' && <div>
            <label htmlFor={`guided-constant-${field}`}>{`${field} constant value`}</label>
            <input
              id={`guided-constant-${field}`}
              maxLength={120}
              value={choice.value}
              disabled={disabled}
              onChange={event => choose(field, { kind: 'constant', value: event.target.value })}
            />
          </div>}
        </div>
      })}
    </div>
    <div className="parameter-grid">
      <div>
        <label htmlFor="guided-timezone">Time zone</label>
        <input
          id="guided-timezone"
          maxLength={64}
          placeholder="IANA zone, e.g. UTC or Asia/Shanghai"
          value={state.timezone}
          disabled={disabled}
          onChange={event => setZone(event.target.value)}
        />
        <small>The zone these timestamps are stated in. It is never guessed from the values.</small>
      </div>
      {TIMESTAMP_DESTINATIONS.filter(field => field !== 'available_at' || !simulated).map(field =>
        state.choices[field]?.value || state.formats[field]
          ? <div key={field}>
            <label htmlFor={`guided-format-${field}`}>{`${field} time format`}</label>
            <select
              id={`guided-format-${field}`}
              value={state.formats[field] ?? ''}
              disabled={disabled}
              onChange={event => setFormat(field, event.target.value)}
            >
              <option value="">Not stated…</option>
              {TIME_FORMAT_IDS.map(id => <option key={id} value={id}>{id}</option>)}
            </select>
            <small>The exact form this file’s {field} values are written in.</small>
          </div>
          : null,
      )}
    </div>
    <p className="muted">
      {simulated
        ? 'The simulated policy derives availability from each bar’s end and the latency you state, so available_at is not a choice here and no availability column or form is declared.'
        : 'Availability is taken from the source itself under the provided-in-capture policy, so available_at is a column and a form you choose, like every other destination.'}
    </p>
    <p className="muted" aria-live="polite">
      {waiting.length === 0
        ? 'Every destination is declared. Preview it: the host decides whether the declaration and this file agree.'
        : `Still to declare: ${waiting.join(', ')}. A declaration that does not state every destination is the host’s to refuse, and this panel sends exactly the declaration you see.`}
    </p>
  </fieldset>
}
