import { BrowserRouter, NavLink, useLocation } from 'react-router-dom'

export type MockState = 'ready' | 'loading' | 'empty' | 'error'

type Metric = {
  label: string
  value: string
  detail: string
  tone?: 'positive' | 'neutral'
}

const pages = [
  'Orders',
  'Executions',
  'Positions',
  'Cash',
  'Risk',
  'Recovery',
  'Audit',
  'System Health',
]

export type OverviewSnapshot = {
  readonly metrics: readonly Metric[]
  readonly executions: readonly (readonly string[])[]
}

type MockOverview = {
  readonly state: MockState
  readonly snapshot: OverviewSnapshot
}

export type MockAdapter = {
  readonly kind: 'deterministic-read-only-mock'
  getOverview(search: string): MockOverview
}

const deterministicSnapshot: OverviewSnapshot = {
  metrics: [
    { label: 'Total Equity', value: '$2,485,320.18', detail: '+2.8% month to date' },
    { label: 'Daily P&L', value: '+$18,420.64', detail: '+0.75% today', tone: 'positive' },
    { label: 'Unrealized P&L', value: '+$6,184.22', detail: 'Across 7 positions', tone: 'positive' },
    { label: 'Available Margin', value: '$1,724,880.00', detail: '69.4% available', tone: 'neutral' },
  ] satisfies Metric[],
  executions: [
    ['09:42:18', 'ESU6', 'BUY', '12', '5,382.25'],
    ['09:37:04', 'NQU6', 'SELL', '8', '19,624.50'],
    ['09:31:51', 'CLV6', 'BUY', '15', '74.18'],
  ],
}

const deterministicMockAdapter: MockAdapter = {
  kind: 'deterministic-read-only-mock',
  getOverview(search) {
    const requested = new URLSearchParams(search).get('state')
    const state: MockState = requested === 'loading' || requested === 'empty' || requested === 'error'
      ? requested
      : 'ready'
    return { state, snapshot: deterministicSnapshot }
  },
}

function StatePanel({ state }: { state: MockState }) {
  if (state === 'loading') return <section className="state-panel">Loading mock market workspace…</section>
  if (state === 'empty') return <section className="state-panel">No mock positions in this workspace</section>
  if (state === 'error') return <section className="state-panel state-error">Mock data is temporarily unavailable</section>
  return null
}

function MetricCard({ metric }: { metric: Metric }) {
  return (
    <article className="metric-card">
      <span>{metric.label}</span>
      <strong className={metric.tone === 'positive' ? 'positive' : ''}>{metric.value}</strong>
      <small>{metric.detail}</small>
    </article>
  )
}

function Overview({ overview }: { overview: MockOverview }) {
  const { state, snapshot } = overview
  return (
    <>
      <PageTitle title="Overview" subtitle="Capital, exposure, and operations at a glance" />
      <StatePanel state={state} />
      {state === 'ready' && (
        <div className="overview-grid">
          <section className="metrics" aria-label="Mock account metrics">
            {snapshot.metrics.map((metric) => <MetricCard key={metric.label} metric={metric} />)}
          </section>
          <section className="panel curve-panel">
            <PanelHeading title="Equity Curve" note="Mock · trailing 30 sessions" />
            <div className="curve" aria-label="Mock equity curve"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
            <div className="curve-labels"><span>Jul 31</span><span>Aug 14</span><span>Aug 29</span></div>
          </section>
          <section className="panel allocation-panel">
            <PanelHeading title="Asset Allocation" note="Mock exposure" />
            <div className="allocation-ring"><strong>100%</strong><span>Allocated</span></div>
            <dl className="allocation-list"><div><dt>Equity index</dt><dd>42%</dd></div><div><dt>Rates</dt><dd>28%</dd></div><div><dt>Energy</dt><dd>18%</dd></div><div><dt>FX</dt><dd>12%</dd></div></dl>
          </section>
          <section className="panel positions-panel">
            <PanelHeading title="Open Positions" note="Mock · 7 instruments" />
            <div className="position-stat"><strong>7</strong><span>Open Positions</span><em>Gross $760,440</em></div>
          </section>
          <section className="panel orders-panel">
            <PanelHeading title="Active Orders" note="Mock · working" />
            <div className="position-stat"><strong>4</strong><span>Active Orders</span><em>All limits</em></div>
          </section>
          <section className="panel executions-panel">
            <PanelHeading title="Recent Executions" note="Mock · today" />
            <table><thead><tr><th>Time</th><th>Instrument</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead><tbody>{snapshot.executions.map((row) => <tr key={row.join('-')}>{row.map((cell) => <td key={cell}>{cell}</td>)}</tr>)}</tbody></table>
          </section>
          <section className="panel health-panel">
            <PanelHeading title="System Health" note="Mock · local adapter" />
            <div className="health-row"><span className="status-dot" />All mock services nominal</div>
            <dl className="environment"><div><dt>Environment</dt><dd>SIMULATION</dd></div><div><dt>Last Sync</dt><dd>09:45:00 UTC</dd></div></dl>
          </section>
        </div>
      )}
    </>
  )
}

function PanelHeading({ title, note }: { title: string; note: string }) {
  return <header className="panel-heading"><h2>{title}</h2><span>{note}</span></header>
}

function PageTitle({ title, subtitle }: { title: string; subtitle: string }) {
  return <header className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div><span className="mock-badge">MOCK DATA</span></header>
}

function SkeletonPage({ title }: { title: string }) {
  return <><PageTitle title={title} subtitle="Operational view prepared for future read-only integration" /><section className="skeleton"><span>Mock workspace · read-only skeleton</span><div /><div /><div /></section></>
}

function NotFoundPage({ path }: { path: string }) {
  return <><PageTitle title="Not Found" subtitle="This read-only mock workspace route is not available" /><section className="skeleton"><span>Read-only mock workspace has no route for {path}.</span></section></>
}

function Shell({ adapter }: { adapter: MockAdapter }) {
  const location = useLocation()
  const current = pages.find((page) => location.pathname === `/${page.toLowerCase().replaceAll(' ', '-')}`)
  const overview = adapter.getOverview(location.search)

  return (
    <div className="app-shell">
      <aside className="sidebar"><a className="brand" href="/"><span>EA</span><strong>QUANT</strong></a><p className="workspace">OPERATIONS CONSOLE</p><nav aria-label="Primary navigation"><NavLink end to="/">Overview</NavLink>{pages.map((page) => <NavLink key={page} to={`/${page.toLowerCase().replaceAll(' ', '-')}`}>{page}</NavLink>)}</nav><div className="sidebar-footer"><span className="status-dot" />Mock adapter active</div></aside>
      <main><header className="topbar"><span>Global Markets · USD</span><span>Read-only workspace</span></header><div className="content">{current ? <SkeletonPage title={current} /> : location.pathname === '/' ? <Overview overview={overview} /> : <NotFoundPage path={location.pathname} />}</div></main>
    </div>
  )
}

export function App({ adapter = deterministicMockAdapter }: { readonly adapter?: MockAdapter }) {
  return <BrowserRouter><Shell adapter={adapter} /></BrowserRouter>
}
