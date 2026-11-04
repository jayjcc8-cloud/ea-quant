import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { ApiAdapter } from './app'
import type { ResearchSummary } from './research-workbench-api'
import { DialogSurface, ErrorNote, Logo, Icon } from '../studio/src/layout'
import { useRead } from './studio-read'

function ResearchLinks({ items, selected, close }: { items: ResearchSummary[]; selected: string; close?: () => void }) {
  return <>{items.map(item => <Link key={item.user_strategy_id} title={item.name}
    className={`recent-link ${selected.includes(item.user_strategy_id) ? 'selected' : ''}`}
    to={`/research/${encodeURIComponent(item.user_strategy_id)}/overview`} onClick={close}>
    <i className="recent-dot" /><span><strong>{item.name}</strong>
      <small>草稿 r{item.draft.revision} · {item.draft.pending_proposals ? '待审阅' : item.draft.kind === 'intent' ? '待绑定' : '已绑定'}</small>
    </span>
  </Link>)}</>
}

export function WorkspaceNav({ api, researchVersion = '' }: { api: ApiAdapter; researchVersion?: string }) {
  const location = useLocation(), [query, setQuery] = useState('')
  const [panel, setPanel] = useState<'navigation' | 'search' | null>(null)
  const searchInput = useRef<HTMLInputElement>(null), results = useRef<HTMLDivElement>(null)
  const recent = useRead(useCallback(() => /^\/(?:$|research\/|report\/|records$|resources\/|data$|strategies|simulations|compare|holdout|candidates|settings\/)/.test(location.pathname)
    ? api.listResearch!({ sort: 'updated' }) : Promise.resolve(null), [api, location.pathname]), researchVersion)
  const search = useRead(useCallback(() => panel === 'search' ? api.listResearch!({ q: query, sort: 'updated' }) : Promise.resolve(null), [api, panel, query]), researchVersion)
  const close = () => setPanel(null)
  const moveSearchFocus = (event: ReactKeyboardEvent) => {
    if (event.nativeEvent.isComposing || !['ArrowDown', 'ArrowUp'].includes(event.key)) return
    const links = [...results.current?.querySelectorAll<HTMLAnchorElement>('a[href]') ?? []]
    if (!links.length) return
    const index = links.findIndex(link => link === document.activeElement)
    if (index === -1 && event.key === 'ArrowUp') return
    event.preventDefault()
    if (event.key === 'ArrowUp' && index === 0) searchInput.current?.focus()
    else links[Math.min(links.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1))]?.focus()
  }
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (event.isComposing || event.defaultPrevented || event.altKey || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return
      // Do not replace another task's confirmation or unsaved-input dialog.
      if (document.querySelector('dialog[open]:not(.persistent-agent):not(.real-search-dialog):not(.real-navigation-dialog), [role="dialog"][aria-modal="true"]:not(.real-search-dialog):not(.real-navigation-dialog)')) return
      event.preventDefault(); setPanel('search')
    }
    document.addEventListener('keydown', shortcut)
    return () => document.removeEventListener('keydown', shortcut)
  }, [])
  const sidebar = (drawer = false) => <aside className={`sidebar${drawer ? ' drawer-sidebar' : ''}`} aria-label="工作区导航">
    <Link className="real-logo" to="/" onClick={close}><Logo /></Link>
    <div className="workspace-select"><Link className="button" to="/settings/workspace" aria-label="工作区位置" onClick={close}><Icon name="folder" /><span className="label">本地研究空间</span></Link></div>
    <Link className="button primary new-button" to="/" title="新建研究" onClick={close}><Icon name="plus" /><span>新建研究</span></Link>
    <button className="sidebar-search" aria-label="搜索研究" onClick={() => setPanel('search')}><Icon name="search" /><span>搜索研究</span><kbd>⌘ K</kbd></button>
    <div className="recent-heading">最近研究</div>
    <div className="recent-list"><ErrorNote>{recent.error}</ErrorNote>
      <ResearchLinks items={recent.value?.items.slice(0, 5) ?? []} selected={location.pathname} close={close} />
      {recent.value && !recent.value.items.length && <small className="muted">尚无研究</small>}
    </div>
    <div className="sidebar-footer">
      <Link className="sidebar-link" to="/resources/data" title="数据资源" onClick={close}><Icon name="data" /><span className="label">数据资源</span></Link>
      <Link className="sidebar-link" to="/resources/models" title="模型连接" onClick={close}><Icon name="cube" /><span className="label">模型连接</span></Link>
      <span className="demo-foot">本地研究 · 实盘尚未开放</span>
    </div>
  </aside>
  return <>{sidebar()}<header className="topbar">
    <button className="icon-button workspace-menu" aria-label="打开工作区导航" onClick={() => setPanel('navigation')}><Icon name="menu" /></button>
    <nav className="primary-nav" aria-label="主要导航">{[['/', '研究'], ['/records', '回测记录'], ['/simulations', '模拟运行']].map(([path, label]) =>
      <Link key={path} aria-current={(path === '/' ? location.pathname === '/' || location.pathname.startsWith('/research/') : path === '/records' ? /\/(records|report|compare|holdout|candidates)/.test(location.pathname) : location.pathname.startsWith(path)) ? 'page' : undefined} to={path} onClick={close}>{label}</Link>)}</nav>
    <span className="environment"><i className="recent-dot" />本地</span>
  </header>
  {panel && <DialogSurface title={panel === 'search' ? '搜索研究' : '工作区导航'} onClose={close} className={panel === 'search' ? 'real-search-dialog' : 'real-navigation-dialog'}>
    {panel === 'navigation' ? sidebar(true) : <>
      <label className="real-search-input"><Icon name="search" /><input ref={searchInput} type="search" data-autofocus aria-label="搜索研究" placeholder="输入研究名称、目标或标的" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={moveSearchFocus} /></label>
      <div ref={results} className="real-search-results" aria-live="polite" onKeyDown={moveSearchFocus}>
        <ErrorNote>{search.error}</ErrorNote>
        {!search.value && !search.error && <p role="status">正在搜索研究…</p>}
        {search.value && <><p className="muted">{query ? '匹配研究' : '全部研究'} · {search.value.matched} 项</p>
          <ResearchLinks items={search.value.items} selected={location.pathname} close={close} />
          {!search.value.items.length && <p>没有匹配研究，试试其他名称或目标。</p>}</>}
      </div>
    </>}
  </DialogSurface>}
  </>
}
