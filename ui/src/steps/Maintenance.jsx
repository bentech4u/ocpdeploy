import { useState } from 'react'
import { api } from '../api.js'
import { Alert, Num } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Maintenance(p) {
  const { spec } = p
  const { data: d, err, busy, reload, setErr } = useFetch(p.name, '/maintenance')
  const [job, setJob] = useState(null)
  const [opt, setOpt] = useState({ timeout: 900, force: false })
  const [removing, setRemoving] = useState(null)
  const [confirmName, setConfirmName] = useState('')
  const [delMachine, setDelMachine] = useState(true)
  const run = jobRunner(p.name, setJob, setErr)
  const quick = async (action, node) => { setErr(''); try { await api.post(`/api/clusters/${p.name}/maintenance/${action}`, { node }); reload() } catch (e) { setErr(e.message) } }
  const precheck = async (n) => {
    setErr('')
    try {
      const r = await api.post(`/api/clusters/${p.name}/maintenance/drain-check`, { node: n.name, force: opt.force })
      if (r.blockers) { setErr(`Draining ${n.name} would fail: ${r.blockers}${opt.force ? '' : ' — tick "force" to delete pods without a controller (their data is lost).'}`); return false }
      return true
    } catch (e) { setErr(e.message); return false }
  }
  const drain = async (n) => { if (await precheck(n)) run('/maintenance/drain', { node: n.name, ...opt }, `Drain ${n.name}? It is cordoned and its ${n.pods} pods are evicted (DaemonSet pods stay). The dry run found no blockers.`) }
  const reboot = async (n) => { if (await precheck(n)) run('/maintenance/reboot', { node: n.name, drain: true, ...opt }, `Reboot ${n.name}? It is drained first, rebooted, and uncordoned when it is Ready again.${n.master ? ' Control-plane node: only one at a time; the other masters must be Ready.' : ''}`) }
  const remove = async () => { if (!(await precheck(removing))) return; return run('/maintenance/remove', { node: removing.name, confirm: confirmName, delete_machine: delMachine, ...opt }, null).then(r => { if (r) { setRemoving(null); setConfirmName('') } }) }
  const done = () => reload()
  const removeText = (n) => {
    if (n.machine && delMachine) return `Its Machine ${n.machine} is deleted: the machine API shuts down and deletes the VM.`
    if (!spec.imported && spec.install_method === 'agent') return 'The node is deleted and the VM powered off through the infrastructure provider.'
    return 'The node object is deleted; power the machine off yourself (otherwise it re-registers when it boots).'
  }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Node maintenance</h2><span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead" style={{ marginTop: 6 }}>Cordon stops new pods; drain also moves the existing ones away (respecting PodDisruptionBudgets); reboot drains, restarts the node, waits until it is Ready and uncordons it; remove drains the node and takes it out of the cluster.</p>
        <Alert kind="error">{err}</Alert>
        <div className="row help" style={{ marginBottom: 10 }}>
          Drain timeout <Num value={opt.timeout} onChange={v => setOpt({ ...opt, timeout: v })} style={{ width: 90 }} /> s
          <label className="row"><input type="checkbox" checked={opt.force} onChange={e => setOpt({ ...opt, force: e.target.checked })} /> force (also delete pods without a controller; their data is lost)</label>
        </div>
        {d && (
          <table className="tbl"><thead><tr><th>Node</th><th>Roles</th><th>Status</th><th>Pods</th><th>IP</th><th>Machine</th><th></th></tr></thead>
            <tbody>{d.nodes.map(n => <tr key={n.name}>
              <td className="mono">{n.name}</td><td className="help">{n.roles.join(', ')}</td>
              <td><Badge s={n.ready === 'True' ? 'pass' : 'fail'} />{n.unschedulable && <span className="badge warn" style={{ marginLeft: 4 }}>cordoned</span>}</td>
              <td>{n.pods}</td><td className="mono">{n.ip}</td><td className="help mono">{n.machine || '—'}</td>
              <td><div className="row end">
                {n.unschedulable ? <button className="small" onClick={() => quick('uncordon', n.name)}>Uncordon</button> : <button className="small" onClick={() => quick('cordon', n.name)}>Cordon</button>}
                <button className="small" onClick={() => drain(n)}>Drain</button>
                <button className="small" onClick={() => reboot(n)}>Reboot</button>
                {!n.master && <button className="small danger" onClick={() => { setRemoving(n); setConfirmName(''); setDelMachine(!!n.machine) }}>Remove</button>}
              </div></td></tr>)}</tbody></table>
        )}
        {removing && (
          <div className="alert warn" style={{ marginTop: 12 }}>
            <b>Remove {removing.name}</b>: it is drained first. {removeText(removing)}
            {removing.machine && <label className="row" style={{ marginTop: 6 }}><input type="checkbox" checked={delMachine} onChange={e => setDelMachine(e.target.checked)} /> delete its Machine {removing.machine} (destroys the VM)</label>}
            <div className="row" style={{ marginTop: 8 }}>Type the node name to confirm: <input type="text" value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={removing.name} style={{ width: 300 }} />
              <button className="danger" onClick={remove} disabled={confirmName !== removing.name}>Remove node</button><button onClick={() => setRemoving(null)}>Cancel</button></div>
          </div>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
