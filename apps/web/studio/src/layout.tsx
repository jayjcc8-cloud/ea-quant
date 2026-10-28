/** Existing v3 presentation, separated from the offline scenario store. */
import { useState, type ReactNode } from 'react';
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

export function AgentSurface({ context, report = false, onClose, history, input }: {
  context: string; report?: boolean; onClose?: () => void; history: ReactNode; input: ReactNode;
}) {
  return <aside className={`agent-panel${report ? ' report-agent' : ''}`} aria-label="研究助手">
    <div className="agent-heading"><Logo small /><div><strong>QUINE 研究助手</strong><small>{context}</small></div>
      {onClose && <button className="icon-button" aria-label="收起 Agent" onClick={onClose}><Icon name="close" /></button>}
    </div>
    <div className="agent-history">{history}</div>
    <div className="agent-input">{input}</div>
  </aside>;
}
export function DetailSurface({ children, assistant, openAgent, className = '' }: {
  children: ReactNode; assistant: ReactNode; openAgent?: () => void; className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  return <div className={`detail-layout ${className}${expanded ? ' local-agent-open' : ''}`}>
    <div className="detail-content">
      <div className="agent-open"><button className="button" onClick={openAgent ?? (() => setExpanded(true))}><Icon name="panel" />打开 Agent</button></div>
      {children}
    </div>
    <div className="desktop-agent">{!openAgent && expanded && <button className="button agent-close" onClick={() => setExpanded(false)}>收起 Agent</button>}{assistant}</div>
  </div>;
}
