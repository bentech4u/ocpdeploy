import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

const PRESETS = [{ value: '', label: 'Disabled' }, { value: 'daily', label: 'Daily at 00:00' }, { value: '*-*-* 02:00:00', label: 'Daily at 02:00' }, { value: 'Sun *-*-* 03:00:00', label: 'Weekly (Sunday 03:00)' }, { value: 'hourly', label: 'Hourly' }]

export default function Backup(p) {
  const { data: st, err, reload, setErr } = useFetch(p.name, '/backup')
  const [job, setJob] = useState(null)
  const [schedule, setSchedule] = useState(null)
  const [keep, setKeep] = useState(null)
  const run = jobRunner(p.name, setJob, setErr)
  const sched = schedule ?? st?.schedule ?? ''
  const keepN = keep ?? st?.keep ?? 7
  const save = async () => { setErr(''); try { const r = await api.post(`/api/clusters/${p.name}/backup/schedule`, { schedule: sched, keep: keepN }); alert(sched ? `Timer active; next run ${r.next || 'pending'}` : 'Timer disabled'); reload(); p.reload() } catch (e) { setErr(e.message) } }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>etcd backups</h2><span className="spacer" /><button onClick={reload}>Refresh</button></div>
        {p.spec.imported && <Alert kind="warn">Connected cluster: backups are kept in memory for this session only. Download them before you disconnect; scheduling is not available.</Alert>}
        <p className="lead">Runs <span className="mono">cluster-backup.sh</span> on a control-plane node over SSH (the cluster's SSH key is this host's key), copies the snapshot and static pod resources here as a tarball, and keeps the last N. A systemd timer on the installer host runs it on a schedule.</p>
        <Alert kind="error">{err}</Alert>
        {!p.spec.imported && <div className="grid3">
          <Field label="Schedule (systemd OnCalendar)" help="Presets or any OnCalendar expression, e.g. Mon..Fri *-*-* 01:30:00"><Select value={PRESETS.some(x => x.value === sched) ? sched : 'custom'} onChange={v => setSchedule(v === 'custom' ? sched : v)} options={[...PRESETS, { value: 'custom', label: 'Custom…' }]} /></Field>
          <Field label="Expression"><Text value={sched} onChange={setSchedule} placeholder="*-*-* 02:00:00" /></Field>
          <Field label="Keep last N backups"><Num value={keepN} onChange={setKeep} /></Field>
        </div>}
        <div className="toolbar">
          {st?.timer && <span className="help">Timer {st.timer.active ? <Badge s="pass" /> : <Badge s="grey" />} {st.timer.active && st.timer.next ? `next ${st.timer.next}` : ''}{st.timer.last && st.timer.last !== 'n/a' ? ` · last ${st.timer.last}` : ''}</span>}
          <span className="spacer" />
          {!p.spec.imported && <button onClick={save}>Save schedule</button>}
          <button className="primary" onClick={() => run('/backup/run', {}, 'Take an etcd backup now?')}>Back up now</button>
        </div>
        <h3>Backups on this host</h3>
        {st && (st.backups.length === 0 ? <p className="muted">No backups yet.</p> : (
          <table className="tbl"><thead><tr><th>File</th><th>Created</th><th>Size</th><th></th></tr></thead>
            <tbody>{st.backups.map(b => <tr key={b.file}><td className="mono">{b.file}</td><td className="help">{b.created}</td><td>{b.size_mb} MB</td><td><a className="btn small" href={`/api/clusters/${p.name}/backup/download/${b.file}`}>Download</a></td></tr>)}</tbody></table>
        ))}
        <p className="help">Restore procedure: docs.redhat.com → "Restoring to a previous cluster state" (needs the snapshot and static pod resources from the tarball on one control-plane node).</p>
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={reload} /></div>}
      </div>
    </div>
  )
}
