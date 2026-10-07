/**
 * The local appearance controls on the Settings page.
 *
 * Three choices, one restore action, and an honest line when this browser refused
 * to save them. Every value is read from, and written through, the one appearance
 * runtime: this panel holds no preference state of its own, so a change made here
 * is applied to the whole app immediately and survives a refresh.
 *
 * What is deliberately absent: there is no language, timezone or notification
 * switch here, because none exists in this product. The interface language is the
 * one the pages already use and timestamps are the ones the existing pages already
 * show; notifications are the in-app notices the pages already render. The scope
 * note below says exactly that rather than offering controls that would do nothing.
 */
import { useEffect, useState } from 'react'
import {
  AI_WIDTH_MAX,
  AI_WIDTH_MIN,
  APPEARANCE_DEFAULTS,
  getAppearanceRuntime,
  type AppearancePreferences,
  type AppearanceRuntime,
  type DensityPreference,
  type ThemePreference,
} from './appearance'

const THEME_CHOICES: { value: ThemePreference; label: string }[] = [
  { value: 'system', label: '跟随系统' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
]

const DENSITY_CHOICES: { value: DensityPreference; label: string }[] = [
  { value: 'normal', label: '标准' },
  { value: 'compact', label: '紧凑' },
]

/** A typed width, or the value already in force when it cannot be read. */
function widthFrom(raw: string, current: number): number {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return current
  return Math.min(AI_WIDTH_MAX, Math.max(AI_WIDTH_MIN, Math.round(parsed)))
}

export function AppearanceSettings({ runtime }: { runtime?: AppearanceRuntime } = {}) {
  const [appearance] = useState(() => runtime ?? getAppearanceRuntime())
  const [preferences, setPreferences] = useState<AppearancePreferences>(() => appearance.get())
  const [unsaved, setUnsaved] = useState(false)
  // The runtime outlives this panel, so unmounting only drops this subscriber.
  useEffect(() => appearance.subscribe(setPreferences), [appearance])
  const apply = (next: AppearancePreferences) => setUnsaved(!appearance.set(next))

  return <section className="panel appearance-settings" aria-label="本地外观">
    <header><h2>本地外观</h2><span>只保存在本机浏览器</span></header>
    <p className="muted">外观偏好立即作用于整个页面，并保存在这台机器的这个浏览器里，刷新后仍然生效。</p>
    <p className="muted">这里没有语言、时区或通知开关：界面语言与时间戳沿用现有页面，通知也只是现有的页内反馈，本页不申请系统或浏览器通知权限。</p>
    <div className="appearance-fields">
      <fieldset className="appearance-field">
        <legend>主题</legend>
        <div className="appearance-choices">{THEME_CHOICES.map(choice => <label key={choice.value}>
          <input type="radio" name="appearance-theme" value={choice.value} checked={preferences.theme === choice.value}
            onChange={() => apply({ ...preferences, theme: choice.value })} />
          {choice.label}
        </label>)}</div>
        <small>「跟随系统」时随操作系统的浅色/深色设置变化；选择浅色或深色后不再读取系统配色。本页不修改系统设置。</small>
      </fieldset>
      <fieldset className="appearance-field">
        <legend>密度</legend>
        <div className="appearance-choices">{DENSITY_CHOICES.map(choice => <label key={choice.value}>
          <input type="radio" name="appearance-density" value={choice.value} checked={preferences.density === choice.value}
            onChange={() => apply({ ...preferences, density: choice.value })} />
          {choice.label}
        </label>)}</div>
        <small>紧凑会减小桌面端的留白与行高；窄屏下的可点按目标仍不小于 44px。</small>
      </fieldset>
      <div className="appearance-field">
        <label htmlFor="appearance-ai-width">AI 侧栏宽度</label>
        <div className="appearance-width">
          <input id="appearance-ai-width" type="range" min={AI_WIDTH_MIN} max={AI_WIDTH_MAX} step={1}
            value={preferences.aiWidth} aria-describedby="appearance-ai-width-help"
            onChange={event => apply({ ...preferences, aiWidth: widthFrom(event.target.value, preferences.aiWidth) })} />
          <output data-appearance="ai-width" htmlFor="appearance-ai-width">{preferences.aiWidth} px</output>
        </div>
        <small id="appearance-ai-width-help">取值范围 {AI_WIDTH_MIN}–{AI_WIDTH_MAX} px，可用方向键调整。仅影响桌面端的 AI 栏；窄屏下 AI 栏仍然显示，并折叠为单列。</small>
      </div>
    </div>
    <div className="actions"><button type="button" onClick={() => apply(APPEARANCE_DEFAULTS)}>恢复默认</button><span className="muted">跟随系统 · 标准 · {APPEARANCE_DEFAULTS.aiWidth} px</span></div>
    {unsaved && <p role="alert" className="notice error">外观已应用于当前页面，但未能保存到本地存储；刷新后可能会回到上次保存的外观。</p>}
  </section>
}
