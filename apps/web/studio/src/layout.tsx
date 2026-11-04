/** Existing v3 presentation, separated from the offline scenario store. */
import { useEffect, useId, useRef, useState, type ReactNode, type KeyboardEvent } from 'react';
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
    <div className="agent-heading"><Logo small /><div><strong>研究助手</strong><small>{context}</small></div>
      {onClose && <button className="icon-button" aria-label="收起 Agent" onClick={onClose}><Icon name="close" /></button>}
    </div>
    <div className="agent-history">{history}</div>
    <div className="agent-input">{input}</div>
  </aside>;
}
function modalKey(event: KeyboardEvent<HTMLDialogElement>, close: () => void) {
  if (event.defaultPrevented || event.nativeEvent.isComposing) return;
  const dialog = event.currentTarget;
  if ((event.target as Element).closest('dialog, [role="dialog"]') !== dialog) return;
  if (event.key === 'Escape') { event.preventDefault(); close(); return; }
  if (event.key !== 'Tab') return;
  const controls = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])')]
    .filter(element => element.getClientRects().length > 0 && !element.closest('[inert]'));
  const first = controls[0], last = controls.at(-1);
  if (!first) { event.preventDefault(); dialog.focus(); }
  else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

/** A modal owns browser focus/inert behavior; callers keep business state outside it. */
export function DialogSurface({ title, children, onClose, className = '' }: {
  title: string; children: ReactNode; onClose: () => void; className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null), id = useId();
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null, dialog = ref.current;
    return () => {
      if (dialog?.open) { if (dialog.close) dialog.close(); else dialog.open = false; }
      if (prior?.isConnected) prior.focus();
    };
  }, []);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    dialog.querySelector<HTMLElement>('[data-autofocus]')?.focus();
  }, [title]);
  return <dialog ref={ref} className={`dialog ${className}`} aria-labelledby={id}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onKeyDown={event => modalKey(event, onClose)}>
    <div className="dialog-head"><h2 id={id}>{title}</h2><button className="icon-button" aria-label={`关闭${title}`} onClick={onClose}><Icon name="close" /></button></div>
    {children}
  </dialog>;
}

export function DetailSurface({ children, assistant, openAgent, className = '', defaultCollapsed = false }: {
  children: ReactNode; assistant: ReactNode; openAgent?: () => void; className?: string; defaultCollapsed?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [narrow, setNarrow] = useState(() => window.matchMedia?.('(max-width: 1279px)').matches ?? false);
  const ref = useRef<HTMLDialogElement>(null), opener = useRef<HTMLButtonElement>(null), panelId = useId();
  const wasVisible = useRef<boolean | null>(null);
  const openedByUser = useRef(false);
  const visible = narrow ? expanded : !collapsed;
  useEffect(() => {
    if (openAgent || !window.matchMedia) return;
    const media = window.matchMedia('(max-width: 1279px)');
    const resize = (event: MediaQueryListEvent) => { setNarrow(event.matches); setExpanded(false); };
    media.addEventListener('change', resize);
    return () => media.removeEventListener('change', resize);
  }, [openAgent]);
  useEffect(() => {
    const dialog = ref.current, trigger = opener.current;
    if (!dialog) return;
    const active = document.activeElement as HTMLElement | null;
    const focused = active?.closest('dialog') === dialog ? active : null;
    // Keep this one assistant subtree mounted as the panel changes mode. A
    // showModal drawer gives keyboard focus containment without a second stream.
    if (dialog.open) { if (dialog.close) dialog.close(); else dialog.open = false; }
    if (visible) {
      if (narrow) dialog.showModal();
      else dialog.open = true;
      if (focused) focused.focus();
      else if (!narrow && openedByUser.current) (dialog.querySelector<HTMLElement>('textarea') ?? dialog.querySelector<HTMLElement>('button'))?.focus();
    } else if (focused && wasVisible.current) trigger?.focus();
    openedByUser.current = false;
    wasVisible.current = visible;
  }, [narrow, visible]);
  useEffect(() => {
    const dialog = ref.current;
    return () => {
      if (dialog?.open) { if (dialog.close) dialog.close(); else dialog.open = false; }
    };
  }, []);
  const close = () => { if (narrow) setExpanded(false); else setCollapsed(true); };
  const open = () => { if (narrow) setExpanded(true); else { openedByUser.current = true; setCollapsed(false); } };
  return <div className={`detail-layout ${className}${expanded ? ' local-agent-open' : ''}${!openAgent && !narrow && collapsed ? ' agent-collapsed' : ''}`}>
    <div className="detail-content">
      <div className="agent-open"><button ref={opener} className="button" aria-label="打开研究助手" aria-controls={openAgent ? undefined : panelId} aria-expanded={openAgent ? undefined : visible}
        onClick={openAgent ?? open}><Icon name="panel" /><span>{openAgent ? '打开研究助手' : '研究助手'}</span></button></div>
      {children}
    </div>
    {openAgent ? <div className="desktop-agent">{assistant}</div> :
      <dialog ref={ref} id={panelId} className="desktop-agent persistent-agent" role={narrow ? 'dialog' : 'complementary'}
        aria-label="研究助手面板" aria-modal={narrow && expanded ? true : undefined}
        onCancel={event => { event.preventDefault(); close(); }}
        onKeyDown={event => { if (narrow) modalKey(event, close); }}>
        <button className="icon-button agent-close" aria-label="收起研究助手" title="收起研究助手" onClick={close}><Icon name="panel" /></button>{assistant}
      </dialog>}
  </div>;
}

/** The draft and report sections used by the v3 views, with data owned by callers. */
export function DraftSurface({ source, parameters, costs }: { source: ReactNode; parameters: ReactNode; costs: ReactNode }) {
  return <section className="panel parameter-form"><h2>来源</h2>{source}<h2>参数</h2>{parameters}<h2>成本与约束</h2>{costs}</section>;
}
export function ReportOverviewSurface({ metrics, chart, sources }: { metrics: ReactNode; chart: ReactNode; sources: ReactNode }) {
  return <>{metrics}{chart}<section className="panel"><h2>来源与范围</h2>{sources}</section></>;
}

export function ErrorNote({ children }: { children: ReactNode }) { return children ? <p className="notice error" role="alert">{children}</p> : null }
