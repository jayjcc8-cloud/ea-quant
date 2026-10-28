import { useState } from "react";
import { adapter } from "./scenario";
import {
  download,
  useAction,
  useBuffer,
  useStudio,
  useUnsaved,
} from "./context";
import type { DemoState, Scenario } from "./model";
import {
  ActionError,
  Button,
  Dialog,
  Empty,
  Field,
  Icon,
  KeyValues,
  LinkButton,
  Logo,
  Notice,
  PageHeader,
  Panel,
  Snapshot,
  Status,
  Tabs,
} from "./ui";

function attention(state: DemoState) {
  return [
    ...state.proposals.map((p) => ({
      id: p.id,
      title: `${state.researches.find((r) => r.id === p.researchId)?.name} · 草稿 r${p.base.revision}`,
      description: p.explanation,
      status:
        p.state === "pending"
          ? "提案等待审阅"
          : p.state === "applied"
            ? "提案已应用"
            : "提案已处理",
      pending: p.state === "pending",
      route: `/proposal/${p.id}`,
      action: "审阅提案",
    })),
    ...state.candidates.map((c) => ({
      id: c.id,
      title: `候选 ${c.id}`,
      description: `证据 ${c.reportId} / ${c.holdoutId}，请复核限制。`,
      status: c.state === "pending" ? "等待人工决定" : "已记录人工决定",
      pending: c.state === "pending",
      route: `/candidate/${c.id}`,
      action: "查看证据",
    })),
    ...state.requests
      .filter((r) => r.state === "unknown")
      .map((r) => ({
        id: r.id,
        title: `请求 ${r.id} · 结果未知`,
        description: "保留原请求身份，查询不会重复执行。",
        status: "需要查询",
        pending: true,
        route:
          r.operation === "stop"
            ? `/simulation/${r.objectId}/overview`
            : `/research/${state.experiments.find((e) => e.id === r.objectId)?.researchId}/experiments`,
        action: "查询结果",
      })),
  ];
}
export function Attention() {
  const { state, navigate } = useStudio(),
    action = useAction(),
    [tab, setTab] = useState("pending"),
    items = attention(state),
    shown = items.filter((i) =>
      tab === "processed"
        ? !i.pending
        : tab === "later"
          ? i.pending && state.snoozed.includes(i.id)
          : i.pending && !state.snoozed.includes(i.id),
    );
  return (
    <div className="page">
      <PageHeader title="需要处理" subtitle="只展示需要你决定的事项。" />
      <div className="with-inspector">
        <section>
          <Tabs
            active={tab}
            onChange={setTab}
            items={[
              {
                id: "pending",
                label: `待处理 ${items.filter((i) => i.pending && !state.snoozed.includes(i.id)).length}`,
              },
              {
                id: "later",
                label: `稍后处理 ${items.filter((i) => i.pending && state.snoozed.includes(i.id)).length}`,
              },
              { id: "processed", label: "已处理" },
            ]}
          />
          {shown.map((i) => (
            <article key={i.id} className="attention-item">
              <Icon name={i.pending ? "clock" : "check"} size={25} />
              <div>
                <h2>{i.title}</h2>
                <Status tone={i.pending ? "warn" : "good"}>{i.status}</Status>
                <p>{i.description}</p>
                <small>来源：{i.id} · 确定性场景数据</small>
              </div>
              <div className="button-row">
                <Button
                  kind={i.pending ? "primary" : "secondary"}
                  onClick={() => navigate(i.route)}
                >
                  {i.action}
                </Button>
                {i.pending && (
                  <Button
                    disabled={action.busy}
                    onClick={() =>
                      void action.execute({
                        type: "attention.snooze",
                        id: i.id,
                        snoozed: tab !== "later",
                      })
                    }
                  >
                    {tab === "later" ? "恢复待处理" : "稍后处理"}
                  </Button>
                )}
              </div>
            </article>
          ))}
          {!shown.length && (
            <Empty
              title="这里已处理妥当"
              description="新的提案或候选决定会出现在这里。"
            />
          )}
          <ActionError error={action.error} />
        </section>
        <Panel title="处理规则">
          <h3>保存不是运行</h3>
          <p className="muted">保存记录修改，实验和模拟仍有独立确认。</p>
          <hr />
          <h3>应用不是接受</h3>
          <p className="muted">应用修改草稿，候选决定保留独立语义。</p>
          <hr />
          <h3>确认后才执行</h3>
          <p className="muted">结果未知时查询原请求，保留请求身份。</p>
        </Panel>
      </div>
    </div>
  );
}

export function CommandSearch({ onClose }: { onClose: () => void }) {
  const { state, navigate } = useStudio(),
    [query, setQuery] = useState(""),
    [type, setType] = useState("all"),
    [selected, setSelected] = useState(0);
  const choices = [
    ...state.researches.map((r) => ({
      id: r.id,
      name: r.name,
      kind: "research",
      detail: `研究 · 草稿 r${r.draft.revision}`,
      route: `/research/${r.id}/draft`,
      icon: "file",
    })),
    ...state.reports.map((r) => ({
      id: r.id,
      name: `${r.id} ${state.researches.find((x) => x.id === r.researchId)?.name ?? ""}报告`,
      kind: "report",
      detail: "报告 · 只读冻结证据",
      route: `/report/${r.id}/overview`,
      icon: "file",
    })),
    ...state.datasets.map((d) => ({
      id: d.id,
      name: d.name,
      kind: "data",
      detail: `数据 · ${d.frequency}`,
      route: `/resources/data?data=${encodeURIComponent(d.id)}`,
      icon: "data",
    })),
    {
      id: "new",
      name: "新建研究",
      kind: "command",
      detail: "保存目标，稍后绑定上下文",
      route: "/research/new",
      icon: "plus",
    },
    {
      id: "model",
      name: "打开模型连接",
      kind: "command",
      detail: "配置与测试演示模型",
      route: "/resources/models",
      icon: "cube",
    },
    {
      id: "candidate",
      name: "打开候选中心",
      kind: "command",
      detail: "审阅证据和人工决定",
      route: "/candidates",
      icon: "file",
    },
    ...(state.researches[0]
      ? [
          {
            id: "run",
            name: "运行实验",
            kind: "command",
            detail: "先进入冻结输入确认页",
            route: `/research/${state.researches[0].id}/confirm`,
            icon: "chart",
          },
        ]
      : []),
  ].filter(
    (c) =>
      (type === "all" || c.kind === type) &&
      `${c.name} ${c.id} ${c.detail}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const open = (index: number) => {
    const choice = choices[index];
    if (choice) {
      onClose();
      navigate(choice.route);
    }
  };
  return (
    <Dialog title="搜索与命令" onClose={onClose} className="command-dialog">
      <label className="search-field">
        <Icon name="search" />
        <input
          autoFocus
          aria-label="全局搜索"
          role="combobox"
          aria-expanded="true"
          aria-controls="command-results"
          aria-activedescendant={
            choices[selected] ? `command-${selected}` : undefined
          }
          value={query}
          placeholder="搜索研究、报告、数据与操作…"
          onChange={(e) => {
            setQuery(e.target.value);
            setSelected(0);
          }}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSelected((s) => Math.min(choices.length - 1, s + 1));
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setSelected((s) => Math.max(0, s - 1));
            }
            if (e.key === "Enter") {
              e.preventDefault();
              open(selected);
            }
          }}
        />
      </label>
      <Tabs
        active={type}
        onChange={(t) => {
          setType(t);
          setSelected(0);
        }}
        items={[
          ["all", "全部"],
          ["research", "研究"],
          ["report", "报告"],
          ["data", "数据"],
          ["command", "操作"],
        ].map(([id, label]) => ({ id, label }))}
      />
      <div
        className="command-results"
        id="command-results"
        role="listbox"
        aria-label="搜索结果"
      >
        {choices.map((c, i) => (
          <button
            className={`command-result ${i === selected ? "selected" : ""}`}
            id={`command-${i}`}
            key={`${c.kind}-${c.id}`}
            role="option"
            aria-selected={i === selected}
            onMouseEnter={() => setSelected(i)}
            onClick={() => open(i)}
          >
            <Icon name={c.icon} />
            <span>
              <strong>{c.name}</strong>
              <small>{c.detail}</small>
            </span>
            <Icon name="arrow" size={16} />
          </button>
        ))}
        {!choices.length && (
          <Empty
            title="没有匹配结果"
            description="试试研究名称、报告编号或 AAPL。"
          />
        )}
      </div>
      <div className="command-help">
        <span>↑ ↓ 选择</span>
        <span>Enter 打开</span>
        <span>Esc 关闭</span>
        <span>执行操作仍需确认</span>
      </div>
    </Dialog>
  );
}

export function Settings({ tab = "appearance" }: { tab?: string }) {
  const { state, navigate } = useStudio(),
    action = useAction(),
    preferences = state.preferences;
  const update = (patch: Partial<typeof preferences>) =>
    void action.execute({
      type: "preferences.save",
      preferences: { ...preferences, ...patch },
    });
  return (
    <div className="page">
      <PageHeader title="设置" />
      <Tabs
        active={tab}
        onChange={(t) => navigate(`/settings/${t}`)}
        items={[
          { id: "appearance", label: "外观" },
          { id: "workspace", label: "工作区" },
          { id: "diagnostics", label: "诊断" },
          { id: "about", label: "关于" },
        ]}
      />
      {tab === "appearance" ? (
        <div className="two-column">
          <section>
            <h1>外观</h1>
            <p className="muted">自定义界面外观和显示偏好。</p>
            <h3 className="spacing-top">主题</h3>
            <div className="theme-options">
              {(["light", "dark", "system"] as const).map((theme, i) => (
                <button
                  key={theme}
                  className={`theme-option ${preferences.theme === theme ? "selected" : ""}`}
                  disabled={action.busy}
                  aria-pressed={preferences.theme === theme}
                  onClick={() => update({ theme })}
                >
                  <div
                    className={`theme-thumbnail ${theme}`}
                    aria-hidden="true"
                  />
                  {["浅色", "深色", "跟随系统"][i]}
                </button>
              ))}
            </div>
            <hr />
            <Field label="界面密度">
              <select
                value={preferences.density}
                disabled={action.busy}
                onChange={(e) =>
                  update({
                    density: e.target.value as typeof preferences.density,
                  })
                }
              >
                <option value="compact">紧凑</option>
                <option value="standard">标准</option>
                <option value="comfortable">宽松</option>
              </select>
            </Field>
            <Field label={`文字大小 · ${preferences.fontSize}px`}>
              <input
                type="range"
                min="14"
                max="20"
                step="1"
                value={preferences.fontSize}
                disabled={action.busy}
                onChange={(e) => update({ fontSize: Number(e.target.value) })}
              />
            </Field>
            <Field label="界面缩放">
              <select
                value={preferences.scale}
                disabled={action.busy}
                onChange={(e) => update({ scale: Number(e.target.value) })}
              >
                <option value="100">100%</option>
                <option value="110">110%</option>
                <option value="125">125%</option>
              </select>
            </Field>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={preferences.reducedMotion}
                disabled={action.busy}
                onChange={(e) => update({ reducedMotion: e.target.checked })}
              />
              减少动态效果
            </label>
            <p className="muted">更改立即应用，无需重启。</p>
            <Button
              disabled={action.busy}
              onClick={() =>
                update({
                  theme: "light",
                  fontSize: 15,
                  density: "standard",
                  scale: 100,
                  reducedMotion: false,
                })
              }
            >
              恢复默认外观
            </Button>
            <ActionError error={action.error} />
          </section>
          <Panel title="界面预览">
            <p className="muted">当前设置应用于所有页面。这是实际组件。</p>
            <div className="preview-component">
              <Logo />
              <h2>双均线趋势研究</h2>
              <p className="muted">查看输入，再检查证据。</p>
              {state.researches[0] && (
                <Snapshot input={state.researches[0].draft} />
              )}
              <hr />
              <Button
                kind="primary"
                onClick={() =>
                  navigate(
                    state.researches[0]
                      ? `/research/${state.researches[0].id}/draft`
                      : "/research/new",
                  )
                }
              >
                查看研究
              </Button>
              <label className="field spacing-top">
                <span>输入预览</span>
                <textarea placeholder="继续研究…" />
              </label>
            </div>
          </Panel>
        </div>
      ) : tab === "workspace" ? (
        <Workspace />
      ) : tab === "diagnostics" ? (
        <Diagnostics />
      ) : (
        <Panel title="关于 QUINE">
          <Logo />
          <p className="spacing-top">
            以研究目标、版本化草稿和冻结证据为中心的量化研究空间。
          </p>
          <KeyValues
            items={[
              ["当前前端", "v3 · 完整交互演示"],
              ["数据环境", "隔离浏览器场景"],
              ["真实后端", "尚未接入"],
              ["实盘能力", "功能预览 · 未接入"],
            ]}
          />
          <Notice>
            本阶段展示前端产品流程，不代表正式研究结果或原生桌面发布。
          </Notice>
          <Button onClick={() => navigate("/records")}>查看研究记录</Button>
        </Panel>
      )}
    </div>
  );
}

function Workspace() {
  const { state, toast } = useStudio(),
    action = useAction(),
    buffer = useBuffer("workspace", state.preferences.nextWorkspace),
    [saved, setSaved] = useState(state.preferences.nextWorkspace);
  const save = async () => {
    if (!buffer.value.trim()) {
      action.setError("请填写下次启动的位置说明。");
      return false;
    }
    const result = await action.execute({
      type: "preferences.save",
      preferences: { ...state.preferences, nextWorkspace: buffer.value.trim() },
    });
    if (result) {
      setSaved(buffer.value);
      buffer.clear();
      toast("下次启动位置已保存为配置预览。");
    }
    return !!result;
  };
  useUnsaved(buffer.value !== saved, save, () => {
    buffer.update(saved);
    buffer.clear();
  });
  return (
    <>
      <h2>工作区</h2>
      <p className="muted">管理研究数据的存储位置。</p>
      <Panel>
        <div className="form-row">
          <strong>当前工作区</strong>
          <Field label="当前位置（示例）">
            <input readOnly value={state.preferences.workspace} />
          </Field>
        </div>
        <div className="form-row">
          <strong>下次启动工作区</strong>
          <Field
            label="下次启动位置（示例）"
            hint="接入桌面后，重启应用才会使用新位置。"
          >
            <input
              value={buffer.value}
              onChange={(e) => buffer.update(e.target.value)}
            />
          </Field>
        </div>
        {saved !== state.preferences.workspace && (
          <Status tone="warn">重启后生效</Status>
        )}
        <Notice>
          当前仅保存位置配置预览，不操作真实文件目录。浏览器刷新不会迁移或切换实际工作区。
        </Notice>
        <ActionError error={action.error || buffer.bufferError} />
        <div className="button-row">
          <Button onClick={() => buffer.update(saved)}>取消更改</Button>
          <Button
            kind="primary"
            disabled={action.busy}
            onClick={() => void save()}
          >
            保存下次位置
          </Button>
        </div>
      </Panel>
      <Diagnostics />
    </>
  );
}

function Diagnostics() {
  const { state, navigate, toast } = useStudio(),
    [included, setIncluded] = useState([
      "版本信息",
      "配置摘要",
      "任务状态",
      "错误记录",
    ]),
    [checking, setChecking] = useState(false),
    [lastChecked, setLastChecked] = useState("尚未检查");
  const payload = Object.fromEntries(
    included.map((key) => [
      key,
      key === "版本信息"
        ? { frontend: "v3", schema: state.schema, environment: "demo" }
        : key === "配置摘要"
          ? {
              theme: state.preferences.theme,
              density: state.preferences.density,
              modelCount: state.connections.length,
              credentials: "not collected",
            }
          : key === "任务状态"
            ? state.requests.map((r) => ({
                id: r.id,
                state: r.state,
                operation: r.operation,
              }))
            : state.experiments
                .filter((e) => e.error)
                .map((e) => ({ id: e.id, error: e.error })),
    ]),
  );
  return (
    <section className="spacing-top">
      <div className="section-heading">
        <div>
          <h2>诊断</h2>
          <small>导出前先检查内容，不包含密钥或个人路径。</small>
        </div>
        <Button
          icon="refresh"
          disabled={checking}
          onClick={async () => {
            setChecking(true);
            await new Promise((resolve) => setTimeout(resolve, 350));
            setLastChecked("刚刚 · 演示适配器可用");
            setChecking(false);
            toast("诊断检查完成：演示适配器可用。");
          }}
        >
          {checking ? "检查中…" : "重新检查"}
        </Button>
      </div>
      <p className="muted">最近检查：{lastChecked}</p>
      <div className="two-column">
        <Panel title="诊断信息包含">
          {["版本信息", "配置摘要", "任务状态", "错误记录"].map((key) => (
            <label className="inline-check" key={key}>
              <input
                type="checkbox"
                checked={included.includes(key)}
                onChange={() =>
                  setIncluded((s) =>
                    s.includes(key) ? s.filter((k) => k !== key) : [...s, key],
                  )
                }
              />
              {key}
            </label>
          ))}
        </Panel>
        <Panel title="导出内容预览">
          <pre className="diagnostic-preview">
            {JSON.stringify(payload, null, 2)}
          </pre>
          <Button
            kind="primary"
            icon="download"
            onClick={() => download("quine-demo-diagnostics.json", payload)}
          >
            导出诊断
          </Button>
        </Panel>
      </div>
      <div className="button-row">
        <LinkButton onClick={() => navigate("/records")}>
          查看历史任务
        </LinkButton>
        <LinkButton onClick={() => navigate("/simulations")}>
          查看模拟事件
        </LinkButton>
        <LinkButton onClick={() => navigate("/settings/about")}>
          关于 QUINE
        </LinkButton>
      </div>
    </section>
  );
}

export function AcceptanceTools() {
  const { state, navigate } = useStudio(),
    [scenario, setScenario] = useState<Scenario>("baseline"),
    [confirm, setConfirm] = useState(false),
    [error, setError] = useState("");
  const scenarios: [Scenario, string, string][] = [
    ["baseline", "完整基线", "/"],
    ["empty", "首次空工作区", "/research/new"],
    ["read-error", "读取失败一次", "/resources/data"],
    ["conflict", "提案版本冲突", "/proposal/P-008"],
    ["unknown", "实验结果未知", "/research/R-001/experiments"],
    ["stale", "模拟观测陈旧", "/simulation/SIM-003/events"],
    ["stop-unknown", "停止结果未知", "/simulation/SIM-003/overview"],
    ["model-error", "模型测试失败一次", "/resources/models"],
  ];
  return (
    <div className="page">
      <PageHeader
        title="前端开发与验收工具"
        subtitle="独立验收入口。所有场景仅作用于隔离的浏览器演示存储。"
      />
      <div className="development">
        <Field label="确定性场景">
          <select
            value={scenario}
            onChange={(e) => setScenario(e.target.value as Scenario)}
          >
            {scenarios.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Button kind="primary" onClick={() => setConfirm(true)}>
          复位并载入场景
        </Button>
        <Notice>
          此操作只重置 quine.frontend-v3
          演示状态与编辑缓冲，不访问正式工作区、密钥或数据库。
        </Notice>
        <KeyValues
          items={[
            ["当前场景", state.scenario],
            ["研究数", state.researches.length],
            ["实验数", state.experiments.length],
            ["请求数", state.requests.length],
            [
              "模拟运行中",
              state.simulations.filter((s) => s.status === "running").length,
            ],
          ]}
        />
        <ActionError error={error} />
        <Button
          icon="download"
          onClick={() => download("quine-demo-state.json", state)}
        >
          导出演示状态
        </Button>
      </div>
      {confirm && (
        <Dialog title="复位演示状态？" onClose={() => setConfirm(false)}>
          <p>当前浏览器中的演示修改和编辑缓冲将被所选场景替换。</p>
          <div className="dialog-actions">
            <Button onClick={() => setConfirm(false)}>取消</Button>
            <Button
              kind="primary"
              onClick={() => {
                try {
                  adapter.reset(scenario);
                  Object.keys(sessionStorage)
                    .filter((k) => k.startsWith("quine.frontend-v3.buffer."))
                    .forEach((k) => sessionStorage.removeItem(k));
                  setConfirm(false);
                  navigate(scenarios.find((s) => s[0] === scenario)![2], true);
                  if (scenario === "read-error") location.reload();
                } catch (err) {
                  setError((err as Error).message);
                  setConfirm(false);
                }
              }}
            >
              确认复位
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
