/**
 * The address is the selection: evidence_a names the frozen run a formal report is
 * about, and evidence_b adds the second run of a comparison, in that order.
 *
 * The report card at the top of the 证据 tab and the research panel below it are two
 * views of one selection, so both read this one module: neither can validate an
 * address differently from the other, and neither can show a run the address does
 * not state. An address that names something unverifiable is refused by both and
 * never repaired into a pair that happens to be valid — the plain address is the
 * only fallback, and it is the latest frozen run as A with no B.
 *
 * Everything here is pure: no reads, no writes, no state.
 */
import type { UserStrategy, UserStrategyExperiment } from './user-strategies-api'

/** The two query parameters a verified report or comparison carries back here. */
export const EVIDENCE_A = 'evidence_a'
export const EVIDENCE_B = 'evidence_b'

/**
 * What the address asks for: the default selection this page has always had, the
 * runs it names, or a refusal that names what could not be verified.
 */
export type EvidenceSelection =
  | { kind: 'default' }
  | { kind: 'draft' }
  | { kind: 'explicit'; a: UserStrategyExperiment; b: UserStrategyExperiment | null }
  | { kind: 'invalid'; note: string }

/** The runs of this strategy that an address may name: a created record with a job. */
export function selectableRuns(strategy: UserStrategy): UserStrategyExperiment[] {
  return strategy.experiments.filter(item => item.status === 'created' && item.job_id)
}

/**
 * Read the address against the runs this strategy really froze. Nothing named means
 * nothing is claimed; anything named must be exactly one created record of this
 * strategy, and a pair must be two different runs.
 */
export function readEvidenceSelection(
  params: URLSearchParams,
  runs: UserStrategyExperiment[],
): EvidenceSelection {
  const aParams = params.getAll(EVIDENCE_A)
  const bParams = params.getAll(EVIDENCE_B)
  // The address this page has always accepted: nothing named, so nothing is
  // claimed — A is the latest frozen run and B is absent.
  if (!aParams.length && !bParams.length) return { kind: params.get('context') === 'draft' ? 'draft' : 'default' }
  const invalid = (note: string): EvidenceSelection => ({ kind: 'invalid', note })
  if (aParams.length > 1 || bParams.length > 1) {
    return invalid('同一个参数在地址里出现了多次，无法确定要解释哪一次运行。')
  }
  const aId = aParams[0] ?? ''
  const bId = bParams[0] ?? ''
  if (!aId) return invalid('A 是空的：空值不是一次运行。')
  if (bParams.length && !bId) return invalid('B 是空的：空值不是一次运行。')
  // Exactly one created record of this strategy per id, or the id is not the run it
  // claims to be. An unknown, foreign or ambiguous id is refused here and refused
  // again by the host, which never falls back to a run of its own choosing.
  const own = (id: string) => runs.filter(item => item.job_id === id)
  const aMatches = own(aId)
  if (aMatches.length !== 1) {
    return invalid(`A 指定的运行（${aId}）不是这条策略自己唯一的一次冻结运行。`)
  }
  if (!bParams.length) return { kind: 'explicit', a: aMatches[0], b: null }
  if (bId === aId) return invalid('A 和 B 是同一次运行：一次运行不能与它自己比较。')
  const bMatches = own(bId)
  if (bMatches.length !== 1) {
    return invalid(`B 指定的运行（${bId}）不是这条策略自己唯一的一次冻结运行。`)
  }
  return { kind: 'explicit', a: aMatches[0], b: bMatches[0] }
}

/**
 * The pair the address resolves to. A refused address resolves to nothing at all:
 * neither surface may read the latest run's report in place of a run it could not
 * verify, so there is no fallback here beyond the plain address.
 */
export function selectedRuns(
  selection: EvidenceSelection,
  runs: UserStrategyExperiment[],
): { a: UserStrategyExperiment | null; b: UserStrategyExperiment | null } {
  if (selection.kind === 'explicit') return { a: selection.a, b: selection.b }
  if (selection.kind === 'default') return { a: runs.at(-1) ?? null, b: null }
  return { a: null, b: null }
}

/**
 * The address another selection means: exactly these runs, A first. A B that is the
 * run chosen as A cannot stay, and every unrelated parameter already in the address
 * is left where it is.
 */
export function evidenceAddress(
  params: URLSearchParams,
  a: UserStrategyExperiment | null,
  b: UserStrategyExperiment | null,
): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete('context')
  next.delete(EVIDENCE_A)
  next.delete(EVIDENCE_B)
  if (a) next.set(EVIDENCE_A, a.job_id ?? '')
  if (b && b.launch_intent_id !== a?.launch_intent_id) next.set(EVIDENCE_B, b.job_id ?? '')
  return next
}
