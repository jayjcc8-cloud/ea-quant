import type { ResearchReference } from './research-workbench-api'

export type StrategyAssociationRecord = { userStrategyId: string; name: string; revision: number | null }

/**
 * One frozen record's revision, printed only when the record really carries a
 * positive whole revision. A payload that is missing, empty or nonsensical is
 * named unknown; this page never invents a revision to fill the gap.
 */
function frozenRevision(payload: { draft_revision?: number | null } | undefined): number | null {
  const revision = payload?.draft_revision
  return typeof revision === 'number' && Number.isInteger(revision) && revision > 0 ? revision : null
}

export const frozenRevisionText = (revision: number | null) =>
  revision === null ? '冻结修订未知' : `冻结 v${revision}`

/**
 * Every frozen record in the saved strategies whose own experiment names this
 * exact job. This is the one association rule: the record's persisted `job_id`,
 * never the report's strategy id, engine implementation, symbol, name, or a
 * "first/latest/current" choice. Both the report page and the comparison page
 * read it here, so neither can drift into a different rule.
 */
export function experimentRecordsForJob(strategies: ResearchReference[], jobId: string): StrategyAssociationRecord[] {
  return strategies.flatMap(strategy => strategy.experiments
    .filter(experiment => experiment.job_id === jobId)
    .map(experiment => ({
      userStrategyId: strategy.user_strategy_id,
      name: strategy.name,
      revision: frozenRevision(experiment.payload),
    })))
}
