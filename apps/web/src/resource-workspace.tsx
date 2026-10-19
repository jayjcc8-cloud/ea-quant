import { Navigate, NavLink, useLocation, useParams } from 'react-router-dom'
import type { ApiAdapter } from './app'
import { DataWorkspace, ExtensionWorkspace, ModelConnections } from './research'

/** Old links retain their import target, revision, return address and anchor. */
export function ResourceRedirect({ section }: { section: string }) {
  const location = useLocation()
  return <Navigate replace to={`/resources/${section}${location.search}${location.hash}`} state={location.state} />
}

export function ResourceWorkspace({ api }: { api: ApiAdapter }) {
  const { section } = useParams()
  const location = useLocation()
  if (section === 'diagnostics') return <Navigate replace to="/settings?section=diagnostics" state={location.state} />
  if (!['data', 'extensions', 'models'].includes(section ?? '')) return <ResourceRedirect section="data" />
  if (section === 'models') return <ModelConnections api={api} />
  return <>
    <header className="resource-page-title"><h1>资源</h1><p className="resource-scope">跨交易所共享 · 数据、策略与模型连接</p></header>
    <nav className="resource-section-tabs" aria-label="资源分类">
      <NavLink to={`/resources/data${location.search}`} state={location.state}>数据</NavLink>
      <NavLink to={`/resources/extensions${location.search}`} state={location.state}>策略与扩展</NavLink>
      <NavLink to={`/resources/models${location.search}`} state={location.state}>模型连接</NavLink>
    </nav>
    {section === 'data' && <DataWorkspace api={api} />}
    {section === 'extensions' && <ExtensionWorkspace api={api} />}
  </>
}
