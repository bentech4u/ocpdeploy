import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Alert, RadioCards } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner, fmtDate } from '../components/ops.js'

const prefixOf = (cidr) => { const p = parseInt(String(cidr || '').split('/')[1], 10); return Number.isFinite(p) ? p : 24 }
const blankHost = (spec) => ({ hostname: '', mac: '', ip: '', prefix: prefixOf(spec.network?.machine_cidr), gateway: spec.network?.gateway || '',
  dns: (spec.network?.dns_servers || []).join(', '), root_device: '/dev/sda', interface: 'eth0', nmstate: '', adv: false })

export default function ExtraNodes(p) {
  const { spec } = p
  const { data: d, err, reload, setErr } = useFetch(p.name, '/extra-nodes')
  const [mode, setMode] = useState('form')
  const [hosts, setHosts] = useState([blankHost(spec)])
  const [yamlText, setYamlText] = useState('')
  const [job, setJob] = useState(null)
  const [watch, setWatch] = useState(null)        // build being watched
  const [csr, setCsr] = useState(null)
  const [roles, setRoles] = useState({})
  const run = jobRunner(p.name, setJob, setErr)
  const setH = (i, k, v) => setHosts(hs => hs.map((h, j) => j === i ? { ...h, [k]: v } : h))
  const hostnames = watch ? watch.hosts.map(h => h.hostname) : []

  const build = () => {
    const body = mode === 'yaml' ? { nodes_config: yamlText } : { hosts: hosts.map(({ adv, ...h }) => ({ ...h, nmstate: adv ? h.nmstate : '' })) }
    const n = mode === 'yaml' ? 'the pasted hosts' : hosts.map(h => h.hostname).join(', ')
    return run('/extra-nodes/build', body, `Build a node ISO for ${n}? It runs a temporary pod in the cluster and takes several minutes.`)
  }
  const loadCsrs = async () => {
    if (!watch) return
    try { setCsr(await api.post(`/api/clusters/${p.name}/extra-nodes/csrs`, { hostnames })) } catch (e) { setErr(e.message) }
  }
  useEffect(() => { setCsr(null); loadCsrs(); if (!watch) return; const t = setInterval(loadCsrs, 15000); return () => clearInterval(t) }, [watch?.id])
  const approve = async (c) => {
    setErr('')
    if (!confirm(`Approve the ${c.kind} certificate request for ${c.node}?`)) return
    try { await api.post(`/api/clusters/${p.name}/extra-nodes/csrs/${c.name}/approve`, { hostnames }); loadCsrs() } catch (e) { setErr(e.message) }
  }
  const label = async (node) => {
    setErr('')
    const role = (roles[node] || '').trim()
    try { await api.post(`/api/clusters/${p.name}/extra-nodes/label`, { node, role, hostnames }); loadCsrs() } catch (e) { setErr(e.message) }
  }
  const del = async (b) => {
    if (!confirm(`Delete ISO ${b.id}? It cannot be downloaded again.`)) return
    try { await api.del(`/api/extra-isos/${b.id}`); if (watch?.id === b.id) setWatch(null); reload() } catch (e) { setErr(e.message) }
  }
  const monitor = (b) => { setWatch(b); run('/extra-nodes/monitor', { ips: b.hosts.flatMap(h => h.ips) }, null) }
  const done = () => reload()

  if (!d) return <div className="panel"><h2>Add Extra nodes</h2><Alert kind="error">{err}</Alert><p className="muted">Loading…</p></div>
  return (
    <div>
      <div className="panel">
        <h2>Add Extra nodes</h2>
        <p className="lead">Add bare-metal (or any) machines to this cluster with <span className="mono">oc adm node-image</span>: describe the hosts, build a node ISO, boot each machine from it, watch it join and approve its certificates. Static IP addressing.</p>
        <Alert kind="error">{err}</Alert>
        {!d.supported && <Alert kind="warn">{d.read_only ? 'Read-only connection: building ISOs and approving certificates are disabled.' : d.reason}</Alert>}
        {d.supported && (<>
          <RadioCards value={mode} onChange={setMode} options={[
            { value: 'form', label: 'Describe hosts', desc: 'One row per machine: hostname, the MAC of the NIC on the machine network, static IP, gateway, DNS and install disk.' },
            { value: 'yaml', label: 'Paste nodes-config.yaml', desc: 'Full control (bonds, VLANs, several NICs) with the format from the OpenShift documentation.' },
          ]} />
          {mode === 'form' && (
            <div style={{ marginTop: 14, display: 'grid', gap: 12 }}>
              {hosts.map((h, i) => (
                <div className="cert" key={i}>
                  <div className="row"><b>Host {i + 1}</b><span className="spacer" />{hosts.length > 1 && <button className="small" onClick={() => setHosts(hs => hs.filter((_, j) => j !== i))}>Remove</button>}</div>
                  <div className="grid4">
                    <Field label="Hostname" help="becomes the node name"><Text value={h.hostname} onChange={v => setH(i, 'hostname', v)} placeholder="extra-worker-1" /></Field>
                    <Field label="MAC address" help="NIC on the machine network"><Text value={h.mac} onChange={v => setH(i, 'mac', v)} placeholder="00:11:22:33:44:55" /></Field>
                    <Field label="IP address"><Text value={h.ip} onChange={v => setH(i, 'ip', v)} placeholder="192.168.1.50" /></Field>
                    <Field label="Prefix length"><Num value={h.prefix} onChange={v => setH(i, 'prefix', v)} /></Field>
                    <Field label="Gateway"><Text value={h.gateway} onChange={v => setH(i, 'gateway', v)} /></Field>
                    <Field label="DNS servers" help="comma separated"><Text value={h.dns} onChange={v => setH(i, 'dns', v)} /></Field>
                    <Field label="Install disk" help="root device hint"><Text value={h.root_device} onChange={v => setH(i, 'root_device', v)} /></Field>
                    <Field label="Interface name" help="any name; matched by MAC"><Text value={h.interface} onChange={v => setH(i, 'interface', v)} /></Field>
                  </div>
                  <label className="row help"><input type="checkbox" checked={h.adv} onChange={e => setH(i, 'adv', e.target.checked)} /> Custom NMState network configuration (replaces the IP, gateway and DNS fields)</label>
                  {h.adv && <textarea style={{ minHeight: 140 }} value={h.nmstate} onChange={e => setH(i, 'nmstate', e.target.value)} placeholder={'interfaces:\n- name: bond0\n  type: bond\n  state: up\n  ...'} />}
                </div>
              ))}
              <div className="row"><button onClick={() => setHosts(hs => [...hs, { ...blankHost(spec), prefix: hs[hs.length - 1].prefix, gateway: hs[hs.length - 1].gateway, dns: hs[hs.length - 1].dns }])}>+ Add host</button>
                <span className="help">All hosts share one ISO; each machine picks its own settings by MAC address.</span></div>
            </div>
          )}
          {mode === 'yaml' && <Field label="nodes-config.yaml"><textarea style={{ minHeight: 260, marginTop: 12 }} value={yamlText} onChange={e => setYamlText(e.target.value)} placeholder={'hosts:\n- hostname: extra-worker-1\n  rootDeviceHints:\n    deviceName: /dev/sda\n  interfaces:\n  - macAddress: 00:00:00:00:00:00\n    name: eth0\n  networkConfig:\n    interfaces:\n    - name: eth0\n      type: ethernet\n      state: up\n      mac-address: 00:00:00:00:00:00\n      ipv4:\n        enabled: true\n        address:\n        - ip: 192.168.122.2\n          prefix-length: 23\n        dhcp: false'} /></Field>}
          <div className="toolbar"><span className="help">The ISO is written to the installer host and kept until you delete it (it contains the cluster's join details: treat it as a secret).</span><span className="spacer" />
            <button className="primary" onClick={build} disabled={!!d.running_job || (mode === 'yaml' ? !yamlText.trim() : hosts.some(h => !h.hostname || !h.mac || (!h.adv && !h.ip)))}>Build node ISO</button></div>
        </>)}
        {job && <div style={{ marginTop: 10 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>

      <div className="panel">
        <h2>Node ISOs</h2>
        {d.builds.length === 0 ? <p className="muted">No ISOs built for this cluster yet.</p> : (
          <table className="tbl"><thead><tr><th>Built</th><th>Hosts</th><th>Status</th><th>Size</th><th></th></tr></thead>
            <tbody>{d.builds.map(b => <tr key={b.id}>
              <td className="help">{fmtDate(b.created)}</td>
              <td>{b.hosts.map(h => <div key={h.hostname} className="mono">{h.hostname} {h.ips.join(', ')} <span className="help">{h.macs.join(', ')}</span></div>)}</td>
              <td><Badge s={b.status === 'ready' ? 'pass' : b.status === 'failed' ? 'fail' : 'running'} /></td>
              <td>{b.size_mb ? `${b.size_mb} MB` : ''}</td>
              <td className="row end">{b.status === 'ready' && <a className="btn small primary" href={`/api/extra-isos/${b.id}/download`}>Download ISO</a>}
                {b.status === 'ready' && d.supported && <button className="small" onClick={() => monitor(b)} disabled={!!d.running_job || b.hosts.every(h => !h.ips.length)}>Watch installation</button>}
                <button className="small danger" onClick={() => del(b)}>Delete ISO</button></td>
            </tr>)}</tbody></table>
        )}
      </div>

      {watch && (
        <div className="panel">
          <div className="row"><h2 style={{ margin: 0 }}>Joining: {hostnames.join(', ')}</h2><span className="spacer" /><button onClick={loadCsrs}>Refresh</button><button onClick={() => setWatch(null)}>Close</button></div>
          <p className="lead" style={{ marginTop: 6 }}>Boot each machine from the ISO. The monitor job above reports validations and progress. Each node then asks for two certificates (client, then serving): approve them below. Only requests for these hosts are shown.</p>
          <h3>Pending certificate requests</h3>
          {!csr ? <p className="muted">Checking…</p> : csr.csrs.length === 0 ? <p className="muted">None pending for these hosts.{csr.other_pending ? ` (${csr.other_pending} other pending request(s) not shown.)` : ''}</p> : (
            <table className="tbl"><thead><tr><th>Node</th><th>Type</th><th>Requested by</th><th>Age</th><th></th></tr></thead>
              <tbody>{csr.csrs.map(c => <tr key={c.name}><td className="mono">{c.node}</td><td>{c.kind}</td><td className="mono help">{c.requestor}</td><td className="help">{c.age}</td>
                <td><button className="small primary" onClick={() => approve(c)}>Approve</button></td></tr>)}</tbody></table>
          )}
          <h3>Nodes</h3>
          {!csr || csr.nodes.length === 0 ? <p className="muted">Not joined yet.</p> : (
            <table className="tbl"><thead><tr><th>Node</th><th>Ready</th><th>Roles</th><th>IP</th><th>Add role</th></tr></thead>
              <tbody>{csr.nodes.map(n => <tr key={n.name}><td className="mono">{n.name}</td><td><Badge s={n.ready === 'True' ? 'pass' : 'warn'} /></td><td>{n.roles.join(', ')}</td><td className="mono">{n.ip}</td>
                <td><div className="row"><Text value={roles[n.name] || ''} onChange={v => setRoles({ ...roles, [n.name]: v })} placeholder="infra" style={{ maxWidth: 140 }} /><button className="small" onClick={() => label(n.name)} disabled={!(roles[n.name] || '').trim()}>Apply</button></div></td></tr>)}</tbody></table>
          )}
        </div>
      )}
    </div>
  )
}
