/**
 * The local appearance controls on the Settings page.
 *
 * Two choices, one restore action, and an honest line when this browser refused
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

export function AppearanceSettings({ runtime }: { runtime?: AppearanceRuntime } = {}) {
  const [appearance] = useState(() => runtime ?? getAppearanceRuntime())
  const [preferences, setPreferences] = useState<AppearancePreferences>(() => appearance.get())
  const [unsaved, setUnsaved] = useState(false)
  // The runtime outlives this panel, so unmounting only drops this subscriber.
  useEffect(() => appearance.subscribe(setPreferences), [appearance])
  const apply = (next: AppearancePreferences) => setUnsaved(!appearance.set(next))

  return <section className="panel appearance-settings" aria-label="本地外观">
    <header><h2>本地外观</h2><span>自动保存</span></header>
    <p className="muted">选择适合你的显示方式。更改立即生效，并保存在本机。</p>
    <div className="appearance-fields">
      <fieldset className="appearance-field">
        <legend>主题</legend>
        <div className="appearance-choices">{THEME_CHOICES.map(choice => <label key={choice.value}>
          <input type="radio" name="appearance-theme" value={choice.value} checked={preferences.theme === choice.value}
            onChange={() => apply({ ...preferences, theme: choice.value })} />
          {choice.label}
        </label>)}</div>
        <small>跟随系统，或为 QUINE 单独选择浅色与深色。</small>
      </fieldset>
      <fieldset className="appearance-field">
        <legend>密度</legend>
        <div className="appearance-choices">{DENSITY_CHOICES.map(choice => <label key={choice.value}>
          <input type="radio" name="appearance-density" value={choice.value} checked={preferences.density === choice.value}
            onChange={() => apply({ ...preferences, density: choice.value })} />
          {choice.label}
        </label>)}</div>
        <small>紧凑模式在同一屏幕中呈现更多内容。</small>
      </fieldset>
    </div>
    <div className="actions"><button type="button" onClick={() => apply(APPEARANCE_DEFAULTS)}>恢复默认</button><span className="muted">跟随系统 · 标准</span></div>
    {unsaved && <p role="alert" className="notice error">外观已应用于当前页面，但未能保存到本地存储；刷新后可能会回到上次保存的外观。</p>}
  </section>
}
