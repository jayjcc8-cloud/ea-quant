import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type { ButtonHTMLAttributes, ReactElement, ReactNode } from "react";
import { useAction, useBuffer, useStudio } from "./context";
import type { Draft, Point, Report } from "./model";
import { metrics } from "./model";
import { Icon, AgentSurface, DetailSurface } from "./layout";
export { Icon, Logo, PageHeader, Tabs } from "./layout";

export function Button({
  children,
  kind = "secondary",
  icon,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  kind?: "primary" | "secondary" | "ghost" | "danger";
  icon?: string;
}) {
  return (
    <button type="button" className={`button ${kind}`} {...props}>
      {icon && <Icon name={icon} />}
      <span>{children}</span>
    </button>
  );
}
export function IconButton({
  name,
  label,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { name: string; label: string }) {
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      {...props}
    >
      <Icon name={name} />
    </button>
  );
}
export function LinkButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="text-link" onClick={onClick}>
      {children}
    </button>
  );
}
export function Status({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad" | "blue";
}) {
  return <span className={`status ${tone}`}>{children}</span>;
}
export function PhaseStatus({ value }: { value: string }) {
  const labels: Record<string, string> = {
    saved: "配置已保存",
    running: "运行中",
    completed: "已完成",
    failed: "已失败",
    unknown: "结果未知",
    stopped: "已停止",
    pending: "待审阅",
    accepted: "已接受",
    rejected: "已拒绝",
    confirmed: "已确认",
    applied: "已应用",
    superseded: "已保留原案",
  };
  return (
    <Status
      tone={
        ["completed", "accepted", "confirmed", "applied"].includes(value)
          ? "good"
          : ["unknown", "pending"].includes(value)
            ? "warn"
            : value === "failed"
              ? "bad"
              : value === "running"
                ? "blue"
                : "neutral"
      }
    >
      {labels[value] ?? value}
    </Status>
  );
}
export function Notice({
  children,
  tone = "info",
  action,
}: {
  children: ReactNode;
  tone?: "info" | "warn" | "error" | "success";
  action?: ReactNode;
}) {
  return (
    <div
      className={`notice ${tone}`}
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon name={tone === "success" ? "check" : "info"} />
      <div>{children}</div>
      {action && <div className="notice-action">{action}</div>}
    </div>
  );
}
export function ActionError({ error }: { error: string }) {
  return error ? <Notice tone="error">{error}</Notice> : null;
}
export function Panel({
  title,
  children,
  actions,
  className = "",
}: {
  title?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <div className="section-heading">
          <h2>{title}</h2>
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {isValidElement(children)
        ? cloneElement(
            children as ReactElement<{
              id: string;
              "aria-describedby"?: string;
            }>,
            { id, "aria-describedby": hint ? `${id}-hint` : undefined },
          )
        : children}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
export function KeyValues({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="key-values">
      {items.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
export function Empty({
  title = "暂无记录",
  description = "调整筛选条件，或从一个新想法开始。",
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name="file" size={32} />
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Dialog({
  title,
  children,
  onClose,
  wide = false,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null),
    id = useId(),
    closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    const el = ref.current,
      prior = document.activeElement as HTMLElement;
    el?.showModal();
    return () => {
      el?.close();
      if (prior?.isConnected) prior.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={id}
      className={`dialog ${wide ? "wide" : ""} ${className}`}
      onCancel={(event) => {
        event.preventDefault();
        closeRef.current();
      }}
      onClick={(e) => {
        if (e.target === ref.current) closeRef.current();
      }}
    >
      <div className="dialog-head">
        <h2 id={id}>{title}</h2>
        <IconButton name="close" label="关闭对话框" onClick={onClose} />
      </div>
      {children}
    </dialog>
  );
}
export function Inspector({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose?: () => void;
}) {
  return (
    <aside className="inspector">
      <div className="section-heading">
        <h2>{title}</h2>
        {onClose && (
          <IconButton name="close" label="关闭检查器" onClick={onClose} />
        )}
      </div>
      {children}
    </aside>
  );
}
export function Snapshot({ input }: { input: Draft }) {
  return (
    <KeyValues
      items={[
        ["草稿版本", `r${input.revision}`],
        ["数据版本", input.datasetId || "尚未绑定"],
        ["模板", input.templateId || "尚未绑定"],
        ["样本区间", `${input.start} — ${input.end}`],
        ["快 / 慢均线", `${input.fast} / ${input.slow}`],
        ["手续费", `${input.fee.toFixed(2)}%`],
        ["滑点", `${input.slippage.toFixed(2)}%`],
        ["仓位比例", `${input.allocation}%`],
      ]}
    />
  );
}
export function Metrics({ report }: { report: Report }) {
  const m = metrics(report);
  return (
    <div className="metric-strip">
      {[
        ["净收益（演示）", `${m.net.toFixed(1)}%`],
        ["最大回撤", `${m.drawdown.toFixed(1)}%`],
        ["交易次数", m.count],
        ["费用影响", `${m.cost.toFixed(2)}%`],
      ].map(([k, v]) => (
        <div key={k}>
          <small>{k}</small>
          <strong>{v}</strong>
        </div>
      ))}
    </div>
  );
}
export function Chart({
  series,
  title = "净值曲线",
  normalize = true,
}: {
  series: { name: string; points: Point[] }[];
  title?: string;
  normalize?: boolean;
}) {
  const [visible, setVisible] = useState(series.map(() => true)),
    [selected, setSelected] = useState<number | null>(null),
    container = useRef<HTMLElement>(null),
    [width, setWidth] = useState(780);
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver((entries) =>
      setWidth(Math.max(280, entries[0].contentRect.width)),
    );
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const data = series.map((s) =>
    s.points.map((p) => ({
      ...p,
      value: normalize ? p.value / (s.points[0]?.value || 1) : p.value,
    })),
  );
  const values = data.flat().map((p) => p.value),
    min = Math.min(...values) * 0.99,
    max = Math.max(...values) * 1.01;
  const x = (n: number, total: number) =>
      65 + (n * (width - 90)) / Math.max(1, total - 1),
    y = (value: number) =>
      210 - ((value - min) / Math.max(0.001, max - min)) * 170;
  return (
    <section ref={container} className="chart panel">
      <div className="section-heading">
        <h2>
          {title} <small>演示数据</small>
        </h2>
        <div className="chart-legend">
          {series.map((s, i) => (
            <button
              key={s.name}
              aria-pressed={visible[i]}
              onClick={() =>
                setVisible((v) => v.map((b, j) => (i === j ? !b : b)))
              }
            >
              <i className={`series-${i}`} />
              {s.name}
            </button>
          ))}
        </div>
      </div>
      <svg
        viewBox={`0 0 ${width} 255`}
        role="img"
        aria-label={`${title}，${series.map((s, i) => `${s.name}终值 ${data[i].at(-1)?.value.toFixed(normalize ? 3 : 2)}`).join("；")}`}
      >
        {[0, 1, 2, 3, 4].map((i) => {
          const value = min + ((max - min) * i) / 4;
          return (
            <g key={i}>
              <line
                x1="65"
                x2={width - 25}
                y1={y(value)}
                y2={y(value)}
                className="grid-line"
              />
              <text x="58" y={y(value) + 4} textAnchor="end">
                {normalize
                  ? value.toFixed(2)
                  : Math.round(value).toLocaleString("en-US")}
              </text>
            </g>
          );
        })}
        {data.map(
          (points, i) =>
            visible[i] && (
              <g key={i}>
                <polyline
                  className={`plot series-${i}`}
                  points={points
                    .map((p, j) => `${x(j, points.length)},${y(p.value)}`)
                    .join(" ")}
                />
                {points.map((p, j) => (
                  <circle
                    key={j}
                    cx={x(j, points.length)}
                    cy={y(p.value)}
                    r={selected === j ? 5 : 3}
                    className={`point series-${i}`}
                  >
                    <title>
                      {p.date}：{p.value.toFixed(normalize ? 3 : 2)}
                    </title>
                  </circle>
                ))}
              </g>
            ),
        )}
        {[
          0,
          Math.floor((data[0]?.length ?? 1) / 2),
          (data[0]?.length ?? 1) - 1,
        ].map((n, i) => (
          <text
            key={i}
            x={x(n, data[0]?.length ?? 1)}
            y="241"
            textAnchor={i === 2 ? "end" : i === 0 ? "start" : "middle"}
          >
            {data[0]?.[n]?.date}
          </text>
        ))}
      </svg>
      <details>
        <summary>查看曲线数据表</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>日期</th>
                {series.map((s) => (
                  <th key={s.name}>{s.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data[0]?.map((p, i) => (
                <tr key={p.date} onMouseEnter={() => setSelected(i)}>
                  <td>{p.date}</td>
                  {data.map((values, j) => (
                    <td key={j}>
                      {values[i]?.value.toFixed(normalize ? 3 : 2) ?? "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}
function ReportEvidence({ report }: { report: Report }) {
  const { navigate } = useStudio();
  const result = metrics(report);
  return (
    <section className="report-evidence" aria-label="报告解读与证据">
      <div className="report-evidence-heading">
        <h2>这份报告说明了什么</h2>
        <small>离线演示 · 按冻结数据汇总 · 未调用模型</small>
      </div>
      <div className="report-reading">
        <p>
          本场景净收益 <strong>{result.net.toFixed(1)}%</strong>，
          最大回撤 <strong>{Math.abs(result.drawdown).toFixed(1)}%</strong>。
        </p>
        <p>
          共 {result.count} 笔交易，费用影响 {result.cost.toFixed(2)}%。
          单次场景不足以判断策略稳健性。
        </p>
      </div>
      <div className="report-evidence-links" aria-label="报告证据">
        <LinkButton onClick={() => navigate(`/report/${report.id}/overview`)}>
          [1] {report.id} · 指标与净值
        </LinkButton>
        <LinkButton onClick={() => navigate(`/report/${report.id}/inputs`)}>
          [2] 冻结输入 · r{report.input.revision}
        </LinkButton>
        <LinkButton onClick={() => navigate(`/report/${report.id}/trades`)}>
          [3] 交易明细 · {result.count} 笔
        </LinkButton>
      </div>
      <div className="report-next-step">
        <h3>下一步检查</h3>
        <p>先核对交易与成本，再用独立区间继续验证。</p>
        <div className="button-row">
          <Button onClick={() => navigate(`/report/${report.id}/trades`)}>
            检查交易
          </Button>
          <Button onClick={() => navigate(`/holdout/new?report=${report.id}`)}>
            配置留出验证
          </Button>
        </div>
      </div>
    </section>
  );
}

export function AgentPanel({
  objectId,
  context,
  references = [],
  onClose,
}: {
  objectId: string;
  context: string;
  references?: { label: string; route: string }[];
  onClose?: () => void;
}) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    buffer = useBuffer(`agent-${objectId}`, "");
  const inputId = useId(),
    report = state.reports.find((r) => r.id === objectId),
    hasReport = !!report,
    messages = state.messages.filter((m) => m.objectId === objectId),
    end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!hasReport || messages.length > 0)
      end.current?.scrollIntoView({ block: "nearest" });
  }, [messages.length, hasReport]);
  const send = async () => {
    const result = await action.execute({
      type: "agent.ask",
      objectId,
      text: buffer.value,
      references,
    });
    if (result) {
      buffer.update("");
      buffer.clear();
    }
  };
  return (
    <AgentSurface context={context} report={!!report} onClose={onClose} history={<>
        {report ? (
          <ReportEvidence report={report} />
        ) : (
          <>
            <div className="agent-note">
              当前上下文已绑定。先检查产物与证据，再决定下一步。
              <small>演示任务 · 未调用模型</small>
            </div>
            <details className="agent-context">
              <summary>
                <Icon name="file" />
                来源与上下文 <span>{references.length} 项</span>
              </summary>
              <p>{context}</p>
              {references.map((r) => (
                <LinkButton key={r.route} onClick={() => navigate(r.route)}>
                  {r.label}
                </LinkButton>
              ))}
            </details>
          </>
        )}
        {messages.length === 0 && (
          <div className="suggestions">
            <small>{report ? "继续与研究助手讨论" : "你可以这样问"}</small>
            <Button
              icon="chart"
              onClick={() => buffer.update("解释当前结果与限制")}
            >
              解释当前结果与限制
            </Button>
            <Button
              icon="file"
              onClick={() => buffer.update("下一步应该检查哪些证据？")}
            >
              检查下一步
            </Button>
          </div>
        )}
        {messages.map((message) => (
          <div key={message.id} className={`message ${message.role}`}>
            <small>{message.role === "user" ? "你" : "场景助手"}</small>
            <p>{message.text}</p>
            {message.references.map((r) => (
              <LinkButton key={r.route} onClick={() => navigate(r.route)}>
                {r.label}
              </LinkButton>
            ))}
          </div>
        ))}
        {messages.length > 0 && (
          <details>
            <summary>查看执行过程</summary>
            <ol>
              <li>读取当前对象与版本（演示）</li>
              <li>引用已保存的场景证据</li>
              <li>返回预设解读，没有运行实验</li>
            </ol>
          </details>
        )}
        <div ref={end} />
      </>} input={<>
        <ActionError error={action.error || buffer.bufferError} />
        <label className="sr-only" htmlFor={inputId}>
          询问当前对象
        </label>
        <textarea
          id={inputId}
          placeholder={report ? "询问这份报告…" : "继续讨论这个研究…"}
          value={buffer.value}
          onChange={(e) => buffer.update(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              e.keyCode !== 229
            ) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <div>
          <span>
            <Icon name="cube" />
            模型 ·{" "}
            {state.connections.find((c) => c.isDefault)?.name ?? "未选择"}
          </span>
          <IconButton
            name="send"
            label="发送演示问题"
            disabled={action.busy || !buffer.value.trim()}
            onClick={() => void send()}
          />
        </div>
        <small>演示回复，不构成研究结论。</small>
      </>} />
  );
}
export function DetailLayout({
  children,
  objectId,
  context,
  references,
  className = "",
}: {
  children: ReactNode;
  objectId: string;
  context: string;
  references?: { label: string; route: string }[];
  className?: string;
}) {
  const { openAgent } = useStudio();
  return <DetailSurface className={className} openAgent={openAgent}
    assistant={<AgentPanel objectId={objectId} context={context} references={references} />}>
    {children}
  </DetailSurface>;
}
