import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Select, Alert, RadioCards } from './Field.jsx'
import CertView from './CertView.jsx'

/** Connect to an existing OpenShift cluster. Credentials stay in this form until they are
 *  sent once; the server keeps the connection in RAM only. */
export default function Connect({ onConnected }) {
  const [method, setMethod] = useState('kubeconfig')
  const [server, setServer] = useState('')
  const [kc, setKc] = useState('')
  const [kcName, setKcName] = useState('')
  const [context, setContext] = useState('')
  const [username, setUsername] = useState('kubeadmin')
  const [password, setPassword] = useState('')
  const [token, setToken] = useState('')
  const [name, setName] = useState('')
  const [readOnly, setReadOnly] = useState(false)
  const [probe, setProbe] = useState(null)
  const [accApi, setAccApi] = useState(false)
  const [accOauth, setAccOauth] = useState(false)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const reset = () => { setProbe(null); setAccApi(false); setAccOauth(false) }
  const pickMethod = (m) => { setMethod(m); reset(); setErr('') }
  const readFile = (f) => {
    if (!f) return
    const r = new FileReader()
    r.onload = () => { setKc(String(r.result || '')); setKcName(f.name); setContext(''); reset() }
    r.readAsText(f)
  }
  const doProbe = async (ctx) => {
    setErr(''); setBusy('probe'); reset()
    try {
      const body = method === 'kubeconfig' ? { method, kubeconfig: kc, context: ctx ?? context } : { method, server: server.trim() }
      const p = await api.post('/api/imported/probe', body)
      setProbe(p)
      if (p.context) setContext(p.context)
    } catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const creds = method === 'kubeconfig' ? !!kc : method === 'password' ? !!(username && password) : !!token.trim()
  const canConnect = probe && accApi && (method !== 'password' || accOauth) && creds && !busy
  const connect = async () => {
    setErr(''); setBusy('connect')
    const body = { method, api_sha256: probe.api.sha256, name: name.trim() || null, read_only: readOnly }
    if (method === 'kubeconfig') Object.assign(body, { kubeconfig: kc, context })
    else Object.assign(body, { server: probe.server })
    if (method === 'password') Object.assign(body, { username, password, oauth_sha256: probe.oauth.sha256 })
    if (method === 'token') Object.assign(body, { token: token.trim() })
    try {
      const r = await api.post('/api/imported/connect', body)
      setPassword(''); setToken(''); setKc(''); setKcName(''); reset()
      onConnected(r)
    } catch (e) { setErr(e.message) } finally { setBusy('') }
  }

  return (
    <div>
      <RadioCards value={method} onChange={pickMethod} options={[
        { value: 'kubeconfig', label: 'Kubeconfig file', desc: 'Upload a kubeconfig with an inline token or client certificate, for example install/auth/kubeconfig or the output of oc config view --flatten.' },
        { value: 'password', label: 'Username and password', desc: 'Log in through the cluster OAuth server (kubeadmin, htpasswd, LDAP). The password is used once and never kept; the session token is revoked when you disconnect.' },
        { value: 'token', label: 'API token', desc: 'Paste an API URL and token, for example from "Copy login command" in the web console.' },
      ]} />
      <div className="grid2" style={{ marginTop: 14 }}>
        {method === 'kubeconfig' ? <>
          <Field label="Kubeconfig file" help={kcName ? `Loaded ${kcName}` : 'Read in the browser and sent once; not saved'}>
            <input type="file" onChange={e => readFile(e.target.files?.[0])} />
          </Field>
          {probe?.contexts?.length > 1 && <Field label="Context"><Select value={context} onChange={v => { setContext(v); doProbe(v) }} options={probe.contexts} /></Field>}
        </> : <Field label="API URL" help="https://api.<cluster>.<domain>:6443"><Text value={server} onChange={v => { setServer(v); reset() }} placeholder="https://api.ocp1.example.com:6443" /></Field>}
        {method === 'password' && <>
          <Field label="Username"><Text value={username} onChange={setUsername} autoComplete="off" /></Field>
          <Field label="Password"><Text type="password" value={password} onChange={setPassword} autoComplete="new-password" /></Field>
        </>}
        {method === 'token' && <Field label="Token"><Text type="password" value={token} onChange={setToken} autoComplete="off" placeholder="sha256~…" /></Field>}
      </div>
      <div className="toolbar"><button onClick={() => doProbe()} disabled={busy === 'probe' || (method === 'kubeconfig' ? !kc : !server.trim())}>{busy === 'probe' ? 'Checking…' : 'Check certificate'}</button>
        <span className="help">Nothing is sent to the cluster until you accept its certificate.</span></div>
      <Alert kind="error">{err}</Alert>
      {probe && (
        <div style={{ display: 'grid', gap: 12 }}>
          <CertView title="API server certificate" bundle={probe.api} accepted={accApi} onAccept={setAccApi} />
          {method === 'password' && <CertView title="OAuth server certificate (receives your password)" bundle={probe.oauth} accepted={accOauth} onAccept={setAccOauth} />}
          <div className="grid2">
            <Field label="Name in this console (optional)" help="Defaults to the cluster name; must be unique"><Text value={name} onChange={setName} placeholder="auto" /></Field>
            <Field label="Mode"><label className="row" style={{ marginTop: 8 }}><input type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} /> Read-only: view health, versions, operators and apps; every change is refused</label></Field>
          </div>
          <Alert kind="info">Nothing about this cluster is written to disk. The connection lives in memory and ends when you disconnect, sign out, your console session expires, the token expires, or the app restarts. Connect again with your credentials next time.</Alert>
          <div className="row end"><button className="primary" onClick={connect} disabled={!canConnect}>{busy === 'connect' ? 'Connecting…' : 'Connect'}</button></div>
        </div>
      )}
    </div>
  )
}
