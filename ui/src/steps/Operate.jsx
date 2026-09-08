import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'

export default function Operate(p) {
  const { spec } = p
  const [st, setSt] = useState(null)
  const [jobs, setJobs] = useState([])
  const [job, setJob] = useState(null)
  const [err, setErr] = useState('')
  const [pw, setPw] = useState('')
  const [confirmName, setConfirmName] = useState('')

  const refresh = () => {
    api.get(`/api/clusters/${p.name}/status`).then(s => { setSt(s); if (s.running_job) setJob(s.running_job) }).catch(e => setErr(e.message))
    api.get(`/api/clusters/${p.name}/jobs`).then(setJobs).catch(() => {})
  }
  useEffect(() => { refresh() }, [p.name])
  const run = (path, body, msg) => async () => {
    setErr('')
    if (msg && !confirm(msg)) return
    try { const r = await api.post(`/api/clusters/${p.name}${path}`, body); setJob(r.job_id) } catch (e) { setErr(e.message) }
  }
  const showPw = async () => { try { setPw(await api.get(`/api/clusters/${p.name}/credentials/kubeadmin-password`)) } catch (e) { setErr(e.message) } }
  const infra = spec.nodes.filter(n => n.role === 'infra')
  const c = st?.cluster

  return (
    <div>
      <div className="panel">
        <h2>Operate / Day 2</h2>
        <Alert kind="error">{err}</Alert>
        <div className="row">
          <span>Status: <Badge s={spec.status === 'installed' ? 'pass' : spec.status === 'failed' ? 'fail' : 'grey'} /> {spec.status}</span>
          {st?.infra_id && <span className="help">infra ID <span className="mono">{st.infra_id}</span></span>}
          <span className="spacer" /><button onClick={() => { p.reload(); refresh() }}>Refresh</button>
        </div>
        {st?.has_kubeconfig && (
          <dl className="kv" style={{ marginTop: 10 }}>
            <dt>Console</dt><dd><a href={st.console_url} target="_blank" rel="noreferrer">{st.console_url}</a></dd>
            <dt>API</dt><dd className="mono">{st.api_url}</dd>
            <dt>kubeconfig</dt><dd><a href={`/api/clusters/${p.name}/credentials/kubeconfig`}>download</a></dd>
            <dt>kubeadmin</dt><dd>{pw ? <span className="mono">{pw}</span> : <button onClick={showPw}>reveal password</button>}</dd>
          </dl>
        )}
        {c && (c.reachable ? (
          <>
            <h3>Nodes</h3>
            <table className="tbl"><thead><tr><th>Name</th><th>Roles</th><th>Ready</th><th>IP</th><th>Kubelet</th></tr></thead>
              <tbody>{c.nodes.map(n => <tr key={n.name}><td className="mono">{n.name}</td><td>{n.roles.join(', ')}</td><td><Badge s={n.ready === 'True' ? 'pass' : 'fail'} /></td><td className="mono">{n.ip}</td><td>{n.version}</td></tr>)}</tbody></table>
            <h3>Cluster operators <span className="help">version {c.version?.desired} · {c.version?.state}</span></h3>
            <div className="row">{c.operators.map(o => <span key={o.name} className={`badge ${o.degraded === 'True' ? 'fail' : o.available === 'True' ? (o.progressing === 'True' ? 'warn' : 'pass') : 'fail'}`} title={`available=${o.available} progressing=${o.progressing} degraded=${o.degraded}`}>{o.name}</span>)}</div>
          </>
        ) : <Alert kind="warn">API not reachable: {c.reason}</Alert>)}
      </div>

      <div className="panel">
        <h2>Actions</h2>
        <div className="cards">
          <div className="card">
            <h3>Remove bootstrap from API LB</h3>
            <p className="help">After install-complete the bootstrap node is gone. Drops it from the 6443/22623 pools{spec.lb.mode === 'haproxy' ? ' and pushes HAProxy' : ' (external LB: shows instructions)'}.</p>
            <button onClick={run('/day2/remove-bootstrap', {})} disabled={spec.lb.bootstrap_removed || spec.install_method === 'agent'}>{spec.lb.bootstrap_removed ? 'Done' : 'Remove bootstrap'}</button>
          </div>
          <div className="card">
            <h3>Add infra nodes ({infra.length})</h3>
            <p className="help">{spec.install_method === 'ipi' ? 'Creates one MachineSet per infra node with its static IP, waits for Ready, labels and taints them.' : 'Builds a node ISO with oc adm node-image, creates the VMs, approves CSRs, labels and taints.'} Then updates the apps LB pools.</p>
            <button onClick={run('/day2/add-infra', {}, `Create ${infra.length} infra node(s) now?`)} disabled={!infra.length || spec.status !== 'installed'}>Add infra nodes</button>
          </div>
          <div className="card">
            <h3>Move ingress, monitoring & registry to infra</h3>
            <p className="help">Patches the default IngressController node placement, cluster-monitoring-config and the image registry, waits for router pods on infra, then points the apps LB at infra only.</p>
            <button onClick={run('/day2/move-ingress', { monitoring: true, registry: true }, 'Relocate ingress, monitoring and registry to infra nodes?')} disabled={!infra.length || spec.status !== 'installed'}>Move to infra</button>
          </div>
          <div className="card">
            <h3>Push load balancer config</h3>
            <p className="help">Re-render and push the HAProxy files from the current node table.</p>
            <button onClick={run('/lb/push', {})} disabled={spec.lb.mode !== 'haproxy'}>Push HAProxy</button>
          </div>
          <div className="card" style={{ borderColor: 'var(--fail)' }}>
            <h3>Destroy cluster</h3>
            <p className="help">{spec.install_method === 'ipi' ? 'Runs openshift-install destroy cluster: removes all VMs, folder and tags created by the installer.' : 'Deletes the node VMs created by this app in vCenter.'} Type the cluster name to enable.</p>
            <div className="row"><input type="text" value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={spec.name} style={{ width: 160 }} />
              <button className="danger" onClick={run('/destroy', { confirm: confirmName }, `Destroy ${spec.name}? All cluster VMs will be deleted.`)} disabled={confirmName !== spec.name}>Destroy</button></div>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Jobs</h2>
        {job && <JobLog cluster={p.name} jobId={job} onDone={() => { p.reload(); refresh() }} />}
        <table className="tbl" style={{ marginTop: 10 }}>
          <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Started</th><th>Finished</th><th></th></tr></thead>
          <tbody>{jobs.map(j => <tr key={j.id}><td>{j.id}</td><td>{j.kind}</td><td><Badge s={j.status} /></td><td>{j.started}Z</td><td>{j.finished ? j.finished + 'Z' : ''}</td><td><button onClick={() => setJob(j.id)}>view log</button></td></tr>)}</tbody>
        </table>
      </div>
    </div>
  )
}
