import { useEffect, useState, useCallback } from 'react'
import { NavLink, Routes, Route, Navigate, useParams, useNavigate, useLocation } from 'react-router-dom'
import { api } from '../api.js'
import { Alert } from '../components/Field.jsx'
import Basics from '../steps/Basics.jsx'
import VCenter from '../steps/VCenter.jsx'
import LoadBalancer from '../steps/LoadBalancer.jsx'
import Network from '../steps/Network.jsx'
import Nodes from '../steps/Nodes.jsx'
import Secrets from '../steps/Secrets.jsx'
import Dns from '../steps/Dns.jsx'
import Preflight from '../steps/Preflight.jsx'
import Review from '../steps/Review.jsx'
import Operate from '../steps/Operate.jsx'

const STEPS = [
  ['basics', 'Cluster & version', Basics],
  ['vcenter', 'vCenter', VCenter],
  ['network', 'Network', Network],
  ['nodes', 'Nodes', Nodes],
  ['lb', 'Load balancer', LoadBalancer],
  ['secrets', 'Secrets', Secrets],
  ['dns', 'DNS records', Dns],
  ['preflight', 'Pre-flight', Preflight],
  ['review', 'Review & deploy', Review],
  ['operate', 'Operate / Day 2', Operate],
]

export default function Cluster() {
  const { name } = useParams()
  const nav = useNavigate()
  const loc = useLocation()
  const [spec, setSpec] = useState(null)
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)

  const reload = useCallback(() => api.get(`/api/clusters/${name}`).then(s => { setSpec(s); setDirty(false) }).catch(e => setErr(e.message)), [name])
  useEffect(() => { reload() }, [reload])

  const update = (fn) => { setSpec(prev => { const n = structuredClone(prev); fn(n); return n }); setDirty(true) }

  const save = async (next) => {
    setErr(''); setSaving(true)
    try {
      const s = await api.put(`/api/clusters/${name}`, spec)
      setSpec(s); setDirty(false)
      if (next) nav(`/clusters/${name}/${next}`)
      return true
    } catch (e) { setErr(e.message); return false } finally { setSaving(false) }
  }

  if (!spec) return <div className="page"><Alert kind="error">{err}</Alert><p className="muted">Loading…</p></div>
  const cur = loc.pathname.split('/').pop()
  const idx = STEPS.findIndex(s => s[0] === cur)
  const props = { spec, update, save, reload, saving, dirty, name,
    next: idx >= 0 && idx < STEPS.length - 1 ? STEPS[idx + 1][0] : null,
    prev: idx > 0 ? STEPS[idx - 1][0] : null }

  return (
    <div className="page split">
      <nav className="sidenav">
        <div style={{ padding: '6px 14px 10px' }}>
          <b>{spec.name}.{spec.base_domain}</b><br />
          <span className="status-line">{spec.install_method.toUpperCase()} · {spec.ocp_version || 'no version'} · {spec.status}</span>
        </div>
        {STEPS.map(([k, label], i) => (
          <NavLink key={k} to={`/clusters/${name}/${k}`} className={({ isActive }) => isActive ? 'active' : ''}>
            <span className="num">{i + 1}.</span>{label}
          </NavLink>
        ))}
      </nav>
      <div>
        <Alert kind="error">{err}</Alert>
        {dirty && <Alert kind="warn">Unsaved changes on this page.</Alert>}
        <Routes>
          {STEPS.map(([k, , C]) => <Route key={k} path={k} element={<C {...props} />} />)}
          <Route path="*" element={<Navigate to="basics" replace />} />
        </Routes>
      </div>
    </div>
  )
}
