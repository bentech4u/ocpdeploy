import { useEffect, useState, useCallback } from 'react'
import { NavLink, Routes, Route, Navigate, useParams, useNavigate, useLocation } from 'react-router-dom'
import { api, MASK } from '../api.js'
import { Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import Basics from '../steps/Basics.jsx'
import VCenter from '../steps/VCenter.jsx'
import LoadBalancer from '../steps/LoadBalancer.jsx'
import Network from '../steps/Network.jsx'
import Nodes from '../steps/Nodes.jsx'
import Secrets from '../steps/Secrets.jsx'
import ProxyMirror from '../steps/ProxyMirror.jsx'
import Dns from '../steps/Dns.jsx'
import Preflight from '../steps/Preflight.jsx'
import Review from '../steps/Review.jsx'
import Operate from '../steps/Operate.jsx'
import Health from '../steps/Health.jsx'
import Upgrade from '../steps/Upgrade.jsx'
import Scale from '../steps/Scale.jsx'
import Identity from '../steps/Identity.jsx'
import Certs from '../steps/Certs.jsx'
import Storage from '../steps/Storage.jsx'
import Operators from '../steps/Operators.jsx'
import Backup from '../steps/Backup.jsx'
import Power from '../steps/Power.jsx'
import Apps from '../steps/Apps.jsx'
import ExtraNodes from '../steps/ExtraNodes.jsx'

/* key, label, component, group, done(spec) */
const STEPS = [
  ['basics', 'Cluster & version', Basics, 'Configure', s => !!s.ocp_version && !!s.base_domain],
  ['vcenter', 'Infrastructure', VCenter, 'Configure', s => s.install_method === 'agent' && s.provider !== 'vsphere'
    ? ({ proxmox: !!(s.proxmox.host && s.proxmox.token_id && s.proxmox.node), libvirt: !!s.libvirt.host, redfish: s.nodes.some(n => n.bmc_address), manual: true }[s.provider] || false)
    : !!(s.vcenter.host && s.vcenter.datacenter && s.vcenter.cluster && s.vcenter.datastore && s.vcenter.network)],
  ['network', 'Network', Network, 'Configure', s => !!(s.network.machine_cidr && s.network.gateway && s.network.dns_servers?.length)],
  ['nodes', 'Nodes', Nodes, 'Configure', s => s.nodes.some(n => n.role === 'master')],
  ['lb', 'Load balancer', LoadBalancer, 'Configure', s => s.lb.mode === 'none' || (s.lb.mode === 'external' ? !!(s.lb.external_api.ip && s.lb.external_apps.ip) : s.lb.vms.length > 0)],
  ['secrets', 'Secrets', Secrets, 'Configure', s => s.pull_secret === MASK && !!s.ssh_public_key],
  ['proxy', 'Proxy & mirror', ProxyMirror, 'Configure', s => !!(s.proxy.http_proxy || s.proxy.https_proxy || s.mirror.enabled)],
  ['dns', 'DNS records', Dns, 'Validate', null],
  ['preflight', 'Pre-flight', Preflight, 'Validate', null],
  ['review', 'Review & deploy', Review, 'Deploy', s => ['installed', 'deploying'].includes(s.status)],
  ['operate', 'Operate / Day 2', Operate, 'Operate', null],
  ['health', 'Health', Health, 'Operate', null],
  ['upgrade', 'Upgrade', Upgrade, 'Operate', null],
  ['scale', 'Scaling', Scale, 'Operate', null],
  ['identity', 'Identity providers', Identity, 'Operate', s => !!(s.day2?.identity?.htpasswd_enabled || s.day2?.identity?.ldap?.enabled || s.day2?.identity?.oidc?.enabled)],
  ['certs', 'Certificates', Certs, 'Operate', s => s.day2?.certs?.mode !== 'none'],
  ['storage', 'Storage & registry', Storage, 'Operate', null],
  ['operators', 'Operators', Operators, 'Operate', null],
  ['backup', 'etcd backup', Backup, 'Operate', s => !!s.day2?.backup?.schedule],
  ['power', 'Power', Power, 'Operate', null],
  ['apps', 'Applications', Apps, 'Operate', null],
  ['extranodes', 'Add Extra nodes', ExtraNodes, 'Operate', null],
]

const TOPOLOGY = { standard: 'Standard', compact: 'Compact 3-node', sno: 'Single node' }
// pages that work on a connected (imported) cluster; the server enforces the same list
const IMPORTED_STEPS = ['health', 'upgrade', 'scale', 'identity', 'certs', 'storage', 'operators', 'backup', 'apps', 'extranodes']

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
  const steps = spec.imported ? STEPS.filter(s => IMPORTED_STEPS.includes(s[0]) && (s[0] !== 'scale' || spec.install_method === 'ipi')) : STEPS.filter(s => s[0] !== 'extranodes')
  const idx = steps.findIndex(s => s[0] === cur)
  const props = { spec, update, save, reload, saving, dirty, name,
    next: spec.imported ? null : (idx >= 0 && idx < steps.length - 1 ? steps[idx + 1][0] : null),
    prev: spec.imported ? null : (idx > 0 ? steps[idx - 1][0] : null) }
  const statusBadge = spec.status === 'installed' ? 'pass' : spec.status === 'failed' ? 'fail' : spec.status === 'deploying' ? 'running' : 'grey'

  let lastGroup = null
  return (
    <div className="page split">
      <nav className="sidenav" aria-label="wizard steps">
        <div className="head">
          <b title={spec.cluster_domain || `${spec.name}.${spec.base_domain}`}>{spec.cluster_domain || `${spec.name}.${spec.base_domain}`}</b>
          <span className="status-line">{spec.imported ? `Connected${spec.read_only ? ' · read-only' : ''} · ${spec.ocp_version}` : `${spec.install_method.toUpperCase()} · ${spec.ocp_version || 'no version'} · ${spec.status}`}</span>
        </div>
        {steps.map(([k, label, , group, done], i) => {
          const header = group !== lastGroup ? <div className="group" key={`g-${group}`}>{group}</div> : null
          lastGroup = group
          const isDone = done ? done(spec) : false
          return (
            <span key={k}>
              {header}
              <NavLink to={`/clusters/${name}/${k}`} className={({ isActive }) => `${isActive ? 'active' : ''} ${isDone && !isActive ? 'done' : ''}`}>
                <span className="num"><span>{i + 1}</span></span>{label}
              </NavLink>
            </span>
          )
        })}
      </nav>
      <div>
        <div className="cluster-head">
          <h1>{spec.name}<span className="muted">{spec.cluster_domain ? ` · ${spec.cluster_domain}` : `.${spec.base_domain}`}</span></h1>
          <div className="tags">
            <Badge s={statusBadge} />
            {spec.imported && <span className="tag info">Connected</span>}
            {spec.read_only && <span className="tag">Read-only</span>}
            {!spec.imported && <span className="tag">{spec.install_method === 'ipi' ? 'IPI' : 'Agent'}</span>}
            {spec.imported && spec.platform && <span className="tag">{spec.platform}</span>}
            <span className="tag">{TOPOLOGY[spec.topology] || spec.topology}</span>
            {spec.ocp_version && <span className="tag accent">{spec.ocp_version}</span>}
            {spec.mirror?.enabled && <span className="tag">disconnected</span>}
          </div>
        </div>
        <Alert kind="error">{err}</Alert>
        {spec.read_only && <Alert kind="info">Read-only connection: you can look at everything, but every change is refused by the server.</Alert>}
        {dirty && <Alert kind="warn">Unsaved changes on this page.</Alert>}
        <div className={spec.read_only ? 'readonly-scope' : ''}>
          <Routes>
            {steps.map(([k, , C]) => <Route key={k} path={k} element={<C {...props} />} />)}
            <Route path="*" element={<Navigate to={spec.imported ? 'health' : 'basics'} replace />} />
          </Routes>
        </div>
      </div>
    </div>
  )
}
