import type NVL from '@neo4j-nvl/base'
import type { Node, Relationship } from '@neo4j-nvl/base'
import { InteractiveNvlWrapper } from '@neo4j-nvl/react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { LABEL_COLORS, nodeCaption } from '../api'
import type { GNode, Graph } from '../api'

const SIZES: Record<string, number> = {
  Project: 40, Component: 32, Decision: 26, Bug: 26, Fix: 24, FailedAttempt: 24, TestRun: 22,
  Dependency: 22, File: 16, Commit: 14, Developer: 16,
}
const DIM = '#1e293b'
const relId = (s: string, t: string, d: string) => `${s}|${t}|${d}`

type Props = {
  graph: Graph
  highlight: { nodes: Set<string>; rels: Set<string> } | null
  flash: Set<string>
  focusUid: string | null
  onClearHighlight: () => void
}

export default function GraphView({ graph, highlight, flash, focusUid, onClearHighlight }: Props) {
  const nvlRef = useRef<NVL>(null)
  const [selected, setSelected] = useState<GNode | null>(null)
  const [showNoise, setShowNoise] = useState(true)

  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.uid, n])), [graph])

  const { nodes, rels } = useMemo(() => {
    const keep = graph.nodes.filter(
      (n) => showNoise || !['File', 'Commit', 'Developer'].includes(n.label) || highlight?.nodes.has(n.uid) || flash.has(n.uid),
    )
    const ids = new Set(keep.map((n) => n.uid))
    const nodes: Node[] = keep.map((n) => {
      const lit = !highlight || highlight.nodes.has(n.uid)
      return {
        id: n.uid,
        caption: nodeCaption(n),
        color: lit ? LABEL_COLORS[n.label] ?? '#888' : DIM,
        size: (SIZES[n.label] ?? 18) * (flash.has(n.uid) ? 1.5 : 1),
        selected: n.uid === focusUid || flash.has(n.uid),
      }
    })
    const rels: Relationship[] = graph.rels
      .filter((r) => ids.has(r.source) && ids.has(r.target))
      .map((r) => {
        const id = relId(r.source, r.type, r.target)
        const lit = !!highlight && highlight.rels.has(id)
        return {
          id,
          from: r.source,
          to: r.target,
          caption: lit ? r.type : '',
          color: lit ? '#facc15' : highlight ? '#0f172a' : '#475569',
          width: lit ? 4 : 1,
        }
      })
    return { nodes, rels }
  }, [graph, highlight, flash, focusUid, showNoise])

  useEffect(() => {
    if (focusUid && byId.has(focusUid)) {
      setSelected(byId.get(focusUid)!)
      setTimeout(() => nvlRef.current?.fit([focusUid], {}), 50)
    }
  }, [focusUid, byId])

  useEffect(() => {
    if (highlight && highlight.nodes.size) {
      const ids = [...highlight.nodes].filter((u) => byId.has(u))
      setTimeout(() => nvlRef.current?.fit(ids, {}), 400)
    }
  }, [highlight, byId])

  return (
    <div className="relative h-full w-full bg-slate-950">
      {graph.nodes.length === 0 ? (
        <div className="flex h-full items-center justify-center text-slate-500">Ingest a repo to see its knowledge graph.</div>
      ) : (
        <InteractiveNvlWrapper
          ref={nvlRef}
          nodes={nodes}
          rels={rels}
          nvlOptions={{ initialZoom: 0.7, renderer: 'canvas', disableTelemetry: true }}
          mouseEventCallbacks={{
            onNodeClick: (n: Node) => setSelected(byId.get(n.id) ?? null),
            onCanvasClick: () => setSelected(null),
            onZoomAndPan: true,
            onPan: true,
            onDrag: true,
          }}
        />
      )}

      <div className="pointer-events-none absolute bottom-3 left-3 flex max-w-[60%] flex-wrap gap-1.5 text-[11px]">
        {Object.entries(LABEL_COLORS).map(([l, c]) => (
          <span key={l} className="flex items-center gap-1 rounded bg-slate-900/80 px-1.5 py-0.5">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
            {l}
          </span>
        ))}
      </div>
      <div className="absolute right-3 top-3 flex gap-2 text-xs">
        <label className="flex items-center gap-1 rounded bg-slate-900/80 px-2 py-1">
          <input type="checkbox" checked={showNoise} onChange={(e) => setShowNoise(e.target.checked)} />
          files & commits
        </label>
        {highlight && (
          <button onClick={onClearHighlight} className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700">
            clear highlight
          </button>
        )}
        <button onClick={() => nvlRef.current?.fit(nodes.map((n) => n.id), {})} className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700">
          fit
        </button>
      </div>

      {selected && (
        <div className="absolute bottom-3 right-3 max-h-[60%] w-80 overflow-auto rounded-lg border border-slate-700 bg-slate-900/95 p-3 text-xs shadow-xl scroll-thin">
          <div className="mb-2 flex items-center justify-between">
            <span className="rounded px-1.5 py-0.5 font-semibold text-slate-950" style={{ background: LABEL_COLORS[selected.label] }}>
              {selected.label}
            </span>
            <button onClick={() => setSelected(null)} className="text-slate-400 hover:text-white">✕</button>
          </div>
          <div className="mb-2 font-mono text-[10px] text-slate-400">{selected.uid}</div>
          {Object.entries(selected.props)
            .filter(([k]) => !['uid', 'project'].includes(k))
            .map(([k, v]) => (
              <div key={k} className="mb-1.5">
                <div className="text-[10px] uppercase tracking-wide text-slate-500">{k}</div>
                <div className="whitespace-pre-wrap break-words text-slate-200">
                  {k === 'ts' && typeof v === 'number' ? new Date(v * 1000).toISOString().slice(0, 10) : typeof v === 'object' ? JSON.stringify(v) : String(v)}
                </div>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}
