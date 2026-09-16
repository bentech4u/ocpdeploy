import { useState } from 'react'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Power(p) {
  const { spec } = p
  const { data: st, err, busy, reload, setErr } = useFetch(p.name, '/power')
  const [job, setJob] = useState(null)
  const [backup, setBackup] = useState(true)
  const [confirmName, setConfirmName] = useState('')
  const run = jobRunner(p.name, setJob, setErr)
  const shutdown = () => run('/power/shutdown', { backup, confirm: confirmName }, `Shut down ${spec.name}? Workers first, then the control plane. Applications are unavailable until you start it again.`)
  const startup = () => run('/power/startup', {}, `Start ${spec.name}? Masters first, then workers; pending CSRs are approved automatically.`)
  const done = () => { setConfirmName(''); reload(); p.reload() }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Power</h2>{st && (st.all_on ? <Badge s="pass" /> : st.all_off ? <Badge s="grey" /> : <Badge s="warn" />)}<span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead">Graceful shutdown and startup for labs that do not run around the clock. Shutdown takes an etcd backup, asks every node's OS to power off (workers, then masters) through VMware Tools and force-stops stragglers. Startup powers the masters on, waits for the API, powers the workers on, approves certificate requests and waits for the operators to settle.</p>
        <Alert kind="error">{err}</Alert>
        {st && (
          <>
            <div className="stat-grid">
              <div className="stat"><div className="k">API</div><div className="v" style={{ color: st.api_reachable ? 'var(--ok)' : 'var(--muted)' }}>{st.api_reachable ? 'reachable' : 'down'}</div></div>
              <div className="stat"><div className="k">VMs on</div><div className="v">{st.vms.filter(v => v.power === 'poweredOn').length}<small> / {st.vms.length}</small></div></div>
              <div className="stat"><div className="k">Kubelet signer</div><div className="v" style={{ color: (st.kubelet_signer_days ?? 999) < 30 ? 'var(--fail)' : 'var(--ok)' }}>{st.kubelet_signer_days ?? '—'}<small> days left</small></div><div className="help">Start the cluster before it expires; CSRs are approved on startup.</div></div>
            </div>
            <table className="tbl"><thead><tr><th>VM</th><th>Role</th><th>Power</th><th>IP</th><th>Tools</th></tr></thead>
              <tbody>{st.vms.map(v => <tr key={v.name}><td className="mono">{v.name}</td><td>{v.role}</td><td><Badge s={v.power === 'poweredOn' ? 'pass' : v.power === 'poweredOff' ? 'grey' : 'warn'} /> {v.power}</td><td className="mono">{v.ip || ''}</td><td className="help">{v.tools}</td></tr>)}</tbody></table>
            <div className="cards" style={{ marginTop: 16 }}>
              <div className="card action" style={{ borderColor: 'var(--warn)' }}>
                <h3>Shut down cluster</h3>
                <p className="help">Type the cluster name to enable. The kube-apiserver-to-kubelet-signer certificate must still be valid when you start it again.</p>
                <label className="row help"><input type="checkbox" checked={backup} onChange={e => setBackup(e.target.checked)} /> take an etcd backup first</label>
                <div className="row"><input type="text" value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={spec.name} style={{ width: 160 }} /><button className="danger" onClick={shutdown} disabled={confirmName !== spec.name || st.all_off}>Shut down</button></div>
              </div>
              <div className="card action">
                <h3>Start cluster</h3>
                <p className="help">Powers on masters, then workers; approves pending CSRs; waits for the cluster operators.</p>
                <div><button className="primary" onClick={startup} disabled={st.all_on}>Start</button></div>
              </div>
            </div>
          </>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
