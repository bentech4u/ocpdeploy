import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert, RadioCards } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner, fmtDate } from '../components/ops.js'

function PodLogs({ name, setErr }) {
  const [nss, setNss] = useState([])
  const [ns, setNs] = useState('')
  const [pods, setPods] = useState([])
  const [pod, setPod] = useState('')
  const [container, setContainer] = useState('')
  const [tail, setTail] = useState('500')
  const [previous, setPrevious] = useState(false)
  const [text, setText] = useState(null)
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.get(`/api/clusters/${name}/logs/namespaces`).then(setNss).catch(e => setErr(e.message)) }, [name])
  useEffect(() => { setPods([]); setPod(''); if (ns) api.get(`/api/clusters/${name}/logs/pods?ns=${encodeURIComponent(ns)}`).then(setPods).catch(e => setErr(e.message)) }, [ns])
  const cur = pods.find(x => x.name === pod)
  useEffect(() => { setContainer(cur?.containers?.[cur.containers.length - 1] || '') }, [pod])
  const load = async () => {
    setErr(''); setBusy(true)
    try { setText(await api.get(`/api/clusters/${name}/logs/pod?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}&container=${encodeURIComponent(container)}&tail=${tail}&previous=${previous}`)) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const shown = useMemo(() => {
    if (text == null) return []
    const lines = String(text).split('\n')
    return filter ? lines.filter(l => l.toLowerCase().includes(filter.toLowerCase())) : lines
  }, [text, filter])
  return (
    <div>
      <div className="grid4">
        <Field label="Namespace"><Select value={ns} onChange={setNs} placeholder="select…" options={nss} /></Field>
        <Field label="Pod"><Select value={pod} onChange={setPod} placeholder={ns ? 'select…' : '—'} options={pods.map(x => ({ value: x.name, label: `${x.name} (${x.reason || x.phase}${x.restarts ? `, ${x.restarts} restarts` : ''})` }))} /></Field>
        <Field label="Container"><Select value={container} onChange={setContainer} options={cur?.containers || []} /></Field>
        <Field label="Lines"><Select value={tail} onChange={setTail} options={['200', '500', '2000', '10000']} /></Field>
      </div>
      <div className="toolbar">
        <label className="row help"><input type="checkbox" checked={previous} onChange={e => setPrevious(e.target.checked)} /> previous container (after a crash)</label>
        <Text value={filter} onChange={setFilter} placeholder="filter lines…" style={{ maxWidth: 260 }} />
        <span className="spacer" />
        <button className="primary" onClick={load} disabled={!pod || busy}>{busy ? 'Loading…' : text == null ? 'Show log' : 'Reload'}</button>
      </div>
      {text != null && <><p className="help">{shown.length} line(s){filter ? ' matching' : ''}</p><pre className="podlog">{shown.join('\n') || '(empty)'}</pre></>}
    </div>
  )
}

function Events({ name, setErr }) {
  const [ns, setNs] = useState('')
  const [nss, setNss] = useState([])
  const [warn, setWarn] = useState(true)
  const [q, setQ] = useState('')
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.get(`/api/clusters/${name}/logs/namespaces`).then(setNss).catch(() => {}) }, [name])
  const load = async () => {
    setErr(''); setBusy(true)
    try { setRows(await api.get(`/api/clusters/${name}/logs/events?ns=${encodeURIComponent(ns)}&warnings=${warn}&q=${encodeURIComponent(q)}`)) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  useEffect(() => { load() }, [])
  return (
    <div>
      <div className="toolbar">
        <Select value={ns} onChange={setNs} placeholder="all namespaces" options={nss} />
        <label className="row help"><input type="checkbox" checked={warn} onChange={e => setWarn(e.target.checked)} /> warnings only</label>
        <Text value={q} onChange={setQ} placeholder="search reason, object or message…" style={{ maxWidth: 320 }} />
        <button className="primary" onClick={load} disabled={busy}>{busy ? 'Searching…' : 'Search'}</button>
      </div>
      {rows && (rows.length === 0 ? <p className="muted">No matching events (events are kept for about an hour).</p> : (
        <table className="tbl"><thead><tr><th>Last</th><th>Type</th><th>Namespace</th><th>Object</th><th>Reason</th><th>Message</th><th>#</th></tr></thead>
          <tbody>{rows.map((e, i) => <tr key={i}><td className="help">{fmtDate(e.last)}</td><td><Badge s={e.type === 'Warning' ? 'warn' : 'info'} /></td><td className="mono">{e.namespace}</td><td className="mono">{e.object}</td><td>{e.reason}</td><td className="help">{e.message}</td><td>{e.count}</td></tr>)}</tbody></table>
      ))}
    </div>
  )
}

export default function Logs(p) {
  const { spec } = p
  const [tab, setTab] = useState('pods')
  const [err, setErr] = useState('')
  const { data: mg, reload } = useFetch(p.name, '/mustgather')
  const [job, setJob] = useState(null)
  const [since, setSince] = useState('')
  const [images, setImages] = useState('')
  const run = jobRunner(p.name, setJob, setErr)
  const gather = () => run('/mustgather/run', { since, images: images.split(/[\s,]+/).filter(Boolean) }, 'Run must-gather? It starts pods on the cluster and takes several minutes; the archive is kept on the installer host until you delete it.')
  const del = async (id) => { if (!confirm(`Delete ${id}?`)) return; try { await api.del(`/api/extra-isos/${id}`); reload() } catch (e) { setErr(e.message) } }
  return (
    <div>
      <div className="panel">
        <h2>Logs &amp; events</h2>
        <Alert kind="error">{err}</Alert>
        <div className="tabs"><button className={tab === 'pods' ? 'on' : ''} onClick={() => setTab('pods')}>Pod logs</button><button className={tab === 'events' ? 'on' : ''} onClick={() => setTab('events')}>Events</button></div>
        {tab === 'pods' ? <PodLogs name={p.name} setErr={setErr} /> : <Events name={p.name} setErr={setErr} />}
      </div>
      <div className="panel">
        <h2>must-gather</h2>
        <p className="lead">Collects cluster state and logs for troubleshooting or a Red Hat support case (<span className="mono">oc adm must-gather</span>). The archive is written to the installer host{spec.imported ? ' (also for this connected cluster)' : ''} and kept until you delete it.</p>
        {!spec.read_only && <div className="grid3">
          <Field label="Only logs newer than (optional)" help="e.g. 2h or 30m"><Text value={since} onChange={setSince} placeholder="all" /></Field>
          <Field label="Additional images (optional)" help="product must-gather images, e.g. for ODF or Virtualization"><Text value={images} onChange={setImages} placeholder="registry.redhat.io/…/…-must-gather-rhel9:v4.x" /></Field>
          <Field label=" "><button className="primary" onClick={gather}>Run must-gather</button></Field>
        </div>}
        {mg && mg.length > 0 && (
          <table className="tbl" style={{ marginTop: 10 }}><thead><tr><th>Collected</th><th>Scope</th><th>Status</th><th>Size</th><th></th></tr></thead>
            <tbody>{mg.map(b => <tr key={b.id}><td className="help">{fmtDate(b.created)}</td><td className="help">{b.note}</td><td><Badge s={b.status === 'ready' ? 'pass' : b.status === 'failed' ? 'fail' : 'running'} /></td><td>{b.size_mb ? `${b.size_mb} MB` : ''}</td>
              <td className="row end">{b.file && <a className="btn small primary" href={`/api/extra-isos/${b.id}/download`}>Download</a>}<button className="small danger" onClick={() => del(b.id)}>Delete</button></td></tr>)}</tbody></table>
        )}
        {job && <div style={{ marginTop: 10 }}><JobLog cluster={p.name} jobId={job} onDone={reload} /></div>}
      </div>
    </div>
  )
}
