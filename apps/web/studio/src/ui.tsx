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

export function Icon({
  name = "file",
  size = 20,
}: {
  name?: string;
  size?: number;
}) {
  const paths: Record<string, ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <path d="m16 16 5 5" />
      </>
    ),
    arrow: <path d="m9 5 7 7-7 7" />,
    back: <path d="m15 5-7 7 7 7" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    send: <path d="M12 20V4m-7 7 7-7 7 7" />,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    file: (
      <>
        <path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6" />
      </>
    ),
    data: (
      <>
        <ellipse cx="12" cy="5" rx="8" ry="3" />
        <path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" />
      </>
    ),
    cube: <path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm-9 5 9 5 9-5M12 13v9" />,
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="m9 3 6 0 1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z" />
      </>
    ),
    inbox: <path d="M3 13 7 4h10l4 9v7H3v-7Zm0 0h5l2 3h4l2-3h5" />,
    folder: <path d="M3 6h7l2 3h9v12H3z" />,
    plug: <path d="M8 3v5m8-5v5M5 8h14v5a7 7 0 0 1-14 0V8Zm7 12v4" />,
    chart: <path d="M3 3v18h18M6 15l5-6 4 3 6-8" />,
    check: <path d="m5 12 5 5L20 6" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 6v6l4 3" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v6M12 7v1" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7V2m0 5h-5M4 17v5m0-5h5M20 7a9 9 0 0 0-16 3M4 17a9 9 0 0 0 16-3" />
      </>
    ),
    download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
    stop: <rect x="5" y="5" width="14" height="14" rx="2" />,
    light: (
      <>
        <path d="M9 18h6M9 21h6M8 15a7 7 0 1 1 8 0l-1 3H9z" />
      </>
    ),
    chevron: <path d="m6 9 6 6 6-6" />,
    edit: <path d="m4 16 12-12 4 4L8 20H4v-4ZM14 6l4 4" />,
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3" />
      </>
    ),
    panel: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M15 4v16" />
      </>
    ),
    trash: <path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />,
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] ?? paths.file}
    </svg>
  );
}
export function Logo({ small = false }: { small?: boolean }) {
  return (
    <div className={`brand ${small ? "small" : ""}`}>
      <span className="q-mark" aria-hidden="true">
        Q
      </span>
      {!small && (
        <span>
          <strong>QUINE</strong>
          <small>量化研究平台</small>
        </span>
      )}
    </div>
  );
}
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
export function PageHeader({
  title,
  subtitle,
  breadcrumb,
  actions,
  badge,
}: {
  title: string;
  subtitle?: ReactNode;
  breadcrumb?: ReactNode;
  actions?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <>
      <div className="breadcrumb">{breadcrumb}</div>
      <div className="page-heading">
        <div>
          <h1>
            {title} {badge}
          </h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions && <div className="actions">{actions}</div>}
      </div>
    </>
  );
}
export function Tabs({
  items,
  active,
  onChange,
}: {
  items: { id: string; label: ReactNode }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <nav className="tabs" aria-label="页面标签">
      {items.map((item) => (
        <button
          key={item.id}
          aria-current={active === item.id ? "page" : undefined}
          className={active === item.id ? "selected" : ""}
          onClick={() => onChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
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
    messages = state.messages.filter((m) => m.objectId === objectId),
    end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [messages.length]);
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
    <aside className="agent-panel">
      <div className="agent-heading">
        <Logo small />
        <div>
          <strong>QUINE 研究助手</strong>
          <small>{context}</small>
        </div>
        {onClose && (
          <IconButton name="close" label="收起 Agent" onClick={onClose} />
        )}
      </div>
      <div className="agent-history">
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
        {messages.length === 0 && (
          <div className="suggestions">
            <small>你可以这样问</small>
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
      </div>
      <div className="agent-input">
        <ActionError error={action.error || buffer.bufferError} />
        <label className="sr-only" htmlFor={inputId}>
          询问当前对象
        </label>
        <textarea
          id={inputId}
          placeholder="继续讨论这个研究…"
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
      </div>
    </aside>
  );
}
export function DetailLayout({
  children,
  objectId,
  context,
  references,
}: {
  children: ReactNode;
  objectId: string;
  context: string;
  references?: { label: string; route: string }[];
}) {
  const { openAgent } = useStudio();
  return (
    <div className="detail-layout">
      <div className="detail-content">
        <div className="agent-open">
          <Button icon="panel" onClick={openAgent}>
            打开 Agent
          </Button>
        </div>
        {children}
      </div>
      <div className="desktop-agent">
        <AgentPanel
          objectId={objectId}
          context={context}
          references={references}
        />
      </div>
    </div>
  );
}
