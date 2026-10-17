import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, wsUrl } from './api'
import type { AskResponse, Graph, ProgressMsg } from './api'
import AskPanel from './components/AskPanel'
import CommitModal from './components/CommitModal'
import EditorPanel from './components/EditorPanel'
import GraphView from './components/GraphView'
import RepoInput from './components/RepoInput'
import TerminalPanel from './components/TerminalPanel'
import TokenMeter from './components/TokenMeter'

type State = { project?: string; url?: string; repo_dir?: string; counts?: Record<string, number> }

export default function App() {
  const [state, setState] = useState<State>({})
  const [graph, setGraph] = useState<Graph>({ nodes: [], rels: [] })
  const [files, setFiles] = useState<string[]>([])
  const [changed, setChanged] = useState<string[]>([])
  const [log, setLog] = useState<ProgressMsg[]>([])
  const [ingesting, setIngesting] = useState(false)
  const [tab, setTab] = useState<'graph' | 'editor'>('graph')
  const [openFile, setOpenFile] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<{ nodes: Set<string>; rels: Set<string> } | null>(null)
  const [flash, setFlash] = useState<Set<string>>(new Set())
  const [focusUid, setFocusUid] = useState<string | null>(null)
  const [answer, setAnswer] = useState<AskResponse | null>(null)
  const [models, setModels] = useState<{ id: string; model: string; available: boolean }[]>([])
  const [model, setModel] = useState('groq')
  const [showTerm, setShowTerm] = useState(true)
  const [showCommit, setShowCommit] = useState(false)
  const [toast, setToast] = useState('')

  const refresh = useCallback(async () => {
    api.get<{ id: string; model: string; available: boolean }[]>('/api/models').then((m) => {
      setModels(m)
      setModel((cur) => (m.find((x) => x.id === cur && x.available) ? cur : m.find((x) => x.available)?.id ?? cur))
    }).catch(() => {})
    const s = await api.get<State>('/api/state')
    setState(s)
    if (!s.project) return
    const [g, f] = await Promise.all([api.get<Graph>('/api/graph'), api.get<{ files: string[]; changed: string[] }>('/api/files')])
    setGraph(g)
    setFiles(f.files)
    setChanged(f.changed)
  }, [])

  const refreshFiles = useCallback(async () => {
    const f = await api.get<{ files: string[]; changed: string[] }>('/api/files')
    setFiles(f.files)
    setChanged(f.changed)
  }, [])

  useEffect(() => {
    refresh()
    const retry = setInterval(() => { if (!models.length) refresh() }, 4000) // backend may still be starting
    return () => clearInterval(retry)
  }, [refresh, models.length])

  useEffect(() => {
    let ws: WebSocket
    let stop = false
    const connect = () => {
      ws = new WebSocket(wsUrl('/ws/progress'))
      ws.onmessage = (e) => {
        const m: ProgressMsg = JSON.parse(e.data)
        setLog((l) => (m.type === 'start' ? [m] : [...l.slice(-300), m]))
        if (m.type === 'start') {
          setIngesting(true)
          setGraph({ nodes: [], rels: [] }); setFiles([]); setChanged([]); setAnswer(null)
          setHighlight(null); setOpenFile(null); setState({ project: m.project })
        }
        if (m.type === 'done' || m.type === 'error') { setIngesting(false); refresh() }
        if (m.type === 'nodes' && m.uids) {
          refresh()
          setFlash(new Set(m.uids))
          setTimeout(() => setFlash(new Set()), 6000)
        }
        if (m.type === 'testrun') {
          setToast(`${m.passed ? '✓ Tests passed' : '✗ Tests failed'}: ${m.command} — recorded as TestRun`)
          setTimeout(() => setToast(''), 5000)
          refreshFiles()
        }
      }
      ws.onclose = () => { if (!stop) setTimeout(connect, 1500) }
    }
    connect()
    return () => { stop = true; ws.close() }
  }, [refresh, refreshFiles])

  const nodeById = useMemo(() => new Map(graph.nodes.map((n) => [n.uid, n])), [graph])

  const onAnswer = (r: AskResponse) => {
    setAnswer(r)
    const nodes = new Set<string>(r.package_uids)
    const rels = new Set<string>()
    for (const p of r.evidence_paths) {
      p.nodes.forEach((n) => nodes.add(n))
      p.rels.forEach(([s, t, d]) => rels.add(`${s}|${t}|${d}`))
    }
    setHighlight({ nodes, rels })
    setTab('graph')
  }

  const onCite = (uid: string) => { setTab('graph'); setFocusUid(null); setTimeout(() => setFocusUid(uid), 0) }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-6 border-b border-slate-800 px-4 py-2">
        <div>
          <div className="text-lg font-bold tracking-tight">Repo<span className="text-blue-500">Memory</span></div>
          <div className="text-[11px] text-slate-500">The LLM never reads your repo. It reads the 15 nodes that explain your bug.</div>
        </div>
        <div className="flex-1" />
        <TokenMeter repo={answer?.repo_tokens_estimate ?? 0} sent={answer?.tokens_sent ?? null} />
        <button
          onClick={() => setShowCommit(true)}
          disabled={!changed.length}
          className="rounded bg-emerald-700 px-3 py-1.5 text-sm font-semibold hover:bg-emerald-600 disabled:opacity-40"
        >
          Commit + record decision{changed.length ? ` (${changed.length})` : ''}
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="w-72 shrink-0 border-r border-slate-800">
          <RepoInput log={log} ingesting={ingesting} current={state} files={files} changed={changed} openFile={openFile}
            onOpen={(p) => { setOpenFile(p); setTab('editor') }} />
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex gap-1 border-b border-slate-800 px-2 pt-1 text-sm">
            {(['graph', 'editor'] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`rounded-t px-3 py-1 capitalize ${tab === t ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-slate-200'}`}>
                {t}
              </button>
            ))}
            <div className="flex-1" />
            <button onClick={() => setShowTerm(!showTerm)} className="px-2 text-xs text-slate-400 hover:text-white">
              {showTerm ? '▾ hide terminal' : '▴ show terminal'}
            </button>
          </div>
          <div className="relative min-h-0 flex-1">
            <div className={`absolute inset-0 ${tab === 'graph' ? '' : 'invisible'}`}>
              <GraphView graph={graph} highlight={highlight} flash={flash} focusUid={focusUid} onClearHighlight={() => setHighlight(null)} />
            </div>
            <div className={`absolute inset-0 ${tab === 'editor' ? '' : 'invisible'}`}>
              <EditorPanel path={openFile} onSaved={refreshFiles} />
            </div>
          </div>
          {showTerm && (
            <div className="h-64 shrink-0 border-t border-slate-800 bg-slate-950">
              {state.project ? <TerminalPanel sessionKey={state.repo_dir ?? ''} /> : <div className="p-3 text-xs text-slate-500">Terminal opens in the repo after ingestion.</div>}
            </div>
          )}
        </main>

        <aside className="w-[440px] shrink-0 border-l border-slate-800">
          <AskPanel model={model} setModel={setModel} models={models} nodeById={nodeById} onAnswer={onAnswer} onCite={onCite} disabled={!state.project || ingesting} />
        </aside>
      </div>

      {showCommit && (
        <CommitModal model={model} onClose={() => setShowCommit(false)}
          onDone={(r) => {
            setShowCommit(false)
            setToast(`Committed ${r.sha.slice(0, 7)} — decision recorded${r.verified ? ', fix VERIFIED by passing test run' : ''}`)
            setTimeout(() => setToast(''), 6000)
          }} />
      )}
      {toast && <div className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg bg-slate-800 px-4 py-2 text-sm shadow-xl">{toast}</div>}
    </div>
  )
}
