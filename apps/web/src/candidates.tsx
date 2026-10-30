import { SourceLink as Link } from './workspace-navigation'
import { useEffect, useRef, useState } from 'react'
import { StrategyAssociation } from './research-association-panel'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { ApiAdapter, ChronologicalHoldout } from './app'

type Evidence = { job_id: string; run_id: string; report_sha256: string; equity_path_sha256: string; input_sha256: string; scenario_sha256: string; data_sha256: string; record_count: number }
export type Candidate = {
  candidate_id: string; status: 'EVALUATED' | 'ACCEPTED' | 'REJECTED' | 'UNAVAILABLE'
  fingerprint?: string; created_at?: string; evaluated_at?: string
  decision?: { outcome: string; reason: string; decided_at: string } | null
  projection?: { strategy: { id: string; version: number; parameters: Record<string, string | number> }; source: Evidence; holdout: Evidence; relationship: { validation_id: string; sha256: string } }
}

export type SimulationPreparation = {
  eligible: boolean; blockers: string[]; fingerprint: string; scenario_version: number
  execution_authorized: false; start_available: boolean; runtime_state: 'not_started'
  binding: Record<string, unknown> | null; required_action: string; stop_semantics: string
  small_sample_warning: boolean
}

function SimulationReadiness({ api, candidateId, status }: { api: ApiAdapter; candidateId: string; status: string }) {
  const [preparation, setPreparation] = useState<SimulationPreparation | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api.prepareSimulation?.(candidateId).then(value => { if (active) { setPreparation(value); setError('') } }).catch(() => { if (active) { setPreparation(null); setError('Preparation error: candidate evidence could not be verified. Reload to retry.') } })
    return () => { active = false }
  }, [api, candidateId, status])
  if (!api.prepareSimulation) return null
  return <section className="panel" aria-label="Local simulation preparation"><h2>Local simulation preparation</h2>
    <p>Generated local prices · in-process Paper broker · no external account · Live unavailable.</p>
    {error && <p role="alert">{error}</p>}
    {preparation && <><p>{preparation.eligible ? 'Profile eligible; separate startup authorization required.' : 'Blocked for local Paper startup.'}</p>
      <dl className="summary-list"><div><dt>Runtime</dt><dd>{preparation.runtime_state}</dd></div><div><dt>Scenario version</dt><dd>{preparation.scenario_version}</dd></div><div><dt>Bound fingerprint</dt><dd>{preparation.fingerprint}</dd></div></dl>
      <ul>{preparation.blockers.map(reason => <li key={reason}>{reason.replaceAll('_', ' ')}</li>)}</ul>
      {preparation.binding && <details><summary>Verified accepted binding</summary><pre>{JSON.stringify(preparation.binding, null, 2)}</pre></details>}
      {preparation.small_sample_warning && <p className="notice">Six or fewer records: workflow validation only. This is insufficient for automatic economic approval.</p>}
      <p>{preparation.required_action}</p><p>{preparation.stop_semantics}</p>
      {preparation.start_available && preparation.eligible ? <Link to={`/runs?candidate=${encodeURIComponent(candidateId)}`}>检查配置并启动本地模拟</Link> : <p>此入口不可启动，或候选配置不支持本地模拟。</p>}
    </>}
  </section>
}

export function CandidateCreate({ api, jobId }: { api: ApiAdapter; jobId: string }) {
  const navigate = useNavigate()
  const [relations, setRelations] = useState<ChronologicalHoldout[]>([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    api.listHoldouts().then(items => { if (active) setRelations(items.filter(item => item.source_job_id === jobId)) }).catch(() => { if (active) setError('Holdout evidence unavailable') })
    return () => { active = false }
  }, [api, jobId])
  if (!api.createCandidate) return null
  const create = async () => {
    setBusy(true); setError('')
    try { const record = await api.createCandidate!(selected); navigate(`/candidates/${record.candidate_id}`) }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Creation not confirmed. Check saved candidates before retrying.') }
  }
  return <section className="panel" aria-label="Create research candidate">
    <header><h2>Research candidate</h2><span>Explicit evidence selection</span></header>
    <p>Choose a completed chronological Holdout. Creation verifies both runs and preserves this exact evidence set.</p>
    <label htmlFor="candidate-holdout">Candidate Holdout</label>
    <select id="candidate-holdout" value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}>
      <option value="">Select a Holdout evaluation</option>{relations.map(item => <option key={item.validation_id} value={item.validation_id}>{item.validation_id} · {item.created_at}</option>)}
    </select>
    {!relations.length && <p className="muted">Complete a chronological Holdout for this source first.</p>}
    {error && <p role="alert" className="notice error">{error}</p>}
    <div className="actions"><button disabled={!selected || busy} onClick={() => { void create() }}>Create evaluated candidate</button><Link to="/candidates">Saved candidates</Link></div>
  </section>
}

export function CandidateHistory({ api, studio = false }: { api: ApiAdapter; studio?: boolean }) {
  const [items, setItems] = useState<Candidate[]>([])
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0), [params] = useSearchParams()
  const validation = studio ? params.get('validation') : null
  const visible = items.filter(item => !validation || item.status === 'UNAVAILABLE' || item.projection?.relationship.validation_id === validation)
  useEffect(() => {
    let active = true
    api.listCandidates?.().then(items => { if (active) { setItems(items); setError('') } }).catch(() => { if (active) setError(studio ? '候选历史暂不可读，请重新读取。' : 'Candidate history unavailable') })
    return () => { active = false }
  }, [api, refresh, studio])
  return <><header className="page-title"><div><h1>{studio ? '研究候选历史' : 'Research Candidates'}</h1><p>{studio ? '已保存的冻结证据与人工研究决定。' : 'Saved evidence and human research decisions.'}</p></div></header>
    <button className="button" onClick={() => setRefresh(n => n + 1)}>{studio ? '重新读取历史' : 'Reload candidates'}</button>
    {validation && <p>留出关系：{validation} · <Link to="/candidates">全部候选</Link></p>}
    {error && <p role="alert" className="notice error">{error}</p>}
    <section className="panel"><ul className="job-list">{visible.map(item => <li className="job-card" key={item.candidate_id}><Link to={`/candidates/${item.candidate_id}`}>{item.candidate_id}</Link><strong>{item.status}</strong><small>{item.projection?.strategy.id ?? (studio ? '证据不可用' : 'Evidence unavailable')}</small></li>)}</ul>
      {!visible.length && !error && <p>{studio ? '尚未读到候选。从已完成的留出关系创建；未知回执请稍后重新查询。' : 'Create a candidate from a completed source run with chronological Holdout evidence.'}</p>}</section></>
}

export function CandidateDetail({ api, studio = false }: { api: ApiAdapter; studio?: boolean }) {
  const { candidateId = '' } = useParams()
  return <CandidateDetailContent key={candidateId} api={api} candidateId={candidateId} studio={studio} />
}

function CandidateDetailContent({ api, candidateId, studio }: { api: ApiAdapter; candidateId: string; studio: boolean }) {
  const [record, setRecord] = useState<Candidate | null>(null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0), sending = useRef(false)
  const t = (en: string, zh: string) => studio ? zh : en
  useEffect(() => {
    let active = true
    api.getCandidate?.(candidateId).then(item => { if (active) { setRecord(item); setError('') } }).catch(() => { if (active) { setRecord(null); setError(studio ? '候选证据不可用，无法作出决定。请重新读取。' : 'Candidate evidence unavailable. Decisions are disabled.') } })
    return () => { active = false }
  }, [api, candidateId, refresh, studio])
  const decide = async (outcome: 'ACCEPTED' | 'REJECTED') => {
    if (sending.current || record?.status !== 'EVALUATED' || !reason.trim()) return
    sending.current = true; setBusy(true); setError('')
    try { setRecord(await api.decideCandidate!(candidateId, outcome, reason)) }
    catch (caught) { setRecord(null); setError(`${caught instanceof Error ? caught.message : 'Decision not confirmed.'} ${t('Reload to inspect saved state; no decision is resent.', '决定结果未确认，请重新读取已保存状态；不会重发决定。')}`) }
    finally { sending.current = false; setBusy(false) }
  }
  return <><header className="page-title"><div><h1>{t('Research Candidate', '研究候选')}</h1><p>{candidateId}</p></div></header>
    <div className="actions"><Link to="/candidates">{t('Saved candidates', '研究候选历史')}</Link><button className="button" disabled={busy} onClick={() => setRefresh(n => n + 1)}>{t('Reload candidate', '重新读取候选')}</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {record?.projection && <>
      {!studio && <StrategyAssociation key={record.projection.source.job_id} api={api} jobId={record.projection.source.job_id} />}
      <section className="panel"><header><h2>{t('Research decision', '人工研究决定')}</h2><strong>{record.status}</strong></header>
        <p>{t('EVALUATED means the selected evidence is complete. ACCEPTED retains a research judgment and grants no execution permission.', 'EVALUATED 表示选定证据完整，不代表统计有效、盈利或自动合格。ACCEPTED 只记录人工研究判断，不启动实验或模拟。')}</p>
        <dl className="summary-list"><div><dt>{t('Fingerprint', '证据指纹')}</dt><dd>{record.fingerprint}</dd></div><div><dt>{t('Strategy', '冻结策略')}</dt><dd>{record.projection.strategy.id} v{record.projection.strategy.version}</dd></div><div><dt>{t('Parameters', '冻结参数')}</dt><dd>{Object.entries(record.projection.strategy.parameters).map(([key, value]) => `${key}: ${value}`).join(' · ')}</dd></div></dl>
        {record.status === 'EVALUATED' && <><label htmlFor="candidate-reason">{t('Decision reason', '决定理由')}</label><textarea id="candidate-reason" maxLength={4096} value={reason} disabled={busy} onChange={event => setReason(event.target.value)} /><div className="actions"><button className="button" disabled={!reason.trim() || busy} onClick={() => { void decide('ACCEPTED') }}>{t('Accept candidate', '接受候选')}</button><button className="button" disabled={!reason.trim() || busy} onClick={() => { void decide('REJECTED') }}>{t('Reject candidate', '拒绝候选')}</button>{busy && <span role="status">{t('Saving decision…', '正在保存决定…')}</span>}</div></>}
        {record.decision && <><h3>{t('Decision reason', '决定理由')}</h3><p>{record.decision.reason}</p><time>{record.decision.decided_at}</time></>}
      </section>
      {!studio && <SimulationReadiness api={api} candidateId={candidateId} status={record.status} />}
      <section className="panel"><h2>{t('Pinned evidence', '冻结证据')}</h2><p><Link to={`/${studio ? 'holdout' : 'holdouts'}/${record.projection.relationship.validation_id}`}>{t('Chronological Holdout evaluation', '留出验证关系')}</Link></p>
        <div className="compare-identities">{(['source', 'holdout'] as const).map(role => { const evidence = record.projection![role]; const label = role === 'source' ? '来源报告' : '留出报告'; return <section key={role}><h3>{t(role === 'source' ? 'Source run' : 'Holdout run', label)}</h3><Link to={studio ? `/report/${encodeURIComponent(evidence.job_id)}/overview` : `/backtests/${evidence.job_id}`}>{studio ? `${label} · ${evidence.job_id}` : evidence.job_id}</Link><dl className="summary-list">{Object.entries(evidence).filter(([key]) => key !== 'job_id').map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></section> })}</div>
        {studio && <details><summary>完整冻结身份</summary><pre>{JSON.stringify(record.projection, null, 2)}</pre></details>}
      </section>
    </>}
  </>
}
