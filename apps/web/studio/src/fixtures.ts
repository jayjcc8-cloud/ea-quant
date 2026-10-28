import type { DemoState, Draft, Report, Scenario, SimConfig } from "./model";

export const DEMO_TIME = "2026-10-23 14:32:08";
export const baseDraft: Draft = {
  revision: 4,
  fast: 20,
  slow: 60,
  allocation: 100,
  fee: 0.1,
  slippage: 0.05,
  datasetId: "AAPL-daily-v3",
  templateId: "ma-trend",
  start: "2024-01-02",
  end: "2025-12-31",
};
export const defaultSimConfig: SimConfig = {
  cash: 100000,
  fee: 0.1,
  slippage: 0.05,
  maxOrder: 10000,
  maxPosition: 20,
  maxPositions: 3,
  frequency: "日线",
};
export const sampleCSV =
  "Date,Open,High,Low,Close,Volume\n2024-01-02,185.64,187.15,184.22,186.19,82416900\n2024-01-03,184.83,185.98,182.37,184.40,77805200\n2024-01-04,183.90,184.12,181.50,,91234800";
export function makeReport(
  id: string,
  experimentId: string,
  researchId: string,
  input: Draft,
  variant = 0,
  symbol = "AAPL",
): Report {
  const holdout = input.start >= "2026-01-01";
  const anchors = holdout
    ? [1, 1.006, 1.011, 1.004, 1.01, 0.983, 0.969, 0.991, 1.019]
    : variant % 2
      ? [
          1, 0.954, 0.91, 0.872, 0.908, 0.925, 0.943, 0.965, 0.95, 0.973, 1.01,
          1.084,
        ]
      : [
          1, 0.973, 0.946, 0.894, 0.922, 0.95, 0.966, 0.984, 0.972, 1.01, 1.039,
          1.079,
        ];
  const start = new Date(input.start).getTime(),
    end = new Date(input.end).getTime();
  const date = (n: number, total: number) =>
    new Date(start + ((end - start) * n) / (total - 1))
      .toISOString()
      .slice(0, 10);
  const equity = anchors.map((value, n) => ({
    date: date(n, anchors.length),
    value: Math.round(value * 100000),
  }));
  const count = holdout ? 9 : variant % 2 ? 42 : 31;
  return {
    id,
    experimentId,
    researchId,
    input: structuredClone(input),
    initialCash: 100000,
    equity,
    benchmark: anchors.map((_, n) => ({
      date: date(n, anchors.length),
      value: Math.round(
        (1 +
          ((holdout ? 0.012 : 0.05) * n) / (anchors.length - 1) -
          0.09 * Math.sin((Math.PI * n) / (anchors.length - 1))) *
          100000,
      ),
    })),
    trades: Array.from({ length: count }, (_, n) => {
      const price = Math.round((185.2 + n * 1.31) * 100) / 100,
        quantity = n % 3 ? 20 : 50;
      return {
        id: `${id}-T${n + 1}`,
        date: `${date(n, count)} 09:30`,
        symbol,
        side: n % 2 ? "卖出" : "买入",
        price,
        quantity,
        fee: Math.round(price * quantity * input.fee) / 100,
        signal: n % 2 ? "快均线下穿" : "快均线上穿",
      };
    }),
    createdAt: DEMO_TIME,
  };
}
export function createFixture(scenario: Scenario = "baseline"): DemoState {
  const report = makeReport("REP-021", "E-021", "R-001", baseDraft);
  const comparison = makeReport(
    "REP-020",
    "E-020",
    "R-001",
    { ...baseDraft, revision: 3, fast: 30 },
    1,
  );
  const outside = makeReport("REP-025", "E-025", "R-001", {
    ...baseDraft,
    datasetId: "AAPL-daily-v4",
    start: "2026-01-02",
    end: "2026-06-30",
  });
  const batchAlternative = makeReport(
    "REP-024",
    "E-024",
    "R-001",
    { ...baseDraft, fast: 30 },
    1,
  );
  const acceptedOutside = makeReport("REP-026", "E-026", "R-001", {
    ...outside.input,
    revision: 3,
    fast: 30,
  });
  const state: DemoState = {
    livePreview: {
      candidate: "C-006",
      cash: 10000,
      order: 500,
      loss: 200,
      symbol: "AAPL",
      orderType: "限价",
      allowOpen: false,
      session: "09:30–16:00",
      expiry: "当日有效",
      stopLoss: 5,
    },
    liveStop: "running",
    schema: 1,
    sequence: 100,
    scenario,
    researches: [
      {
        id: "R-001",
        name: "双均线趋势研究",
        goal: "AAPL 日线双均线趋势策略，在加入成本后是否仍有稳定表现？",
        notes: "先检查基线，再比较参数与成本。所有结果为演示场景。",
        draft: { ...baseDraft },
        history: [{ ...baseDraft, revision: 3, fast: 30 }, { ...baseDraft }],
        updatedAt: DEMO_TIME,
      },
      {
        id: "R-002",
        name: "成本敏感性验证",
        goal: "观察不同成本假设对结果的影响。",
        notes: "",
        draft: { ...baseDraft },
        history: [{ ...baseDraft }],
        updatedAt: "2026-10-22 11:08:00",
      },
      {
        id: "R-003",
        name: "波动过滤观察",
        goal: "观察高波动期间的信号变化。",
        notes: "",
        draft: { ...baseDraft, revision: 1, datasetId: "", templateId: "" },
        history: [],
        updatedAt: "2026-10-20 10:00:00",
      },
    ],
    proposals: [
      {
        id: "P-008",
        researchId: "R-001",
        base: { ...baseDraft },
        suggested: { ...baseDraft, fast: 30 },
        state: "pending",
        explanation: "延长快均线窗口，观察更平滑的信号对交易次数与成本的影响。",
      },
    ],
    datasets: [
      {
        id: "AAPL-daily-v3",
        name: "AAPL-daily-v3",
        symbol: "AAPL",
        frequency: "日线",
        start: "2024-01-02",
        end: "2025-12-31",
        rows: 503,
        source: "内置场景",
        status: "ready",
        fields: ["timestamp", "open", "high", "low", "close", "volume"],
        samples: [
          ["2024-01-02", "185.64", "187.15", "184.22", "186.19", "82416900"],
          ["2024-01-03", "184.83", "185.98", "182.37", "184.40", "77805200"],
        ],
        fingerprint: "demo-aapl-v3",
      },
      {
        id: "AAPL-daily-v4",
        name: "AAPL-daily-v4",
        symbol: "AAPL",
        frequency: "日线",
        start: "2026-01-02",
        end: "2026-06-30",
        rows: 124,
        source: "内置场景",
        status: "ready",
        fields: ["timestamp", "open", "high", "low", "close", "volume"],
        samples: [["2026-01-02", "200", "203", "198", "201", "43000000"]],
        fingerprint: "demo-aapl-v4",
      },
      {
        id: "BTCUSDT-hourly-v2",
        name: "BTCUSDT-hourly-v2",
        symbol: "BTC/USDT",
        frequency: "1小时",
        start: "2024-01-01",
        end: "2025-12-31",
        rows: 17520,
        source: "内置场景",
        status: "review",
        fields: ["timestamp", "open", "high", "low", "close", "volume"],
        samples: [],
        fingerprint: "demo-btc-v2",
      },
    ],
    templates: [
      {
        id: "ma-trend",
        name: "双均线趋势",
        kind: "规则模板",
        scope: "日线趋势",
        description: "用快慢均线交叉描述趋势变化。",
        rule: "快均线上穿慢均线时做多，下穿时平仓。",
        source: "内置示例",
        defaults: { ...baseDraft },
      },
      {
        id: "ma-observe",
        name: "均线偏离观察",
        kind: "规则模板",
        scope: "日线观察",
        description: "观察价格与均线的偏离，作为研究起点。",
        rule: "使用均线交叉规则进行基础观察；未验证盈利能力。",
        source: "内置示例",
        defaults: { ...baseDraft, fast: 10, slow: 40 },
      },
    ],
    reports: [report, comparison, outside, batchAlternative, acceptedOutside],
    experiments: [
      report,
      comparison,
      outside,
      batchAlternative,
      acceptedOutside,
    ].map((r) => ({
      id: r.experimentId,
      researchId: r.researchId,
      input: r.input,
      status: "completed",
      progress: 100,
      requestId: `REQ-${r.experimentId}`,
      reportId: r.id,
      createdAt: DEMO_TIME,
    })),
    batches: [
      {
        id: "B-004",
        researchId: "R-001",
        input: { ...baseDraft },
        combinations: [
          { fast: 20, slow: 60 },
          { fast: 30, slow: 60 },
          { fast: 40, slow: 90 },
        ],
        experimentIds: ["E-021", "E-024", "E-023"],
        state: "completed",
      },
    ],
    holdouts: [
      {
        id: "H-001",
        researchId: "R-001",
        sourceReportId: "REP-020",
        reportId: "REP-026",
        datasetId: "AAPL-daily-v4",
        start: "2026-01-02",
        end: "2026-06-30",
        state: "completed",
        input: { ...acceptedOutside.input },
      },
      {
        id: "H-002",
        researchId: "R-001",
        sourceReportId: "REP-021",
        reportId: "REP-025",
        datasetId: "AAPL-daily-v4",
        start: "2026-01-02",
        end: "2026-06-30",
        state: "completed",
        input: { ...outside.input },
      },
    ],
    candidates: [
      {
        id: "C-007",
        researchId: "R-001",
        reportId: "REP-021",
        holdoutId: "H-002",
        state: "pending",
        reason: "",
      },
      {
        id: "C-006",
        researchId: "R-001",
        reportId: "REP-020",
        holdoutId: "H-001",
        state: "accepted",
        reason: "演示历史决定；只用于检查模拟流程。",
        decidedAt: "2026-10-22 14:20:00",
      },
    ],
    simulations: [
      {
        id: "SIM-003",
        candidateId: "C-006",
        configVersion: 1,
        config: { ...defaultSimConfig },
        input: { ...comparison.input },
        status: "running",
        observedAt: DEMO_TIME,
        stale: false,
        cash: 97243,
        quantity: 20,
        cost: 200,
        quote: 199.85,
        equity: [100000, 100550, 100420, 100880, 101240].map((value, i) => ({
          date: `2026-10-${19 + i}`,
          value,
        })),
        events: [
          {
            id: "EVT-089",
            time: DEMO_TIME,
            type: "模拟成交",
            summary: "AAPL 买入20股，成交价 200.00 USD",
          },
          {
            id: "EVT-088",
            time: "2026-10-23 14:32:06",
            type: "策略计算",
            summary: "目标仓位更新：20股",
          },
          {
            id: "EVT-087",
            time: "2026-10-23 14:32:04",
            type: "行情",
            summary: "AAPL 日线已接收，收盘价 199.85 USD",
          },
        ],
      },
    ],
    requests: [],
    connections: [
      {
        id: "MODEL-001",
        name: "本地开发连接",
        provider: "OpenAI 兼容",
        endpoint: "https://api.example.com/v1",
        model: "research-model",
        isDefault: true,
        timeout: 30,
        tested: true,
      },
      {
        id: "MODEL-002",
        name: "备用连接",
        provider: "OpenAI 兼容",
        endpoint: "https://backup.example.com/v1",
        model: "research-model",
        isDefault: false,
        timeout: 30,
        tested: false,
      },
    ],
    messages: [],
    snoozed: [],
    preferences: {
      theme: "light",
      fontSize: 15,
      density: "standard",
      scale: 100,
      reducedMotion: false,
      workspace: "Research/QUINE",
      nextWorkspace: "Research/QUINE",
    },
  };
  state.experiments.push({
    id: "E-023",
    researchId: "R-001",
    input: { ...baseDraft, fast: 40, slow: 90 },
    status: "failed",
    progress: 20,
    requestId: "REQ-E-023",
    error: "本次任务读取数据失败。固定输入未改变，可明确重试此成员。",
    createdAt: DEMO_TIME,
  });
  if (scenario === "empty") {
    state.researches = [];
    state.proposals = [];
    state.experiments = [];
    state.reports = [];
    state.batches = [];
    state.holdouts = [];
    state.candidates = [];
    state.simulations = [];
  }
  if (scenario === "conflict") {
    state.researches[0].draft = { ...baseDraft, revision: 5, fast: 25 };
    state.researches[0].history.push({ ...state.researches[0].draft });
  }
  if (scenario === "unknown") {
    state.requests.push({
      id: "REQ-022",
      key: "seed-unknown",
      operation: "experiment",
      objectId: "E-022",
      state: "unknown",
      queries: 0,
      submittedAt: DEMO_TIME,
    });
    state.experiments.unshift({
      id: "E-022",
      researchId: "R-001",
      input: { ...baseDraft },
      status: "unknown",
      progress: 10,
      requestId: "REQ-022",
      createdAt: DEMO_TIME,
    });
  }
  if (scenario === "stale" || scenario === "stop-unknown")
    state.simulations[0].stale = true;
  if (scenario === "stop-unknown") {
    state.simulations[0].stopRequestId = "STOP-008";
    state.requests.push({
      id: "STOP-008",
      key: "seed-stop",
      operation: "stop",
      objectId: "SIM-003",
      state: "unknown",
      submittedAt: DEMO_TIME,
      queries: 0,
    });
  }
  return state;
}
