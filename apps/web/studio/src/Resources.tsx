import { useState } from "react";
import { adapter } from "./scenario";
import { sampleCSV } from "./fixtures";
import { autoMap, csvFields, parseCSV, validateCSV } from "./csv";
import {
  download,
  useAction,
  useBuffer,
  useStudio,
  useUnsaved,
} from "./context";
import type { Connection } from "./model";
import {
  ActionError,
  Button,
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
  Status,
  Tabs,
} from "./ui";

export function ResourceTabs({ active }: { active: string }) {
  const { navigate } = useStudio();
  return (
    <Tabs
      active={active}
      onChange={(t) => navigate(`/resources/${t}`)}
      items={[
        { id: "data", label: "数据" },
        { id: "templates", label: "策略与模板" },
        { id: "models", label: "模型连接" },
      ]}
    />
  );
}

export function DataCatalog() {
  const { state, navigate } = useStudio(),
    [selected, setSelected] = useState(
      new URLSearchParams(location.hash.split("?")[1]).get("data") ??
        state.datasets[0]?.id ??
        "",
    ),
    [search, setSearch] = useState(""),
    [source, setSource] = useState("all"),
    [sample, setSample] = useState(false);
  const d = state.datasets.find((d) => d.id === selected),
    rows = state.datasets.filter(
      (d) =>
        `${d.name} ${d.symbol}`.toLowerCase().includes(search.toLowerCase()) &&
        (source === "all" || d.source === source),
    );
  return (
    <div className="page">
      <PageHeader
        title="数据与模型"
        subtitle="管理研究所需的数据集、策略模板与模型连接。"
      />
      <ResourceTabs active="data" />
      <div className="with-inspector">
        <div>
          <div className="toolbar">
            <label className="search-field">
              <Icon name="search" />
              <input
                aria-label="搜索数据"
                placeholder="搜索数据名称或标的"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <select
              aria-label="数据来源"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              <option value="all">全部来源</option>
              {[...new Set(state.datasets.map((d) => d.source))].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            <Button
              kind="primary"
              icon="plus"
              onClick={() => navigate("/resources/import")}
            >
              导入数据
            </Button>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>标的</th>
                  <th>周期</th>
                  <th>范围</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((data) => (
                  <tr
                    key={data.id}
                    className={data.id === selected ? "selected" : ""}
                  >
                    <td>
                      <LinkButton onClick={() => setSelected(data.id)}>
                        {data.name}
                      </LinkButton>
                    </td>
                    <td>{data.symbol}</td>
                    <td>{data.frequency}</td>
                    <td>
                      {data.start}
                      <small>— {data.end}</small>
                    </td>
                    <td>
                      <Status tone={data.status === "ready" ? "good" : "warn"}>
                        {data.status === "ready"
                          ? "可用于演示实验"
                          : "数据待检查"}
                      </Status>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rows.length && <Empty title="没有匹配数据" />}
          </div>
          <section className="activity">
            <h2>版本历史</h2>
            <ul className="timeline">
              {d && (
                <li>
                  <span>
                    {d.name} 只读版本
                    <br />
                    <small>用于研究时冻结版本身份，后续导入不覆盖。</small>
                  </span>
                </li>
              )}
            </ul>
          </section>
        </div>
        {d && (
          <Inspector title={d.name}>
            <Status>只读版本</Status>
            <h3 className="spacing-top">数据范围</h3>
            <KeyValues
              items={[
                ["开始时间", d.start],
                ["结束时间", d.end],
                ["记录", `${d.rows} 行`],
                ["字段", d.fields.join(" / ")],
              ]}
            />
            <h3 className="spacing-top">数据质量</h3>
            <KeyValues
              items={[
                ["结构检查", d.status === "ready" ? "通过" : "待检查"],
                ["市场覆盖", "未评估"],
                ["来源", d.source],
                ["演示标识", d.fingerprint],
              ]}
            />
            <div className="button-row">
              <Button onClick={() => setSample(true)}>查看样本</Button>
              <Button
                kind="primary"
                disabled={d.status !== "ready"}
                onClick={() => navigate(`/research/new?data=${d.id}`)}
              >
                用于研究
              </Button>
            </div>
            <Notice>结构可用不代表研究充分。目录内置数据为场景元数据。</Notice>
          </Inspector>
        )}
      </div>
      {sample && d && (
        <Dialog
          title={`${d.name} · 样本预览`}
          wide
          onClose={() => setSample(false)}
        >
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {d.fields.map((f) => (
                    <th key={f}>{f}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.samples.slice(0, 10).map((r, i) => (
                  <tr key={i}>
                    {r.map((value, j) => (
                      <td key={j}>{value}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {!d.samples.length && <Empty title="当前版本无可预览样本" />}
          </div>
          <small>
            只展示前 {Math.min(10, d.samples.length)} 行；内置数据是演示样本。
          </small>
        </Dialog>
      )}
    </div>
  );
}

export function CSVImport() {
  const { navigate, toast } = useStudio(),
    action = useAction(),
    buffer = useBuffer("csv-import", {
      text: sampleCSV,
      mapping: autoMap(parseCSV(sampleCSV).headers),
      name: "aapl-daily-import",
      symbol: "AAPL",
      frequency: "日线",
      fileName: "aapl-daily.csv · 示例文件",
    }),
    [savedText, setSavedText] = useState(JSON.stringify(buffer.value)),
    [reading, setReading] = useState(false);
  const parsed = parseCSV(buffer.value.text),
    validation = validateCSV(parsed, buffer.value.mapping);
  const saveDraft = async () => {
    buffer.update(buffer.value);
    setSavedText(JSON.stringify(buffer.value));
    toast("导入草稿已保留在当前浏览器。");
    return !buffer.bufferError;
  };
  useUnsaved(JSON.stringify(buffer.value) !== savedText, saveDraft, () => {
    buffer.clear();
    setSavedText(JSON.stringify(buffer.value));
  });
  const publish = async () => {
    if (validation.errors.length) {
      action.setError("请先修复全部问题行和字段映射。");
      return;
    }
    const r = await action.execute({
      type: "dataset.publish",
      dataset: {
        name: buffer.value.name,
        symbol: buffer.value.symbol,
        frequency: buffer.value.frequency,
        start: validation.start,
        end: validation.end,
        rows: parsed.rows.length,
        fields: [...csvFields],
        samples: validation.samples,
        source: "本地 CSV",
        status: "ready",
      },
    });
    if (r) {
      buffer.clear();
      toast("已发布新的演示数据版本。");
      navigate("/resources/data", true);
    }
  };
  return (
    <div className="page">
      <PageHeader
        title="导入数据"
        subtitle={buffer.value.fileName}
        breadcrumb={
          <LinkButton onClick={() => navigate("/resources/data")}>
            数据与模型 / 数据 / 导入
          </LinkButton>
        }
      />
      <div className="steps">
        <span className="done">
          <i>1</i>选择文件
        </span>
        <span className="done">
          <i>2</i>字段映射
        </span>
        <span>
          <i>3</i>发布版本
        </span>
      </div>
      <div className="toolbar">
        <Field label="选择本地 CSV（不上传）">
          <input
            type="file"
            accept=".csv,text/csv"
            disabled={reading}
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              action.clearError();
              if (file.size > 2 * 1024 * 1024) {
                action.setError("本阶段支持不超过 2 MB 的本地 CSV。");
                return;
              }
              setReading(true);
              try {
                const text = await file.text(),
                  parsed = parseCSV(text);
                buffer.update({
                  ...buffer.value,
                  text,
                  mapping: autoMap(parsed.headers),
                  fileName: file.name,
                });
              } catch {
                action.setError("无法读取这个文件，请重新选择。");
              } finally {
                setReading(false);
              }
            }}
          />
        </Field>
        <Button
          onClick={() => {
            buffer.update({
              ...buffer.value,
              text: sampleCSV,
              mapping: autoMap(parseCSV(sampleCSV).headers),
              fileName: "aapl-daily.csv · 示例文件",
            });
            action.clearError();
          }}
        >
          载入示例 CSV
        </Button>
      </div>
      <div className="two-column">
        <Panel
          title="字段映射"
          actions={
            <LinkButton
              onClick={() =>
                buffer.update({
                  ...buffer.value,
                  mapping: autoMap(parsed.headers),
                })
              }
            >
              自动匹配
            </LinkButton>
          }
        >
          <p className="muted">将文件中的列映射到标准字段。</p>
          {csvFields.map((field) => (
            <div className="form-row" key={field}>
              <span>{field}</span>
              <Field label={`${field} 来源列`}>
                <select
                  value={buffer.value.mapping[field]}
                  onChange={(e) =>
                    buffer.update({
                      ...buffer.value,
                      mapping: {
                        ...buffer.value.mapping,
                        [field]: e.target.value,
                      },
                    })
                  }
                >
                  <option value="">未映射</option>
                  {parsed.headers.map((h) => (
                    <option key={h}>{h}</option>
                  ))}
                </select>
              </Field>
            </div>
          ))}
          <hr />
          <div className="form-grid">
            <Field label="标的">
              <input
                value={buffer.value.symbol}
                onChange={(e) =>
                  buffer.update({ ...buffer.value, symbol: e.target.value })
                }
              />
            </Field>
            <Field label="周期">
              <select
                value={buffer.value.frequency}
                onChange={(e) =>
                  buffer.update({ ...buffer.value, frequency: e.target.value })
                }
              >
                <option>日线</option>
              </select>
            </Field>
          </div>
          <Field label="数据版本名称">
            <input
              value={buffer.value.name}
              onChange={(e) =>
                buffer.update({ ...buffer.value, name: e.target.value })
              }
            />
          </Field>
          <small>日期格式 YYYY-MM-DD，日期按 UTC 解释。</small>
        </Panel>
        <div>
          <Panel title={`样本预览 · 前 ${Math.min(5, parsed.rows.length)} 行`}>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {csvFields.map((f) => (
                      <th key={f}>{f}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {validation.samples.slice(0, 5).map((row, i) => (
                    <tr
                      id={`csv-row-${i + 2}`}
                      key={i}
                      className={
                        validation.errors.some((e) => e.row === i + 2)
                          ? "row-error"
                          : ""
                      }
                    >
                      {row.map((v, j) => (
                        <td key={j}>{v || "缺失"}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {validation.errors.length ? (
              <Notice tone="error">
                {validation.errors.length} 项问题需要处理。发布已阻止。
              </Notice>
            ) : (
              <Notice tone="success">
                结构校验通过，{parsed.rows.length} 行可发布为演示版本。
              </Notice>
            )}
            <div id="csv-errors" tabIndex={-1}>
              {validation.errors.slice(0, 20).map((e, i) => (
                <p key={i} className="muted">
                  第 {e.row} 行 · {e.field}：{e.message}
                </p>
              ))}
            </div>
            {validation.errors.length > 0 && (
              <Button
                onClick={() => document.getElementById("csv-errors")?.focus()}
              >
                定位问题行
              </Button>
            )}
          </Panel>
          <Panel title="导入规则">
            <ul>
              <li>必须包含六个标准字段，每列只能映射一次。</li>
              <li>日期有效、升序且不重复。</li>
              <li>价格为正、成交量非负，最高最低价需一致。</li>
              <li>有问题的版本不能发布。原目录保持不变。</li>
            </ul>
          </Panel>
        </div>
      </div>
      <details>
        <summary>编辑 CSV 内容以修复问题</summary>
        <Field label="CSV 内容">
          <textarea
            style={{ minHeight: 170 }}
            value={buffer.value.text}
            onChange={(e) =>
              buffer.update({ ...buffer.value, text: e.target.value })
            }
          />
        </Field>
        <small>修改表头后请重新执行自动匹配。</small>
      </details>
      <ActionError error={action.error || buffer.bufferError} />
      <div className="action-footer">
        <small>
          {parsed.rows.length} 行 · {validation.errors.length} 项问题 · 未发布
        </small>
        <div className="button-row">
          <Button onClick={() => navigate("/resources/data")}>取消</Button>
          <Button onClick={() => void saveDraft()}>保存导入草稿</Button>
          <Button
            kind="primary"
            disabled={
              action.busy ||
              reading ||
              validation.errors.length > 0 ||
              !buffer.value.name.trim() ||
              !buffer.value.symbol.trim()
            }
            onClick={() => void publish()}
          >
            发布版本
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Templates() {
  const { state, navigate } = useStudio(),
    action = useAction(),
    [selected, setSelected] = useState(state.templates[0]?.id ?? ""),
    [search, setSearch] = useState(""),
    [kind, setKind] = useState("all"),
    [tab, setTab] = useState("parameters"),
    [registration, setRegistration] = useState(false),
    [name, setName] = useState(""),
    [rule, setRule] = useState(""),
    [ascending, setAscending] = useState(true);
  const t = state.templates.find((t) => t.id === selected),
    rows = state.templates
      .filter(
        (t) =>
          `${t.name} ${t.description}`.includes(search) &&
          (kind === "all" || t.source === kind),
      )
      .sort(
        (a, b) => a.name.localeCompare(b.name, "zh") * (ascending ? 1 : -1),
      );
  return (
    <div className="page">
      <PageHeader title="策略与模板" subtitle="从明确的规则开始研究。" />
      <ResourceTabs active="templates" />
      <div className="toolbar">
        <label className="search-field">
          <Icon name="search" />
          <input
            value={search}
            aria-label="搜索策略模板"
            placeholder="搜索策略名称或说明"
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <select
          aria-label="模板类型"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          <option value="all">全部来源</option>
          {[...new Set(state.templates.map((t) => t.source))].map((source) => (
            <option key={source}>{source}</option>
          ))}
        </select>
        <Button icon="file" onClick={() => setRegistration(true)}>
          登记本地策略
        </Button>
      </div>
      <div className="two-column">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th aria-sort={ascending ? "ascending" : "descending"}>
                  <button onClick={() => setAscending((v) => !v)}>
                    名称 {ascending ? "↓" : "↑"}
                  </button>
                </th>
                <th>类型</th>
                <th>适用范围</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((template) => (
                <tr
                  key={template.id}
                  className={template.id === selected ? "selected" : ""}
                >
                  <td>
                    <LinkButton onClick={() => setSelected(template.id)}>
                      {template.name}
                    </LinkButton>
                  </td>
                  <td>{template.kind}</td>
                  <td>{template.scope}</td>
                  <td>{template.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && <Empty />}
        </div>
        {t && (
          <Panel
            title={
              <>
                {t.name} <Status tone="blue">模板</Status>
              </>
            }
          >
            <p className="muted">{t.description}</p>
            <Tabs
              active={tab}
              onChange={setTab}
              items={[
                { id: "parameters", label: "参数" },
                { id: "rules", label: "规则" },
                { id: "source", label: "来源" },
              ]}
            />
            {tab === "parameters" ? (
              <KeyValues
                items={[
                  ["快均线", t.defaults.fast],
                  ["慢均线", t.defaults.slow],
                  ["价格字段", "close"],
                  ["时间周期", "日线"],
                ]}
              />
            ) : tab === "rules" ? (
              <p>{t.rule}</p>
            ) : (
              <KeyValues
                items={[
                  ["来源", t.source],
                  ["身份", t.id],
                  ["版本", "1.0 · 示例模板"],
                  ["能力", "本阶段记录规则元数据，不加载或执行任意代码"],
                ]}
              />
            )}
            <h3 className="spacing-top">适用范围</h3>
            <p>{t.scope}</p>
            <details>
              <summary>规则说明</summary>
              <p>{t.rule}</p>
            </details>
            <Notice>模板需经过独立实验与审阅，不暗示已经验证或盈利。</Notice>
            <div className="button-row">
              <Button
                kind="primary"
                onClick={() => navigate(`/research/new?template=${t.id}`)}
              >
                从模板创建研究
              </Button>
              <Button
                icon="download"
                onClick={() => download(`${t.id}-source.json`, t)}
              >
                查看源文件
              </Button>
            </div>
          </Panel>
        )}
      </div>
      {registration && (
        <Dialog title="登记本地策略" onClose={() => setRegistration(false)}>
          <Notice>登记名称与规则说明，不加载文件或执行代码。</Notice>
          <Field label="策略名称">
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="规则说明">
            <textarea value={rule} onChange={(e) => setRule(e.target.value)} />
          </Field>
          <ActionError error={action.error} />
          <div className="dialog-actions">
            <Button onClick={() => setRegistration(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={action.busy || !name.trim() || !rule.trim()}
              onClick={() =>
                void action.execute(
                  { type: "template.register", name, rule },
                  (r) => {
                    setSelected(r.id);
                    setRegistration(false);
                    setName("");
                    setRule("");
                  },
                )
              }
            >
              登记模板
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

const emptyConnection: Connection = {
  id: "",
  name: "新连接",
  provider: "OpenAI 兼容",
  endpoint: "https://api.example.com/v1",
  model: "research-model",
  isDefault: false,
  timeout: 30,
  tested: false,
};
function ConnectionEditor({ connection }: { connection: Connection }) {
  const { navigate, toast } = useStudio(),
    action = useAction(),
    buffer = useBuffer(`model-${connection.id || "new"}`, connection),
    [testing, setTesting] = useState(false),
    [testMessage, setTestMessage] = useState(""),
    [testError, setTestError] = useState("");
  const dirty = JSON.stringify(buffer.value) !== JSON.stringify(connection),
    update = (patch: Partial<Connection>) => {
      buffer.update({ ...buffer.value, ...patch, tested: false });
      setTestMessage("");
      setTestError("");
      action.clearError();
    };
  const save = async () => {
    const result = await action.execute({
      type: "connection.save",
      connection: buffer.value,
    });
    if (result) {
      buffer.clear();
      toast("演示连接已保存，没有发送网络请求。");
      navigate(`/resources/models/${result.id}`, true);
    }
    return !!result;
  };
  useUnsaved(dirty, save, () => {
    buffer.update(connection);
    buffer.clear();
  });
  return (
    <Panel
      title={
        <>
          {buffer.value.name} {dirty && <Status tone="warn">未保存更改</Status>}
        </>
      }
    >
      <p className="muted">修改不会影响已经记录的任务。</p>
      <Field label="连接名称">
        <input
          value={buffer.value.name}
          onChange={(e) => update({ name: e.target.value })}
        />
      </Field>
      <Field label="服务类型">
        <select
          value={buffer.value.provider}
          onChange={(e) => update({ provider: e.target.value })}
        >
          <option>OpenAI 兼容</option>
          <option>本地示例</option>
        </select>
      </Field>
      <Field label="接口地址（演示配置）">
        <input
          value={buffer.value.endpoint}
          onChange={(e) => update({ endpoint: e.target.value })}
        />
      </Field>
      <Field label="默认模型">
        <input
          value={buffer.value.model}
          onChange={(e) => update({ model: e.target.value })}
        />
      </Field>
      <Field label="API Key" hint="本阶段不收集或保存真实密钥。">
        <input
          type="password"
          readOnly
          value="demo-placeholder"
          aria-label="密钥演示占位"
        />
      </Field>
      <details>
        <summary>请求选项</summary>
        <Field label="超时（秒）">
          <input
            type="number"
            min="1"
            max="300"
            value={buffer.value.timeout}
            onChange={(e) => update({ timeout: Number(e.target.value) })}
          />
        </Field>
      </details>
      <label className="inline-check">
        <input
          type="checkbox"
          checked={buffer.value.isDefault}
          onChange={(e) => update({ isDefault: e.target.checked })}
        />
        设为默认连接
      </label>
      <Panel title="连接测试">
        <p className="muted">测试仅验证演示场景，保存后才用于研究上下文。</p>
        {testMessage && <Notice tone="success">{testMessage}</Notice>}
        <ActionError error={testError} />
        <Button
          disabled={testing}
          onClick={async () => {
            setTesting(true);
            setTestError("");
            setTestMessage("");
            const tested = { ...buffer.getCurrent() };
            try {
              const message = await adapter.testConnection();
              if (
                JSON.stringify(buffer.getCurrent()) === JSON.stringify(tested)
              ) {
                setTestMessage(message);
                buffer.update({ ...tested, tested: true });
              } else {
                setTestMessage(
                  "测试期间配置已更改；新输入已保留，请重新测试当前配置。",
                );
              }
            } catch (error) {
              setTestError((error as Error).message);
            } finally {
              setTesting(false);
            }
          }}
        >
          {testing ? "测试中…" : testError ? "重试连接测试" : "测试连接"}
        </Button>
      </Panel>
      <ActionError error={action.error || buffer.bufferError} />
      <div className="action-footer">
        <Button
          onClick={() => {
            buffer.update(connection);
            buffer.clear();
            setTestMessage("");
            setTestError("");
          }}
        >
          恢复已保存
        </Button>
        <Button
          kind="primary"
          disabled={action.busy || testing}
          onClick={() => void save()}
        >
          保存连接
        </Button>
      </div>
    </Panel>
  );
}

export function Models({ id }: { id?: string }) {
  const { state, navigate } = useStudio(),
    selected =
      id === "new"
        ? emptyConnection
        : (state.connections.find((c) => c.id === id) ??
          state.connections[0] ??
          emptyConnection);
  return (
    <div className="page">
      <PageHeader
        title="模型连接"
        subtitle="配置研究助手使用的模型。当前为场景演示。"
      />
      <ResourceTabs active="models" />
      <div className="connection-layout">
        <section className="directory-list">
          <Button
            kind="primary"
            icon="plus"
            onClick={() => navigate("/resources/models/new")}
          >
            新建连接
          </Button>
          {state.connections.map((c) => (
            <button
              key={c.id}
              className={`directory-item ${c.id === selected.id ? "selected" : ""}`}
              onClick={() => navigate(`/resources/models/${c.id}`)}
            >
              <Icon name="cube" />
              <span>
                <strong>{c.name}</strong>
                <small>
                  {c.tested ? "已保存 · 演示测试成功" : "已保存 · 未测试"}
                </small>
              </span>
              {c.isDefault && <Status tone="blue">默认</Status>}
            </button>
          ))}
        </section>
        <ConnectionEditor
          key={`${selected.id}-${JSON.stringify(selected)}`}
          connection={selected}
        />
      </div>
    </div>
  );
}
