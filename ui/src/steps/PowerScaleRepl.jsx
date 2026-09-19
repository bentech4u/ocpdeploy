import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import CheckTable, { Badge, Summary } from '../components/CheckTable.jsx'
import JobLog from '../components/JobLog.jsx'
import { jobRunner, fmtDate } from '../components/ops.js'

const RPOS = ['Five_Minutes', 'Fifteen_Minutes', 'Thirty_Minutes', 'One_Hour', 'Six_Hours', 'Twelve_Hours', 'One_Day']
const LINK = { SYNCHRONIZED: 'pass', SYNC_IN_PROGRESS: 'running', SUSPENDED: 'warn', FAILEDOVER: 'warn', INVALID: 'fail', UNKNOWN: 'grey', EMPTY: 'grey' }

export default function PowerScaleRepl(p) {
  const inst = p.st?.install
  const [cfg, setCfg] = useState(null)
  const [peers, setPeers] = useState([])
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [checks, setChecks] = useState(null)
  const [checking, setChecking] = useState(false)
  const [job, setJob] = useState(null)
  const [act, setAct] = useState(null)          // {group, action}
  const [confirmName, setConfirmName] = useState('')
  const [editing, setEditing] = useState(false)

  const load = async () => {
    setErr('')
    try {
      const [c, pe, g] = await Promise.all([api.get(`/api/clusters/${p.name}/powerscale/config`), api.get(`/api/clusters/${p.name}/powerscale/peers`),
        api.get(`/api/clusters/${p.name}/powerscale/replication`)])
      const conf = c.config
      if (!conf.replication.local_cluster_id) conf.replication.local_cluster_id = p.name.toLowerCase().replace(/[^a-z0-9-]/g, '-')
      if (!conf.replication.source_array && conf.arrays[0]?.name) conf.replication.source_array = conf.arrays[0].name
      setCfg(conf); setPeers(pe); setData(g)
      setEditing(!Object.values(g.controllers || {}).some(x => x.cluster_id))
    } catch (e) { setErr(e.message) }
  }
  const reloadPeers = async () => { try { setPeers(await api.get(`/api/clusters/${p.name}/powerscale/peers`)) } catch { /* keep the old list */ } }
  const reloadGroups = async () => { try { setData(await api.get(`/api/clusters/${p.name}/powerscale/replication`)) } catch (e) { setErr(e.message) } }
  useEffect(() => { load() }, [p.name])
  useEffect(() => { const t = setInterval(() => { if (!document.hidden) reloadGroups() }, 20000); return () => clearInterval(t) }, [p.name])
  if (!p.st) return <p className="muted">Loading…</p>
  if (!inst?.installed) return <Alert kind="info">Install the driver first (Install / upgrade tab), with "replication" ticked.</Alert>
  if (!cfg) return <div><Alert kind="error">{err}</Alert><p className="muted">Loading…</p></div>

  const r = cfg.replication
  const setR = (k, v) => setCfg(c => ({ ...c, replication: { ...c.replication, [k]: v } }))
  const run = jobRunner(p.name, setJob, setErr)
  const body = () => ({ config: { ...cfg, replication: { ...r, enabled: true } }, passwords: {} })
  const doCheck = async () => {
    setErr(''); setChecking(true)
    try { const x = await api.post(`/api/clusters/${p.name}/powerscale/replication/check`, body()); setChecks(x); return x } catch (e) { setErr(e.message); return null } finally { setChecking(false) }
  }
  const setup = async () => {
    const x = await doCheck()
    if (!x) return
    const fails = x.rows.filter(y => y.status === 'fail').length
    if (fails) { setErr(`${fails} pre-check(s) failed; fix them first.`); return }
    return run('/powerscale/replication/setup', body(), `${r.peer ? '' : 'SINGLE-CLUSTER replication (no peer selected)! '}Set up replication ${p.name} → ${r.peer || 'same cluster'} (${r.source_array} → ${r.target_array}, RPO ${r.rpo})? It installs the replication controller where missing, wires the clusters with a service-account identity and creates the replicated StorageClasses.`)
  }
  const actions = data?.actions || {}
  // what makes sense in the group's current state (the driver refuses the rest, e.g. reprotect before any failover)
  const WHEN = {
    FAILOVER_REMOTE: g => g.is_source && g.link_state !== 'FAILEDOVER',
    UNPLANNED_FAILOVER_LOCAL: g => !g.is_source && g.link_state !== 'FAILEDOVER',
    REPROTECT_LOCAL: g => !g.is_source && g.link_state === 'FAILEDOVER',
    FAILBACK_LOCAL: g => g.is_source && g.link_state === 'FAILEDOVER',
    ACTION_FAILBACK_DISCARD_CHANGES_LOCAL: g => g.is_source && g.link_state === 'FAILEDOVER',
    SUSPEND: g => g.is_source && g.link_state !== 'FAILEDOVER' && g.link_state !== 'SUSPENDED',
    RESUME: g => g.is_source && g.link_state === 'SUSPENDED',
    SYNC: g => g.is_source && g.link_state !== 'FAILEDOVER' && g.link_state !== 'SUSPENDED',
  }
  const allowed = (g) => Object.entries(actions).filter(([k]) => (WHEN[k] || (() => true))(g))
  const busy = (g) => !!g.action || (g.state || '').endsWith('_IN_PROGRESS')
  const startAction = (g, action) => { setAct({ group: g, action }); setConfirmName('') }
  const doAction = async () => {
    const { group: g, action } = act
    const needConfirm = actions[action].confirm
    const res = await run('/powerscale/replication/action', { cluster: g.cluster, group: g.name, action, confirm: needConfirm ? confirmName : '' },
      needConfirm ? null : `${actions[action].label} for ${g.name} on ${g.cluster}?`)
    if (res) setAct(null)
  }
  const arrays = cfg.arrays.map(a => a.name).filter(Boolean)
  const peerOpts = [{ value: '', label: '(same cluster: single-cluster replication)' }, ...peers.map(x => ({ value: x.name, label: `${x.name} (${x.kind}${x.read_only ? ', read-only' : ''})` }))]

  return (
    <div>
      <Alert kind="error">{err}</Alert>
      {(data?.errors || []).map((e, i) => <Alert key={i} kind="warn">{e}</Alert>)}
      <p className="help">Replication copies each namespace's volumes with SyncIQ to a second array, and makes them available on a second cluster (or on this one).
        The peer cluster must be installed by this app or connected in this session, and must run the PowerScale driver with replication ticked.</p>

      <div className="row"><h3 style={{ margin: 0 }}>Setup</h3><span className="spacer" />{!editing && <button className="small" onClick={() => setEditing(true)}>Edit</button>}</div>
      {!editing && <dl className="kv" style={{ marginTop: 8 }}>
        <dt>Clusters</dt><dd>{p.name} ({r.local_cluster_id}) → {r.peer ? `${r.peer} (${r.remote_cluster_id})` : 'same cluster'}</dd>
        <dt>Arrays</dt><dd>{r.source_array} → {r.target_array} · RPO {r.rpo}</dd>
        <dt>Storage classes</dt><dd className="mono">{r.class_name} → {r.remote_class_name}</dd>
      </dl>}
      {editing && <>
        <div className="grid3" style={{ marginTop: 8 }}>
          <Field label="Peer cluster (DR site)" help={peers.length ? '' : 'No other cluster here: connect the DR cluster on the Clusters page, then pick it'}>
            <div onFocus={reloadPeers} onMouseDown={reloadPeers}><Select value={r.peer} onChange={v => setCfg(c => ({ ...c, replication: { ...c.replication, peer: v,
              remote_cluster_id: v ? (c.replication.remote_cluster_id || v.toLowerCase().replace(/[^a-z0-9-]/g, '-')) : '' } }))} options={peerOpts} /></div></Field>
          <Field label="This cluster's ID" help="Name used in the replication config; lower-case"><Text value={r.local_cluster_id} onChange={v => setR('local_cluster_id', v)} /></Field>
          {r.peer && <Field label="Peer cluster's ID"><Text value={r.remote_cluster_id} onChange={v => setR('remote_cluster_id', v)} placeholder={r.peer.toLowerCase()} /></Field>}
          <Field label="Source array (here)"><Select value={r.source_array} onChange={v => setR('source_array', v)} options={arrays} placeholder="" /></Field>
          <Field label="Target array (DR)" help="clusterName as listed in the driver secret (either cluster)"><Text value={r.target_array} onChange={v => setR('target_array', v)} placeholder="PS-DR" /></Field>
          <Field label="RPO"><Select value={r.rpo} onChange={v => setR('rpo', v)} options={RPOS} /></Field>
          <Field label="Source access zone / path"><div className="row"><Text value={r.source_zone} onChange={v => setR('source_zone', v)} style={{ width: 90 }} /><Text value={r.source_path} onChange={v => setR('source_path', v)} /></div></Field>
          <Field label="Target access zone / path"><div className="row"><Text value={r.target_zone} onChange={v => setR('target_zone', v)} style={{ width: 90 }} /><Text value={r.target_path} onChange={v => setR('target_path', v)} /></div></Field>
          <Field label="NFS addresses (source / target)" help="SmartConnect names; blank = array endpoint"><div className="row"><Text value={r.source_az_service_ip} onChange={v => setR('source_az_service_ip', v)} style={{ width: 130 }} /><Text value={r.target_az_service_ip} onChange={v => setR('target_az_service_ip', v)} style={{ width: 130 }} /></div></Field>
          <Field label="StorageClass here"><Text value={r.class_name} onChange={v => setR('class_name', v)} /></Field>
          <Field label="StorageClass on the peer" help={r.peer ? '' : 'Single cluster: becomes <name>-tgt when equal'}><Text value={r.remote_class_name} onChange={v => setR('remote_class_name', v)} /></Field>
          <Field label="Volume group prefix"><Text value={r.volume_group_prefix} onChange={v => setR('volume_group_prefix', v)} /></Field>
        </div>
        {!r.peer && <Alert kind="warn">No peer cluster selected: this sets up <b>single-cluster</b> replication (both copies used from {p.name}). For DR to another cluster, connect it and pick it as the peer.</Alert>}
        <label className="row help" style={{ marginTop: 6 }}><input type="checkbox" checked={r.ignore_namespaces} onChange={e => setR('ignore_namespaces', e.target.checked)} /> one replication group for all namespaces (ignoreNamespaces)</label>
        <div className="row" style={{ marginTop: 12 }}><span className="spacer" />
          <button onClick={doCheck} disabled={checking}>{checking ? 'Checking…' : 'Run pre-checks'}</button>
          <button className="primary" onClick={setup} disabled={checking}>Set up replication</button></div>
        {checks && <div style={{ marginTop: 12 }}><Summary rows={checks.rows} /><CheckTable rows={checks.rows} /></div>}
      </>}

      <h3>Replication controllers</h3>
      <table className="tbl"><thead><tr><th>Cluster</th><th>Controller</th><th>Cluster ID</th><th>Targets</th></tr></thead>
        <tbody>{Object.entries(data?.controllers || {}).map(([n, c]) => <tr key={n}><td>{n}</td>
          <td>{c.installed ? <><Badge s={c.ready ? 'pass' : 'warn'} /> {c.ready}/{c.replicas} ({c.managed_by})</> : <span className="muted">not installed</span>}</td>
          <td className="mono">{c.cluster_id || '—'}</td><td className="mono">{(c.targets || []).map(t => `${t.clusterId} (${t.address})`).join(', ') || '—'}</td></tr>)}</tbody></table>

      <div className="row" style={{ marginTop: 14 }}><h3 style={{ margin: 0 }}>Replication groups</h3><span className="spacer" /><button className="small" onClick={reloadGroups}>Refresh</button></div>
      {data?.groups?.length ? (
        <table className="tbl"><thead><tr><th>Group</th><th>Cluster</th><th>Role</th><th>Link</th><th>Last sync</th><th>State</th><th>Last action</th><th>PVs</th><th></th></tr></thead>
          <tbody>{data.groups.map(g => <tr key={`${g.cluster}/${g.name}`}>
            <td className="mono">{g.name}<div className="help">{g.system} → {g.remote_system}</div></td>
            <td>{g.cluster}</td><td>{g.is_source ? <span className="tag accent">source</span> : <span className="tag">target</span>}</td>
            <td><Badge s={LINK[g.link_state] || 'grey'} /> {g.link_state}{g.link_error && <div className="help">{g.link_error}</div>}</td>
            <td className="help">{fmtDate(g.last_sync)}</td><td>{g.action ? <span className="badge running">{g.action}</span> : g.state}</td>
            <td className="help">{g.last_action}{g.last_error && <div style={{ color: 'var(--fail)' }}>{g.last_error}</div>}</td><td>{g.pvs}</td>
            <td><select value="" disabled={busy(g)} onChange={e => e.target.value && startAction(g, e.target.value)}>
              <option value="">{busy(g) ? 'busy…' : 'Action…'}</option>
              {allowed(g).map(([k, a]) => <option key={k} value={k}>{a.label}</option>)}</select></td></tr>)}</tbody></table>
      ) : <p className="muted">No replication groups yet. They appear when a PVC is created with the replicated StorageClass ({r.class_name}).</p>}

      {act && (
        <div className="alert warn" style={{ marginTop: 12 }}>
          <b>{actions[act.action].label}</b> — group <span className="mono">{act.group.name}</span> on {act.group.cluster} ({act.action}).
          <div className="help" style={{ marginTop: 4 }}>{HELP[act.action]}</div>
          {actions[act.action].confirm ? (
            <div className="row" style={{ marginTop: 8 }}>Type the group name to confirm: <input type="text" value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={act.group.name} style={{ width: 320 }} />
              <button className="danger" onClick={doAction} disabled={confirmName !== act.group.name}>Run</button><button onClick={() => setAct(null)}>Cancel</button></div>
          ) : <div className="row" style={{ marginTop: 8 }}><button className="primary" onClick={doAction}>Run</button><button onClick={() => setAct(null)}>Cancel</button></div>}
        </div>
      )}
      {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={() => reloadGroups()} /></div>}
    </div>
  )
}

const HELP = {
  FAILOVER_REMOTE: 'Syncs the latest changes, stops writes here and makes the DR copy writable. Stop the applications here first; start them on the DR cluster afterwards.',
  UNPLANNED_FAILOVER_LOCAL: 'For when the source site is lost: makes the DR copy writable right away, without a final sync. Changes since the last sync are lost.',
  REPROTECT_LOCAL: 'Starts replicating from the site that now runs the applications back to the other site. Run it on the site that is active after a failover.',
  FAILBACK_LOCAL: 'Copies the data written at the DR site back, then makes the original site writable again. Stop the applications at the DR site first.',
  ACTION_FAILBACK_DISCARD_CHANGES_LOCAL: 'Makes the original site writable again and throws away everything written at the DR site since the failover.',
  SUSPEND: 'Pauses the SyncIQ policy; nothing is copied until Resume.',
  RESUME: 'Resumes a suspended policy.',
  SYNC: 'Runs a SyncIQ job now instead of waiting for the RPO.',
}
