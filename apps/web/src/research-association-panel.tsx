import { useEffect, useRef, useState } from 'react'
import { SourceLink as Link } from './workspace-navigation'
import type { ResearchReference } from './research-workbench-api'
import type { ApiAdapter } from './app'
import { experimentRecordsForJob, frozenRevisionText, type StrategyAssociationRecord } from './research-association'

/**
 * The way back from one formal report to the persisted strategy whose frozen run
 * it is. The association is read on its own and matched only by the job each
 * experiment recorded: never the report's own strategy id, engine implementation,
 * symbol, name, or a "first/latest/current" choice. One exact record offers the
 * return link; no record, an unavailable strategy API or a refused read says so
 * honestly and falls back to the strategy list; several records are a conflict
 * this page refuses to resolve silently. The caller keys the component on the job
 * id and on the explicit refresh, and its read is generation-guarded, so a route
 * change, a refresh or a late answer can never put another job's strategy on this
 * page.
 */
export function StrategyAssociation({ api, jobId, onRead, compact = false }: { api: ApiAdapter; jobId: string; onRead?: (items: ResearchReference[]) => void; compact?: boolean }) {
  type Association =
    | { kind: 'reading' }
    | { kind: 'none'; note: string }
    | { kind: 'one'; record: StrategyAssociationRecord }
    | { kind: 'several'; count: number; records: StrategyAssociationRecord[] }
  // The first render of every mount reads; the caller keys this component on the
  // job and on the explicit refresh, so a new job or a new refresh starts here in
  // the reading state without the effect having to write it synchronously.
  const [association, setAssociation] = useState<Association>({ kind: 'reading' })
  const generation = useRef(0)

  useEffect(() => {
    const read = ++generation.current
    const list = api.researchReferences
    if (!list) {
      // The optional read settles asynchronously, so a workspace that unmounts or
      // swaps its api never writes this page from a stale answer.
      void Promise.resolve().then(() => {
        if (generation.current === read) { setAssociation({ kind: 'none', note: '这个工作区不提供已保存的用户策略。' }); onRead?.([]) }
      })
      return () => { generation.current += 1 }
    }
    list(jobId).then(strategies => {
      if (generation.current !== read) return
      onRead?.(strategies)
      const records = experimentRecordsForJob(strategies, jobId)
      if (records.length === 1) setAssociation({ kind: 'one', record: records[0] })
      else if (records.length === 0) setAssociation({ kind: 'none', note: '没有已保存的用户策略冻结了这个运行。' })
      else setAssociation({ kind: 'several', count: records.length, records })
    }).catch(error => {
      if (generation.current !== read) return
      onRead?.([])
      setAssociation({
        kind: 'none',
        note: `${error instanceof Error ? error.message : '策略关联读取失败。'} · 这次关联读取失败了，正式报告不受影响。`,
      })
    })
    return () => { generation.current += 1 }
  }, [api, jobId, onRead])

  // One candidate per distinct user strategy, named with its own id so two
  // strategies that share a name stay told apart. No revision is printed here:
  // records that disagree about the frozen revision are exactly the case this
  // page refuses to resolve, so it names the strategies and picks no revision.
  const candidates = association.kind === 'several'
    ? association.records.filter((record, index) =>
        association.records.findIndex(item => item.userStrategyId === record.userStrategyId) === index)
    : []

  return <section className={`panel association-panel${compact ? " association-compact" : ""}`} aria-label="策略关联">
    <header><h2>策略关联</h2><span>只读 · 不启动任何运行</span></header>
    {association.kind === 'reading' && <p className="muted">正在读取冻结了这个运行的已保存策略……</p>}
    {association.kind === 'none' && <>
      <p className="muted">{association.note}</p>
      <p><Link to="/strategies">打开策略列表</Link></p>
    </>}
    {association.kind === 'one' && <>
      <p><strong>{association.record.name}</strong> · <span>{frozenRevisionText(association.record.revision)}</span></p>
      {/* The way back names the frozen run this report is about, so the strategy
          opens on that run instead of resetting the evidence to the latest one. */}
      <p><Link to={`/strategies/${encodeURIComponent(association.record.userStrategyId)}?evidence_a=${encodeURIComponent(jobId)}`}>返回此策略继续研究</Link></p>
    </>}
    {association.kind === 'several' && <>
      <p className="notice error">{`这个运行匹配到 ${association.count} 条冻结记录，无法确定唯一的来源策略，因此这里不替你选择，也不显示任何冻结修订：请自己打开要继续的那一个。`}</p>
      <ul className="association-candidates">{candidates.map(candidate => <li key={candidate.userStrategyId}>
        <Link to={`/strategies/${encodeURIComponent(candidate.userStrategyId)}`}>{`${candidate.name} · ${candidate.userStrategyId}`}</Link>
      </li>)}</ul>
    </>}
  </section>
}
