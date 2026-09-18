import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import { useFetch } from '../components/ops.js'
import { gib, cores, pct, tone } from '../components/bytes.js'

function Meter({ use, req, label }) {
  return (
    <div className="meter-cell">
      <div className="meter" title={`requests ${pct(req)} · usage ${pct(use)}`}>
        {req != null && <span className="req" style={{ width: `${Math.min(100, req * 100)}%`, background: tone(req) }} />}
        {use != null && <span style={{ width: `${Math.min(100, use * 100)}%`, background: tone(use) }} />}
      </div>
      <span className="help">{label}</span>
    </div>
  )
}

export default function Capacity(p) {
  const { data: c, err, busy, reload } = useFetch(p.name, '/capacity')
  if (!c) return <div className="panel"><h2>Capacity</h2><Alert kind="error">{err}</Alert><p className="muted">{busy ? 'Collecting…' : ''}</p></div>
  const t = c.totals
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Capacity</h2><span className="spacer" /><button onClick={reload} disabled={busy}>{busy ? 'Refreshing…' : 'Refresh'}</button></div>
        <p className="lead" style={{ marginTop: 6 }}>Requests decide whether new pods can be scheduled; usage shows how busy the nodes really are. Bars: pale part = requests, solid part = actual usage. Totals count schedulable nodes only.</p>
        <Alert kind="error">{err}</Alert>
        {Object.keys(c.errors).length > 0 && <Alert kind="warn">Partial data: {Object.entries(c.errors).map(([k, v]) => `${k}: ${v}`).join('; ')}</Alert>}
        <div className="stat-grid">
          <div className="stat"><div className="k">CPU requested</div><div className="v" style={{ color: tone(t.cpu_req / t.cpu_alloc) }}>{pct(t.cpu_req / t.cpu_alloc)}<small> {cores(t.cpu_req)} of {cores(t.cpu_alloc)} cores</small></div></div>
          <div className="stat"><div className="k">CPU used</div><div className="v" style={{ color: tone(t.cpu_use / t.cpu_alloc) }}>{pct(t.cpu_use / t.cpu_alloc)}<small> {cores(t.cpu_use)} cores</small></div></div>
          <div className="stat"><div className="k">Memory requested</div><div className="v" style={{ color: tone(t.mem_req / t.mem_alloc) }}>{pct(t.mem_req / t.mem_alloc)}<small> {gib(t.mem_req)} of {gib(t.mem_alloc)}</small></div></div>
          <div className="stat"><div className="k">Memory used</div><div className="v" style={{ color: tone(t.mem_use / t.mem_alloc) }}>{pct(t.mem_use / t.mem_alloc)}<small> {gib(t.mem_use)}</small></div></div>
          <div className="stat"><div className="k">Pods</div><div className="v">{t.pods}<small> / {t.pods_cap}</small></div></div>
        </div>
        <h3>Warnings</h3>
        {c.warnings.length === 0 ? <p className="muted">Nothing close to full (thresholds: 85% warn, 95% critical; root disk 80/90%).</p> : (
          <table className="tbl"><thead><tr><th>Level</th><th>Where</th><th>What</th></tr></thead>
            <tbody>{c.warnings.map((w, i) => <tr key={i}><td><Badge s={w.level} /></td><td className="mono">{w.what}</td><td>{w.message}</td></tr>)}</tbody></table>
        )}
      </div>
      <div className="panel">
        <h2>Nodes</h2>
        <table className="tbl"><thead><tr><th>Node</th><th>Roles</th><th>CPU</th><th>Memory</th><th>Pods</th><th>Root disk</th></tr></thead>
          <tbody>{c.nodes.map(n => <tr key={n.name}><td className="mono">{n.name}{n.unschedulable && <span className="badge warn" style={{ marginLeft: 6 }}>cordoned</span>}</td><td className="help">{n.roles.join(', ')}</td>
            <td><Meter req={n.cpu_req_pct} use={n.cpu_use_pct} label={`req ${pct(n.cpu_req_pct)} · use ${pct(n.cpu_use_pct)} of ${cores(n.cpu_alloc)}`} /></td>
            <td><Meter req={n.mem_req_pct} use={n.mem_use_pct} label={`req ${pct(n.mem_req_pct)} · use ${pct(n.mem_use_pct)} of ${gib(n.mem_alloc)}`} /></td>
            <td>{n.pods}<span className="help"> / {n.pods_cap}</span></td>
            <td>{n.fs == null ? <span className="help">—</span> : <Meter use={n.fs} label={pct(n.fs)} />}</td></tr>)}</tbody></table>
      </div>
      <div className="panel">
        <h2>Namespaces <span className="help">— top 60 by memory usage</span></h2>
        <table className="tbl"><thead><tr><th>Namespace</th><th>Pods</th><th>CPU used</th><th>CPU requested</th><th>Memory used</th><th>Memory requested</th></tr></thead>
          <tbody>{c.namespaces.map(n => <tr key={n.namespace}><td className="mono">{n.namespace}</td><td>{n.pods}</td><td>{cores(n.cpu_use)}</td><td className="help">{cores(n.cpu_req)}</td><td>{gib(n.mem_use)}</td><td className="help">{gib(n.mem_req)}</td></tr>)}</tbody></table>
      </div>
      <div className="panel">
        <h2>Persistent volumes</h2>
        {c.pvcs.length === 0 ? <p className="muted">No persistent volume claims.</p> : (
          <table className="tbl"><thead><tr><th>Claim</th><th>Class</th><th>Status</th><th>Size</th><th>Used</th></tr></thead>
            <tbody>{c.pvcs.map(v => <tr key={v.pvc}><td className="mono">{v.pvc}</td><td className="help">{v.class}</td><td>{v.phase}</td><td>{gib(v.size)}</td><td>{v.used_pct == null ? <span className="help">no data</span> : <Meter use={v.used_pct} label={pct(v.used_pct)} />}</td></tr>)}</tbody></table>
        )}
      </div>
    </div>
  )
}
