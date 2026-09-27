import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { ApiAdapter, ChronologicalHoldout } from './app'

type Evidence = { job_id: string; run_id: string; report_sha256: string; equity_path_sha256: string; input_sha256: string; scenario_sha256: string; data_sha256: string; record_count: number }
export type Candidate = {
  candidate_id: string; status: 'EVALUATED' | 'ACCEPTED' | 'REJECTED' | 'UNAVAILABLE'
  fingerprint?: string; created_at?: string; evaluated_at?: string
  decision?: { outcome: string; reason: string; decided_at: string } | null
  projection?: { strategy: { id: string; version: number; parameters: Record<string, string | number> }; source: Evidence; holdout: Evidence; relationship: { validation_id: string; sha256: string } }
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

export function CandidateHistory({ api }: { api: ApiAdapter }) {
  const [items, setItems] = useState<Candidate[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api.listCandidates?.().then(items => { if (active) setItems(items) }).catch(() => { if (active) setError('Candidate history unavailable') })
    return () => { active = false }
  }, [api])
  return <><header className="page-title"><div><h1>Research Candidates</h1><p>Saved evidence and human research decisions.</p></div></header>
    {error && <p role="alert" className="notice error">{error}</p>}
    <section className="panel"><ul className="job-list">{items.map(item => <li className="job-card" key={item.candidate_id}><Link to={`/candidates/${item.candidate_id}`}>{item.candidate_id}</Link><strong>{item.status}</strong><small>{item.projection?.strategy.id ?? 'Evidence unavailable'}</small></li>)}</ul>
      {!items.length && !error && <p>Create a candidate from a completed source run with chronological Holdout evidence.</p>}</section></>
}

export function CandidateDetail({ api }: { api: ApiAdapter }) {
  const { candidateId = '' } = useParams()
  return <CandidateDetailContent key={candidateId} api={api} candidateId={candidateId} />
}

function CandidateDetailContent({ api, candidateId }: { api: ApiAdapter; candidateId: string }) {
  const [record, setRecord] = useState<Candidate | null>(null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    api.getCandidate?.(candidateId).then(item => { if (active) setRecord(item) }).catch(() => { if (active) setError('Candidate evidence unavailable. Decisions are disabled.') })
    return () => { active = false }
  }, [api, candidateId])
  const decide = async (outcome: 'ACCEPTED' | 'REJECTED') => {
    setBusy(true); setError('')
    try { setRecord(await api.decideCandidate!(candidateId, outcome, reason)) }
    catch (caught) { setRecord(null); setError(caught instanceof Error ? caught.message : 'Decision not confirmed. Reload to inspect saved state.') }
    finally { setBusy(false) }
  }
  return <><header className="page-title"><div><h1>Research Candidate</h1><p>{candidateId}</p></div></header>
    <p><Link to="/candidates">Saved candidates</Link></p>
    {error && <p role="alert" className="notice error">{error}</p>}
    {record?.projection && <>
      <section className="panel"><header><h2>Research decision</h2><strong>{record.status}</strong></header>
        <p>EVALUATED means the selected evidence is complete. ACCEPTED retains a research judgment and grants no execution permission.</p>
        <dl className="summary-list"><div><dt>Fingerprint</dt><dd>{record.fingerprint}</dd></div><div><dt>Strategy</dt><dd>{record.projection.strategy.id} v{record.projection.strategy.version}</dd></div><div><dt>Parameters</dt><dd>{Object.entries(record.projection.strategy.parameters).map(([key, value]) => `${key}: ${value}`).join(' · ')}</dd></div></dl>
        {record.status === 'EVALUATED' && <><label htmlFor="candidate-reason">Decision reason</label><textarea id="candidate-reason" maxLength={4096} value={reason} onChange={event => setReason(event.target.value)} /><div className="actions"><button disabled={!reason.trim() || busy} onClick={() => { void decide('ACCEPTED') }}>Accept candidate</button><button disabled={!reason.trim() || busy} onClick={() => { void decide('REJECTED') }}>Reject candidate</button></div></>}
        {record.decision && <><h3>Decision reason</h3><p>{record.decision.reason}</p><time>{record.decision.decided_at}</time></>}
      </section>
      <section className="panel"><h2>Pinned evidence</h2><p><Link to={`/holdouts/${record.projection.relationship.validation_id}`}>Chronological Holdout evaluation</Link></p>
        <div className="compare-identities">{(['source', 'holdout'] as const).map(role => { const evidence = record.projection![role]; return <section key={role}><h3>{role === 'source' ? 'Source run' : 'Holdout run'}</h3><Link to={`/backtests/${evidence.job_id}`}>{evidence.job_id}</Link><dl className="summary-list">{Object.entries(evidence).filter(([key]) => key !== 'job_id').map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></section> })}</div>
      </section>
    </>}
  </>
}
