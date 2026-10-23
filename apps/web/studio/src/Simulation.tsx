import { useState } from "react";
import { defaultSimConfig } from "./fixtures";
import type { SimConfig } from "./model";
import {
  useAction,
  useBuffer,
  useStudio,
  useUnsaved,
  money,
  requestKey,
} from "./context";
import {
  ActionError,
  Button,
  Chart,
  DetailLayout,
  Dialog,
  Empty,
  Field,
  Icon,
  Inspector,
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
import { RequestCard } from "./Research";

export function SimulationList() {
  const { state, navigate } = useStudio(),
    filter = useBuffer("simulation-filter", { status: "all", search: "" });
  const instances = state.simulations.filter(
    (s) =>
      (filter.value.status === "all" || s.status === filter.value.status) &&
      `${s.id} ${s.candidateId}`
        .toLowerCase()
        .includes(filter.value.search.toLowerCase()),
  );
  return (
    <div className="page">
      <PageHeader
        title="模拟运行"
        subtitle="管理实例，检查最新观测。"
        actions={
          <Button
            kind="primary"
            icon="settings"
            onClick={() => navigate("/simulation/new")}
          >
            配置实例
          </Button>
        }
      />
      <Tabs
        active={filter.value.status}
        onChange={(status) => filter.update({ ...filter.value, status })}
        items={[
          ["all", "全部实例"],
          ["running", "运行中"],
          ["saved", "待启动"],
          ["stopped", "已停止"],
        ].map(([id, label]) => ({
          id,
          label: `${label}（${state.simulations.filter((s) => id === "all" || s.status === id).length}）`,
        }))}
      />
      <div className="toolbar">
        <label className="search-field">
          <Icon name="search" />
          <input
            aria-label="搜索模拟实例"
            value={filter.value.search}
            onChange={(e) =>
              filter.update({ ...filter.value, search: e.target.value })
            }
            placeholder="搜索实例或候选"
          />
        </label>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>实例</th>
              <th>来源候选</th>
              <th>进程状态</th>
              <th>最新观测</th>
              <th>下一步</th>
            </tr>
          </thead>
          <tbody>
            {instances.map((s) => (
              <tr key={s.id}>
                <td>
                  <strong>{s.id}</strong>
                  <small>本地模拟 · 演示数据</small>
                </td>
                <td>
                  <LinkButton
                    onClick={() => navigate(`/candidate/${s.candidateId}`)}
                  >
                    {s.candidateId}
                  </LinkButton>
                  <small>
                    {s.input.datasetId} · r{s.input.revision}
                  </small>
                  <small>
                    {s.input.fast}/{s.input.slow}
                  </small>
                </td>
                <td>
                  <PhaseStatus value={s.status} />
                  {s.stopRequestId && (
                    <small>
                      停止请求：
                      {state.requests.find((r) => r.id === s.stopRequestId)
                        ?.state === "confirmed"
                        ? "已确认"
                        : "待确认"}
                    </small>
                  )}
                </td>
                <td>
                  {s.observedAt ?? "—"}
                  <small>
                    {s.status === "saved"
                      ? "尚未启动"
                      : s.stale
                        ? "观测陈旧，请刷新"
                        : "演示快照"}
                  </small>
                </td>
                <td>
                  <Button
                    onClick={() =>
                      navigate(
                        `/simulation/${s.id}/${s.status === "saved" ? "config" : "overview"}`,
                      )
                    }
                  >
                    {s.status === "saved" ? "审阅并启动" : "查看实例"}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!instances.length && (
          <Empty
            title="没有匹配实例"
            description="接受候选后，可以单独配置并确认启动模拟。"
            action={
              <Button onClick={() => navigate("/candidates")}>
                查看候选中心
              </Button>
            }
          />
        )}
      </div>
      <Notice>进程状态与观测新鲜度分别显示。保存配置不会启动实例。</Notice>
      <details>
        <summary>
          历史实例（
          {state.simulations.filter((s) => s.status === "stopped").length}）
        </summary>
        {state.simulations
          .filter((s) => s.status === "stopped")
          .map((s) => (
            <p key={s.id}>
              <LinkButton
                onClick={() => navigate(`/simulation/${s.id}/events`)}
              >
                {s.id} · 查看停止与事件记录
              </LinkButton>
            </p>
          ))}
      </details>
    </div>
  );
}

export function SimulationConfig({ id = "new" }: { id?: string }) {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    existing = state.simulations.find((s) => s.id === id),
    requested = new URLSearchParams(location.hash.split("?")[1]).get(
      "candidate",
    ),
    [candidateId, setCandidate] = useState(
      existing?.candidateId ??
        requested ??
        state.candidates.find((c) => c.state === "accepted")?.id ??
        "",
    ),
    candidate = state.candidates.find((c) => c.id === candidateId);
  const source = state.reports.find((r) => r.id === candidate?.reportId),
    buffer = useBuffer(
      `sim-config-${id}-${candidateId}`,
      existing?.config ?? defaultSimConfig,
    ),
    [confirm, setConfirm] = useState(false),
    [key] = useState(requestKey);
  const config = buffer.value,
    dirty =
      JSON.stringify(config) !==
        JSON.stringify(existing?.config ?? defaultSimConfig) ||
      (existing ? candidateId !== existing.candidateId : false),
    locked = !!existing && existing.status !== "saved";
  const save = async () => {
    const result = await action.execute({
      type: "simulation.save",
      id: existing?.id,
      candidateId,
      config,
    });
    if (result) {
      buffer.clear();
      toast("配置已保存，实例尚未启动。");
      navigate(`/simulation/${result.id}/config`, true);
    }
    return !!result;
  };
  useUnsaved(dirty && !locked, save, () => {
    buffer.clear();
    buffer.update(existing?.config ?? defaultSimConfig);
  });
  const fields = (items: [keyof SimConfig, string, string?][]) =>
    items.map(([key, label, hint]) => (
      <Field key={key} label={label} hint={hint}>
        <input
          type="number"
          step={["fee", "slippage"].includes(key) ? ".01" : "1"}
          min="0"
          disabled={locked}
          value={!Number.isFinite(config[key]) ? "" : config[key]}
          onChange={(e) => {
            buffer.update({
              ...config,
              [key]: e.target.value === "" ? NaN : Number(e.target.value),
            });
            action.clearError();
          }}
        />
      </Field>
    ));
  return (
    <div className="page">
      <PageHeader
        title={locked ? "模拟实例配置" : "配置模拟实例"}
        subtitle={
          locked
            ? "这是本次启动使用的只读配置。"
            : "候选已接受，运行仍由你单独启动。"
        }
        breadcrumb={
          <LinkButton onClick={() => navigate("/simulations")}>
            模拟运行 / {existing?.id ?? "配置实例"}
          </LinkButton>
        }
      />
      <div className="with-inspector">
        <div>
          <Panel>
            <Field label="来源候选">
              <select
                value={candidateId}
                disabled={locked || !!existing}
                onChange={(e) => setCandidate(e.target.value)}
              >
                <option value="">选择已接受候选</option>
                {state.candidates.map((c) => (
                  <option
                    key={c.id}
                    value={c.id}
                    disabled={c.state !== "accepted"}
                  >
                    {c.id} ·{" "}
                    {c.state === "accepted" ? "已接受" : "待审或已拒绝"}
                  </option>
                ))}
              </select>
            </Field>
            {candidate && (
              <LinkButton onClick={() => navigate(`/candidate/${candidateId}`)}>
                查看 {candidateId} 证据
              </LinkButton>
            )}
            {candidate?.state !== "accepted" && (
              <Notice
                tone="warn"
                action={
                  <LinkButton onClick={() => navigate("/candidates")}>
                    审阅候选
                  </LinkButton>
                }
              >
                请先完成人工候选决定。
              </Notice>
            )}
          </Panel>
          <div className="two-column">
            <section>
              <h2>资金与成本</h2>
              {fields([
                ["cash", "初始模拟资金（USD）", "用于本次模拟运行的初始资金。"],
                ["fee", "手续费（%）"],
                ["slippage", "滑点（%）"],
              ])}
            </section>
            <section>
              <h2>运行限额</h2>
              {fields([
                ["maxOrder", "单笔最大名义金额（USD）"],
                ["maxPosition", "最大持仓比例（%）"],
                ["maxPositions", "同时持仓上限"],
              ])}
            </section>
          </div>
          <hr />
          <div className="form-grid">
            <Field label="数据频率">
              <select
                value={config.frequency}
                disabled={locked}
                onChange={(e) =>
                  buffer.update({ ...config, frequency: e.target.value })
                }
              >
                <option>日线</option>
                <option>1小时</option>
              </select>
            </Field>
            <Field label="运行模式">
              <input readOnly value="本地模拟 · 场景演示" />
            </Field>
          </div>
          <ActionError error={action.error || buffer.bufferError} />
          <Notice>
            本次配置不会创建运行中的进程。确认启动后才生成演示观测。
          </Notice>
        </div>
        <Panel title="配置概览">
          <KeyValues
            items={[
              ["来源候选", candidateId || "未选择"],
              [
                "策略状态",
                candidate?.state === "accepted" ? "已接受候选" : "待确认",
              ],
              ["数据参考", source?.input.datasetId ?? "—"],
              [
                "草稿",
                source
                  ? `r${source.input.revision} · ${source.input.fast}/${source.input.slow}`
                  : "—",
              ],
              ["初始资金", `${money(config.cash)} USD`],
              ["手续费 / 滑点", `${config.fee}% / ${config.slippage}%`],
              ["单笔限额", `${money(config.maxOrder)} USD`],
              ["最大持仓", `${config.maxPosition}%`],
              ["持仓上限", config.maxPositions],
              [
                "配置版本",
                existing ? `v${existing.configVersion}` : "尚未保存",
              ],
              [
                "状态",
                existing ? (
                  <PhaseStatus value={existing.status} />
                ) : (
                  <Status>尚未保存</Status>
                ),
              ],
            ]}
          />
          {!existing && <small>配置尚未保存，不能启动。</small>}
        </Panel>
      </div>
      <div className="action-footer">
        <small>保存配置不会启动实例。</small>
        <div className="button-row">
          <Button onClick={() => navigate("/simulations")}>返回实例</Button>
          {!locked && (
            <>
              <Button
                disabled={action.busy || candidate?.state !== "accepted"}
                onClick={() => void save()}
              >
                保存配置
              </Button>
              <Button
                kind="primary"
                disabled={
                  !existing ||
                  dirty ||
                  action.busy ||
                  candidate?.state !== "accepted"
                }
                onClick={() => setConfirm(true)}
              >
                审阅并启动
              </Button>
            </>
          )}
        </div>
      </div>
      {confirm && existing && (
        <Dialog title="启动模拟实例？" onClose={() => setConfirm(false)}>
          <p>
            {existing.id} <Status>未启动</Status>
          </p>
          <KeyValues
            items={[
              ["来源候选", `${candidateId} · 已接受`],
              ["配置版本", `v${existing.configVersion}`],
              [
                "策略草稿",
                `r${existing.input.revision} · ${existing.input.fast}/${existing.input.slow}`,
              ],
              ["数据参考", existing.input.datasetId],
              ["初始模拟资金", `${money(existing.config.cash)} USD`],
              [
                "手续费 / 滑点",
                `${existing.config.fee}% / ${existing.config.slippage}%`,
              ],
              ["单笔最大金额", `${money(existing.config.maxOrder)} USD`],
              ["最大持仓比例", `${existing.config.maxPosition}%`],
              ["同时持仓上限", existing.config.maxPositions],
            ]}
          />
          <Notice>启动后按此配置生成模拟观测。配置变更需重新确认。</Notice>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setConfirm(false)}>返回修改</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute(
                  {
                    type: "simulation.start",
                    id: existing.id,
                    expected: existing.configVersion,
                    key,
                  },
                  () => navigate(`/simulation/${existing.id}/overview`, true),
                )
              }
            >
              确认启动模拟
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function SimulationDetail({
  id,
  tab = "overview",
}: {
  id: string;
  tab?: string;
}) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    instance = state.simulations.find((s) => s.id === id),
    [stopDialog, setStopDialog] = useState(false),
    [selected, setSelected] = useState(""),
    [search, setSearch] = useState(""),
    [eventType, setEventType] = useState("all"),
    [key] = useState(requestKey);
  if (!instance) return <Empty title="实例不存在" />;
  const stop = state.requests.find((r) => r.id === instance.stopRequestId),
    equity = instance.cash + instance.quantity * instance.quote,
    event = instance.events.find((e) => e.id === selected);
  const header = (
    <>
      <PageHeader
        title={
          state.researches.find(
            (r) =>
              r.id ===
              state.candidates.find((c) => c.id === instance.candidateId)
                ?.researchId,
          )?.name ?? "模拟实例"
        }
        badge={<PhaseStatus value={instance.status} />}
        subtitle={`${id} · ${instance.candidateId} · 本地模拟`}
        breadcrumb={
          <LinkButton onClick={() => navigate("/simulations")}>
            模拟运行 / {id}
          </LinkButton>
        }
        actions={
          <>
            <Button onClick={() => navigate(`/simulation/${id}/config`)}>
              查看配置
            </Button>
            <Button
              icon="stop"
              disabled={instance.status !== "running" || !!stop}
              onClick={() => setStopDialog(true)}
            >
              停止实例
            </Button>
          </>
        }
      />
      <Tabs
        active={tab}
        onChange={(t) => {
          setSelected("");
          navigate(`/simulation/${id}/${t}`);
        }}
        items={[
          { id: "overview", label: "概览" },
          { id: "positions", label: "持仓" },
          { id: "trades", label: "成交" },
          { id: "events", label: "事件" },
        ]}
      />
      <div className="section-heading">
        <small>最新观测 {instance.observedAt ?? "尚未启动"} · 演示快照</small>
        <Button
          kind="ghost"
          icon="refresh"
          disabled={action.busy}
          onClick={() =>
            void action.execute({ type: "simulation.refresh", id })
          }
        >
          刷新观测
        </Button>
      </div>
      {instance.stale && (
        <Notice tone="warn">
          观测可能已过期。当前仍显示最后确认的进程状态，不能据此判断实例已停止。
        </Notice>
      )}
      <ActionError error={action.error} />
      {stop && <RequestCard requestId={stop.id} />}
    </>
  );
  const overview = (
    <>
      <div className="metric-strip sim-metrics">
        {[
          ["模拟权益", equity],
          ["模拟现金", instance.cash],
          ["浮动盈亏", instance.quantity * (instance.quote - instance.cost)],
        ].map(([name, value]) => (
          <div key={name}>
            <small>{name}（USD）</small>
            <strong>{money(Number(value))}</strong>
          </div>
        ))}
      </div>
      <Chart
        title="权益变化"
        normalize={false}
        series={[{ name: "模拟权益", points: instance.equity }]}
      />
      <div className="two-column">
        <Panel
          title="最新事件"
          actions={
            <LinkButton onClick={() => navigate(`/simulation/${id}/events`)}>
              查看全部
            </LinkButton>
          }
        >
          <ul className="timeline">
            {instance.events.slice(0, 4).map((e) => (
              <li key={e.id}>
                <div>
                  <small>
                    {e.time.slice(11)} · {e.type}
                  </small>
                  <p style={{ margin: 0 }}>{e.summary}</p>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
        <Panel title="来源与配置">
          <KeyValues
            items={[
              ["来源候选", instance.candidateId],
              [
                "数据与草稿",
                `${instance.input.datasetId} · r${instance.input.revision}`,
              ],
              ["参数设置", `${instance.input.fast}/${instance.input.slow}`],
              ["配置版本", `v${instance.configVersion}`],
              [
                "费用设置",
                `${instance.config.fee}% / ${instance.config.slippage}%`,
              ],
            ]}
          />
        </Panel>
      </div>
      <Notice>运行状态与观测时间独立显示。演示快照不构成真实交易记录。</Notice>
    </>
  );
  const content = (
    <>
      {header}
      {tab === "overview" ? (
        overview
      ) : (
        <div className={selected ? "with-inspector" : ""}>
          <section>
            {tab === "events" && (
              <div className="toolbar">
                <select
                  aria-label="事件类型"
                  value={eventType}
                  onChange={(e) => setEventType(e.target.value)}
                >
                  <option value="all">全部事件</option>
                  {[...new Set(instance.events.map((e) => e.type))].map(
                    (type) => (
                      <option key={type}>{type}</option>
                    ),
                  )}
                </select>
                <label className="search-field">
                  <Icon name="search" />
                  <input
                    aria-label="搜索事件"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="搜索事件"
                  />
                </label>
              </div>
            )}
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {(tab === "events"
                      ? ["时间", "类型", "摘要"]
                      : tab === "positions"
                        ? [
                            "代码",
                            "方向",
                            "数量",
                            "开仓均价",
                            "当前价（快照）",
                            "浮动盈亏",
                          ]
                        : ["时间", "标的", "方向", "数量", "价格"]
                    ).map((t) => (
                      <th key={t}>{t}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tab === "events"
                    ? instance.events
                        .filter(
                          (e) =>
                            (eventType === "all" || e.type === eventType) &&
                            `${e.summary} ${e.type}`.includes(search),
                        )
                        .map((e) => (
                          <tr
                            key={e.id}
                            className={selected === e.id ? "selected" : ""}
                          >
                            <td>
                              <LinkButton onClick={() => setSelected(e.id)}>
                                {e.time.slice(11)}
                              </LinkButton>
                            </td>
                            <td>{e.type}</td>
                            <td>{e.summary}</td>
                          </tr>
                        ))
                    : instance.quantity > 0 &&
                      (tab === "positions" ? (
                        <tr>
                          <td>
                            <LinkButton onClick={() => setSelected("position")}>
                              AAPL
                            </LinkButton>
                          </td>
                          <td>多</td>
                          <td>{instance.quantity}</td>
                          <td>{money(instance.cost)}</td>
                          <td>{money(instance.quote)}</td>
                          <td>
                            {money(
                              instance.quantity *
                                (instance.quote - instance.cost),
                            )}
                          </td>
                        </tr>
                      ) : (
                        <tr>
                          <td>
                            <LinkButton onClick={() => setSelected("trade")}>
                              {
                                instance.events.find(
                                  (e) => e.type === "模拟成交",
                                )?.time
                              }
                            </LinkButton>
                          </td>
                          <td>AAPL</td>
                          <td>买入</td>
                          <td>{instance.quantity}</td>
                          <td>{money(instance.cost)}</td>
                        </tr>
                      ))}
                </tbody>
              </table>
              {tab !== "events" && !instance.quantity && (
                <Empty
                  title={tab === "positions" ? "当前没有持仓" : "当前没有成交"}
                  description="本实例已启动演示观测，尚未产生模拟成交。"
                />
              )}
            </div>
          </section>
          {selected && (
            <Inspector
              title={
                tab === "events"
                  ? "事件详情"
                  : tab === "positions"
                    ? "持仓详情"
                    : "成交详情"
              }
              onClose={() => setSelected("")}
            >
              <KeyValues
                items={[
                  ["来源", id],
                  ["候选", instance.candidateId],
                  ["时间", event?.time ?? instance.observedAt],
                  [
                    "类型",
                    event?.type ??
                      (tab === "positions" ? "持仓快照" : "模拟成交"),
                  ],
                  ["摘要", event?.summary ?? `AAPL · ${instance.quantity} 股`],
                  ...(event?.type === "模拟成交" ||
                  selected === "position" ||
                  selected === "trade"
                    ? ([
                        ["数量", instance.quantity],
                        ["成交均价", `${money(instance.cost)} USD`],
                        ["报价", `${money(instance.quote)} USD`],
                      ] as [string, string | number][])
                    : []),
                ]}
              />
              <div className="button-row">
                <LinkButton
                  onClick={() => {
                    setSelected("position");
                    navigate(`/simulation/${id}/positions`);
                  }}
                >
                  查看对应持仓
                </LinkButton>
                <LinkButton
                  onClick={() => navigate(`/simulation/${id}/config`)}
                >
                  查看配置
                </LinkButton>
              </div>
              <Notice>仅为模拟记录示例，时间和来源按当前观测保留。</Notice>
            </Inspector>
          )}
        </div>
      )}
      {stopDialog && (
        <Dialog title="停止模拟实例？" onClose={() => setStopDialog(false)}>
          <p>{id} · 当前仍为运行中</p>
          <Notice tone="warn">
            确认后发送一次停止请求。收到确认前不会显示已停止。
          </Notice>
          <Snapshot input={instance.input} />
          <p>停止接收新信号，保留当前持仓快照与记录。不自动平仓。</p>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setStopDialog(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={action.busy}
              onClick={() =>
                void action.execute({ type: "simulation.stop", id, key }, () =>
                  setStopDialog(false),
                )
              }
            >
              提交停止请求
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
  return tab === "overview" ? (
    <DetailLayout
      objectId={id}
      context={`${id} · 观测 ${instance.observedAt ?? "尚未启动"}`}
      references={[
        { label: "最新观测 / 事件", route: `/simulation/${id}/events` },
        { label: "来源候选", route: `/candidate/${instance.candidateId}` },
      ]}
    >
      {content}
    </DetailLayout>
  ) : (
    <div className="page">{content}</div>
  );
}
