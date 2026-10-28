import { useState } from "react";
import { DraftSurface } from "./layout";
import { adapter } from "./scenario";
import {
  useAction,
  useBuffer,
  useStudio,
  useUnsaved,
  requestKey,
} from "./context";
import type { Draft, Experiment, Research } from "./model";
import {
  ActionError,
  Button,
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
  Status,
  Tabs,
} from "./ui";

export function Home({ fresh = false }: { fresh?: boolean }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    buffer = useBuffer("research-goal", "");
  const query = new URLSearchParams(location.hash.split("?")[1]),
    [dataset, setDataset] = useState(query.get("data") ?? ""),
    [template, setTemplate] = useState(query.get("template") ?? ""),
    [filter, setFilter] = useState("all");
  const create = async () => {
    const result = await action.execute({
      type: "research.create",
      goal: buffer.value,
      datasetId: dataset,
      templateId: template,
    });
    if (result) {
      buffer.clear();
      buffer.update("");
      navigate(`/research/${result.id}/overview`, true);
    }
    return !!result;
  };
  useUnsaved(!!buffer.value, create, () => {
    buffer.update("");
    buffer.clear();
  });
  const list = state.researches.filter(
    (r) =>
      filter === "all" ||
      (filter === "attention"
        ? state.proposals.some(
            (p) => p.researchId === r.id && p.state === "pending",
          )
        : state.experiments.some(
            (e) => e.researchId === r.id && e.status === "completed",
          )),
  );
  return (
    <div className={`home ${fresh ? "new-research" : ""}`}>
      <h1>{fresh ? "新建研究" : "今天，验证什么想法？"}</h1>
      <p className="subtitle">
        {fresh ? "把你想验证的问题写下来。" : "描述目标，或继续一项研究。"}
      </p>
      <div className="composer">
        <label className="sr-only" htmlFor="research-goal">
          研究目标
        </label>
        <textarea
          id="research-goal"
          placeholder="例如：检验双均线策略在不同交易成本下的表现…"
          value={buffer.value}
          onChange={(e) => buffer.update(e.target.value)}
          onKeyDown={(e) => {
            if (
              (e.metaKey || e.ctrlKey) &&
              e.key === "Enter" &&
              !e.nativeEvent.isComposing
            )
              void create();
          }}
        />
        <div className="composer-bottom">
          <div className="composer-context">
            <Icon name="plus" />
            <select
              aria-label="添加数据上下文"
              value={dataset}
              onChange={(e) => setDataset(e.target.value)}
            >
              <option value="">添加数据（可选）</option>
              {state.datasets
                .filter((d) => d.status === "ready")
                .map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
            </select>
            <select
              aria-label="选择策略模板"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
            >
              <option value="">选择策略模板（可选）</option>
              {state.templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
            <LinkButton onClick={() => navigate("/resources/models")}>
              模型 · 默认
            </LinkButton>
          </div>
          <Button
            kind="primary"
            icon="send"
            disabled={action.busy || !buffer.value.trim()}
            onClick={() => void create()}
          >
            {action.busy ? "保存中…" : fresh ? "保存研究并继续" : "开始研究"}
          </Button>
        </div>
        <ActionError error={action.error || buffer.bufferError} />
      </div>
      <div className="quick-actions">
        <button
          onClick={() =>
            buffer.update(
              "我想研究 AAPL 日线双均线趋势策略，比较 20/60 与 30/60，并检查交易成本的影响。",
            )
          }
        >
          <Icon name="light" />
          从一个假设开始
          <Icon name="arrow" size={16} />
        </button>
        <button onClick={() => navigate("/records")}>
          <Icon name="chart" />
          分析已有结果
          <Icon name="arrow" size={16} />
        </button>
        <button onClick={() => navigate("/resources/templates")}>
          <Icon name="file" />
          从模板开始
          <Icon name="arrow" size={16} />
        </button>
      </div>
      {fresh ? (
        <small>
          可以先保存想法，稍后补充数据与策略。实验在独立确认页启动。
        </small>
      ) : (
        <>
          <div className="section-heading">
            <h2>继续研究</h2>
            <div className="home-filters">
              {[
                ["all", "全部"],
                ["attention", "需要处理"],
                ["completed", "最近完成"],
              ].map(([id, name]) => (
                <button
                  key={id}
                  className={filter === id ? "selected" : ""}
                  onClick={() => setFilter(id)}
                >
                  {name}
                </button>
              ))}
            </div>
          </div>
          {list.length ? (
            list.map((r) => {
              const pending = state.proposals.find(
                  (p) => p.researchId === r.id && p.state === "pending",
                ),
                experiment = state.experiments.find(
                  (e) => e.researchId === r.id && e.reportId,
                );
              return (
                <div className="continue-row" key={r.id}>
                  <div>
                    <strong>{r.name}</strong>
                    <small>
                      {pending
                        ? "Agent 已生成参数修改提案，等待你的审阅。"
                        : experiment
                          ? "实验已完成，报告可复核。"
                          : "想法已保存，可以继续完善草稿。"}
                    </small>
                  </div>
                  <span className="muted">
                    {r.draft.datasetId
                      ? state.datasets.find((d) => d.id === r.draft.datasetId)
                          ?.symbol + " · 日线"
                      : "尚未绑定数据"}
                  </span>
                  <Button
                    kind="ghost"
                    onClick={() =>
                      navigate(
                        pending
                          ? `/proposal/${pending.id}`
                          : experiment?.reportId
                            ? `/report/${experiment.reportId}/overview`
                            : `/research/${r.id}/overview`,
                      )
                    }
                  >
                    {pending
                      ? "待审阅提案"
                      : experiment
                        ? "查看报告"
                        : "继续草稿"}
                  </Button>
                </div>
              );
            })
          ) : (
            <Empty
              title="还没有研究"
              description="保存一个想法，开始你的探索。"
            />
          )}
          <section className="activity">
            <div className="section-heading">
              <h2>最近活动</h2>
              <LinkButton onClick={() => navigate("/records")}>
                查看全部记录
              </LinkButton>
            </div>
            <ul className="timeline">
              {state.researches.slice(0, 3).map((r) => (
                <li key={r.id}>
                  <time>{r.updatedAt.slice(5, 16)}</time>
                  <span>
                    保存草稿 r{r.draft.revision}{" "}
                    <LinkButton
                      onClick={() => navigate(`/research/${r.id}/draft`)}
                    >
                      {r.name}
                    </LinkButton>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}

function Binding({
  research,
  draft = research.draft,
  onClose,
}: {
  research: Research;
  draft?: Draft;
  onClose: () => void;
}) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    [data, setData] = useState(research.draft.datasetId),
    [template, setTemplate] = useState(research.draft.templateId),
    [loading, setLoading] = useState(false),
    [readError, setReadError] = useState("");
  const save = async () => {
    const dataset = state.datasets.find((d) => d.id === data),
      t = state.templates.find((t) => t.id === template);
    const result = await action.execute({
      type: "draft.save",
      id: research.id,
      expected: research.draft.revision,
      draft: {
        ...draft,
        ...(template !== draft.templateId ? t?.defaults : {}),
        datasetId: data,
        templateId: template,
        start:
          data !== draft.datasetId
            ? (dataset?.start ?? draft.start)
            : draft.start,
        end: data !== draft.datasetId ? (dataset?.end ?? draft.end) : draft.end,
      },
    });
    if (result) {
      onClose();
      navigate(`/research/${research.id}/draft`, true);
    }
  };
  return (
    <Dialog title="绑定研究上下文" onClose={onClose}>
      <p className="muted">
        选择不可变数据版本和策略模板，保存为新的草稿修订。保持当前模板时保留正在编辑的参数；更换模板会采用新模板的默认参数。
      </p>
      <Field label="策略模板">
        <select value={template} onChange={(e) => setTemplate(e.target.value)}>
          <option value="">选择策略模板</option>
          {state.templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="数据版本">
        <select value={data} onChange={(e) => setData(e.target.value)}>
          <option value="">选择数据版本</option>
          {state.datasets.map((d) => (
            <option key={d.id} value={d.id} disabled={d.status !== "ready"}>
              {d.name}
              {d.status !== "ready" ? " · 待检查" : ""}
            </option>
          ))}
        </select>
      </Field>
      <ActionError error={readError} />
      <Button
        icon="refresh"
        disabled={loading}
        onClick={async () => {
          setReadError("");
          setLoading(true);
          try {
            await adapter.read();
          } catch (err) {
            setReadError((err as Error).message);
          } finally {
            setLoading(false);
          }
        }}
      >
        {loading ? "读取目录中…" : readError ? "重试读取" : "刷新资源目录"}
      </Button>
      <ActionError error={action.error} />
      <div className="dialog-actions">
        <Button onClick={onClose}>取消</Button>
        <Button
          kind="primary"
          disabled={action.busy || !data || !template}
          onClick={() => void save()}
        >
          绑定并保存草稿
        </Button>
      </div>
    </Dialog>
  );
}

function ResearchOverview({ research }: { research: Research }) {
  const { navigate } = useStudio(),
    [binding, setBinding] = useState(false),
    [editing, setEditing] = useState(false),
    action = useAction(),
    [goal, setGoal] = useState(research.goal);
  return (
    <>
      <h2>研究问题</h2>
      <Panel>
        <div className="section-heading">
          <p style={{ margin: 0 }}>{research.goal}</p>
          <Button icon="edit" kind="ghost" onClick={() => setEditing(true)}>
            编辑
          </Button>
        </div>
      </Panel>
      <h2>计划</h2>
      <ol className="plan-steps">
        <li>
          <div>
            <strong>绑定策略与数据</strong>
            <p>选择适合的策略模板，绑定不可变的数据版本。</p>
          </div>
        </li>
        <li>
          <div>
            <strong>检查参数与成本</strong>
            <p>设置快慢均线、手续费和滑点，保存可检查的草稿。</p>
          </div>
        </li>
        <li>
          <div>
            <strong>运行并比较实验</strong>
            <p>独立确认冻结输入，查看报告并决定后续验证。</p>
          </div>
        </li>
      </ol>
      <Panel>
        <div className="binding-row">
          <Icon name="cube" size={25} />
          <div>
            <strong>策略模板</strong>
            <small>{research.draft.templateId || "尚未绑定"}</small>
          </div>
          <Button onClick={() => setBinding(true)}>选择策略模板</Button>
        </div>
        <div className="binding-row">
          <Icon name="data" size={25} />
          <div>
            <strong>数据</strong>
            <small>{research.draft.datasetId || "尚未绑定"}</small>
          </div>
          <Button onClick={() => setBinding(true)}>选择数据</Button>
        </div>
      </Panel>
      <Notice>开始实验前需要绑定策略与数据。</Notice>
      <div className="button-row">
        <Button
          kind="primary"
          onClick={() =>
            research.draft.datasetId && research.draft.templateId
              ? navigate(`/research/${research.id}/draft`)
              : setBinding(true)
          }
        >
          继续检查草稿
        </Button>
        <Button onClick={() => navigate("/candidates")}>候选中心</Button>
      </div>
      {binding && (
        <Binding research={research} onClose={() => setBinding(false)} />
      )}{" "}
      {editing && (
        <Dialog title="编辑研究问题" onClose={() => setEditing(false)}>
          <Field label="研究问题">
            <textarea value={goal} onChange={(e) => setGoal(e.target.value)} />
          </Field>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setEditing(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={action.busy || !goal.trim()}
              onClick={() =>
                void action.execute(
                  {
                    type: "research.save",
                    id: research.id,
                    goal,
                    notes: research.notes,
                  },
                  () => setEditing(false),
                )
              }
            >
              保存研究问题
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}

export function DraftEditor({ research }: { research: Research }) {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    buffer = useBuffer(
      `draft-${research.id}-${research.draft.revision}`,
      research.draft,
    ),
    [history, setHistory] = useState(false),
    [binding, setBinding] = useState(false);
  const draft = buffer.value,
    dirty = JSON.stringify(draft) !== JSON.stringify(research.draft);
  const save = async () => {
    const result = await action.execute({
      type: "draft.save",
      id: research.id,
      expected: research.draft.revision,
      draft,
    });
    if (result) {
      buffer.clear();
      toast("草稿已保存为新版本。");
    }
    return !!result;
  };
  useUnsaved(dirty, save, () => {
    buffer.update(research.draft);
    buffer.clear();
  });
  const setNumber = (key: keyof Draft, value: string) => {
    buffer.update({ ...draft, [key]: value === "" ? NaN : Number(value) });
    action.clearError();
  };
  const proposal = state.proposals
    .filter((p) => p.researchId === research.id && p.state === "pending")
    .at(-1);
  return (
    <>
      <div className="section-heading">
        <h2>
          策略草稿 <Status>r{research.draft.revision}</Status>{" "}
          <small>{dirty ? "未保存修改" : "已保存"}</small>
        </h2>
        <Button
          kind="primary"
          disabled={dirty || !draft.datasetId || !draft.templateId}
          onClick={() => navigate(`/research/${research.id}/confirm`)}
        >
          检查并运行实验
        </Button>
      </div>
      <DraftSurface source={<>
        <KeyValues
          items={[
            [
              "策略模板",
              state.templates.find((t) => t.id === draft.templateId)?.name ??
                "尚未绑定",
            ],
            ["数据", draft.datasetId || "尚未绑定"],
            [
              "周期",
              state.datasets.find((d) => d.id === draft.datasetId)?.symbol +
                " · " +
                state.datasets.find((d) => d.id === draft.datasetId)?.frequency,
            ],
            ["样本区间", `${draft.start} — ${draft.end}`],
          ]}
        />
        <div className="button-row" style={{ marginTop: 10 }}>
          <LinkButton onClick={() => setBinding(true)}>
            修改绑定上下文
          </LinkButton>
        </div>
      </>} parameters={<>
        {(
          [
            ["fast", "快均线（周期）"],
            ["slow", "慢均线（周期）"],
            ["allocation", "仓位比例（%）"],
          ] as const
        ).map(([key, label]) => (
          <Field key={key} label={label}>
            <input
              type="number"
              value={!Number.isFinite(draft[key]) ? "" : draft[key]}
              onChange={(e) => setNumber(key, e.target.value)}
              min="1"
            />
          </Field>
        ))}
      </>} costs={<>
        {(
          [
            ["fee", "手续费（%）"],
            ["slippage", "滑点（%）"],
          ] as const
        ).map(([key, label]) => (
          <Field key={key} label={label}>
            <input
              type="number"
              step="0.01"
              value={!Number.isFinite(draft[key]) ? "" : draft[key]}
              onChange={(e) => setNumber(key, e.target.value)}
              min="0"
              max="5"
            />
          </Field>
        ))}
        <KeyValues
          items={[
            ["交易方向", "只做多"],
            [
              "交易逻辑",
              state.templates.find((t) => t.id === draft.templateId)?.rule ??
                "绑定模板后查看规则",
            ],
          ]}
        />
      </>} />
      <ActionError error={action.error || buffer.bufferError} />
      {dirty && (
        <Notice tone="warn">
          参数尚未保存。保存后生成新修订，已有报告保持冻结。
        </Notice>
      )}
      {proposal && (
        <Notice
          action={
            <LinkButton onClick={() => navigate(`/proposal/${proposal.id}`)}>
              查看提案
            </LinkButton>
          }
        >
          Agent 有一项参数提案待审阅，建议比较另一组参数。
        </Notice>
      )}
      <div className="action-footer">
        <small>草稿修改会保存为新版本。</small>
        <div className="button-row">
          <Button icon="clock" onClick={() => setHistory(true)}>
            查看版本
          </Button>
          <Button
            disabled={dirty || action.busy || !draft.datasetId}
            onClick={() =>
              void action.execute(
                { type: "proposal.generate", id: research.id },
                (r) => navigate(`/proposal/${r.id}`),
              )
            }
          >
            生成演示提案
          </Button>
          <Button
            kind="primary"
            disabled={!dirty || action.busy}
            onClick={() => void save()}
          >
            {action.busy ? "保存中…" : "保存草稿"}
          </Button>
        </div>
      </div>
      {history && (
        <Dialog title="草稿修订历史" onClose={() => setHistory(false)}>
          {research.history
            .slice()
            .reverse()
            .map((d) => (
              <details key={d.revision}>
                <summary>
                  r{d.revision} 快 / 慢均线 {d.fast} / {d.slow}
                </summary>
                <Snapshot input={d} />
              </details>
            ))}
        </Dialog>
      )}
      {binding && (
        <Binding
          draft={draft}
          research={research}
          onClose={() => setBinding(false)}
        />
      )}
    </>
  );
}

function Notes({ research }: { research: Research }) {
  const action = useAction(),
    { toast } = useStudio(),
    buffer = useBuffer(`notes-${research.id}`, research.notes);
  const save = async () => {
    const result = await action.execute({
      type: "research.save",
      id: research.id,
      goal: research.goal,
      notes: buffer.value,
    });
    if (result) {
      buffer.clear();
      toast("研究笔记已保存。");
    }
    return !!result;
  };
  useUnsaved(buffer.value !== research.notes, save, () => {
    buffer.update(research.notes);
    buffer.clear();
  });
  return (
    <>
      <Field label="研究笔记" hint="记录假设、观察和下一步；不会触发实验。">
        <textarea
          style={{ minHeight: 260 }}
          value={buffer.value}
          onChange={(e) => buffer.update(e.target.value)}
        />
      </Field>
      <ActionError error={action.error || buffer.bufferError} />
      <Button
        kind="primary"
        icon="file"
        disabled={action.busy}
        onClick={() => void save()}
      >
        保存笔记
      </Button>
    </>
  );
}

export function RequestCard({ requestId }: { requestId: string }) {
  const { state } = useStudio(),
    action = useAction(),
    req = state.requests.find((r) => r.id === requestId);
  if (!req) return null;
  return (
    <Panel
      title={
        req.state === "unknown"
          ? "暂时无法确认执行结果"
          : req.state === "confirmed"
            ? "请求已确认"
            : "请求状态"
      }
    >
      <Notice tone={req.state === "confirmed" ? "success" : "warn"}>
        {req.state === "unknown"
          ? "请求已发送，响应中断。保留原请求，查询不会重复执行。"
          : req.state === "confirmed"
            ? "已取得本次请求的确认结果。"
            : "请求已提交，正在等待确认。请求停止不等于已停止。"}
      </Notice>
      <KeyValues
        items={[
          ["请求编号", req.id],
          ["对象", req.objectId],
          ["提交时间", req.submittedAt],
          ["最近查询", req.queriedAt ?? "尚未查询"],
        ]}
      />
      <ActionError error={action.error} />
      {req.state !== "confirmed" && (
        <Button
          kind="primary"
          disabled={action.busy}
          onClick={() =>
            void action.execute({ type: "request.query", id: req.id })
          }
        >
          {action.busy ? "查询中…" : "查询原请求"}
        </Button>
      )}
      <details>
        <summary>查看请求记录</summary>
        <pre className="diagnostic-preview">{JSON.stringify(req, null, 2)}</pre>
      </details>
    </Panel>
  );
}

export function Experiments({ research }: { research: Research }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    [selected, setSelected] = useState<string[]>([]),
    [detail, setDetail] = useState<Experiment | null>(null);
  const experiments = state.experiments.filter(
      (e) => e.researchId === research.id,
    ),
    active = experiments.find((e) => ["running", "unknown"].includes(e.status)),
    stop = state.requests.find(
      (r) => r.objectId === active?.id && r.operation === "experiment-stop",
    );
  const check = (id: string) =>
    setSelected((s) =>
      s.includes(id)
        ? s.filter((x) => x !== id)
        : s.length < 2
          ? [...s, id]
          : s,
    );
  return (
    <>
      <div className="section-heading">
        <h2>实验</h2>
        <div className="button-row">
          <Button
            onClick={() => navigate(`/batch/new?research=${research.id}`)}
          >
            新建批次
          </Button>
          <Button
            kind="primary"
            icon="plus"
            onClick={() => navigate(`/research/${research.id}/confirm`)}
          >
            新建实验
          </Button>
        </div>
      </div>
      {active && (
        <Panel>
          <div className="progress-header">
            <h2>
              {active.id} <PhaseStatus value={active.status} />
            </h2>
            {active.status === "running" && !stop && (
              <Button
                disabled={action.busy}
                onClick={() =>
                  void action.execute({
                    type: "experiment.stop",
                    id: active.id,
                    key: requestKey(),
                  })
                }
              >
                请求停止
              </Button>
            )}
          </div>
          <details open>
            <summary>执行详情 · 演示进度</summary>
            <p>
              当前阶段：
              {active.progress < 25
                ? "冻结输入"
                : active.progress < 60
                  ? "读取场景数据"
                  : "生成演示报告"}
            </p>
            <div
              className="progress"
              role="progressbar"
              aria-label="实验进度"
              aria-valuenow={active.progress}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <span style={{ width: `${active.progress}%` }} />
            </div>
            <div className="steps">
              {["冻结输入", "读取数据", "整理指标", "生成报告"].map(
                (name, i) => (
                  <span
                    key={name}
                    className={active.progress > i * 25 ? "done" : ""}
                  >
                    <i>{i + 1}</i>
                    {name}
                  </span>
                ),
              )}
            </div>
          </details>
          {(active.status === "unknown" || stop) && (
            <RequestCard requestId={stop?.id ?? active.requestId} />
          )}
        </Panel>
      )}
      <ActionError error={action.error} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>比较</th>
              <th>实验</th>
              <th>冻结输入</th>
              <th>状态</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {experiments.map((e) => (
              <tr
                key={e.id}
                className={selected.includes(e.id) ? "selected" : ""}
              >
                <td>
                  <input
                    type="checkbox"
                    aria-label={`比较 ${e.id}`}
                    checked={selected.includes(e.id)}
                    disabled={
                      !e.reportId ||
                      (!selected.includes(e.id) && selected.length === 2)
                    }
                    onChange={() => check(e.id)}
                  />
                </td>
                <td>
                  <strong>{e.id}</strong>
                  <small>{e.createdAt.slice(5, 16)}</small>
                </td>
                <td>
                  r{e.input.revision} · {e.input.datasetId}
                  <small>
                    {e.input.fast}/{e.input.slow}
                  </small>
                </td>
                <td>
                  <PhaseStatus value={e.status} />
                </td>
                <td>
                  <Button
                    onClick={() =>
                      e.reportId
                        ? navigate(`/report/${e.reportId}/overview`)
                        : setDetail(e)
                    }
                  >
                    {e.reportId
                      ? "查看报告"
                      : e.status === "failed"
                        ? "查看原因"
                        : "查看状态"}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!experiments.length && (
          <Empty
            title="尚未运行实验"
            description="先检查草稿，在独立确认页冻结输入。"
          />
        )}
      </div>
      <div className="action-footer">
        <small>已选 {selected.length} 项 · 仅比较同一研究的完成实验</small>
        <Button
          disabled={selected.length !== 2}
          onClick={() => navigate(`/compare/${selected.join("/")}`)}
        >
          比较已选实验
        </Button>
      </div>
      {detail && (
        <Dialog
          title={`${detail.id} · 执行记录`}
          onClose={() => setDetail(null)}
        >
          {detail.error ? (
            <>
              <Notice tone="error">{detail.error}</Notice>
              <Button
                disabled={action.busy}
                onClick={() =>
                  void action.execute(
                    {
                      type: "experiment.retry",
                      id: detail.id,
                      key: requestKey(),
                    },
                    () => setDetail(null),
                  )
                }
              >
                重试此任务
              </Button>
            </>
          ) : (
            <RequestCard requestId={detail.requestId} />
          )}
          <Snapshot input={detail.input} />
          <ActionError error={action.error} />
        </Dialog>
      )}
    </>
  );
}

export function ResearchView({
  id,
  tab = "overview",
}: {
  id: string;
  tab?: string;
}) {
  const { state, navigate } = useStudio(),
    research = state.researches.find((r) => r.id === id);
  if (!research)
    return (
      <div className="page">
        <Empty
          title="研究不存在"
          action={<Button onClick={() => navigate("/")}>返回研究</Button>}
        />
      </div>
    );
  return (
    <DetailLayout
      objectId={id}
      context={`当前草稿 r${research.draft.revision} · ${research.draft.datasetId || "未绑定数据"}`}
      references={[
        {
          label: `草稿 r${research.draft.revision}`,
          route: `/research/${id}/draft`,
        },
        ...state.experiments
          .filter((e) => e.researchId === id && e.reportId)
          .slice(0, 2)
          .map((e) => ({
            label: e.reportId!,
            route: `/report/${e.reportId}/overview`,
          })),
      ]}
    >
      <PageHeader
        title={research.name}
        subtitle={
          <>
            {research.draft.datasetId || "想法已保存"}{" "}
            <Status>草稿 r{research.draft.revision}</Status>
          </>
        }
        breadcrumb={
          <LinkButton onClick={() => navigate("/")}>
            研究 / {research.name}
          </LinkButton>
        }
      />
      <Tabs
        items={[
          { id: "overview", label: "概览" },
          { id: "draft", label: "策略草稿" },
          { id: "experiments", label: "实验" },
          { id: "notes", label: "笔记" },
        ]}
        active={tab}
        onChange={(t) => navigate(`/research/${id}/${t}`)}
      />
      {tab === "draft" ? (
        <DraftEditor
          key={`${id}-${research.draft.revision}`}
          research={research}
        />
      ) : tab === "experiments" ? (
        <Experiments research={research} />
      ) : tab === "notes" ? (
        <Notes key={id} research={research} />
      ) : (
        <ResearchOverview key={id} research={research} />
      )}
    </DetailLayout>
  );
}

export function ProposalView({ id }: { id: string }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    p = state.proposals.find((p) => p.id === id),
    r = state.researches.find((r) => r.id === p?.researchId);
  if (!p || !r) return <Empty title="提案不存在" />;
  const conflict =
    p.base.revision !== r.draft.revision && p.state === "pending";
  return (
    <DetailLayout
      objectId={p.id}
      context={`提案 ${p.id} · 基于 r${p.base.revision}`}
      references={[
        {
          label: `当前草稿 r${r.draft.revision}`,
          route: `/research/${r.id}/draft`,
        },
      ]}
    >
      <PageHeader
        title="审阅修改提案"
        badge={<PhaseStatus value={p.state} />}
        subtitle={`基于草稿 r${p.base.revision} · ${conflict ? `当前草稿 r${r.draft.revision}` : `应用后生成 r${p.base.revision + 1}`}`}
        breadcrumb={
          <LinkButton onClick={() => navigate(`/research/${r.id}/draft`)}>
            {r.name} / 策略草稿
          </LinkButton>
        }
      />
      <p>{p.explanation}</p>
      {conflict && (
        <Notice tone="warn">
          草稿已更新，这份提案暂不能应用。你的修改与原提案均已保留。
        </Notice>
      )}
      <h2>{conflict ? "参数变更对比" : "1 项变更"}</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>参数</th>
              <th>原提案基线 r{p.base.revision}</th>
              <th>提案建议</th>
              {conflict && <th>当前草稿 r{r.draft.revision}</th>}
            </tr>
          </thead>
          <tbody>
            {(
              [
                ["fast", "快均线"],
                ["slow", "慢均线"],
                ["fee", "手续费（%）"],
                ["slippage", "滑点（%）"],
              ] as const
            ).map(([key, name]) => (
              <tr key={key}>
                <td>{name}</td>
                <td>{p.base[key]}</td>
                <td
                  className={
                    p.base[key] !== p.suggested[key] ? "diff-change" : ""
                  }
                >
                  {p.suggested[key]}
                </td>
                {conflict && <td>{r.draft[key]}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Panel title="相关信息" className="spacing-top">
        <KeyValues
          items={[
            ["数据", p.base.datasetId],
            ["适用研究", r.name],
            ["来源提案", p.id],
          ]}
        />
      </Panel>
      <ActionError error={action.error} />
      <Notice>应用仅更新草稿，不会运行实验。已有冻结报告保持原版本。</Notice>
      <div className="button-row">
        {conflict && (
          <Button
            kind="primary"
            disabled={action.busy}
            onClick={() =>
              void action.execute(
                { type: "proposal.generate", id: r.id },
                (result) => navigate(`/proposal/${result.id}`),
              )
            }
          >
            基于新草稿重新生成
          </Button>
        )}
        <Button
          kind={conflict ? "secondary" : "primary"}
          disabled={action.busy || conflict || p.state !== "pending"}
          onClick={() =>
            void action.execute({ type: "proposal.apply", id }, () =>
              navigate(`/research/${r.id}/draft`),
            )
          }
        >
          应用到草稿
        </Button>
        <Button
          disabled={action.busy || p.state !== "pending"}
          onClick={() =>
            void action.execute({ type: "proposal.reject", id }, () =>
              navigate(`/research/${r.id}/draft`),
            )
          }
        >
          {conflict ? "放弃提案" : "拒绝提案"}
        </Button>
        <LinkButton onClick={() => navigate(`/research/${r.id}/draft`)}>
          返回草稿
        </LinkButton>
      </div>
      <details className="spacing-top">
        <summary>查看历史提案</summary>
        {state.proposals
          .filter((x) => x.researchId === r.id)
          .map((x) => (
            <p key={x.id}>
              <LinkButton onClick={() => navigate(`/proposal/${x.id}`)}>
                {x.id} · 基于 r{x.base.revision}
              </LinkButton>{" "}
              <PhaseStatus value={x.state} />
            </p>
          ))}
      </details>
    </DetailLayout>
  );
}

export function ExperimentConfirm({ id }: { id: string }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    r = state.researches.find((r) => r.id === id),
    [key] = useState(requestKey);
  if (!r) return <Empty title="研究不存在" />;
  const existing = state.experiments.find(
    (e) =>
      e.researchId === id &&
      e.reportId &&
      JSON.stringify(e.input) === JSON.stringify(r.draft),
  );
  return (
    <div className="page">
      <PageHeader
        title="确认实验输入"
        subtitle="确认后将保存输入快照，并开始一次演示实验。"
        breadcrumb={
          <LinkButton onClick={() => navigate(`/research/${id}/draft`)}>
            {r.name} / 新建实验
          </LinkButton>
        }
      />
      <div className="two-column">
        <Panel title="来源、参数与成本">
          <Snapshot input={r.draft} />
        </Panel>
        <Panel title="本次执行">
          <KeyValues
            items={[
              ["实验数", "1"],
              ["模式", "离线场景演示"],
              ["数据状态", r.draft.datasetId ? "确认后冻结" : "尚未绑定"],
              ["时间预算", "5 分钟（演示在数秒内完成）"],
            ]}
          />
          <hr />
          <h2>说明</h2>
          <p className="muted">
            完成后返回演示报告，后续实验由你确认。没有真实引擎、模型或账户调用。
          </p>
          {existing && (
            <Notice
              action={
                <LinkButton
                  onClick={() =>
                    navigate(`/report/${existing.reportId}/overview`)
                  }
                >
                  复用已有报告
                </LinkButton>
              }
            >
              已找到相同输入实验 {existing.id}。
            </Notice>
          )}
        </Panel>
      </div>
      <Notice>实验创建后，后续草稿修改不会改变本次输入。</Notice>
      <ActionError error={action.error} />
      <div className="action-footer">
        <div className="button-row" style={{ marginLeft: 0 }}>
          <Button onClick={() => navigate(`/research/${id}/draft`)}>
            取消
          </Button>
          <Button onClick={() => navigate(`/research/${id}/draft`)}>
            返回修改
          </Button>
        </div>
        <Button
          kind="primary"
          disabled={action.busy || !r.draft.datasetId || !r.draft.templateId}
          onClick={() =>
            void action.execute(
              { type: "experiment.start", id, expected: r.draft.revision, key },
              () => navigate(`/research/${id}/experiments`),
            )
          }
        >
          {action.busy ? "冻结输入中…" : "冻结输入并运行"}
        </Button>
      </div>
    </div>
  );
}
