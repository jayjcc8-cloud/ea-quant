import { useState } from "react";
import {
  useAction,
  useBuffer,
  useStudio,
  useUnsaved,
  requestKey,
  percent,
} from "./context";
import { metrics } from "./model";
import {
  ActionError,
  Button,
  Chart,
  DetailLayout,
  Dialog,
  Empty,
  Field,
  Icon,
  KeyValues,
  LinkButton,
  Notice,
  PageHeader,
  Panel,
  PhaseStatus,
  Snapshot,
  Tabs,
} from "./ui";

export function BatchConfig() {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    researchId =
      new URLSearchParams(location.hash.split("?")[1]).get("research") ??
      state.researches[0]?.id,
    research = state.researches.find((r) => r.id === researchId);
  const buffer = useBuffer(`batch-${researchId}`, [
      { fast: 20, slow: 60, selected: true },
      { fast: 30, slow: 60, selected: true },
      { fast: 40, slow: 90, selected: true },
    ]),
    [savedId, setSavedId] = useState(""),
    [savedText, setSavedText] = useState(JSON.stringify(buffer.value)),
    [confirm, setConfirm] = useState(false),
    [key] = useState(requestKey);
  const selected = buffer.value
      .filter((c) => c.selected)
      .map(({ fast, slow }) => ({ fast, slow })),
    dirty = JSON.stringify(buffer.value) !== savedText;
  const save = async () => {
    if (!research) return false;
    const result = await action.execute({
      type: "batch.save",
      id: savedId || undefined,
      researchId,
      expected: research.draft.revision,
      combinations: selected,
    });
    if (result) {
      setSavedId(result.id);
      setSavedText(JSON.stringify(buffer.value));
      toast("批次配置已保存，尚未启动。");
    }
    return !!result;
  };
  useUnsaved(dirty, save, () => {
    buffer.clear();
    setSavedText(JSON.stringify(buffer.value));
  });
  if (!research) return <Empty title="请先创建研究" />;
  return (
    <div className="page">
      <PageHeader
        title="批次实验"
        subtitle="固定数据与成本，只比较你选择的参数。"
        breadcrumb={
          <LinkButton
            onClick={() => navigate(`/research/${researchId}/experiments`)}
          >
            {research.name} / 实验 / 新建批次
          </LinkButton>
        }
        actions={
          <Button disabled={action.busy} onClick={() => void save()}>
            保存配置
          </Button>
        }
      />
      <div className="with-inspector">
        <div>
          <Panel title="固定条件">
            <Snapshot input={research.draft} />
          </Panel>
          <Panel title="参数组合">
            <p className="muted">
              快均线必须小于慢均线，选择 2–10 个不同组合。
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>选择</th>
                    <th>快均线（周期）</th>
                    <th>慢均线（周期）</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {buffer.value.map((c, i) => (
                    <tr key={i}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择组合 ${i + 1}`}
                          checked={c.selected}
                          onChange={(e) =>
                            buffer.update(
                              buffer.value.map((x, j) =>
                                j === i
                                  ? { ...x, selected: e.target.checked }
                                  : x,
                              ),
                            )
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`组合 ${i + 1} 快均线`}
                          type="number"
                          min="1"
                          value={!Number.isFinite(c.fast) ? "" : c.fast}
                          onChange={(e) =>
                            buffer.update(
                              buffer.value.map((x, j) =>
                                j === i
                                  ? {
                                      ...x,
                                      fast:
                                        e.target.value === ""
                                          ? NaN
                                          : Number(e.target.value),
                                    }
                                  : x,
                              ),
                            )
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label={`组合 ${i + 1} 慢均线`}
                          type="number"
                          min="2"
                          value={!Number.isFinite(c.slow) ? "" : c.slow}
                          onChange={(e) =>
                            buffer.update(
                              buffer.value.map((x, j) =>
                                j === i
                                  ? {
                                      ...x,
                                      slow:
                                        e.target.value === ""
                                          ? NaN
                                          : Number(e.target.value),
                                    }
                                  : x,
                              ),
                            )
                          }
                        />
                      </td>
                      <td>
                        <Button
                          aria-label={`删除组合 ${i + 1}`}
                          icon="trash"
                          onClick={() =>
                            buffer.update(
                              buffer.value.filter((_, j) => j !== i),
                            )
                          }
                        >
                          删除
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Button
              icon="plus"
              disabled={buffer.value.length >= 10}
              onClick={() =>
                buffer.update([
                  ...buffer.value,
                  { fast: 45, slow: 100, selected: true },
                ])
              }
            >
              添加组合
            </Button>
            <ActionError error={action.error || buffer.bufferError} />
          </Panel>
        </div>
        <Panel title="批次预览">
          <h2>{selected.length} 个实验</h2>
          <KeyValues
            items={[
              ["数据", research.draft.datasetId],
              ["草稿", `r${research.draft.revision}`],
              ["手续费", `${research.draft.fee}%`],
              ["滑点", `${research.draft.slippage}%`],
            ]}
          />
          <ol>
            {selected.map((c, i) => (
              <li key={i}>
                快 {c.fast} / 慢 {c.slow}
              </li>
            ))}
          </ol>
          <Notice>本次比较只用于观察参数敏感性，不自动接受候选。</Notice>
          <small>{savedId ? `${savedId} · 配置已保存` : "尚未保存"}</small>
          <div className="dialog-actions">
            <Button
              onClick={() => navigate(`/research/${researchId}/experiments`)}
            >
              取消
            </Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={async () => {
                if (await save()) setConfirm(true);
              }}
            >
              审阅并确认
            </Button>
          </div>
        </Panel>
      </div>
      {confirm && (
        <Dialog title="确认批次输入" onClose={() => setConfirm(false)}>
          <p>
            {savedId} · {selected.length} 个实验。固定数据{" "}
            {research.draft.datasetId} 与草稿 r{research.draft.revision}。
          </p>
          <KeyValues
            items={selected.map((c, i) => [
              `实验 ${i + 1}`,
              `快 ${c.fast} / 慢 ${c.slow}`,
            ])}
          />
          <Notice>确认后仅运行这些组合；后续运行需要再次明确确认。</Notice>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setConfirm(false)}>返回修改</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute(
                  { type: "batch.start", id: savedId, key },
                  (r) => {
                    buffer.clear();
                    navigate(`/batch/${r.id}`);
                  },
                )
              }
            >
              确认运行批次
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function BatchResults({ id }: { id: string }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    b = state.batches.find((b) => b.id === id),
    [selected, setSelected] = useState<string[]>([]),
    [status, setStatus] = useState("all"),
    [search, setSearch] = useState(""),
    [sort, setSort] = useState("net"),
    [errorId, setErrorId] = useState(""),
    [resume, setResume] = useState(false),
    [resumeKey] = useState(requestKey);
  if (!b) return <Empty title="批次不存在" />;
  if (b.state === "saved")
    return (
      <div className="page">
        <PageHeader title="批次配置已保存" subtitle={b.id} />
        <Panel title="冻结条件">
          <Snapshot input={b.input} />
          <KeyValues
            items={b.combinations.map((c, i) => [
              `组合 ${i + 1}`,
              `${c.fast} / ${c.slow}`,
            ])}
          />
        </Panel>
        <Button kind="primary" onClick={() => setResume(true)}>
          继续审阅批次
        </Button>
        {resume && (
          <Dialog title="确认运行已保存批次" onClose={() => setResume(false)}>
            <Snapshot input={b.input} />
            <p>{b.combinations.length} 个固定组合。保存本身尚未运行。</p>
            <ActionError error={action.error} />
            <div className="dialog-actions">
              <Button onClick={() => setResume(false)}>取消</Button>
              <Button
                kind="primary"
                disabled={action.busy}
                onClick={() =>
                  void action.execute(
                    { type: "batch.start", id, key: resumeKey },
                    () => setResume(false),
                  )
                }
              >
                确认运行批次
              </Button>
            </div>
          </Dialog>
        )}
      </div>
    );
  const rows = b.experimentIds
      .map((id) => state.experiments.find((e) => e.id === id)!)
      .filter(Boolean),
    visible = rows
      .filter(
        (e) =>
          (status === "all" || e.status === status) &&
          `${e.id} ${e.input.fast}/${e.input.slow}`.includes(search),
      )
      .sort((a, c) => {
        const ar = state.reports.find((r) => r.id === a.reportId),
          cr = state.reports.find((r) => r.id === c.reportId);
        if (!ar) return 1;
        if (!cr) return -1;
        return (
          metrics(cr)[sort === "net" ? "net" : "drawdown"] -
          metrics(ar)[sort === "net" ? "net" : "drawdown"]
        );
      });
  return (
    <div className="page">
      <PageHeader
        title="批次结果"
        subtitle={`固定数据 ${b.input.datasetId} · ${rows.length} 个实验`}
        breadcrumb={`研究 / 批次 ${id}`}
        actions={
          <Button
            icon="settings"
            onClick={() => navigate(`/batch/new?research=${b.researchId}`)}
          >
            重新配置
          </Button>
        }
      />
      <div className="section-heading">
        <p>
          {rows.filter((e) => e.status === "completed").length} 已完成{" "}
          {rows.filter((e) => e.status === "failed").length} 失败{" "}
          {rows.filter((e) => e.status === "running").length} 运行中
        </p>
        <small>演示任务状态</small>
      </div>
      <div className="toolbar">
        <select
          aria-label="批次成员状态"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="all">全部状态</option>
          <option value="completed">已完成</option>
          <option value="failed">已失败</option>
          <option value="running">运行中</option>
        </select>
        <label className="search-field">
          <Icon name="search" />
          <input
            aria-label="搜索批次实验"
            placeholder="搜索实验或参数"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <select
          aria-label="批次排序"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="net">排序：净收益</option>
          <option value="drawdown">排序：最大回撤</option>
        </select>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>比较</th>
              <th>实验</th>
              <th>参数</th>
              <th>状态</th>
              <th>净收益</th>
              <th>最大回撤</th>
              <th>交易数</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((e) => {
              const r = state.reports.find((r) => r.id === e.reportId),
                m = r ? metrics(r) : null;
              return (
                <tr
                  key={e.id}
                  className={selected.includes(e.id) ? "selected" : ""}
                >
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`批次比较 ${e.id}`}
                      checked={selected.includes(e.id)}
                      disabled={
                        !r ||
                        (selected.length === 2 && !selected.includes(e.id))
                      }
                      onChange={() =>
                        setSelected((s) =>
                          s.includes(e.id)
                            ? s.filter((x) => x !== e.id)
                            : [...s, e.id],
                        )
                      }
                    />
                  </td>
                  <td>
                    <strong>{e.id}</strong>
                    <small>{e.input.datasetId}</small>
                  </td>
                  <td>
                    {e.input.fast} / {e.input.slow}
                    <small>
                      r{e.input.revision} · fee {e.input.fee}%
                    </small>
                  </td>
                  <td>
                    <PhaseStatus value={e.status} />
                  </td>
                  <td>{m ? percent(m.net) : "—"}</td>
                  <td>{m ? percent(m.drawdown) : "—"}</td>
                  <td>{m?.count ?? "—"}</td>
                  <td>
                    <Button
                      onClick={() =>
                        r
                          ? navigate(`/report/${r.id}/overview`)
                          : e.status === "failed"
                            ? setErrorId(e.id)
                            : navigate(`/research/${e.researchId}/experiments`)
                      }
                    >
                      {r
                        ? "查看报告"
                        : e.status === "failed"
                          ? "查看原因"
                          : "查看进度"}
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!visible.length && <Empty />}
      </div>
      <Notice>指标仅描述当前演示样本，排名不构成“最优策略”结论。</Notice>
      <details>
        <summary>批次过程与日志</summary>
        <p>
          按固定输入创建 {rows.length}{" "}
          个成员。失败成员仅表示该任务读取失败，其余输入保持不变。
        </p>
      </details>
      <div className="action-footer">
        <span>
          已选 {selected.length} 个实验{" "}
          <LinkButton onClick={() => setSelected([])}>清除选择</LinkButton>
        </span>
        <Button
          kind="primary"
          disabled={selected.length !== 2}
          onClick={() => navigate(`/compare/${selected.join("/")}`)}
        >
          比较已选实验
        </Button>
      </div>
      {errorId && (
        <Dialog title={`${errorId} · 失败详情`} onClose={() => setErrorId("")}>
          <Notice tone="error">
            {rows.find((e) => e.id === errorId)?.error}
          </Notice>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setErrorId("")}>关闭</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute(
                  { type: "experiment.retry", id: errorId, key: requestKey() },
                  () => setErrorId(""),
                )
              }
            >
              重试此任务
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function HoldoutConfig() {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    reportId =
      new URLSearchParams(location.hash.split("?")[1]).get("report") ??
      "REP-021",
    source = state.reports.find((r) => r.id === reportId);
  const buffer = useBuffer(`holdout-${reportId}`, {
      datasetId: "AAPL-daily-v4",
      start: "2026-01-02",
      end: "2026-06-30",
    }),
    [savedId, setSavedId] = useState(""),
    [savedText, setSavedText] = useState(JSON.stringify(buffer.value)),
    [confirm, setConfirm] = useState(false),
    [key] = useState(requestKey);
  const save = async () => {
    const r = await action.execute({
      type: "holdout.save",
      id: savedId || undefined,
      reportId,
      ...buffer.value,
    });
    if (r) {
      setSavedId(r.id);
      setSavedText(JSON.stringify(buffer.value));
      toast("留出配置已保存，尚未执行。");
    }
    return !!r;
  };
  useUnsaved(JSON.stringify(buffer.value) !== savedText, save, () => {
    buffer.clear();
    setSavedText(JSON.stringify(buffer.value));
  });
  if (!source) return <Empty title="先选择一份完成报告" />;
  return (
    <div className="page">
      <PageHeader
        title="留出验证"
        subtitle="在未参与调参的数据上检查同一策略。"
        breadcrumb={
          <LinkButton onClick={() => navigate(`/report/${reportId}/overview`)}>
            研究 / {source.experimentId} / 留出验证
          </LinkButton>
        }
        actions={
          <Button disabled={action.busy} onClick={() => void save()}>
            保存配置
          </Button>
        }
      />
      <div className="window-bar">
        <div>
          <strong>样本内</strong>
          <small>
            {source.input.start} — {source.input.end}
          </small>
        </div>
        <Icon name="lock" />
        <div>
          <strong>留出</strong>
          <small>
            {buffer.value.start} — {buffer.value.end}
          </small>
        </div>
      </div>
      <p className="muted" style={{ textAlign: "center" }}>
        两个区间不重叠。留出数据不参与本次调参。
      </p>
      <div className="two-column">
        <Panel title="来源与冻结参数">
          <Snapshot input={source.input} />
        </Panel>
        <Panel title="留出数据">
          <Field label="数据版本">
            <select
              value={buffer.value.datasetId}
              onChange={(e) => {
                const data = state.datasets.find(
                  (d) => d.id === e.target.value,
                )!;
                buffer.update({
                  datasetId: data.id,
                  start: data.start,
                  end: data.end,
                });
              }}
            >
              {state.datasets
                .filter((d) => d.status === "ready")
                .map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="开始日期">
            <input
              type="date"
              value={buffer.value.start}
              onChange={(e) =>
                buffer.update({ ...buffer.value, start: e.target.value })
              }
            />
          </Field>
          <Field label="结束日期">
            <input
              type="date"
              value={buffer.value.end}
              onChange={(e) =>
                buffer.update({ ...buffer.value, end: e.target.value })
              }
            />
          </Field>
          <Notice>如果重新调参，需要另建实验并保留本次留出记录。</Notice>
          <ActionError error={action.error || buffer.bufferError} />
        </Panel>
      </div>
      <div className="action-footer">
        <p className="muted">策略参数、数据版本和成本假设将随实验保存。</p>
        <div className="button-row">
          <Button onClick={() => navigate(`/report/${reportId}/overview`)}>
            取消
          </Button>
          <Button
            kind="primary"
            disabled={action.busy}
            onClick={async () => {
              if (await save()) setConfirm(true);
            }}
          >
            审阅冻结配置
          </Button>
        </div>
      </div>
      {confirm && (
        <Dialog title="确认留出验证" onClose={() => setConfirm(false)}>
          <KeyValues
            items={[
              ["来源报告", reportId],
              ["留出数据", buffer.value.datasetId],
              ["区间", `${buffer.value.start} — ${buffer.value.end}`],
              ["参数", `${source.input.fast}/${source.input.slow}`],
              ["草稿", `r${source.input.revision}`],
            ]}
          />
          <Notice>仅增加演示证据，不自动接受候选或启动模拟。</Notice>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setConfirm(false)}>返回修改</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute(
                  { type: "holdout.start", id: savedId, key },
                  (r) => {
                    buffer.clear();
                    navigate(`/holdout/${r.id}`);
                  },
                )
              }
            >
              冻结并运行留出
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function HoldoutDetail({ id }: { id: string }) {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    holdout = state.holdouts.find((h) => h.id === id),
    source = state.reports.find((r) => r.id === holdout?.sourceReportId),
    target = state.reports.find((r) => r.id === holdout?.reportId);
  if (!holdout || !source) return <Empty title="留出不存在" />;
  if (!target)
    return (
      <div className="page">
        <PageHeader title="留出配置已保存" subtitle={`${id} · 尚未执行`} />
        <Snapshot input={holdout.input} />
        <ActionError error={action.error} />
        <Button
          kind="primary"
          onClick={() =>
            void action.execute({
              type: "holdout.start",
              id,
              key: requestKey(),
            })
          }
        >
          确认运行已保存留出
        </Button>
      </div>
    );
  return (
    <DetailLayout
      objectId={id}
      context={`${id} · 两份报告`}
      references={[
        { label: source.id, route: `/report/${source.id}/overview` },
        { label: target.id, route: `/report/${target.id}/overview` },
      ]}
    >
      <PageHeader
        title="留出验证"
        badge={<PhaseStatus value="completed" />}
        subtitle={`参数已冻结 · 来源 ${source.experimentId}`}
        breadcrumb={`研究 / 留出 ${id}`}
      />
      <div className="two-column">
        {[source, target].map((r, i) => (
          <div key={r.id}>
            <Panel
              title={
                <>
                  {i ? "留出" : "样本内"}{" "}
                  <LinkButton
                    onClick={() => navigate(`/report/${r.id}/overview`)}
                  >
                    {r.id}
                  </LinkButton>
                </>
              }
            >
              <small>
                {r.input.start} — {r.input.end}
              </small>
              <p className="muted">数据：{r.input.datasetId}</p>
              <KeyValues
                items={[
                  ["净收益", percent(metrics(r).net)],
                  ["最大回撤", percent(metrics(r).drawdown)],
                  ["交易数", metrics(r).count],
                ]}
              />
            </Panel>
            <Chart
              title={i ? "留出曲线" : "样本内曲线"}
              series={[{ name: r.id, points: r.equity }]}
            />
          </div>
        ))}
      </div>
      <Panel title="证据说明">
        <p>
          参数 {source.input.fast}/{source.input.slow}、手续费{" "}
          {source.input.fee}%、滑点 {source.input.slippage}% 保持一致。
        </p>
        <Notice tone="warn">
          留出仅有 {target.trades.length} 笔演示交易，证据仍需人工复核。
        </Notice>
        <ActionError error={action.error} />
        <div className="button-row">
          <Button
            onClick={() => {
              toast("已保留这份留出记录，可从回测记录继续查看。");
              navigate("/records");
            }}
          >
            保留观察
          </Button>
          <Button
            kind="primary"
            disabled={action.busy}
            onClick={() =>
              void action.execute(
                { type: "candidate.create", holdoutId: id },
                (r) => navigate(`/candidate/${r.id}`),
              )
            }
          >
            创建候选记录
          </Button>
        </div>
      </Panel>
    </DetailLayout>
  );
}

export function Candidates() {
  const { state, navigate } = useStudio(),
    [tab, setTab] = useState("all"),
    [search, setSearch] = useState(""),
    [expanded, setExpanded] = useState("C-007"),
    [sort, setSort] = useState("pending");
  const list = state.candidates
    .filter(
      (c) =>
        (tab === "all" || c.state === tab) &&
        `${c.id} ${state.researches.find((r) => r.id === c.researchId)?.name} ${c.reason}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    )
    .sort((a, b) =>
      sort === "name"
        ? a.id.localeCompare(b.id)
        : Number(a.state !== "pending") - Number(b.state !== "pending"),
    );
  return (
    <div className="page">
      <PageHeader
        title="候选中心"
        subtitle="先审阅证据，再记录人工决定。"
        breadcrumb={
          <LinkButton onClick={() => navigate("/")}>研究 / 候选中心</LinkButton>
        }
        actions={
          <Button onClick={() => setTab("accepted")}>查看决定记录</Button>
        }
      />
      <Tabs
        active={tab}
        onChange={setTab}
        items={[
          ["all", "全部"],
          ["pending", "待审"],
          ["accepted", "已接受"],
          ["rejected", "已拒绝"],
        ].map(([id, label]) => ({
          id,
          label: `${label} ${state.candidates.filter((c) => id === "all" || c.state === id).length}`,
        }))}
      />
      <div className="toolbar">
        <label className="search-field">
          <Icon name="search" />
          <input
            value={search}
            aria-label="搜索候选"
            placeholder="搜索候选、研究或备注"
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <select
          aria-label="候选排序"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="pending">待审优先</option>
          <option value="name">编号排序</option>
        </select>
      </div>
      {list.length ? (
        list.map((c) => (
          <Panel key={c.id}>
            <div className="continue-row" style={{ borderTop: 0 }}>
              <div>
                <LinkButton
                  onClick={() => setExpanded(expanded === c.id ? "" : c.id)}
                >
                  {c.id} {expanded === c.id ? "⌃" : "⌄"}
                </LinkButton>
                <strong>
                  {state.researches.find((r) => r.id === c.researchId)?.name}
                </strong>
                <small>
                  {c.reportId} + {c.holdoutId}
                </small>
              </div>
              <PhaseStatus value={c.state} />
              <Button
                kind={c.state === "pending" ? "primary" : "secondary"}
                onClick={() => navigate(`/candidate/${c.id}`)}
              >
                {c.state === "pending" ? "审阅证据" : "查看决定"}
              </Button>
            </div>
            {expanded === c.id && (
              <>
                <Notice>接受候选只记录决定，模拟需要单独配置与启动。</Notice>
                {c.reason && <p>决定理由：{c.reason}</p>}
                <div className="button-row">
                  <LinkButton
                    onClick={() => navigate(`/report/${c.reportId}/overview`)}
                  >
                    查看来源报告
                  </LinkButton>
                  <LinkButton
                    onClick={() => navigate(`/holdout/${c.holdoutId}`)}
                  >
                    查看留出
                  </LinkButton>
                  {c.state === "accepted" && (
                    <Button
                      onClick={() =>
                        navigate(`/simulation/new?candidate=${c.id}`)
                      }
                    >
                      配置模拟
                    </Button>
                  )}
                </div>
              </>
            )}
          </Panel>
        ))
      ) : (
        <Empty title="没有匹配候选" />
      )}
    </div>
  );
}

export function CandidateDetail({ id }: { id: string }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    c = state.candidates.find((c) => c.id === id),
    r = state.reports.find((r) => r.id === c?.reportId),
    buffer = useBuffer(`candidate-${id}`, ""),
    [decision, setDecision] = useState<"accepted" | "rejected">("accepted"),
    [confirm, setConfirm] = useState(false);
  if (!c || !r) return <Empty title="候选证据缺失" />;
  return (
    <DetailLayout
      objectId={id}
      context={`${id} · 人工决定`}
      references={[
        { label: c.reportId, route: `/report/${c.reportId}/overview` },
        { label: c.holdoutId, route: `/holdout/${c.holdoutId}` },
      ]}
    >
      <PageHeader
        title={
          state.researches.find((x) => x.id === c.researchId)?.name ??
          "候选详情"
        }
        subtitle={
          <>
            {id} <PhaseStatus value={c.state} />
          </>
        }
        breadcrumb={
          <LinkButton onClick={() => navigate("/candidates")}>
            候选中心 / {id}
          </LinkButton>
        }
      />
      <h2>证据材料</h2>
      <Panel>
        <div className="binding-row">
          <Icon name="file" />
          <div>
            <strong>{c.reportId} 样本内报告</strong>
            <small>
              {state.datasets.find((d) => d.id === r.input.datasetId)?.symbol}{" "}
              {
                state.datasets.find((d) => d.id === r.input.datasetId)
                  ?.frequency
              }{" "}
              · r{r.input.revision} · {r.input.fast}/{r.input.slow}
            </small>
          </div>
          <Button onClick={() => navigate(`/report/${c.reportId}/overview`)}>
            查看报告
          </Button>
        </div>
        <div className="binding-row">
          <Icon name="file" />
          <div>
            <strong>{c.holdoutId} 留出验证</strong>
            <small>冻结参数与独立报告</small>
          </div>
          <Button onClick={() => navigate(`/holdout/${c.holdoutId}`)}>
            查看留出
          </Button>
        </div>
      </Panel>
      <h2>检查清单</h2>
      <ul className="checks">
        <li>
          <Icon name="check" />
          <div>
            <strong>数据与参数已冻结</strong>
            <small>版本、参数与成本已记录，后续草稿不覆盖证据。</small>
          </div>
        </li>
        <li>
          <Icon name="check" />
          <div>
            <strong>成本假设可追溯</strong>
            <small>
              手续费 {r.input.fee}%、滑点 {r.input.slippage}%。
            </small>
          </div>
        </li>
        <li>
          <Icon name="info" />
          <div>
            <strong>样本数量有限，仍需观察</strong>
            <small>演示证据不能证明策略稳健性。</small>
          </div>
        </li>
      </ul>
      <hr />
      <h2>人工决定</h2>
      {c.state === "pending" ? (
        <>
          <Field
            label="决定理由"
            hint="最多500字。理由草稿在本浏览器保留，确认后才记录决定。"
          >
            <textarea
              maxLength={500}
              value={buffer.value}
              onChange={(e) => buffer.update(e.target.value)}
              placeholder="说明已检查的证据、保留的限制与决定理由…"
            />
          </Field>
          <small>{buffer.value.length}/500</small>
          <div className="radio-row">
            <label>
              <input
                type="radio"
                name="candidate-decision"
                checked={decision === "accepted"}
                onChange={() => setDecision("accepted")}
              />
              接受候选
            </label>
            <label>
              <input
                type="radio"
                name="candidate-decision"
                checked={decision === "rejected"}
                onChange={() => setDecision("rejected")}
              />
              拒绝候选
            </label>
          </div>
          <Notice>接受只记录决定，不会启动模拟。</Notice>
          <ActionError error={action.error || buffer.bufferError} />
          <div className="button-row">
            <Button onClick={() => navigate("/candidates")}>暂不决定</Button>
            <Button
              kind="primary"
              disabled={!buffer.value.trim() || action.busy}
              onClick={() => setConfirm(true)}
            >
              确认记录决定
            </Button>
          </div>
        </>
      ) : (
        <>
          <Panel title={c.state === "accepted" ? "已接受候选" : "已拒绝候选"}>
            <p>{c.reason}</p>
            <small>{c.decidedAt}</small>
          </Panel>
          <Notice>决定已记录；未因此启动任何实例。</Notice>
          {c.state === "accepted" && (
            <Button
              kind="primary"
              onClick={() => navigate(`/simulation/new?candidate=${id}`)}
            >
              配置模拟
            </Button>
          )}
        </>
      )}
      {confirm && (
        <Dialog
          title={decision === "accepted" ? "记录接受决定？" : "记录拒绝决定？"}
          onClose={() => setConfirm(false)}
        >
          <p>
            候选 {id} · {decision === "accepted" ? "接受" : "拒绝"}
          </p>
          <Panel title="决定理由">
            <p>{buffer.value}</p>
          </Panel>
          <Notice>这项操作仅记录人工决定，不启动运行。</Notice>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setConfirm(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute(
                  {
                    type: "candidate.decide",
                    id,
                    decision,
                    reason: buffer.value,
                  },
                  () => {
                    buffer.clear();
                    setConfirm(false);
                  },
                )
              }
            >
              确认{decision === "accepted" ? "接受" : "拒绝"}候选
            </Button>
          </div>
        </Dialog>
      )}
    </DetailLayout>
  );
}
