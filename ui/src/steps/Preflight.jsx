import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import CheckTable, { Summary } from '../components/CheckTable.jsx'

const CATS = [['general', 'Installer host, secrets & node sanity'], ['dns', 'DNS'], ['lb', 'Load balancer'], ['vcenter', 'vCenter']]

export default function Preflight(p) {
  const [res, setRes] = useState({})
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { api.get(`/api/clusters/${p.name}/checks`).then(setRes).catch(() => {}) }, [p.name])
  const run = async () => {
    setErr(''); setBusy(true)
    try { if (p.dirty) await p.save(); setRes(await api.post(`/api/clusters/${p.name}/preflight`)) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const all = Object.values(res).flat()
  const fails = all.filter(r => r.status === 'fail').length
  return (
    <div>
      <div className="panel">
        <h2>Pre-flight</h2>
        <p className="lead">Everything the install depends on, checked in one go. Fix every <b>fail</b>; review each <b>warn</b>.</p>
        <Alert kind="error">{err}</Alert>
        <div className="toolbar"><button className="primary" onClick={run} disabled={busy}>{busy ? 'Running checks (up to a minute)…' : 'Run all checks'}</button>
          {all.length > 0 && <span className="help">last run {all[0].ts ? all[0].ts.slice(0, 19) + 'Z' : 'just now'}</span>}</div>
        {all.length > 0 && <Summary rows={all} />}
        {all.length > 0 && (fails ? <Alert kind="warn">{fails} failing check(s). The deploy button stays enabled, but expect the install to fail.</Alert> : <Alert kind="ok">No failing checks.</Alert>)}
        {CATS.map(([k, label]) => res[k] && <div key={k}><h3>{label}</h3><CheckTable rows={res[k]} /></div>)}
      </div>
      <Footer {...p} />
    </div>
  )
}
