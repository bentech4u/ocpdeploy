import { useEffect, useState } from 'react'
import { api, MASK } from '../api.js'
import { Field, Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'

export default function Secrets(p) {
  const { spec, update } = p
  const [sys, setSys] = useState(null)
  useEffect(() => { api.get('/api/system').then(setSys).catch(() => {}) }, [])
  let psStatus = null
  if (spec.pull_secret && spec.pull_secret !== MASK) {
    try { const j = JSON.parse(spec.pull_secret); const a = Object.keys(j.auths || {}); psStatus = a.length ? `valid JSON with registries: ${a.join(', ')}` : 'JSON has no auths' } catch { psStatus = 'not valid JSON yet' }
  }
  return (
    <div>
      <div className="panel">
        <h2>Secrets</h2>
        <p className="lead">Stored encrypted on the installer host; never shown again after saving.</p>
        <Field label="Pull secret" help={<span>Get it from <a href="https://console.redhat.com/openshift/install/pull-secret" target="_blank" rel="noreferrer">console.redhat.com/openshift/install/pull-secret</a>. {spec.pull_secret === MASK ? 'A pull secret is stored; paste a new one to replace it.' : ''}</span>}>
          <textarea value={spec.pull_secret === MASK ? '' : spec.pull_secret} onChange={e => update(s => s.pull_secret = e.target.value)} placeholder={spec.pull_secret === MASK ? '(stored)' : '{"auths":{"cloud.openshift.com":{...}}}'} />
        </Field>
        {psStatus && <Alert kind={psStatus.startsWith('valid') ? 'ok' : 'warn'}>{psStatus}</Alert>}
        <Field label="SSH public key" help="Injected into every node as the core user's authorized key. Defaults to the installer host's key.">
          <textarea style={{ minHeight: 60 }} value={spec.ssh_public_key} onChange={e => update(s => s.ssh_public_key = e.target.value)} />
        </Field>
        {sys?.ssh_public_key && spec.ssh_public_key !== sys.ssh_public_key && <button onClick={() => update(s => s.ssh_public_key = sys.ssh_public_key)}>Use installer host key</button>}
        <h3>Options</h3>
        <label className="row"><input type="checkbox" checked={spec.fips} onChange={e => update(s => s.fips = e.target.checked)} /> FIPS mode</label>
      </div>
      <Footer {...p} />
    </div>
  )
}
