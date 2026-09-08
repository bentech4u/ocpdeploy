import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Field, Text, Alert, RadioCards } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'

export default function Clusters() {
  const [list, setList] = useState([])
  const [sys, setSys] = useState(null)
  const [form, setForm] = useState({ name: '', base_domain: '', install_method: 'ipi' })
  const [err, setErr] = useState('')
  const nav = useNavigate()

  const load = () => api.get('/api/clusters').then(setList).catch(e => setErr(e.message))
  useEffect(() => { load(); api.get('/api/system').then(setSys).catch(() => {}) }, [])

  const create = async () => {
    setErr('')
    try {
      await api.post('/api/clusters', form)
      nav(`/clusters/${form.name}/basics`)
    } catch (e) { setErr(e.message) }
  }
  const remove = async (n) => {
    if (!confirm(`Delete the profile for ${n}? This removes the folder under clusters/ (it does not touch vCenter).`)) return
    try { await api.del(`/api/clusters/${n}`); load() } catch (e) { setErr(e.message) }
  }

  return (
    <div className="page">
      <Alert kind="error">{err}</Alert>
      <div className="panel">
        <h2>Clusters</h2>
        <p className="lead">Each cluster is a folder under <span className="mono">{sys?.clusters_dir}</span> holding its spec, generated assets, logs and state.</p>
        {list.length === 0 && <p className="muted">No clusters yet.</p>}
        <div className="cards">
          {list.map(c => (
            <div className="card" key={c.name}>
              <h3><Link to={`/clusters/${c.name}/basics`}>{c.name}</Link> <Badge s={c.status === 'installed' ? 'pass' : c.status === 'failed' ? 'fail' : 'grey'} /></h3>
              <dl className="kv">
                <dt>Domain</dt><dd>{c.name}.{c.base_domain}</dd>
                <dt>Version</dt><dd>{c.ocp_version || '—'}</dd>
                <dt>Method</dt><dd>{c.install_method === 'ipi' ? 'IPI (installer-provisioned)' : 'Agent-based (UPI)'}</dd>
                <dt>Status</dt><dd>{c.status}</dd>
              </dl>
              <div className="row end" style={{ marginTop: 8 }}>
                <button className="danger" onClick={() => remove(c.name)}>Delete profile</button>
                <Link className="btn" to={`/clusters/${c.name}/operate`}>Operate</Link>
                <Link className="btn" to={`/clusters/${c.name}/basics`}>Open</Link>
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="panel">
        <h2>New cluster</h2>
        <div className="grid2">
          <Field label="Cluster name" help="Lowercase DNS label, e.g. homeshift. Becomes <name>.<base domain>.">
            <Text value={form.name} onChange={v => setForm({ ...form, name: v })} placeholder="homeshift" />
          </Field>
          <Field label="Base domain" help="Zone in your DNS server, e.g. bentech.work">
            <Text value={form.base_domain} onChange={v => setForm({ ...form, base_domain: v })} placeholder="bentech.work" />
          </Field>
        </div>
        <h3>Install method</h3>
        <RadioCards value={form.install_method} onChange={v => setForm({ ...form, install_method: v })} options={[
          { value: 'ipi', label: 'IPI — installer-provisioned on vSphere', desc: 'The installer creates and deletes VMs through vCenter. Static IPs, your own load balancer. Needs 4.15+. Cleanest destroy/retry.' },
          { value: 'agent', label: 'Agent-based (UPI)', desc: 'The app builds a bootable ISO, uploads it to a datastore and creates the VMs. No machine API integration; infra nodes are added with node ISOs.' },
        ]} />
        <div className="row end" style={{ marginTop: 14 }}>
          <button className="primary" onClick={create} disabled={!form.name || !form.base_domain}>Create</button>
        </div>
      </div>
      {sys && (
        <div className="panel">
          <h2>Installer host</h2>
          <dl className="kv">
            <dt>Host</dt><dd>{sys.hostname}</dd>
            <dt>DNS servers</dt><dd>{sys.dns_servers.join(', ')}</dd>
            <dt>Tools cached</dt><dd>{sys.installed_tool_versions.join(', ') || 'none yet'}</dd>
            <dt>SSH key</dt><dd className="mono">{sys.ssh_public_key.slice(0, 60)}…</dd>
          </dl>
        </div>
      )}
    </div>
  )
}
