import { useState } from 'react'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Operators(p) {
  const { spec } = p
  const { data: st, err, busy, reload, setErr } = useFetch(p.name, '/operators')
  const [job, setJob] = useState(null)
  const [sel, setSel] = useState([])
  const run = jobRunner(p.name, setJob, setErr)
  const install = () => run('/operators/install', { packages: sel }, `Install ${sel.length} operator(s): ${sel.join(', ')}?`)
  const remove = (pkg) => run('/operators/remove', { package: pkg }, `Remove the ${pkg} operator? Its custom resources and namespace stay in place.`)
  const done = () => { setSel([]); reload() }
  const phase = (o) => o.phase === 'Succeeded' ? 'pass' : o.phase ? 'running' : 'grey'
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Operators</h2><span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead">Install operators from the Red Hat catalogs through OLM. Channels and namespaces come from the marketplace; the app waits for each operator to report Succeeded and creates the initial custom resource where one is needed (HyperConverged, NMState, NFD).</p>
        <Alert kind="error">{err}</Alert>
        {spec.mirror?.enabled && <div className="alert info"><div className="row">Disconnected cluster: apply the CatalogSource and mirror mappings that oc-mirror produced so these packages resolve from {spec.mirror.registry}. <span className="spacer" /><button className="small" onClick={() => run('/operators/mirror-resources', {}, 'Apply the oc-mirror catalog resources and disable the default internet catalogs?')}>Apply mirror catalog</button></div></div>}
        {st && (
          <div className="cards">
            {st.catalog.map(o => (
              <div className={`card stripe ${o.installed ? (o.phase === 'Succeeded' ? 'installed' : 'deploying') : ''}`} key={o.package}>
                <h3><label className="row" style={{ cursor: 'pointer' }}>{!o.installed && <input type="checkbox" checked={sel.includes(o.package)} onChange={e => setSel(e.target.checked ? [...sel, o.package] : sel.filter(x => x !== o.package))} />}{o.title}</label>{o.installed && <Badge s={phase(o)} />}</h3>
                <p className="help">{o.desc}</p>
                <div className="help mono">{o.package} · {o.namespace}{o.installed ? ` · ${o.channel} · ${o.version || o.phase}` : ''}</div>
                {o.installed && <div className="row end"><button className="small danger" onClick={() => remove(o.package)}>Remove</button></div>}
              </div>
            ))}
          </div>
        )}
        <div className="toolbar"><span className="help">{sel.length ? `${sel.length} selected` : 'Tick operators to install'}</span><span className="spacer" /><button className="primary" onClick={install} disabled={!sel.length}>Install selected</button></div>
        {st?.other_csvs?.length > 0 && <><h3>Other operators on the cluster</h3><div className="chips">{st.other_csvs.map(c => <span key={c.namespace + c.name} className={`badge ${c.phase === 'Succeeded' ? 'pass' : 'warn'}`} title={c.namespace}>{c.display || c.name} {c.version}</span>)}</div></>}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
