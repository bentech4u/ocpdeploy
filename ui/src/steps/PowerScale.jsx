import { useState } from 'react'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner, fmtDate } from '../components/ops.js'
import PowerScaleInstall from './PowerScaleInstall.jsx'
import PowerScaleRepl from './PowerScaleRepl.jsx'
import DellBundle from './DellBundle.jsx'

const TABS = [['overview', 'Overview'], ['install', 'Install / upgrade'], ['replication', 'Replication & DR'], ['bundle', 'Offline bundle']]

export default function PowerScale(p) {
  const [tab, setTab] = useState(() => { try { return localStorage.getItem('ps.tab') || 'overview' } catch { return 'overview' } })
  const { data: st, err, busy, reload, setErr } = useFetch(p.name, '/powerscale')
  const pick = (t) => { setTab(t); try { localStorage.setItem('ps.tab', t) } catch { /* private mode */ } }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Dell PowerScale CSI</h2><span className="spacer" />
          {st && <InstallBadge inst={st.install} />}
          <button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead" style={{ marginTop: 6 }}>Installs and runs Dell's CSI driver for PowerScale (Isilon) NFS storage, with the Helm chart or the Dell CSM Operator,
          and wires up CSM Replication (SyncIQ) between two clusters with failover and failback. Every change is checked against the array and the cluster first;
          array passwords are never stored by this app (they only go into the driver's secret in the cluster).</p>
        <div className="tabs">{TABS.map(([k, l]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => pick(k)}>{l}</button>)}</div>
        <Alert kind="error">{err}</Alert>
        {tab === 'overview' && <Overview {...p} st={st} reload={reload} setErr={setErr} />}
        {tab === 'install' && <PowerScaleInstall {...p} st={st} reloadStatus={reload} />}
        {tab === 'replication' && <PowerScaleRepl {...p} st={st} />}
        {tab === 'bundle' && <DellBundle />}
      </div>
    </div>
  )
}

function InstallBadge({ inst }) {
  if (!inst?.installed) return <span className="badge grey">not installed</span>
  return <span className="row" style={{ gap: 6 }}><Badge s="pass" /><span className="tag">{inst.method}</span><span className="tag accent">{inst.driver_version}</span></span>
}

function Overview(p) {
  const { st } = p
  const [job, setJob] = useState(null)
  const [confirmNs, setConfirmNs] = useState('')
  const [removeSc, setRemoveSc] = useState(false)
  const [showUninstall, setShowUninstall] = useState(false)
  const run = jobRunner(p.name, setJob, p.setErr)
  if (!st) return <p className="muted">Loading…</p>
  const inst = st.install
  const notReady = st.pods.filter(x => { const [a, b] = x.ready.split('/'); return a !== b })
  return (
    <div>
      {!inst.installed && <Alert kind="info">The driver is not installed on this cluster. Upload the chart on the <b>Offline bundle</b> tab (Helm method), then use <b>Install / upgrade</b>.
        {st.operator?.available && ' The Dell CSM Operator is available in this cluster\'s catalogs, so the Operator method works too.'}</Alert>}
      {inst.installed && inst.method === 'manual' && <Alert kind="warn">The driver runs in {inst.namespace} but was not installed with Helm or the CSM Operator, so this app only shows it.</Alert>}
      {inst.installed && (
        <div className="grid2">
          <dl className="kv">
            <dt>Method</dt><dd>{inst.method === 'helm' ? `Helm release ${inst.release}` : inst.method === 'operator' ? `CSM Operator (ContainerStorageModule ${inst.release})` : inst.method}</dd>
            <dt>Namespace</dt><dd className="mono">{inst.namespace}</dd>
            <dt>Driver</dt><dd className="mono">{inst.driver_image}</dd>
            {st.helm && <><dt>Chart</dt><dd>{st.helm.chart} {st.helm.chart_version} · revision {st.helm.revision} · {st.helm.status} · {fmtDate(st.helm.deployed)}</dd></>}
            {st.csm && <><dt>CSM</dt><dd>{st.csm.version} · state <Badge s={st.csm.state === 'Succeeded' ? 'pass' : 'warn'} /> {st.csm.state}</dd></>}
            <dt>Volumes</dt><dd>{st.pv_count} PV(s), {st.pv_bound} bound</dd>
          </dl>
          <dl className="kv">
            {st.arrays.map(a => <span key={a.clusterName} style={{ display: 'contents' }}><dt>Array {a.clusterName}{a.isDefault ? ' (default)' : ''}</dt>
              <dd className="mono">{a.username}@{a.endpoint}:{a.endpointPort} · {a.isiPath}{a.skipCertificateValidation ? ' · TLS not verified' : ' · TLS verified'}</dd></span>)}
            {st.operator?.installed && <><dt>CSM Operator</dt><dd>{st.operator.csv} · {st.operator.phase}</dd></>}
          </dl>
        </div>
      )}
      {notReady.length > 0 && <Alert kind="warn">{notReady.length} driver pod(s) not ready: {notReady.map(x => `${x.name} ${x.ready}${x.waiting ? ` (${x.waiting})` : ''}`).join(', ')}</Alert>}
      {st.pods.length > 0 && <>
        <h3>Pods</h3>
        <table className="tbl"><thead><tr><th>Pod</th><th>Ready</th><th>Status</th><th>Restarts</th><th>Node</th><th>Age</th></tr></thead>
          <tbody>{st.pods.map(x => <tr key={x.name}><td className="mono">{x.name}</td><td>{x.ready}</td><td>{x.waiting || x.phase}</td><td>{x.restarts}</td><td className="mono help">{x.node}</td><td>{x.age}</td></tr>)}</tbody></table>
      </>}
      <h3>Storage classes</h3>
      {st.classes.length ? (
        <table className="tbl"><thead><tr><th>Name</th><th>Array</th><th>Zone</th><th>Path</th><th>NFS address</th><th>Reclaim</th><th>Binding</th><th></th></tr></thead>
          <tbody>{st.classes.map(c => <tr key={c.name}><td className="mono">{c.name}{c.default && <span className="tag accent" style={{ marginLeft: 6 }}>default</span>}{c.replication && <span className="tag info" style={{ marginLeft: 6 }}>replicated</span>}</td>
            <td>{c.parameters.ClusterName || '(default)'}</td><td>{c.parameters.AccessZone}</td><td className="mono">{c.parameters.IsiPath}</td>
            <td className="mono">{c.parameters.AzServiceIP || '(API endpoint)'}</td><td>{c.reclaim_policy}</td><td>{c.binding_mode}</td><td className="help">{c.expansion ? 'expandable' : ''}</td></tr>)}</tbody></table>
      ) : <p className="muted">No StorageClass uses the driver.</p>}
      {st.snapshot_classes.length > 0 && <p className="help">Snapshot classes: {st.snapshot_classes.map(v => `${v.name} (${v.deletion_policy})`).join(', ')}</p>}
      {inst.installed && inst.method !== 'manual' && (
        <div style={{ marginTop: 18 }}>
          {!showUninstall ? <button className="danger small" onClick={() => setShowUninstall(true)}>Uninstall the driver…</button> : (
            <div className="alert warn">
              <b>Uninstall the PowerScale driver</b>: {inst.method === 'helm' ? `helm uninstall ${inst.release}` : `deletes ContainerStorageModule ${inst.release}`}.
              It is refused while PersistentVolumes still use the driver ({st.pv_count} now). The namespace and its secrets stay.
              <label className="row" style={{ marginTop: 6 }}><input type="checkbox" checked={removeSc} onChange={e => setRemoveSc(e.target.checked)} /> also delete the StorageClasses and snapshot classes of the driver</label>
              <div className="row" style={{ marginTop: 8 }}>Type the namespace to confirm: <input type="text" value={confirmNs} onChange={e => setConfirmNs(e.target.value)} placeholder={inst.namespace} style={{ width: 200 }} />
                <button className="danger" disabled={confirmNs !== inst.namespace || st.pv_count > 0}
                  onClick={() => run('/powerscale/uninstall', { confirm: confirmNs, remove_classes: removeSc }, null).then(r => { if (r) setShowUninstall(false) })}>Uninstall</button>
                <button onClick={() => setShowUninstall(false)}>Cancel</button></div>
            </div>)}
        </div>
      )}
      {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={() => p.reload()} /></div>}
    </div>
  )
}
