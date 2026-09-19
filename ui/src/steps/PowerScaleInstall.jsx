import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Num, Select, Alert, RadioCards } from '../components/Field.jsx'
import CheckTable, { Summary } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { jobRunner } from '../components/ops.js'

const blankArray = (first) => ({ name: '', endpoint: '', port: 8080, username: '', is_default: !!first, skip_cert_validation: true, ca_pem: '',
  access_zone: 'System', isi_path: '/ifs/data/csi', az_service_ip: '', replication_certificate_id: '' })
const blankClass = (arr) => ({ name: 'isilon', array: arr || '', access_zone: 'System', isi_path: '/ifs/data/csi', az_service_ip: '',
  root_client_enabled: false, reclaim_policy: 'Delete', binding_mode: 'Immediate', default: false })

export default function PowerScaleInstall(p) {
  const inst = p.st?.install
  const [cfg, setCfg] = useState(null)
  const [source, setSource] = useState('')
  const [pw, setPw] = useState({})
  const [charts, setCharts] = useState([])
  const [err, setErr] = useState('')
  const [checks, setChecks] = useState(null)
  const [checking, setChecking] = useState(false)
  const [nodes, setNodes] = useState(true)
  const [prev, setPrev] = useState(null)
  const [job, setJob] = useState(null)
  const [info, setInfo] = useState('')

  const load = async () => {
    setErr('')
    try {
      const [c, b] = await Promise.all([api.get(`/api/clusters/${p.name}/powerscale/config`), api.get('/api/bundles/dell?online=false')])
      setCfg(c.config); setSource(c.source)
      setCharts((b.components.find(x => x.component === 'csi-isilon')?.files || []).map(f => f.version))
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [p.name])
  if (!cfg || !p.st) return <div><Alert kind="error">{err}</Alert><p className="muted">Loading…</p></div>

  const set = (fn) => setCfg(c => { const n = structuredClone(c); fn(n); return n })
  const setA = (i, k, v) => set(c => {
    if (k === 'name') c.classes.forEach(sc => { if (sc.array === c.arrays[i].name) sc.array = v })
    c.arrays[i][k] = v
    if (k === 'is_default' && v) c.arrays.forEach((a, j) => { if (j !== i) a.is_default = false })
  })
  const setC = (i, k, v) => set(c => { c.classes[i][k] = v; if (k === 'default' && v) c.classes.forEach((x, j) => { if (j !== i) x.default = false }) })
  const installed = inst?.installed && inst.method !== 'manual'
  const body = () => ({ config: cfg, passwords: pw, nodes })
  const run = jobRunner(p.name, setJob, setErr)

  const doCheck = async () => {
    setErr(''); setChecking(true); setPrev(null)
    try { const r = await api.post(`/api/clusters/${p.name}/powerscale/check`, body()); setChecks(r); return r } catch (e) { setErr(e.message); return null } finally { setChecking(false) }
  }
  const doPreview = async () => {
    setErr('')
    try { setPrev(await api.post(`/api/clusters/${p.name}/powerscale/preview`, body())) } catch (e) { setErr(e.message) }
  }
  const doInstall = async () => {
    const r = await doCheck()
    if (!r) return
    const fails = r.rows.filter(x => x.status === 'fail').length, warns = r.rows.filter(x => x.status === 'warn').length
    if (fails) { setErr(`${fails} pre-check(s) failed; fix them first (table below).`); return }
    const what = installed ? `Upgrade/re-apply the PowerScale driver (${cfg.method}${cfg.method === 'helm' ? ` chart ${cfg.chart_version}` : ''}) in ${cfg.namespace}?` : `Install the PowerScale driver with ${cfg.method === 'helm' ? `Helm (chart ${cfg.chart_version})` : 'the Dell CSM Operator'} in ${cfg.namespace}?`
    return run('/powerscale/install', body(), `${what}${warns ? ` ${warns} warning(s) in the pre-checks.` : ' All pre-checks passed.'} Tip: "Preview changes" shows exactly what would change.`)
  }
  const createPath = async (a) => {
    setErr(''); setInfo('')
    if (!confirm(`Create ${a.isi_path} (mode 0777) on ${a.endpoint}?`)) return
    try { const r = await api.post(`/api/clusters/${p.name}/powerscale/create-path`, { config: cfg, array: a.name, password: pw[a.name] || '' }); setInfo(r.output) } catch (e) { setErr(e.message) }
  }
  const facts = checks?.facts || {}
  const useDetected = () => set(c => {
    const f = facts[c.arrays[0]?.name] || Object.values(facts)[0]
    if (f?.auth_type !== undefined) c.auth_type = f.auth_type
    c.arrays.forEach(a => { const fa = facts[a.name]; if (fa?.az_service_ip && !a.az_service_ip) a.az_service_ip = fa.az_service_ip })
  })
  const trustCert = (a, i) => { const f = facts[a.name]; if (f?.cert?.chain_pem) setA(i, 'ca_pem', f.cert.chain_pem.split('-----END CERTIFICATE-----').slice(-2, -1).map(x => x.trim() + '\n-----END CERTIFICATE-----').join('') ) }
  const missingPath = (a) => checks?.rows?.some(r => r.host === `array ${a.name}` && r.name.startsWith('isiPath') && r.actual === 'missing')

  return (
    <div>
      <Alert kind="error">{err}</Alert>
      <Alert kind="info">{info}</Alert>
      {source === 'cluster' && inst && <Alert kind="info">The form shows the driver that is running now ({inst.method} {inst.namespace}/{inst.release}, {inst.driver_version}). Installing re-applies it in place: with unchanged fields, <b>Preview changes</b> shows no difference.</Alert>}

      <h3>Method</h3>
      <RadioCards value={cfg.method} onChange={v => { if (!installed) set(c => { c.method = v }) }} options={[
        { value: 'helm', label: 'Helm chart', desc: 'Dell\'s csi-isilon chart from the offline bundle. Works without internet on the installer; the cluster needs the images (or a mirror).' },
        { value: 'operator', label: 'Dell CSM Operator', desc: `OperatorHub dell-csm-operator-certified${p.st?.operator?.csv ? ` (${p.st.operator.csv})` : ''}. The operator manages the driver; disconnected clusters need the certified catalog mirrored.` }]} />
      {installed && <p className="help">The running driver was installed with {inst.method}; the method cannot be switched while it runs (uninstall first).</p>}

      <div className="grid3" style={{ marginTop: 10 }}>
        {cfg.method === 'helm' && <Field label="csi-isilon chart" help={charts.length ? 'From the offline bundle' : 'Nothing uploaded yet: Offline bundle tab'}>
          <Select value={cfg.chart_version} onChange={v => set(c => { c.chart_version = v })} options={charts} placeholder={charts.length ? undefined : '(none)'} /></Field>}
        <Field label="Namespace"><Text value={cfg.namespace} onChange={v => set(c => { c.namespace = v })} disabled={installed} /></Field>
        <Field label={cfg.method === 'helm' ? 'Helm release' : 'ContainerStorageModule name'} help="Also the prefix of the secrets <name>-creds / <name>-certs-0"><Text value={cfg.release} onChange={v => set(c => { c.release = v })} disabled={installed} /></Field>
      </div>

      <h3>Arrays</h3>
      <p className="help">Each PowerScale cluster the driver talks to. The name is the <span className="mono">clusterName</span> StorageClasses refer to. For replication, both arrays must be listed on both clusters (the Replication tab copies them across).</p>
      {cfg.arrays.map((a, i) => (
        <div key={i} className="card" style={{ marginBottom: 12 }}>
          <div className="grid4">
            <Field label="Name (clusterName)"><Text value={a.name} onChange={v => setA(i, 'name', v)} placeholder={facts[a.name]?.cluster_name || 'PS-PROD'} /></Field>
            <Field label="Endpoint (API)"><Text value={a.endpoint} onChange={v => setA(i, 'endpoint', v)} placeholder="10.0.0.5 or ps.example.com" /></Field>
            <Field label="Port"><Num value={a.port} onChange={v => setA(i, 'port', v)} /></Field>
            <Field label="API user"><Text value={a.username} onChange={v => setA(i, 'username', v)} placeholder="csiuser" /></Field>
            <Field label="Password" help={installed ? 'Blank = keep the one in the cluster secret' : 'Not stored by this app'}>
              <Text type="password" value={pw[a.name] || ''} onChange={v => setPw({ ...pw, [a.name]: v })} autoComplete="new-password" /></Field>
            <Field label="Access zone"><Text value={a.access_zone} onChange={v => setA(i, 'access_zone', v)} /></Field>
            <Field label="Base path (isiPath)"><Text value={a.isi_path} onChange={v => setA(i, 'isi_path', v)} /></Field>
            <Field label="NFS address (AzServiceIP)" help="SmartConnect name/IP; blank = endpoint"><Text value={a.az_service_ip} onChange={v => setA(i, 'az_service_ip', v)} /></Field>
            {cfg.replication.enabled && <Field label="SyncIQ certificate ID" help="replicationCertificateID, for encrypted SyncIQ (isi sync certificates server list)">
              <Text value={a.replication_certificate_id} onChange={v => setA(i, 'replication_certificate_id', v.trim())} className="mono" /></Field>}
          </div>
          <div className="row" style={{ marginTop: 6 }}>
            {cfg.arrays.length > 1 && <label className="row"><input type="radio" checked={a.is_default} onChange={() => setA(i, 'is_default', true)} /> default array</label>}
            <label className="row"><input type="checkbox" checked={!a.skip_cert_validation} onChange={e => setA(i, 'skip_cert_validation', !e.target.checked)} /> verify the array's TLS certificate</label>
            {facts[a.name]?.cluster_name && facts[a.name].cluster_name !== a.name && <span className="help">OneFS calls itself <b>{facts[a.name].cluster_name}</b></span>}
            {missingPath(a) && <button className="small" onClick={() => createPath(a)}>Create {a.isi_path}</button>}
            <span className="spacer" />
            {cfg.arrays.length > 1 && <button className="small danger" onClick={() => set(c => { c.arrays.splice(i, 1); if (!c.arrays.some(x => x.is_default)) c.arrays[0].is_default = true })}>Remove</button>}
          </div>
          {!a.skip_cert_validation && <Field label="CA certificate (PEM)" help="The CA that signed the array's certificate; it goes into the <release>-certs-N secret">
            <textarea rows={4} value={a.ca_pem} onChange={e => setA(i, 'ca_pem', e.target.value)} className="mono" />
            {facts[a.name]?.cert && <button className="small" style={{ marginTop: 4 }} onClick={() => trustCert(a, i)}>Use the certificate the array presents ({facts[a.name].cert.self_signed ? 'self-signed' : 'issuer'})</button>}</Field>}
        </div>
      ))}
      <button className="small" onClick={() => set(c => { c.arrays.push(blankArray(false)) })}>Add array</button>

      <h3>Driver</h3>
      <div className="grid4">
        <Field label="Auth type (isiAuthType)" help="Session (1) is required by OneFS 9.15+"><Select value={String(cfg.auth_type)} onChange={v => set(c => { c.auth_type = Number(v) })} options={[{ value: '1', label: '1 - session' }, { value: '0', label: '0 - basic' }]} /></Field>
        <Field label="Controller replicas"><Num value={cfg.controller_count} onChange={v => set(c => { c.controller_count = v })} min={1} max={5} /></Field>
        <Field label="Volume name prefix"><Text value={cfg.volume_name_prefix} onChange={v => set(c => { c.volume_name_prefix = v })} /></Field>
        <Field label="Log level"><Select value={cfg.log_level} onChange={v => set(c => { c.log_level = v })} options={['error', 'warn', 'info', 'debug']} /></Field>
        <Field label="Image registry" help="Disconnected: pull the driver images from here (same paths as quay.io/registry.k8s.io)"><Text value={cfg.image_registry} onChange={v => set(c => { c.image_registry = v })} placeholder="mirror.example.com:5000" /></Field>
        <Field label="Snapshot class" help="VolumeSnapshotClass created with the driver"><Text value={cfg.snapshot_class} onChange={v => set(c => { c.snapshot_class = v })} disabled={!cfg.snapshots} /></Field>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <label className="row"><input type="checkbox" checked={cfg.enable_quota} onChange={e => set(c => { c.enable_quota = e.target.checked })} /> size limits (SmartQuotas)</label>
        <label className="row"><input type="checkbox" checked={cfg.snapshots} onChange={e => set(c => { c.snapshots = e.target.checked })} /> snapshots</label>
        <label className="row"><input type="checkbox" checked={cfg.resizer} onChange={e => set(c => { c.resizer = e.target.checked })} /> volume expansion</label>
        <label className="row"><input type="checkbox" checked={cfg.infra_nodes} onChange={e => set(c => { c.infra_nodes = e.target.checked })} /> also run on infra nodes</label>
        <label className="row"><input type="checkbox" checked={cfg.replication.enabled} onChange={e => set(c => { c.replication.enabled = e.target.checked })} /> replication (adds the replicator sidecar; set up on the Replication tab)</label>
        {checks && <button className="small" onClick={useDetected}>Use detected values</button>}
      </div>

      <h3>Storage classes</h3>
      <p className="help">StorageClass parameters cannot be changed after creation: an existing class with other settings is flagged by the pre-checks, never overwritten.</p>
      <table className="tbl"><thead><tr><th>Name</th><th>Array</th><th>Zone</th><th>Path</th><th>NFS address</th><th>Reclaim</th><th>Binding</th><th>Root client</th><th>Default</th><th></th></tr></thead>
        <tbody>{cfg.classes.map((c, i) => <tr key={i}>
          <td><Text value={c.name} onChange={v => setC(i, 'name', v)} /></td>
          <td><Select value={c.array} onChange={v => setC(i, 'array', v)} options={[{ value: '', label: '(default)' }, ...cfg.arrays.map(a => a.name).filter(Boolean)]} /></td>
          <td><Text value={c.access_zone} onChange={v => setC(i, 'access_zone', v)} style={{ width: 90 }} /></td>
          <td><Text value={c.isi_path} onChange={v => setC(i, 'isi_path', v)} /></td>
          <td><Text value={c.az_service_ip} onChange={v => setC(i, 'az_service_ip', v)} placeholder="(array)" style={{ width: 120 }} /></td>
          <td><Select value={c.reclaim_policy} onChange={v => setC(i, 'reclaim_policy', v)} options={['Delete', 'Retain']} /></td>
          <td><Select value={c.binding_mode} onChange={v => setC(i, 'binding_mode', v)} options={['Immediate', 'WaitForFirstConsumer']} /></td>
          <td><input type="checkbox" checked={c.root_client_enabled} onChange={e => setC(i, 'root_client_enabled', e.target.checked)} /></td>
          <td><input type="radio" checked={c.default} onChange={() => setC(i, 'default', true)} onClick={() => c.default && setC(i, 'default', false)} /></td>
          <td>{cfg.classes.length > 1 && <button className="small" onClick={() => set(x => { x.classes.splice(i, 1) })}>✕</button>}</td></tr>)}</tbody></table>
      <button className="small" style={{ marginTop: 6 }} onClick={() => set(c => { c.classes.push({ ...blankClass(''), name: `isilon-${c.classes.length + 1}` }) })}>Add storage class</button>

      <div className="row" style={{ marginTop: 18 }}>
        <label className="row help"><input type="checkbox" checked={nodes} onChange={e => setNodes(e.target.checked)} /> test reachability from a worker node (oc debug, ~30 s)</label>
        <span className="spacer" />
        <button onClick={doCheck} disabled={checking}>{checking ? 'Checking…' : 'Run pre-checks'}</button>
        <button onClick={doPreview}>Preview changes</button>
        <button className="primary" onClick={doInstall} disabled={checking}>{installed ? 'Upgrade / re-apply' : 'Install driver'}</button>
      </div>
      {checks && <div style={{ marginTop: 12 }}><Summary rows={checks.rows} /><CheckTable rows={checks.rows} /></div>}
      {prev && <Preview prev={prev} />}
      {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={() => { p.reloadStatus(); load() }} /></div>}
    </div>
  )
}

function Preview({ prev }) {
  const [show, setShow] = useState(prev.upgrade ? 'diff' : 'objects')
  const yaml = (o) => JSON.stringify(o, null, 2)
  if (prev.method !== 'helm') return (
    <div style={{ marginTop: 12 }}><h3>ContainerStorageModule that would be applied</h3><pre className="code">{yaml(prev.cr)}</pre></div>)
  const nothing = prev.upgrade && !prev.manifest_diff && !prev.values_diff
  return (
    <div style={{ marginTop: 12 }}>
      <h3>Preview: {prev.upgrade ? `upgrade ${prev.from_version} → ${prev.chart_version}` : `new install of ${prev.chart_version}`}</h3>
      {nothing && <Alert kind="info">No changes: the rendered chart and its values are identical to the running release.</Alert>}
      <div className="tabs">
        {prev.upgrade && <button className={show === 'diff' ? 'on' : ''} onClick={() => setShow('diff')}>Manifest diff</button>}
        {prev.upgrade && <button className={show === 'vdiff' ? 'on' : ''} onClick={() => setShow('vdiff')}>Values diff</button>}
        <button className={show === 'values' ? 'on' : ''} onClick={() => setShow('values')}>Values</button>
        {!prev.upgrade && <button className={show === 'objects' ? 'on' : ''} onClick={() => setShow('objects')}>Objects</button>}
        <button className={show === 'classes' ? 'on' : ''} onClick={() => setShow('classes')}>Storage classes</button>
      </div>
      {show === 'diff' && prev.upgrade && <pre className="code">{prev.manifest_diff || '(no difference)'}</pre>}
      {show === 'vdiff' && <pre className="code">{prev.values_diff || '(no difference)'}</pre>}
      {show === 'values' && <pre className="code">{yaml(prev.values)}</pre>}
      {show === 'objects' && <pre className="code">{(prev.objects || []).join('\n')}</pre>}
      {show === 'classes' && <pre className="code">{yaml(prev.classes)}</pre>}
    </div>
  )
}
