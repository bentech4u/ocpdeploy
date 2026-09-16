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
  const badge = (s) => s === 'installed' ? 'pass' : s === 'failed' ? 'fail' : s === 'deploying' ? 'running' : 'grey'

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Clusters</h1>
          <p>Each cluster is a folder under <span className="mono">{sys?.clusters_dir}</span> holding its spec, generated assets, logs and state.</p>
        </div>
      </div>
      <Alert kind="error">{err}</Alert>
      <div className="panel">
        <h2>Your clusters</h2>
        <p className="lead">{list.length ? `${list.length} cluster profile${list.length > 1 ? 's' : ''}` : 'No clusters yet — create one below.'}</p>
        {list.length > 0 && (
          <div className="cards">
            {list.map(c => (
              <div className={`card stripe ${c.status}`} key={c.name}>
                <h3><Link to={`/clusters/${c.name}/basics`}>{c.name}</Link> <Badge s={badge(c.status)} /></h3>
                <dl className="kv">
                  <dt>Domain</dt><dd>{c.name}.{c.base_domain}</dd>
                  <dt>Version</dt><dd>{c.ocp_version || '—'}</dd>
                  <dt>Method</dt><dd>{c.install_method === 'ipi' ? 'IPI (installer-provisioned)' : 'Agent-based (UPI)'}</dd>
                  <dt>Status</dt><dd>{c.status}</dd>
                </dl>
                <div className="row end" style={{ marginTop: 6 }}>
                  <button className="danger small" onClick={() => remove(c.name)}>Delete profile</button>
                  <span className="spacer" />
                  <Link className="btn" to={`/clusters/${c.name}/operate`}>Operate</Link>
                  <Link className="btn primary" to={`/clusters/${c.name}/basics`}>Open</Link>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="panel">
        <h2>New cluster</h2>
        <p className="lead">Pick a name and base domain, choose how the cluster is installed, then walk through the wizard.</p>
        <div className="grid2">
          <Field label="Cluster name" help="Lowercase DNS label, e.g. ocp1. Becomes <name>.<base domain>.">
            <Text value={form.name} onChange={v => setForm({ ...form, name: v })} placeholder="ocp1" />
          </Field>
          <Field label="Base domain" help="Zone in your DNS server, e.g. example.com">
            <Text value={form.base_domain} onChange={v => setForm({ ...form, base_domain: v })} placeholder="example.com" />
          </Field>
        </div>
        <h3>Install method</h3>
        <RadioCards value={form.install_method} onChange={v => setForm({ ...form, install_method: v })} options={[
          { value: 'ipi', label: 'IPI — installer-provisioned on vSphere', desc: 'The installer creates and deletes VMs through vCenter. Static IPs, your own load balancer. Needs 4.15+. Cleanest destroy/retry.' },
          { value: 'agent', label: 'Agent-based (UPI)', desc: 'The app builds a bootable ISO, uploads it to a datastore and creates the VMs. No machine API integration; extra nodes are added with node ISOs.' },
        ]} />
        <div className="row end" style={{ marginTop: 16 }}>
          <button className="primary" onClick={create} disabled={!form.name || !form.base_domain}>Create cluster</button>
        </div>
      </div>
      {sys && (
        <div className="panel">
          <h2>Installer host</h2>
          <div className="stat-grid">
            <div className="stat"><div className="k">Host</div><div className="v">{sys.hostname}</div></div>
            <div className="stat"><div className="k">DNS servers</div><div className="v">{sys.dns_servers.join(', ') || '—'}</div></div>
            <div className="stat"><div className="k">Tools cached</div><div className="v">{sys.installed_tool_versions.join(', ') || <small>none yet</small>}</div></div>
            <div className="stat"><div className="k">App version</div><div className="v">{sys.app_version}</div></div>
          </div>
          <dl className="kv">
            <dt>SSH key</dt><dd className="mono">{sys.ssh_public_key ? sys.ssh_public_key.slice(0, 60) + '…' : 'none'}</dd>
            <dt>Clusters dir</dt><dd className="mono">{sys.clusters_dir}</dd>
          </dl>
        </div>
      )}
      <div className="footer-note">ocpdeploy · OpenShift on vSphere install console · <a href="https://github.com/bentech4u/ocpdeploy" target="_blank" rel="noreferrer">GitHub</a> · <a href="https://buymeacoffee.com/bentech4u" target="_blank" rel="noreferrer">☕ Buy me a coffee</a></div>
    </div>
  )
}
