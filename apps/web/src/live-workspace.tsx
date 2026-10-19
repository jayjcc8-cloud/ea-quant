import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ResearchSplitLayout } from './agent-panel-layout'
import { platformNames, usePlatform } from './platform-context'
import { SourceLink as Link } from './workspace-navigation'

type StopState = 'running' | 'processing' | 'unknown' | 'confirmed'
const labels: Record<StopState, string> = { running: '演示运行中', processing: '处理中 · 停止尚未确认', unknown: '结果未知 · 等待原请求查询', confirmed: '已确认停止 · 仍有持仓与委托' }
function Banner() { return <p className="preview-banner">实盘预览 · 未接入 · 全部为演示数据</p> }
const read = (key: string, fallback: string) => { try { return sessionStorage.getItem(key) ?? fallback } catch { return fallback } }

/** Deliberately has no API adapter or execution transport. All amounts and states
 * in this route are a named, local UI demonstration, never account observations. */
export function LiveWorkspace() {
  const { platform } = usePlatform()
  const [params] = useSearchParams()
  const view = params.get('view') ?? 'overview'
  const exchange = platform ? platformNames[platform] : '未选择交易所'
  const [stopState, setStopState] = useState<StopState>('running')
  const [queried, setQueried] = useState(false)
  const [modal, setModal] = useState(false)
  const dialog = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const [allocation, setAllocation] = useState(() => read('quine.live-preview.allocation', '5000'))
  const [orderLimit, setOrderLimit] = useState(() => read('quine.live-preview.order-limit', '200'))
  const [positionLimit, setPositionLimit] = useState(() => read('quine.live-preview.position-limit', '3000'))
  useEffect(() => { try {
    sessionStorage.setItem('quine.live-preview.allocation', allocation)
    sessionStorage.setItem('quine.live-preview.order-limit', orderLimit)
    sessionStorage.setItem('quine.live-preview.position-limit', positionLimit)
  } catch { /* fields still work in memory */ } }, [allocation, orderLimit, positionLimit])
  useEffect(() => { if (modal) dialog.current?.focus() }, [modal])
  const close = () => { setModal(false); trigger.current?.focus() }
  const query = () => setQueried(true)
  return <ResearchSplitLayout storageKey="live-preview">
    <div className="strategy-column live-main">
      <Banner />
      {view !== 'overview' && <Link to="/live">← 实盘运行列表</Link>}
      <header className="page-title"><div><h1>{view === 'start' ? '实盘启动预览' : view === 'detail' ? '趋势策略 · 演示详情' : '实盘运行'}</h1><p>{exchange} · 现货（演示） · BTC/USDT（演示标的）</p></div><span className="chip">未接入</span></header>
      {view === 'start' ? <>
        <section className="panel"><h2>候选与冻结输入（演示）</h2><p>C-DEMO · 趋势策略 v3 · 来源报告 R-DEMO · 后续时间窗已验证（演示）</p><p className="preview-banner">演示候选，不是真实已接受证据。接受候选不等于实盘执行授权。</p><dl className="fact-strip"><div><dt>策略参数</dt><dd>示例周期 4h · 只读</dd></div><div><dt>连接状态</dt><dd>未接入</dd></div></dl></section>
        <section className="panel"><h2>运行资金配置（预览草稿）</h2><div className="parameter-grid"><label>运行资金分配（USDT）<input inputMode="decimal" value={allocation} onChange={e => setAllocation(e.target.value)} /></label><label>单笔保护限额（USDT）<input inputMode="decimal" value={orderLimit} onChange={e => setOrderLimit(e.target.value)} /></label><label>最大持仓限额（USDT）<input inputMode="decimal" value={positionLimit} onChange={e => setPositionLimit(e.target.value)} /></label></div><p className="muted">仅保留本窗口的演示输入，不改真实候选或冻结资金。修改冻结输入需重新形成证据并接受。</p><div className="actions"><Link to="/live">返回并保留预览草稿</Link><button disabled>启动实盘（未接入）</button></div><p>交易所未接入，当前仅可预览。</p></section>
      </> : <>
        {view === 'overview' ? <>
          <section className="panel"><h2>演示账户概况</h2><dl className="live-metrics"><div><dt>账户总权益</dt><dd>10,000 USDT</dd></div><div><dt>可用资金</dt><dd>7,500 USDT</dd></div><div><dt>持仓市值</dt><dd>2,500 USDT</dd></div></dl></section>
          <section className="panel"><h2>策略列表（演示）</h2><div className="table-scroll"><table><thead><tr><th>策略</th><th>状态</th><th>标的</th><th>操作</th></tr></thead><tbody><tr><td>趋势策略 v3</td><td>{labels[stopState]}</td><td>BTC/USDT</td><td><Link to="/live?view=detail">查看演示详情</Link></td></tr></tbody></table></div><div className="actions"><Link to="/live?view=start">查看启动预览</Link><button disabled>启动实盘（未接入）</button></div><p className="muted">没有外部账户或执行能力。</p></section>
        </> : <>
          <section className="panel"><h2>本运行配置（演示）</h2><dl className="live-metrics"><div><dt>本运行分配</dt><dd>5,000 USDT</dd></div><div><dt>单笔保护限额</dt><dd>200 USDT</dd></div><div><dt>最大持仓限额</dt><dd>3,000 USDT</dd></div></dl><button ref={trigger} onClick={() => setModal(true)}>预览停止流程</button><p className="muted">停止策略不撤单、不平仓。关闭预览窗口不代表已停止。</p></section>
          <section className="panel" aria-live="polite"><h2>策略状态（演示）</h2><p>{labels[stopState]}</p>{stopState !== 'running' && <><p>原请求 DEMO-042 · 演示观测时间 10:03:12</p><button onClick={query}>查询原请求（预览）</button>{queried && <p role="status">已查询 DEMO-042，返回当前演示状态；没有重复发送停止指令。</p>}</>}</section>
        </>}
        <section className="panel"><h2>当前持仓（演示）</h2><div className="table-scroll"><table><thead><tr><th>标的</th><th>数量</th><th>均价（USDT）</th><th>市值（USDT）</th><th>盈亏（USDT）</th></tr></thead><tbody><tr><td>BTC/USDT</td><td>0.05 BTC</td><td>49,800</td><td>2,500</td><td>+10</td></tr></tbody></table></div></section>
        <section className="panel"><h2>未完成委托（演示）</h2><div className="table-scroll"><table><thead><tr><th>时间</th><th>方向</th><th>数量</th><th>限价（USDT）</th><th>状态</th></tr></thead><tbody><tr><td>10:01:26</td><td>买入</td><td>0.001 BTC</td><td>49,500</td><td>待成交（演示）</td></tr></tbody></table></div><p className="muted">这些是固定演示数据；停止状态切换不删除持仓或委托。</p></section>
      </>}
      {modal && <div className="preview-backdrop"><div className="preview-dialog" ref={dialog} role="dialog" aria-modal="true" aria-label="停止流程预览" tabIndex={-1} onKeyDown={e => {
        if (e.key === 'Escape') { e.preventDefault(); close() }
        if (e.key === 'Tab') {
          const nodes = dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
          const first = nodes?.[0], last = nodes?.[nodes.length - 1]
          if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { e.preventDefault(); last?.focus() }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
        }
      }}><h2>停止流程预览</h2><Banner /><p>停止策略不撤单、不平仓；关闭窗口不代表已停止。</p><p className="muted">选择独立的演示状态，编号或顺序不代表真实流程。</p><div className="actions">{([['processing', '处理中'], ['unknown', '结果未知'], ['confirmed', '已确认停止']] as const).map(([key, name]) => <button key={key} aria-pressed={stopState === key} onClick={() => { setStopState(key); setQueried(false) }}>{name}</button>)}</div><p>{labels[stopState]}</p><p>原请求 DEMO-042 · 剩余持仓 0.05 BTC · 未完成委托 1</p><div className="actions"><button onClick={close}>返回详情</button><button onClick={query}>查询原请求（预览）</button></div>{queried && <p role="status">已查询 DEMO-042；没有重复发出停止指令。</p>}</div></div>}
    </div>
    <aside className="strategy-ai preview-assistant"><header><h2>QUINE 运行助手</h2><p>当前上下文：实盘预览 · {exchange}</p></header><div><h3>预览说明</h3><p>这里展示页面与状态语义，所有数字均为演示。没有真实账户、行情或委托。</p><p>接受候选、预览资金与停止演示都不会获得或触发执行授权。</p><small>界面说明 · 非模型回复</small></div><footer><p>实盘 AI 尚未接入。研究与本地模拟可使用各自的真实上下文。</p><Link to="/strategies">返回研究</Link></footer></aside>
  </ResearchSplitLayout>
}
