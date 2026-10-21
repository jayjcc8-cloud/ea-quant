import { SourceLink as Link } from './workspace-navigation'
/**
 * QUINE 资源中心 — the workspace's directory of what this host has, and the one
 * place a reader can ask the host whether a composition of declared capabilities
 * would be authorized.
 *
 * Four distinctions the page is built around, because each of them is a place
 * where a research workspace can quietly lie:
 *
 * 1. A catalog entry the host can load is not a model this workspace has
 *    configured. `usable` is host admissibility; configuration, credential
 *    presence, connection and request-capability validation are separate facts,
 *    and nothing here has observed a connection at all. Which adapter a
 *    configuration belongs to is decided by the identity the host declared for
 *    that entry — never by its name.
 * 2. An empty directory of published datasets is not a scenario without data.
 *    Registered scenarios keep their own registered data and are listed apart.
 * 3. A source that failed to answer is unknown, never zero: it contributes no
 *    reference and is never reported as an empty one. The composition a task or
 *    a run holds is the composition its own record states — the current spec, not
 *    the selection the task was created with, and never one inferred from a name.
 * 4. A recipe verdict is a composition authorization check. It is not a
 *    connection, not a request validation and not a frozen run binding, and the
 *    page says so where the verdict is shown.
 *
 * The page writes nothing on its own: the single action it offers sends exactly
 * the selection on screen to the host's own resolve endpoint and prints the
 * host's answer. It saves no profile, applies nothing to a task, starts no run
 * and holds nothing beyond this render.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ApiAdapter, BacktestJob, ScenarioSummary } from './app'
import {
  LAYER_ORDER,
  type AvailabilityPolicy,
  type LayerName,
  type ModelConfigurationStatus,
  type PluginBinding,
  type PluginCatalogEntry,
  type PluginSelection,
  type RecipeResolution,
  type ResearchTask,
} from './research-api'
import { isIntentDraft, type UserStrategy } from './user-strategies-api'
import './resources.css'

const UNKNOWN = '未知'

/** The host's five binding layers, under the noun each one names in an identity. */
const IDENTITY_LAYER_NAMES: Record<LayerName, string> = {
  model: 'model',
  data: 'dataset',
  strategy: 'strategy',
  validator: 'validator',
  backtest: 'backtest',
}

const LAYER_TITLES: Record<LayerName, string> = {
  model: '模型',
  data: '数据',
  strategy: '策略',
  validator: '校验',
  backtest: '回测',
}

/** Which declared kinds a layer may bind. The host remains the authority. */
const LAYER_KINDS: Record<LayerName, string[]> = {
  model: ['model'],
  data: ['data'],
  strategy: ['strategy'],
  validator: ['validator'],
  backtest: ['backtest-method', 'backtest'],
}

/**
 * What one model entry can be said to be, in this workspace, right now. Nothing
 * between these three is a guess: an entry whose identity does not name a
 * provider cannot be attributed to the configured one, so it stays unknown.
 */
type ModelPresence = 'configured' | 'unconfigured' | 'unknown'

const PRESENCE_TEXT: Record<ModelPresence, string> = {
  configured: '已配置',
  unconfigured: '未配置',
  unknown: '配置未知',
}

/** One capability a reference observed, with the digest only a frozen match has. */
export type KnownReferenceLayer = {
  layer: string
  plugin_id: string
  package_version: string
  artifact_digest: string | null
}

export type KnownReferenceLink = { to: string; label: string }

/**
 * One run or one unfrozen selection, as the sources that were actually read
 * projected it. `unknown` names every identity dimension this read could not
 * observe; nothing here is inferred from a name, a symbol or the current model.
 * `default_route` states the one thing that is known rather than missing: the
 * record names no layer at all, so the host default route is what it holds.
 */
export type KnownReference = {
  key: string
  kind: 'draft' | 'frozen'
  job_id: string | null
  scenario_id: string | null
  dataset_id: string | null
  source_sha256: string | null
  layers: KnownReferenceLayer[]
  default_route: boolean
  /** Where this reference was observed. Associations, never a count of runs. */
  scope: string[]
  links: KnownReferenceLink[]
  unknown: string[]
}

export type KnownReferenceSources = {
  strategies?: UserStrategy[]
  tasks?: ResearchTask[]
  jobs?: BacktestJob[]
}

const UNKNOWN_SCENARIO = '场景'
const UNKNOWN_DATASET = '数据集'
const UNKNOWN_SOURCE = '来源摘要'
const UNKNOWN_PLUGIN = '插件标识'
const UNKNOWN_DIGEST = '插件摘要'

function push(list: string[], value: string) {
  if (!list.includes(value)) list.push(value)
}

function pushLink(reference: KnownReference, link: KnownReferenceLink) {
  if (!reference.links.some((existing) => existing.to === link.to)) reference.links.push(link)
}

function ensureReference(
  references: Map<string, KnownReference>,
  key: string,
  kind: 'draft' | 'frozen',
  jobId: string | null,
): KnownReference {
  const existing = references.get(key)
  if (existing) return existing
  const created: KnownReference = {
    key,
    kind,
    job_id: jobId,
    scenario_id: null,
    dataset_id: null,
    source_sha256: null,
    layers: [],
    default_route: false,
    scope: [],
    links: [],
    unknown: [],
  }
  // A run names itself; that is the one link every frozen reference carries.
  if (jobId) created.links.push({ to: `/backtests/${encodeURIComponent(jobId)}`, label: '运行' })
  references.set(key, created)
  return created
}

function bindingLayers(binding: PluginBinding | null | undefined): KnownReferenceLayer[] {
  return (binding?.components ?? []).map((component) => ({
    layer: component.layer,
    plugin_id: component.plugin_id,
    package_version: component.package_version,
    artifact_digest: component.artifact_digest,
  }))
}

/** A selection is a choice, not a freeze: it names versions and no digests. */
function selectionLayers(selection: PluginSelection): KnownReferenceLayer[] {
  return LAYER_ORDER.filter((layer) => selection[layer]).map((layer) => ({
    layer,
    plugin_id: selection[layer]!.plugin_id,
    package_version: selection[layer]!.package_version,
    artifact_digest: null,
  }))
}

/**
 * The composition one task currently holds, as the host itself will read it.
 *
 * `spec_draft.selection` is what an edit writes and what confirmation freezes
 * (`research.py`: `binding = None if spec.selection is None else …`), and an
 * explicitly cleared selection is persisted as its absence — the host attaches
 * the user's selection to the drafted spec when the spec is proposed, and never
 * copies a later edit back. So a spec that states no selection means the host
 * default route, and the selection recorded at creation must not be put back.
 * Only a task that has no spec at all is described by that creation-time record.
 */
function currentComposition(task: ResearchTask, spec: ResearchTask['spec_draft']): { selection: PluginSelection; stated: boolean } {
  if (spec) return { selection: spec.selection ?? {}, stated: true }
  return { selection: task.selection ?? {}, stated: task.selection !== null && task.selection !== undefined }
}

function identityOf(value: unknown): { dataset_id: string | null; source_sha256: string | null } {
  if (typeof value !== 'object' || value === null) return { dataset_id: null, source_sha256: null }
  const record = value as Record<string, unknown>
  return {
    dataset_id: typeof record.dataset_id === 'string' ? record.dataset_id : null,
    source_sha256: typeof record.source_sha256 === 'string' ? record.source_sha256 : null,
  }
}

function fillIdentity(
  reference: KnownReference,
  identity: { scenario_id?: string | null; dataset_id?: string | null; source_sha256?: string | null },
) {
  if (!reference.scenario_id && identity.scenario_id) reference.scenario_id = identity.scenario_id
  if (!reference.dataset_id && identity.dataset_id) reference.dataset_id = identity.dataset_id
  if (!reference.source_sha256 && identity.source_sha256) reference.source_sha256 = identity.source_sha256
}

function finalize(reference: KnownReference): KnownReference {
  const unknown: string[] = []
  if (!reference.scenario_id) unknown.push(UNKNOWN_SCENARIO)
  if (!reference.dataset_id) unknown.push(UNKNOWN_DATASET)
  if (!reference.source_sha256) unknown.push(UNKNOWN_SOURCE)
  // A record that states no layer at all is on the host default route: that is
  // an observed composition, not an unobserved one.
  if (!reference.layers.length && !reference.default_route) unknown.push(UNKNOWN_PLUGIN)
  else if (reference.layers.some((layer) => !layer.artifact_digest)) unknown.push(UNKNOWN_DIGEST)
  return { ...reference, unknown }
}

/**
 * Every reference the three read sources can be observed to hold, deduplicated by
 * identity: one run named by a strategy experiment, by a task and by the job index
 * is one reference with three associations, not three runs.
 *
 * A source that was not read is absent from the argument and contributes nothing;
 * a reference it alone would have named is never invented, and a task that names a
 * run this read cannot see keeps the association and states every identity it does
 * not have.
 */
export function deriveKnownReferences(sources: KnownReferenceSources): KnownReference[] {
  const references = new Map<string, KnownReference>()

  // The job index: what each recorded run itself froze.
  for (const job of sources.jobs ?? []) {
    const reference = ensureReference(references, `job:${job.job_id}`, 'frozen', job.job_id)
    push(reference.scope, '作业记录')
    if (job.scenario_id) fillIdentity(reference, { scenario_id: job.scenario_id })
    const identity = job.input_snapshot?.research_input ?? job.input_identity
    fillIdentity(reference, { dataset_id: identity?.dataset_id, source_sha256: identity?.source_sha256 })
    if (job.input_snapshot) push(reference.scope, '作业输入快照')
    if (job.plugin_binding) {
      if (!reference.layers.length) reference.layers = bindingLayers(job.plugin_binding)
      push(reference.scope, '作业插件绑定')
    }
    if (job.dataset_version) {
      fillIdentity(reference, { dataset_id: job.dataset_version.dataset_id })
      push(reference.scope, '作业数据集版本')
    }
  }

  // Research tasks: the frozen spec, the pinned binding and the job associations.
  for (const task of sources.tasks ?? []) {
    const link = { to: `/research/${encodeURIComponent(task.task_id)}`, label: '研究' }
    const confirmed = task.spec_confirmed ?? []
    const spec = confirmed.length ? confirmed[confirmed.length - 1].spec : task.spec_draft
    const specScope = confirmed.length ? '任务冻结规范' : '任务草稿规范'
    if (task.jobs?.length) {
      for (const jobId of task.jobs) {
        const reference = ensureReference(references, `job:${jobId}`, 'frozen', jobId)
        push(reference.scope, '任务作业')
        pushLink(reference, link)
        if (spec) {
          fillIdentity(reference, spec)
          push(reference.scope, specScope)
        }
        if (task.pinned_binding) {
          if (!reference.layers.length) reference.layers = bindingLayers(task.pinned_binding)
          push(reference.scope, '任务冻结绑定')
        }
      }
      continue
    }
    if (task.pinned_binding) {
      // Confirmed and pinned before any run was recorded — or after a run that
      // never reached the index. The binding is an immutable historical identity
      // of its own; it gets no invented run to belong to.
      const reference = ensureReference(references, `task:${task.task_id}`, 'frozen', null)
      reference.layers = bindingLayers(task.pinned_binding)
      push(reference.scope, '任务冻结绑定')
      if (spec) {
        fillIdentity(reference, spec)
        push(reference.scope, specScope)
      }
      pushLink(reference, link)
      continue
    }
    const composition = currentComposition(task, spec)
    if (!spec && !composition.stated) continue
    const reference = ensureReference(references, `selection:${task.task_id}`, 'draft', null)
    if (spec) {
      fillIdentity(reference, spec)
      push(reference.scope, specScope)
    }
    reference.layers = selectionLayers(composition.selection)
    reference.default_route = composition.stated && !reference.layers.length
    push(reference.scope, '任务选择')
    pushLink(reference, link)
  }

  // User strategies: the draft the user is editing, and the payload each
  // experiment froze. A saved intent is not a draft this page can name: it holds
  // no scenario, dataset or source at all, so it contributes no reference rather
  // than one whose every identity this page would have to report as unknown.
  for (const strategy of sources.strategies ?? []) {
    const link = { to: `/strategies/${encodeURIComponent(strategy.user_strategy_id)}`, label: '策略' }
    const draft = strategy.draft
    if (draft && !isIntentDraft(draft)) {
      const reference = ensureReference(references, `draft:${strategy.user_strategy_id}:${draft.revision}`, 'draft', null)
      fillIdentity(reference, draft)
      push(reference.scope, '策略草稿')
      pushLink(reference, link)
    }
    for (const experiment of strategy.experiments ?? []) {
      if (!experiment.job_id) continue
      const reference = ensureReference(references, `job:${experiment.job_id}`, 'frozen', experiment.job_id)
      fillIdentity(reference, {
        scenario_id: experiment.payload?.scenario_id,
        ...identityOf(experiment.payload?.input_identity),
      })
      push(reference.scope, '策略实验冻结载荷')
      pushLink(reference, link)
    }
  }

  return [...references.values()].map(finalize)
}

/** One read of one source: it is loading, it answered, it failed, or it is absent. */
type Load<T> =
  | { state: 'loading' }
  | { state: 'ready'; value: T }
  | { state: 'failed'; message: string }
  | { state: 'absent' }

function messageOf(caught: unknown): string {
  if (caught instanceof Error && caught.message) return caught.message
  if (caught === null || caught === undefined) return '读取失败'
  return String(caught)
}

function codeOf(caught: unknown): string {
  if (typeof caught === 'object' && caught !== null && 'code' in caught) {
    const code = (caught as { code?: unknown }).code
    if (typeof code === 'string' && code) return code
  }
  return 'request_failed'
}

/**
 * One independent source read. What is on screen is derived from the answer this
 * attempt produced, never written into state by the effect itself: a read that
 * has not answered yet is loading, a reload is loading again, and a reply that
 * arrives after a reload, after an explicit refresh or after the page is gone is
 * dropped rather than shown over a newer answer.
 */
function useRead<T>(read: (() => Promise<T>) | undefined): [Load<T>, () => void] {
  const [completed, setCompleted] = useState<{ attempt: number; load: Load<T> } | null>(null)
  const [attempt, setAttempt] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    if (!read) return
    const current = generation.current + 1
    generation.current = current
    Promise.resolve()
      .then(read)
      .then(
        (value) => { if (generation.current === current) setCompleted({ attempt, load: { state: 'ready', value } }) },
        (caught: unknown) => {
          if (generation.current === current) setCompleted({ attempt, load: { state: 'failed', message: messageOf(caught) } })
        },
      )
    return () => { generation.current += 1 }
  }, [read, attempt])
  const reload = useCallback(() => setAttempt((value) => value + 1), [])
  const load: Load<T> = !read
    ? { state: 'absent' }
    : completed && completed.attempt === attempt
      ? completed.load
      : { state: 'loading' }
  return [load, reload]
}

const identityText = (identity: Record<string, unknown>) =>
  Object.entries(identity)
    .map(([key, value]) => `${key}: ${typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}`)
    .join(' · ') || '未记录'

const effectsText = (effects: PluginCatalogEntry['effects']) => {
  const part = (values: string[]) => (values.length ? values.join(' · ') : '无')
  return `声明 ${part(effects.declared)} · 授予 ${part(effects.granted)} · 主机授予 ${part(effects.host_granted)}`
}

const identityLabel = (layer: string) => IDENTITY_LAYER_NAMES[layer as LayerName] ?? layer

const availabilityText = (policy?: AvailabilityPolicy) =>
  !policy
    ? '未记录'
    : `${policy.id} v${policy.version} · ${policy.pit_verified ? '点位可用性已验证' : '点位可用性未验证'}`

const instrumentText = (item: { venue?: string; symbol?: string }) =>
  item.venue && item.symbol ? `${item.venue}:${item.symbol}` : '未记录'

const loadStateText = (load: Load<unknown>) => {
  if (load.state === 'ready') return '已读取'
  if (load.state === 'loading') return '读取中…'
  if (load.state === 'absent') return '此工作区不提供'
  return `不可用（${load.message}）`
}

const selectionText = (selection: PluginSelection | null) => {
  if (!selection) return '主机未返回选择身份'
  if (!Object.keys(selection).length) return '未选择任何层（主机默认路径）'
  return LAYER_ORDER.filter((layer) => selection[layer])
    .map((layer) => `${identityLabel(layer)}: ${selection[layer]!.plugin_id} ${selection[layer]!.package_version}`)
    .join(' · ')
}

/** The host string a fact has, or the reason there is none to show yet. */
const fromStatus = (load: Load<ModelConfigurationStatus>, value: (status: ModelConfigurationStatus) => string) =>
  load.state === 'ready' ? value(load.value) : load.state === 'loading' ? '读取中' : UNKNOWN

const textOrUnknown = (value: string | null | undefined) => (typeof value === 'string' && value ? value : UNKNOWN)
const yesNo = (value: boolean | undefined) => (value === true ? '是' : value === false ? '否' : UNKNOWN)
const present = (value: boolean | undefined) => (value === true ? '已提供' : value === false ? '未提供' : UNKNOWN)

/**
 * Which configured adapter one catalog entry is, if it can be attributed at all.
 * The comparison is the host's own identity field against the host's own settings
 * status; an entry that names no provider, or a workspace that names none, is
 * unknown rather than assumed.
 */
function modelPresence(entry: PluginCatalogEntry, settings: Load<ModelConfigurationStatus>): ModelPresence {
  if (settings.state !== 'ready') return 'unknown'
  const status = settings.value
  if (status.configured !== true) return 'unconfigured'
  const configured = textOrUnknown(status.provider)
  const declared = textOrUnknown(typeof entry.identity?.provider === 'string' ? entry.identity.provider : null)
  if (configured === UNKNOWN || declared === UNKNOWN) return 'unknown'
  if (configured !== declared) return 'unconfigured'
  const configuredProtocol = textOrUnknown(status.protocol)
  const declaredProtocol = textOrUnknown(typeof entry.identity?.protocol === 'string' ? entry.identity.protocol : null)
  if (configuredProtocol !== UNKNOWN && declaredProtocol !== UNKNOWN && configuredProtocol !== declaredProtocol) return 'unconfigured'
  return 'configured'
}

const optionValue = (entry: PluginCatalogEntry) => `${entry.plugin_id}@${entry.package_version}`

function SourceState({ name, label, load }: { name: string; label: string; load: Load<unknown> }) {
  const text = `${label}：${loadStateText(load)}`
  return load.state === 'failed'
    ? <li className="notice error" role="alert" data-ref={name}>{text}</li>
    : <li className="muted" data-ref={name}>{text}</li>
}

/** One declared capability: its host identity in one row, its evidence behind a
 * keyboard-reachable disclosure, and the host's own refusal reason always shown. */
function CatalogEntry({ entry, kind, presence }: { entry: PluginCatalogEntry; kind: 'model' | 'plugin'; presence: ModelPresence | null }) {
  return <li className="resource-entry" data-resource-entry={kind} data-id={entry.plugin_id}>
    <div className="resource-entry-head">
      <strong>{entry.plugin_id}</strong>
      <span className="resource-version">{entry.package_version}</span>
      <span className="resource-chip">{entry.kind}</span>
      <span className={`resource-chip ${entry.usable ? 'is-admissible' : 'is-refused'}`}>主机{entry.usable ? '可加载' : '拒绝'}</span>
      {presence && <span className={`resource-chip ${presence === 'configured' ? 'is-admissible' : ''}`}>
        {PRESENCE_TEXT[presence]}
      </span>}
    </div>
    {entry.reason && <p className="resource-refusal">{entry.reason.code} · {entry.reason.message}</p>}
    <details className="resource-details">
      <summary>摘要、加载点、效果与身份</summary>
      <dl className="resource-entry-facts">
        <div><dt>摘要</dt><dd>{entry.artifact_digest}</dd></div>
        <div><dt>加载点</dt><dd>{entry.entrypoint}</dd></div>
        <div><dt>效果</dt><dd>{effectsText(entry.effects)}</dd></div>
        <div><dt>身份</dt><dd>{identityText(entry.identity)}</dd></div>
      </dl>
    </details>
  </li>
}

function ReferenceItem({ reference }: { reference: KnownReference }) {
  // A draft holds versions that no digest has frozen yet — except one that states
  // no layer at all, which is already exactly what the host default route is.
  const unfrozen = reference.layers.some((layer) => !layer.artifact_digest)
    || (reference.kind === 'draft' && !reference.default_route)
  const heading = reference.job_id
    ? `运行 ${reference.job_id}`
    : reference.kind === 'frozen' ? '冻结绑定（没有对应的运行记录）' : '当前选择（未冻结）'
  return <li className="resource-reference" data-resource="reference" data-key={reference.key} data-kind={reference.kind}>
    <div className="resource-reference-head">
      <strong>{heading}</strong>
      <span className="resource-chip">{reference.kind === 'frozen' ? '历史冻结' : '草稿选择'}</span>
      {unfrozen && <span className="resource-chip is-unfrozen">摘要未冻结</span>}
      {reference.scenario_id && <span className="resource-version">场景 {reference.scenario_id}</span>}
      {reference.default_route && <span className="resource-version">主机默认路径</span>}
    </div>
    {reference.links.length > 0 && <p className="resource-reference-links">
      {reference.links.map((link) => <Link key={link.to} to={link.to}>{link.label}</Link>)}
    </p>}
    <details className="resource-details">
      <summary>身份与关联证据</summary>
      <dl className="resource-entry-facts">
        <div><dt>场景</dt><dd data-ref="scenario">{reference.scenario_id ?? UNKNOWN}</dd></div>
        <div><dt>数据集</dt><dd data-ref="dataset">{reference.dataset_id ?? UNKNOWN}</dd></div>
        <div><dt>来源摘要</dt><dd data-ref="source_sha256">{reference.source_sha256 ?? UNKNOWN}</dd></div>
      </dl>
      {reference.layers.length > 0 && <ul className="resource-layer-list">
        {reference.layers.map((layer) => <li key={layer.layer} data-ref="layer">
          {identityLabel(layer.layer)}: {layer.plugin_id} {layer.package_version} · {layer.artifact_digest ?? '摘要未冻结'}
        </li>)}
      </ul>}
      {reference.default_route && <p className="muted" data-ref="default-route">记录的这一份组合没有指定任何层：它走主机默认路径。</p>}
      {reference.unknown.length > 0 && <p className="muted" data-ref="unknown">{UNKNOWN}：{reference.unknown.join(' · ')}</p>}
      {reference.scope.length > 0 && <p className="muted">观察位置：{reference.scope.join(' · ')}</p>}
    </details>
  </li>
}

export function ResourcesPage({ api }: { readonly api: ApiAdapter }) {
  const [settings, reloadSettings] = useRead(api.settingsStatus)
  const [catalog, reloadCatalog] = useRead(api.pluginCatalog)
  const [datasets, reloadDatasets] = useRead(api.listDatasets)
  const [scenarios, reloadScenarios] = useRead(api.listScenarios)
  const [strategies, reloadStrategies] = useRead(api.listUserStrategies)
  const [tasks, reloadTasks] = useRead(api.listResearchTasks)
  const [jobs, reloadJobs] = useRead(api.listBacktests)

  const [query, setQuery] = useState('')
  const [selection, setSelection] = useState<PluginSelection>({})
  const [verdict, setVerdict] = useState<RecipeResolution | null>(null)
  const [refusal, setRefusal] = useState<{ code: string; message: string } | null>(null)
  const [verifying, setVerifying] = useState(false)
  // Every change to the selection, every catalog refresh and the unmount retire
  // the current attempt, so a reply from an older one can never be displayed.
  const attempt = useRef(0)

  useEffect(() => () => { attempt.current += 1 }, [])

  const invalidateVerdict = useCallback(() => {
    attempt.current += 1
    setVerdict(null)
    setRefusal(null)
    // A verification whose answer was retired is no longer in flight, so the
    // button that started it becomes available again.
    setVerifying(false)
  }, [])

  const needle = query.trim().toLowerCase()
  const matches = useCallback(
    (...fields: (string | number | null | undefined)[]) =>
      !needle || fields.some((field) => field !== null && field !== undefined && String(field).toLowerCase().includes(needle)),
    [needle],
  )

  const plugins = catalog.state === 'ready' ? catalog.value.plugins : []
  const modelPlugins = plugins.filter((entry) => entry.kind === 'model')
  const otherPlugins = plugins.filter((entry) => entry.kind !== 'model')

  const hostAdmissible =
    catalog.state === 'ready' ? (modelPlugins.some((entry) => entry.usable) ? '是' : '否')
      : catalog.state === 'loading' ? '读取中' : UNKNOWN
  const configuredFact = fromStatus(settings, (status) => (status.configured ? '是' : '否'))
  const credentialFact = fromStatus(settings, (status) => yesNo(status.credential_available))

  const visibleModels = modelPlugins.filter((entry) =>
    matches(entry.plugin_id, entry.kind, entry.package_version, entry.artifact_digest, entry.entrypoint, identityText(entry.identity), effectsText(entry.effects), entry.reason?.code, entry.reason?.message))
  const visiblePlugins = otherPlugins.filter((entry) =>
    matches(entry.plugin_id, entry.kind, entry.package_version, entry.artifact_digest, entry.entrypoint, identityText(entry.identity), effectsText(entry.effects), entry.reason?.code, entry.reason?.message))
  const scenarioItems = scenarios.state === 'ready' ? scenarios.value : []
  const visibleScenarios = scenarioItems.filter((item) =>
    matches(item.scenario_id, item.name, item.input_identity?.dataset_id, item.input_identity?.source_sha256, item.summary?.strategy_id, item.summary?.venue, item.summary?.symbol))
  const datasetItems = datasets.state === 'ready' ? datasets.value : []
  const visibleDatasets = datasetItems.filter((item) =>
    matches(item.dataset_id, item.origin, item.source_sha256, item.version_evidence_digest, item.venue, item.symbol))

  const referenceSources = useMemo(() => {
    const sources: KnownReferenceSources = {}
    if (strategies.state === 'ready') sources.strategies = strategies.value
    if (tasks.state === 'ready') sources.tasks = tasks.value
    if (jobs.state === 'ready') sources.jobs = jobs.value
    return sources
  }, [strategies, tasks, jobs])
  const knownReferences = useMemo(() => deriveKnownReferences(referenceSources), [referenceSources])
  const sourceLoads = [strategies, tasks, jobs]
  const anySourceLoading = sourceLoads.some((load) => load.state === 'loading')
  const anySourceReady = sourceLoads.some((load) => load.state === 'ready')

  const choose = (layer: LayerName, value: string) => {
    invalidateVerdict()
    setSelection((current) => {
      const next: PluginSelection = { ...current }
      if (!value) delete next[layer]
      else {
        const separator = value.lastIndexOf('@')
        next[layer] = { plugin_id: value.slice(0, separator), package_version: value.slice(separator + 1) }
      }
      return next
    })
  }

  const refreshCatalog = () => {
    invalidateVerdict()
    reloadCatalog()
  }

  // An empty selection is a real composition: every layer on the host's own
  // default route. It is what the copy above the button promises, and the host
  // answers it like any other — so it is sent as it stands rather than refused
  // here, where we could only guess.
  const verify = async () => {
    const resolve = api.resolveRecipe
    if (!resolve) return
    const current = attempt.current + 1
    attempt.current = current
    setVerdict(null)
    setRefusal(null)
    setVerifying(true)
    try {
      const resolution = await resolve(selection)
      if (current !== attempt.current) return
      if (resolution.usable) setVerdict(resolution)
      else setRefusal({ code: resolution.code ?? 'not_usable', message: resolution.message ?? '主机没有说明拒绝的原因。' })
    } catch (caught) {
      if (current !== attempt.current) return
      setRefusal({ code: codeOf(caught), message: messageOf(caught) })
    } finally {
      if (current === attempt.current) setVerifying(false)
    }
  }

  const layerOptions = (layer: LayerName) => plugins.filter((entry) => LAYER_KINDS[layer].includes(entry.kind))

  return <div className="resources-page">
    <header className="page-title">
      <div>
        <h1>资源中心</h1>
        <p>本主机已声明资源的只读目录，以及组合授权校验。</p>
      </div>
    </header>

    <section className="panel resource-toolbar" aria-label="资源搜索">
      <div>
        <label htmlFor="resource-search">搜索资源</label>
        <input id="resource-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="模型、数据集、场景、插件" />
      </div>
      <div className="resource-toolbar-note">
        <span className="muted">只筛选下面的目录；引用一节来自已读取的记录，不受搜索影响。</span>
        <button disabled={!query} onClick={() => setQuery('')}>清除搜索</button>
      </div>
    </section>

    <section className="panel resource-section" aria-label="组合选择">
      <header>
        <h2>组合选择</h2>
        <span className="muted">只保留在这一次页面里；不保存、不套用、不建任务</span>
      </header>
      <div className="resource-layers">
        {LAYER_ORDER.map((layer) => {
          const options = layerOptions(layer)
          const chosen = selection[layer]
          const value = chosen ? `${chosen.plugin_id}@${chosen.package_version}` : ''
          // A version chosen before the last refresh can be gone from the catalog.
          // It stays as the exact value that will be sent, clearly marked, so the
          // host can refuse that version explicitly instead of this page silently
          // dropping it or showing an empty control.
          const missing = Boolean(chosen) && !options.some((entry) => optionValue(entry) === value)
          return <div key={layer}>
            <label htmlFor={`resource-layer-${layer}`}>{LAYER_TITLES[layer]}层</label>
            <select
              id={`resource-layer-${layer}`}
              value={value}
              onChange={(event) => choose(layer, event.target.value)}
            >
              <option value="">不指定</option>
              {options.map((entry) => <option key={optionValue(entry)} value={optionValue(entry)}>
                {entry.plugin_id} {entry.package_version}{entry.usable ? '' : ' · 主机拒绝'}
              </option>)}
              {missing && <option value={value}>{chosen!.plugin_id} {chosen!.package_version} · 当前目录中不存在</option>}
            </select>
          </div>
        })}
      </div>
      <div className="actions">
        <button className="primary" disabled={verifying || !api.resolveRecipe} onClick={() => { void verify() }}>
          {verifying ? '校验中…' : '校验组合'}
        </button>
        <Link to="/strategies">策略工作区</Link>
        <Link to="/research">历史研究任务</Link>
      </div>
      <p className="muted">每一层都可以留空：留空表示交给主机默认路径。选择本身不写任何东西，也不询问主机；策略与研究工作区仍是上面两个入口，这一页不代答，也不调用任何模型。</p>
      {catalog.state === 'failed' && <p className="muted">能力目录不可用，这里没有可选择的版本。</p>}
      {refusal && <p className="notice error" role="alert">主机拒绝：{refusal.code} · {refusal.message}</p>}
    </section>

    {verdict && <section className="panel resource-verdict" aria-label="组合校验结果">
      <header><h2>组合校验结果</h2><span>{verdict.schema}</span></header>
      <p><strong>组合授权检查</strong>：主机确认这一组声明可以被授权。它不说明模型连接、请求校验或运行绑定的状态，也不会启动任何运行。</p>
      <dl className="resource-facts">
        <div><dt>绑定摘要</dt><dd>{verdict.binding?.binding_sha256 ?? '主机未返回绑定'}</dd></div>
        <div><dt>被校验的选择</dt><dd>{selectionText(verdict.selection)}</dd></div>
      </dl>
      {verdict.binding && <ul className="resource-layer-list">
        {verdict.binding.components.map((component) => <li key={component.layer}>
          {identityLabel(component.layer)}: {component.plugin_id} {component.package_version}
        </li>)}
      </ul>}
    </section>}

    <section className="panel resource-section" aria-label="模型与协议封装">
      <header>
        <h2>模型与协议封装</h2>
        <div className="resource-section-actions">
          <span className="muted">{catalog.state === 'ready' ? `${plugins.length} 个已声明能力` : loadStateText(catalog)}</span>
          <button disabled={!api.settingsStatus} onClick={reloadSettings}>刷新配置状态</button>
          <button disabled={!api.pluginCatalog} onClick={refreshCatalog}>刷新能力目录</button>
        </div>
      </header>
      <dl className="resource-facts">
        <div><dt>主机可加载</dt><dd data-resource-fact="host-admissible">{hostAdmissible}</dd></div>
        <div><dt>已配置</dt><dd data-resource-fact="configured">{configuredFact}</dd></div>
        <div><dt>凭据可用</dt><dd data-resource-fact="credential">{credentialFact}</dd></div>
        <div><dt>连接</dt><dd data-resource-fact="connection">未验证</dd></div>
        <div><dt>请求能力校验</dt><dd data-resource-fact="request-validation">未验证</dd></div>
        <div><dt>当前提供方</dt><dd>{fromStatus(settings, (status) => textOrUnknown(status.provider))}</dd></div>
        <div><dt>当前模型</dt><dd>{fromStatus(settings, (status) => textOrUnknown(status.model))}</dd></div>
        <div><dt>协议</dt><dd>{fromStatus(settings, (status) => textOrUnknown(status.protocol))}</dd></div>
        <div><dt>端点</dt><dd>{fromStatus(settings, (status) => textOrUnknown(status.endpoint ?? status.base_url))}</dd></div>
        <div><dt>配置来源</dt><dd>{fromStatus(settings, (status) => present(status.configuration_present))}</dd></div>
        <div><dt>配置文件</dt><dd>{fromStatus(settings, (status) => textOrUnknown(status.settings_file))}</dd></div>
      </dl>
      <p className="muted">主机可加载不代表连接已验证。配置、凭据与验证状态分别显示；刷新只重读配置。真实调用记录请在策略工作区查看。</p>
      {settings.state === 'failed' && <p className="notice error" role="alert">模型配置状态不可用：{settings.message}</p>}
      {settings.state === 'ready' && settings.value.missing_capabilities?.length
        ? <p className="muted">主机报告的缺失能力：{settings.value.missing_capabilities.join(' · ')}</p>
        : null}
      {settings.state === 'ready' && settings.value.configuration_error
        ? <p className="muted">配置问题：{settings.value.configuration_error}</p>
        : null}
      {catalog.state === 'loading' && <p className="muted">正在读取能力目录…</p>}
      {catalog.state === 'failed' && <p className="notice error" role="alert">能力目录不可用：{catalog.message}</p>}
      {catalog.state === 'absent' && <p className="muted">此工作区不提供能力目录。</p>}
      {catalog.state === 'ready' && <>
        {visibleModels.length > 0 && <ul className="resource-entries">
          {visibleModels.map((entry) => <CatalogEntry key={optionValue(entry)} entry={entry} kind="model" presence={modelPresence(entry, settings)} />)}
        </ul>}
        {modelPlugins.length === 0 && <p className="muted">主机没有声明任何模型协议封装。</p>}
        {modelPlugins.length > 0 && visibleModels.length === 0 && <p className="muted">没有匹配当前搜索的资源。</p>}
      </>}
    </section>

    <section className="panel resource-section" aria-label="已声明能力">
      <header>
        <h2>已声明能力</h2>
        <span className="muted">数据、策略、校验与回测方法的声明</span>
      </header>
      {catalog.state === 'ready' && <>
        {visiblePlugins.length > 0 && <ul className="resource-entries">
          {visiblePlugins.map((entry) => <CatalogEntry key={optionValue(entry)} entry={entry} kind="plugin" presence={null} />)}
        </ul>}
        {otherPlugins.length === 0 && <p className="muted">主机没有声明除模型以外的能力。</p>}
        {otherPlugins.length > 0 && visiblePlugins.length === 0 && <p className="muted">没有匹配当前搜索的资源。</p>}
      </>}
      {catalog.state === 'loading' && <p className="muted">正在读取能力目录…</p>}
      {catalog.state === 'failed' && <p className="muted">能力目录不可用；这一节没有内容可显示。</p>}
      {catalog.state === 'absent' && <p className="muted">此工作区不提供能力目录。</p>}
    </section>

    <section className="panel resource-section" aria-label="数据集">
      <header>
        <h2>数据集</h2>
        <div className="resource-section-actions">
          <button disabled={!api.listDatasets} onClick={reloadDatasets}>刷新数据集</button>
        </div>
      </header>
      <p className="muted">只列已发布或已注册的独立数据集版本；注册场景带着自己的数据，单独列在场景一节。</p>
      {datasets.state === 'loading' && <p className="muted">正在读取数据集目录…</p>}
      {datasets.state === 'failed' && <p className="notice error" role="alert">数据集目录不可用：{datasets.message}</p>}
      {datasets.state === 'absent' && <p className="muted">此工作区不提供数据集目录。</p>}
      {datasets.state === 'ready' && <>
        {visibleDatasets.length > 0 && <ul className="resource-entries">
          {visibleDatasets.map((item) => <li key={item.dataset_id} className="resource-entry" data-resource-entry="dataset" data-id={item.dataset_id}>
            <div className="resource-entry-head">
              <strong>{item.dataset_id}</strong>
              <span className="resource-chip">{item.origin === 'upload' ? '已发布版本' : item.origin === 'registered' ? '已注册' : '来源未记录'}</span>
              <span className="resource-chip">{item.valid ? '有效' : '无效'}</span>
            </div>
            {!item.valid && (item.error_code || item.message) && <p className="resource-refusal">{item.error_code ?? '无效'} · {item.message}</p>}
            <details className="resource-details">
              <summary>版本证据</summary>
              <dl className="resource-entry-facts">
                <div><dt>版本证据摘要</dt><dd>{item.version_evidence_digest ?? '未记录'}</dd></div>
                <div><dt>来源摘要</dt><dd>{item.source_sha256 ?? '未记录'}</dd></div>
                <div><dt>记录 / 标的</dt><dd>{item.record_count ?? '未记录'} · {instrumentText(item)}</dd></div>
                <div><dt>可用性策略</dt><dd>{availabilityText(item.availability_policy)}</dd></div>
              </dl>
            </details>
          </li>)}
        </ul>}
        {datasets.value.length === 0 && <>
          <p className="muted">本工作区没有独立数据集。</p>
          <p className="muted">数据集为空不代表场景没有数据：已注册场景仍然使用它自己的已注册数据，列在下面一节。</p>
        </>}
        {datasets.value.length > 0 && visibleDatasets.length === 0 && <p className="muted">没有匹配当前搜索的资源。</p>}
      </>}
      <p><Link to="/resources/data">导入数据</Link></p>
    </section>

    <section className="panel resource-section" aria-label="场景">
      <header>
        <h2>场景</h2>
        <div className="resource-section-actions">
          <span className="muted">{scenarios.state === 'ready' ? `${scenarioItems.length} 个已注册场景` : loadStateText(scenarios)}</span>
          <button disabled={!api.listScenarios} onClick={reloadScenarios}>刷新场景</button>
        </div>
      </header>
      {scenarios.state === 'loading' && <p className="muted">正在读取已注册场景…</p>}
      {scenarios.state === 'failed' && <p className="notice error" role="alert">场景目录不可用：{scenarios.message}</p>}
      {scenarios.state === 'absent' && <p className="muted">此工作区不提供场景目录。</p>}
      {scenarios.state === 'ready' && <>
        {visibleScenarios.length > 0 && <ul className="resource-entries">
          {visibleScenarios.map((item: ScenarioSummary) => <li key={item.scenario_id} className="resource-entry" data-resource-entry="scenario" data-id={item.scenario_id}>
            <div className="resource-entry-head">
              <strong>{item.scenario_id}</strong>
              <span className="resource-chip">{item.valid ? '有效' : '无效'}</span>
            </div>
            {!item.valid && <p className="resource-refusal">{item.error_code ?? '无效'} · {item.message ?? '主机没有说明原因。'}</p>}
            <details className="resource-details">
              <summary>名称、数据集与策略</summary>
              <dl className="resource-entry-facts">
                <div><dt>名称</dt><dd>{item.name}</dd></div>
                <div><dt>数据集</dt><dd>{item.input_identity?.dataset_id ?? '未记录'}</dd></div>
                <div><dt>来源摘要</dt><dd>{item.input_identity?.source_sha256 ?? '未记录'}</dd></div>
                <div><dt>策略 / 标的</dt><dd>{item.summary ? `${item.summary.strategy_id} · ${instrumentText(item.summary)}` : '未记录'}</dd></div>
              </dl>
            </details>
          </li>)}
        </ul>}
        {scenarioItems.length === 0 && <p className="muted">本工作区没有已注册场景。</p>}
        {scenarioItems.length > 0 && visibleScenarios.length === 0 && <p className="muted">没有匹配当前搜索的资源。</p>}
      </>}
    </section>

    <section className="panel resource-section" aria-label="可观察到的已知引用">
      <header>
        <h2>可观察到的已知引用</h2>
        <div className="resource-section-actions">
          <span className="muted">不是完整的运行时依赖</span>
          <button onClick={() => { reloadStrategies(); reloadTasks(); reloadJobs() }}>刷新引用</button>
        </div>
      </header>
      <ul className="resource-source-state">
        <SourceState name="source-strategies" label="策略来源" load={strategies} />
        <SourceState name="source-tasks" label="研究任务来源" load={tasks} />
        <SourceState name="source-jobs" label="运行记录来源" load={jobs} />
      </ul>
      <p className="muted">这一节只重述已读取记录里存在的事实，不是完整的运行时依赖；读不到的标识保持为未知，不按名称、符号或当前模型推断。</p>
      {knownReferences.length > 0
        ? <ul className="resource-references">
          {knownReferences.map((reference) => <ReferenceItem key={reference.key} reference={reference} />)}
        </ul>
        : !anySourceLoading && anySourceReady
          ? <p className="muted">已读取的来源中没有已知引用。</p>
          : null}
    </section>

    <section className="panel resource-section" aria-label="当前未支持">
      <header><h2>当前未支持</h2></header>
      <p className="muted">多账户、数据库与多套执行配置在本工作区没有实现：这里没有表单、没有保存，也没有假装可用的开关。</p>
    </section>
  </div>
}
