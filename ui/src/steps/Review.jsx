import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import Footer from '../components/Footer.jsx'
import JobLog from '../components/JobLog.jsx'

export default function Review(p) {
  const { spec } = p
  const [files, setFiles] = useState(null)
  const [tab, setTab] = useState('install-config.yaml')
  const [err, setErr] = useState('')
  const [job, setJob] = useState(null)
  const [status, setStatus] = useState(null)

  const load = () => { setErr(''); api.get(`/api/clusters/${p.name}/config/preview`).then(setFiles).catch(e => { setFiles(null); setErr(e.message) }) }
  useEffect(() => { load(); api.get(`/api/clusters/${p.name}/status`).then(s => { setStatus(s); if (s.running_job) setJob(s.running_job) }).catch(() => {}) }, [p.name, spec])

  const generate = async () => { setErr(''); try { if (p.dirty) await p.save(); const r = await api.post(`/api/clusters/${p.name}/generate`); setJob(r.job_id) } catch (e) { setErr(e.message) } }
  const deploy = async () => {
    setErr('')
    const shape = { sno: 'single-node', compact: 'compact three-node', standard: 'standard' }[spec.topology] || spec.topology
    if (!confirm(`Start the ${spec.install_method.toUpperCase()} installation of ${spec.name}.${spec.base_domain} (${spec.ocp_version}, ${shape}${spec.mirror?.enabled ? ', from mirror ' + spec.mirror.registry : ''})? This creates VMs in vCenter and runs 30-60 minutes.`)) return
    try { if (p.dirty) await p.save(); const r = await api.post(`/api/clusters/${p.name}/deploy`); setJob(r.job_id) } catch (e) { setErr(e.message) }
  }

  return (
    <div>
      <div className="panel">
        <h2>Review & deploy</h2>
        <p className="lead">Generated from the spec with secrets masked. The real files are written into <span className="mono">clusters/{spec.name}/install/</span> when you generate or deploy.</p>
        <Alert kind="error">{err}</Alert>
        {files && (
          <>
            <div className="tabs">{Object.keys(files).map(f => <button key={f} className={tab === f ? 'on' : ''} onClick={() => setTab(f)}>{f}</button>)}</div>
            <pre className="code">{files[tab]}</pre>
          </>
        )}
        <div className="toolbar">
          <button onClick={load}>Refresh preview</button>
          <button onClick={generate} disabled={!!job && status?.running_job}>Generate files only</button>
          <span className="spacer" />
          <button className="primary" onClick={deploy} disabled={!files || spec.status === 'installed' || spec.status === 'deploying'}>
            {spec.install_method === 'ipi' ? 'Deploy cluster (openshift-install create cluster)' : 'Deploy cluster (agent ISO → VMs → wait-for install-complete)'}
          </button>
        </div>
        {spec.status === 'installed' && <Alert kind="ok">This cluster is installed. Use Operate for day-2 actions or destroy it first to reinstall.</Alert>}
        {job && <JobLog cluster={p.name} jobId={job} onDone={() => p.reload()} />}
      </div>
      <Footer {...p} />
    </div>
  )
}
