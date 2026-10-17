const fmt = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 10_000 ? `${Math.round(n / 1000)}K` : n.toLocaleString()

export default function TokenMeter({ repo, sent }: { repo: number; sent: number | null }) {
  return (
    <div className="flex items-baseline gap-3 font-mono">
      <span className="text-slate-400">
        Repo: <span className="text-2xl font-bold text-slate-200">{fmt(repo)}</span> tokens
      </span>
      <span className="text-2xl text-slate-500">→</span>
      <span className="text-slate-400">
        Sent: <span className="text-2xl font-bold text-emerald-400">{sent === null ? '—' : fmt(sent)}</span> tokens
      </span>
      {sent !== null && repo > 0 && (
        <span className="rounded bg-emerald-950 px-2 py-0.5 text-xs text-emerald-300">{((sent / repo) * 100).toFixed(1)}% of repo</span>
      )}
    </div>
  )
}
