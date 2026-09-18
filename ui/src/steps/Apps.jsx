import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

function Inputs({ app, values, setValues }) {
  return (
    <div className="grid2" style={{ marginTop: 8 }}>
      {app.inputs.map(i => (
        <Field key={i.key} label={i.label} help={i.help}>
          {i.type === 'number' ? <Num value={values[i.key] ?? i.default} onChange={v => setValues({ ...values, [i.key]: v })} />
            : i.type === 'select' ? <Select value={values[i.key] ?? i.default} onChange={v => setValues({ ...values, [i.key]: v })} options={i.options} />
            : <Text value={values[i.key] ?? i.default} onChange={v => setValues({ ...values, [i.key]: v })} />}
        </Field>
      ))}
    </div>
  )
}

export default function Apps(p) {
  const { spec } = p
  const { data: d, err, busy, reload, setErr } = useFetch(p.name, '/apps')
  const [job, setJob] = useState(null)
  const [open, setOpen] = useState('')
  const [values, setValues] = useState({})
  const [creds, setCreds] = useState({})
  const run = jobRunner(p.name, setJob, setErr)
  const install = (app) => run(`/apps/${app.key}/install`, { inputs: values }, `Install ${app.title} in namespace ${app.namespace}?`)
  const remove = (app) => run(`/apps/${app.key}/remove`, {}, `Remove ${app.title}? The whole ${app.namespace} namespace and its data are deleted.`)
  const reveal = async (key) => { try { setCreds({ ...creds, [key]: await api.get(`/api/clusters/${p.name}/apps/${key}/credentials`) }) } catch (e) { setErr(e.message) } }
  const st = (key) => (d?.apps || []).find(a => a.key === key) || {}
  const done = () => { reload(); setOpen('') }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Applications</h2><span className="spacer" /><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead">One-click lab workloads. Each lands in its own namespace with a Route on <span className="mono">*.apps.{spec.cluster_domain || `${spec.name}.${spec.base_domain}`}</span>; generated passwords are kept in a Secret in that namespace and can be revealed here. Persistent apps need a default storage class (Storage page).</p>
        <Alert kind="error">{err}</Alert>
        {spec.mirror?.enabled && <Alert kind="warn">Disconnected cluster: the images these apps use (docker.io, quay.io, registry.redhat.io, helm.goharbor.io) must be mirrored first.</Alert>}
        {d && (
          <div className="cards">
            {d.catalog.map(app => { const s = st(app.key); const isOpen = open === app.key; return (
              <div className={`card stripe ${s.installed ? (s.ready ? 'installed' : 'deploying') : ''}`} key={app.key}>
                <h3>{app.title} {s.installed && <Badge s={s.ready ? 'pass' : 'running'} />}</h3>
                <p className="help">{app.desc}</p>
                <div className="help mono">{app.namespace} · {app.kind}{s.installed ? ` · ${s.detail}` : ''}</div>
                {s.url && <div className="help"><a href={s.url} target="_blank" rel="noreferrer">{s.url}</a></div>}
                {creds[app.key] && <dl className="kv" style={{ marginTop: 4 }}>{Object.entries(creds[app.key]).map(([k, v]) => <><dt key={k + 'k'}>{k}</dt><dd key={k + 'v'} className="mono">{v}</dd></>)}</dl>}
                {isOpen && !s.installed && <Inputs app={app} values={values} setValues={setValues} />}
                <div className="row end" style={{ marginTop: 6 }}>
                  {s.installed && !creds[app.key] && <button className="small" onClick={() => reveal(app.key)}>Credentials</button>}
                  {s.installed && <button className="small danger" onClick={() => remove(app)}>Remove</button>}
                  {!s.installed && !isOpen && <button className="small" onClick={() => { setOpen(app.key); setValues({}) }}>Configure</button>}
                  {!s.installed && isOpen && <><button className="small" onClick={() => setOpen('')}>Cancel</button><button className="small primary" onClick={() => install(app)}>Install</button></>}
                </div>
              </div>) })}
          </div>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
    </div>
  )
}
