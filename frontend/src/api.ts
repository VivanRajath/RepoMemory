export type GNode = { uid: string; label: string; props: Record<string, any> }
export type GRel = { source: string; type: string; target: string; props: Record<string, any> }
export type Graph = { nodes: GNode[]; rels: GRel[] }
export type EvidencePath = { nodes: string[]; rels: [string, string, string][] }

export type AskResponse = {
  answer: string
  citations: string[]
  invalid_citations: string[]
  model: string
  evidence_paths: EvidencePath[]
  package_text: string
  tokens_sent: number
  repo_tokens_estimate: number
  included_sections: string[]
  omitted_sections: string[]
  trimmed: Record<string, number>
  package_uids: string[]
  candidates_considered: number
}

export type ProgressMsg = { type: string; msg?: string; uids?: string[]; project?: string; passed?: boolean; command?: string; ts: number }

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail ?? detail } catch { /* not json */ }
    throw new Error(detail)
  }
  return r.json()
}

export const api = {
  get: <T,>(url: string) => req<T>('GET', url),
  post: <T,>(url: string, body?: unknown) => req<T>('POST', url, body ?? {}),
  put: <T,>(url: string, body?: unknown) => req<T>('PUT', url, body),
}

export const wsUrl = (path: string) => `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`

export const LABEL_COLORS: Record<string, string> = {
  Project: '#94a3b8',
  Component: '#3b82f6',
  File: '#64748b',
  Dependency: '#a855f7',
  Commit: '#78716c',
  Developer: '#14b8a6',
  Decision: '#22c55e',
  Bug: '#ef4444',
  Fix: '#10b981',
  FailedAttempt: '#f97316',
  TestRun: '#eab308',
}

export function nodeCaption(n: GNode): string {
  const p = n.props
  const cut = (s: string | undefined, k = 34) => (s && s.length > k ? s.slice(0, k - 1) + '…' : s ?? '')
  switch (n.label) {
    case 'Project': return p.name
    case 'Component': return p.path || '(root)'
    case 'File': return (p.path as string)?.split('/').pop() ?? ''
    case 'Dependency': return `${p.name}${p.current_version ? ' ' + p.current_version : ''}`
    case 'Commit': return `${p.short} ${cut(p.msg?.split('\n')[0], 26)}`
    case 'Developer': return p.name
    case 'Decision': return cut(p.summary)
    case 'Bug': return cut(p.title)
    case 'Fix': return (p.verified ? '✓ ' : '') + cut(p.summary)
    case 'FailedAttempt': return '✗ ' + cut(p.what)
    case 'TestRun': return `${p.passed ? '✓' : '✗'} ${cut(p.command, 26)}`
    default: return n.uid
  }
}
