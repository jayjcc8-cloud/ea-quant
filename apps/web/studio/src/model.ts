export type Phase =
  | "saved"
  | "running"
  | "completed"
  | "failed"
  | "unknown"
  | "stopped";
export type Scenario =
  | "baseline"
  | "empty"
  | "read-error"
  | "conflict"
  | "unknown"
  | "stale"
  | "stop-unknown"
  | "model-error";
export interface Parameters {
  fast: number;
  slow: number;
  allocation: number;
  fee: number;
  slippage: number;
}
export interface Draft extends Parameters {
  revision: number;
  datasetId: string;
  templateId: string;
  start: string;
  end: string;
}
export interface Research {
  id: string;
  name: string;
  goal: string;
  notes: string;
  draft: Draft;
  history: Draft[];
  updatedAt: string;
}
export interface Proposal {
  id: string;
  researchId: string;
  base: Draft;
  suggested: Parameters;
  state: "pending" | "applied" | "rejected" | "superseded";
  explanation: string;
}
export interface Dataset {
  id: string;
  name: string;
  symbol: string;
  frequency: string;
  start: string;
  end: string;
  rows: number;
  source: string;
  status: "ready" | "review";
  samples: string[][];
  fields: string[];
  fingerprint: string;
}
export interface Template {
  id: string;
  name: string;
  kind: string;
  scope: string;
  description: string;
  rule: string;
  source: string;
  defaults: Parameters;
}
export interface Trade {
  id: string;
  date: string;
  symbol: string;
  side: "买入" | "卖出";
  quantity: number;
  price: number;
  fee: number;
  signal: string;
}
export interface Point {
  date: string;
  value: number;
}
export interface Report {
  id: string;
  experimentId: string;
  researchId: string;
  input: Draft;
  initialCash: number;
  equity: Point[];
  benchmark: Point[];
  trades: Trade[];
  createdAt: string;
}
export interface Experiment {
  id: string;
  researchId: string;
  input: Draft;
  status: Phase;
  progress: number;
  requestId: string;
  reportId?: string;
  error?: string;
  createdAt: string;
}
export interface Request {
  id: string;
  key: string;
  operation: "experiment" | "start" | "stop" | "experiment-stop";
  objectId: string;
  state: "pending" | "unknown" | "confirmed";
  submittedAt: string;
  queriedAt?: string;
  queries: number;
}
export interface Batch {
  id: string;
  researchId: string;
  input: Draft;
  combinations: { fast: number; slow: number }[];
  experimentIds: string[];
  state: "saved" | "running" | "completed";
}
export interface Holdout {
  id: string;
  researchId: string;
  sourceReportId: string;
  reportId?: string;
  datasetId: string;
  start: string;
  end: string;
  state: "saved" | "completed";
  input: Draft;
}
export interface Candidate {
  id: string;
  researchId: string;
  reportId: string;
  holdoutId: string;
  state: "pending" | "accepted" | "rejected";
  reason: string;
  decidedAt?: string;
}
export interface SimConfig {
  cash: number;
  fee: number;
  slippage: number;
  maxOrder: number;
  maxPosition: number;
  maxPositions: number;
  frequency: string;
}
export interface SimEvent {
  id: string;
  time: string;
  type: string;
  summary: string;
}
export interface Simulation {
  id: string;
  candidateId: string;
  configVersion: number;
  config: SimConfig;
  input: Draft;
  status: "saved" | "running" | "stopped";
  observedAt?: string;
  stale: boolean;
  cash: number;
  quantity: number;
  cost: number;
  quote: number;
  equity: Point[];
  events: SimEvent[];
  startRequestId?: string;
  stopRequestId?: string;
}
export interface Connection {
  id: string;
  name: string;
  provider: string;
  endpoint: string;
  model: string;
  isDefault: boolean;
  timeout: number;
  tested: boolean;
}
export interface AgentMessage {
  id: string;
  objectId: string;
  role: "user" | "assistant";
  text: string;
  references: { label: string; route: string }[];
}
export interface Preferences {
  theme: "light" | "dark" | "system";
  fontSize: number;
  density: "compact" | "standard" | "comfortable";
  scale: number;
  reducedMotion: boolean;
  workspace: string;
  nextWorkspace: string;
}
export interface LivePreview {
  candidate: string;
  cash: number;
  order: number;
  loss: number;
  symbol: string;
  orderType: string;
  allowOpen: boolean;
  session: string;
  expiry: string;
  stopLoss: number;
}
export interface DemoState {
  livePreview: LivePreview;
  liveStop: "running" | "pending" | "unknown" | "stopped";
  schema: 1;
  sequence: number;
  scenario: Scenario;
  researches: Research[];
  proposals: Proposal[];
  datasets: Dataset[];
  templates: Template[];
  experiments: Experiment[];
  reports: Report[];
  batches: Batch[];
  holdouts: Holdout[];
  candidates: Candidate[];
  simulations: Simulation[];
  requests: Request[];
  connections: Connection[];
  messages: AgentMessage[];
  snoozed: string[];
  preferences: Preferences;
}
export type Command =
  | { type: "live.preview.save"; config: LivePreview }
  | { type: "live.preview.stop" | "live.preview.query" }
  | {
      type: "research.create";
      goal: string;
      datasetId?: string;
      templateId?: string;
    }
  | { type: "research.save"; id: string; goal: string; notes: string }
  | { type: "draft.save"; id: string; expected: number; draft: Draft }
  | { type: "proposal.generate"; id: string; text?: string }
  | { type: "proposal.apply" | "proposal.reject"; id: string }
  | { type: "experiment.start"; id: string; expected: number; key: string }
  | { type: "experiment.stop"; id: string; key: string }
  | { type: "experiment.retry"; id: string; key: string }
  | { type: "request.query"; id: string }
  | {
      type: "batch.save";
      id?: string;
      researchId: string;
      expected: number;
      combinations: { fast: number; slow: number }[];
    }
  | { type: "batch.start"; id: string; key: string }
  | {
      type: "holdout.save";
      id?: string;
      reportId: string;
      datasetId: string;
      start: string;
      end: string;
    }
  | { type: "holdout.start"; id: string; key: string }
  | { type: "candidate.create"; holdoutId: string }
  | {
      type: "candidate.decide";
      id: string;
      decision: "accepted" | "rejected";
      reason: string;
    }
  | {
      type: "simulation.save";
      id?: string;
      candidateId: string;
      config: SimConfig;
    }
  | { type: "simulation.start"; id: string; expected: number; key: string }
  | { type: "simulation.stop"; id: string; key: string }
  | { type: "simulation.refresh"; id: string }
  | { type: "dataset.publish"; dataset: Omit<Dataset, "id" | "fingerprint"> }
  | { type: "template.register"; name: string; rule: string }
  | { type: "connection.save"; connection: Connection }
  | {
      type: "agent.ask";
      objectId: string;
      text: string;
      references: AgentMessage["references"];
    }
  | { type: "attention.snooze"; id: string; snoozed: boolean }
  | { type: "preferences.save"; preferences: Preferences }
  | { type: "tick" };
export interface ActionResult {
  id: string;
  requestId?: string;
}
export class ProductError extends Error {
  constructor(
    public code: "validation" | "conflict" | "read" | "storage" | "missing",
    message: string,
  ) {
    super(message);
    this.name = "ProductError";
  }
}
export function metrics(report: Report) {
  let peak = report.initialCash,
    drawdown = 0;
  for (const point of report.equity) {
    peak = Math.max(peak, point.value);
    drawdown = Math.min(drawdown, point.value / peak - 1);
  }
  const fees = report.trades.reduce((sum, trade) => sum + trade.fee, 0);
  return {
    net:
      ((report.equity.at(-1)?.value ?? report.initialCash) /
        report.initialCash -
        1) *
      100,
    drawdown: drawdown * 100,
    count: report.trades.length,
    fees,
    cost: (-fees / report.initialCash) * 100,
  };
}
export function validateDraft(draft: Draft) {
  if (
    !Number.isSafeInteger(draft.fast) ||
    !Number.isSafeInteger(draft.slow) ||
    draft.fast < 1 ||
    draft.slow <= draft.fast ||
    draft.slow > 1000
  )
    throw new ProductError(
      "validation",
      "快均线须为正整数且小于慢均线，慢均线不超过 1000。",
    );
  if (
    ![draft.fee, draft.slippage, draft.allocation].every(Number.isFinite) ||
    draft.fee < 0 ||
    draft.fee > 5 ||
    draft.slippage < 0 ||
    draft.slippage > 5 ||
    draft.allocation <= 0 ||
    draft.allocation > 100
  )
    throw new ProductError(
      "validation",
      "仓位须在 0–100% 之间；手续费与滑点须在 0–5% 之间。",
    );
  if (!draft.datasetId || !draft.templateId)
    throw new ProductError("validation", "请先绑定策略模板和数据版本。");
  if (!draft.start || !draft.end || draft.start >= draft.end)
    throw new ProductError(
      "validation",
      "请选择有效的起止日期，开始日期须早于结束日期。",
    );
}
