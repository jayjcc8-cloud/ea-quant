import { useState } from "react";
import { useAction, useBuffer, useStudio, useUnsaved, money } from "./context";
import {
  Button,
  Dialog,
  Field,
  Icon,
  KeyValues,
  LinkButton,
  Notice,
  PageHeader,
  Panel,
  Status,
  Tabs,
} from "./ui";

export function PreviewBoundary() {
  return (
    <Notice tone="warn">
      <strong>功能预览 · 未接入</strong>
      <br />
      此页面仅展示未来交互，所有金额、账户与委托均为演示数据。
    </Notice>
  );
}
export function LiveWorkspace({ detail = false }: { detail?: boolean }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    config = state.livePreview,
    name =
      state.researches.find(
        (r) =>
          r.id ===
          state.candidates.find((c) => c.id === config.candidate)?.researchId,
      )?.name ?? "策略预览",
    [tab, setTab] = useState("overview"),
    [stopDialog, setStopDialog] = useState(false),
    stop = state.liveStop;
  return (
    <div className="page">
      <PageHeader
        title={detail ? name : "实盘运行"}
        subtitle={
          detail
            ? "演示运行详情 · 未接入真实环境"
            : "连接策略、账户与执行状态。"
        }
        breadcrumb={
          detail ? (
            <LinkButton onClick={() => navigate("/live")}>
              实盘运行 / {name}
            </LinkButton>
          ) : undefined
        }
        actions={
          <>
            <Button onClick={() => navigate("/live/config")}>
              {detail ? "查看配置" : "查看启动配置"}
            </Button>
            {detail && (
              <Button
                kind="primary"
                disabled={stop !== "running"}
                onClick={() => setStopDialog(true)}
              >
                预览停止请求
              </Button>
            )}
          </>
        }
      />
      <PreviewBoundary />
      <Tabs
        active={tab}
        onChange={setTab}
        items={[
          { id: "overview", label: "运行概览" },
          { id: "orders", label: "委托" },
          { id: "events", label: "事件" },
        ]}
      />
      {stop !== "running" && (
        <Panel
          title={
            stop === "stopped"
              ? "预览停止已确认"
              : stop === "unknown"
                ? "预览结果未知"
                : "预览请求已提交"
          }
        >
          <Notice tone="warn">
            {stop === "stopped"
              ? "演示确认已到达；没有真实实例被停止。"
              : "PREVIEW-STOP-001 · 等待确认，保留最后观测，不能推断已停止。"}
          </Notice>
          {stop !== "stopped" && (
            <Button
              onClick={() =>
                void action.execute({ type: "live.preview.query" })
              }
            >
              查询原预览请求
            </Button>
          )}
        </Panel>
      )}
      {tab === "overview" ? (
        <>
          <div className="metric-strip">
            <div>
              <small>数据环境</small>
              <strong style={{ fontSize: "1.3rem" }}>演示数据</strong>
            </div>
            <div>
              <small>预览策略</small>
              <strong>1</strong>
            </div>
            <div>
              <small>待处理委托</small>
              <strong>0</strong>
            </div>
            <div>
              <small>最近同步</small>
              <strong>—</strong>
            </div>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>策略</th>
                  <th>账户</th>
                  <th>预览状态</th>
                  <th>资金限额</th>
                  <th>最近事件</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>
                    {name}
                    <small>
                      {config.candidate} · {config.symbol}
                    </small>
                  </td>
                  <td>演示账户</td>
                  <td>
                    <Status tone="blue">
                      {stop === "stopped"
                        ? "预览已停止"
                        : "最后观测：预览运行中"}
                    </Status>
                  </td>
                  <td>
                    {money(config.cash)} USD<small>演示数据</small>
                  </td>
                  <td>暂无真实执行连接</td>
                  <td>
                    <LinkButton
                      onClick={() =>
                        detail ? setTab("events") : navigate("/live/detail")
                      }
                    >
                      {detail ? "查看事件" : "查看详情"}
                    </LinkButton>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="two-column spacing-top">
            <Panel title="事件记录">
              <ul className="timeline">
                <li>生成预览数据，没有发送真实委托。</li>
                <li>账户、行情和执行能力均未接入。</li>
                <li>配置保存不会授予执行权限。</li>
              </ul>
              <LinkButton onClick={() => setTab("events")}>
                查看全部事件
              </LinkButton>
            </Panel>
            <Panel title="接入能力">
              {[
                ["folder", "账户读取"],
                ["chart", "行情订阅"],
                ["file", "订单执行"],
              ].map(([icon, name]) => (
                <div className="capability" key={name}>
                  <Icon name={icon} />
                  <div>
                    <strong>{name}</strong>
                  </div>
                  <Status>未接入</Status>
                </div>
              ))}
              <LinkButton onClick={() => navigate("/exchanges")}>
                查看交易所能力配置
              </LinkButton>
            </Panel>
          </div>
        </>
      ) : tab === "orders" ? (
        <Panel title="委托 · 演示数据">
          <p>当前预览没有委托；未连接账户或订单执行服务。</p>
          <Button onClick={() => navigate("/live/config")}>
            查看执行配置预览
          </Button>
        </Panel>
      ) : (
        <Panel title="执行日志 · 演示数据">
          <KeyValues
            items={[
              ["14:20:00", "生成预览配置；未提交真实请求"],
              ["14:20:01", "账户读取：未接入"],
              ["14:20:02", "行情订阅：未接入"],
              ["14:20:03", "订单执行：未接入"],
              [
                "停止预览",
                stop === "running" ? "未提交" : `PREVIEW-STOP-001 · ${stop}`,
              ],
            ]}
          />
        </Panel>
      )}
      {stopDialog && (
        <Dialog title="预览停止请求" onClose={() => setStopDialog(false)}>
          <PreviewBoundary />
          <p>此操作演示请求流程，不会改变真实账户。</p>
          <Notice tone="warn">
            若响应中断，状态为“结果未知”，请核查原请求。
          </Notice>
          <div className="steps">
            <span>
              <i>1</i>提交请求
            </span>
            <span>
              <i>2</i>等待确认
            </span>
            <span>
              <i>3</i>确认后显示停止
            </span>
          </div>
          <label className="inline-check">
            <input type="radio" checked readOnly />
            停止接收新信号
          </label>
          <p className="muted">不自动平仓，当前持仓保持不变。</p>
          <div className="dialog-actions">
            <Button onClick={() => setStopDialog(false)}>取消</Button>
            <Button
              kind="primary"
              onClick={() => {
                void action.execute({ type: "live.preview.stop" });
                setStopDialog(false);
              }}
            >
              提交预览请求
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function LiveConfig() {
  const { state, navigate, toast } = useStudio(),
    action = useAction(),
    buffer = useBuffer("live-config", state.livePreview),
    saved = JSON.stringify(state.livePreview),
    [preview, setPreview] = useState(false),
    [error, setError] = useState("");
  const save = async () => {
    setError("");
    const result = await action.execute({
      type: "live.preview.save",
      config: buffer.value,
    });
    if (result) {
      buffer.clear();
      toast("配置预览已保存，没有授予执行权限。");
    }
    return !!result;
  };
  useUnsaved(saved !== JSON.stringify(buffer.value), save, () =>
    buffer.update(JSON.parse(saved)),
  );
  return (
    <div className="page">
      <PageHeader
        title="启动配置"
        badge={<Status tone="blue">预览</Status>}
        breadcrumb={
          <LinkButton onClick={() => navigate("/live")}>
            实盘运行 / 启动配置
          </LinkButton>
        }
      />
      <div className="with-inspector">
        <section>
          <PreviewBoundary />
          <Field label="策略版本">
            <select
              value={buffer.value.candidate}
              onChange={(e) =>
                buffer.update({ ...buffer.value, candidate: e.target.value })
              }
            >
              {state.candidates.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id} · {c.state === "accepted" ? "已接受" : "待审或已拒绝"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="账户">
            <input readOnly value="演示账户（未连接）" />
          </Field>
          <hr />
          <h2>资金与限制 · 演示数据</h2>
          <div className="form-grid three">
            {(
              [
                ["cash", "资金上限（USD）"],
                ["order", "单笔上限（USD）"],
                ["loss", "每日亏损限额（USD）"],
              ] as const
            ).map(([key, label]) => (
              <Field key={key} label={label}>
                <input
                  type="number"
                  min="1"
                  value={buffer.value[key]}
                  onChange={(e) =>
                    buffer.update({
                      ...buffer.value,
                      [key]: Number(e.target.value),
                    })
                  }
                />
              </Field>
            ))}
          </div>
          <details open>
            <summary>执行设置</summary>
            <div className="form-grid">
              <Field label="交易标的">
                <select
                  value={buffer.value.symbol}
                  onChange={(e) =>
                    buffer.update({ ...buffer.value, symbol: e.target.value })
                  }
                >
                  <option>AAPL</option>
                  <option>MSFT</option>
                </select>
              </Field>
              <Field label="订单类型">
                <select
                  value={buffer.value.orderType}
                  onChange={(e) =>
                    buffer.update({
                      ...buffer.value,
                      orderType: e.target.value,
                    })
                  }
                >
                  <option>限价</option>
                  <option>市价</option>
                </select>
              </Field>
            </div>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={buffer.value.allowOpen}
                onChange={(e) =>
                  buffer.update({
                    ...buffer.value,
                    allowOpen: e.target.checked,
                  })
                }
              />
              允许新开仓（仅预览）
            </label>
          </details>
          <details>
            <summary>交易时段</summary>
            <Field label="允许交易时间">
              <input
                value={buffer.value.session}
                onChange={(e) =>
                  buffer.update({ ...buffer.value, session: e.target.value })
                }
              />
            </Field>
          </details>
          <details>
            <summary>订单策略</summary>
            <Field label="有效期">
              <select
                value={buffer.value.expiry}
                onChange={(e) =>
                  buffer.update({ ...buffer.value, expiry: e.target.value })
                }
              >
                <option>当日有效</option>
                <option>撤销前有效</option>
              </select>
            </Field>
          </details>
          <details>
            <summary>风险规则</summary>
            <Field label="止损阈值（%）">
              <input
                type="number"
                min="1"
                max="100"
                value={buffer.value.stopLoss}
                onChange={(e) =>
                  buffer.update({
                    ...buffer.value,
                    stopLoss: Number(e.target.value),
                  })
                }
              />
            </Field>
            <small>仅用于配置预览，不作为真实风险执行规则。</small>
          </details>
          {(error || action.error) && (
            <Notice tone="error">{error || action.error}</Notice>
          )}
        </section>
        <Panel title="启动前检查">
          {["策略来源", "账户连接", "行情连接", "执行权限"].map((label, i) => (
            <div className="capability" key={label}>
              <Icon name={i ? "lock" : "file"} />
              <div>
                <strong>{label}</strong>
                <small>
                  {i ? "当前未接入真实服务" : buffer.value.candidate}
                </small>
              </div>
              <Status tone={i ? "warn" : "blue"}>
                {i ? "未接入" : "已选择"}
              </Status>
            </div>
          ))}
          <Notice>当前仅可查看预览。真实启动不可用。</Notice>
        </Panel>
      </div>
      <div className="action-footer">
        <Button onClick={() => navigate("/live")}>返回</Button>
        <div className="button-row">
          <Button disabled={action.busy} onClick={() => void save()}>
            保存配置预览
          </Button>
          <Button
            kind="primary"
            disabled={action.busy}
            onClick={async () => {
              if (await save()) setPreview(true);
            }}
          >
            查看启动预览
          </Button>
          <Button disabled icon="lock">
            启动实盘
          </Button>
        </div>
      </div>
      {preview && (
        <Dialog title="启动配置预览" onClose={() => setPreview(false)}>
          <PreviewBoundary />
          <KeyValues
            items={[
              ["策略", buffer.value.candidate],
              ["账户", "演示账户 · 未连接"],
              ["资金上限", `${money(buffer.value.cash)} USD`],
              ["单笔上限", `${money(buffer.value.order)} USD`],
              ["每日亏损限额", `${money(buffer.value.loss)} USD`],
              ["交易标的", buffer.value.symbol],
              ["订单类型", buffer.value.orderType],
              ["允许新开仓", buffer.value.allowOpen ? "是（预览）" : "否"],
            ]}
          />
          <div className="dialog-actions">
            <Button onClick={() => setPreview(false)}>返回修改</Button>
            <Button kind="primary" onClick={() => navigate("/live/detail")}>
              查看演示详情
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function Exchanges() {
  const { navigate } = useStudio(),
    [plan, setPlan] = useState(false);
  return (
    <div className="page">
      <PageHeader
        title="交易所配置"
        subtitle="分别理解账户读取、行情订阅和订单执行能力。"
      />
      <PreviewBoundary />
      <div className="connection-layout">
        <Panel title="全部连接">
          <button
            className="directory-item selected"
            onClick={() => setPlan(true)}
          >
            <Icon name="chart" />
            <span>
              <strong>演示交易所</strong>
              <small>示例账户 · 未接入</small>
            </span>
          </button>
          <Button icon="plus" onClick={() => setPlan(true)}>
            添加交易所预览
          </Button>
        </Panel>
        <section>
          <h1>
            演示交易所 <Status>未接入</Status>
          </h1>
          {[
            ["folder", "账户读取", "查看账户概况、资产与持仓。"],
            ["chart", "行情订阅", "获取指定市场的实时与历史行情。"],
            ["file", "订单执行", "提交、撤销与查询委托。"],
          ].map(([icon, title, text]) => (
            <div className="capability" key={title}>
              <Icon name={icon} size={25} />
              <div>
                <strong>{title}</strong>
                <small>{text}</small>
              </div>
              <Status>未接入</Status>
              <Button disabled>配置</Button>
            </div>
          ))}
          <h2 className="spacing-top">规划中的授权范围</h2>
          <p className="muted">连接账户不自动授予执行权限。</p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>能力</th>
                  <th>权限</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["账户读取", "读取资产与持仓"],
                  ["行情订阅", "订阅与获取行情"],
                  ["订单执行", "提交、撤销与查询委托及成交"],
                ].map(([ability, scope]) => (
                  <tr key={ability}>
                    <td>{ability}</td>
                    <td>{scope}</td>
                    <td>未授权</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="button-row spacing-top">
            <Button kind="primary" onClick={() => setPlan(true)}>
              查看接入规划
            </Button>
            <Button onClick={() => navigate("/live")}>返回实盘预览</Button>
          </div>
        </section>
      </div>
      {plan && (
        <Dialog title="交易所接入规划" onClose={() => setPlan(false)}>
          <PreviewBoundary />
          <ol>
            <li>分别提供账户读取、行情订阅与订单执行能力。</li>
            <li>明确每个能力的权限、连接状态与观测时间。</li>
            <li>执行能力必须另行人工授权，并提供请求查询与恢复。</li>
          </ol>
          <p className="muted">本轮不接入真实服务，不采集密钥。</p>
          <div className="dialog-actions">
            <Button onClick={() => setPlan(false)}>关闭规划</Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
