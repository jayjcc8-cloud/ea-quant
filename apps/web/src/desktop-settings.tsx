import { useCallback, useEffect, useRef, useState } from 'react'
import type { ApiAdapter } from './app'
import type { ModelConfigurationStatus } from './research-api'
import { getAppearanceRuntime, type ThemePreference } from './appearance'
import { AppearanceSettings } from './appearance-settings'

export type DesktopStatus = {
  application?: { desktop_version: string | null; engine_version: string; build_id: string | null; source_revision: string | null; source_dirty: boolean | null; built_at?: string | null }
  workspace: string
  next_workspace: string
  theme: ThemePreference
  startup_error: string | null
  model_error?: string | null
  model: ModelConfigurationStatus
}

export function DesktopSettings({ api, onSaved, section = 'all' }: { api: ApiAdapter; onSaved: () => void; section?: 'all' | 'model' | 'application' | 'appearance' | 'workspace' }) {
  const [webOnly, setWebOnly] = useState(false)
  const [status, setStatus] = useState<DesktopStatus | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const mounted = useRef(true)
  const [profileId, setProfileId] = useState('legacy')
  const [profileName, setProfileName] = useState('原有连接')
  const [provider, setProvider] = useState('deepseek')
  const [model, setModel] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [secret, setSecret] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [workspace, setWorkspace] = useState('')
  const [theme, setTheme] = useState<ThemePreference>('system')
  const edit = useCallback((id: string, value: ModelConfigurationStatus, name: string) => {
    setProfileId(id); setProfileName(name)
    setProvider(value.provider ?? 'deepseek'); setModel(value.model ?? '')
    setEndpoint(value.base_url ?? ''); setSecret(''); setClearKey(false)
  }, [])
  const draftKey = useCallback((value: DesktopStatus) => `quine.settings-draft.${encodeURIComponent(value.workspace)}.${section}`, [section])
  const baseline = useCallback((value: DesktopStatus) => JSON.stringify(section === 'workspace' ? value.next_workspace : section === 'appearance' ? value.theme : section === 'model' ? value.model : section === 'application' ? [value.next_workspace, value.theme] : value), [section])
  const reset = useCallback((value: DesktopStatus, editing = 'legacy') => {
    setStatus(value)
    const selected = value.model.profiles?.find(item => item.id === editing)
      ?? value.model.profiles?.find(item => item.id === value.model.default_profile_id)
    edit(selected?.id ?? 'legacy', selected ?? value.model, selected?.name ?? '原有连接')
    setWorkspace(value.next_workspace); setTheme(value.theme)
  }, [edit])
  useEffect(() => {
    mounted.current = true
    let current = true
    if (api.desktopSettings) void api.desktopSettings().then(value => {
      if (current) {
        reset(value)
        try {
          const draft = JSON.parse(sessionStorage.getItem(draftKey(value)) ?? 'null')
          if (draft?.baseline === baseline(value) && typeof draft.profileId === 'string') {
            setProfileId(draft.profileId); setProfileName(draft.profileName)
            setProvider(draft.provider); setModel(draft.model); setEndpoint(draft.endpoint)
            setWorkspace(draft.workspace); setTheme(draft.theme)
          }
        } catch { /* saved settings remain authoritative if the local draft cannot be read */ }
      }
    }).catch(caught => { if (current) {
      if (caught?.code === 'desktop_only') setWebOnly(true)
      else setError('无法读取桌面设置，请重试。')
    } })
    return () => { current = false; mounted.current = false }
  }, [api, reset, draftKey, baseline])
  useEffect(() => {
    if (!status) return
    // Keep ordinary input through navigation/reload. Never retain secrets or removal approval.
    try { sessionStorage.setItem(draftKey(status), JSON.stringify({ baseline: baseline(status), profileId, profileName, provider, model, endpoint, workspace, theme })) } catch { /* optional draft, never a settings write */ }
  }, [status, draftKey, baseline, profileId, profileName, provider, model, endpoint, workspace, theme])
  const save = async (body: object, success: string) => {
    if (pending.current || !api.saveDesktopSettings) return
    pending.current = true; setBusy(true); setError(''); setMessage('')
    // The password is held only in this form and this request; never persisted in the browser.
    setSecret('')
    try {
      const value = await api.saveDesktopSettings(body)
      if (!mounted.current) return
      reset(value, profileId); setMessage(success); onSaved()
      const appearance = getAppearanceRuntime()
      appearance.set({ ...appearance.get(), theme: value.theme })
    } catch (caught) {
      if (mounted.current) setError('next_workspace' in body && caught instanceof Error && 'code' in caught && caught.code === 'settings_not_saved'
        ? '工作区未保存。请选择已有的绝对路径（空目录或 QUINE 工作区），检查目录占用和读写权限，避免符号链接、应用目录或嵌套工作区。原设置和输入已保留。'
        : caught instanceof Error ? caught.message : '保存失败；原设置保留，请重试。')
    } finally {
      pending.current = false
      if (mounted.current) setBusy(false)
    }
  }
  let endpointError = ''
  if (endpoint.trim()) { try { const url = new URL(endpoint); if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) endpointError = '请输入不含凭据的 http:// 或 https:// 服务地址。' } catch { endpointError = '地址格式无效，请包含 http:// 或 https://；输入已保留。' } }
  const savedModel = status?.model.profiles?.find(item => item.id === profileId) ?? status?.model
  const modelDirty = !!status && (provider !== (savedModel?.provider ?? 'deepseek') || model !== (savedModel?.model ?? '') || endpoint !== (savedModel?.base_url ?? '') || !!secret || clearKey || (profileId !== 'legacy' && (!status.model.profiles?.some(item => item.id === profileId && item.name === profileName))))
  const dirty = !!status && (((section === 'model' || section === 'all') && modelDirty) || ((section === 'workspace' || section === 'application' || section === 'all') && workspace !== status.next_workspace) || ((section === 'appearance' || section === 'application' || section === 'all') && theme !== status.theme))
  const editingCredential = status?.model.profiles
    ? status.model.profiles.find(item => item.id === profileId)?.credential_available ?? false
    : status?.model.credential_available
  const cancel = () => { if (status) { reset(status, profileId); setError(''); setMessage('未保存的输入已取消。') } }
  if (!api.desktopSettings || webOnly) return section === 'model' ? <p>模型连接管理仅在 QUINE 桌面应用中可用。</p> : section === 'workspace' ? <p>工作区位置仅在 QUINE 桌面应用中管理；浏览器不会移动任何数据。</p> : <AppearanceSettings />
  return <section className={`panel settings-form settings-form-${section}`} aria-label={section === "model" ? "模型连接管理" : "桌面设置"}>
    <header><h2>{section === "model" ? "编辑模型连接" : section === "workspace" ? "工作区位置" : section === "appearance" ? "外观" : "桌面设置"}</h2><span>{section === "model" ? `${profileName || "当前连接"} · ${dirty ? "未保存草稿" : "连接未验证"}` : section === "workspace" ? "独立工作区 · 重启生效" : "保存后应用"}</span></header>
    {error && <p role="alert" className="notice error">{error}</p>}
    {!status && <button onClick={() => { void api.desktopSettings?.().then(reset).catch(() => setError('无法读取设置，请检查文件权限。')) }}>重新读取桌面设置</button>}
    {dirty && <p className="draft-state">未保存草稿</p>}
    {message && <p role="status">{message}</p>}
    {status && <>
      {status.startup_error && <p role="alert">{status.startup_error}</p>}
      {(section === 'all' || section === 'model') && <fieldset disabled={busy || !!status.model_error}>
        <legend>模型连接</legend>
        {status.model_error && <p role="alert">{status.model_error}</p>}
        <p className="model-scope">作用域：当前工作区。已批准轮次保留当时的连接身份。</p>
        {status.model.profiles && <>
          <p>工作区默认：<strong>{status.model.profiles.find(item => item.id === status.model.default_profile_id)?.name ?? '未设置'}</strong></p>
          <ul className="resource-entries">{status.model.profiles.map(item => <li className="resource-entry" key={item.id}>
            <strong>{item.name}</strong> · {item.provider} · {item.model} · v{item.revision}
            <p className="muted">{item.base_url || '提供方默认端点'} · {item.credential_available ? '凭据已保存，连接未验证' : '未配置凭据'}</p>
            <button onClick={() => edit(item.id, item, item.name)}>编辑 {item.name}</button>
            <button disabled={item.id === status.model.default_profile_id} onClick={() => void save({ default_model_profile: item.id }, '工作区默认已更改；历史调用和已批准轮次保持原配置。')}>{item.id === status.model.default_profile_id ? '当前默认' : `设为默认 ${item.name}`}</button>
          </li>)}</ul>
          <button onClick={() => edit(crypto.randomUUID(), { provider: 'deepseek', model: '', base_url: null } as ModelConfigurationStatus, '')}>添加连接</button>
          <label>连接名称<input value={profileName} onChange={e => setProfileName(e.target.value)} maxLength={100} disabled={profileId === 'legacy'} /></label>
        </>}
        <p>仅保存本地配置，不发送请求、不验证密钥。回到研究并发送消息后才会调用模型。</p>
        <label>提供方<select value={provider} onChange={e => setProvider(e.target.value)}>
          <option value="deepseek">DeepSeek</option><option value="anthropic">Anthropic</option>
          <option value="openai-chat-completions-compatible">OpenAI 兼容协议</option>
        </select></label>
        <label>模型 ID<input value={model} onChange={e => setModel(e.target.value)} maxLength={4096} /></label>
        <label>Base URL<input value={endpoint} onChange={e => setEndpoint(e.target.value)} placeholder="留空使用提供方默认端点" maxLength={4096} /></label>
        {endpointError && <p role="alert">{endpointError}</p>}
        <label>新 API Key<input type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} maxLength={4096} /></label>
        <p className="muted">{editingCredential ? '已有凭据；输入框留空保留当前凭据。' : editingCredential === false ? '尚无凭据。' : '凭据状态不可读。'}密钥只写不回显，不存入浏览器；离开页面会清空未保存密钥。更改提供方或端点需重新输入或清除凭据。</p>
        <label><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)} />清除已保存凭据</label>
        <button disabled={!!endpointError || !model.trim() || (profileId !== 'legacy' && !profileName.trim()) || (clearKey && !!secret)} onClick={() => void save(profileId === 'legacy' ? { model: { provider, model, base_url: endpoint, api_key: secret, clear_key: clearKey } } : { model_profile: { id: profileId, name: profileName, provider, model, base_url: endpoint, api_key: secret, clear_key: clearKey } }, '模型设置已保存；真实请求认证仍未验证。')}>保存模型设置</button>
      </fieldset>}
      {(section === 'all' || section === 'application' || section === 'workspace') && <fieldset disabled={busy}>
        <legend>工作区</legend>
        <div className="workspace-path-fact"><span>当前使用</span><code>{status.workspace}</code></div>
        {status.next_workspace !== status.workspace && <div className="workspace-path-fact"><span>下次启动</span><code>{status.next_workspace}</code></div>}
        <label className="workspace-path-field"><span>待使用位置<small>保存后重启生效</small></span><input aria-label="工作区绝对路径" value={workspace} onChange={e => setWorkspace(e.target.value)} maxLength={4096} /></label>
        <p className="muted workspace-path-help">选择已有的空目录或 QUINE 工作区。保存后继续使用当前位置；重启时打开新位置，旧数据不会移动或删除。</p><details className="workspace-path-details"><summary>切换前须知</summary><p>先等待运行结束，再退出并重新打开 QUINE；未完成的工作可能保留为结果未知。启动时会重新检查占用；模型配置属于各自工作区。</p></details>
        <div className="workspace-edit-actions">
          <button className="primary" disabled={!workspace.trim()} onClick={() => void save({ next_workspace: workspace }, '已保存下次启动位置；当前工作区未切换。请等待工作结束后退出并重开。')}>保存工作区位置</button>
          {section === 'workspace' && <button onClick={cancel}>取消未保存的修改</button>}
          {status.next_workspace !== status.workspace && <button onClick={() => void save({ next_workspace: status.workspace }, '已取消待切换；下次继续使用当前工作区。')}>取消待切换</button>}
        </div>
        {section === 'workspace' && <p className="workspace-edit-note">页面切换保留本次会话普通草稿；退出应用不保证保留。请保存设置，或取消放弃编辑。</p>}
      </fieldset>}
      {(section === 'all' || section === 'application' || section === 'appearance') && <fieldset disabled={busy}>
        <legend>外观</legend>
        <label>主题<select value={theme} onChange={e => setTheme(e.target.value as ThemePreference)}>
          <option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option>
        </select></label>
        <button onClick={() => void save({ theme }, '主题已保存，退出重开后仍然生效。')}>保存主题</button>
      </fieldset>}
      {section !== 'workspace' && <footer className="settings-edit-footer"><p>页面切换保留本次会话普通草稿；退出应用不保证保留。请保存设置，或取消放弃编辑。</p><button disabled={busy} onClick={cancel}>取消未保存的修改</button></footer>}
    </>}
  </section>
}
