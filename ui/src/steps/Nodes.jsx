import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert, RadioCards } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'

const SIZE = { cpus: 4, memory_mb: 16384, disk_gb: 120 }
const POOL_PRESETS = {
  gpu: { kind: 'gpu', description: 'GPU workers', labels: { 'nvidia.com/gpu.present': 'true' }, taints: [{ key: 'nvidia.com/gpu', value: '', effect: 'NoSchedule' }], extra_disks_gb: [], serve_ingress: false, size: { cpus: 16, memory_mb: 65536, disk_gb: 200 } },
  storage: { kind: 'storage', description: 'Storage workers (ODF / LVM Storage)', labels: { 'cluster.ocs.openshift.io/openshift-storage': '' }, taints: [{ key: 'node.ocs.openshift.io/storage', value: 'true', effect: 'NoSchedule' }], extra_disks_gb: [500, 500], serve_ingress: false, size: { cpus: 16, memory_mb: 65536, disk_gb: 120 } },
  general: { kind: 'general', description: 'Extra workers with their own size', labels: {}, taints: [], extra_disks_gb: [], serve_ingress: true, size: { cpus: 8, memory_mb: 32768, disk_gb: 120 } },
}
const TOPO_COUNTS = { sno: { masters: 1, workers: 0 }, compact: { masters: 3, workers: 0 }, standard: { masters: 3, workers: 3 } }

const labelsToText = (o) => Object.entries(o || {}).map(([k, v]) => v === '' ? k : `${k}=${v}`).join(', ')
const textToLabels = (t) => { const o = {}; t.split(/[,\s]+/).filter(Boolean).forEach(kv => { const [k, ...r] = kv.split('='); if (k) o[k] = r.join('=') }); return o }
const taintsToText = (l) => (l || []).map(t => `${t.key}${t.value ? '=' + t.value : ''}:${t.effect}`).join(', ')
const textToTaints = (t) => t.split(/[,\s]+/).filter(Boolean).map(x => { const [kv, effect] = x.split(':'); const [key, ...r] = kv.split('='); return { key, value: r.join('='), effect: effect || 'NoSchedule' } }).filter(t => t.key)
const listToText = (l) => (l || []).join(', ')
const textToInts = (t) => t.split(/[,\s]+/).map(x => parseInt(x, 10)).filter(n => Number.isFinite(n) && n > 0)

export default function Nodes(p) {
  const { spec, update } = p
  const isIPI = spec.install_method === 'ipi'
  const topo = spec.topology || 'standard'
  const [plan, setPlan] = useState({ ...TOPO_COUNTS[topo], infra: 0, bootstrap: true, first_ip: '', master_size: { ...SIZE }, worker_size: { ...SIZE }, infra_size: { ...SIZE }, name_style: 'master01' })
  const [poolCounts, setPoolCounts] = useState({})
  const [preset, setPreset] = useState('gpu')
  const [err, setErr] = useState('')
  useEffect(() => { setPlan(pl => ({ ...pl, ...TOPO_COUNTS[topo] })) }, [topo])

  const generate = async () => {
    setErr('')
    try {
      const pools = spec.pools.filter(pl => (poolCounts[pl.name] || 0) > 0).map(pl => ({ name: pl.name, count: poolCounts[pl.name], size: POOL_PRESETS[pl.kind]?.size || POOL_PRESETS.general.size }))
      const nodes = await api.post(`/api/clusters/${p.name}/nodes/plan`, { ...plan, topology: topo, pools })
      update(s => s.nodes = nodes)
    } catch (e) { setErr(e.message) }
  }
  const setN = (i, k, v) => update(s => s.nodes[i][k] = v)
  const add = () => update(s => s.nodes.push({ name: '', role: 'worker', ip: '', mac: '', ...SIZE, pool: '', failure_domain: '', extra_disks_gb: [] }))
  const del = (i) => update(s => s.nodes.splice(i, 1))
  const addPool = () => {
    const pr = POOL_PRESETS[preset]
    let name = preset; let n = 2
    while (spec.pools.some(pl => pl.name === name)) name = `${preset}${n++}`
    update(s => s.pools.push({ name, kind: pr.kind, description: pr.description, labels: { ...pr.labels }, taints: pr.taints.map(t => ({ ...t })), extra_disks_gb: [...pr.extra_disks_gb], serve_ingress: pr.serve_ingress }))
  }
  const setPool = (i, k, v) => update(s => s.pools[i][k] = v)
  const delPool = (i) => update(s => { const nm = s.pools[i].name; s.pools.splice(i, 1); s.nodes.forEach(n => { if (n.pool === nm) n.pool = '' }) })
  const sizeRow = (label, key) => (
    <tr>
      <td>{label}</td>
      <td><Num value={plan[key].cpus} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], cpus: v } })} /></td>
      <td><Num value={plan[key].memory_mb} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], memory_mb: v } })} /></td>
      <td><Num value={plan[key].disk_gb} onChange={v => setPlan({ ...plan, [key]: { ...plan[key], disk_gb: v } })} /></td>
    </tr>
  )
  const fds = isIPI ? (spec.vcenter.failure_domains || []) : []
  const day1Workers = spec.nodes.filter(n => n.role === 'worker' && !n.pool).length
  const pooled = spec.nodes.filter(n => n.role === 'worker' && n.pool).length
  const counts = [`bootstrap: ${spec.nodes.filter(n => n.role === 'bootstrap').length}`, `master: ${spec.nodes.filter(n => n.role === 'master').length}`,
    `worker (day 1): ${day1Workers}`, `pool nodes: ${pooled}`, `infra: ${spec.nodes.filter(n => n.role === 'infra').length}`].join(' · ')
  const fixed = topo !== 'standard'

  return (
    <div>
      <div className="panel">
        <h2>Topology</h2>
        <p className="lead">How many control-plane nodes the cluster has and whether the masters also run workloads.</p>
        <RadioCards value={topo} onChange={v => update(s => s.topology = v)} options={[
          { value: 'standard', label: 'Standard — 3 masters + workers', desc: 'Control plane is dedicated; workloads run on the workers. The usual production layout.' },
          { value: 'compact', label: 'Compact — 3 schedulable masters', desc: 'No workers on day 1; the installer makes the masters schedulable and the routers run on them. Workers or pools can still be added later.' },
          { value: 'sno', label: 'Single node OpenShift', desc: `One node runs everything. No load balancer needed (DNS points at the node).${isIPI ? ' IPI still uses a temporary bootstrap VM.' : ''} Minimum 8 vCPU / 16 GB / 120 GB.` },
        ]} />
      </div>

      <div className="panel">
        <h2>Nodes</h2>
        <p className="lead">Day 1 installs {topo === 'sno' ? 'the single node' : `masters${topo === 'standard' ? ' and workers' : ''}`}{isIPI ? ' (plus a temporary bootstrap VM)' : ''}. Infra nodes and pool members are defined here too but created after the install from the Operate page.</p>
        <Alert kind="error">{err}</Alert>
        <h3>Generate a node table</h3>
        <div className="grid3">
          <Field label="Masters"><Num value={plan.masters} onChange={v => setPlan({ ...plan, masters: v })} disabled={fixed} /></Field>
          <Field label="Workers (day 1)"><Num value={plan.workers} onChange={v => setPlan({ ...plan, workers: v })} disabled={fixed} /></Field>
          <Field label="Infra (day 2)"><Num value={plan.infra} onChange={v => setPlan({ ...plan, infra: v })} /></Field>
          <Field label="First IP" help="Assigned sequentially: bootstrap, masters, infra, workers, pools"><Text value={plan.first_ip} onChange={v => setPlan({ ...plan, first_ip: v })} placeholder="10.0.10.20" /></Field>
          <Field label="Naming"><Select value={plan.name_style} onChange={v => setPlan({ ...plan, name_style: v })} options={[{ value: 'master01', label: 'master01, worker01…' }, { value: 'master-0', label: 'master-0, worker-0…' }]} /></Field>
          {isIPI && <Field label="Bootstrap VM"><Select value={String(plan.bootstrap)} onChange={v => setPlan({ ...plan, bootstrap: v === 'true' })} options={[{ value: 'true', label: 'include (required for IPI)' }, { value: 'false', label: 'omit' }]} /></Field>}
          {spec.pools.map(pl => (
            <Field key={pl.name} label={`Pool "${pl.name}" nodes (day 2)`}><Num value={poolCounts[pl.name] || 0} onChange={v => setPoolCounts({ ...poolCounts, [pl.name]: v })} /></Field>
          ))}
        </div>
        <table className="tbl compact" style={{ marginTop: 12, maxWidth: 560 }}>
          <thead><tr><th>Sizing</th><th>vCPU</th><th>Memory MB</th><th>Disk GB</th></tr></thead>
          <tbody>{sizeRow('Master', 'master_size')}{sizeRow('Worker', 'worker_size')}{sizeRow('Infra', 'infra_size')}</tbody>
        </table>
        <p className="help">Minimums: master 4 vCPU / 16 GB / 120 GB, worker 2 vCPU / 8 GB / 120 GB, single node 8 vCPU / 16 GB / 120 GB. Bootstrap uses the master size; pool members use their pool's preset size.</p>
        <div className="toolbar"><button className="primary" onClick={generate}>Generate table {spec.nodes.length ? '(replaces current)' : ''}</button></div>

        <h3>Node table <span className="help">— {counts}</span></h3>
        <table className="tbl">
          <thead><tr><th>Name</th><th>Role</th><th>Pool</th>{fds.length > 0 && <th>Failure domain</th>}<th>IP</th><th>MAC {isIPI ? '(agent only)' : ''}</th><th>vCPU</th><th>MB</th><th>GB</th><th>Data disks GB</th><th></th></tr></thead>
          <tbody>
            {spec.nodes.map((n, i) => (
              <tr key={i}>
                <td><Text value={n.name} onChange={v => setN(i, 'name', v)} /></td>
                <td><Select value={n.role} onChange={v => update(s => { s.nodes[i].role = v; if (v !== 'worker') s.nodes[i].pool = '' })} options={['bootstrap', 'master', 'worker', 'infra']} /></td>
                <td style={{ width: 120 }}>{n.role === 'worker'
                  ? <Select value={n.pool || ''} onChange={v => setN(i, 'pool', v)} options={[{ value: '', label: 'day 1' }, ...spec.pools.map(pl => ({ value: pl.name, label: pl.name }))]} />
                  : <span className="help">—</span>}</td>
                {fds.length > 0 && <td style={{ width: 130 }}>{n.role !== 'bootstrap'
                  ? <Select value={n.failure_domain || ''} onChange={v => setN(i, 'failure_domain', v)} options={[{ value: '', label: 'any' }, ...fds.map(f => ({ value: f.name, label: f.name }))]} />
                  : <span className="help">—</span>}</td>}
                <td><Text value={n.ip} onChange={v => setN(i, 'ip', v)} /></td>
                <td><Text value={n.mac || ''} onChange={v => setN(i, 'mac', v)} placeholder="auto" /></td>
                <td style={{ width: 70 }}><Num value={n.cpus} onChange={v => setN(i, 'cpus', v)} /></td>
                <td style={{ width: 90 }}><Num value={n.memory_mb} onChange={v => setN(i, 'memory_mb', v)} /></td>
                <td style={{ width: 80 }}><Num value={n.disk_gb} onChange={v => setN(i, 'disk_gb', v)} /></td>
                <td style={{ width: 110 }}><Text value={listToText(n.extra_disks_gb)} onChange={v => setN(i, 'extra_disks_gb', textToInts(v))} placeholder={n.pool ? 'pool default' : '—'} /></td>
                <td><button className="small" onClick={() => del(i)} title="remove">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="toolbar"><button onClick={add}>+ Add node</button>
          <span className="help">Names become DNS labels under {spec.name}.{spec.base_domain}. MACs are generated deterministically and used for VM NICs in the agent method. Data disks are extra thin-provisioned VMDKs (storage pools).</span></div>
      </div>

      <div className="panel">
        <h2>Node pools</h2>
        <p className="lead">Groups of workers with their own size, labels, taints and data disks — GPU nodes, storage nodes, big-memory nodes. Members are created on day 2 ({isIPI ? 'one MachineSet per node with its static IP' : 'node ISO, then labelled'}), so the day-1 install stays uniform.</p>
        {spec.pools.length === 0 && <div className="empty">No pools yet. Add a GPU or storage pool below, then assign worker rows to it in the node table.</div>}
        {spec.pools.length > 0 && (
          <table className="tbl">
            <thead><tr><th>Name</th><th>Kind</th><th>Labels</th><th>Taints</th><th>Data disks GB</th><th>Serves *.apps</th><th>Members</th><th></th></tr></thead>
            <tbody>
              {spec.pools.map((pl, i) => (
                <tr key={i}>
                  <td style={{ width: 120 }}><Text value={pl.name} onChange={v => update(s => { const old = s.pools[i].name; s.pools[i].name = v; s.nodes.forEach(n => { if (n.pool === old) n.pool = v }) })} /></td>
                  <td style={{ width: 110 }}><Select value={pl.kind} onChange={v => setPool(i, 'kind', v)} options={['general', 'gpu', 'storage']} /></td>
                  <td><Text value={labelsToText(pl.labels)} onChange={v => setPool(i, 'labels', textToLabels(v))} placeholder="key=value, key2" /></td>
                  <td><Text value={taintsToText(pl.taints)} onChange={v => setPool(i, 'taints', textToTaints(v))} placeholder="key=value:NoSchedule" /></td>
                  <td style={{ width: 120 }}><Text value={listToText(pl.extra_disks_gb)} onChange={v => setPool(i, 'extra_disks_gb', textToInts(v))} placeholder="500, 500" /></td>
                  <td style={{ width: 90 }}><input type="checkbox" checked={!!pl.serve_ingress} onChange={e => setPool(i, 'serve_ingress', e.target.checked)} /></td>
                  <td>{spec.nodes.filter(n => n.pool === pl.name).length}</td>
                  <td><button className="small" onClick={() => delPool(i)} title="remove">✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="toolbar">
          <Select value={preset} onChange={setPreset} options={[{ value: 'gpu', label: 'GPU pool preset' }, { value: 'storage', label: 'Storage pool preset' }, { value: 'general', label: 'General pool' }]} />
          <button onClick={addPool}>+ Add pool</button>
          <span className="help">Members get the label <span className="mono">node-role.kubernetes.io/&lt;pool&gt;</span> plus the labels above. GPU passthrough itself is attached in vSphere after the VM exists. Data disks on MachineSets need OpenShift 4.18+.</span>
        </div>
      </div>
      <Footer {...p} />
    </div>
  )
}
