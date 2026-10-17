import Editor from '@monaco-editor/react'
import type { OnMount } from '@monaco-editor/react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

const LANGS: Record<string, string> = {
  py: 'python', ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript', mjs: 'javascript',
  json: 'json', md: 'markdown', toml: 'ini', yml: 'yaml', yaml: 'yaml', css: 'css', html: 'html', sh: 'shell', txt: 'plaintext',
}

export default function EditorPanel({ path, onSaved }: { path: string | null; onSaved: () => void }) {
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState('')
  const contentRef = useRef('')
  const pathRef = useRef(path)

  useEffect(() => {
    pathRef.current = path
    if (!path) return
    api.get<{ content: string }>(`/api/file?path=${encodeURIComponent(path)}`).then((r) => {
      setContent(r.content)
      contentRef.current = r.content
      setDirty(false)
      setStatus('')
    })
  }, [path])

  const save = async () => {
    if (!pathRef.current) return
    await api.put(`/api/file?path=${encodeURIComponent(pathRef.current)}`, { content: contentRef.current })
    setDirty(false)
    setStatus(`saved ${new Date().toLocaleTimeString()}`)
    onSaved()
  }
  const saveRef = useRef(save)
  saveRef.current = save

  const onMount: OnMount = (editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
  }

  if (!path) return <div className="flex h-full items-center justify-center text-slate-500">Open a file from the tree.</div>
  const ext = path.split('.').pop() ?? ''
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-slate-800 px-3 py-1 font-mono text-xs">
        <span className="text-slate-300">{path}{dirty ? ' ●' : ''}</span>
        <span className="text-slate-500">{status}</span>
        <button onClick={save} className="ml-auto rounded bg-slate-800 px-2 py-0.5 hover:bg-slate-700">Save (Ctrl+S)</button>
      </div>
      <Editor
        height="100%"
        theme="vs-dark"
        path={path}
        language={LANGS[ext] ?? 'plaintext'}
        value={content}
        onMount={onMount}
        onChange={(v) => { contentRef.current = v ?? ''; setDirty(true) }}
        options={{ fontSize: 13, minimap: { enabled: false }, scrollBeyondLastLine: false }}
      />
    </div>
  )
}
