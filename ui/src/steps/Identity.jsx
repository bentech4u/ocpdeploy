import { useState } from 'react'
import { api, MASK } from '../api.js'
import { Field, Text, Select, Alert } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import Footer from '../components/Footer.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner } from '../components/ops.js'

export default function Identity(p) {
  const { spec, update } = p
  const id = spec.day2.identity
  const { data: st, err, reload, setErr } = useFetch(p.name, '/identity')
  const [job, setJob] = useState(null)
  const [confirmName, setConfirmName] = useState('')
  const [preview, setPreview] = useState(null)
  const set = (k, v) => update(s => s.day2.identity[k] = v)
  const setL = (k, v) => update(s => s.day2.identity.ldap[k] = v)
  const setO = (k, v) => update(s => s.day2.identity.oidc[k] = v)
  const setU = (i, k, v) => update(s => s.day2.identity.users[i][k] = v)
  const run = jobRunner(p.name, setJob, setErr, () => p.dirty ? p.save() : true)
  const doPreview = async () => { setErr(''); try { if (p.dirty) { if (!(await p.save())) return } setPreview(await api.get(`/api/clusters/${p.name}/identity/preview`)) } catch (e) { setErr(e.message) } }
  const apply = () => run('/identity/apply', {}, 'Apply the identity providers to the cluster? This replaces the OAuth identityProviders list with the ones configured here and rolls the authentication pods.')
  const disable = () => run('/identity/disable-kubeadmin', { confirm: confirmName }, 'Remove the kubeadmin user permanently? Make sure another cluster-admin can log in first.')
  const done = () => { reload(); p.reload() }
  return (
    <div>
      <div className="panel">
        <h2>Identity providers</h2>
        <p className="lead">Configure how people log in to the console and <span className="mono">oc</span>: local users (htpasswd), an LDAP / Active Directory directory, or an OpenID Connect provider (Keycloak, Entra ID, Google…). The app owns the OAuth identity provider list.</p>
        <Alert kind="error">{err}</Alert>
        {st && (
          <div className="stat-grid">
            <div className="stat"><div className="k">Providers</div><div className="v">{st.identity_providers.length ? st.identity_providers.map(i => `${i.name} (${i.type})`).join(', ') : <small>none</small>}</div></div>
            <div className="stat"><div className="k">Users seen</div><div className="v">{st.users.length}<small> {st.users.slice(0, 5).join(', ')}</small></div></div>
            <div className="stat"><div className="k">cluster-admin users</div><div className="v">{st.cluster_admins.filter(a => a !== 'system:admin').join(', ') || <small>only kubeadmin</small>}</div></div>
            <div className="stat"><div className="k">kubeadmin</div><div className="v">{st.kubeadmin_present ? <span style={{ color: 'var(--warn)' }}>present</span> : <span style={{ color: 'var(--ok)' }}>removed</span>}</div></div>
          </div>
        )}
        <h3>Local users (htpasswd)</h3>
        <label className="row"><input type="checkbox" checked={!!id.htpasswd_enabled} onChange={e => set('htpasswd_enabled', e.target.checked)} /> Enable the htpasswd provider <span className="help">— name</span> <Text value={id.htpasswd_name} onChange={v => set('htpasswd_name', v)} style={{ maxWidth: 140 }} /></label>
        {id.htpasswd_enabled && (
          <>
            <table className="tbl" style={{ marginTop: 10 }}>
              <thead><tr><th>Username</th><th>Password</th><th>cluster-admin</th><th></th></tr></thead>
              <tbody>{id.users.map((u, i) => <tr key={i}>
                <td><Text value={u.username} onChange={v => setU(i, 'username', v)} /></td>
                <td><Text type="password" value={u.password === MASK ? '' : u.password} onChange={v => setU(i, 'password', v)} placeholder={st?.stored_users?.includes(u.username) || u.password === MASK ? 'stored — type to change' : 'new password'} /></td>
                <td><input type="checkbox" checked={!!u.cluster_admin} onChange={e => setU(i, 'cluster_admin', e.target.checked)} /></td>
                <td><button className="small" onClick={() => update(s => s.day2.identity.users.splice(i, 1))}>✕</button></td></tr>)}</tbody>
            </table>
            <div className="toolbar"><button onClick={() => update(s => s.day2.identity.users.push({ username: '', password: '', cluster_admin: false }))}>+ Add user</button><span className="help">Passwords are bcrypt-hashed into the htpasswd secret and then cleared from the app's store. Removing a user here removes it from the cluster on the next apply.</span></div>
          </>
        )}
        <h3>LDAP / Active Directory</h3>
        <label className="row"><input type="checkbox" checked={!!id.ldap.enabled} onChange={e => setL('enabled', e.target.checked)} /> Enable LDAP</label>
        {id.ldap.enabled && (
          <div className="grid3" style={{ marginTop: 10 }}>
            <Field label="Provider name"><Text value={id.ldap.name} onChange={v => setL('name', v)} /></Field>
            <Field label="LDAP URL" help="ldaps://host/base-dn?attribute?scope?filter — AD: ldaps://dc.example.com/DC=example,DC=com?sAMAccountName"><Text value={id.ldap.url} onChange={v => setL('url', v)} placeholder="ldaps://dc.example.com/DC=example,DC=com?sAMAccountName" /></Field>
            <Field label="Bind DN" help="Empty for anonymous bind"><Text value={id.ldap.bind_dn} onChange={v => setL('bind_dn', v)} placeholder="CN=svc-ocp,OU=Service,DC=example,DC=com" /></Field>
            <Field label="Bind password" help={id.ldap.bind_password === MASK ? 'Stored. Type to replace.' : ''}><Text type="password" value={id.ldap.bind_password === MASK ? '' : id.ldap.bind_password} onChange={v => setL('bind_password', v)} /></Field>
            <Field label="ID attribute"><Text value={id.ldap.attr_id} onChange={v => setL('attr_id', v)} /></Field>
            <Field label="Preferred username attribute" help="AD: sAMAccountName"><Text value={id.ldap.attr_preferred_username} onChange={v => setL('attr_preferred_username', v)} /></Field>
            <Field label="Name attribute"><Text value={id.ldap.attr_name} onChange={v => setL('attr_name', v)} /></Field>
            <Field label="E-mail attribute"><Text value={id.ldap.attr_email} onChange={v => setL('attr_email', v)} /></Field>
            <Field label="TLS"><label className="row"><input type="checkbox" checked={!!id.ldap.insecure} onChange={e => setL('insecure', e.target.checked)} /> insecure (ldap:// without TLS)</label></Field>
            <Field label="CA certificate (PEM)" help="For ldaps:// with a private CA"><textarea style={{ minHeight: 60 }} value={id.ldap.ca_pem} onChange={e => setL('ca_pem', e.target.value)} /></Field>
          </div>
        )}
        <h3>OpenID Connect</h3>
        <label className="row"><input type="checkbox" checked={!!id.oidc.enabled} onChange={e => setO('enabled', e.target.checked)} /> Enable OIDC</label>
        {id.oidc.enabled && (
          <div className="grid3" style={{ marginTop: 10 }}>
            <Field label="Provider name"><Text value={id.oidc.name} onChange={v => setO('name', v)} /></Field>
            <Field label="Issuer URL" help="Must serve /.well-known/openid-configuration"><Text value={id.oidc.issuer} onChange={v => setO('issuer', v)} placeholder="https://keycloak.example.com/realms/lab" /></Field>
            <Field label="Client ID"><Text value={id.oidc.client_id} onChange={v => setO('client_id', v)} /></Field>
            <Field label="Client secret" help={id.oidc.client_secret === MASK ? 'Stored. Type to replace.' : `Redirect URI: https://oauth-openshift.apps.${spec.name}.${spec.base_domain}/oauth2callback/${id.oidc.name || 'oidc'}`}><Text type="password" value={id.oidc.client_secret === MASK ? '' : id.oidc.client_secret} onChange={v => setO('client_secret', v)} /></Field>
            <Field label="Preferred username claim"><Text value={id.oidc.claim_preferred_username} onChange={v => setO('claim_preferred_username', v)} /></Field>
            <Field label="Name claim"><Text value={id.oidc.claim_name} onChange={v => setO('claim_name', v)} /></Field>
            <Field label="E-mail claim"><Text value={id.oidc.claim_email} onChange={v => setO('claim_email', v)} /></Field>
            <Field label="Groups claim (optional)"><Text value={id.oidc.claim_groups} onChange={v => setO('claim_groups', v)} placeholder="groups" /></Field>
            <Field label="Extra scopes" help="Comma separated"><Text value={(id.oidc.extra_scopes || []).join(', ')} onChange={v => setO('extra_scopes', v.split(/[,\s]+/).filter(Boolean))} /></Field>
            <Field label="CA certificate (PEM, optional)"><textarea style={{ minHeight: 60 }} value={id.oidc.ca_pem} onChange={e => setO('ca_pem', e.target.value)} /></Field>
          </div>
        )}
        <h3>Cluster administrators</h3>
        <Field label="Additional cluster-admin users" help="Comma separated user names as they appear after login (LDAP / OIDC users). htpasswd users use the checkbox above.">
          <Text value={(id.cluster_admins || []).join(', ')} onChange={v => set('cluster_admins', v.split(/[,\s]+/).filter(Boolean))} />
        </Field>
        <div className="toolbar">
          <button onClick={doPreview}>Preview resources</button>
          <span className="spacer" />
          <button className="primary" onClick={apply}>Apply identity providers</button>
        </div>
        {preview && <pre className="code">{JSON.stringify(preview, null, 2)}</pre>}
        <h3>Remove kubeadmin</h3>
        <p className="help">Once another cluster-admin can log in, delete the temporary kubeadmin user. This cannot be undone; the app keeps working through its certificate-based kubeconfig.</p>
        <div className="row"><input type="text" value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={spec.name} style={{ width: 160 }} />
          <button className="danger" onClick={disable} disabled={confirmName !== spec.name || (st && !st.kubeadmin_present)}>{st && !st.kubeadmin_present ? 'kubeadmin already removed' : 'Remove kubeadmin'}</button></div>
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
      <Footer {...p} next={null} />
    </div>
  )
}
