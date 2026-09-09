import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'

const SIZE = { cpus: 4, memory_mb: 16384, disk_gb: 120 }

export default function Nodes(p) {
  const { spec, update } = p
  const [plan, setPlan] = useState({ masters: 3, workers: 3, infra: 3, bootstrap: true, first_ip: '', master_size: { ...SIZE }, worker_size: { ...SIZE }, infra_size: { ...SIZE }, name_style: 'master01' })
  const [err, setErr] = useState('')
  const isIPI = spec.install_method === 'ipi'

  const generate = async () => {
    setErr('')
    try {
      const nodes = await api.post(`/api/clusters/${p.name}/nodes/plan`, plan)
      update(s => s.nodes = nodes)
    } catch (e) { setErr(e.message) }
  }
  const setN = (i, k, v) => update(s => s.nodes[i][k] = v)
  const add = () => update(s => s.nodes.push({ name: '', role: 'worker', ip: '', mac: '', ...SIZE }))
  const del = (i) => update(s => s.nodes.splice(i, 1))
  const sizeRow = (label, key) => (
    <tr>
      <td>{label}</td>
      <td><Num value={plan[key].cpus} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], cpus: v } })} /></td>
      <td><Num value={plan[key].memory_mb} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], memory_mb: v } })} /></td>
      <td><Num value={plan[key].disk_gb} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], disk_gb: v } })} /></td>
    </tr>
  )
  const counts = ['bootstrap', 'master', 'worker', 'infra'].map(r => `${r}: ${spec.nodes.filter(n => n.role === r).length}`).join(' · ')

  return (
    <div>
      <div className="panel">
        <h2>Nodes</h2>
        <p className="lead">Day 1 installs masters and workers{isIPI ? ' (plus a temporary bootstrap VM)' : ''}. Infra nodes are defined here too but created after the install from the Operate page.</p>
        <Alert kind="error">{err}</Alert>
        <h3>Generate a node table</h3>
        <div className="grid3">
          <Field label="Masters"><Num value={plan.masters} onChange={v => setPlan({ ...plan, masters: v })} /></Field>
          <Field label="Workers"><Num value={plan.workers} onChange={v => setPlan({ ...plan, workers: v })} /></Field>
          <Field label="Infra (day 2)"><Num value={plan.infra} onChange={v => setPlan({ ...plan, infra: v })} /></Field>
          <Field label="First IP" help="Assigned sequentially: bootstrap, masters, infra, workers"><Text value={plan.first_ip} onChange={v => setPlan({ ...plan, first_ip: v })} placeholder="10.0.10.20" /></Field>
          <Field label="Naming"><Select value={plan.name_style} onChange={v => setPlan({ ...plan, name_style: v })} options={[{ value: 'master01', label: 'master01, worker01…' }, { value: 'master-0', label: 'master-0, worker-0…' }]} /></Field>
          {isIPI && <Field label="Bootstrap VM"><Select value={String(plan.bootstrap)} onChange={v => setPlan({ ...plan, bootstrap: v === 'true' })} options={[{ value: 'true', label: 'include (required for IPI)' }, { value: 'false', label: 'omit' }]} /></Field>}
        </div>
        <table className="tbl" style={{ marginTop: 10, maxWidth: 560 }}>
          <thead><tr><th>Sizing</th><th>vCPU</th><th>Memory MB</th><th>Disk GB</th></tr></thead>
          <tbody>{sizeRow('Master', 'master_size')}{sizeRow('Worker', 'worker_size')}{sizeRow('Infra', 'infra_size')}</tbody>
        </table>
        <p className="help">Minimums: master 4 vCPU / 16 GB / 120 GB, worker 2 vCPU / 8 GB / 120 GB. Bootstrap uses the master size.</p>
        <div className="toolbar"><button className="primary" onClick={generate}>Generate table {spec.nodes.length ? '(replaces current)' : ''}</button></div>

        <h3>Node table <span className="help">— {counts}</span></h3>
        <table className="tbl">
          <thead><tr><th>Name</th><th>Role</th><th>IP</th><th>MAC {isIPI ? '(agent only)' : ''}</th><th>vCPU</th><th>MB</th><th>GB</th><th></th></tr></thead>
          <tbody>
            {spec.nodes.map((n, i) => (
              <tr key={i}>
                <td><Text value={n.name} onChange={v => setN(i, 'name', v)} /></td>
                <td><Select value={n.role} onChange={v => setN(i, 'role', v)} options={['bootstrap', 'master', 'worker', 'infra']} /></td>
                <td><Text value={n.ip} onChange={v => setN(i, 'ip', v)} /></td>
                <td><Text value={n.mac || ''} onChange={v => setN(i, 'mac', v)} placeholder="auto" /></td>
                <td style={{ width: 70 }}><Num value={n.cpus} onChange={v => setN(i, 'cpus', v)} /></td>
                <td style={{ width: 90 }}><Num value={n.memory_mb} onChange={v => setN(i, 'memory_mb', v)} /></td>
                <td style={{ width: 80 }}><Num value={n.disk_gb} onChange={v => setN(i, 'disk_gb', v)} /></td>
                <td><button onClick={() => del(i)}>✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="toolbar"><button onClick={add}>+ Add node</button>
          <span className="help">Names become DNS labels under {spec.name}.{spec.base_domain}. MACs are generated deterministically and used for VM NICs in the agent method.</span></div>
      </div>
      <Footer {...p} />
    </div>
  )
}
