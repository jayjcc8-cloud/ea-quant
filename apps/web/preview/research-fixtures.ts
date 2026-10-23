/** Scripted transport projection for existing UI fixtures, never a runtime adapter. */
import { isIntentDraft, type UserStrategy } from '../src/user-strategies-api'
import type { ResearchCollection, ResearchQuery } from '../src/research-workbench-api'
export function researchCollection(records: UserStrategy[], query: ResearchQuery = {}): ResearchCollection {
  const items = records.filter(record => (!query.exchange_id || record.exchange_id === query.exchange_id) && `${record.name} ${record.intent} ${isIntentDraft(record.draft) ? '' : record.draft.symbol}`.toLowerCase().includes((query.q ?? '').toLowerCase())).map(record => ({
    user_strategy_id: record.user_strategy_id, name:record.name, intent:record.intent, updated_at:record.updated_at,
    exchange_id:record.exchange_id ?? null, instrument:isIntentDraft(record.draft) ? null : {venue:record.draft.venue,symbol:record.draft.symbol},
    draft:{kind:isIntentDraft(record.draft) ? 'intent' as const : 'configured' as const,revision:record.draft.revision,pending_proposals:record.proposals.filter(p=>p.status==='open').length},
    experiment_count:record.experiments.length,
    latest_experiment:record.experiments.length ? {launch_intent_id:record.experiments.at(-1)!.launch_intent_id,job_id:record.experiments.at(-1)!.job_id,draft_revision:record.experiments.at(-1)!.payload.draft_revision,launch_status:record.experiments.at(-1)!.status,job_status:null,report_ready:false,read_error:null} : null,
    next_step:isIntentDraft(record.draft) ? 'draft' as const : 'experiment' as const,
  })).sort((a,b)=>query.sort==='name'?a.name.localeCompare(b.name):b.updated_at.localeCompare(a.updated_at))
  return {items,total:records.length,matched:items.length}
}
