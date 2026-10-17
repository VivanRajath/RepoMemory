import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import { useEffect, useRef } from 'react'
import { wsUrl } from '../api'

export default function TerminalPanel({ sessionKey }: { sessionKey: string }) {
  const host = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!host.current) return
    const term = new Terminal({ fontSize: 13, fontFamily: 'Consolas, Menlo, monospace', theme: { background: '#020617' }, cursorBlink: true })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    const ws = new WebSocket(wsUrl('/ws/terminal'))
    const sendSize = () => {
      try { fit.fit() } catch { /* hidden */ }
      if (ws.readyState === WebSocket.OPEN) ws.send(`\x00resize:${term.cols}x${term.rows}`)
    }
    ws.onopen = sendSize
    ws.onmessage = (e) => term.write(e.data)
    ws.onclose = () => term.write('\r\n\x1b[90m[terminal closed]\x1b[0m\r\n')
    const sub = term.onData((d) => { if (ws.readyState === WebSocket.OPEN) ws.send(d) })
    const ro = new ResizeObserver(sendSize)
    ro.observe(host.current)
    return () => { ro.disconnect(); sub.dispose(); ws.close(); term.dispose() }
  }, [sessionKey])

  return <div ref={host} className="h-full w-full overflow-hidden px-2 py-1" />
}
