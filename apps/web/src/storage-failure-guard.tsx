import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'

// A confirmed leave applies to every uncached input on this page. The bypass exists
// only during the synchronous replay, so multiple guards cannot prompt each other.
let replayedAnchor: HTMLAnchorElement | null = null

/**
 * One cancellable, keyboard-operable leave confirmation for a page whose
 * unsaved input could not be retained in sessionStorage. Nothing navigates
 * before the user decides; confirming replays the clicked link exactly once so
 * React Router keeps its own query, state and replace semantics. The native
 * beforeunload prompt remains as the last-resort loss guard.
 */
export function StorageFailureGuard({ active, message }: { active: boolean; message: string }) {
  const [pending, setPending] = useState<HTMLAnchorElement | null>(null)
  const pendingAnchor = useRef<HTMLAnchorElement | null>(null)
  const dialog = useRef<HTMLDivElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const messageId = useId()

  useEffect(() => {
    if (!active) return
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    const leave = (event: MouseEvent) => {
      if (event.defaultPrevented) return
      if (!(event.target instanceof Element)) return
      const anchor = event.target.closest('a[href]')
      if (!(anchor instanceof HTMLAnchorElement)) return
      // Leaving is a plain, unmodified primary click on a same-window app link.
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      if (anchor.hasAttribute('download')) return
      const target = anchor.getAttribute('target')
      if (target !== null && target !== '_self') return
      let url: URL
      try { url = new URL(anchor.href, window.location.href) } catch { return }
      if (url.origin !== window.location.origin) return
      // Same-origin API paths serve attachments, not application pages.
      if (url.pathname.startsWith('/api/')) return
      // A same-document hash link does not leave the page; keep its existing behavior.
      if (url.pathname === window.location.pathname && url.search === window.location.search) return
      // One synchronous bypass for the click replay below, never a lasting exemption.
      if (replayedAnchor === anchor) return
      event.preventDefault(); event.stopPropagation()
      // A second click behind the dialog must not replace the chosen destination.
      if (pendingAnchor.current !== null) return
      pendingAnchor.current = anchor
      setPending(anchor)
    }
    window.addEventListener('beforeunload', unload)
    document.addEventListener('click', leave, true)
    return () => {
      window.removeEventListener('beforeunload', unload)
      document.removeEventListener('click', leave, true)
      // A guard that becomes inactive, or unmounts, leaves no open confirmation.
      pendingAnchor.current = null
      setPending(null)
    }
  }, [active])

  useEffect(() => { if (pending) cancelButton.current?.focus() }, [pending])

  const close = () => {
    const anchor = pendingAnchor.current
    pendingAnchor.current = null
    setPending(null)
    if (anchor?.isConnected) anchor.focus()
  }

  const confirmLeave = () => {
    const anchor = pendingAnchor.current
    pendingAnchor.current = null
    setPending(null)
    // A detached link is closed rather than navigating through a stale target.
    if (!anchor || !anchor.isConnected) return
    replayedAnchor = anchor
    try { anchor.click() } finally {
      if (replayedAnchor === anchor) replayedAnchor = null
    }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); return }
    if (event.key !== 'Tab') return
    const nodes = dialog.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    )
    if (!nodes || nodes.length === 0) return
    const first = nodes[0]
    const last = nodes[nodes.length - 1]
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }

  if (!active || !pending) return null
  return <div className="preview-backdrop">
    <div ref={dialog} className="preview-dialog" role="dialog" aria-modal="true"
      aria-labelledby={titleId} aria-describedby={messageId} tabIndex={-1} onKeyDown={onKeyDown}>
      <h2 id={titleId}>离开此页？</h2>
      <p id={messageId}>{message}</p>
      <p className="muted">继续编辑会留在本页并保留输入；放弃并离开会丢弃本页未能缓冲的输入，只导航到你刚才点击的链接。</p>
      <div className="actions">
        <button ref={cancelButton} type="button" onClick={close}>继续编辑</button>
        <button type="button" onClick={confirmLeave}>放弃并离开</button>
      </div>
    </div>
  </div>
}
