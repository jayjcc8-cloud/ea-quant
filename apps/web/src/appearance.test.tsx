import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import {
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
})

describe('the appearance preference model', () => {
  it('writes the effective theme and density onto the root element', () => {
    const element = document.createElement('div')
    expect(applyAppearance({ theme: 'dark', density: 'compact' }, element, false)).toBe('dark')
    expect(element.dataset.theme).toBe('dark')
    expect(element.dataset.density).toBe('compact')

    // The system preference is the only one that reads the OS scheme; an explicit
    // theme is the effective theme whatever the OS reports.
    expect(applyAppearance({ theme: 'system', density: 'normal' }, element, true)).toBe('dark')
    expect(element.dataset.theme).toBe('dark')
    expect(applyAppearance({ theme: 'system', density: 'normal' }, element, false)).toBe('light')
    expect(element.dataset.theme).toBe('light')
    expect(applyAppearance({ theme: 'light', density: 'normal' }, element, true)).toBe('light')
    expect(element.dataset.theme).toBe('light')
  })

  it('falls back on the field that is illegal and keeps the fields that are not', () => {
    expect(APPEARANCE_DEFAULTS).toEqual({ theme: 'system', density: 'normal' })

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
    ]
    for (const [label, value] of illegal) {
      const normalized = normalizeAppearance(value)
      expect(normalized, label).toEqual(APPEARANCE_DEFAULTS)
    }

    // One bad field never discards a valid field beside it.
    expect(normalizeAppearance({ theme: 'sepia', density: 'compact' }))
      .toEqual({ theme: 'system', density: 'compact' })
    expect(normalizeAppearance({ theme: 'dark', density: 'roomy' }))
      .toEqual({ theme: 'dark', density: 'normal' })

  })

  it('reads, writes and falls back without letting a storage failure escape', () => {
    const store = storageWith({ theme: 'dark', density: 'compact' })
    expect(readAppearance(store)).toEqual({ theme: 'dark', density: 'compact' })

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

    expect(writeAppearance({ theme: 'dark', density: 'compact' }, store)).toBe(true)
    expect(store.stored()).toEqual({ theme: 'dark', density: 'compact' })
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

    expect(first.runtime.set({ theme: 'dark', density: 'compact' })).toBe(true)
    expect(first.element.dataset.theme).toBe('dark')
    expect(first.element.dataset.density).toBe('compact')
    expect(first.runtime.get()).toEqual({ theme: 'dark', density: 'compact' })
    first.runtime.dispose()

    // The reload: a new runtime over the same store starts from what was saved.
    const reloaded = detachedRuntime(store)
    expect(reloaded.runtime.get()).toEqual({ theme: 'dark', density: 'compact' })
    expect(reloaded.element.dataset.theme).toBe('dark')
    reloaded.runtime.dispose()

    // An illegal stored document is applied per field: a valid density is
    // kept, and only the illegal theme is replaced by its default.
    const corrupt = detachedRuntime(storageWith({ theme: 'sepia', density: 'compact' }))
    expect(corrupt.runtime.get()).toEqual({ theme: 'system', density: 'compact' })
    expect(corrupt.element.dataset.density).toBe('compact')
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
    runtime.set({ theme: 'dark', density: 'normal' })
    expect(system.subscriptions()).toEqual({ adds: 1, removes: 1, listeners: 0 })
    system.setScheme(false)
    expect(root.dataset.theme).toBe('dark')

    runtime.set({ theme: 'light', density: 'normal' })
    system.setScheme(true)
    expect(root.dataset.theme).toBe('light')
    expect(system.subscriptions().adds).toBe(1)

    // Going back to the system subscribes once more, and the OS scheme — which the
    // test left reporting dark — takes effect again immediately. Disposing leaves
    // nothing behind, so the scheme cannot reach the document any more.
    runtime.set({ theme: 'system', density: 'normal' })
    expect(root.dataset.theme).toBe('dark')
    expect(system.subscriptions()).toEqual({ adds: 2, removes: 1, listeners: 1 })
    runtime.dispose()
    expect(system.subscriptions()).toEqual({ adds: 2, removes: 2, listeners: 0 })
    system.setScheme(true)
    expect(root.dataset.theme).toBe('dark')

    // A workspace with no media query at all still applies and persists.
    const none = detachedRuntime(store, null)
    expect(none.runtime.set({ theme: 'system', density: 'compact' })).toBe(true)
    expect(none.element.dataset.density).toBe('compact')
    none.runtime.dispose()
  })
})

describe('the local appearance settings panel', () => {
  function show(runtime: AppearanceRuntime) {
    return render(<AppearanceSettings runtime={runtime} />)
  }

  it('presents the saved appearance with labelled, keyboard-operable controls', () => {
    const store = storageWith({ theme: 'dark', density: 'compact' })
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)

    expect(screen.getByRole('region', { name: '本地外观' })).toBeTruthy()
    expect(checked('深色')).toBe(true)
    expect(checked('跟随系统')).toBe(false)
    expect(checked('紧凑')).toBe(true)
    expect(checked('标准')).toBe(false)

    expect(screen.queryByRole('slider', { name: 'AI 侧栏宽度' })).toBeNull()
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
    expect(store.stored()).toEqual({ theme: 'dark', density: 'normal' })

    await user.click(screen.getByRole('radio', { name: '紧凑' }))
    expect(root.dataset.density).toBe('compact')
    runtime.dispose()

    const reloaded = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    expect(reloaded.get()).toEqual({ theme: 'dark', density: 'compact' })
    expect(root.dataset.theme).toBe('dark')
    expect(root.dataset.density).toBe('compact')
    reloaded.dispose()
  })

  it('ignores an obsolete saved width without discarding theme or density', async () => {
    const user = userEvent.setup()
    const legacy = { theme: 'dark', density: 'compact', aiWidth: 480 }
    const store = storageWith(legacy)
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)
    expect(runtime.get()).toEqual({ theme: 'dark', density: 'compact' })
    expect(root.dataset.theme).toBe('dark')
    expect(root.dataset.density).toBe('compact')
    expect(root.style.getPropertyValue('--quine-ai-width')).toBe('')
    expect(screen.queryByRole('slider')).toBeNull()
    expect(screen.queryByText('AI 侧栏宽度')).toBeNull()
    expect(store.stored()).toEqual(legacy)
    expect(store.writes).toBe(0)
    await user.click(screen.getByRole('radio', { name: '浅色' }))
    expect(store.stored()).toEqual({ theme: 'light', density: 'compact' })
    runtime.dispose()
  })

  it('restores every default from the one explicit action', async () => {
    const user = userEvent.setup()
    const store = storageWith({ theme: 'dark', density: 'compact' })
    const runtime = createAppearanceRuntime({ storage: store, systemQuery: null, root })
    show(runtime)
    expect(root.dataset.theme).toBe('dark')

    await user.click(screen.getByRole('button', { name: '恢复默认' }))

    expect(runtime.get()).toEqual(APPEARANCE_DEFAULTS)
    expect(root.dataset.theme).toBe('light')
    expect(root.dataset.density).toBe('normal')
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
    expect(store.stored()).toEqual({ theme: 'dark', density: 'normal' })
    saving.dispose()
  })
})
