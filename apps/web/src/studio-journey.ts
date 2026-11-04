import type { ResearchSummary } from './research-workbench-api'
import { isIntentDraft, type UserStrategy, type UserStrategyExperiment, type UserStrategyDraftDocument } from './user-strategies-api'

type NextStep = { kind: ResearchSummary['next_step']; href: string; label: string; detail: string }
const unresolved = new Set(['pending', 'confirmed', 'running', 'stopping', 'unknown', 'unconfirmed'])
export const holdsExperiment = (item: UserStrategyExperiment) => unresolved.has(item.status)
  && !(item.status === 'unknown' && item.slot_held === false && item.recovery?.status === 'released')

export function experimentStatus(status: string): string {
  return ({ pending: '确认待核对', dispatched: '已发送，等待回执', confirmed: '已确认，等待运行',
    accepted: '已受理，等待运行', running: '运行中', stopping: '正在停止，尚未确认停止',
    created: '已登记运行', succeeded: '运行完成', failed: '运行失败', unknown: '结果未知',
    unconfirmed: '确认结果未知', cancelled: '已停止', interrupted_before_dispatch: '派发前中断' } as Record<string, string>)[status] ?? `状态：${status}`
}
function nextStep(id: string, kind: ResearchSummary['next_step'], detail: string, jobId?: string | null): NextStep {
  const base = `/research/${encodeURIComponent(id)}`
  const steps = {
    draft: { href: `${base}/draft`, label: '绑定策略与数据' },
    review: { href: `${base}/proposal`, label: '审阅待处理提案' },
    experiment: { href: `${base}/confirm`, label: '准备一次实验' },
    inspect: { href: `${base}/experiments`, label: '核对原实验' },
    evidence: { href: `/report/${encodeURIComponent(jobId ?? '')}/overview`, label: '阅读冻结报告' },
  }
  return { kind, ...steps[kind], detail }
}

/** Translate the server collection projection into navigation, never an execution decision. */
export function summaryNextStep(item: ResearchSummary): NextStep {
  const latest = item.latest_experiment
  const inspecting = latest && (latest.read_error || ['pending', 'dispatched', 'confirmed', 'running', 'stopping', 'unknown', 'unconfirmed'].includes(latest.launch_status)
    || ['accepted', 'running', 'stopping', 'failed'].includes(latest.job_status ?? ''))
  if (inspecting || item.next_step === 'inspect' || (item.next_step === 'evidence' && (!latest?.report_ready || !latest.job_id))) {
    const status = latest && unresolved.has(latest.launch_status) ? latest.launch_status : latest?.job_status ?? latest?.launch_status
    const action = nextStep(item.user_strategy_id, 'inspect', latest?.read_error ?? (status ? experimentStatus(status) : '请核对已有实验记录'))
    if (!latest?.read_error && ['accepted', 'confirmed', 'running', 'stopping'].includes(status ?? '')) action.label = '查看运行进度'
    return action
  }
  const details = {
    draft: '尚未绑定策略与数据', review: `${item.draft.pending_proposals} 项提案等待审阅`,
    experiment: '核对参数与成本后，单独确认一次实验', inspect: '请核对已有实验记录',
    evidence: `报告可读 · 冻结草稿 r${latest?.draft_revision ?? '未记录'}`,
  }
  return nextStep(item.user_strategy_id, item.next_step, details[item.next_step], latest?.job_id)
}

/** Detail actions use the same saved document shown on this page, not a second lifecycle. */
export function detailNextStep(strategy: UserStrategy): NextStep {
  const id = strategy.user_strategy_id, latest = strategy.experiments.at(-1)
  const held = strategy.experiments.find(holdsExperiment)
  if (held) {
    const action = nextStep(id, 'inspect', `${experimentStatus(held.status)}。请查看原实验，确认新的状态后再继续。`)
    if (['confirmed', 'running', 'stopping'].includes(held.status)) action.label = '查看运行进度'
    return action
  }
  if (latest?.status === 'created' && latest.report_ready && latest.job_id) {
    return nextStep(id, 'evidence', `报告对应冻结草稿 r${latest.payload.draft_revision}；当前草稿为 r${strategy.draft.revision}。`, latest.job_id)
  }
  const pending = strategy.proposals.filter(item => item.status === 'open').length
  if (pending) return nextStep(id, 'review', `${pending} 项提案等待审阅。应用只更新草稿，不会运行实验。`)
  if (isIntentDraft(strategy.draft)) return nextStep(id, 'draft', '尚未绑定策略与数据')
  if (latest) return nextStep(id, 'inspect', `${experimentStatus(latest.status)}。先核对这次运行的记录与结果。`)
  return nextStep(id, 'experiment', '策略与数据已绑定。核对参数、成本和冻结输入后，单独确认一次实验。')
}

/** Proposal values refer to today's saved draft; before/after belong to the proposal itself. */
export function currentProposalValue(draft: UserStrategyDraftDocument, path: string): string | number | undefined {
  if (isIntentDraft(draft)) return undefined
  if (path === 'initial_cash') return draft.initial_cash
  if (path.startsWith('parameters.')) {
    const key = path.slice('parameters.'.length)
    return Object.hasOwn(draft.parameters, key) ? draft.parameters[key] : undefined
  }
  return undefined
}
