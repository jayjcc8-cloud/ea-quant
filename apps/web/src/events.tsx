import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

type Sample = {
  sample_id: string
  contract: { question: string; rules: string; authority: string; source: string; source_kind: string; rules_observed_at: string }
  side: string; quantity: number
  observation: { available_at: string; evidence: string; status: string; payout_yes: string | null; payout_no: string | null }
  result?: { status: string; action?: string; cash: string; currency: string; cash_semantics: string; outcome: string | null; success: boolean; settlement_count: number; open_claims: number; retired_claims: number; postings: { account: string; currency: string; amount: string }[] } | null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const value = await response.json()
  if (!response.ok) throw new Error(`${value.error?.code ?? 'request_failed'}: ${value.error?.message ?? 'Event evidence unavailable'}`)
  return value as T
}

export function EventValidation() {
  const [samples, setSamples] = useState<Sample[]>([])
  const [search, setSearch] = useSearchParams()
  const selected = search.get('sample') ?? ''
  const [loadedSample, setSample] = useState<Sample | null>(null)
  const sample = loadedSample?.sample_id === selected ? loadedSample : null
  const [asOf, setAsOf] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    request<{ samples: Sample[] }>('/api/events').then(value => { if (active) setSamples(value.samples) }).catch(caught => { if (active) setError(String(caught)) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    let active = true
    if (selected) request<Sample>(`/api/events/${encodeURIComponent(selected)}`).then(value => {
      if (active) { setSample(value); setError(''); setAsOf(value.observation.available_at) }
    }).catch(caught => { if (active) setError(String(caught)) })
    return () => { active = false }
  }, [selected])
  const run = async (simulate: boolean) => {
    setBusy(true); setError(''); setSample(null)
    try {
      setSample(await request<Sample>(`/api/events/${encodeURIComponent(selected)}${simulate ? '/simulate' : ''}`, simulate ? {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EA-Web-Request': '1' }, body: JSON.stringify({ as_of: asOf }),
      } : undefined))
    } catch (caught) { setError(String(caught)) }
    finally { setBusy(false) }
  }
  return <>
    <header className="page-title"><div><h1>Event Validation</h1><p>Offline event lifecycle validation only · fixed curated samples</p></div></header>
    <section className="notice"><p><Link to="/research">Price research</Link> studies price rules and backtests. Event validation consumes explicit rules and authority observations to simulate a pre-endowed binary claim.</p>
      <p>Unavailable: AI prediction strategy generation, probability forecasts, market discovery, real trading, wallet redemption, Candidate acceptance and Paper promotion.</p>
      <p>Local trusted evidence only; source binding does not verify signatures. USD_SIM is a simulation unit. No account or trading permission is granted.</p></section>
    {error && <p role="alert" className="notice error">{error}</p>}
    <section className="panel"><label htmlFor="event-sample">Existing event sample</label><select id="event-sample" value={selected} disabled={busy} onChange={event => setSearch(event.target.value ? { sample: event.target.value } : {})}>
      <option value="">Select a historical or synthetic example</option>{samples.map(item => <option key={item.sample_id} value={item.sample_id}>{item.contract.source_kind} · {item.contract.question}</option>)}
    </select></section>
    {selected && <section className="panel"><label htmlFor="event-as-of">Simulation as-of (UTC)</label><input id="event-as-of" value={asOf} disabled={busy} onChange={event => setAsOf(event.target.value)} />
      <button disabled={busy || !asOf} onClick={() => { void run(true) }}>Validate / simulate locally</button> <button disabled={busy} onClick={() => { void run(false) }}>Refresh saved result</button>
      <p>Repeating the same final observation is duplicate_noop. Conflicting terminal revisions are rejected by C2 and require explicit adjudication; this view cannot submit revisions. Earlier times require a fresh journal once the saved frontier has advanced.</p></section>}
    {sample && <section className="panel"><h2>Rules and evidence</h2><dl className="summary-list">
      <div><dt>Source kind</dt><dd>{sample.contract.source_kind}</dd></div><div><dt>Source</dt><dd>{sample.contract.source.startsWith('https://') ? <a href={sample.contract.source}>{sample.contract.source}</a> : sample.contract.source}</dd></div>
      <div><dt>Authority</dt><dd>{sample.contract.authority}</dd></div><div><dt>Rules observed at</dt><dd>{sample.contract.rules_observed_at}</dd></div>
      <div><dt>available_at</dt><dd>{sample.observation.available_at}</dd></div><div><dt>Claim</dt><dd>{sample.quantity} pre-endowed {sample.side}</dd></div>
      <div><dt>Declared final observation</dt><dd>{sample.observation.status} · YES {sample.observation.payout_yes} / NO {sample.observation.payout_no}</dd></div>
    </dl><p>{sample.contract.rules}</p><p>{sample.observation.evidence}</p>
      <p>Historical available_at is the 2026-10-03 capture time, not proven historical publication time. Before availability the fresh simulation remains pending. Synthetic void is a declared fixed cancellation payout, not a historical venue rule.</p></section>}
    {sample && <section className="panel" aria-label="Event result"><h2>Local lifecycle result</h2>
      <p>pending: no final result · resolved: explicit winner · void: explicit cancellation payout · unknown/disputed: no final settlement</p>
      {sample.result ? <><dl className="summary-list">{Object.entries(sample.result).filter(([name]) => name !== 'postings').map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{String(value ?? 'none')}</dd></div>)}</dl>
        <h3>Balanced simulated postings</h3><ul>{sample.result.postings.map(item => <li key={item.account}>{item.account}: {item.amount} {item.currency}</li>)}</ul></> : <p>No saved simulation. Validate the selected fixture explicitly.</p>}
    </section>}
  </>
}
