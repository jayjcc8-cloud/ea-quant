import { baseDraft, createFixture, DEMO_TIME, makeReport } from "./fixtures";
import { ProductError, validateDraft, metrics } from "./model";
import type {
  ActionResult,
  Command,
  DemoState,
  Draft,
  Experiment,
  Request,
  Scenario,
} from "./model";

export const STORAGE_KEY = "quine.frontend-v3.demo.v1";
const clone = <T>(value: T): T => structuredClone(value);
function found<T>(value: T | undefined, name: string): T {
  if (!value) throw new ProductError("missing", `${name}不存在，请返回列表。`);
  return value;
}
export function applyCommand(
  initial: DemoState,
  command: Command,
): { state: DemoState; result: ActionResult } {
  const s = clone(initial),
    result: ActionResult = { id: "" };
  const next = (prefix: string) => `${prefix}-${++s.sequence}`;
  const research = (id: string) =>
    found(
      s.researches.find((r) => r.id === id),
      "研究",
    );
  const report = (id: string) =>
    found(
      s.reports.find((r) => r.id === id),
      "报告",
    );
  const sim = (id: string) =>
    found(
      s.simulations.find((r) => r.id === id),
      "模拟实例",
    );
  const checkRevision = (draft: Draft, expected: number) => {
    if (draft.revision !== expected)
      throw new ProductError(
        "conflict",
        "草稿已更新，请保留修改并重新审阅当前版本。",
      );
  };
  const complete = (e: Experiment) => {
    if (e.reportId) return;
    e.status = "completed";
    e.progress = 100;
    e.reportId = next("REP");
    s.reports.push(
      makeReport(
        e.reportId,
        e.id,
        e.researchId,
        e.input,
        e.input.fast >= 30 ? 1 : 0,
        found(
          s.datasets.find((d) => d.id === e.input.datasetId),
          "数据版本",
        ).symbol,
      ),
    );
    const request = s.requests.find((r) => r.id === e.requestId);
    if (request) request.state = "confirmed";
  };
  const request = (
    key: string,
    operation: Request["operation"],
    objectId: string,
  ) => {
    const r: Request = {
      id: next(operation.includes("stop") ? "STOP" : "REQ"),
      key,
      operation,
      objectId,
      state: "pending",
      submittedAt: DEMO_TIME,
      queries: 0,
    };
    s.requests.push(r);
    return r;
  };
  const startExperiment = (researchId: string, input: Draft, key: string) => {
    const id = next("E"),
      req = request(key, "experiment", id);
    const unknown = s.scenario === "unknown";
    if (unknown) req.state = "unknown";
    const e: Experiment = {
      id,
      researchId,
      input: clone(input),
      status: unknown ? "unknown" : "running",
      progress: 5,
      requestId: req.id,
      createdAt: DEMO_TIME,
    };
    s.experiments.unshift(e);
    return e;
  };
  if ("key" in command) {
    const prior = s.requests.find((r) => r.key === command.key);
    if (prior)
      return {
        state: initial,
        result: { id: prior.objectId, requestId: prior.id },
      };
  }
  switch (command.type) {
    case "live.preview.save": {
      const c = command.config;
      if (
        !s.candidates.some((x) => x.id === c.candidate) ||
        ![c.cash, c.order, c.loss, c.stopLoss].every(
          (v) => Number.isFinite(v) && v > 0,
        ) ||
        c.order > c.cash ||
        c.stopLoss > 100
      )
        throw new ProductError("validation", "请检查预览资金、候选与限额。");
      s.livePreview = clone(c);
      result.id = "PREVIEW";
      break;
    }
    case "live.preview.stop": {
      if (s.liveStop === "running") s.liveStop = "pending";
      result.id = "PREVIEW-STOP-001";
      break;
    }
    case "live.preview.query": {
      if (s.liveStop === "pending") s.liveStop = "unknown";
      else if (s.liveStop === "unknown") s.liveStop = "stopped";
      result.id = "PREVIEW-STOP-001";
      break;
    }
    case "research.create": {
      if (!command.goal.trim())
        throw new ProductError("validation", "请写下要验证的问题。");
      const id = next("R"),
        template = s.templates.find((t) => t.id === command.templateId),
        data = s.datasets.find((d) => d.id === command.datasetId);
      const draft = {
        ...baseDraft,
        ...template?.defaults,
        revision: 1,
        datasetId: data?.id ?? "",
        templateId: template?.id ?? "",
        start: data?.start ?? baseDraft.start,
        end: data?.end ?? baseDraft.end,
      };
      s.researches.unshift({
        id,
        name: command.goal.trim().slice(0, 24),
        goal: command.goal.trim(),
        notes: "",
        draft,
        history: [clone(draft)],
        updatedAt: DEMO_TIME,
      });
      result.id = id;
      break;
    }
    case "research.save": {
      const r = research(command.id);
      r.goal = command.goal;
      r.notes = command.notes;
      r.updatedAt = DEMO_TIME;
      result.id = r.id;
      break;
    }
    case "draft.save": {
      const r = research(command.id);
      checkRevision(r.draft, command.expected);
      validateDraft(command.draft);
      const data = found(
        s.datasets.find((d) => d.id === command.draft.datasetId),
        "数据版本",
      );
      if (
        data.status !== "ready" ||
        command.draft.start < data.start ||
        command.draft.end > data.end
      )
        throw new ProductError(
          "validation",
          "数据待检查或所选区间超出数据范围。",
        );
      r.draft = { ...clone(command.draft), revision: r.draft.revision + 1 };
      r.history.push(clone(r.draft));
      r.updatedAt = DEMO_TIME;
      result.id = r.id;
      break;
    }
    case "proposal.generate": {
      const r = research(command.id);
      validateDraft(r.draft);
      s.proposals
        .filter((p) => p.researchId === r.id && p.state === "pending")
        .forEach((p) => {
          p.state = "superseded";
        });
      const id = next("P");
      s.proposals.push({
        id,
        researchId: r.id,
        base: clone(r.draft),
        suggested: {
          fast: Math.min(r.draft.slow - 1, r.draft.fast === 30 ? 20 : 30),
          slow: r.draft.slow,
          fee: r.draft.fee,
          slippage: r.draft.slippage,
          allocation: r.draft.allocation,
        },
        state: "pending",
        explanation:
          command.text?.trim() ||
          "只调整快均线窗口，保留数据、区间与成本，便于观察参数差异。",
      });
      result.id = id;
      break;
    }
    case "proposal.apply":
    case "proposal.reject": {
      const p = found(
          s.proposals.find((p) => p.id === command.id),
          "提案",
        ),
        r = research(p.researchId);
      if (p.state !== "pending")
        throw new ProductError("conflict", "这份提案已处理，请查看当前草稿。");
      if (command.type === "proposal.apply") {
        checkRevision(r.draft, p.base.revision);
        const draft = {
          ...r.draft,
          ...p.suggested,
          revision: r.draft.revision + 1,
        };
        validateDraft(draft);
        r.draft = draft;
        r.history.push(clone(draft));
        p.state = "applied";
      } else p.state = "rejected";
      result.id = r.id;
      break;
    }
    case "experiment.start": {
      const r = research(command.id);
      checkRevision(r.draft, command.expected);
      validateDraft(r.draft);
      const e = startExperiment(r.id, r.draft, command.key);
      result.id = e.id;
      result.requestId = e.requestId;
      break;
    }
    case "experiment.retry": {
      const e = found(
        s.experiments.find((e) => e.id === command.id),
        "实验",
      );
      if (e.status !== "failed")
        throw new ProductError(
          "validation",
          "只有已确认失败的任务可以明确重试。未知结果请查询原请求。",
        );
      const replacement = startExperiment(e.researchId, e.input, command.key);
      s.batches.forEach((b) => {
        b.experimentIds = b.experimentIds.map((id) =>
          id === e.id ? replacement.id : id,
        );
      });
      result.id = replacement.id;
      break;
    }
    case "experiment.stop": {
      const e = found(
        s.experiments.find((e) => e.id === command.id),
        "实验",
      );
      const existing = s.requests.find(
        (r) => r.objectId === e.id && r.operation === "experiment-stop",
      );
      if (existing) {
        result.id = e.id;
        result.requestId = existing.id;
        break;
      }
      if (e.status !== "running")
        throw new ProductError("validation", "仅运行中的实验可请求停止。");
      const req = request(command.key, "experiment-stop", e.id);
      result.id = e.id;
      result.requestId = req.id;
      break;
    }
    case "request.query": {
      const req = found(
        s.requests.find((r) => r.id === command.id),
        "请求",
      );
      req.queries++;
      req.queriedAt = DEMO_TIME;
      result.id = req.objectId;
      result.requestId = req.id;
      if (req.state === "confirmed") break;
      if (req.operation === "stop") {
        const instance = sim(req.objectId);
        instance.status = "stopped";
        instance.stale = false;
        instance.observedAt = DEMO_TIME;
        instance.events.unshift({
          id: next("EVT"),
          time: DEMO_TIME,
          type: "停止确认",
          summary: `原请求 ${req.id} 已确认，实例已停止；持仓快照保留。`,
        });
        req.state = "confirmed";
      } else if (req.operation === "experiment-stop") {
        const e = found(
          s.experiments.find((e) => e.id === req.objectId),
          "实验",
        );
        e.status = "stopped";
        req.state = "confirmed";
      } else if (req.operation === "experiment") {
        const e = found(
          s.experiments.find((e) => e.id === req.objectId),
          "实验",
        );
        if (req.queries === 1 && e.status === "unknown") {
          e.status = "running";
          e.progress = 45;
          req.state = "pending";
        } else complete(e);
      }
      break;
    }
    case "batch.save": {
      const r = research(command.researchId);
      checkRevision(r.draft, command.expected);
      validateDraft(r.draft);
      if (command.combinations.length < 2 || command.combinations.length > 10)
        throw new ProductError("validation", "请选择 2–10 个参数组合。");
      command.combinations.forEach((c) => validateDraft({ ...r.draft, ...c }));
      if (
        new Set(command.combinations.map((c) => `${c.fast}/${c.slow}`)).size !==
        command.combinations.length
      )
        throw new ProductError("validation", "参数组合不能重复。");
      const id = command.id ?? next("B"),
        existing = s.batches.find((b) => b.id === id);
      if (existing && existing.state !== "saved")
        throw new ProductError(
          "conflict",
          "已执行批次不可修改，请另存新批次。",
        );
      const b = {
        id,
        researchId: r.id,
        input: clone(r.draft),
        combinations: clone(command.combinations),
        experimentIds: [],
        state: "saved" as const,
      };
      s.batches = [...s.batches.filter((b) => b.id !== id), b];
      result.id = id;
      break;
    }
    case "batch.start": {
      const b = found(
        s.batches.find((b) => b.id === command.id),
        "批次",
      );
      result.id = b.id;
      if (b.state !== "saved") break;
      b.experimentIds = b.combinations.map(
        (c, i) =>
          startExperiment(
            b.researchId,
            { ...b.input, ...c },
            `${command.key}-${i}`,
          ).id,
      );
      b.state = "running";
      break;
    }
    case "holdout.save": {
      const source = report(command.reportId),
        data = found(
          s.datasets.find((d) => d.id === command.datasetId),
          "数据版本",
        );
      if (
        command.start <= source.input.end ||
        command.start >= command.end ||
        command.start < data.start ||
        command.end > data.end ||
        data.status !== "ready"
      )
        throw new ProductError(
          "validation",
          "留出区间必须在样本之后、互不重叠，并处于可用数据范围。",
        );
      const id = command.id ?? next("H");
      const prior = s.holdouts.find((h) => h.id === id);
      if (prior?.state === "completed")
        throw new ProductError("conflict", "已冻结留出不可修改，请新建。");
      s.holdouts = [
        ...s.holdouts.filter((h) => h.id !== id),
        {
          id,
          researchId: source.researchId,
          sourceReportId: source.id,
          datasetId: data.id,
          start: command.start,
          end: command.end,
          state: "saved",
          input: {
            ...clone(source.input),
            datasetId: data.id,
            start: command.start,
            end: command.end,
          },
        },
      ];
      result.id = id;
      break;
    }
    case "holdout.start": {
      const h = found(
        s.holdouts.find((h) => h.id === command.id),
        "留出",
      );
      result.id = h.id;
      if (h.state === "completed") break;
      const e = startExperiment(h.researchId, h.input, command.key);
      complete(e);
      h.reportId = e.reportId;
      h.state = "completed";
      break;
    }
    case "candidate.create": {
      const h = found(
        s.holdouts.find((h) => h.id === command.holdoutId),
        "留出",
      );
      if (!h.reportId)
        throw new ProductError("validation", "请先完成留出并检查两份报告。");
      const existing = s.candidates.find(
        (c) => c.holdoutId === h.id && c.reportId === h.sourceReportId,
      );
      if (existing) {
        result.id = existing.id;
        break;
      }
      const id = next("C");
      s.candidates.unshift({
        id,
        researchId: h.researchId,
        reportId: h.sourceReportId,
        holdoutId: h.id,
        state: "pending",
        reason: "",
      });
      result.id = id;
      break;
    }
    case "candidate.decide": {
      const c = found(
        s.candidates.find((c) => c.id === command.id),
        "候选",
      );
      if (c.state !== "pending")
        throw new ProductError("conflict", "已记录的决定不可覆盖。");
      if (!command.reason.trim() || command.reason.length > 500)
        throw new ProductError("validation", "请填写 1–500 字的决定理由。");
      report(c.reportId);
      const h = found(
        s.holdouts.find((h) => h.id === c.holdoutId),
        "留出证据",
      );
      if (!h.reportId)
        throw new ProductError("validation", "留出报告缺失，暂不能决定。");
      c.state = command.decision;
      c.reason = command.reason.trim();
      c.decidedAt = DEMO_TIME;
      result.id = c.id;
      break;
    }
    case "simulation.save": {
      const c = found(
        s.candidates.find((c) => c.id === command.candidateId),
        "候选",
      );
      if (c.state !== "accepted")
        throw new ProductError(
          "validation",
          "请先审阅并接受候选，再配置模拟。",
        );
      const config = command.config;
      if (
        ![
          config.cash,
          config.maxOrder,
          config.maxPosition,
          config.maxPositions,
          config.fee,
          config.slippage,
        ].every(Number.isFinite) ||
        config.cash <= 0 ||
        config.maxOrder <= 0 ||
        config.maxOrder > config.cash ||
        config.maxPosition <= 0 ||
        config.maxPosition > 100 ||
        !Number.isInteger(config.maxPositions) ||
        config.maxPositions < 1 ||
        config.maxPositions > 100 ||
        config.fee < 0 ||
        config.fee > 5 ||
        config.slippage < 0 ||
        config.slippage > 5
      )
        throw new ProductError(
          "validation",
          "请检查资金、成本和限额；单笔金额不能超过初始资金。",
        );
      const prior = s.simulations.find((i) => i.id === command.id);
      if (prior && prior.status !== "saved")
        throw new ProductError(
          "conflict",
          "已启动实例的配置不可覆盖，请创建新配置。",
        );
      const id = prior?.id ?? next("SIM");
      const instance = {
        id,
        candidateId: c.id,
        configVersion: (prior?.configVersion ?? 0) + 1,
        config: clone(config),
        input: clone(report(c.reportId).input),
        status: "saved" as const,
        stale: false,
        cash: config.cash,
        quantity: 0,
        cost: 0,
        quote: 199.85,
        equity: [{ date: "2026-10-23", value: config.cash }],
        events: [],
      };
      s.simulations = [...s.simulations.filter((i) => i.id !== id), instance];
      result.id = id;
      break;
    }
    case "simulation.start": {
      const instance = sim(command.id);
      if (instance.configVersion !== command.expected)
        throw new ProductError(
          "conflict",
          "配置版本已改变，请重新审阅保存后的配置版本。",
        );
      if (instance.startRequestId) {
        result.id = instance.id;
        result.requestId = instance.startRequestId;
        break;
      }
      if (instance.status !== "saved")
        throw new ProductError("validation", "该实例已启动。");
      const req = request(command.key, "start", instance.id);
      req.state = "confirmed";
      instance.startRequestId = req.id;
      instance.status = "running";
      instance.observedAt = DEMO_TIME;
      instance.events.unshift({
        id: next("EVT"),
        time: DEMO_TIME,
        type: "状态",
        summary: `按配置 v${instance.configVersion} 开始演示观测；未产生真实成交。`,
      });
      result.id = instance.id;
      result.requestId = req.id;
      break;
    }
    case "simulation.stop": {
      const instance = sim(command.id);
      result.id = instance.id;
      if (instance.stopRequestId) {
        result.requestId = instance.stopRequestId;
        break;
      }
      if (instance.status !== "running")
        throw new ProductError("validation", "只有运行中的实例可请求停止。");
      const req = request(command.key, "stop", instance.id);
      if (s.scenario === "stop-unknown") req.state = "unknown";
      instance.stopRequestId = req.id;
      result.requestId = req.id;
      break;
    }
    case "simulation.refresh": {
      const instance = sim(command.id);
      instance.stale = false;
      if (instance.status !== "saved") instance.observedAt = DEMO_TIME;
      result.id = instance.id;
      break;
    }
    case "dataset.publish": {
      if (
        command.dataset.status !== "ready" ||
        !command.dataset.rows ||
        !command.dataset.name.trim()
      )
        throw new ProductError("validation", "先完成字段映射和数据校验。");
      const id = next("DATA");
      s.datasets.unshift({
        ...clone(command.dataset),
        id,
        fingerprint: `demo-import-${id}`,
      });
      result.id = id;
      break;
    }
    case "template.register": {
      if (!command.name.trim() || !command.rule.trim())
        throw new ProductError("validation", "请填写名称与规则说明。");
      const id = next("TPL");
      s.templates.push({
        id,
        name: command.name.trim(),
        kind: "规则模板",
        scope: "日线观察",
        description: command.rule,
        rule: command.rule,
        source: "本地登记 · 仅元数据",
        defaults: { ...baseDraft },
      });
      result.id = id;
      break;
    }
    case "connection.save": {
      const c = clone(command.connection);
      if (
        !c.name.trim() ||
        !c.model.trim() ||
        !/^https?:\/\/[^\s]+$/.test(c.endpoint) ||
        c.timeout < 1 ||
        c.timeout > 300
      )
        throw new ProductError(
          "validation",
          "请检查名称、模型、地址格式和超时（1–300秒）。",
        );
      if (!c.id) c.id = next("MODEL");
      if (c.isDefault)
        s.connections.forEach((x) => {
          x.isDefault = false;
        });
      s.connections = [...s.connections.filter((x) => x.id !== c.id), c];
      result.id = c.id;
      break;
    }
    case "agent.ask": {
      if (!command.text.trim())
        throw new ProductError("validation", "请先输入问题。");
      s.messages.push({
        id: next("MSG"),
        objectId: command.objectId,
        role: "user",
        text: command.text.trim(),
        references: [],
      });
      const r = s.reports.find((r) => r.id === command.objectId),
        m = r ? metrics(r) : null;
      const answer = m
        ? `演示解读：本报告净收益 ${m.net.toFixed(1)}%，最大回撤 ${m.drawdown.toFixed(1)}%，共 ${m.count} 笔成交。单次样本不足以判断稳健性。可检查冻结输入或设计留出验证。`
        : "演示任务已整理：先检查当前对象与冻结证据；修改参数请生成可审阅提案，执行操作仍需单独确认。此回复来自场景数据，未调用模型。";
      const id = next("MSG");
      s.messages.push({
        id,
        objectId: command.objectId,
        role: "assistant",
        text: answer,
        references: command.references,
      });
      result.id = id;
      break;
    }
    case "attention.snooze": {
      s.snoozed = s.snoozed.filter((id) => id !== command.id);
      if (command.snoozed) s.snoozed.push(command.id);
      result.id = command.id;
      break;
    }
    case "preferences.save": {
      s.preferences = clone(command.preferences);
      result.id = "preferences";
      break;
    }
    case "tick": {
      let changed = false;
      s.experiments
        .filter(
          (e) =>
            e.status === "running" &&
            !s.requests.some(
              (r) => r.objectId === e.id && r.operation === "experiment-stop",
            ),
        )
        .forEach((e) => {
          changed = true;
          e.progress = Math.min(100, e.progress + 18);
          if (e.progress === 100) complete(e);
        });
      if (!changed) return { state: initial, result };
      break;
    }
  }
  s.batches.forEach((b) => {
    if (b.experimentIds.length)
      b.state = b.experimentIds.every((id) =>
        ["completed", "failed", "stopped"].includes(
          s.experiments.find((e) => e.id === id)?.status ?? "unknown",
        ),
      )
        ? "completed"
        : "running";
  });
  return { state: s, result };
}

interface StoragePort {
  load: () => string | null;
  save: (value: string) => void;
}
export class ScenarioAdapter {
  private state: DemoState;
  private listeners = new Set<() => void>();
  private epoch = 0;
  private pending = new Map<string, Promise<ActionResult>>();
  private readFailed = false;
  private modelFailed = false;
  readonly storageWarning: string;
  constructor(
    private storage?: StoragePort,
    private latency = 260,
  ) {
    this.storageWarning = "";
    try {
      const raw = storage?.load();
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && parsed.schema !== 1) throw new Error("schema");
      this.state = parsed ?? createFixture();
    } catch {
      this.state = createFixture();
      this.storageWarning =
        "演示存储无法读取。当前为临时基线；请在验收工具复位前保留需要的演示数据。";
    }
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private commit(state: DemoState) {
    if (this.state === state) return;
    try {
      this.storage?.save(JSON.stringify(state));
    } catch {
      throw new ProductError(
        "storage",
        "无法保存演示数据，请检查浏览器存储。输入已保留，请重试。",
      );
    }
    this.state = state;
    this.listeners.forEach((l) => l());
  }
  async read(signal?: AbortSignal) {
    await new Promise((resolve) => setTimeout(resolve, this.latency));
    if (signal?.aborted) throw new DOMException("取消读取", "AbortError");
    if (this.state.scenario === "read-error" && !this.readFailed) {
      this.readFailed = true;
      throw new ProductError(
        "read",
        "无法读取演示目录，已保留你的选择。请重试读取。",
      );
    }
    return this.state;
  }
  execute(command: Command): Promise<ActionResult> {
    const key = JSON.stringify(command);
    if (this.pending.has(key)) return this.pending.get(key)!;
    const epoch = this.epoch;
    const task = (async () => {
      await new Promise((resolve) =>
        setTimeout(resolve, command.type === "tick" ? 0 : this.latency),
      );
      if (epoch !== this.epoch)
        throw new ProductError("conflict", "场景已复位，本次操作已取消。");
      const changed = applyCommand(this.state, command);
      this.commit(changed.state);
      return changed.result;
    })().finally(() => {
      if (this.pending.get(key) === task) this.pending.delete(key);
    });
    this.pending.set(key, task);
    return task;
  }
  async testConnection() {
    await new Promise((resolve) => setTimeout(resolve, this.latency + 200));
    if (this.state.scenario === "model-error" && !this.modelFailed) {
      this.modelFailed = true;
      throw new ProductError(
        "read",
        "演示连接测试失败：连接超时。输入已保留，可重试。",
      );
    }
    return "演示连接测试成功；尚未保存，没有发送网络请求。";
  }
  reset(scenario: Scenario) {
    this.epoch++;
    this.pending.clear();
    this.readFailed = false;
    this.modelFailed = false;
    this.commit(createFixture(scenario));
  }
}
export const adapter = new ScenarioAdapter(
  typeof window === "undefined"
    ? undefined
    : {
        load: () => localStorage.getItem(STORAGE_KEY),
        save: (value) => localStorage.setItem(STORAGE_KEY, value),
      },
);
