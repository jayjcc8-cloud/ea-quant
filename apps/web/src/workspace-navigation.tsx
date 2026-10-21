import { Link, useLocation, type LinkProps } from 'react-router-dom'
import { importTarget } from './research-upload-binding'

type Origin = { to: string; label: string; state?: unknown }
const local = (to: unknown): to is string => typeof to === 'string' && /^\/(strategies|runs|resources|live|backtests|research|settings|platform|candidates|holdouts|batches|events|data|extensions)([/?#]|$)/.test(to) && !/[\\\r\n]/.test(to)
const labelFor = (path: string) => path === '/platform/binance' ? 'Binance' : path === '/platform/okx' ? 'OKX' : path === '/platform' ? '交易所配置' : path.startsWith('/strategies/') ? '当前研究'
  : path.startsWith('/live') ? '实盘预览' : path.startsWith('/runs') ? '模拟运行' : path.startsWith('/resources') || path === '/data' || path === '/extensions' ? '资源'
    : path === '/backtests' ? '回测列表' : path.startsWith('/backtests/') ? '实验报告' : path === '/settings' ? '设置' : '研究'
const originOf = (state: unknown): Origin | null => {
  const value = (state as { origin?: Origin } | null)?.origin
  return value && local(value.to) && typeof value.label === 'string' ? value : null
}

/** A real in-app source survives refresh in the history entry; no guessed history.back(). */
export function SourceLink({ state, ...props }: LinkProps) {
  const location = useLocation()
  const to = typeof props.to === 'string' ? props.to : props.to.pathname ?? location.pathname
  const sameArea = to.split(/[?#]/)[0] === location.pathname || (to.startsWith('/resources/') && location.pathname.startsWith('/resources/'))
  const origin = sameArea ? originOf(location.state) : {
    to: location.pathname + location.search + location.hash, label: location.pathname === '/strategies' && to.startsWith('/strategies/') ? '研究列表' : labelFor(location.pathname), state: location.state,
  }
  return <Link {...props} state={{ ...state, ...(origin ? { origin } : {}) }} />
}

function parentFor(path: string): Origin | null {
  if (path.startsWith('/strategies/')) return { to: '/strategies', label: '研究列表' }
  if (path.startsWith('/backtests/')) return { to: '/backtests', label: '回测列表' }
  if (path.startsWith('/candidates/')) return { to: '/candidates', label: '候选列表' }
  if (path.startsWith('/holdouts/')) return { to: '/holdouts', label: '留出记录' }
  if (path.startsWith('/batches/')) return { to: '/backtests', label: '回测列表' }
  if (path.startsWith('/research/')) return { to: '/research', label: '历史研究任务' }
  if (path === '/events') return { to: '/settings?section=diagnostics', label: '应用诊断' }
  if (/^\/(settings|platform|resources)(\/|$)/.test(path)) return { to: '/strategies', label: '工作页' }
  return null
}

export function WorkspaceNavigation() {
  const location = useLocation()
  const target = location.pathname.startsWith('/resources/') ? importTarget(location.search) : null
  const origin = target ? { to: target.returnTo, label: '当前研究' } : originOf(location.state)
  const parent = parentFor(location.pathname)
  const current = location.pathname + location.search + location.hash
  // Primary areas are directly selectable; only deeper/supporting pages have a
  // source return. The source carries its own query and state, not a history delta.
  if (['/strategies', '/runs', '/live', '/backtests'].includes(location.pathname)) return null
  const back = origin?.to !== current ? origin ?? parent : parent
  return back ? <nav className="workspace-navigation" aria-label="页面导航">
    <Link to={back.to} state={back.state}>返回{back.label}</Link>
  </nav> : null
}
