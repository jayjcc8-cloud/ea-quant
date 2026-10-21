import { useParams } from 'react-router-dom'
import { platformNames, usePlatform } from './platform-context'

export function PlatformWorkspace() {
  const { platformId } = useParams()
  const { platform } = usePlatform()
  const id = platformId === 'binance' || platformId === 'okx' ? platformId : platform
  const name = id ? platformNames[id] : null
  return <section className="platform-page">
    <header className="page-title"><div><h1>{name ? `${name} 交易所配置` : '交易所配置'}</h1><p>账户、行情与执行能力分别管理。共享研究和资源保持原归属。</p></div><span className="chip">未接入</span></header>
    {!name && <p className="notice">先从侧栏选择 Binance 或 OKX，查看该交易所的连接能力。</p>}
    <dl className="fact-strip"><div><dt>交易所</dt><dd>{name ?? '未选择'}</dd></div><div><dt>市场类型</dt><dd>尚未配置</dd></div><div><dt>连接状态</dt><dd>未接入 · 界面预览</dd></div></dl>
    {[['账户连接', '账户信息与资产读取尚未接入。当前不收集交易所凭据。'], ['行情能力', '交易所行情尚未接入。本地历史数据仍可用于研究，本机生成行情仍可用于模拟。'], ['执行能力', '真实委托与自动交易不可用。接受研究候选不等于获得实盘执行授权。']].map(([title, note]) => <section className="connection-row" key={title}><div><h2>{title}</h2><p>{note}</p></div><span className="chip">未接入</span><button disabled aria-describedby="platform-unavailable">配置接入</button></section>)}
    <p id="platform-unavailable" className="muted">当前版本尚无连接服务，因此无法配置或保存连接；本页不会发出外部请求。</p>
  </section>
}
