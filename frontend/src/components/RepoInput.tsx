import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { ProgressMsg } from '../api'

type Props = {
  log: ProgressMsg[]
  ingesting: boolean
  current: { project?: string; url?: string; counts?: Record<string, number> }
  files: string[]
  changed: string[]
  openFile: string | null
  onOpen: (path: string) => void
}

type Tree = { [name: string]: Tree | null }

function buildTree(paths: string[]): Tree {
  const root: Tree = {}
  for (const p of paths) {
    const parts = p.split('/')
    let cur = root
    parts.forEach((part, i) => {
      if (i === parts.length - 1) cur[part] = null
      else cur = (cur[part] ??= {}) as Tree
    })
  }
  return root
}

function TreeView({ tree, prefix, changed, openFile, onOpen }: { tree: Tree; prefix: string; changed: Set<string>; openFile: string | null; onOpen: (p: string) => void }) {
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const entries = Object.entries(tree).sort(([a, av], [b, bv]) => (av === null ? 1 : 0) - (bv === null ? 1 : 0) || a.localeCompare(b))
  return (
    <ul className="pl-2">
      {entries.map(([name, sub]) => {
        const path = prefix + name
        if (sub === null)
          return (
            <li key={path}>
              <button
                onClick={() => onOpen(path)}
                className={`w-full truncate rounded px-1 text-left hover:bg-slate-800 ${openFile === path ? 'bg-slate-800 text-white' : ''} ${changed.has(path) ? 'text-amber-400' : 'text-slate-300'}`}
              >
                {name}{changed.has(path) ? ' ●' : ''}
              </button>
            </li>
          )
        const isClosed = closed.has(path)
        return (
          <li key={path}>
            <button
              onClick={() => setClosed((s) => { const n = new Set(s); if (isClosed) n.delete(path); else n.add(path); return n })}
              className="w-full rounded px-1 text-left text-slate-400 hover:bg-slate-800"
            >
              {isClosed ? '▸' : '▾'} {name}
            </button>
            {!isClosed && <TreeView tree={sub} prefix={path + '/'} changed={changed} openFile={openFile} onOpen={onOpen} />}
          </li>
        )
      })}
    </ul>
  )
}

export default function RepoInput({ log, ingesting, current, files, changed, openFile, onOpen }: Props) {
  const [url, setUrl] = useState('')
  const [err, setErr] = useState('')
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight) }, [log])

  const ingest = async () => {
    setErr('')
    try { await api.post('/api/ingest', { repo_url: url }) } catch (e) { setErr(String(e)) }
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3 text-xs">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Repository</h2>
      <input
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://github.com/org/repo or local path"
        className="rounded border border-slate-700 bg-slate-900 p-2 outline-none focus:border-blue-500"
      />
      <button onClick={ingest} disabled={ingesting || !url.trim()} className="rounded bg-blue-600 py-1.5 font-semibold hover:bg-blue-500 disabled:opacity-40">
        {ingesting ? 'Ingesting…' : 'Ingest'}
      </button>
      {err && <div className="text-red-400">{err}</div>}
      <div ref={logRef} className="h-32 shrink-0 overflow-auto rounded bg-slate-900 p-2 font-mono text-[10px] text-slate-400 scroll-thin">
        {log.filter((m) => m.msg).map((m, i) => <div key={i}>{m.msg}</div>)}
      </div>
      {current.counts && (
        <div className="flex flex-wrap gap-1 text-[10px] text-slate-400">
          {Object.entries(current.counts).sort().map(([k, v]) => <span key={k} className="rounded bg-slate-800 px-1">{k} {v}</span>)}
        </div>
      )}
      <div className="mt-1 font-semibold text-slate-400">{current.project ?? 'Files'}</div>
      <div className="min-h-0 flex-1 overflow-auto font-mono scroll-thin">
        <TreeView tree={buildTree(files)} prefix="" changed={new Set(changed)} openFile={openFile} onOpen={onOpen} />
      </div>
    </div>
  )
}
