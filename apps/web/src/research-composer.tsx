import { useRef, useState } from 'react'

export type ComposerCommand = {
  name: string; label: string; scope: string
  options: { value: string; label: string }[]
  choose: (value: string) => void | Promise<void>
}

/** Commands configure the host. They never enter a model prompt. */
export function ResearchComposer({ value, change, send, stop, sending, canSend, locked, commands, status, children }: {
  value: string; change: (value: string) => void; send: () => void; stop?: () => void
  sending: boolean; canSend: boolean; locked: boolean; commands: ComposerCommand[]
  status: string; children?: React.ReactNode
}) {
  const input = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  // Button-opened configuration is temporary; the host keeps its unsent draft.
  const [configuration, setConfiguration] = useState<string | null>(null)
  const text = configuration ?? value
  const [cursor, setCursor] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  const [choosing, setChoosing] = useState(false)
  const [error, setError] = useState('')
  const match = /^\/(\S*)(?:\s+(.*))?$/.exec(text)
  const command = match && match[2] !== undefined ? commands.find(c => c.name === match[1]) : undefined
  const filter = (command ? match?.[2] : match?.[1])?.toLowerCase() ?? ''
  const options = command ? command.options.filter(o => o.label.toLowerCase().includes(filter))
    : commands.filter(c => `${c.name} ${c.label}`.toLowerCase().includes(filter)).map(c => ({value:c.name,label:`/${c.name} · ${c.label}`}))
  const slash = text.startsWith('/')
  const open = slash && !dismissed
  const selected = Math.min(cursor, Math.max(options.length - 1, 0))
  const edit = (next: string) => {
    if (configuration !== null) setConfiguration(next.startsWith('/') ? next : null)
    else change(next)
    setCursor(0); setDismissed(false); setError('')
  }
  const choose = async (option: {value:string;label:string}) => {
    if (locked || choosing) return
    if (!command) {edit(`/${option.value} `); input.current?.focus(); return}
    setChoosing(true)
    try {await command.choose(option.value); edit(''); input.current?.focus()}
    catch (e) {setError(e instanceof Error ? e.message : '配置未改变，请重试。')}
    finally {setChoosing(false)}
  }
  return <div className="research-composer">
    {open && <div className="command-menu">
      <p className="command-scope">{locked ? '当前任务结束后可更改配置' : command?.scope ?? '选择配置 · 不发送给模型'}</p>
      <div role="listbox" id="research-commands" aria-label="研究配置">
        {options.map((option,index)=><button type="button" role="option" id={`research-command-${index}`}
          key={option.value} aria-selected={index===selected} disabled={locked || choosing}
          onMouseDown={e=>e.preventDefault()} onClick={()=>void choose(option)}>{option.label}</button>)}
      </div>
      {!options.length && <p>没有匹配配置。Esc 关闭。</p>}
    </div>}
    <div className="composer-entry">
      <label className="sr-only" htmlFor="strategy-message">消息</label>
      <textarea ref={input} id="strategy-message" rows={2} value={text} maxLength={4000}
        aria-controls={open?'research-commands':undefined} aria-expanded={open}
        aria-activedescendant={open&&options.length?`research-command-${selected}`:undefined}
        placeholder="继续研究… 输入 / 更改配置" onChange={e=>edit(e.target.value)}
        onCompositionStart={()=>{composing.current=true}} onCompositionEnd={()=>{composing.current=false}}
        onKeyDown={e=>{
          if(composing.current || e.nativeEvent.isComposing || e.keyCode===229) return
          if(e.key==='Enter'&&e.shiftKey) return
          if(open && ['ArrowDown','ArrowUp','Enter','Escape'].includes(e.key)) {
            e.preventDefault()
            if(e.key==='Escape') {setDismissed(true); setConfiguration(null)}
            else if(e.key==='ArrowDown') setCursor((selected+1)%Math.max(options.length,1))
            else if(e.key==='ArrowUp') setCursor((selected+options.length-1)%Math.max(options.length,1))
            else if(options[selected]) void choose(options[selected])
            return
          }
          if(e.key==='Enter'&&!e.shiftKey) {e.preventDefault(); if(!slash&&canSend&&!sending) send()}
        }} />
      {stop ? <button className="composer-send" type="button" aria-label="停止生成" title="停止后续执行" onClick={stop}><span aria-hidden="true">■</span></button>
        : <button className="composer-send" type="button" aria-label="发送消息" title="发送 · Enter" disabled={!canSend||sending||slash} onClick={send}><span aria-hidden="true">↑</span></button>}
    </div>
    <div className="composer-bar"><button type="button" className="command-trigger" aria-label="打开配置菜单" onClick={()=>{setConfiguration('/');setCursor(0);setDismissed(false);setError('');input.current?.focus()}}>/</button>
      <span role="status">{status}</span><span className="composer-shortcut">⇧ Enter 换行</span></div>
    {error&&<p role="alert">{error}</p>}
    {children}
  </div>
}
