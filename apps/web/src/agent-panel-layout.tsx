import { useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

import { WorkspaceChrome, WorkspaceView, type WorkspaceViewName as View, type FeedPosition as ScrollPosition } from './agent-panel-view'

// Presentation state only. No conversation, permission or execution state lives here.
const WIDTH_KEY = 'quine.research.ai-width'
const clamp = (value: number) => Math.min(58, Math.max(32, value))

export function ResearchSplitLayout({ children, storageKey = 'research' }: { children: [ReactNode, ReactNode]; storageKey?: string }) {
  const chrome = useContext(WorkspaceChrome)
  const root = useRef<HTMLDivElement>(null)
  const [positions] = useState(() => new Map<string, ScrollPosition>())
  const key = `quine.panel.${storageKey}`
  const saved = () => { try { return JSON.parse(sessionStorage.getItem(key) ?? '{}') } catch { return {} } }
  const [view, setView] = useState<View>(() => saved().view === 'ai' ? 'ai' : 'work')
  const [dock, setDock] = useState<'split' | 'closed' | 'expanded'>(() => ['closed', 'expanded'].includes(saved().dock) ? saved().dock : 'split')
  useEffect(() => { try { sessionStorage.setItem(key, JSON.stringify({ view, dock })) } catch { /* optional view preference */ } }, [key, view, dock])
  useLayoutEffect(() => {
    const column = root.current?.querySelector<HTMLElement>(':scope > .strategy-column')
    if (!column) return
    let wanted = 0
    try { wanted = Number(sessionStorage.getItem(`${key}.scroll`)) || 0 } catch { /* optional position */ }
    let pending = wanted > 0
    const restore = () => {
      if (!pending || column.clientHeight === 0) return
      column.scrollTop = wanted
      if (column.scrollTop >= wanted - 1) pending = false
    }
    const cancel = () => { pending = false }
    // Wait for asynchronous reports before restoring their scroll offset.
    const mutations = new MutationObserver(restore)
    mutations.observe(column, { childList: true, subtree: true })
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(restore)
    resizeObserver?.observe(column)
    for (const event of ['wheel', 'pointerdown', 'keydown']) column.addEventListener(event, cancel, { passive: true })
    restore()
    return () => {
      mutations.disconnect(); resizeObserver?.disconnect()
      for (const event of ['wheel', 'pointerdown', 'keydown']) column.removeEventListener(event, cancel)
      try { sessionStorage.setItem(`${key}.scroll`, String(pending ? wanted : column.scrollTop)) } catch { /* optional position */ }
    }
  }, [key])
  const show = (next: View) => { setView(next); setDock('split') }
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(WIDTH_KEY))
      return saved && Number.isFinite(saved) ? clamp(saved) : 32
    } catch { return 32 }
  })
  const resize = (value: number) => {
    const next = Math.round(clamp(value))
    setWidth(next)
    try { localStorage.setItem(WIDTH_KEY, String(next)) } catch { /* optional appearance preference */ }
  }
  // The shell's header wraps on small screens. Measure the space actually left
  // below this panel rather than assuming a fixed header height.
  useLayoutEffect(() => {
    const node = root.current
    const panel = node?.querySelector<HTMLElement>(':scope > .strategy-ai')
    if (!node || !panel) return
    const fit = () => {
      const top = Math.max(0, node.getBoundingClientRect().top)
      const height = `${Math.max(180, document.documentElement.clientHeight - top)}px`
      if (node.style.getPropertyValue('--workspace-height') !== height) node.style.setProperty('--workspace-height', height)
    }
    fit()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit)
    observer?.observe(node)
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, { passive: true })
    return () => { observer?.disconnect(); window.removeEventListener('resize', fit); window.removeEventListener('scroll', fit) }
  }, [view, dock])
  const dragging = useRef(false)
  return <WorkspaceView value={{ show, positions }}>
    <div className="strategy-workspace" ref={root} data-workspace-view={view} data-ai-dock={dock}
      style={{ '--agent-width': `${width}%` } as CSSProperties}>
      <header className="canvas-chrome">{chrome}</header>
      <div className="workspace-view-switch" role="group" aria-label="工作区视图">
        <button type="button" aria-pressed={view === 'work'} onClick={() => show('work')}>主内容</button>
        <button type="button" aria-pressed={view === 'ai'} onClick={() => show('ai')}>AI 协作</button>
      </div>
      <div className="ai-dock-controls" role="group" aria-label="AI 面板布局">
        {dock === 'closed' ? <button type="button" aria-label="展开 AI 面板" title="展开 AI 面板" onClick={() => show('ai')}>✦ AI</button> : <>
          <button type="button" aria-label={dock === 'expanded' ? '恢复双栏' : '放大 AI 面板'} title={dock === 'expanded' ? '恢复双栏' : '放大 AI 面板'} onClick={() => { setView('ai'); setDock(dock === 'expanded' ? 'split' : 'expanded') }}>↗</button>
          <button type="button" aria-label="收起 AI 面板" title="收起 AI 面板" onClick={() => { setDock('closed'); setView('work') }}>−</button>
        </>}
      </div>
      {children[0]}
      <div className="workspace-resizer" role="separator" tabIndex={0} aria-label="调整 AI 面板宽度"
        aria-orientation="vertical" aria-valuemin={32} aria-valuemax={58} aria-valuenow={width}
        aria-valuetext={`AI 面板占 ${width}%`} title="拖动调整宽度；方向键微调；双击恢复"
        onDoubleClick={() => resize(32)}
        onKeyDown={event => {
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault()
            resize(event.key === 'Home' ? 32 : event.key === 'End' ? 58 : width + (event.key === 'ArrowLeft' ? 2 : -2))
          }
        }}
        onPointerDown={event => {
          if (event.button !== 0) return
          dragging.current = true
          event.currentTarget.setPointerCapture(event.pointerId)
          event.preventDefault()
        }}
        onPointerMove={event => {
          if (!dragging.current) return
          const box = root.current?.getBoundingClientRect()
          if (box?.width) resize((box.right - event.clientX) / box.width * 100)
        }}
        onPointerUp={() => { dragging.current = false }}
        onPointerCancel={() => { dragging.current = false }}
        onLostPointerCapture={() => { dragging.current = false }} />
      {children[1]}
    </div>
  </WorkspaceView>
}

/** Follow new output only while the reader is at the end. Revision remounts keep
 * the current research's scroll position, never another research's position. */
export function AgentFeed({ identity, version, children }: { identity: string; version: string; children: ReactNode }) {
  const { positions } = useContext(WorkspaceView)
  const scroller = useRef<HTMLDivElement>(null)
  const position = useRef<ScrollPosition>(positions.get(identity) ?? { top: 0, following: true })
  const [paused, setPaused] = useState(() => !(positions.get(identity)?.following ?? true))
  const place = () => {
    const node = scroller.current
    if (!node || !node.clientHeight) return
    node.scrollTop = position.current.following ? node.scrollHeight : position.current.top
    position.current.top = node.scrollTop
  }
  useLayoutEffect(place, [version])
  useLayoutEffect(() => {
    const node = scroller.current
    if (!node) return
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    observer?.observe(node)
    return () => { positions.set(identity, { ...position.current }); observer?.disconnect() }
    // The feed is keyed by its research; mutable position is read at cleanup.
  }, [identity, positions])
  return <>
    <div className="chat-body" ref={scroller} role="group" aria-label="对话与操作记录" tabIndex={0}
      onScroll={event => {
        const node = event.currentTarget
        if (!node.clientHeight) return
        const following = node.scrollHeight - node.scrollTop - node.clientHeight < 40
        position.current = { top: node.scrollTop, following }
        positions.set(identity, { ...position.current })
        setPaused(!following)
      }}>
      {children}
    </div>
    {paused && <button type="button" className="feed-latest" onClick={() => {
      position.current.following = true
      place()
      positions.set(identity, { ...position.current })
      setPaused(false)
    }}>回到最新 ↓</button>}
  </>
}
