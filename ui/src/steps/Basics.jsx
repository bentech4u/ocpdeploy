import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert, RadioCards } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import JobLog from '../components/JobLog.jsx'

export default function Basics(p) {
  const { spec, update } = p
  const [versions, setVersions] = useState(null)
  const [verr, setVerr] = useState('')
  const [minor, setMinor] = useState('')
  const [channel, setChannel] = useState('stable')
  const [tool, setTool] = useState(null)
  const [job, setJob] = useState(null)

  const loadVersions = (refresh) => {
    setVerr(''); setVersions(null)
    api.get(`/api/versions${refresh ? '?refresh=true' : ''}`).then(v => {
      setVersions(v)
      if (spec.ocp_version) setMinor(spec.ocp_version.split('.').slice(0, 2).join('.'))
      else if (v.minors.length) setMinor(v.minors[v.minors.length - 1].minor)
    }).catch(e => setVerr(e.message))
  }
  useEffect(() => { loadVersions(false) }, [])
  useEffect(() => { if (spec.ocp_version) api.get(`/api/tools/${spec.ocp_version}`).then(setTool).catch(() => setTool(null)) }, [spec.ocp_version, job])

  const m = versions?.minors.find(x => x.minor === minor)
  const list = m ? (m.channels[channel] || []) : []
  const download = async () => { const r = await api.post(`/api/clusters/${p.name}/tools/download`); setJob(r.job_id) }

  return (
    <div>
      <div className="panel">
        <h2>Cluster & version</h2>
        <p className="lead">Identity of the cluster and the OpenShift release to install.</p>
        <div className="grid2">
          <Field label="Cluster name"><Text value={spec.name} onChange={() => {}} disabled /></Field>
          <Field label="Base domain" help={`Cluster domain becomes ${spec.name}.${spec.base_domain || '<base>'}`}>
            <Text value={spec.base_domain} onChange={v => update(s => s.base_domain = v)} />
          </Field>
        </div>
        <h3>Install method</h3>
        <RadioCards value={spec.install_method} onChange={v => update(s => s.install_method = v)} options={[
          { value: 'ipi', label: 'IPI — installer-provisioned on vSphere', desc: 'Installer creates VMs via vCenter. Bootstrap VM required in the node list. Needs 4.15+ for static IPs with your own load balancer.' },
          { value: 'agent', label: 'Agent-based (UPI)', desc: 'App builds the agent ISO, uploads it and creates VMs itself. No bootstrap VM; the first master is the rendezvous host.' },
        ]} />
        <h3>OpenShift version</h3>
        <Alert kind="error">{verr}</Alert>
        {!versions && !verr && <p className="muted">Querying the Red Hat update graph…</p>}
        {versions && (
          <div className="grid3">
            <Field label="Minor release">
              <Select value={minor} onChange={setMinor} options={versions.minors.map(x => ({ value: x.minor, label: `${x.minor} (latest ${x.latest})` }))} />
            </Field>
            <Field label="Channel" help="stable is what production uses; candidate builds are for testing.">
              <Select value={channel} onChange={setChannel} options={Object.keys(m?.channels || { stable: 1 })} />
            </Field>
            <Field label="Version">
              <Select value={spec.ocp_version} onChange={v => update(s => s.ocp_version = v)} placeholder="select…"
                options={[...list].reverse()} />
            </Field>
          </div>
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={() => loadVersions(true)}>Refresh list</button>
          <span className="help">Source: {versions?.source} · fetched {versions?.fetched}</span>
        </div>
        {spec.ocp_version && (
          <div style={{ marginTop: 14 }}>
            <h3>Tools for {spec.ocp_version}</h3>
            {tool && (
              <div className="row">
                <span>openshift-install: <b>{tool['openshift-install'] ? 'present' : 'missing'}</b></span>
                <span>oc: <b>{tool.oc ? 'present' : 'missing'}</b></span>
                <span className="help">{tool.on_mirror ? 'available on mirror.openshift.com' : 'not found on the mirror'}</span>
                <span className="spacer" />
                <button onClick={download} disabled={tool['openshift-install'] && tool.oc}>Download & verify now</button>
              </div>
            )}
            {job && <div style={{ marginTop: 10 }}><JobLog cluster={p.name} jobId={job} compact onDone={() => setJob(j => j)} /></div>}
          </div>
        )}
      </div>
      <Footer {...p} />
    </div>
  )
}
