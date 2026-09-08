import { useState } from 'react'
import { api, MASK } from '../api.js'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import CheckTable from '../components/CheckTable.jsx'

export default function VCenter(p) {
  const { spec, update } = p
  const vc = spec.vcenter
  const set = (k, v) => update(s => s.vcenter[k] = v)
  const [cert, setCert] = useState(null)
  const [inv, setInv] = useState(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [about, setAbout] = useState(null)
  const [checks, setChecks] = useState(null)

  const creds = { host: vc.host, username: vc.username, password: vc.password, cluster: p.name }

  const fetchCert = async () => {
    setErr(''); setBusy('cert')
    try { setCert(await api.post('/api/vcenter/cert', { host: vc.host })) } catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const accept = () => { update(s => { s.vcenter.cert_pem = cert.trust_pem || cert.pem; s.vcenter.cert_thumbprint = cert.sha1 }) }
  const test = async () => {
    setErr(''); setBusy('test')
    try { setAbout(await api.post('/api/vcenter/test', creds)) } catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const load = async () => {
    setErr(''); setBusy('inv')
    try { const i = await api.post('/api/vcenter/inventory', creds); setInv(i); setAbout(i.about) } catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const check = async () => {
    setErr(''); setBusy('check')
    try { if (p.dirty) await p.save(); setChecks(await api.post(`/api/clusters/${p.name}/vcenter/check`)) } catch (e) { setErr(e.message) } finally { setBusy('') }
  }

  const dc = inv?.datacenters.find(d => d.name === vc.datacenter)
  const cl = dc?.clusters.find(c => c.name === vc.cluster)

  return (
    <div>
      <div className="panel">
        <h2>vCenter</h2>
        <p className="lead">{spec.install_method === 'ipi'
          ? 'The installer uses these credentials to create the cluster VMs and for the in-cluster vSphere integration (CSI, machine API).'
          : 'Used by this app to upload the agent ISO and create the node VMs. The cluster itself is installed with platform "none".'}</p>
        <Alert kind="error">{err}</Alert>
        <div className="grid3">
          <Field label="vCenter host" help="FQDN preferred; it is written into install-config."><Text value={vc.host} onChange={v => set('host', v)} placeholder="vcenter.bentech.work" /></Field>
          <Field label="Username"><Text value={vc.username} onChange={v => set('username', v)} placeholder="administrator@vsphere.local" /></Field>
          <Field label="Password" help={vc.password === MASK ? 'Stored (encrypted). Type to replace.' : ''}><Text type="password" value={vc.password} onChange={v => set('password', v)} /></Field>
        </div>
        <h3>Certificate</h3>
        <div className="row">
          <button onClick={fetchCert} disabled={!vc.host || busy === 'cert'}>Fetch certificate</button>
          {vc.cert_thumbprint && <span className="help">Accepted thumbprint: <span className="mono">{vc.cert_thumbprint}</span></span>}
        </div>
        {cert && (
          <div className="alert info" style={{ marginTop: 10 }}>
            <dl className="kv">
              <dt>Subject</dt><dd className="mono">{cert.subject}</dd>
              <dt>Issuer</dt><dd className="mono">{cert.issuer}</dd>
              <dt>SHA-1</dt><dd className="mono">{cert.sha1}</dd>
              <dt>SHA-256</dt><dd className="mono">{cert.sha256}</dd>
              <dt>Expires</dt><dd>{cert.not_after}</dd>
              <dt>Self-signed</dt><dd>{cert.self_signed ? 'yes' : 'no (chain from ' + cert.issuer + ')'}</dd>
              <dt>Trust bundle</dt><dd>{cert.ca_pem ? `vCenter CA bundle (${cert.ca_subjects.length} root(s)): ${cert.ca_subjects.join('; ')}` : 'CA bundle not published by this vCenter; the server certificate itself will be trusted'}</dd>
            </dl>
            <div className="row end" style={{ marginTop: 8 }}>
              <button className="primary" onClick={accept} disabled={vc.cert_thumbprint === cert.sha1}>{vc.cert_thumbprint === cert.sha1 ? 'Accepted' : 'Accept and trust this certificate'}</button>
            </div>
          </div>
        )}
        <h3>Connection & inventory</h3>
        <div className="row">
          <button onClick={test} disabled={busy === 'test'}>Test login</button>
          <button className="primary" onClick={load} disabled={busy === 'inv'}>{busy === 'inv' ? 'Loading…' : 'Load inventory'}</button>
          {about && <span className="help">{about.name} · build {about.build}</span>}
        </div>
        {inv && (
          <div className="grid2" style={{ marginTop: 12 }}>
            <Field label="Datacenter"><Select value={vc.datacenter} onChange={v => update(s => { s.vcenter.datacenter = v; s.vcenter.cluster = ''; s.vcenter.datastore = ''; s.vcenter.network = '' })} placeholder="select…" options={inv.datacenters.map(d => d.name)} /></Field>
            <Field label="Compute cluster" help={cl ? `${cl.hosts} host(s), ${cl.cpu_cores} cores, ${cl.memory_gb} GB RAM` : ''}>
              <Select value={vc.cluster} onChange={v => set('cluster', v)} placeholder="select…" options={(dc?.clusters || []).map(c => ({ value: c.name, label: `${c.name} (${c.kind})` }))} />
            </Field>
            <Field label="Datastore">
              <Select value={vc.datastore} onChange={v => set('datastore', v)} placeholder="select…"
                options={(dc?.datastores || []).filter(d => !cl || cl.datastores.includes(d.name)).map(d => ({ value: d.name, label: `${d.name} — ${d.free_gb} GB free of ${d.capacity_gb} (${d.type})` }))} />
            </Field>
            <Field label="Network / port group">
              <Select value={vc.network} onChange={v => set('network', v)} placeholder="select…"
                options={(dc?.networks || []).filter(n => !cl || cl.networks.includes(n.name)).map(n => ({ value: n.name, label: `${n.name} (${n.type})` }))} />
            </Field>
            <Field label="Resource pool (optional)">
              <Select value={vc.resource_pool} onChange={v => set('resource_pool', v)} placeholder="cluster root"
                options={(cl?.resource_pools || []).slice(1).map(r => ({ value: r.path, label: `${'  '.repeat(r.depth)}${r.name}` }))} />
            </Field>
            <Field label="VM folder (optional)" help="IPI creates its own folder named after the infra ID when empty.">
              <Select value={vc.folder} onChange={v => set('folder', v)} placeholder="datacenter root"
                options={(dc?.folders || []).map(f => ({ value: f.path, label: `${'  '.repeat(f.depth)}${f.name}` }))} />
            </Field>
          </div>
        )}
        {cl && cl.kind === 'host' && (
          <Alert kind={spec.install_method === 'ipi' ? 'error' : 'warn'}>
            <b>{vc.cluster}</b> is a standalone ESXi host, not a vSphere cluster. {spec.install_method === 'ipi'
              ? 'The IPI installer only accepts a cluster object. In vSphere Client right-click the datacenter → New Cluster (DRS and HA can stay off), drag the host into it, then reload the inventory here and select the new cluster.'
              : 'The agent method tolerates this.'}
          </Alert>
        )}
        {!inv && (vc.datacenter || vc.cluster) && (
          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Datacenter</dt><dd>{vc.datacenter}</dd><dt>Cluster</dt><dd>{vc.cluster}</dd>
            <dt>Datastore</dt><dd>{vc.datastore}</dd><dt>Network</dt><dd>{vc.network}</dd>
            <dt>Resource pool</dt><dd>{vc.resource_pool || '—'}</dd><dt>Folder</dt><dd>{vc.folder || '—'}</dd>
          </dl>
        )}
        <h3>Validation</h3>
        <div className="row"><button onClick={check} disabled={busy === 'check'}>Check objects, capacity & privileges</button></div>
        {checks && <div style={{ marginTop: 10 }}><CheckTable rows={checks} /></div>}
      </div>
      <Footer {...p} />
    </div>
  )
}
