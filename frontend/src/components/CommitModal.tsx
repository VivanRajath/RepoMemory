import { useEffect, useState } from 'react'
import { api } from '../api'

type Ctx = {
  diff: string
  changed: string[]
  decisions: { uid: string; summary: string; ts: number }[]
  open_bugs: { uid: string; title: string; ts: number; on_touched: boolean }[]
}

export default function CommitModal({ model, onClose, onDone }: { model: string; onClose: () => void; onDone: (r: { sha: string; verified: boolean }) => void }) {
  const [ctx, setCtx] = useState<Ctx | null>(null)
  const [summary, setSummary] = useState('')
  const [reason, setReason] = useState('')
  const [draft, setDraft] = useState<{ summary: string; reason: string } | null>(null)
  const [supersedes, setSupersedes] = useState<Set<string>>(new Set())
  const [bug, setBug] = useState('')
  const [newBug, setNewBug] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    api.get<Ctx>('/api/commit_context').then((c) => {
      setCtx(c)
      const first = c.open_bugs.find((b) => b.on_touched) ?? c.open_bugs.find((b) => b.title.startsWith('Failing:'))
      if (first) setBug(first.uid)
    }).catch((e) => setErr(String(e)))
  }, [])

  const draftWithAI = async () => {
    setBusy('draft'); setErr('')
    try {
      const d = await api.post<{ summary: string; reason: string }>('/api/draft', { model })
      setDraft(d); setSummary(d.summary); setReason(d.reason)
    } catch (e) { setErr(String(e)) } finally { setBusy('') }
  }

  const submit = async () => {
    setBusy('commit'); setErr('')
    const source = draft && draft.summary === summary && draft.reason === reason ? 'llm_confirmed' : 'human'
    try {
      const r = await api.post<{ sha: string; verified: boolean }>('/api/commit', {
        summary, reason, source, supersedes: [...supersedes],
        fixes_bug: bug && bug !== '__new' ? bug : null,
        new_bug_title: bug === '__new' ? newBug : null,
      })
      onDone(r)
    } catch (e) { setErr(String(e)); setBusy('') }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6" onClick={onClose}>
      <div className="flex max-h-full w-full max-w-4xl flex-col gap-3 overflow-auto rounded-xl border border-slate-700 bg-slate-900 p-5 text-sm scroll-thin" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Commit + record decision</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-white">✕</button>
        </div>
        {!ctx ? <div className="text-slate-400">Loading…</div> : (
          <>
            <div className="text-xs text-slate-400">Changed: {ctx.changed.join(', ') || 'nothing'}</div>
            <pre className="max-h-56 overflow-auto rounded bg-slate-950 p-2 font-mono text-[11px] scroll-thin">
              {ctx.diff.split('\n').map((l, i) => (
                <div key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'text-emerald-400' : l.startsWith('-') && !l.startsWith('---') ? 'text-red-400' : l.startsWith('@@') ? 'text-sky-400' : 'text-slate-400'}>{l || ' '}</div>
              ))}
            </pre>
            <div className="flex items-center gap-2">
              <button onClick={draftWithAI} disabled={!!busy} className="rounded bg-violet-700 px-3 py-1.5 font-semibold hover:bg-violet-600 disabled:opacity-40">
                {busy === 'draft' ? 'Drafting…' : '✨ Draft reason with AI'}
              </button>
              <span className="text-xs text-slate-500">
                sends the diff + the retrieval package for the touched components ·{' '}
                source: <b>{draft && draft.summary === summary && draft.reason === reason ? 'llm_confirmed' : 'human'}</b>
              </span>
            </div>
            <input value={summary} onChange={(e) => setSummary(e.target.value)} placeholder="Summary (becomes the commit subject + Decision)" className="rounded border border-slate-700 bg-slate-950 p-2 outline-none focus:border-blue-500" />
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} placeholder="Reason — why this change?" className="rounded border border-slate-700 bg-slate-950 p-2 outline-none focus:border-blue-500" />

            {ctx.decisions.length > 0 && (
              <div>
                <div className="mb-1 text-xs font-semibold text-slate-300">Does this replace any of these decisions?</div>
                {ctx.decisions.map((d) => (
                  <label key={d.uid} className="flex items-start gap-2 text-xs text-slate-300">
                    <input type="checkbox" className="mt-0.5" checked={supersedes.has(d.uid)}
                      onChange={(e) => setSupersedes((s) => { const n = new Set(s); if (e.target.checked) n.add(d.uid); else n.delete(d.uid); return n })} />
                    <span>{d.summary} <span className="text-slate-500">({new Date(d.ts * 1000).toISOString().slice(0, 10)})</span></span>
                  </label>
                ))}
              </div>
            )}
            <div>
              <div className="mb-1 text-xs font-semibold text-slate-300">This fixes bug:</div>
              <select value={bug} onChange={(e) => setBug(e.target.value)} className="w-full rounded border border-slate-700 bg-slate-950 p-2 text-xs">
                <option value="">— none —</option>
                {ctx.open_bugs.map((b) => <option key={b.uid} value={b.uid}>{b.on_touched ? '● ' : ''}{b.title}</option>)}
                <option value="__new">+ new bug…</option>
              </select>
              {bug === '__new' && <input value={newBug} onChange={(e) => setNewBug(e.target.value)} placeholder="Bug title" className="mt-1 w-full rounded border border-slate-700 bg-slate-950 p-2 text-xs" />}
            </div>
            {err && <div className="rounded bg-red-950 p-2 text-xs text-red-300">{err}</div>}
            <button onClick={submit} disabled={!!busy || !summary.trim() || ctx.changed.length === 0} className="rounded bg-emerald-600 py-2 font-semibold hover:bg-emerald-500 disabled:opacity-40">
              {busy === 'commit' ? 'Committing…' : 'Commit & update graph'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
