import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Badge } from './CheckTable.jsx'

/** Live log for one job: replays stored lines then follows via SSE. */
export default function JobLog({ cluster, jobId, onDone, compact }) {
  const [lines, setLines] = useState([])
  const [status, setStatus] = useState('running')
  const [job, setJob] = useState(null)
  const pre = useRef(null)

  useEffect(() => {
    if (!jobId) return
    setLines([]); setStatus('running')
    api.get(`/api/clusters/${cluster}/jobs/${jobId}`).then(setJob).catch(() => {})
    const es = new EventSource(`/api/clusters/${cluster}/jobs/${jobId}/stream`)
    es.addEventListener('log', e => {
      const l = JSON.parse(e.data)
      setLines(prev => prev.length > 5000 ? [...prev.slice(-4000), l] : [...prev, l])
    })
    es.addEventListener('done', e => {
      const d = JSON.parse(e.data)
      setStatus(d.status); es.close()
      api.get(`/api/clusters/${cluster}/jobs/${jobId}`).then(setJob).catch(() => {})
      onDone && onDone(d.status)
    })
    es.onerror = () => { /* SSE reconnects automatically */ }
    return () => es.close()
  }, [cluster, jobId])

  useEffect(() => { if (pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [lines])

  const cancel = () => api.post(`/api/clusters/${cluster}/jobs/${jobId}/cancel`).catch(() => {})
  if (!jobId) return null
  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <b>Job #{jobId}</b> {job && <span className="muted">{job.kind} · started {job.started}Z</span>}
        <Badge s={status} />
        <span className="spacer" />
        {status === 'running' && <button onClick={cancel}>Cancel</button>}
      </div>
      <pre className="log" ref={pre} style={compact ? { maxHeight: 260 } : {}}>
        {lines.map(l => `${l.ts.slice(11, 19)}  ${l.line}\n`)}
      </pre>
    </div>
  )
}
