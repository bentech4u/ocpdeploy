import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner, fmtDate } from '../components/ops.js'

export default function Upgrade(p) {
  const { data: u, err, busy, reload, setErr } = useFetch(p.name, '/upgrade')
  const [job, setJob] = useState(null)
  const [channel, setChannel] = useState('')
  const [explicit, setExplicit] = useState('')
  const [force, setForce] = useState(false)
  const run = jobRunner(p.name, setJob, setErr)
  const setCh = async () => { try { const r = await api.post(`/api/clusters/${p.name}/upgrade/channel`, { channel: channel || u.channel }); alert(r.output || 'channel set'); reload() } catch (e) { setErr(e.message) } }
  const ack = async () => { try { const r = await api.post(`/api/clusters/${p.name}/upgrade/ack`); alert(`Acknowledged: ${r.acked.join(', ') || 'nothing'}`); reload() } catch (e) { setErr(e.message) } }
  const upgrade = (v) => run('/upgrade/start', { version: v, force }, `Upgrade ${p.name} from ${u.version} to ${v}? The control plane and every node restart in turn; this runs 60-120 minutes.`)
  const done = () => { reload(); p.reload() }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Cluster upgrade</h2>{u && <span className="tag accent">{u.version}</span>}{u?.progressing && <Badge s="running" />}<span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead">The Cluster Version Operator fetches available updates for the selected channel from Red Hat's update graph. Pick a target, the app starts the upgrade and follows it to completion.</p>
        <Alert kind="error">{err}</Alert>
        {u && (
          <>
            {u.progressing && <Alert kind="info">Upgrade in progress: {u.message}</Alert>}
            {u.failing && <Alert kind="error">Failing: {u.failing_message}</Alert>}
            {!u.retrieved_updates && <Alert kind="warn">Updates not retrieved: {u.retrieved_message || 'the cluster cannot reach the update graph'}{u.mirror ? ' — disconnected cluster: mirror the target release and enter the version below.' : ''}</Alert>}
            {!u.upgradeable && <Alert kind="warn"><b>Upgradeable=False ({u.upgradeable_reason})</b> — {u.upgradeable_message}</Alert>}
            {u.admin_gates?.length > 0 && (
              <div className="alert warn"><b>Administrator acknowledgement required</b>
                <ul className="hint-list">{u.admin_gates.map(g => <li key={g.key}><span className="mono">{g.key}</span> {g.acked ? <Badge s="pass" /> : ''}<br /><span className="help">{g.message}</span></li>)}</ul>
                {u.admin_gates.some(g => !g.acked) && <button className="primary small" onClick={ack}>Acknowledge all gates</button>}
              </div>
            )}
            <h3>Channel</h3>
            <div className="row">
              <Select value={channel || u.channel} onChange={setChannel} options={[...new Set([u.channel, ...(u.channels || [])].filter(Boolean))]} />
              <Text value={channel} onChange={setChannel} placeholder="or type e.g. stable-4.23" style={{ maxWidth: 220 }} />
              <button onClick={setCh} disabled={!(channel || u.channel)}>Set channel</button>
              <span className="help">Move to the next minor's stable channel to see the next release; eus channels for even minors.</span>
            </div>
            <h3>Available updates</h3>
            {u.available_updates.length === 0 && <p className="muted">No updates offered on {u.channel || 'the current channel'}.</p>}
            {u.available_updates.length > 0 && (
              <table className="tbl"><thead><tr><th>Version</th><th>Image</th><th></th></tr></thead>
                <tbody>{u.available_updates.map(a => <tr key={a.version}><td><b>{a.version}</b></td><td className="mono help">{a.image}</td><td><button className="primary small" onClick={() => upgrade(a.version)} disabled={u.progressing}>Upgrade to {a.version}</button></td></tr>)}</tbody></table>
            )}
            {u.conditional_updates?.length > 0 && <p className="help">Conditional updates (known risks): {u.conditional_updates.map(c => `${c.version} [${c.risks.join(', ')}]`).join('; ')} — use the explicit version with "force" if you accept the risk.</p>}
            <h3>Explicit version</h3>
            <div className="row">
              <Text value={explicit} onChange={setExplicit} placeholder="4.23.2" style={{ maxWidth: 160 }} />
              <label className="row help"><input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} /> force (skip Upgradeable / precondition checks)</label>
              <button onClick={() => upgrade(explicit)} disabled={!explicit || u.progressing}>Upgrade to version</button>
              <span className="help">{u.mirror ? `Uses ${'<registry>'}/openshift/release-images:<version>-x86_64 from the mirror.` : 'For versions not on the graph; the CVO still verifies the release signature.'}</span>
            </div>
            <h3>History</h3>
            <table className="tbl"><thead><tr><th>Version</th><th>State</th><th>Started</th><th>Completed</th></tr></thead>
              <tbody>{u.history.map((h, i) => <tr key={i}><td>{h.version}</td><td><Badge s={h.state === 'Completed' ? 'pass' : 'running'} /></td><td className="help">{fmtDate(h.started)}</td><td className="help">{fmtDate(h.completed)}</td></tr>)}</tbody></table>
          </>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
