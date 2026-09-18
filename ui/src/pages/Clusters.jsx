import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Field, Text, Select, Alert, RadioCards } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import Connect from '../components/Connect.jsx'

export default function Clusters() {
  const [list, setList] = useState([])
  const [sys, setSys] = useState(null)
  const [form, setForm] = useState({ name: '', base_domain: '', install_method: 'ipi' })
  const [tpl, setTpl] = useState({ source: '', name: '', base_domain: '', first_ip: '', machine_cidr: '', gateway: '', yaml: '' })
  const [templates, setTemplates] = useState([])
  const [isos, setIsos] = useState([])
  const [err, setErr] = useState('')
  const nav = useNavigate()

  const load = () => { api.get('/api/clusters').then(setList).catch(e => setErr(e.message)); api.get('/api/templates').then(setTemplates).catch(() => {}); api.get('/api/extra-isos').then(setIsos).catch(() => {}) }
  const delIso = async (id) => { if (!confirm(`Delete ISO ${id}?`)) return; try { await api.del(`/api/extra-isos/${id}`); load() } catch (e) { setErr(e.message) } }
  useEffect(() => { load(); api.get('/api/system').then(setSys).catch(() => {}) }, [])
  const fromTemplate = async () => {
    setErr('')
    const body = { name: tpl.name, base_domain: tpl.base_domain || null, first_ip: tpl.first_ip || null, machine_cidr: tpl.machine_cidr || null, gateway: tpl.gateway || null }
    if (tpl.source === 'yaml') body.yaml = tpl.yaml
    else if (tpl.source.startsWith('cluster:')) body.from_cluster = tpl.source.slice(8)
    else if (tpl.source.startsWith('template:')) body.template = tpl.source.slice(9)
    else { setErr('Pick a template, a cluster to clone, or paste YAML'); return }
    try { await api.post('/api/clusters/import', body); nav(`/clusters/${tpl.name}/basics`) } catch (e) { setErr(e.message) }
  }
  const delTemplate = async (n) => { if (!confirm(`Delete template ${n}?`)) return; try { await api.del(`/api/templates/${n}`); load() } catch (e) { setErr(e.message) } }
  const sources = [{ value: '', label: 'select…' }, ...templates.map(t => ({ value: `template:${t.name}`, label: `Template: ${t.name} (${t.install_method} ${t.topology}, ${t.nodes} nodes${t.has_secrets ? ', with secrets' : ''})` })),
    ...list.filter(c => !c.imported).map(c => ({ value: `cluster:${c.name}`, label: `Clone cluster: ${c.name} (${c.install_method}, ${c.ocp_version || 'no version'})` })), { value: 'yaml', label: 'Paste template YAML' }]

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
  const disconnect = async (n) => {
    if (!confirm(`Disconnect ${n}? Its credentials are dropped from memory${list.find(c => c.name === n)?.method === 'password' ? ' and the login token is revoked' : ''}.`)) return
    try { await api.del(`/api/imported/${n}`); load() } catch (e) { setErr(e.message) }
  }
  const [showConnect, setShowConnect] = useState(false)

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
        <p className="lead">{list.length ? `${list.filter(c => !c.imported).length} cluster profile${list.filter(c => !c.imported).length === 1 ? '' : 's'}${list.some(c => c.imported) ? `, ${list.filter(c => c.imported).length} connected` : ''}` : 'No clusters yet — create one below, or connect to an existing cluster.'}</p>
        {list.length > 0 && (
          <div className="cards">
            {list.map(c => c.imported ? (
              <div className="card stripe imported" key={c.name}>
                <h3><Link to={`/clusters/${c.name}/health`}>{c.name}</Link> <span className="tag info">Connected</span>{c.read_only && <span className="tag">Read-only</span>}</h3>
                <dl className="kv">
                  <dt>API</dt><dd className="mono">{c.server}</dd>
                  <dt>Signed in as</dt><dd>{c.user} <span className="help">({{ kubeconfig: 'kubeconfig', password: 'password login', token: 'token' }[c.method]})</span></dd>
                  <dt>Version</dt><dd>{c.ocp_version}{c.platform ? ` · ${c.platform}` : ''}</dd>
                  {c.expires && <><dt>Expires</dt><dd>{new Date(c.expires * 1000).toLocaleString()}</dd></>}
                </dl>
                <div className="row end" style={{ marginTop: 6 }}>
                  <button className="small" onClick={() => disconnect(c.name)}>Disconnect</button>
                  <span className="spacer" />
                  <Link className="btn primary" to={`/clusters/${c.name}/health`}>Open</Link>
                </div>
              </div>
            ) : (
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
        <div className="row"><h2 style={{ margin: 0 }}>Connect to an existing cluster</h2><span className="spacer" />
          <button onClick={() => setShowConnect(!showConnect)}>{showConnect ? 'Close' : 'Connect…'}</button></div>
        <p className="lead" style={{ marginTop: 6 }}>Manage an OpenShift 4 cluster this console did not install: health, upgrades, scaling, identity, certificates, storage, operators, apps and backups. Session only — nothing about it is stored.</p>
        {showConnect && <Connect onConnected={r => { setShowConnect(false); nav(`/clusters/${r.name}/health`) }} />}
      </div>
      {isos.length > 0 && (
        <div className="panel">
          <h2>Files kept on the installer host</h2>
          <p className="lead">Node ISOs (Add Extra nodes) and must-gather archives. They stay here, also after a connection ends, until you delete them.</p>
          <table className="tbl"><thead><tr><th>Cluster</th><th>Created</th><th>Contents</th><th>Size</th><th></th></tr></thead>
            <tbody>{isos.map(b => <tr key={b.id}><td>{b.cluster}<div className="help mono">{b.server}</div></td><td className="help">{b.created.replace('T', ' ').slice(0, 16)}</td>
              <td className="mono">{b.kind === 'mustgather' ? `must-gather (${b.note})` : `node ISO: ${b.hosts.map(h => h.hostname).join(', ')}`}</td><td>{b.size_mb ? `${b.size_mb} MB` : b.status}</td>
              <td className="row end">{b.file && <a className="btn small" href={`/api/extra-isos/${b.id}/download`}>Download</a>}<button className="small danger" onClick={() => delIso(b.id)}>Delete</button></td></tr>)}</tbody></table>
        </div>
      )}
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
      <div className="panel">
        <h2>New cluster from a template</h2>
        <p className="lead">Clone an existing cluster or start from a saved template: everything except the name, domain and node addresses carries over (vCenter, network, load balancer layout, node table, pools, proxy, day-2 settings). Secrets carry over when cloning a cluster or when the template was saved with them.</p>
        <div className="grid3">
          <Field label="Source"><Select value={tpl.source} onChange={v => setTpl({ ...tpl, source: v })} options={sources} /></Field>
          <Field label="New cluster name"><Text value={tpl.name} onChange={v => setTpl({ ...tpl, name: v })} placeholder="ocp2" /></Field>
          <Field label="Base domain" help="empty = same as the template"><Text value={tpl.base_domain} onChange={v => setTpl({ ...tpl, base_domain: v })} /></Field>
          <Field label="First node IP" help="Renumbers every node sequentially in table order; empty keeps the template's IPs"><Text value={tpl.first_ip} onChange={v => setTpl({ ...tpl, first_ip: v })} placeholder="10.0.20.20" /></Field>
          <Field label="Machine network CIDR" help="empty = same as the template"><Text value={tpl.machine_cidr} onChange={v => setTpl({ ...tpl, machine_cidr: v })} /></Field>
          <Field label="Gateway" help="empty = same as the template"><Text value={tpl.gateway} onChange={v => setTpl({ ...tpl, gateway: v })} /></Field>
        </div>
        {tpl.source === 'yaml' && <Field label="Template YAML" help="As exported from the Cluster & version step of any ocpdeploy host"><textarea style={{ minHeight: 160 }} value={tpl.yaml} onChange={e => setTpl({ ...tpl, yaml: e.target.value })} /></Field>}
        <div className="row end" style={{ marginTop: 14 }}>
          <span className="help">Afterwards: check the vCenter/Infrastructure, Load balancer and DNS steps, paste secrets if the template had none, run pre-flight, deploy.</span>
          <span className="spacer" />
          <button className="primary" onClick={fromTemplate} disabled={!tpl.name || !tpl.source}>Create from template</button>
        </div>
        {templates.length > 0 && (
          <>
            <h3>Template library <span className="help">— {sys?.root}/templates</span></h3>
            <table className="tbl"><thead><tr><th>Name</th><th>From</th><th>Method</th><th>Topology</th><th>Version</th><th>Nodes</th><th>Secrets</th><th></th></tr></thead>
              <tbody>{templates.map(t => <tr key={t.name}><td className="mono">{t.name}</td><td>{t.source}</td><td>{t.install_method}{t.provider && t.provider !== 'vsphere' ? ` / ${t.provider}` : ''}</td><td>{t.topology}</td><td>{t.ocp_version}</td><td>{t.nodes}</td><td>{t.has_secrets ? <Badge s="warn" /> : <span className="help">no</span>}</td>
                <td className="row end"><a className="btn small" href={`/api/templates/${t.name}`} download={`${t.name}.yaml`}>Download</a><button className="small danger" onClick={() => delTemplate(t.name)}>Delete</button></td></tr>)}</tbody></table>
          </>
        )}
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
