import { describe, expect, it } from "vitest";
import {
  createFixture,
  baseDraft,
  defaultSimConfig,
  sampleCSV,
} from "./fixtures";
import { applyCommand, ScenarioAdapter } from "./scenario";
import { parseCSV, validateCSV, autoMap } from "./csv";
import { metrics } from "./model";

describe("产品边界与冻结证据", () => {
  it("应用提案产生r5，不运行实验，不改变r4报告", () => {
    const initial = createFixture(),
      frozen = structuredClone(initial.reports[0]);
    const { state } = applyCommand(initial, {
      type: "proposal.apply",
      id: "P-008",
    });
    expect(state.researches[0].draft).toMatchObject({ revision: 5, fast: 30 });
    expect(state.experiments).toEqual(initial.experiments);
    expect(state.reports[0]).toEqual(frozen);
    expect(initial.researches[0].draft.revision).toBe(4);
  });
  it("过期提案保留基线与用户修改；明确重新生成不执行", () => {
    const initial = createFixture("conflict");
    expect(() =>
      applyCommand(initial, { type: "proposal.apply", id: "P-008" }),
    ).toThrow("草稿已更新");
    const { state } = applyCommand(initial, {
      type: "proposal.generate",
      id: "R-001",
    });
    expect(state.proposals[0].base.fast).toBe(20);
    expect(state.proposals.at(-1)?.base.fast).toBe(25);
    expect(state.experiments).toEqual(initial.experiments);
  });
  it("确认冻结当前版本；重复请求仅一个实验", () => {
    const initial = createFixture();
    const first = applyCommand(initial, {
      type: "experiment.start",
      id: "R-001",
      expected: 4,
      key: "same-request",
    });
    const duplicate = applyCommand(first.state, {
      type: "experiment.start",
      id: "R-001",
      expected: 4,
      key: "same-request",
    });
    expect(duplicate.state.experiments).toHaveLength(
      initial.experiments.length + 1,
    );
    expect(duplicate.result.id).toBe(first.result.id);
    const changed = applyCommand(duplicate.state, {
      type: "draft.save",
      id: "R-001",
      expected: 4,
      draft: { ...baseDraft, fast: 25 },
    });
    expect(
      changed.state.experiments.find((e) => e.id === first.result.id)?.input
        .fast,
    ).toBe(20);
  });
  it("未知结果查询保留身份，刷新也不创建额外实验", () => {
    const initial = createFixture("unknown");
    const queried = applyCommand(initial, {
      type: "request.query",
      id: "REQ-022",
    }).state;
    const resumed = applyCommand(JSON.parse(JSON.stringify(queried)), {
      type: "request.query",
      id: "REQ-022",
    }).state;
    expect(resumed.experiments).toHaveLength(initial.experiments.length);
    expect(resumed.requests[0].id).toBe("REQ-022");
    expect(resumed.experiments.find((e) => e.id === "E-022")?.status).toBe(
      "completed",
    );
  });
  it("候选决定和配置保存不启动；启动校验保存版本", () => {
    const initial = createFixture();
    expect(() =>
      applyCommand(initial, {
        type: "candidate.decide",
        id: "C-007",
        decision: "accepted",
        reason: "",
      }),
    ).toThrow("理由");
    const decided = applyCommand(initial, {
      type: "candidate.decide",
      id: "C-007",
      decision: "accepted",
      reason: "已审阅，保留样本限制。",
    }).state;
    expect(decided.simulations).toEqual(initial.simulations);
    const saved = applyCommand(decided, {
      type: "simulation.save",
      candidateId: "C-007",
      config: defaultSimConfig,
    });
    expect(saved.state.simulations.at(-1)?.status).toBe("saved");
    expect(saved.state.requests).toHaveLength(0);
    expect(() =>
      applyCommand(saved.state, {
        type: "simulation.start",
        id: saved.result.id,
        expected: 0,
        key: "start",
      }),
    ).toThrow("配置版本");
  });
  it("停止不是已停止；陈旧不会改变进程；查询确认只用原请求", () => {
    const initial = createFixture("stale");
    const requested = applyCommand(initial, {
      type: "simulation.stop",
      id: "SIM-003",
      key: "stop-once",
    });
    expect(requested.state.simulations[0].status).toBe("running");
    const repeated = applyCommand(requested.state, {
      type: "simulation.stop",
      id: "SIM-003",
      key: "different-click",
    });
    expect(repeated.state.requests).toHaveLength(1);
    const confirmed = applyCommand(repeated.state, {
      type: "request.query",
      id: requested.result.requestId!,
    }).state;
    expect(confirmed.simulations[0].status).toBe("stopped");
    expect(confirmed.requests).toHaveLength(1);
    expect(confirmed.simulations[0].stale).toBe(false);
  });
  it("批次校验整个组合；留出不能与样本重叠", () => {
    const initial = createFixture();
    expect(() =>
      applyCommand(initial, {
        type: "batch.save",
        researchId: "R-001",
        expected: 4,
        combinations: [
          { fast: 20, slow: 60 },
          { fast: 80, slow: 60 },
        ],
      }),
    ).toThrow("快均线");
    expect(() =>
      applyCommand(initial, {
        type: "holdout.save",
        reportId: "REP-021",
        datasetId: "AAPL-daily-v4",
        start: "2025-12-01",
        end: "2026-06-30",
      }),
    ).toThrow("留出");
    expect(initial.experiments).toEqual(createFixture().experiments);
  });
  it("指标/图表/交易和模拟资金闭合", () => {
    const initial = createFixture(),
      report = initial.reports[0],
      sim = initial.simulations[0];
    expect(metrics(report).net).toBeCloseTo(7.9);
    expect(metrics(report).drawdown).toBeCloseTo(-10.6);
    expect(metrics(report).count).toBe(report.trades.length);
    expect(metrics(report).fees).toBe(
      report.trades.reduce((s, t) => s + t.fee, 0),
    );
    expect(sim.cash + sim.quantity * sim.quote).toBe(sim.equity.at(-1)?.value);
    expect(sim.quantity * (sim.quote - sim.cost)).toBeCloseTo(-3);
  });
  it("基线批次版本与候选证据可相互追溯", () => {
    const s = createFixture();
    for (const b of s.batches)
      for (const id of b.experimentIds) {
        const input = s.experiments.find((e) => e.id === id)!.input;
        expect(input.revision).toBe(b.input.revision);
        expect(input.datasetId).toBe(b.input.datasetId);
      }
    for (const c of s.candidates)
      expect(s.holdouts.find((h) => h.id === c.holdoutId)?.sourceReportId).toBe(
        c.reportId,
      );
  });
  it("批次保存可恢复、执行期间未完成、完成由成员汇总", () => {
    const saved = applyCommand(createFixture(), {
      type: "batch.save",
      researchId: "R-001",
      expected: 4,
      combinations: [
        { fast: 20, slow: 60 },
        { fast: 30, slow: 60 },
      ],
    });
    let s = applyCommand(JSON.parse(JSON.stringify(saved.state)), {
      type: "batch.start",
      id: saved.result.id,
      key: "batch",
    }).state;
    expect(s.batches.at(-1)?.state).toBe("running");
    for (let n = 0; n < 6; n++) s = applyCommand(s, { type: "tick" }).state;
    expect(s.batches.at(-1)?.state).toBe("completed");
  });
  it("复位取消尚未提交的旧场景动作", async () => {
    const a = new ScenarioAdapter(undefined, 10);
    const old = a.execute({ type: "proposal.apply", id: "P-008" });
    a.reset("empty");
    await expect(old).rejects.toThrow("场景已复位");
    expect(a.getSnapshot().researches).toHaveLength(0);
  });
  it("非AAPL绑定标的传播到冻结报告交易", () => {
    const s = createFixture();
    s.datasets[0].symbol = "MSFT";
    const started = applyCommand(s, {
      type: "experiment.start",
      id: "R-001",
      expected: 4,
      key: "msft",
    });
    let next = started.state;
    for (let n = 0; n < 6; n++)
      next = applyCommand(next, { type: "tick" }).state;
    expect(next.reports.at(-1)?.trades.every((t) => t.symbol === "MSFT")).toBe(
      true,
    );
  });
  it("保存存储失败不发布内存状态、不谎报成功", async () => {
    const adapter = new ScenarioAdapter(
      {
        load: () => null,
        save: () => {
          throw new Error("quota");
        },
      },
      0,
    );
    await expect(
      adapter.execute({ type: "proposal.apply", id: "P-008" }),
    ).rejects.toThrow("无法保存");
    expect(adapter.getSnapshot().researches[0].draft.revision).toBe(4);
  });
});

describe("本地CSV导入", () => {
  it("映射、问题行定位、修复后发布所需数据", () => {
    const parsed = parseCSV(sampleCSV),
      mapping = autoMap(parsed.headers);
    const invalid = validateCSV(parsed, mapping);
    expect(invalid.errors.some((e) => e.row === 4 && e.field === "close")).toBe(
      true,
    );
    const fixed = parseCSV(sampleCSV.replace("181.50,,", "181.50,182.70,"));
    expect(validateCSV(fixed, autoMap(fixed.headers)).errors).toEqual([]);
  });
  it("重复时间、非法日期、负数、OHLC与引号不能静默通过", () => {
    const text =
      "Date,Open,High,Low,Close,Volume\n2024-02-30,10,9,8,10,100\n2024-01-02,10,12,8,11,-1\n2024-01-02,10,12,8,11,100";
    const result = validateCSV(parseCSV(text), autoMap(parseCSV(text).headers));
    expect(result.errors.length).toBeGreaterThanOrEqual(4);
    expect(parseCSV('a,b\n"with,comma","quoted"').rows[0]).toEqual([
      "with,comma",
      "quoted",
    ]);
  });
});
