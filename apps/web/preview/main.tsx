/**
 * The isolated UI preview.
 *
 * It renders the real `App` over an in-memory `ApiAdapter` and nothing else: no
 * service, no engine, no model, no network request and no proxy of any kind. The
 * seam the production client uses for tests is the seam this page uses, so what a
 * reviewer sees here is the shipped component tree, not a copy of it. Every
 * document it serves comes from `./fixtures`, which marks each one as copied from
 * the observed local report or scripted.
 *
 * StrictMode is deliberately not used: this page exists to show how many times
 * each seam is actually called, and the double render would double the reads.
 */
import { useCallback, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { App, type ApiAdapter, type BacktestParameters } from '../src/app'
import { ResearchRequestError, type PluginSelection, type ResearchTask } from '../src/research-api'
import '../src/styles.css'
import './preview.css'
import {
  STATES, analytics, batch, cancelTask, equityPath, holdout, job, report, scenario, settings,
  type PreviewState,
} from './fixtures'

/** Every seam this page can be asked for, counted by name. */
const COUNTERS = [
  'settingsStatus', 'listResearchTasks', 'getResearchTask', 'listScenarios', 'validateScenario',
  'createResearchTask', 'confirmResearchTask', 'retryResearchTask', 'cancelResearchTask',
  'interpretResearchTask', 'createBacktest', 'listBacktests', 'getBacktest', 'getReport',
  'getEquityPath', 'getTradeAnalytics', 'createBatch', 'getBatch', 'holdoutScenarios',
  'createHoldout', 'getHoldout', 'listHoldouts',
] as const

/** A short read round trip, and a longer write one so a busy control is visible. */
const READ_MS = 240
const WRITE_MS = 800
const wait = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

/** The scripted failures this page can be told to stage. */
type Flags = { createFailure: boolean; cancelFailure: boolean }

/**
 * One stable controller for those switches.
 *
 * The value lives in this factory's closure rather than in a component field, so
 * there is no mutable React value anywhere: the adapter is handed `read` and can
 * only look at the switches from inside one of its own calls, while a render
 * reads nothing at all. `update` replaces the value and hands it back, so the
 * caller can mirror it for rendering without the mirror being the source.
 */
function createFlagsController(initial: Flags) {
  let value: Flags = initial
  return {
    read: () => value,
    update: (next: Partial<Flags>) => {
      value = { ...value, ...next }
      return value
    },
  }
}

/** Where the chosen scenario is remembered, so a reload lands back on it. */
const STORAGE_KEY = 'ea.preview.state'
const remember = (id: string) => { try { window.sessionStorage.setItem(STORAGE_KEY, id) } catch { /* a private window just forgets */ } }
const remembered = () => { try { return window.sessionStorage.getItem(STORAGE_KEY) } catch { return null } }

/**
 * The scenario this load belongs to: the `?state=` query first, then a remembered
 * scenario that already matches the path, then the path itself, then the first.
 *
 * The query and the path are written together whenever a scenario is chosen, so a
 * reload of any scenario — including a report URL — stays where it was instead of
 * being sent home.
 */
function resolveIndex(): number {
  const wanted = new URLSearchParams(window.location.search).get('state') ?? remembered()
  const byId = STATES.findIndex(item => item.id === wanted)
  if (byId >= 0 && (new URLSearchParams(window.location.search).get('state') || STATES[byId].path === window.location.pathname)) return byId
  const byPath = STATES.findIndex(item => item.path === window.location.pathname)
  return byPath >= 0 ? byPath : 0
}

/**
 * The whole backend of this page: one mutable task document per preview state.
 *
 * Reads never write. Create, confirm, retry, cancel and interpret are the only
 * seams that move the document, and each is recorded by name before it settles.
 * The one semantic counter beside them is the number of real backtest dispatches
 * the research workspace caused: a confirm or a retry only counts when the
 * document it returns has a run bound to it that the previous one did not, so a
 * retried model step and a published conclusion both count zero.
 */
function previewApi(
  state: PreviewState,
  record: (name: string) => void,
  flags: () => Flags,
  note: (text: string) => void,
): ApiAdapter {
  let current = state.task
  let created: ResearchTask | null = null
  const list = state.recent ?? [state.task]
  const read = async <T,>(name: string, value: T): Promise<T> => {
    record(name)
    await wait(READ_MS)
    return value
  }
  const write = async <T,>(name: string, value: T): Promise<T> => {
    record(name)
    await wait(WRITE_MS)
    return value
  }
  /** A dispatch is only ever counted from the documents themselves. */
  const adopt = (next: ResearchTask, what: string) => {
    if (next.jobs.length > current.jobs.length) record('backtestDispatch')
    // A lifecycle document belongs to the task this route is on. The fixtures for
    // confirm, retry and interpret are built from the observed record and carry
    // its id, so adopting that id verbatim would leave the route pointing at a
    // task the next read cannot find: the list snapshot would come back and undo
    // the write the reader just made. The routed id is preserved instead, and the
    // job, run and report identities inside the document are served unchanged.
    current = { ...next, task_id: current.task_id }
    note(what)
    return current
  }
  /** The persisted list, with this adapter's live documents in place of its snapshot. */
  const persisted = () => {
    const live = created
    const items = list.map(item => item.task_id === current.task_id ? current : item)
    return live && !items.some(item => item.task_id === live.task_id) ? [live, ...items] : items
  }
  /** A document for an id this page was never told about keeps that id, not another task's. */
  const known = (taskId: string) =>
    [current, created].find(item => item?.task_id === taskId)
    ?? list.find(item => item.task_id === taskId)
    ?? { ...current, task_id: taskId }
  return {
    holdoutScenarios: () => read('holdoutScenarios', [scenario]),
    createHoldout: () => read('createHoldout', holdout),
    getHoldout: () => read('getHoldout', holdout),
    listHoldouts: () => read('listHoldouts', [holdout]),
    listScenarios: () => read('listScenarios', [scenario]),
    // Scripted input validation only: no request leaves this page and no backtest
    // is dispatched by validating. Initial cash is held to the one rule this form
    // can check here — a decimal string naming a positive amount — and anything
    // else is refused with the same sentence the host uses, so the product's own
    // alert and its focus move are what a reviewer sees. A valid request is
    // answered with this scenario carrying the cash and parameters that were sent.
    validateScenario: async (scenarioId: string, parameters: BacktestParameters) => {
      record('validateScenario')
      await wait(READ_MS)
      const cash = parameters.initial_cash.trim()
      if (!/^\d+(\.\d+)?$/.test(cash) || Number(cash) <= 0) throw new Error('Initial cash must be a positive decimal amount.')
      return {
        ...scenario,
        scenario_id: scenarioId,
        summary: {
          ...scenario.summary!,
          initial_cash: cash,
          parameters: parameters.strategy_parameters ?? scenario.summary!.parameters,
        },
      }
    },
    createBacktest: () => write('createBacktest', job),
    createBatch: () => write('createBatch', batch),
    getBatch: () => read('getBatch', batch),
    listBacktests: () => read('listBacktests', [job]),
    getBacktest: async jobId => read('getBacktest', jobId === job.job_id ? job : { ...job, job_id: jobId }),
    getReport: () => read('getReport', report),
    getEquityPath: () => read('getEquityPath', equityPath),
    getTradeAnalytics: () => read('getTradeAnalytics', analytics),
    // Downloads stay inside this page: the fragment makes no request at all.
    artifactUrl: (jobId, name) => `#preview-artifact/${jobId}/${name}`,
    settingsStatus: () => read('settingsStatus', settings),
    listResearchTasks: () => read('listResearchTasks', persisted()),
    // The live document wins over the list snapshot, so confirming, retrying or
    // cancelling here can never be reverted by the next read.
    getResearchTask: async taskId => read('getResearchTask', known(taskId)),
    createResearchTask: async (ideaText: string, selection?: PluginSelection) => {
      record('createResearchTask')
      const said = selection ? `${Object.keys(selection).length} capability layer(s) selected` : 'no capability selection'
      await wait(WRITE_MS)
      if (flags().createFailure) throw new ResearchRequestError('research_task_failed', 'scripted preview failure: the draft was refused')
      const fresh = state.created ?? { ...state.task, task_id: 'scripted-task-created' }
      created = { ...fresh, raw_idea: ideaText, status: 'drafting', operation_kind: 'propose', spec_draft: null, jobs: [], interpretation: null, interpretation_error: null, error: null, cancel_note: null, usage: { ...fresh.usage, model_calls: 1, backtests: 0 } }
      current = created
      note(`createResearchTask accepted ${ideaText.length} characters of idea with ${said}`)
      return created
    },
    confirmResearchTask: async () => {
      record('confirmResearchTask')
      await wait(WRITE_MS)
      if (!state.confirmed) throw new ResearchRequestError('research_task_state', 'scripted preview: this state has nothing to confirm')
      return adopt(state.confirmed, 'confirmResearchTask accepted; the returned document is what this page now serves')
    },
    retryResearchTask: async () => {
      record('retryResearchTask')
      await wait(WRITE_MS)
      if (!state.retried) throw new ResearchRequestError('research_task_state', 'scripted preview: this state has nothing to retry')
      return adopt(state.retried, state.retryDispatchesBacktest
        ? 'retryResearchTask re-dispatched one backtest for this scripted state'
        : 'retryResearchTask resumed the model step; no backtest was dispatched')
    },
    cancelResearchTask: async () => {
      record('cancelResearchTask')
      await wait(WRITE_MS)
      if (flags().cancelFailure) throw new ResearchRequestError('research_task_state', 'scripted preview failure: the cancel was refused')
      return adopt(cancelTask(current), 'cancelResearchTask recorded a terminal cancel; the run and its report are unchanged')
    },
    interpretResearchTask: async () => {
      record('interpretResearchTask')
      await wait(WRITE_MS)
      if (state.interpretError) throw new ResearchRequestError(state.interpretError.code, state.interpretError.message)
      // A conclusion publishes text about the run that already exists; it never
      // dispatches anything, which is why this path moves no dispatch counter.
      return adopt(state.concluded ?? current, 'interpretResearchTask published the host conclusion for the bound run')
    },
  }
}

/**
 * The app under test, with the adapter that serves it.
 *
 * The adapter holds the one mutable task document this page serves, so it is
 * built once per scenario body: rebuilding it on a render would silently undo a
 * write. `epoch` is applied to this component's key by its parent instead, so a
 * reset remounts the body and the app meets the document as it was served.
 */
function ScenarioBody({ state, record, flags, note }: {
  state: PreviewState
  record: (name: string) => void
  flags: () => Flags
  note: (text: string) => void
}) {
  const api = useMemo(() => previewApi(state, record, flags, note), [state, record, flags, note])
  return <App api={api} />
}

function Preview() {
  const [selected, setSelected] = useState(resolveIndex)
  const [epoch, setEpoch] = useState(0)
  const [calls, setCalls] = useState<Record<string, number>>({})
  const [note, setNote] = useState('No write has been made since this scenario was opened.')
  // The staged failures live in one controller created once, so the adapter reads
  // the switch as it stands when a call is made and is never rebuilt when a switch
  // is flipped. `staged` mirrors it for rendering only.
  const [controller] = useState(() => createFlagsController({ createFailure: false, cancelFailure: false }))
  const [staged, setStaged] = useState<Flags>({ createFailure: false, cancelFailure: false })
  const record = useCallback((name: string) => {
    setCalls(current => ({ ...current, [name]: (current[name] ?? 0) + 1 }))
  }, [])
  const state = STATES[selected]

  const stage = (next: Partial<Flags>) => setStaged(controller.update(next))
  /** Switching scenario is also a fresh route: the app is remounted at the new one. */
  const choose = (index: number) => {
    const next = STATES[index]
    window.history.replaceState({}, '', `${next.path}?state=${next.id}`)
    remember(next.id)
    setCalls({})
    setSelected(index)
  }
  /** One control that puts this scenario back exactly as it was served. */
  const reset = () => {
    setStaged(controller.update({ createFailure: false, cancelFailure: false }))
    setCalls({})
    setNote('No write has been made since this scenario was opened.')
    setEpoch(value => value + 1)
  }

  return <>
    {/* One default-closed summary, so this page's own chrome costs a phone very
        little of the first screen. The attribution sentence, every control, the
        counters and the notes that describe this page are all inside it. */}
    <div className="preview-bar">
      <details className="disclosure preview-controls">
        <summary><strong>Scripted preview · Controls</strong><span>{state.label}</span></summary>
        <div className="disclosure-body">
          <p className="preview-attribution">Scripted interactions; success financial snapshot from observed local report; no real API/model/engine calls.</p>
          <label htmlFor="preview-state">Previewed state</label>
          <select id="preview-state" value={selected} onChange={event => choose(Number(event.target.value))}>
            {STATES.map((item, index) => <option key={item.id} value={index}>{item.label}</option>)}
          </select>
          <p className="preview-note">
            Reads settle in {READ_MS} ms and writes in {WRITE_MS} ms, so a control really is disabled while its
            request is in flight and a second click on it does nothing. The chosen state is kept in
            <code> ?state=</code> and in this tab's session storage only, which is why a reload — including a
            reload of a report URL — lands back on the same state.
          </p>
          <div className="preview-staging">
            <label><input type="checkbox" checked={staged.createFailure} onChange={event => stage({ createFailure: event.target.checked })} />
              Make <code>createResearchTask</code> fail</label>
            <label><input type="checkbox" checked={staged.cancelFailure} onChange={event => stage({ cancelFailure: event.target.checked })} />
              Make <code>cancelResearchTask</code> fail</label>
            <button type="button" onClick={reset}>Reset this scenario</button>
          </div>
          <p className="preview-note">
            Unchecking a staged failure is the recovery: the same control then succeeds and the page shows the
            persisted document it reloaded. <code>Reset this scenario</code> clears both switches, the counters and
            every local task change.
          </p>
          <p className="preview-note">
            The backtest form&rsquo;s validation is scripted here too, and it is input validation only: a
            non-decimal, missing or non-positive initial cash is refused so the product&rsquo;s own alert appears,
            and a valid one is answered with the scenario the request named. Validating dispatches no backtest and
            spends no model call, so only <code>validateScenario</code> moves.
          </p>
          <p className="preview-note" aria-live="polite">{note}</p>
          <div className="preview-counters" aria-label="Adapter calls since this scenario was opened">
            {COUNTERS.map(name => <span key={name}><code>{name}</code> {calls[name] ?? 0}</span>)}
          </div>
          <div className="preview-counters" aria-label="Semantic counters since this scenario was opened">
            <span><code>backtestDispatch</code> {calls.backtestDispatch ?? 0}</span>
          </div>
          <p className="preview-note">
            Reading the page only ever moves the read counters. A confirm or a retry moves
            <code> backtestDispatch</code> only when the document it returns binds a run the previous one did not,
            so a retried model step, a refused request and a published conclusion all count zero — the conclusion
            never dispatches a backtest. This page runs no proxy and has no <code>fetch</code> fallback: the adapter
            above is the only source of every document, and every byte of it comes from
            <code> preview/fixtures.ts</code>.
          </p>
          <p className="preview-note">
            Surfaces this page does not serve, and therefore leaves as the shipped client leaves them: the
            capability catalog and recipe resolution, the draft-selection editor, declaration verification,
            controlled upload, and the local strategy slot. Nothing here fakes a host verification, and a control
            that needs one of those seams is simply absent rather than inert.
          </p>
        </div>
      </details>
    </div>
    <ScenarioBody key={`${state.id}-${epoch}`} state={state} record={record} flags={controller.read} note={setNote} />
  </>
}

const first = resolveIndex()
remember(STATES[first].id)
if (!new URLSearchParams(window.location.search).get('state')) {
  window.history.replaceState({}, '', `${STATES[first].path}?state=${STATES[first].id}`)
}
createRoot(document.getElementById('preview-root')!).render(<Preview />)
