import { useState } from 'react'
import { api, MASK } from '../api.js'
import { Field, Text, Num, Select, Alert, RadioCards } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import CheckTable from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'

const emptyVM = (role) => ({ role, host: '', ip: '', ssh_user: 'root', ssh_password: '', ssh_port: 22 })

export default function LoadBalancer(p) {
  const { spec, update } = p
  const lb = spec.lb
  const [err, setErr] = useState('')
  const [probe, setProbe] = useState(null)
  const [inspect, setInspect] = useState(null)
  const [render, setRender] = useState(null)
  const [job, setJob] = useState(null)
  const [backends, setBackends] = useState(null)
  const masters = spec.nodes.filter(n => n.role === 'master')
  const sno = spec.topology === 'sno' || masters.length === 1

  const setLB = (k, v) => update(s => s.lb[k] = v)
  const setVM = (i, k, v) => update(s => s.lb.vms[i][k] = v)
  const addVM = () => update(s => s.lb.vms.push(emptyVM(s.lb.layout === 'ha' ? 'both' : (s.lb.vms.some(v => v.role === 'api') ? 'apps' : 'api'))))
  const delVM = (i) => update(s => s.lb.vms.splice(i, 1))
  const setLayout = (v) => update(s => { s.lb.layout = v; if (v === 'ha') s.lb.vms.forEach(x => x.role = 'both') })

  const guard = async (fn) => { setErr(''); try { if (p.dirty) { if (!(await p.save())) return } await fn() } catch (e) { setErr(e.message) } }
  const doProbe = () => guard(async () => setProbe(await api.post(`/api/clusters/${p.name}/lb/probe`)))
  const doInspect = () => guard(async () => setInspect(await api.post(`/api/clusters/${p.name}/lb/inspect`)))
  const doRender = () => guard(async () => setRender(await api.get(`/api/clusters/${p.name}/lb/render`)))
  const doBackends = () => guard(async () => setBackends(await api.get(`/api/clusters/${p.name}/lb/backends`)))
  const doPush = () => guard(async () => {
    if (!confirm('Install/configure HAProxy on the listed VMs now? Existing frontends on the cluster ports are moved aside (backup kept), other sections are preserved.')) return
    const r = await api.post(`/api/clusters/${p.name}/lb/push`); setJob(r.job_id)
  })

  const modes = [
    { value: 'haproxy', label: 'HAProxy managed by this app', desc: 'Give SSH access to one or two Enterprise Linux VMs. The app installs HAProxy, writes /etc/haproxy/conf.d/ocp-<cluster>.cfg, validates and reloads. Other services on the same HAProxy are preserved.' },
    { value: 'external', label: 'External, pre-configured', desc: 'You run your own balancer (F5, NSX, another HAProxy…). Paste the IP and FQDN; the app validates DNS and ports and tells you which backends to configure.' },
  ]
  if (sno || lb.mode === 'none') modes.push({ value: 'none', label: 'No load balancer (single node)', desc: 'DNS for api, api-int and *.apps points straight at the node. Only valid for a single-node cluster.' })

  return (
    <div>
      <div className="panel">
        <h2>Load balancer</h2>
        <p className="lead">API traffic (6443, 22623) and application ingress (80, 443) must be balanced across the nodes. Choose how.{sno ? ' A single-node cluster can skip the balancer entirely.' : ''}</p>
        <Alert kind="error">{err}</Alert>
        <RadioCards value={lb.mode} onChange={v => setLB('mode', v)} options={modes} />

        {lb.mode === 'none' && (
          <div>
            {!sno && <Alert kind="error">"No load balancer" is only valid for a single-node cluster; this cluster has {masters.length} masters.</Alert>}
            <Alert kind="info">Create A records for <span className="mono">api.{spec.name}.{spec.base_domain}</span>, <span className="mono">api-int.{spec.name}.{spec.base_domain}</span> and <span className="mono">*.apps.{spec.name}.{spec.base_domain}</span> pointing at <b>{masters[0]?.ip || 'the node IP'}</b>. The DNS step lists them.</Alert>
          </div>
        )}

        {lb.mode === 'haproxy' && (
          <div>
            <h3>Layout</h3>
            <RadioCards value={lb.layout} onChange={setLayout} options={[
              { value: 'split', label: 'Role split (API VM + apps VM)', desc: 'One VM serves the API ports, the other the ingress ports. DNS points directly at the VM IPs. No redundancy.' },
              { value: 'ha', label: 'HA pair with keepalived', desc: 'Both VMs serve all four ports and share two floating IPs (VRRP). DNS points at the floating IPs.' },
            ]} />
            {lb.layout === 'ha' && (
              <div className="grid3" style={{ marginTop: 12 }}>
                <Field label="API floating IP" help="api / api-int DNS → this"><Text value={lb.ha.api_vip} onChange={v => update(s => s.lb.ha.api_vip = v)} /></Field>
                <Field label="Apps floating IP" help="*.apps DNS → this"><Text value={lb.ha.apps_vip} onChange={v => update(s => s.lb.ha.apps_vip = v)} /></Field>
                <Field label="Interface (optional)" help="Auto-detected from the default route when empty"><Text value={lb.ha.interface} onChange={v => update(s => s.lb.ha.interface = v)} placeholder="ens192" /></Field>
              </div>
            )}
            <h3>HAProxy VMs</h3>
            <table className="tbl">
              <thead><tr><th>Role</th><th>SSH host</th><th>Service IP</th><th>User</th><th>Password</th><th>Port</th><th></th></tr></thead>
              <tbody>
                {lb.vms.map((vm, i) => (
                  <tr key={i}>
                    <td>{lb.layout === 'ha' ? 'both' : <Select value={vm.role} onChange={v => setVM(i, 'role', v)} options={['api', 'apps', 'both']} />}</td>
                    <td><Text value={vm.host} onChange={v => setVM(i, 'host', v)} placeholder="10.0.10.5" /></td>
                    <td><Text value={vm.ip} onChange={v => setVM(i, 'ip', v)} placeholder="same as host" /></td>
                    <td><Text value={vm.ssh_user} onChange={v => setVM(i, 'ssh_user', v)} /></td>
                    <td><Text type="password" value={vm.ssh_password} onChange={v => setVM(i, 'ssh_password', v)} placeholder={vm.ssh_password === MASK ? 'stored' : 'empty = use installer SSH key'} /></td>
                    <td style={{ width: 80 }}><Num value={vm.ssh_port} onChange={v => setVM(i, 'ssh_port', v)} /></td>
                    <td><button className="small" onClick={() => delVM(i)} title="remove">✕</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="toolbar">
              <button onClick={addVM} disabled={lb.vms.length >= 2}>+ Add VM</button>
              <span className="help">Non-root users need passwordless-or-password sudo. Empty password uses the installer's SSH key.</span>
            </div>
            <div className="toolbar">
              <button onClick={doInspect}>Inspect VMs</button>
              <button onClick={doRender}>Preview generated config</button>
              <button onClick={doProbe}>Probe ports</button>
              <span className="spacer" />
              <button className="primary" onClick={doPush} disabled={!lb.vms.length}>Install & push configuration</button>
            </div>
            {inspect && inspect.map(h => (
              <div className="alert info" key={h.host}>
                <b>{h.host}</b> {h.error ? <span className="mono"> — {h.error}</span> : <>
                  <span className="muted"> {h.os} · {h.haproxy} · {h.active || 'inactive'} · keepalived: {h.keepalived}</span><br />
                  listening: <span className="mono">{h.listening.join(' ')}</span> · conf.d: <span className="mono">{h.confd.join(' ') || '(empty)'}</span>
                </>}
              </div>
            ))}
            {render && Object.entries(render).map(([h, cfg]) => (
              <div key={h}><h3>{h} → /etc/haproxy/conf.d/ocp-{spec.name}.cfg</h3><pre className="code">{cfg}</pre></div>
            ))}
            {job && <div style={{ marginTop: 10 }}><JobLog cluster={p.name} jobId={job} onDone={() => p.reload()} /></div>}
          </div>
        )}

        {lb.mode === 'external' && (
          <div>
            <h3>Endpoints</h3>
            <div className="grid2">
              <Field label="API FQDN" help={`Normally api.${spec.name}.${spec.base_domain}`}><Text value={lb.external_api.fqdn} onChange={v => update(s => s.lb.external_api.fqdn = v)} /></Field>
              <Field label="API IP"><Text value={lb.external_api.ip} onChange={v => update(s => s.lb.external_api.ip = v)} /></Field>
              <Field label="Apps FQDN" help={`Normally *.apps.${spec.name}.${spec.base_domain}`}><Text value={lb.external_apps.fqdn} onChange={v => update(s => s.lb.external_apps.fqdn = v)} /></Field>
              <Field label="Apps IP"><Text value={lb.external_apps.ip} onChange={v => update(s => s.lb.external_apps.ip = v)} /></Field>
            </div>
            <div className="toolbar">
              <button onClick={doBackends}>Show required backend pools</button>
              <button onClick={doProbe}>Probe ports</button>
            </div>
            {backends && (
              <div className="alert info">
                <b>Configure these pools on your load balancer</b> (TCP passthrough, health check on the port; API pool: HTTPS GET /readyz):
                <div className="grid2" style={{ marginTop: 8 }}>
                  <div><b>API — {lb.external_api.ip}:6443 and :22623</b><ul className="hint-list">{backends.api.map(b => <li key={b.name} className="mono">{b.name} {b.ip}</li>)}</ul></div>
                  <div><b>Apps — {lb.external_apps.ip}:80 and :443</b><ul className="hint-list">{backends.apps.map(b => <li key={b.name} className="mono">{b.name} {b.ip}</li>)}</ul></div>
                </div>
                <span className="help">Keep the bootstrap node in the API pool until the install finishes, then remove it (Operate → Remove bootstrap).</span>
              </div>
            )}
          </div>
        )}
        {probe && <div style={{ marginTop: 10 }}><CheckTable rows={probe} /></div>}
      </div>
      <Footer {...p} />
    </div>
  )
}
