import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { CSSProperties } from "react";
import { StudioContext, type LeaveGuard, useStudio } from "./context";
import { adapter } from "./scenario";
import {
  AgentPanel,
  Button,
  Dialog,
  Empty,
  Icon,
  IconButton,
  Logo,
  Notice,
  Status,
} from "./ui";
import {
  Home,
  ResearchView,
  ProposalView,
  ExperimentConfirm,
} from "./Research";
import { Records, ReportView, Compare } from "./Reports";
import {
  BatchConfig,
  BatchResults,
  HoldoutConfig,
  HoldoutDetail,
  Candidates,
  CandidateDetail,
} from "./Validation";
import {
  SimulationList,
  SimulationConfig,
  SimulationDetail,
} from "./Simulation";
import { DataCatalog, CSVImport, Templates, Models } from "./Resources";
import { Attention, CommandSearch, Settings, AcceptanceTools } from "./System";
import { LiveWorkspace, LiveConfig, Exchanges } from "./Live";

function RouteView({ route }: { route: string }) {
  const [area, id, tab] = route.split("?")[0].split("/").filter(Boolean);
  if (!area) return <Home />;
  if (area === "research")
    return id === "new" ? (
      <Home fresh />
    ) : tab === "confirm" ? (
      <ExperimentConfirm id={id} />
    ) : (
      <ResearchView id={id} tab={tab} />
    );
  if (area === "proposal") return <ProposalView id={id} />;
  if (area === "records") return <Records />;
  if (area === "report") return <ReportView id={id} tab={tab} />;
  if (area === "compare") return <Compare left={id} right={tab} />;
  if (area === "batch")
    return id === "new" ? <BatchConfig /> : <BatchResults id={id} />;
  if (area === "holdout")
    return id === "new" ? <HoldoutConfig /> : <HoldoutDetail id={id} />;
  if (area === "candidates") return <Candidates />;
  if (area === "candidate") return <CandidateDetail id={id} />;
  if (area === "simulations") return <SimulationList />;
  if (area === "simulation")
    return id === "new" || tab === "config" ? (
      <SimulationConfig id={id} />
    ) : (
      <SimulationDetail id={id} tab={tab} />
    );
  if (area === "resources")
    return id === "import" || tab === "import" ? (
      <CSVImport />
    ) : id === "templates" ? (
      <Templates />
    ) : id === "models" ? (
      <Models id={tab} />
    ) : (
      <DataCatalog />
    );
  if (area === "live")
    return id === "config" || id === "new" ? (
      <LiveConfig />
    ) : (
      <LiveWorkspace detail={!!id} />
    );
  if (area === "exchanges") return <Exchanges />;
  if (area === "attention") return <Attention />;
  if (area === "settings") return <Settings tab={id} />;
  if (area === "dev") return <AcceptanceTools />;
  return (
    <div className="page">
      <Empty
        title="页面不存在"
        action={
          <Button
            onClick={() => {
              location.hash = "/";
            }}
          >
            返回研究
          </Button>
        }
      />
    </div>
  );
}

function Sidebar({ search }: { search: () => void }) {
  const { state, route, navigate } = useStudio();
  const count = [
    ...state.proposals.filter((p) => p.state === "pending").map((p) => p.id),
    ...state.candidates.filter((c) => c.state === "pending").map((c) => c.id),
    ...state.requests.filter((r) => r.state === "unknown").map((r) => r.id),
  ].filter((id) => !state.snoozed.includes(id)).length;
  const items = [
    ["inbox", "需要处理", "/attention"],
    ["file", "候选中心", "/candidates"],
    ["data", "数据与模型", "/resources/data"],
    ["plug", "交易所配置", "/exchanges"],
  ];
  return (
    <aside className="sidebar" aria-label="工作区导航">
      <Logo />
      <div className="workspace-select">
        <Button icon="folder" onClick={() => navigate("/settings/workspace")}>
          个人工作区 <Icon name="chevron" size={14} />
        </Button>
      </div>
      <Button
        kind="primary"
        icon="plus"
        className="button primary new-button"
        onClick={() => navigate("/research/new")}
      >
        新建研究
      </Button>
      <button
        className="sidebar-search"
        onClick={search}
        aria-label="搜索与命令"
      >
        <Icon name="search" />
        <span>搜索与命令</span>
        <kbd>⌘ K</kbd>
      </button>
      {items.map(([icon, label, path]) => (
        <button
          key={path}
          title={label}
          aria-label={label}
          className={`sidebar-link ${route.startsWith(path.split("/").slice(0, 2).join("/")) ? "selected" : ""}`}
          onClick={() => navigate(path)}
        >
          <Icon name={icon} />
          <span className="label">{label}</span>
          {path === "/attention" && count > 0 && (
            <Status tone="blue">{count}</Status>
          )}
        </button>
      ))}
      <div className="recent-heading">最近研究</div>
      <div className="recent-list">
        {state.researches.slice(0, 5).map((r) => (
          <button
            key={r.id}
            title={r.name}
            className={`recent-link ${route.includes(r.id) ? "selected" : ""}`}
            onClick={() => navigate(`/research/${r.id}/overview`)}
          >
            <i className="recent-dot" />
            <span>
              <strong>{r.name}</strong>
              <small>
                草稿 r{r.draft.revision} ·{" "}
                {r.draft.datasetId ? "上下文已绑定" : "待完善"}
              </small>
            </span>
          </button>
        ))}
      </div>
      <div className="sidebar-footer">
        <button
          className={`sidebar-link ${route.startsWith("/settings") ? "selected" : ""}`}
          onClick={() => navigate("/settings/appearance")}
          aria-label="设置"
        >
          <Icon name="settings" />
          <span className="label">设置</span>
        </button>
        <div className="demo-foot">场景演示 · 数据仅保存在本浏览器</div>
      </div>
    </aside>
  );
}

export default function App() {
  const state = useSyncExternalStore(adapter.subscribe, adapter.getSnapshot);
  const [route, setRoute] = useState(location.hash.slice(1) || "/"),
    routeRef = useRef(route),
    guard = useRef<LeaveGuard | null>(null);
  const [pendingRoute, setPendingRoute] = useState(""),
    [saving, setSaving] = useState(false),
    [search, setSearch] = useState(false),
    [menu, setMenu] = useState(false),
    [agent, setAgent] = useState(false),
    [toastMessage, setToastMessage] = useState("");
  const [loading, setLoading] = useState(true),
    [readError, setReadError] = useState(""),
    [retry, setRetry] = useState(0),
    [systemDark, setSystemDark] = useState(
      matchMedia("(prefers-color-scheme: dark)").matches,
    );
  const setGuard = useCallback((next: LeaveGuard | null) => {
    guard.current = next;
  }, []);
  const commitRoute = useCallback((path: string) => {
    guard.current = null;
    routeRef.current = path;
    setRoute(path);
    setMenu(false);
    setAgent(false);
    location.hash = path;
    document.querySelector(".main")?.scrollTo(0, 0);
  }, []);
  const navigate = useCallback(
    (path: string, afterSave = false) => {
      if (guard.current && path !== routeRef.current && !afterSave) {
        setPendingRoute(path);
        return;
      }
      commitRoute(path);
    },
    [commitRoute],
  );
  useEffect(() => {
    const handle = () => {
      const path = location.hash.slice(1) || "/";
      if (path === routeRef.current) return;
      if (guard.current) {
        history.replaceState(null, "", `#${routeRef.current}`);
        setPendingRoute(path);
      } else commitRoute(path);
    };
    addEventListener("hashchange", handle);
    return () => removeEventListener("hashchange", handle);
  }, [commitRoute]);
  useEffect(() => {
    const abort = new AbortController();
    adapter
      .read(abort.signal)
      .then(() => {
        if (!abort.signal.aborted) setLoading(false);
      })
      .catch((e: Error) => {
        if (!abort.signal.aborted) {
          setReadError(e.message);
          setLoading(false);
        }
      });
    return () => abort.abort();
  }, [retry]);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)"),
      update = () => setSystemDark(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        (e.metaKey || e.ctrlKey) &&
        e.key.toLowerCase() === "k" &&
        !e.isComposing
      ) {
        e.preventDefault();
        setSearch((s) => !s);
      }
    };
    addEventListener("keydown", handler);
    return () => removeEventListener("keydown", handler);
  }, []);
  useEffect(() => {
    if (!state.experiments.some((e) => e.status === "running")) return;
    const timer = setInterval(() => {
      void adapter
        .execute({ type: "tick" })
        .catch((e: Error) => setToastMessage(e.message));
    }, 900);
    return () => clearInterval(timer);
  }, [state.experiments]);
  useEffect(() => {
    if (!toastMessage) return;
    const timer = setTimeout(() => setToastMessage(""), 4500);
    return () => clearTimeout(timer);
  }, [toastMessage]);
  const theme =
    state.preferences.theme === "system"
      ? systemDark
        ? "dark"
        : "light"
      : state.preferences.theme;
  const font = (state.preferences.fontSize * state.preferences.scale) / 100;
  useEffect(() => {
    document.documentElement.style.fontSize = `${font}px`;
    document.documentElement.style.colorScheme = theme;
    document.title = "QUINE · 研究工作区";
  }, [font, theme]);
  const area = /^\/(records|report|compare)/.test(route)
    ? "backtest"
    : /^\/simulation/.test(route)
      ? "simulation"
      : /^\/(live|exchanges)/.test(route)
        ? "live"
        : "research";
  const objectId = route.split("?")[0].split("/")[2] || "workspace",
    report = state.reports.find((r) => r.id === objectId),
    research = state.researches.find(
      (r) => r.id === objectId || r.id === report?.researchId,
    ),
    proposal = state.proposals.find((p) => p.id === objectId),
    holdout = state.holdouts.find((h) => h.id === objectId),
    candidate = state.candidates.find((c) => c.id === objectId),
    simulation = state.simulations.find((s) => s.id === objectId);
  const context = report
    ? `报告 ${report.id} · 冻结草稿 r${report.input.revision}`
    : research
      ? `当前草稿 r${research.draft.revision} · ${research.draft.datasetId}`
      : proposal
        ? `提案 ${proposal.id} · 基于 r${proposal.base.revision}`
        : holdout
          ? `${holdout.id} · 两份冻结报告 · r${holdout.input.revision}`
          : candidate
            ? `${candidate.id} · 人工决定 · ${candidate.reportId}`
            : simulation
              ? `${simulation.id} · 配置 v${simulation.configVersion} · 草稿 r${simulation.input.revision} · 观测 ${simulation.observedAt ?? "尚未启动"}`
              : `当前对象 ${objectId} · 演示环境`;
  const references = report
    ? [{ label: `${report.id} 冻结输入`, route: `/report/${report.id}/inputs` }]
    : research
      ? [
          {
            label: `草稿 r${research.draft.revision}`,
            route: `/research/${research.id}/draft`,
          },
        ]
      : proposal
        ? [
            {
              label: "当前草稿",
              route: `/research/${proposal.researchId}/draft`,
            },
          ]
        : holdout
          ? [holdout.sourceReportId, holdout.reportId]
              .filter(Boolean)
              .map((id) => ({ label: id!, route: `/report/${id}/overview` }))
          : candidate
            ? [
                {
                  label: candidate.reportId,
                  route: `/report/${candidate.reportId}/overview`,
                },
                {
                  label: candidate.holdoutId,
                  route: `/holdout/${candidate.holdoutId}`,
                },
              ]
            : simulation
              ? [
                  {
                    label: "来源候选",
                    route: `/candidate/${simulation.candidateId}`,
                  },
                  {
                    label: "观测与事件",
                    route: `/simulation/${simulation.id}/events`,
                  },
                ]
              : [{ label: "当前对象", route }];

  return (
    <StudioContext.Provider
      value={{
        state,
        route,
        navigate,
        setGuard,
        openAgent: () => setAgent(true),
        toast: setToastMessage,
      }}
    >
      <div
        className="studio"
        data-route={route}
        data-theme={theme}
        data-density={state.preferences.density}
        data-motion={state.preferences.reducedMotion ? "reduced" : "normal"}
        style={{ "--font-size": `${font}px` } as CSSProperties}
      >
        <a
          href="#main"
          className="skip-link"
          onClick={(e) => {
            e.preventDefault();
            document.getElementById("main")?.focus();
          }}
        >
          跳到主要内容
        </a>
        <Sidebar search={() => setSearch(true)} />
        <header className="topbar">
          <div className="mobile-menu">
            <IconButton
              name="menu"
              label="打开导航"
              onClick={() => setMenu(true)}
            />
          </div>
          <nav className="primary-nav" aria-label="主要工作区">
            {[
              ["research", "研究", "/"],
              ["backtest", "回测", "/records"],
              ["simulation", "模拟", "/simulations"],
              ["live", "实盘", "/live"],
            ].map(([key, label, path]) => (
              <button
                key={key}
                className={area === key ? "selected" : ""}
                aria-current={area === key ? "page" : undefined}
                onClick={() => navigate(path)}
              >
                {label}
              </button>
            ))}
          </nav>
          <span className="environment">
            <i />
            演示环境
          </span>
        </header>
        <main id="main" className="main" tabIndex={-1}>
          {adapter.storageWarning && (
            <Notice tone="warn">{adapter.storageWarning}</Notice>
          )}
          {loading ? (
            <div className="page" role="status" aria-label="正在读取工作区">
              <h1>正在读取工作区…</h1>
              {[1, 2, 3].map((x) => (
                <div className="skeleton" key={x} />
              ))}
            </div>
          ) : readError ? (
            <div className="page">
              <Empty
                title="读取失败"
                description={readError}
                action={
                  <Button
                    kind="primary"
                    onClick={() => {
                      setReadError("");
                      setLoading(true);
                      setRetry((r) => r + 1);
                    }}
                  >
                    重试读取
                  </Button>
                }
              />
            </div>
          ) : (
            <RouteView key={route} route={route} />
          )}
        </main>
        {toastMessage && (
          <div className="toast" role="status">
            {toastMessage}
            <IconButton
              name="close"
              label="关闭提示"
              onClick={() => setToastMessage("")}
            />
          </div>
        )}
        {search && <CommandSearch onClose={() => setSearch(false)} />}{" "}
        {menu && (
          <Dialog
            title="工作区导航"
            className="navigation-drawer"
            onClose={() => setMenu(false)}
          >
            <Sidebar
              search={() => {
                setMenu(false);
                setSearch(true);
              }}
            />
          </Dialog>
        )}
        {agent && (
          <Dialog
            title="Agent · 当前上下文"
            className="agent-drawer"
            onClose={() => setAgent(false)}
          >
            <AgentPanel
              key={objectId}
              objectId={objectId}
              context={context}
              references={references}
            />
          </Dialog>
        )}
        {pendingRoute && (
          <Dialog
            title="离开前保存修改？"
            onClose={() => {
              if (!saving) setPendingRoute("");
            }}
          >
            <p>当前编辑尚未保存。你可以继续编辑、放弃修改，或保存后离开。</p>
            <div className="dialog-actions">
              <Button disabled={saving} onClick={() => setPendingRoute("")}>
                继续编辑
              </Button>
              <Button
                disabled={saving}
                onClick={() => {
                  guard.current?.discard();
                  commitRoute(pendingRoute);
                  setPendingRoute("");
                }}
              >
                放弃修改并离开
              </Button>
              <Button
                kind="primary"
                disabled={saving}
                onClick={async () => {
                  const next = pendingRoute;
                  setSaving(true);
                  const ok = await guard.current?.save();
                  setSaving(false);
                  if (ok) {
                    commitRoute(next);
                    setPendingRoute("");
                  } else setPendingRoute("");
                }}
              >
                {saving ? "正在保存…" : "保存并离开"}
              </Button>
            </div>
          </Dialog>
        )}
      </div>
    </StudioContext.Provider>
  );
}
