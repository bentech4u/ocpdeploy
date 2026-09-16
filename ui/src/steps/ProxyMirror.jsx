import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import CheckTable from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'

const lines = (l) => (l || []).join('\n')
const parseLines = (t) => t.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
const defaultSources = (reg) => [
  { source: 'quay.io/openshift-release-dev/ocp-v4.0-art-dev', mirrors: [`${reg}/openshift/release`] },
  { source: 'quay.io/openshift-release-dev/ocp-release', mirrors: [`${reg}/openshift/release-images`] },
]

export default function ProxyMirror(p) {
  const { spec, update } = p
  const px = spec.proxy
  const m = spec.mirror
  const isIPI = spec.install_method === 'ipi'
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [cert, setCert] = useState(null)
  const [checks, setChecks] = useState(null)
  const [imageset, setImageset] = useState(null)
  const [job, setJob] = useState(null)
  const setP = (k, v) => update(s => s.proxy[k] = v)
  const setM = (k, v) => update(s => s.mirror[k] = v)
  const minor = spec.ocp_version ? spec.ocp_version.split('.').slice(0, 2).join('.') : '4.x'

  const guard = async (what, fn) => { setErr(''); setBusy(what); try { if (p.dirty) { if (!(await p.save())) return } await fn() } catch (e) { setErr(e.message) } finally { setBusy('') } }
  const fetchCert = () => guard('cert', async () => setCert(await api.post(`/api/clusters/${p.name}/mirror/cert`, { registry: m.registry })))
  const acceptCert = () => { update(s => s.mirror.ca_pem = cert.trust_pem || cert.pem); setCert(null) }
  const check = () => guard('check', async () => setChecks(await api.post(`/api/clusters/${p.name}/mirror/check`)))
  const preview = () => guard('preview', async () => setImageset((await api.get(`/api/clusters/${p.name}/mirror/imageset`))['imageset-config.yaml']))
  const run = () => guard('run', async () => {
    if (!confirm(`Mirror OpenShift ${spec.ocp_version}${m.operators.length ? ` and ${m.operators.length} operator(s)` : ''} to ${m.registry} now? The installer host needs internet access (directly or via the proxy) and the pull secret must contain the mirror registry login. This downloads tens of GB.`)) return
    const r = await api.post(`/api/clusters/${p.name}/mirror/run`); setJob(r.job_id)
  })
  const setSrc = (i, k, v) => update(s => s.mirror.sources[i][k] = k === 'mirrors' ? v.split(/[,\s]+/).filter(Boolean) : v)

  return (
    <div>
      <div className="panel">
        <h2>Proxy</h2>
        <p className="lead">For clusters that reach the internet through an HTTP proxy. Written to <span className="mono">proxy:</span> in install-config; the installer host also uses it for downloads. Leave empty for direct access.</p>
        <Alert kind="error">{err}</Alert>
        <div className="grid3">
          <Field label="HTTP proxy" help="http://user:pass@proxy.example.com:3128"><Text value={px.http_proxy} onChange={v => setP('http_proxy', v)} placeholder="http://proxy.example.com:3128" /></Field>
          <Field label="HTTPS proxy" help="Usually the same URL (http://…); an https:// proxy needs its CA in the trust bundle below."><Text value={px.https_proxy} onChange={v => setP('https_proxy', v)} placeholder="http://proxy.example.com:3128" /></Field>
          <Field label="No proxy" help="Comma separated hosts, domains (.example.com) and CIDRs. The machine, cluster and service networks, the cluster domain, vCenter and the mirror registry are added automatically."><Text value={px.no_proxy} onChange={v => setP('no_proxy', v)} placeholder="10.0.0.0/8,.example.com" /></Field>
        </div>
      </div>

      <div className="panel">
        <h2>Mirror registry <span className="help">— disconnected / restricted network</span></h2>
        <p className="lead">Pull the release (and optional operators) from a registry you control instead of quay.io. The mappings become <span className="mono">imageDigestSources</span> in install-config, the registry CA joins the trust bundle, and the app can run <span className="mono">oc-mirror</span> for you.</p>
        <label className="row" style={{ marginBottom: 12 }}><input type="checkbox" checked={!!m.enabled} onChange={e => setM('enabled', e.target.checked)} /> <b>Install from a mirror registry</b></label>
        {m.enabled && (
          <div>
            <div className="grid3">
              <Field label="Registry host[:port]" help="Where oc-mirror pushes and the nodes pull from, e.g. mirror.example.com:8443"><Text value={m.registry} onChange={v => setM('registry', v)} placeholder="mirror.example.com:8443" /></Field>
              <Field label="TLS verification" help="Keep on; disable only for a self-signed registry you cannot fetch the CA for.">
                <label className="row" style={{ marginTop: 8 }}><input type="checkbox" checked={!!m.tls_verify} onChange={e => setM('tls_verify', e.target.checked)} /> verify the registry certificate</label>
              </Field>
              <Field label="Registry certificate" help={m.ca_pem ? 'A CA is stored; fetch again to replace it.' : 'Fetch and accept, or paste the CA below.'}>
                <div className="row"><button onClick={fetchCert} disabled={!m.registry || busy === 'cert'}>Fetch certificate</button></div>
              </Field>
            </div>
            {cert && (
              <div className="alert info">
                <dl className="kv">
                  <dt>Subject</dt><dd className="mono">{cert.subject}</dd>
                  <dt>Issuer</dt><dd className="mono">{cert.issuer}</dd>
                  <dt>SHA-256</dt><dd className="mono">{cert.sha256}</dd>
                  <dt>Expires</dt><dd>{cert.not_after}</dd>
                </dl>
                <div className="row end" style={{ marginTop: 8 }}><button className="primary" onClick={acceptCert}>Accept and trust this certificate</button></div>
              </div>
            )}
            <Field label="Registry CA (PEM)" help="Added to additionalTrustBundle (policy Always) and to the installer host's trust bundle.">
              <textarea style={{ minHeight: 80 }} value={m.ca_pem} onChange={e => setM('ca_pem', e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" />
            </Field>

            <h3>Image source mappings</h3>
            <p className="help">Where the cluster looks for each upstream repository. Filled in automatically after the app runs oc-mirror; the defaults match the layout oc-mirror v2 produces.</p>
            {m.sources.length > 0 && (
              <table className="tbl">
                <thead><tr><th>Source</th><th>Mirrors (comma separated)</th><th></th></tr></thead>
                <tbody>
                  {m.sources.map((s, i) => (
                    <tr key={i}>
                      <td><Text value={s.source} onChange={v => setSrc(i, 'source', v)} /></td>
                      <td><Text value={(s.mirrors || []).join(', ')} onChange={v => setSrc(i, 'mirrors', v)} /></td>
                      <td><button className="small" onClick={() => update(x => x.mirror.sources.splice(i, 1))} title="remove">✕</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="toolbar">
              <button onClick={() => update(s => s.mirror.sources.push({ source: '', mirrors: [] }))}>+ Add mapping</button>
              <button onClick={() => update(s => s.mirror.sources = defaultSources(s.mirror.registry || 'REGISTRY'))} disabled={!m.registry}>Use oc-mirror defaults</button>
              {m.sources.length === 0 && <span className="help">Empty = the oc-mirror defaults for {m.registry || 'the registry'} are used.</span>}
            </div>

            <h3>oc-mirror</h3>
            <p className="help">Generates an ImageSetConfiguration for {spec.ocp_version || 'the selected version'} and runs <span className="mono">oc-mirror --v2</span> from this host straight into the registry (mirror-to-mirror). The pull secret in the Secrets step must also contain the login for {m.registry || 'the registry'}.</p>
            <div className="grid3">
              <Field label="Channel" help={`Default stable-${minor}`}><Text value={m.channel} onChange={v => setM('channel', v)} placeholder={`stable-${minor}`} /></Field>
              <Field label="Operator catalog" help={`Default registry.redhat.io/redhat/redhat-operator-index:v${minor}`}><Text value={m.catalog} onChange={v => setM('catalog', v)} placeholder={`registry.redhat.io/redhat/redhat-operator-index:v${minor}`} /></Field>
              <Field label="Last mirrored"><Text value={m.mirrored_version || 'never'} onChange={() => {}} disabled /></Field>
              <Field label="Operators (package names, one per line)" help="e.g. local-storage-operator, kubevirt-hyperconverged, odf-operator, cluster-logging">
                <textarea style={{ minHeight: 90 }} value={lines(m.operators)} onChange={e => setM('operators', parseLines(e.target.value))} placeholder={'local-storage-operator\nkubevirt-hyperconverged'} />
              </Field>
              <Field label="Additional images (one per line)" help="Any extra images the cluster needs, e.g. registry.redhat.io/ubi9/ubi:latest">
                <textarea style={{ minHeight: 90 }} value={lines(m.additional_images)} onChange={e => setM('additional_images', parseLines(e.target.value))} />
              </Field>
            </div>
            <div className="toolbar">
              <button onClick={preview} disabled={busy === 'preview'}>Preview ImageSetConfiguration</button>
              <button onClick={check} disabled={busy === 'check'}>Check registry</button>
              <span className="spacer" />
              <button className="primary" onClick={run} disabled={!m.registry || !spec.ocp_version || busy === 'run'}>Mirror release now</button>
            </div>
            {imageset && <pre className="code">{imageset}</pre>}
            {checks && <CheckTable rows={checks} />}
            {job && <div style={{ marginTop: 10 }}><JobLog cluster={p.name} jobId={job} onDone={() => p.reload()} /></div>}

            {isIPI && (
              <>
                <h3>RHCOS image</h3>
                <Field label="RHCOS OVA URL (optional)" help="Internal http(s) URL of the RHCOS OVA for this release (clusterOSImage). Without it the installer downloads the OVA from the internet, which works behind a proxy but not fully offline. Get the OVA URL with: openshift-install coreos print-stream-json">
                  <Text value={spec.vcenter.cluster_os_image} onChange={v => update(s => s.vcenter.cluster_os_image = v)} placeholder="http://web.example.com/rhcos-vmware.x86_64.ova" />
                </Field>
              </>
            )}
          </div>
        )}
      </div>

      <div className="panel">
        <h2>Additional trust bundle <span className="help">— optional</span></h2>
        <p className="lead">Extra CA certificates (PEM, concatenated) the cluster should trust: an https proxy, an internal Git server, a corporate CA. The vCenter and mirror registry certificates are added automatically.</p>
        <textarea style={{ minHeight: 80 }} value={spec.additional_trust_bundle} onChange={e => update(s => s.additional_trust_bundle = e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" />
      </div>
      <Footer {...p} />
    </div>
  )
}
