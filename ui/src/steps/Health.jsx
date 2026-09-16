import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import { useFetch, fmtDate } from '../components/ops.js'

export default function Health(p) {
  const { data: h, err, busy, reload, setErr } = useFetch(p.name, '/health')
  const [auto, setAuto] = useState(true)
  useEffect(() => { if (!auto) return; const t = setInterval(reload, 60000); return () => clearInterval(t) }, [auto, reload])
  const approve = async () => { try { const r = await api.post(`/api/clusters/${p.name}/health/approve-csrs`); alert(`Approved: ${r.approved.join(', ') || 'nothing pending'}`); reload() } catch (e) { setErr(e.message) } }
  if (!h) return <div className="panel"><h2>Cluster health</h2><Alert kind="error">{err}</Alert><p className="muted">{busy ? 'Collecting…' : 'No data.'}</p></div>
  const ops = h.operators || [], nodes = h.nodes || [], alerts = h.alerts || [], csrs = h.csrs || [], mcps = h.mcps || []
  const bad = ops.filter(o => o.degraded === 'True' || o.available !== 'True')
  const prog = ops.filter(o => o.progressing === 'True' && !bad.includes(o))
  const crit = alerts.filter(a => a.severity === 'critical').length
  const warn = alerts.filter(a => a.severity === 'warning').length
  const errs = Object.entries(h.errors || {})
  return (
    <div>
      <div className="panel">
        <div className="row">
          <h2 style={{ margin: 0 }}>Cluster health</h2>
          {h.version && <span className="tag accent">{h.version.version}</span>}
          {h.version?.progressing && <Badge s="running" />}
          <span className="spacer" />
          <label className="row help"><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> auto-refresh</label>
          <button onClick={reload} disabled={busy}>{busy ? 'Refreshing…' : 'Refresh'}</button>
        </div>
        <Alert kind="error">{err}</Alert>
        {errs.length > 0 && <Alert kind="warn">Some probes failed: {errs.map(([k, v]) => `${k} (${v})`).join('; ')}</Alert>}
        {h.version?.failing && <Alert kind="error">ClusterVersion failing: {h.version.failing_message}</Alert>}
        <div className="stat-grid" style={{ marginTop: 14 }}>
          <div className="stat"><div className="k">Operators</div><div className="v" style={{ color: bad.length ? 'var(--fail)' : 'var(--ok)' }}>{ops.length - bad.length}<small> / {ops.length} healthy{prog.length ? `, ${prog.length} progressing` : ''}</small></div></div>
          <div className="stat"><div className="k">Nodes ready</div><div className="v" style={{ color: nodes.every(n => n.ready === 'True') ? 'var(--ok)' : 'var(--fail)' }}>{nodes.filter(n => n.ready === 'True').length}<small> / {nodes.length}</small></div></div>
          <div className="stat"><div className="k">Alerts firing</div><div className="v" style={{ color: crit ? 'var(--fail)' : warn ? 'var(--warn)' : 'var(--ok)' }}>{alerts.length}<small> {crit} critical · {warn} warning</small></div></div>
          <div className="stat"><div className="k">Pending CSRs</div><div className="v" style={{ color: csrs.length ? 'var(--warn)' : 'var(--ok)' }}>{csrs.length}</div></div>
          <div className="stat"><div className="k">Machine config</div><div className="v">{mcps.some(m => m.updating) ? <span style={{ color: 'var(--warn)' }}>updating</span> : mcps.some(m => m.is_degraded) ? <span style={{ color: 'var(--fail)' }}>degraded</span> : 'up to date'}<small> {mcps.map(m => `${m.name} ${m.updated}/${m.machines}`).join(' · ')}</small></div></div>
          <div className="stat"><div className="k">Kubelet signer cert</div><div className="v" style={{ color: (h.certs?.kubelet_signer_days ?? 999) < 30 ? 'var(--fail)' : 'var(--ok)' }}>{h.certs?.kubelet_signer_days ?? '—'}<small> days left</small></div></div>
        </div>
        {csrs.length > 0 && (
          <div className="alert warn"><div className="row"><b>{csrs.length} pending certificate signing request(s)</b> — {csrs.map(c => c.name).join(', ')} <span className="spacer" /><button className="primary small" onClick={approve}>Approve all</button></div></div>
        )}
        {h.etcd && (h.etcd.degraded ? <Alert kind="error">etcd degraded: {h.etcd.degraded_message}</Alert> : <p className="help">etcd: {h.etcd.members_available || 'members available'}</p>)}
        {h.pvcs?.pending?.length > 0 && <Alert kind="warn">Pending PVCs: {h.pvcs.pending.join(', ')} — is there a default storage class?</Alert>}
      </div>

      <div className="panel">
        <h2>Alerts <span className="help">— from Alertmanager (Watchdog hidden)</span></h2>
        {alerts.length === 0 ? <p className="muted">No alerts firing.</p> : (
          <table className="tbl"><thead><tr><th>Severity</th><th>Alert</th><th>Namespace</th><th>Summary</th><th>Since</th></tr></thead>
            <tbody>{alerts.map((a, i) => <tr key={i}><td><Badge s={a.severity === 'critical' ? 'fail' : a.severity === 'warning' ? 'warn' : 'info'} /></td><td className="mono">{a.name}</td><td className="mono">{a.namespace}</td><td className="help">{a.summary}</td><td className="help">{fmtDate(a.since)}</td></tr>)}</tbody></table>
        )}
      </div>

      <div className="panel">
        <h2>Cluster operators</h2>
        {bad.length === 0 && prog.length === 0 ? <p className="muted">All {ops.length} operators available and not degraded.</p> : (
          <table className="tbl"><thead><tr><th>Operator</th><th>Available</th><th>Progressing</th><th>Degraded</th><th>Message</th></tr></thead>
            <tbody>{[...bad, ...prog].map(o => <tr key={o.name}><td className="mono">{o.name}</td><td><Badge s={o.available === 'True' ? 'pass' : 'fail'} /></td><td>{o.progressing === 'True' ? <Badge s="running" /> : '—'}</td><td>{o.degraded === 'True' ? <Badge s="fail" /> : '—'}</td><td className="help">{o.message}</td></tr>)}</tbody></table>
        )}
        <div className="chips" style={{ marginTop: 10 }}>{ops.map(o => <span key={o.name} className={`badge ${o.degraded === 'True' ? 'fail' : o.available === 'True' ? (o.progressing === 'True' ? 'warn' : 'pass') : 'fail'}`} title={o.message || o.version}>{o.name}</span>)}</div>
      </div>

      <div className="panel">
        <h2>Nodes</h2>
        <table className="tbl"><thead><tr><th>Node</th><th>Roles</th><th>Ready</th><th>Conditions</th><th>IP</th><th>Kubelet</th><th>Allocatable</th><th>Age</th></tr></thead>
          <tbody>{nodes.map(n => <tr key={n.name}><td className="mono">{n.name}</td><td>{n.roles.join(', ')}</td><td><Badge s={n.ready === 'True' ? 'pass' : 'fail'} /></td>
            <td>{[n.memory_pressure && 'MemoryPressure', n.disk_pressure && 'DiskPressure', n.pid_pressure && 'PIDPressure', n.unschedulable && 'Unschedulable'].filter(Boolean).map(c => <span key={c} className="badge warn" style={{ marginRight: 4 }}>{c}</span>)}{!(n.memory_pressure || n.disk_pressure || n.pid_pressure || n.unschedulable) && <span className="help">ok</span>}</td>
            <td className="mono">{n.ip}</td><td>{n.version}</td><td className="help">{n.cpu} cpu · {n.memory}</td><td className="help">{n.age}</td></tr>)}</tbody></table>
        {h.machines?.length > 0 && <p className="help" style={{ marginTop: 8 }}>Machines: {h.machines.map(m => `${m.name} (${m.phase || '?'})`).join(', ')}</p>}
      </div>

      {(h.unhealthy_pods?.length > 0) && (
        <div className="panel"><h2>Pods not running</h2>
          <table className="tbl"><thead><tr><th>Namespace</th><th>Pod</th><th>Phase</th><th>Reason</th></tr></thead>
            <tbody>{h.unhealthy_pods.map((x, i) => <tr key={i}><td className="mono">{x.namespace}</td><td className="mono">{x.name}</td><td>{x.phase}</td><td className="help">{x.reason}</td></tr>)}</tbody></table></div>
      )}
      <div className="panel">
        <h2>Recent warning events</h2>
        {(h.events || []).length === 0 ? <p className="muted">None.</p> : (
          <table className="tbl"><thead><tr><th>Last</th><th>Namespace</th><th>Object</th><th>Reason</th><th>Message</th><th>#</th></tr></thead>
            <tbody>{h.events.map((e, i) => <tr key={i}><td className="help">{fmtDate(e.last)}</td><td className="mono">{e.namespace}</td><td className="mono">{e.object}</td><td>{e.reason}</td><td className="help">{e.message}</td><td>{e.count}</td></tr>)}</tbody></table>
        )}
      </div>
    </div>
  )
}
