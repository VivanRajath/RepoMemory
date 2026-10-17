import { useState } from 'react'
import { api, LABEL_COLORS, nodeCaption } from '../api'
import type { AskResponse, GNode } from '../api'

type Props = {
  model: string
  setModel: (m: string) => void
  models: { id: string; model: string; available: boolean }[]
  nodeById: Map<string, GNode>
  onAnswer: (r: AskResponse) => void
  onCite: (uid: string) => void
  disabled: boolean
}

const CITE_RE = /\[node:([A-Za-z]+:[0-9a-f]{8})\]/g

function AnswerText({ text, nodeById, onCite }: { text: string; nodeById: Map<string, GNode>; onCite: (u: string) => void }) {
  const parts: React.ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(CITE_RE)) {
    parts.push(text.slice(last, m.index))
    const uid = m[1]
    const n = nodeById.get(uid)
    parts.push(
      <button
        key={m.index}
        onClick={() => onCite(uid)}
        title={uid}
        className="mx-0.5 inline-flex max-w-[180px] items-center gap-1 truncate rounded px-1.5 align-baseline text-[11px] font-medium text-slate-950 hover:brightness-125"
        style={{ background: LABEL_COLORS[n?.label ?? ''] ?? '#94a3b8' }}
      >
        {n ? nodeCaption(n) : uid}
      </button>,
    )
    last = m.index! + m[0].length
  }
  parts.push(text.slice(last))
  return <div className="whitespace-pre-wrap text-sm leading-relaxed">{parts}</div>
}

export default function AskPanel({ model, setModel, models, nodeById, onAnswer, onCite, disabled }: Props) {
  const [question, setQuestion] = useState('')
  const [trace, setTrace] = useState('')
  const [showTrace, setShowTrace] = useState(false)
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState<AskResponse | null>(null)
  const [err, setErr] = useState('')

  const ask = async () => {
    setBusy(true)
    setErr('')
    try {
      const r = await api.post<AskResponse>('/api/ask', { question, stack_trace: trace, model })
      setRes(r)
      onAnswer(r)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3 scroll-thin">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Ask the project</h2>
      <textarea
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ask() }}
        rows={3}
        placeholder="Ask about this repo, e.g. why did we change auth, and could it cause this 401 on /api/orders?"
        className="w-full rounded border border-slate-700 bg-slate-900 p-2 text-sm outline-none focus:border-blue-500"
      />
      <button onClick={() => setShowTrace(!showTrace)} className="self-start text-xs text-slate-400 hover:text-slate-200">
        {showTrace ? '▾' : '▸'} stack trace / test output (optional)
      </button>
      {showTrace && (
        <textarea
          value={trace}
          onChange={(e) => setTrace(e.target.value)}
          rows={6}
          placeholder="Paste a stack trace or failing test output…"
          className="w-full rounded border border-slate-700 bg-slate-900 p-2 font-mono text-xs outline-none focus:border-blue-500"
        />
      )}
      <div className="flex gap-2">
        <select value={model} onChange={(e) => setModel(e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-2 text-sm">
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id === 'groq' ? 'Groq' : m.id === 'claude' ? 'Claude' : 'OpenAI'}{m.available ? '' : ' (no key)'}
            </option>
          ))}
        </select>
        <button
          onClick={ask}
          disabled={busy || disabled || !question.trim()}
          className="flex-1 rounded bg-blue-600 py-1.5 text-sm font-semibold hover:bg-blue-500 disabled:opacity-40"
        >
          {busy ? 'Retrieving…' : 'Ask'}
        </button>
      </div>
      {err && <div className="rounded bg-red-950 p-2 text-xs text-red-300">{err}</div>}

      {res && (
        <>
          <div className="rounded-lg border border-slate-700 bg-slate-900 p-3">
            <AnswerText text={res.answer} nodeById={nodeById} onCite={onCite} />
            {res.invalid_citations.length > 0 && (
              <div className="mt-2 text-xs text-amber-400">⚠ {res.invalid_citations.length} citation(s) not in the package were flagged.</div>
            )}
            <div className="mt-2 text-[10px] text-slate-500">
              {res.model} · {res.citations.length} citations, all verified against the package
            </div>
          </div>
          <div className="text-xs text-slate-400">
            Retrieval: {res.candidates_considered} graph nodes scored deterministically → {res.package_uids.length} sent.
            <div className="mt-1 flex flex-wrap gap-1">
              {res.included_sections.map((s) => (
                <span key={s} className="rounded bg-emerald-950 px-1.5 text-emerald-300">{s}{res.trimmed[s] ? ` (−${res.trimmed[s]})` : ''}</span>
              ))}
              {res.omitted_sections.map((s) => (
                <span key={s} className="rounded bg-slate-800 px-1.5 text-slate-500 line-through">{s}</span>
              ))}
            </div>
          </div>
          <details className="rounded border border-slate-800 bg-slate-900/60">
            <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-slate-300">
              What the LLM saw ({res.tokens_sent.toLocaleString()} tokens)
            </summary>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap p-3 font-mono text-[11px] text-slate-300 scroll-thin">{res.package_text}</pre>
          </details>
        </>
      )}
    </div>
  )
}
