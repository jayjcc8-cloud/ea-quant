import { useCallback, useState } from 'react'
import type { ApiAdapter } from './app'
import type { UserStrategy } from './user-strategies-api'
import { DialogSurface } from '../studio/src/layout'
import { DesktopSettings } from './desktop-settings'
import { useRead } from './studio-read'

type ResearchModelControlProps = {
  api: ApiAdapter
  strategy: UserStrategy
  open: boolean
  onOpen: () => void
  onClose: () => void
  onSaved: () => void
  modelVersion?: number
}

function ModelSetupDialog({ api, strategy, onSaved, onClose }: Pick<ResearchModelControlProps, 'api' | 'strategy' | 'onSaved' | 'onClose'>) {
  const [saving, setSaving] = useState(false)
  const close = () => { if (!saving) onClose() }
  return <DialogSurface title="模型连接" className="real-model-dialog" onClose={close}>
    <p className="real-model-dialog-context">{strategy.name}</p>
    <DesktopSettings api={api} section="model" onSaved={onSaved} onSavingChange={setSaving} />
    <div className="real-model-dialog-actions">
      {saving && <small role="status">正在保存模型设置…</small>}
      <button className="button primary" disabled={saving} onClick={close}>返回研究</button>
    </div>
  </DialogSurface>
}

/** Keep the research and its assistant mounted; opening settings never changes the route. */
export function ResearchModelControl({ api, strategy, open, onOpen, onClose, onSaved, modelVersion = 0 }: ResearchModelControlProps) {
  const model = useRead(useCallback(() => api.settingsStatus!(), [api]), String(modelVersion))
  const selected = strategy.model_profile_id ? model.value?.profiles?.find(profile => profile.id === strategy.model_profile_id) : model.value
  const configured = !!selected?.configured
  const label = model.error ? 'AI 状态不可读' : !model.value ? 'AI 读取中…' : configured ? `AI · ${selected.model}` : 'AI 未配置'
  return <>
    <button className="button real-model-action" data-configured={configured} aria-label={configured ? '管理模型连接' : '配置模型连接'} aria-haspopup="dialog" aria-expanded={open}
      title={configured ? '模型配置已保存，连接尚未验证' : '配置模型连接'} onClick={onOpen}>
      <span className="real-model-dot" aria-hidden="true" />{label}
    </button>
    {open && <ModelSetupDialog api={api} strategy={strategy} onSaved={onSaved} onClose={onClose} />}
  </>
}
