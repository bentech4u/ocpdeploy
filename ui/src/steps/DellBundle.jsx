import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Alert, Select } from '../components/Field.jsx'
import { fmtDate } from '../components/ops.js'

const fmtBytes = (n) => n == null ? '' : n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(1)} MiB` : `${Math.max(1, Math.round(n / 1024))} KiB`

async function upload(component, file, sha256) {
  const q = new URLSearchParams({ component, filename: file.name, sha256: sha256 || '' })
  const r = await fetch(`/api/bundles/dell/upload?${q}`, { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/octet-stream' }, body: file })
  const text = await r.text()
  let d = null
  try { d = JSON.parse(text) } catch { /* plain */ }
  if (r.status === 401) window.dispatchEvent(new Event('ocpdeploy:unauthorized'))
  if (!r.ok) throw new Error(d?.detail || `${r.status} ${r.statusText}`)
  return d
}

export default function DellBundle() {
  const [b, setB] = useState(null)
  const [err, setErr] = useState('')
  const [info, setInfo] = useState('')
  const [busy, setBusy] = useState('')
  const [paths, setPaths] = useState({})
  const [shas, setShas] = useState({})
  const [imgVer, setImgVer] = useState('')
  const [imgs, setImgs] = useState('')
  const load = async (online) => {
    setErr('')
    try { const d = await api.get(`/api/bundles/dell?online=${online ? 'true' : 'false'}`); setB(d) } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load(true) }, [])
  if (!b) return <div><Alert kind="error">{err}</Alert><p className="muted">Loading…</p></div>

  const act = async (label, fn) => {
    setErr(''); setInfo(''); setBusy(label)
    try { const r = await fn(); setInfo(r?.component ? `${r.component} ${r.version} stored${r.verified ? ` (verified: ${r.verified})` : ' (no published checksum to compare)'}` : (r?.deleted ? `deleted ${r.deleted}` : 'done')); await load(false) }
    catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const catalogFor = (c) => (b.catalog?.rows || []).filter(r => r.component === c)
  const drivers = b.components.find(c => c.component === 'csi-isilon')?.files || []
  const repls = b.components.find(c => c.component === 'csm-replication')?.files || []
  const showImages = async (v) => {
    setImgVer(v); setImgs('')
    if (!v) return
    try {
      const m = drivers.find(f => f.version === v)
      const rv = repls[0]?.version || ''
      setImgs(await api.get(`/api/bundles/dell/images?version=${encodeURIComponent(m.version)}${rv ? `&replication=${encodeURIComponent(rv)}` : ''}`))
    } catch (e) { setErr(e.message) }
  }

  return (
    <div>
      <p className="help">Files for installs without internet on the installer host, kept in <span className="mono">{b.dir}</span> (only root can read it). Download them on any machine
        with internet (links and checksums below), then upload them here, or import them from a path on this host. Charts are checked against Dell's published digests.
        {b.catalog?.live ? ' This installer can reach Dell\'s chart index, so "Download here" works too.' : ' Dell\'s chart index is not reachable from this installer (offline): use upload or import.'}</p>
      <Alert kind="error">{err}</Alert>
      <Alert kind="info">{info}</Alert>
      {b.components.map(c => (
        <div key={c.component} className="card" style={{ marginBottom: 14 }}>
          <h3>{c.title}</h3>
          <span className="help">{c.desc}</span>
          {c.files.length ? (
            <table className="tbl"><thead><tr><th>Version</th><th>Checked</th><th>sha256</th><th>Size</th><th>Added</th><th></th></tr></thead>
              <tbody>{c.files.map(f => <tr key={f.version}>
                <td className="mono">{f.version}</td><td className="help">{f.verified || 'no published checksum'}</td>
                <td className="mono help" title={f.sha256}>{f.sha256.slice(0, 16)}…</td><td>{fmtBytes(f.size)}</td><td className="help">{fmtDate(f.added)}</td>
                <td><div className="row end">
                  <a className="btn small" href={`/api/bundles/dell/${c.component}/${encodeURIComponent(f.version)}/download`}>Download</a>
                  <button className="small danger" disabled={!!busy} onClick={() => confirm(`Delete ${c.component} ${f.version} from the bundle?`) && act('del', () => api.del(`/api/bundles/dell/${c.component}/${encodeURIComponent(f.version)}`))}>Delete</button>
                </div></td></tr>)}</tbody></table>
          ) : <p className="muted" style={{ margin: 0 }}>Not in the bundle yet.</p>}
          <div className="row">
            <label className="btn small" style={{ cursor: 'pointer' }}>Upload file…<input type="file" hidden disabled={!!busy} onChange={e => { const f = e.target.files[0]; e.target.value = ''; if (f) act('up', () => upload(c.component, f, shas[c.component])) }} /></label>
            <input type="text" placeholder="expected sha256 (optional)" value={shas[c.component] || ''} onChange={e => setShas({ ...shas, [c.component]: e.target.value.trim() })} style={{ width: 230 }} />
            <input type="text" placeholder={c.component === 'csi-isilon' ? '/root/isilon/.../charts/csi-isilon' : '/path/on/this/host'} value={paths[c.component] || ''} onChange={e => setPaths({ ...paths, [c.component]: e.target.value })} style={{ width: 280 }} />
            <button className="small" disabled={!!busy || !paths[c.component]} onClick={() => act('imp', () => api.post('/api/bundles/dell/import', { component: c.component, path: paths[c.component] }))}>Import from path</button>
            {busy && <span className="help">working…</span>}
          </div>
          {catalogFor(c.component).length > 0 && (
            <details><summary className="help">Published versions (download links and sha256)</summary>
              <table className="tbl"><tbody>{catalogFor(c.component).map(r => <tr key={r.version}>
                <td className="mono">{r.version}</td><td className="mono help"><a href={r.url} target="_blank" rel="noreferrer">{r.url}</a><div title={r.sha256}>sha256 {r.sha256}</div></td>
                <td>{r.present ? <span className="badge pass">present</span> : b.catalog.live && <button className="small" disabled={!!busy} onClick={() => act('fetch', () => api.post('/api/bundles/dell/fetch', { component: r.component, version: r.version }))}>Download here</button>}</td></tr>)}</tbody></table>
            </details>)}
          {c.component === 'repctl' && <p className="help" style={{ margin: 0 }}>repctl is only for running DR commands by hand; this app does the same through the Replication tab. Dell publishes it at
            <span className="mono"> https://github.com/dell/csm/releases/download/v1.17.2/repctl</span> (sha256 93cb3f3662780680feba31a0b545dc197380ae430fbeaf5a3827c6d40c9ea2dd).</p>}
        </div>
      ))}
      <div className="card">
        <h3>Container images</h3>
        <span className="help">The cluster pulls these (Helm method). For a disconnected cluster, mirror them and set "Image registry" on the Install tab, or add an ImageTagMirrorSet.
          The CSM Operator method uses the operator's certified images instead (mirror the certified-operators catalog package dell-csm-operator-certified).</span>
        <div className="row"><Select value={imgVer} onChange={showImages} options={drivers.map(f => f.version)} placeholder="csi-isilon chart version…" /></div>
        {imgs && <>
          <pre className="code">{imgs}</pre>
          <p className="help" style={{ margin: 0 }}>Mirror from a host with internet (logged in to your registry):</p>
          <pre className="code">{`while read img; do oc image mirror "$img" "REGISTRY/\${img#*/}"; done <<'EOF'\n${imgs}EOF`}</pre>
        </>}
      </div>
    </div>
  )
}
