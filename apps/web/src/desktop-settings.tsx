import { useEffect, useRef, useState } from 'react'
import type { ApiAdapter } from './app'
import type { ModelConfigurationStatus } from './research-api'
import { getAppearanceRuntime, type ThemePreference } from './appearance'
import { AppearanceSettings } from './appearance-settings'

export type DesktopStatus = {
  workspace: string
  next_workspace: string
  theme: ThemePreference
  startup_error: string | null
  model_error?: string | null
  model: ModelConfigurationStatus
}

export function DesktopSettings({ api, onSaved }: { api: ApiAdapter; onSaved: () => void }) {
  const [webOnly, setWebOnly] = useState(false)
  const [status, setStatus] = useState<DesktopStatus | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const mounted = useRef(true)
  const [provider, setProvider] = useState('deepseek')
  const [model, setModel] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [secret, setSecret] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [workspace, setWorkspace] = useState('')
  const [theme, setTheme] = useState<ThemePreference>('system')
  const reset = (value: DesktopStatus) => {
    setStatus(value); setProvider(value.model.provider ?? 'deepseek'); setModel(value.model.model ?? '')
    setEndpoint(value.model.base_url ?? ''); setSecret(''); setClearKey(false)
    setWorkspace(value.next_workspace); setTheme(value.theme)
  }
  useEffect(() => {
    mounted.current = true
    let current = true
    if (api.desktopSettings) void api.desktopSettings().then(value => {
      if (current) reset(value)
    }).catch(caught => { if (current) {
      if (caught?.code === 'desktop_only') setWebOnly(true)
      else setError('无法读取桌面设置，请重试。')
    } })
    return () => { current = false; mounted.current = false }
  }, [api])
  const save = async (body: object, success: string) => {
    if (pending.current || !api.saveDesktopSettings) return
    pending.current = true; setBusy(true); setError(''); setMessage('')
    // The password is held only in this form and this request; never persisted in the browser.
    setSecret('')
    try {
      const value = await api.saveDesktopSettings(body)
      if (!mounted.current) return
      reset(value); setMessage(success); onSaved()
      const appearance = getAppearanceRuntime()
      appearance.set({ ...appearance.get(), theme: value.theme })
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : '保存失败；原设置保留，请重试。')
    } finally {
      pending.current = false
      if (mounted.current) setBusy(false)
    }
  }
  if (!api.desktopSettings || webOnly) return <AppearanceSettings />
  return <section className="panel" aria-label="桌面设置">
    <header><h2>桌面设置</h2><span>保存在本机，退出重开后恢复</span></header>
    {error && <p role="alert" className="notice error">{error}</p>}
    {!status && <button onClick={() => { void api.desktopSettings?.().then(reset).catch(() => setError('无法读取设置，请检查文件权限。')) }}>重新读取桌面设置</button>}
    {message && <p role="status">{message}</p>}
    {status && <>
      {status.startup_error && <p role="alert">{status.startup_error}</p>}
      <fieldset disabled={busy || !!status.model_error}>
        <legend>模型连接</legend>
        {status.model_error && <p role="alert">{status.model_error}</p>}
        <p>仅保存本地配置，不发送请求、不验证密钥。回到策略并明确批准询问后才会调用模型。</p>
        <label>提供方<select value={provider} onChange={e => setProvider(e.target.value)}>
          <option value="deepseek">DeepSeek</option><option value="anthropic">Anthropic</option>
          <option value="openai-chat-completions-compatible">OpenAI 兼容协议</option>
        </select></label>
        <label>模型 ID<input value={model} onChange={e => setModel(e.target.value)} maxLength={4096} /></label>
        <label>Base URL<input value={endpoint} onChange={e => setEndpoint(e.target.value)} placeholder="留空使用提供方默认端点" maxLength={4096} /></label>
        <label>新 API Key<input type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} maxLength={4096} /></label>
        <p className="muted">{status.model.credential_available ? '已有凭据；输入框留空保留当前凭据。' : status.model.credential_available === false ? '尚无凭据。' : '凭据状态不可读。'}凭据只写不回显，以当前用户可读写的 0600 文件保存在此工作区，不使用浏览器存储。更改提供方或端点需重新输入或清除凭据。</p>
        <label><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)} />清除已保存凭据</label>
        <button disabled={!model.trim() || (clearKey && !!secret)} onClick={() => void save({ model: { provider, model, base_url: endpoint, api_key: secret, clear_key: clearKey } }, '模型设置已保存；真实请求认证仍未验证。')}>保存模型设置</button>
      </fieldset>
      <fieldset disabled={busy}>
        <legend>工作区</legend>
        <p>当前工作区：<code>{status.workspace}</code></p>
        <p>下次启动：<code>{status.next_workspace}</code></p>
        <label>工作区绝对路径<input value={workspace} onChange={e => setWorkspace(e.target.value)} maxLength={4096} /></label>
        <p className="muted">选择已存在的空目录或已有 QUINE 工作区。保存后当前工作继续使用原目录。先等待运行结束，再退出并重新打开 QUINE；未完成的工作可能保留为结果未知。启动时会重新检查占用。不会搬移、复制或删除旧数据；模型配置属于各自工作区。</p>
        <button disabled={!workspace.trim()} onClick={() => void save({ next_workspace: workspace }, '已保存下次启动位置；当前工作区未切换。请等待工作结束后退出并重开。')}>打开此工作区（重启生效）</button>
        {status.next_workspace !== status.workspace && <button onClick={() => void save({ next_workspace: status.workspace }, '已取消待切换；下次继续使用当前工作区。')}>取消待切换</button>}
      </fieldset>
      <fieldset disabled={busy}>
        <legend>外观</legend>
        <label>主题<select value={theme} onChange={e => setTheme(e.target.value as ThemePreference)}>
          <option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option>
        </select></label>
        <button onClick={() => void save({ theme }, '主题已保存，退出重开后仍然生效。')}>保存主题</button>
        <p className="muted">界面沿用现有语言，本批不提供未完成的翻译开关。</p>
      </fieldset>
      <button disabled={busy} onClick={() => { reset(status); setError(''); setMessage('未保存的输入已取消。') }}>取消未保存的修改</button>
    </>}
  </section>
}
