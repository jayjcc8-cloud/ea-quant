import type { PlatformId } from './platform-context'

/** A collection row is a product read model, never an editable strategy document. */
export type ResearchSummary = {
  user_strategy_id: string
  name: string
  intent: string
  exchange_id: PlatformId | null
  updated_at: string
  instrument: { venue: string; symbol: string } | null
  draft: { kind: 'intent' | 'configured'; revision: number; pending_proposals: number }
  experiment_count: number
  latest_experiment: {
    launch_intent_id: string | null
    job_id: string | null
    draft_revision: number | null
    launch_status: string
    job_status: string | null
    report_ready: boolean
    read_error: string | null
  } | null
  next_step: 'draft' | 'review' | 'experiment' | 'inspect' | 'evidence'
}
export type ResearchQuery = { q?: string; exchange_id?: PlatformId; sort?: 'updated' | 'name' }
export type ResearchCollection = { items: ResearchSummary[]; total: number; matched: number }

/** Identity-only provenance for reports and resource references, without conversation history. */
export type ResearchReference = {
  user_strategy_id: string
  name: string
  draft: { kind?: 'strategy' | 'intent'; revision: number; scenario_id?: string | null; dataset_id?: string | null; source_sha256?: string | null }
  experiments: { job_id: string | null; launch_intent_id?: string | null; payload?: {
    draft_revision?: number | null; scenario_id?: string | null
    input_identity?: { dataset_id?: string | null; source_sha256?: string | null }
  } }[]
}
