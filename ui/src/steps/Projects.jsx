import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import { useFetch } from '../components/ops.js'

const EMPTY_Q = { cpu_requests: '', cpu_limits: '', memory_requests: '', memory_limits: '', storage: '', pods: '', pvcs: '', default_request: { cpu: '', memory: '' }, default_limit: { cpu: '', memory: '' } }

function QuotaForm({ q, setQ }) {
  const set = (k, v) => setQ({ ...q, [k]: v })
  const setD = (group, res, v) => setQ({ ...q, [group]: { ...q[group], [res]: v } })
  return (
    <div className="grid4">
      <Field label="CPU requests" help="total, e.g. 4 or 500m"><Text value={q.cpu_requests} onChange={v => set('cpu_requests', v)} /></Field>
      <Field label="CPU limits"><Text value={q.cpu_limits} onChange={v => set('cpu_limits', v)} /></Field>
      <Field label="Memory requests" help="e.g. 8Gi"><Text value={q.memory_requests} onChange={v => set('memory_requests', v)} /></Field>
      <Field label="Memory limits"><Text value={q.memory_limits} onChange={v => set('memory_limits', v)} /></Field>
      <Field label="Storage requests" help="all PVCs together"><Text value={q.storage} onChange={v => set('storage', v)} /></Field>
      <Field label="Max pods"><Text value={q.pods} onChange={v => set('pods', v)} /></Field>
      <Field label="Max PVCs"><Text value={q.pvcs} onChange={v => set('pvcs', v)} /></Field>
      <span />
      <Field label="Default CPU request" help="per container without one"><Text value={q.default_request.cpu} onChange={v => setD('default_request', 'cpu', v)} placeholder="100m" /></Field>
      <Field label="Default memory request"><Text value={q.default_request.memory} onChange={v => setD('default_request', 'memory', v)} placeholder="128Mi" /></Field>
      <Field label="Default CPU limit"><Text value={q.default_limit.cpu} onChange={v => setD('default_limit', 'cpu', v)} placeholder="1" /></Field>
      <Field label="Default memory limit"><Text value={q.default_limit.memory} onChange={v => setD('default_limit', 'memory', v)} placeholder="1Gi" /></Field>
    </div>
  )
}

function qFromProject(pr) {
  const q = JSON.parse(JSON.stringify(EMPTY_Q))
  const hard = (pr.quotas.find(x => x.name === 'ocpdeploy-quota') || {}).hard || {}
  Object.assign(q, { cpu_requests: hard['requests.cpu'] || '', cpu_limits: hard['limits.cpu'] || '', memory_requests: hard['requests.memory'] || '', memory_limits: hard['limits.memory'] || '',
    storage: hard['requests.storage'] || '', pods: hard.pods || '', pvcs: hard.persistentvolumeclaims || '' })
  const lim = ((pr.limits.find(x => x.name === 'ocpdeploy-limits') || {}).limits || [])[0] || {}
  q.default_request = { cpu: lim.defaultRequest?.cpu || '', memory: lim.defaultRequest?.memory || '' }
  q.default_limit = { cpu: lim.default?.cpu || '', memory: lim.default?.memory || '' }
  return q
}

function Bindings({ pr, users, groups, onAdd, onRemove }) {
  const [b, setB] = useState({ kind: 'User', name: '', role: 'edit' })
  return (
    <div>
      {pr.bindings.length === 0 ? <p className="help">No user or group access yet.</p> : (
        <table className="tbl compact"><thead><tr><th>Who</th><th>Role</th><th></th></tr></thead>
          <tbody>{pr.bindings.map(x => <tr key={x.binding + x.name}><td className="mono">{x.kind === 'Group' ? 'group ' : ''}{x.name}</td><td>{x.role}</td><td className="row end"><button className="small danger" onClick={() => onRemove(x.binding)}>Remove</button></td></tr>)}</tbody></table>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <Select value={b.kind} onChange={v => setB({ ...b, kind: v })} options={['User', 'Group']} />
        <input type="text" list={`who-${pr.name}`} value={b.name} onChange={e => setB({ ...b, name: e.target.value })} placeholder={b.kind === 'User' ? 'user name' : 'group name'} style={{ maxWidth: 220 }} />
        <datalist id={`who-${pr.name}`}>{(b.kind === 'User' ? users : groups).map(x => <option key={x} value={x} />)}</datalist>
        <Select value={b.role} onChange={v => setB({ ...b, role: v })} options={[{ value: 'admin', label: 'admin (manage the project)' }, { value: 'edit', label: 'edit (deploy apps)' }, { value: 'view', label: 'view (read only)' }]} />
        <button className="small primary" onClick={() => { onAdd(b); setB({ ...b, name: '' }) }} disabled={!b.name.trim()}>Grant</button>
      </div>
    </div>
  )
}

export default function Projects(p) {
  const [system, setSystem] = useState(false)
  const { data: d, err, busy, reload, setErr } = useFetch(p.name, `/projects?system=${system}`, [system])
  const [form, setForm] = useState({ name: '', display: '', description: '' })
  const [quota, setQuota] = useState(JSON.parse(JSON.stringify(EMPTY_Q)))
  const [binds, setBinds] = useState([])
  const [open, setOpen] = useState('')
  const [editQ, setEditQ] = useState(null)
  const [msg, setMsg] = useState('')
  const call = async (fn, done) => { setErr(''); setMsg(''); try { const r = await fn(); if (done) setMsg(done); reload(); return r } catch (e) { setErr(e.message) } }
  const create = () => call(() => api.post(`/api/clusters/${p.name}/projects`, { ...form, quota, bindings: binds }), `Project ${form.name} created.`).then(r => { if (r) { setForm({ name: '', display: '', description: '' }); setBinds([]); setQuota(JSON.parse(JSON.stringify(EMPTY_Q))) } })
  const del = (pr) => { const c = prompt(`Delete project ${pr.name} and everything in it? Type the project name to confirm.`); if (c === pr.name) call(() => api.post(`/api/clusters/${p.name}/projects/${pr.name}/delete`, { confirm: c }), `Project ${pr.name} is being deleted.`) }
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Projects &amp; access</h2><span className="spacer" /><label className="row help"><input type="checkbox" checked={system} onChange={e => setSystem(e.target.checked)} /> show system projects</label><button onClick={reload} disabled={busy}>Refresh</button></div>
        <p className="lead" style={{ marginTop: 6 }}>Projects with a resource quota (ocpdeploy-quota), default container requests and limits (ocpdeploy-limits) and access for users or groups from your identity providers (admin, edit or view).</p>
        <Alert kind="error">{err}</Alert>{msg && <Alert kind="ok">{msg}</Alert>}
        {d && (d.projects.length === 0 ? <p className="muted">No user projects yet.</p> : (
          <table className="tbl"><thead><tr><th>Project</th><th>Quota</th><th>Access</th><th>Age</th><th></th></tr></thead>
            <tbody>{d.projects.map(pr => {
              const hard = (pr.quotas[0] || {}).hard || {}, used = (pr.quotas[0] || {}).used || {}
              return [
                <tr key={pr.name}><td><b className="mono">{pr.name}</b>{pr.display && <div className="help">{pr.display}</div>}</td>
                  <td className="help">{Object.keys(hard).length ? Object.entries(hard).map(([k, v]) => `${k} ${used[k] || 0}/${v}`).join(' · ') : 'none'}</td>
                  <td className="help">{pr.bindings.map(b => `${b.name} (${b.role})`).join(', ') || '—'}</td><td className="help">{pr.age}</td>
                  <td className="row end">{!pr.system && <button className="small" onClick={() => { setOpen(open === pr.name ? '' : pr.name); setEditQ(qFromProject(pr)) }}>{open === pr.name ? 'Close' : 'Manage'}</button>}
                    {!pr.system && <button className="small danger" onClick={() => del(pr)}>Delete</button>}</td></tr>,
                open === pr.name && <tr key={pr.name + '-edit'}><td colSpan={5}>
                  <h3 style={{ marginTop: 6 }}>Access</h3>
                  <Bindings pr={pr} users={d.users} groups={d.groups} onAdd={b => call(() => api.post(`/api/clusters/${p.name}/projects/${pr.name}/bindings`, b), `Granted ${b.role} to ${b.name}.`)}
                    onRemove={rb => call(() => api.del(`/api/clusters/${p.name}/projects/${pr.name}/bindings/${rb}`), 'Access removed.')} />
                  <h3>Quota and defaults</h3>
                  {editQ && <QuotaForm q={editQ} setQ={setEditQ} />}
                  <div className="row end" style={{ marginTop: 8 }}><span className="help">Empty fields remove the limit.</span><button className="small primary" onClick={() => call(() => api.put(`/api/clusters/${p.name}/projects/${pr.name}/quota`, { quota: editQ }), 'Quota saved.')}>Save quota</button></div>
                </td></tr>]
            })}</tbody></table>
        ))}
      </div>
      <div className="panel">
        <h2>New project</h2>
        <div className="grid3">
          <Field label="Name" help="lowercase DNS label"><Text value={form.name} onChange={v => setForm({ ...form, name: v })} placeholder="team-a" /></Field>
          <Field label="Display name"><Text value={form.display} onChange={v => setForm({ ...form, display: v })} /></Field>
          <Field label="Description"><Text value={form.description} onChange={v => setForm({ ...form, description: v })} /></Field>
        </div>
        <h3>Quota and defaults <span className="help">— optional; empty means unlimited</span></h3>
        <QuotaForm q={quota} setQ={setQuota} />
        <h3>Access</h3>
        {binds.map((b, i) => <div className="row" key={i}><span className="mono">{b.kind === 'Group' ? 'group ' : ''}{b.name}</span><span className="help">{b.role}</span><button className="small" onClick={() => setBinds(binds.filter((_, j) => j !== i))}>✕</button></div>)}
        {d && <Bindings pr={{ name: 'new', bindings: [] }} users={d.users} groups={d.groups} onAdd={b => setBinds([...binds, { ...b, name: b.name.trim() }])} onRemove={() => {}} />}
        <div className="row end" style={{ marginTop: 12 }}><button className="primary" onClick={create} disabled={!form.name}>Create project</button></div>
      </div>
    </div>
  )
}
