import { useState } from "react";
import { download, money, useBuffer, useStudio } from "./context";
import { metrics } from "./model";
import type { Report, Trade } from "./model";
import "./report-overview.css";
import {
  Button,
  Chart,
  DetailLayout,
  Empty,
  Icon,
  Inspector,
  KeyValues,
  LinkButton,
  Metrics,
  Notice,
  PageHeader,
  Panel,
  PhaseStatus,
  Snapshot,
  Tabs,
} from "./ui";

export function Records() {
  const { state, navigate } = useStudio(),
    filter = useBuffer("record-filters", {
      search: "",
      type: "all",
      status: "all",
      order: "newest",
      date: "all",
    }),
    [selection, setSelection] = useState<string[]>([]),
    [page, setPage] = useState(1);
  const entries = [
    ...state.experiments.map((e) => ({
      id: e.id,
      name: e.reportId
        ? `${e.reportId} · ${state.researches.find((r) => r.id === e.researchId)?.name ?? "实验报告"}`
        : e.id,
      researchId: e.researchId,
      type: "experiment",
      status: e.status,
      date: e.createdAt,
      route: e.reportId
        ? `/report/${e.reportId}/overview`
        : `/research/${e.researchId}/experiments`,
      comparable: !!e.reportId,
    })),
    ...state.batches.map((b) => ({
      id: b.id,
      name: `批次 ${b.id}`,
      researchId: b.researchId,
      type: "batch",
      status: b.state,
      date: "2026-10-22 10:42:18",
      route: `/batch/${b.id}`,
      comparable: false,
    })),
    ...state.holdouts.map((h) => ({
      id: h.id,
      name: `留出验证 ${h.id}`,
      researchId: h.researchId,
      type: "holdout",
      status: h.state,
      date: "2026-10-22 10:46:00",
      route: `/holdout/${h.id}`,
      comparable: false,
    })),
    ...state.candidates.map((c) => ({
      id: c.id,
      name: `候选 ${c.id}`,
      researchId: c.researchId,
      type: "candidate",
      status: c.state,
      date: c.decidedAt ?? "2026-10-23 10:50:00",
      route: `/candidate/${c.id}`,
      comparable: false,
    })),
    ...state.researches.map((r) => ({
      id: r.id,
      name: r.name,
      researchId: r.id,
      type: "research",
      status: "saved",
      date: r.updatedAt,
      route: `/research/${r.id}/overview`,
      comparable: false,
    })),
  ];
  const labels: Record<string, string> = {
    experiment: "单次实验",
    batch: "批量实验",
    holdout: "留出验证",
    candidate: "候选",
    research: "研究任务",
  };
  const filtered = entries
    .filter(
      (e) =>
        (filter.value.type === "all" || e.type === filter.value.type) &&
        (filter.value.status === "all" || e.status === filter.value.status) &&
        `${e.name} ${e.id} ${state.researches.find((r) => r.id === e.researchId)?.name}`
          .toLowerCase()
          .includes(filter.value.search.toLowerCase()) &&
        (filter.value.date === "all" || e.date.startsWith("2026-10-23")),
    )
    .sort((a, b) =>
      filter.value.order === "name"
        ? a.name.localeCompare(b.name, "zh")
        : b.date.localeCompare(a.date),
    );
  const set = (key: keyof typeof filter.value, value: string) => {
    filter.update({ ...filter.value, [key]: value });
    setPage(1);
  };
  const selected = state.experiments.filter((e) => selection.includes(e.id)),
    compatible =
      selected.length === 2 &&
      selected[0].researchId === selected[1].researchId &&
      selected[0].input.datasetId === selected[1].input.datasetId &&
      selected[0].input.start === selected[1].input.start &&
      selected[0].input.end === selected[1].input.end;
  return (
    <div className="page">
      <PageHeader
        title="回测记录"
        subtitle="从记录回到输入与证据。"
        actions={
          <Button onClick={() => navigate("/candidates")}>候选中心</Button>
        }
      />
      <div className="toolbar">
        <label className="search-field">
          <Icon name="search" />
          <input
            aria-label="搜索研究、实验或报告"
            placeholder="搜索研究、实验或报告"
            value={filter.value.search}
            onChange={(e) => set("search", e.target.value)}
          />
        </label>
        <select
          aria-label="记录状态"
          value={filter.value.status}
          onChange={(e) => set("status", e.target.value)}
        >
          <option value="all">状态：全部</option>
          <option value="completed">已完成</option>
          <option value="running">运行中</option>
          <option value="failed">已失败</option>
          <option value="unknown">结果未知</option>
          <option value="pending">待审阅</option>
        </select>
        <select
          aria-label="记录日期"
          value={filter.value.date}
          onChange={(e) => set("date", e.target.value)}
        >
          <option value="all">全部日期</option>
          <option value="today">今天 · 10月23日</option>
        </select>
        <select
          aria-label="记录排序"
          value={filter.value.order}
          onChange={(e) => set("order", e.target.value)}
        >
          <option value="newest">最近更新</option>
          <option value="name">名称排序</option>
        </select>
      </div>
      <Tabs
        active={filter.value.type}
        onChange={(v) => set("type", v)}
        items={[
          { id: "all", label: "全部" },
          ...Object.entries(labels).map(([id, label]) => ({ id, label })),
        ]}
      />
      <div className="section-heading">
        <small>
          已选择 {selection.length} 项
          {selection.length === 2 && !compatible
            ? " · 请选择同研究、同数据、同区间的两个完成实验"
            : ""}
        </small>
        <div className="button-row">
          <LinkButton onClick={() => setSelection([])}>清除选择</LinkButton>
          <Button
            kind="primary"
            disabled={!compatible}
            onClick={() => navigate(`/compare/${selection.join("/")}`)}
          >
            比较实验
          </Button>
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>比较</th>
              <th>记录</th>
              <th>关联研究</th>
              <th>类型</th>
              <th>状态</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice((page - 1) * 10, page * 10).map((e) => (
              <tr
                key={e.id}
                className={selection.includes(e.id) ? "selected" : ""}
              >
                <td>
                  <input
                    type="checkbox"
                    aria-label={`选择 ${e.id}`}
                    checked={selection.includes(e.id)}
                    disabled={
                      !e.comparable ||
                      (selection.length === 2 && !selection.includes(e.id))
                    }
                    onChange={() =>
                      setSelection((s) =>
                        s.includes(e.id)
                          ? s.filter((x) => x !== e.id)
                          : [...s, e.id],
                      )
                    }
                  />
                </td>
                <td>
                  <LinkButton onClick={() => navigate(e.route)}>
                    {e.name}
                  </LinkButton>
                </td>
                <td>
                  <LinkButton
                    onClick={() =>
                      navigate(`/research/${e.researchId}/overview`)
                    }
                  >
                    {state.researches.find((r) => r.id === e.researchId)?.name}
                  </LinkButton>
                </td>
                <td>{labels[e.type]}</td>
                <td>
                  <PhaseStatus value={e.status} />
                </td>
                <td>{e.date.slice(5, 16)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!filtered.length && (
          <Empty
            title="没有匹配记录"
            description="修改搜索词或清除筛选条件。"
          />
        )}
      </div>
      <div className="pagination">
        <span>
          {filtered.length} 条记录 · 第 {page} 页
        </span>
        <button
          disabled={page === 1}
          onClick={() => setPage((p) => p - 1)}
          aria-label="上一页"
        >
          ‹
        </button>
        <button
          disabled={page * 10 >= filtered.length}
          onClick={() => setPage((p) => p + 1)}
          aria-label="下一页"
        >
          ›
        </button>
      </div>
    </div>
  );
}

function Trades({ report }: { report: Report }) {
  const { navigate } = useStudio(),
    [side, setSide] = useState("全部"),
    [search, setSearch] = useState(""),
    [page, setPage] = useState(1),
    [selected, setSelected] = useState<Trade | null>(null);
  const rows = report.trades.filter(
    (t) =>
      (side === "全部" || t.side === side) &&
      `${t.date} ${t.symbol}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className={selected ? "with-inspector" : ""}>
      <section>
        <div className="toolbar">
          <h2 style={{ margin: 0 }}>交易记录</h2>
          <select
            aria-label="交易方向"
            value={side}
            onChange={(e) => {
              setSide(e.target.value);
              setPage(1);
            }}
          >
            <option>全部</option>
            <option>买入</option>
            <option>卖出</option>
          </select>
          <label className="search-field">
            <Icon name="search" />
            <input
              aria-label="搜索交易"
              placeholder="搜索日期或标的"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
            />
          </label>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>标的</th>
                <th>方向</th>
                <th>数量</th>
                <th>价格</th>
                <th>费用</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice((page - 1) * 8, page * 8).map((t) => (
                <tr
                  key={t.id}
                  className={t.id === selected?.id ? "selected" : ""}
                >
                  <td>
                    <LinkButton onClick={() => setSelected(t)}>
                      {t.date}
                    </LinkButton>
                  </td>
                  <td>{t.symbol}</td>
                  <td>{t.side}</td>
                  <td>{t.quantity}</td>
                  <td>{money(t.price)}</td>
                  <td>{money(t.fee)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <Empty title="没有匹配交易" />}
        </div>
        <div className="pagination">
          <span>
            共 {rows.length} 条 · 第 {page} 页
          </span>
          <button
            aria-label="交易上一页"
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
          >
            ‹
          </button>
          <button
            aria-label="交易下一页"
            disabled={page * 8 >= rows.length}
            onClick={() => setPage((p) => p + 1)}
          >
            ›
          </button>
        </div>
      </section>
      {selected && (
        <Inspector title="交易详情" onClose={() => setSelected(null)}>
          <KeyValues
            items={[
              ["时间", selected.date],
              ["方向", selected.side],
              ["数量", selected.quantity],
              ["成交价", money(selected.price)],
              ["手续费", money(selected.fee)],
              ["触发信号", selected.signal],
              ["实验", report.experimentId],
              ["草稿", `r${report.input.revision}`],
              ["数据", report.input.datasetId],
            ]}
          />
          <Button
            icon="file"
            onClick={() => navigate(`/report/${report.id}/inputs`)}
          >
            查看冻结输入
          </Button>
          <Notice>本记录来自演示报告，只读。修改输入请新建实验。</Notice>
          <details>
            <summary>查看信号说明</summary>
            <p>
              {selected.signal}，以冻结参数 {report.input.fast}/
              {report.input.slow} 生成示例记录，不代表真实信号计算。
            </p>
          </details>
        </Inspector>
      )}
    </div>
  );
}

export function ReportView({
  id,
  tab = "overview",
}: {
  id: string;
  tab?: string;
}) {
  const { state, navigate, openAgent } = useStudio(),
    r = state.reports.find((r) => r.id === id);
  if (!r)
    return (
      <div className="page">
        <Empty
          title="报告尚不可用"
          description="结果未知时请先查询原请求。"
          action={
            <Button onClick={() => navigate("/records")}>返回记录</Button>
          }
        />
      </div>
    );
  const research = state.researches.find((x) => x.id === r.researchId),
    dataset = state.datasets.find((d) => d.id === r.input.datasetId);
  const content = (
    <>
      <PageHeader
        title={`${research?.name ?? "实验"}报告`}
        subtitle={
          <>
            {r.id} · {r.experimentId} <PhaseStatus value="completed" />
          </>
        }
        breadcrumb={
          <LinkButton onClick={() => navigate("/records")}>
            回测记录 / {r.id}
          </LinkButton>
        }
        actions={
          <>
            <Button
              kind="primary"
              onClick={() => navigate(`/holdout/new?report=${id}`)}
            >
              继续验证
            </Button>
            <Button icon="download" onClick={() => download(`${id}.json`, r)}>
              导出
            </Button>
          </>
        }
      />
      <Tabs
        items={[
          { id: "overview", label: "概览" },
          { id: "trades", label: "交易" },
          { id: "inputs", label: "冻结输入" },
          { id: "files", label: "文件" },
        ]}
        active={tab}
        onChange={(t) => navigate(`/report/${id}/${t}`)}
      />
      {tab === "overview" ? (
        <>
          <Metrics report={r} />
          <Chart
            series={[
              {
                name: `策略 ${r.input.fast}/${r.input.slow}`,
                points: r.equity,
              },
              { name: "基准", points: r.benchmark },
            ]}
          />
          <Panel title="来源与范围">
            <dl className="report-sources">
              <div>
                <dt>策略草稿</dt>
                <dd>
                  <strong>冻结版本 r{r.input.revision}</strong>
                  <span>
                    双均线 {r.input.fast}/{r.input.slow}
                  </span>
                </dd>
              </div>
              <div>
                <dt>数据版本</dt>
                <dd>
                  <strong>{r.input.datasetId}</strong>
                  <span>
                    {dataset?.symbol} · {dataset?.frequency}
                  </span>
                </dd>
              </div>
              <div>
                <dt>样本区间</dt>
                <dd>
                  <span>{r.input.start}</span>
                  <span>至 {r.input.end}</span>
                </dd>
              </div>
            </dl>
            <Notice>
              单次演示回测只反映所选场景，不代表稳健性或未来收益。
            </Notice>
          </Panel>
          <div className="button-row">
            <Button
              onClick={() => navigate(`/batch/new?research=${r.researchId}`)}
            >
              配置参数批次
            </Button>
            <Button onClick={() => navigate("/candidates")}>
              查看候选中心
            </Button>
          </div>
        </>
      ) : tab === "trades" ? (
        <Trades key={id} report={r} />
      ) : tab === "inputs" ? (
        <>
          <Notice>冻结输入只读。当前草稿的后续修改不会改变本报告。</Notice>
          <div className="two-column">
            <Panel title="冻结输入">
              <Snapshot input={r.input} />
            </Panel>
            <Panel title="证据身份">
              <KeyValues
                items={[
                  ["报告", r.id],
                  ["实验", r.experimentId],
                  ["研究", r.researchId],
                  ["生成时间", r.createdAt],
                  ["初始资金", `${money(r.initialCash)} USD`],
                  ["记录类型", "确定性演示报告"],
                ]}
              />
              <Button
                onClick={() => navigate(`/research/${r.researchId}/draft`)}
              >
                查看当前草稿
              </Button>
            </Panel>
          </div>
        </>
      ) : (
        <Panel title="报告文件">
          <p>文件由当前冻结报告导出，可在本地检查。</p>
          <div className="button-row">
            <Button icon="download" onClick={() => download(`${id}.json`, r)}>
              下载报告 JSON
            </Button>
            <Button
              icon="download"
              onClick={() =>
                download(
                  `${id}-trades.csv`,
                  [
                    "id,date,symbol,side,quantity,price,fee",
                    ...r.trades.map((t) =>
                      [
                        t.id,
                        t.date,
                        t.symbol,
                        t.side,
                        t.quantity,
                        t.price,
                        t.fee,
                      ].join(","),
                    ),
                  ].join("\n"),
                  true,
                )
              }
            >
              下载交易 CSV
            </Button>
            <Button
              icon="download"
              onClick={() => download(`${id}-inputs.json`, r.input)}
            >
              下载冻结输入
            </Button>
          </div>
        </Panel>
      )}
    </>
  );
  return tab === "overview" ? (
    <DetailLayout
      className="report-overview"
      objectId={id}
      context={`报告 ${id} · r${r.input.revision} · 只读证据`}
      references={[
        { label: "[1] 指标汇总", route: `/report/${id}/overview` },
        { label: "[2] 冻结输入", route: `/report/${id}/inputs` },
        { label: "检查交易", route: `/report/${id}/trades` },
      ]}
    >
      {content}
    </DetailLayout>
  ) : (
    <div className="page">
      <div className="button-row" style={{ justifyContent: "flex-end" }}>
        <Button icon="panel" onClick={openAgent}>
          询问此报告
        </Button>
      </div>
      {content}
    </div>
  );
}

export function Compare({ left, right }: { left: string; right: string }) {
  const { state, navigate } = useStudio(),
    a = state.reports.find((r) => r.experimentId === left),
    b = state.reports.find((r) => r.experimentId === right);
  if (!a || !b)
    return (
      <Empty
        title="请选择两个已完成实验"
        action={<Button onClick={() => navigate("/records")}>返回记录</Button>}
      />
    );
  const compatible =
    a.researchId === b.researchId &&
    a.input.datasetId === b.input.datasetId &&
    a.input.start === b.input.start &&
    a.input.end === b.input.end;
  if (!compatible)
    return (
      <div className="page">
        <Notice tone="warn">
          两个实验的研究、数据或区间不同，不能在此比较。请重新选择。
        </Notice>
        <Button onClick={() => navigate("/records")}>返回选择</Button>
      </div>
    );
  const am = metrics(a),
    bm = metrics(b);
  return (
    <div className="page">
      <PageHeader
        title="比较实验"
        subtitle={`${left} 与 ${right} · 同一数据版本`}
        breadcrumb={
          <LinkButton
            onClick={() => navigate(`/research/${a.researchId}/experiments`)}
          >
            研究 / 实验
          </LinkButton>
        }
        actions={
          <>
            <Button
              icon="download"
              onClick={() =>
                download(`compare-${left}-${right}.json`, { left: a, right: b })
              }
            >
              导出比较
            </Button>
            <Button
              kind="primary"
              onClick={() => navigate(`/report/${b.id}/overview`)}
            >
              查看 {right} 报告
            </Button>
          </>
        }
      />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>指标</th>
              <th>{left}</th>
              <th>{right}</th>
              <th>差异</th>
            </tr>
          </thead>
          <tbody>
            {(
              [
                ["net", "净收益", "%"],
                ["drawdown", "最大回撤", "%"],
                ["count", "交易次数", ""],
                ["cost", "费用影响", "%"],
              ] as const
            ).map(([key, name, unit]) => (
              <tr key={key}>
                <td>{name}</td>
                <td>
                  {am[key].toFixed(key === "count" ? 0 : 2)}
                  {unit}
                </td>
                <td>
                  {bm[key].toFixed(key === "count" ? 0 : 2)}
                  {unit}
                </td>
                <td>
                  {(bm[key] - am[key]).toFixed(key === "count" ? 0 : 2)}
                  {unit ? " 个百分点" : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="spacing-top">
        <Chart
          series={[
            { name: left, points: a.equity },
            { name: right, points: b.equity },
          ]}
        />
      </div>
      <div className="two-column">
        <Panel title="输入差异">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>参数</th>
                  <th>{left}</th>
                  <th>{right}</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ["revision", "草稿修订"],
                    ["fast", "快均线"],
                    ["slow", "慢均线"],
                    ["fee", "手续费（%）"],
                    ["slippage", "滑点（%）"],
                    ["datasetId", "数据版本"],
                  ] as const
                ).map(([key, label]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td>{a.input[key]}</td>
                    <td>{b.input[key]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel title="研究助手 · 演示解读">
          <p>
            这两个实验使用相同数据与区间。净收益差异{" "}
            {(bm.net - am.net).toFixed(2)} 个百分点，交易次数差异{" "}
            {bm.count - am.count} 笔。请结合成本和回撤检查差异。
          </p>
          <Notice>这里只比较本次场景表现，需要更多证据才能判断。</Notice>
          <div className="button-row">
            <Button onClick={() => navigate(`/report/${a.id}/overview`)}>
              查看 {left} 报告
            </Button>
            <Button onClick={() => navigate(`/holdout/new?report=${b.id}`)}>
              开始后续验证
            </Button>
          </div>
        </Panel>
      </div>
    </div>
  );
}
