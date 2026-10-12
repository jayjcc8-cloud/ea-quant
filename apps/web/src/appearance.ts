/**
 * QUINE local appearance: one small preference document, applied to the document
 * root and kept in this browser's own local storage.
 *
 * The preferences are a theme and density, carried as `data-theme` and
 * `data-density` on the root element. Stylesheets read those attributes, and
 * nothing has to be threaded through the component tree.
 *
 * The rules this module is the authority for:
 *
 *   · Every stored value is validated field by field. An unknown enum member
 *     falls back to that field's default. Obsolete fields, including aiWidth,
 *     are ignored; valid theme and density preferences are retained.
 *   · Storage is untrusted: a read or a write that throws is contained here. A
 *     refused write is reported as `false` — unsaved — and never as an exception,
 *     because a preference that did not persist must not take the app down with it.
 *   · `system` is the only theme that consults the OS colour scheme, and it holds
 *     exactly one media-query listener while it does. Leaving system mode, or
 *     disposing the runtime, releases it, so the OS is never consulted behind an
 *     explicit choice and no listener accumulates. Nothing here changes an OS
 *     setting: this module only reads the scheme it is told about.
 */

export type ThemePreference = 'light' | 'dark' | 'system'
export type DensityPreference = 'normal' | 'compact'

export type AppearancePreferences = {
  readonly theme: ThemePreference
  readonly density: DensityPreference
}

/** The two storage calls this module makes; `localStorage` satisfies it as-is. */
export type PreferenceStorage = {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/**
 * The media query this module watches, narrowed to the one event it uses. A
 * `MediaQueryList` is adapted to it in `defaultSystemQuery`.
 */
export type MediaQueryLike = {
  readonly matches: boolean
  addEventListener?(type: 'change', listener: (event: { matches: boolean }) => void): void
  removeEventListener?(type: 'change', listener: (event: { matches: boolean }) => void): void
  addListener?(listener: (event: { matches: boolean }) => void): void
  removeListener?(listener: (event: { matches: boolean }) => void): void
}

export const APPEARANCE_STORAGE_KEY = 'ea.quine.appearance.v1'
export const APPEARANCE_DEFAULTS: AppearancePreferences = { theme: 'system', density: 'normal' }

const SYSTEM_SCHEME_QUERY = '(prefers-color-scheme: dark)'

const isTheme = (value: unknown): value is ThemePreference =>
  value === 'light' || value === 'dark' || value === 'system'
const isDensity = (value: unknown): value is DensityPreference =>
  value === 'normal' || value === 'compact'
/** One stored document, validated field by field against the defaults. */
export function normalizeAppearance(value: unknown): AppearancePreferences {
  const record: Record<string, unknown> = typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    theme: isTheme(record.theme) ? record.theme : APPEARANCE_DEFAULTS.theme,
    density: isDensity(record.density) ? record.density : APPEARANCE_DEFAULTS.density,
  }
}

/** This browser's own store, or nothing when it is unavailable or refused. */
function defaultStorage(): PreferenceStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    // Reading the property itself can throw when storage is blocked.
    return null
  }
}

function defaultSystemQuery(): MediaQueryLike | null {
  try {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
    // The DOM's MediaQueryList carries these two methods with a narrower event
    // type; this is the one place the two shapes meet, and it is only ever used
    // through MediaQueryLike from here on.
    return window.matchMedia(SYSTEM_SCHEME_QUERY) as unknown as MediaQueryLike
  } catch {
    return null
  }
}

/** The saved appearance, or the defaults for anything absent or unreadable. */
export function readAppearance(storage: PreferenceStorage | null = defaultStorage()): AppearancePreferences {
  if (!storage) return APPEARANCE_DEFAULTS
  try {
    const raw = storage.getItem(APPEARANCE_STORAGE_KEY)
    if (!raw) return APPEARANCE_DEFAULTS
    return normalizeAppearance(JSON.parse(raw))
  } catch {
    return APPEARANCE_DEFAULTS
  }
}

/** True when the document was stored; false — never an exception — when it was not. */
export function writeAppearance(
  preferences: AppearancePreferences,
  storage: PreferenceStorage | null = defaultStorage(),
): boolean {
  if (!storage) return false
  try {
    storage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(preferences))
    return true
  } catch {
    return false
  }
}

/**
 * Apply one preference document to `root` and report the theme actually in force.
 * `systemDark` is the OS scheme as observed; it is read only by the system theme.
 */
export function applyAppearance(
  preferences: AppearancePreferences,
  root: HTMLElement = document.documentElement,
  systemDark = false,
): 'light' | 'dark' {
  const theme = preferences.theme === 'system' ? (systemDark ? 'dark' : 'light') : preferences.theme
  root.dataset.theme = theme
  root.dataset.density = preferences.density
  return theme
}

/** Subscribe to one media query, tolerating either listener API. */
export function watchSystemScheme(query: MediaQueryLike, listener: (dark: boolean) => void): () => void {
  const onChange = (event: { matches: boolean }) => listener(event.matches)
  if (query.addEventListener) {
    query.addEventListener('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }
  if (query.addListener) {
    query.addListener(onChange)
    return () => query.removeListener?.(onChange)
  }
  return () => {}
}

export type AppearanceRuntime = {
  get(): AppearancePreferences
  /** Apply, persist and announce; the return value is whether it was saved. */
  set(preferences: AppearancePreferences): boolean
  subscribe(listener: (preferences: AppearancePreferences) => void): () => void
  /** Release the OS listener and drop every subscriber. */
  dispose(): void
}

/**
 * One appearance for the whole app: it applies what it holds on creation, keeps the
 * document in step with every change, and holds the single OS listener that system
 * mode needs. It is created once by the entry module and lives as long as the page.
 */
export function createAppearanceRuntime(options: {
  storage?: PreferenceStorage | null
  systemQuery?: MediaQueryLike | null
  root?: HTMLElement
} = {}): AppearanceRuntime {
  const storage = options.storage === undefined ? defaultStorage() : options.storage
  const systemQuery = options.systemQuery === undefined ? defaultSystemQuery() : options.systemQuery
  const root = options.root ?? document.documentElement
  const listeners = new Set<(preferences: AppearancePreferences) => void>()
  let preferences = readAppearance(storage)
  let releaseSystem: (() => void) | null = null
  let disposed = false

  const systemDark = () => systemQuery?.matches === true

  const announce = () => {
    applyAppearance(preferences, root, systemDark())
    for (const listener of [...listeners]) listener(preferences)
  }

  // Exactly one listener: attached while the theme follows the system, released the
  // moment it does not, so an explicit choice is never overridden by the OS.
  const followSystem = () => {
    releaseSystem?.()
    releaseSystem = null
    if (disposed || preferences.theme !== 'system' || !systemQuery) return
    releaseSystem = watchSystemScheme(systemQuery, () => announce())
  }

  const runtime: AppearanceRuntime = {
    get: () => preferences,
    set(next) {
      preferences = normalizeAppearance(next)
      const saved = writeAppearance(preferences, storage)
      announce()
      followSystem()
      return saved
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    dispose() {
      disposed = true
      releaseSystem?.()
      releaseSystem = null
      listeners.clear()
    },
  }

  announce()
  followSystem()
  return runtime
}

let runtime: AppearanceRuntime | null = null

/** The one runtime of this page, created on first use. */
export function getAppearanceRuntime(): AppearanceRuntime {
  runtime ??= createAppearanceRuntime()
  return runtime
}

/** Apply the saved appearance before the first paint; the entry module calls this. */
export function startAppearance(): AppearanceRuntime {
  return getAppearanceRuntime()
}
