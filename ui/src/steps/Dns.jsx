import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import CheckTable, { Summary } from '../components/CheckTable.jsx'

export default function Dns(p) {
  const { spec } = p
  const [exp, setExp] = useState([])
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.get(`/api/clusters/${p.name}/dns/expected`).then(setExp).catch(e => setErr(e.message)) }, [p.name, spec])
  const run = async () => {
    setErr(''); setBusy(true)
    try { if (p.dirty) await p.save(); setRes(await api.post(`/api/clusters/${p.name}/dns/check`)) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const dom = `${spec.name}.${spec.base_domain}`
  return (
    <div>
      <div className="panel">
        <h2>DNS records</h2>
        <p className="lead">Create these on your DNS server (for example the AD DNS zone <b>{spec.base_domain}</b>, folder <b>{spec.name}</b>), then validate. The app never writes DNS.</p>
        <Alert kind="error">{err}</Alert>
        <table className="tbl">
          <thead><tr><th>Record</th><th>Type</th><th>Value</th><th>Level</th><th>Why</th></tr></thead>
          <tbody>
            {exp.map((r, i) => <tr key={i}><td className="mono">{r.name}</td><td>{r.type}</td><td className="mono">{r.value}</td><td>{r.level}</td><td className="help">{r.why}</td></tr>)}
          </tbody>
        </table>
        <div className="alert info" style={{ marginTop: 12 }}>
          <b>Windows DNS tip:</b> in DNS Manager, under the {spec.base_domain} zone create a folder (sub-domain) named <span className="mono">{spec.name}</span>, add the A records inside it, and one more folder <span className="mono">apps</span> inside {spec.name} containing a single A record named <span className="mono">*</span>. Reverse records go in the reverse lookup zone for {spec.network.machine_cidr || 'your subnet'}; delete stale PTRs left by old clusters.
        </div>
        <div className="toolbar"><button className="primary" onClick={run} disabled={busy}>{busy ? 'Validating…' : 'Validate against ' + (spec.network.dns_servers?.[0] || 'system resolver')}</button></div>
        {res && <><Summary rows={res} /><CheckTable rows={res} showLevel /></>}
      </div>
      <Footer {...p} />
    </div>
  )
}
