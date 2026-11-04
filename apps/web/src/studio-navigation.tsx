import { useCallback, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { ApiAdapter } from './app'
import { ErrorNote, Logo, Icon } from '../studio/src/layout'
import { useRead } from './studio-read'

export function WorkspaceNav({ api }: { api: ApiAdapter }) {
  const location = useLocation(), [query, setQuery] = useState('')
  const recent = useRead(useCallback(() => /^\/(?:$|research\/|report\/|records$|resources\/|data$|strategies)/.test(location.pathname) ? api.listResearch!({ q: query, sort: 'updated' }) : Promise.resolve(null), [api, query, location.pathname]))
  const rows = recent.value?.items ?? []
  return <><aside className="sidebar" aria-label="工作区导航">
    <Link className="real-logo" to="/"><Logo /></Link>
    <div className="workspace-select"><Link className="button" to="/settings/workspace" aria-label="工作区位置"><Icon name="folder" /><span className="label">本地研究空间</span></Link></div>
    <Link className="button primary new-button" to="/" title="新建研究"><Icon name="plus" /><span>新建研究</span></Link>
    <label className="sidebar-search"><Icon name="search" /><input aria-label="搜索研究" placeholder="搜索研究" value={query} onChange={e => setQuery(e.target.value)} /></label>
    <div className="recent-heading">{query ? '搜索结果' : '最近研究'}</div>
    <div className="recent-list"><ErrorNote>{recent.error}</ErrorNote>{rows.slice(0, 5).map(item => <Link key={item.user_strategy_id} title={item.name} className={`recent-link ${location.pathname.includes(item.user_strategy_id) ? 'selected' : ''}`} to={`/research/${encodeURIComponent(item.user_strategy_id)}/overview`}><i className="recent-dot" /><span><strong>{item.name}</strong><small>草稿 r{item.draft.revision} · {item.draft.pending_proposals ? '待审阅' : item.draft.kind === 'intent' ? '待绑定' : '已绑定'}</small></span></Link>)}{recent.value && !rows.length && <small className="muted">{query ? '没有匹配研究' : '尚无研究'}</small>}</div>
    <div className="sidebar-footer"><Link className="sidebar-link" to="/resources/data" title="数据资源"><Icon name="data" /><span className="label">数据资源</span></Link><Link className="sidebar-link" to="/resources/models" title="模型连接"><Icon name="cube" /><span className="label">模型连接</span></Link><span className="demo-foot">本地研究候选 · 未发布</span></div>
  </aside><header className="topbar"><nav className="primary-nav" aria-label="主要导航">{[['/', '研究'], ['/simulations', '模拟运行'], ['/live', '实盘运行'], ['/records', '回测记录']].map(([path, label]) => <Link key={path} aria-current={(path === '/' ? location.pathname === '/' || location.pathname.startsWith('/research/') : path === '/records' ? /\/(records|report|compare|holdout|candidates)/.test(location.pathname) : location.pathname === path) ? 'page' : undefined} to={path}>{label}{path === '/live' && <small>未接入</small>}</Link>)}</nav><span className="environment"><i className="recent-dot" />本地</span></header></>
}
