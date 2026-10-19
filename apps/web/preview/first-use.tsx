/**
 * The guided-upload first-use preview: the real panel, scripted answers.
 *
 * This page mounts the *unchanged* application over the in-memory adapter in
 * `./first-use-fixtures`. It fetches nothing — a `/api/` request throws here
 * rather than leaving the page — starts no engine, reads no store and makes no
 * model call. Every document on screen is a hand-written synthetic fixture, and
 * the bar this page renders above the application says so.
 *
 * Open it on the preview's own Vite server (its root is `apps/web/preview`, which
 * sets no proxy, so nothing here can reach an installed service):
 *
 *     (cd apps/web && npx vite --config preview/vite.config.ts --port 8901)
 *     http://127.0.0.1:8901/first-use.html?state=guided
 *
 * `state` names one entry of `FIRST_USE_STATES`; `width` caps the capture width
 * (use `width=390` for the narrow capture). Both are optional, and both controls
 * in the banner navigate on this page, so the labels stay on screen while the
 * state changes. This page is not part of the shipped `dist`: the production build
 * starts at `index.html`, and nothing in `src` imports this file.
 */
import { createRoot } from 'react-dom/client'
import { App } from '../src/app'
import { FIRST_USE_STATES, firstUseStateFor, scriptedAdapter } from './first-use-fixtures'
// @ts-expect-error Vite resolves stylesheet assets at build time.
import '../src/styles.css'

/** Refuse a same-origin API call before it leaves the page. */
function sealNetwork(): void {
  const realFetch = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (url.includes('/api/')) {
      throw new Error(`the scripted preview never performs a network request (${url})`)
    }
    return realFetch(input, init)
  }
}

/**
 * This page, which is where every control in the banner navigates to.
 *
 * The application itself is mounted on `/resources/data`, so setting only the search
 * there would leave the harness and load the ordinary preview instead.
 */
const PAGE = '/first-use.html'

/** Write one control's value the way a real edit does, so React sees it. */
function setNativeValue(element: HTMLElement, value: string): void {
  const prototype =
    element instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(element, value)
  element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
}

/**
 * The one label every capture carries: a small scripted-preview line above the
 * application. It is deliberately not sticky — a fixed bar takes the top of a
 * narrow viewport — so it scrolls away with the page and stays out of the panel.
 */
function chrome(stateId: string, width: number | null, state: ReturnType<typeof firstUseStateFor>): HTMLElement {
  const bar = document.createElement('div')
  bar.setAttribute('data-scripted-preview', 'true')
  Object.assign(bar.style, {
    padding: '6px 10px',
    background: '#3b2f00', color: '#ffe9a8', font: '12px/1.45 ui-monospace, monospace',
    borderBottom: '1px solid #7a5c00', display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center',
  })

  const label = document.createElement('strong')
  label.textContent = 'SCRIPTED PREVIEW · synthetic fixtures · in-memory adapter · no engine, no network, no model call'
  bar.append(label)

  /** Both controls below stay on this page; only the query changes. */
  const go = (nextState: string, nextWidth: number | null) => {
    window.location.assign(`${PAGE}?state=${nextState}${nextWidth ? `&width=${nextWidth}` : ''}`)
  }

  const picker = document.createElement('select')
  picker.setAttribute('aria-label', 'Preview state')
  for (const item of FIRST_USE_STATES) {
    const option = document.createElement('option')
    option.value = item.id
    option.textContent = item.title
    option.selected = item.id === stateId
    picker.append(option)
  }
  picker.onchange = () => { go(picker.value, width) }
  bar.append(picker)

  for (const pixels of [390, 1280]) {
    const button = document.createElement('button')
    button.type = 'button'
    button.textContent = `${pixels}px`
    button.onclick = () => { go(stateId, pixels) }
    bar.append(button)
  }

  // One click puts the panel where this state is about: the source in the CSV
  // box and the mapped editor open. Nothing else is filled in — every choice,
  // the zone and the forms are the user's, on purpose.
  const fill = document.createElement('button')
  fill.type = 'button'
  fill.textContent = 'Load this state’s CSV and open the mapped editor'
  fill.onclick = () => {
    const mode = document.getElementById('upload-mode')
    const content = document.getElementById('upload-content')
    if (mode) setNativeValue(mode, 'explicit-mapping')
    if (content) setNativeValue(content, state.csv)
  }
  bar.append(fill)

  const note = document.createElement('span')
  note.textContent = state.note
  bar.append(note)
  return bar
}

function mount(): void {
  sealNetwork()
  const parameters = new URLSearchParams(window.location.search)
  const state = firstUseStateFor(parameters.get('state'))
  const width = Number(parameters.get('width')) || null

  // The application reads the location it is mounted at, so this preview always
  // opens on the data resources section, where the upload panel lives.
  window.history.pushState({}, '', '/resources/data')

  const shell = document.createElement('div')
  document.body.replaceChildren(shell)
  shell.append(chrome(state.id, width, state))

  const viewport = document.createElement('div')
  if (width) {
    // A capture width is a ceiling, not a fixed box: a 390px capture still has to
    // fit the window it is opened in, so the wrapper is capped at that width and
    // is otherwise as wide as there is room for. `border-box` keeps the dashed
    // outline inside the cap rather than 2px past it.
    Object.assign(viewport.style, {
      width: '100%', maxWidth: `${width}px`, margin: '0 auto',
      border: '1px dashed #7a5c00', boxSizing: 'border-box',
    })
  }
  shell.append(viewport)

  const root = document.createElement('div')
  viewport.append(root)
  createRoot(root).render(<App api={scriptedAdapter({ preview: state.preview })} />)
}

mount()
