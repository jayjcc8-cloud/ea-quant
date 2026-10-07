/**
 * QUINE local appearance: one preference model, one runtime, one settings panel.
 *
 * Everything asserted here is this browser's own surface — a preference document
 * in local storage plus three attributes on the document root. No backend, route,
 * provider, model or secret is involved, and no value below is engine evidence.
 *
 * The claims a reader can re-check:
 *   · a saved preference is read back, applied to the root element and persisted,
 *     so a fresh runtime over the same storage comes up with the saved appearance;
 *   · every malformed or illegal stored value falls back to that field's default —
 *     an unknown enum member, an out-of-range, non-finite or non-numeric width —
 *     without discarding the valid fields stored beside it;
 *   · storage that refuses a read or a write never throws into the application,
 *     and a refused write is reported as unsaved rather than as saved;
 *   · the OS colour scheme is consulted only while the theme follows the system,
 *     the media-query listener is released as soon as it stops following, and
 *     disposing the runtime leaves no listener behind;
 *   · the panel presents the saved values, applies a change immediately, keeps the
 *     AI column inside 320–480 px, and restores every default from one action.
 *
 * The width control is a native `input[type=range]`; a change event is what a
 * browser produces when a keyboard adjusts it, so that is what is dispatched here.
 * Real arrow-key operation is accepted in the browser by `e2e/appearance.spec.ts`.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import {
  AI_WIDTH_MAX,
  AI_WIDTH_MIN,
  APPEARANCE_DEFAULTS,
  APPEARANCE_STORAGE_KEY,
  applyAppearance,
  createAppearanceRuntime,
  normalizeAppearance,
  readAppearance,
  writeAppearance,
  type AppearanceRuntime,
  type MediaQueryLike,
  type PreferenceStorage,
} from './appearance'
import { AppearanceSettings } from './appearance-settings'

/** One store this browser owns. `writes` counts the writes it accepted. */
class FakeStorage implements PreferenceStorage {
  private readonly entries = new Map<string, string>()
  writes = 0
  constructor(initial?: Record<string, string>) {
    for (const [key, value] of Object.entries(initial ?? {})) this.entries.set(key, value)
  }
  getItem(key: string): string | null {
    return this.entries.has(key) ? this.entries.get(key)! : null
  }
  setItem(key: string, value: string): void {
    this.writes += 1
    this.entries.set(key, value)
  }
  stored(): unknown {
    const raw = this.getItem(APPEARANCE_STORAGE_KEY)
    return raw === null ? null : JSON.parse(raw)
  }
}

/** A store holding one preference document, written as the app would write it. */
function storageWith(value: unknown): FakeStorage {
  return new FakeStorage({
    [APPEARANCE_STORAGE_KEY]: typeof value === 'string' ? value : JSON.stringify(value),
  })
}

/** A media query a test drives itself, counting its own subscriptions. */
function fakeSystem(initialDark = false) {
  const listeners = new Set<(event: { matches: boolean }) => void>()
  let matches = initialDark
  let adds = 0
  let removes = 0
  const query: MediaQueryLike = {
    get matches() { return matches },
    addEventListener: (_type, listener) => { adds += 1; listeners.add(listener) },
    removeEventListener: (_type, listener) => { removes += 1; listeners.delete(listener) },
  }
  return {
    query,
    subscriptions: () => ({ adds, removes, listeners: listeners.size }),
    setScheme(dark: boolean) {
      matches = dark
      for (const listener of [...listeners]) listener({ matches: dark })
    },
  }
}

const root = document.documentElement
const widthOn = (element: HTMLElement) =>
  element.querySelector('[data-appearance="ai-width"]')?.textContent ?? null
const checked = (name: string) => (screen.getByRole('radio', { name }) as HTMLInputElement).checked

/** A runtime over a detached element, so a pure case cannot disturb the document. */
function detachedRuntime(storage: PreferenceStorage, systemQuery: MediaQueryLike | null = null) {
  const element = document.createElement('div')
  return { element, runtime: createAppearanceRuntime({ storage, systemQuery, root: element }) }
}

afterEach(() => {
  cleanup()
  delete root.dataset.theme
  delete root.dataset.density
  root.style.removeProperty('--quine-ai-width')
})

describe('the appearance preference model', () => {
  it('writes the effective theme, the density and the AI width onto the root element', () => {
    const element = document.createElement('div')
    expect(applyAppearance({ theme: 'dark', density: 'compact', aiWidth: 448 }, element, false)).toBe('dark')
    expect(element.dataset.theme).toBe('dark')
    expect(element.dataset.density).toBe('compact')
    expect(element.style.getPropertyValue('--quine-ai-width')).toBe('448px')

    // The system preference is the only one that reads the OS scheme; an explicit
    // theme is the effective theme whatever the OS reports.
    expect(applyAppearance({ theme: 'system', density: 'normal', aiWidth: 396 }, element, true)).toBe('dark')
    expect(element.dataset.theme).toBe('dark')
    expect(applyAppearance({ theme: 'system', density: 'normal', aiWidth: 396 }, element, false)).toBe('light')
    expect(element.dataset.theme).toBe('light')
    expect(applyAppearance({ theme: 'light', density: 'normal', aiWidth: 396 }, element, true)).toBe('light')
    expect(element.dataset.theme).toBe('light')
  })

  it('falls back on the field that is illegal and keeps the fields that are not', () => {
    expect(APPEARANCE_DEFAULTS).toEqual({ theme: 'system', density: 'normal', aiWidth: 396 })

    const illegal: [string, unknown][] = [
      ['a string', 'dark'],
      ['a number', 7],
      ['null itself', null],
      ['an array', ['dark', 'compact']],
      ['an unknown theme', { theme: 'sepia' }],
      ['a non-string theme', { theme: 42 }],
      ['a null theme', { theme: null }],
      ['an unknown density', { density: 'roomy' }],
      ['a non-string density', { density: {} }],
      ['a numeric string width', { aiWidth: '396' }],
      ['a null width', { aiWidth: null }],
      ['a NaN width', { aiWidth: Number.NaN }],
      ['an infinite width', { aiWidth: Number.POSITIVE_INFINITY }],
      ['a negative infinite width', { aiWidth: Number.NEGATIVE_INFINITY }],
      ['a fractional width', { aiWidth: 396.5 }],
      ['a width below the range', { aiWidth: AI_WIDTH_MIN - 1 }],
      ['a width above the range', { aiWidth: AI_WIDTH_MAX + 1 }],
      ['a boolean width', { aiWidth: true }],
    ]
    for (const [label, value] of illegal) {
      const normalized = normalizeAppearance(value)
      expect(normalized, label).toEqual(APPEARANCE_DEFAULTS)
    }

    // One bad field never discards the two good ones beside it.
    expect(normalizeAppearance({ theme: 'sepia', density: 'compact', aiWidth: 400 }))
      .toEqual({ theme: 'system', density: 'compact', aiWidth: 400 })
    expect(normalizeAppearance({ theme: 'dark', density: 'roomy', aiWidth: 'wide' }))
      .toEqual({ theme: 'dark', density: 'normal', aiWidth: 396 })

    // The bounds themselves are legal values.
    expect(normalizeAppearance({ theme: 'light', density: 'compact', aiWidth: AI_WIDTH_MIN }).aiWidth).toBe(AI_WIDTH_MIN)
    expect(normalizeAppearance({ theme: 'light', density: 'compact', aiWidth: AI_WIDTH_MAX }).aiWidth).toBe(AI_WIDTH_MAX)
  })

  it('reads, writes and falls back without letting a storage failure escape', () => {
    const store = storageWith({ theme: 'dark', density: 'compact', aiWidth: 432 })
    expect(readAppearance(store)).toEqual({ theme: 'dark', density: 'compact', aiWidth: 432 })

    // A document that is absent, unreadable as JSON, or a JSON value that is not
    // one preference record falls back rather than throwing.
    for (const stored of [undefined, '{', '"dark"', '[]', 'null', '{"theme":"sepia"}']) {
      expect(readAppearance(new FakeStorage(stored === undefined ? undefined : { [APPEARANCE_STORAGE_KEY]: stored })))
        .toEqual(APPEARANCE_DEFAULTS)
    }

    const refused: PreferenceStorage = {
      getItem() { throw new Error('storage is disabled') },
      setItem() { throw new Error('quota exceeded') },
    }
    expect(() => readAppearance(refused)).not.toThrow()
    expect(readAppearance(refused)).toEqual(APPEARANCE_DEFAULTS)

    expect(writeAppearance({ theme: 'dark', density: 'compact', aiWidth: 432 }, store)).toBe(true)
    expect(store.stored()).toEqual({ theme: 'dark', density: 'compact', aiWidth: 432 })
    expect(store.writes).toBe(1)

    // A refused write is reported as not saved, and never as an exception.
    expect(() => writeAppearance(APPEARANCE_DEFAULTS, refused)).not.toThrow()
    expect(writeAppearance(APPEARANCE_DEFAULTS, refused)).toBe(false)
  })
})

describe('the appearance runtime', () => {
  it('applies a chosen appearance immediately and comes back with it in a fresh runtime', () => {
    const store = new FakeStorage()
    const first = detachedRuntime(store)
    expect(first.runtime.get()).toEqual(APPEARANCE_DEFAULTS)
    expect(first.element.dataset.theme).toBe('light')
    expect(first.element.dataset.density).toBe('normal')
    expect(first.element.style.getPropertyValue('--quine-ai-width')).toBe('396px')

    expect(first.runtime.set({ theme: 'dark', density: 'compact', aiWidth: 448 })).toBe(true)
    expect(first.element.dataset.theme).toBe('dark')
    expect(first.element.dataset.density).toBe('compact')
    expect(first.element.style.getPropertyValue('--quine-ai-width')).toBe('448px')
    expect(first.runtime.get()).toEqual({ theme: 'dark', density: 'compact', aiWidth: 448 })
    first.runtime.dispose()

    // The reload: a new runtime over the same store starts from what was saved.
    const reloaded = detachedRuntime(store)
    expect(reloaded.runtime.get()).toEqual({ theme: 'dark', density: 'compact', aiWidth: 448 })
    expect(reloaded.element.dataset.theme).toBe('dark')
    reloaded.runtime.dispose()

    // An illegal stored document is applied per field: the two fields it states
    // legally are kept, and only the illegal one is replaced by its default.
    const corrupt = detachedRuntime(storageWith({ theme: 'sepia', density: 'compact', aiWidth: 9000 }))
    expect(corrupt.runtime.get()).toEqual({ theme: 'system', density: 'compact', aiWidth: 396 })
    expect(corrupt.element.dataset.density).toBe('compact')
    expect(corrupt.element.style.getPropertyValue('--quine-ai-width')).toBe('396px')
    corrupt.runtime.dispose()
  })

  it('follows the OS scheme only while the theme follows the system, and releases the listener', () => {
    const store = new FakeStorage()
    const system = fakeSystem(false)
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: system.query, root })

    // Following the system: the OS scheme is consulted, and the listener is held.
    expect(root.dataset.theme).toBe('light')
    expect(system.subscriptions()).toEqual({ adds: 1, removes: 0, listeners: 1 })
    system.setScheme(true)
    expect(root.dataset.theme).toBe('dark')

    // An explicit theme takes over and the OS listener is released, not left behind.
    runtime.set({ theme: 'dark', density: 'normal', aiWidth: 396 })
    expect(system.subscriptions()).toEqual({ adds: 1, removes: 1, listeners: 0 })
    system.setScheme(false)
    expect(root.dataset.theme).toBe('dark')

    runtime.set({ theme: 'light', density: 'normal', aiWidth: 396 })
    system.setScheme(true)
    expect(root.dataset.theme).toBe('light')
    expect(system.subscriptions().adds).toBe(1)

    // Going back to the system subscribes once more, and the OS scheme — which the
    // test left reporting dark — takes effect again immediately. Disposing leaves
    // nothing behind, so the scheme cannot reach the document any more.
    runtime.set({ theme: 'system', density: 'normal', aiWidth: 396 })
    expect(root.dataset.theme).toBe('dark')
    expect(system.subscriptions()).toEqual({ adds: 2, removes: 1, listeners: 1 })
    runtime.dispose()
    expect(system.subscriptions()).toEqual({ adds: 2, removes: 2, listeners: 0 })
    system.setScheme(true)
    expect(root.dataset.theme).toBe('dark')

    // A workspace with no media query at all still applies and persists.
    const none = detachedRuntime(store, null)
    expect(none.runtime.set({ theme: 'system', density: 'compact', aiWidth: 400 })).toBe(true)
    expect(none.element.dataset.density).toBe('compact')
    none.runtime.dispose()
  })
})

describe('the local appearance settings panel', () => {
  function show(runtime: AppearanceRuntime) {
    return render(<AppearanceSettings runtime={runtime} />)
  }

  it('presents the saved appearance with labelled, keyboard-operable controls', () => {
    const store = storageWith({ theme: 'dark', density: 'compact', aiWidth: 420 })
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)

    expect(screen.getByRole('region', { name: '本地外观' })).toBeTruthy()
    expect(checked('深色')).toBe(true)
    expect(checked('跟随系统')).toBe(false)
    expect(checked('紧凑')).toBe(true)
    expect(checked('标准')).toBe(false)

    // The width is a native range inside the allowed span, labelled, described, and
    // showing its value in px.
    const width = screen.getByRole('slider', { name: 'AI 侧栏宽度' }) as HTMLInputElement
    expect(width.getAttribute('type')).toBe('range')
    expect(width.getAttribute('min')).toBe(String(AI_WIDTH_MIN))
    expect(width.getAttribute('max')).toBe(String(AI_WIDTH_MAX))
    expect(width.value).toBe('420')
    expect(width.getAttribute('aria-describedby')).toBeTruthy()
    expect(widthOn(root)).toBe('420 px')
    runtime.dispose()
  })

  it('applies a theme and a density change immediately and keeps them after a reload', async () => {
    const user = userEvent.setup()
    const store = new FakeStorage()
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)
    expect(root.dataset.theme).toBe('light')

    await user.click(screen.getByRole('radio', { name: '深色' }))
    expect(root.dataset.theme).toBe('dark')
    expect(store.stored()).toEqual({ theme: 'dark', density: 'normal', aiWidth: 396 })

    await user.click(screen.getByRole('radio', { name: '紧凑' }))
    expect(root.dataset.density).toBe('compact')
    runtime.dispose()

    const reloaded = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    expect(reloaded.get()).toEqual({ theme: 'dark', density: 'compact', aiWidth: 396 })
    expect(root.dataset.theme).toBe('dark')
    expect(root.dataset.density).toBe('compact')
    reloaded.dispose()
  })

  it('keeps the AI width inside its bounds and never shows a value outside them', () => {
    const store = new FakeStorage()
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)
    const width = screen.getByRole('slider', { name: 'AI 侧栏宽度' })

    fireEvent.change(width, { target: { value: String(AI_WIDTH_MAX) } })
    expect(widthOn(root)).toBe(`${AI_WIDTH_MAX} px`)
    expect(root.style.getPropertyValue('--quine-ai-width')).toBe(`${AI_WIDTH_MAX}px`)

    fireEvent.change(width, { target: { value: String(AI_WIDTH_MIN) } })
    expect(widthOn(root)).toBe(`${AI_WIDTH_MIN} px`)

    // A value outside the span is never applied as it stands.
    fireEvent.change(width, { target: { value: '9000' } })
    expect(runtime.get().aiWidth).toBeLessThanOrEqual(AI_WIDTH_MAX)
    expect(runtime.get().aiWidth).toBeGreaterThanOrEqual(AI_WIDTH_MIN)
    fireEvent.change(width, { target: { value: '1' } })
    expect(runtime.get().aiWidth).toBeGreaterThanOrEqual(AI_WIDTH_MIN)
    expect(runtime.get().aiWidth).toBeLessThanOrEqual(AI_WIDTH_MAX)

    // An unreadable value leaves the applied width alone rather than blanking it.
    fireEvent.change(width, { target: { value: 'wide' } })
    expect(runtime.get().aiWidth).toBeGreaterThanOrEqual(AI_WIDTH_MIN)
    expect(runtime.get().aiWidth).toBeLessThanOrEqual(AI_WIDTH_MAX)
    expect(store.stored()).toEqual(runtime.get())
    runtime.dispose()
  })

  it('restores every default from the one explicit action', async () => {
    const user = userEvent.setup()
    const store = storageWith({ theme: 'dark', density: 'compact', aiWidth: 448 })
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)
    expect(root.dataset.theme).toBe('dark')

    await user.click(screen.getByRole('button', { name: '恢复默认' }))

    expect(runtime.get()).toEqual(APPEARANCE_DEFAULTS)
    expect(root.dataset.theme).toBe('light')
    expect(root.dataset.density).toBe('normal')
    expect(widthOn(root)).toBe('396 px')
    expect(store.stored()).toEqual(APPEARANCE_DEFAULTS)
    expect(checked('跟随系统')).toBe(true)
    expect(checked('标准')).toBe(true)
    runtime.dispose()
  })

  it('reports a change it could not save as unsaved instead of crashing', async () => {
    const user = userEvent.setup()
    const refused: PreferenceStorage = {
      getItem() { throw new Error('storage is disabled') },
      setItem() { throw new Error('quota exceeded') },
    }
    const runtime = createAppearanceRuntime({ storage: refused, systemQuery: null, root })
    expect(() => show(runtime)).not.toThrow()

    // A refused read falls back to the defaults rather than an empty or guessed panel.
    expect(checked('跟随系统')).toBe(true)
    expect(widthOn(root)).toBe('396 px')
    expect(screen.queryByRole('alert')).toBeNull()

    // The change still takes effect for this session, and it is honestly named unsaved.
    await user.click(screen.getByRole('radio', { name: '深色' }))
    expect(root.dataset.theme).toBe('dark')
    expect(screen.getByRole('alert').textContent).toContain('未能保存')
    expect(screen.getByRole('alert').textContent).toContain('本地')

    // A later change that saves again clears the notice.
    runtime.dispose()
    const store = new FakeStorage()
    const saving = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    cleanup()
    show(saving)
    await user.click(screen.getByRole('radio', { name: '深色' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(store.stored()).toEqual({ theme: 'dark', density: 'normal', aiWidth: 396 })
    saving.dispose()
  })
})
