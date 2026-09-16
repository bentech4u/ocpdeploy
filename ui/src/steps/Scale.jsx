import { useState } from 'react'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Scale(p) {
  const { spec } = p
  const { data: d, err, busy, reload, setErr } = useFetch(p.name, '/scale')
  const [job, setJob] = useState(null)
  const [ms, setMs] = useState('')
  const [ips, setIps] = useState('')
  const [sel, setSel] = useState([])
  const run = jobRunner(p.name, setJob, setErr)
  const isIPI = spec.install_method === 'ipi'
  const chosen = d?.machinesets.find(m => m.name === (ms || d.machinesets[0]?.name))
  const ipList = ips.split(/[\s,]+/).filter(Boolean)
  const up = () => run('/scale/up', { machineset: chosen.name, ips: ipList }, `Scale ${chosen.name} by ${chosen.static_ip ? ipList.length : 1} node(s)${chosen.static_ip ? ` using ${ipList.join(', ')}` : ''}?`)
  const down = () => run('/scale/down', { machines: sel }, `Remove ${sel.length} machine(s)?\n${sel.join('\n')}\nEach node is drained, deleted and its VM destroyed.`)
  const delMs = (name) => run('/scale/delete-machineset', { name }, `Delete MachineSet ${name} and all of its machines?`)
  const done = () => { setSel([]); reload(); p.reload() }
  if (!isIPI) return <div className="panel"><h2>Scaling</h2><Alert kind="info">Machine API scaling is only available on IPI clusters. For agent-based clusters add nodes through pools on the Operate page and remove them by deleting the VM after <span className="mono">oc delete node</span>.</Alert></div>
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Scaling</h2><span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead">Add or remove workers through the Machine API. This cluster uses static IPs, so every new machine needs an address: the app raises the MachineSet, then binds each new IP claim to the address you give it. Removing a machine drains the node and destroys the VM; the load balancer pools are updated either way.</p>
        <Alert kind="error">{err}</Alert>
        {d && (
          <>
            <h3>MachineSets</h3>
            <table className="tbl"><thead><tr><th>Name</th><th>Role</th><th>Replicas</th><th>Ready</th><th>Size</th><th>Addressing</th><th></th></tr></thead>
              <tbody>{d.machinesets.map(m => <tr key={m.name}><td className="mono">{m.name}</td><td>{m.role}</td><td>{m.replicas}</td><td>{m.ready}/{m.available}</td><td className="help">{m.cpus} vCPU · {m.memory_mb} MB · {m.disk_gb} GB</td><td>{m.static_ip ? <span className="tag">static IP</span> : <span className="tag">DHCP</span>}</td>
                <td>{m.created_by_app || m.role !== 'worker' ? <button className="small danger" onClick={() => delMs(m.name)}>Delete</button> : ''}</td></tr>)}</tbody></table>
            <h3>Scale up</h3>
            <div className="grid3">
              <Field label="MachineSet"><Select value={ms || d.machinesets[0]?.name || ''} onChange={setMs} options={d.machinesets.map(m => m.name)} /></Field>
              {chosen?.static_ip
                ? <Field label="IP address per new node" help={`One per line, inside ${d.cidr}. In use: ${d.used_ips.slice(-4).join(', ')}…`}><textarea style={{ minHeight: 60 }} value={ips} onChange={e => setIps(e.target.value)} placeholder={'192.168.68.240\n192.168.68.241'} /></Field>
                : <Field label="Nodes to add" help="DHCP MachineSet: one node per click"><Text value="1" onChange={() => {}} disabled /></Field>}
              <Field label=" "><button className="primary" onClick={up} disabled={!chosen || (chosen.static_ip && !ipList.length)}>Add {chosen?.static_ip ? ipList.length || '' : 1} node(s)</button></Field>
            </div>
            {d.unbound_claims.length > 0 && <Alert kind="warn">Unbound IP claims waiting for an address: {d.unbound_claims.join(', ')}. Scaling up with the same MachineSet binds them first.</Alert>}
            <h3>Machines</h3>
            <table className="tbl"><thead><tr><th></th><th>Machine</th><th>Role</th><th>Phase</th><th>Node</th><th>IP</th><th>MachineSet</th><th>In LB pools</th><th>Age</th></tr></thead>
              <tbody>{d.machines.map(m => <tr key={m.name}>
                <td>{m.role !== 'master' && <input type="checkbox" checked={sel.includes(m.name)} onChange={e => setSel(e.target.checked ? [...sel, m.name] : sel.filter(x => x !== m.name))} />}</td>
                <td className="mono">{m.name}</td><td>{m.role}</td><td><Badge s={m.phase === 'Running' ? 'pass' : m.phase === 'Deleting' || m.phase === 'Provisioning' ? 'running' : 'warn'} /> {m.phase}</td><td className="mono help">{m.node}</td><td className="mono">{m.ip}</td><td className="help">{m.machineset || 'standalone'}</td><td>{m.in_spec ? <Badge s="pass" /> : <span className="help">—</span>}</td><td className="help">{m.age}</td></tr>)}</tbody></table>
            <div className="toolbar"><button className="danger" onClick={down} disabled={!sel.length}>Remove selected ({sel.length})</button><span className="help">Control-plane machines cannot be selected.</span></div>
          </>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
