import { useState } from 'react'
import { MASK } from '../api.js'
import { Field, Text, Select, Alert, RadioCards } from '../components/Field.jsx'
import { Badge } from '../components/CheckTable.jsx'
import Footer from '../components/Footer.jsx'
import JobLog from '../components/JobLog.jsx'
import { useFetch, jobRunner, fmtDate } from '../components/ops.js'

function CertCard({ title, c }) {
  if (!c) return null
  if (c.error) return <div className="stat"><div className="k">{title}</div><div className="v"><small>{c.error}</small></div></div>
  const color = c.internal_ca ? 'var(--text)' : c.days_left < 15 ? 'var(--fail)' : c.days_left < 45 ? 'var(--warn)' : 'var(--ok)'
  return <div className="stat"><div className="k">{title}</div><div className="v" style={{ color }}>{c.days_left} <small>days left</small></div>
    <div className="help">{c.internal_ca ? 'Cluster-internal CA: rotated automatically, but browsers warn' : c.self_signed ? 'self-signed' : `issued by ${c.issuer}`}<br />expires {fmtDate(c.not_after)} · {c.sans.slice(0, 2).join(', ')}</div></div>
}

export default function Certs(p) {
  const { spec, update } = p
  const cs = spec.day2.certs
  const { data: st, err, reload, setErr } = useFetch(p.name, '/certs')
  const [job, setJob] = useState(null)
  const run = jobRunner(p.name, setJob, setErr, () => p.dirty ? p.save() : true)
  const setC = (k, v) => update(s => s.day2.certs.custom[k] = v)
  const setA = (k, v) => update(s => s.day2.certs.acme[k] = v)
  const a = cs.acme, c = cs.custom
  const done = () => { reload(); p.reload() }
  const dom = `${spec.name}.${spec.base_domain}`
  return (
    <div>
      <div className="panel">
        <div className="row"><h2 style={{ margin: 0 }}>Certificates</h2><span className="spacer" /><button onClick={reload}>Refresh</button></div>
        <p className="lead">Replace the cluster-internal certificates for <span className="mono">api.{dom}</span> and <span className="mono">*.apps.{dom}</span> with ones your browsers trust: your own from a corporate CA, or Let's Encrypt issued and renewed by cert-manager through a DNS-01 challenge.</p>
        <Alert kind="error">{err}</Alert>
        {st && (
          <div className="stat-grid">
            <CertCard title={`API · api.${dom}`} c={st.api} />
            <CertCard title="Ingress · *.apps" c={st.apps} />
            <div className="stat"><div className="k">Configured</div><div className="v"><small>API: {st.api_named_certs?.length ? st.api_named_certs.map(n => n.servingCertificate?.name).join(', ') : 'default'} · ingress: {st.ingress_default_cert || 'default'}</small></div>
              {st.cert_manager && <div className="help">cert-manager: api {st.api_certificate?.ready || '—'} · apps {st.apps_certificate?.ready || '—'}{st.apps_certificate?.renewal ? ` · renews ${fmtDate(st.apps_certificate.renewal)}` : ''}</div>}</div>
          </div>
        )}
        <RadioCards value={cs.mode} onChange={v => update(s => s.day2.certs.mode = v)} options={[
          { value: 'custom', label: 'Bring your own certificates', desc: 'Paste certificate chains and keys issued by your CA (or bought). You handle renewals.' },
          { value: 'acme', label: "Let's Encrypt via DNS-01", desc: 'Installs the cert-manager operator, creates a ClusterIssuer with your DNS provider credentials and two Certificates that renew automatically.' },
        ]} />
        {cs.mode === 'custom' && (
          <div style={{ marginTop: 14 }}>
            <div className="grid2">
              <Field label={`API certificate (PEM, full chain) for api.${dom}`}><textarea value={c.api_cert_pem} onChange={e => setC('api_cert_pem', e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" /></Field>
              <Field label="API private key (PEM)" help={c.api_key_pem === MASK ? 'Stored. Paste again to replace.' : ''}><textarea value={c.api_key_pem === MASK ? '' : c.api_key_pem} onChange={e => setC('api_key_pem', e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" /></Field>
              <Field label={`Ingress wildcard certificate (PEM, full chain) for *.apps.${dom}`}><textarea value={c.apps_cert_pem} onChange={e => setC('apps_cert_pem', e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" /></Field>
              <Field label="Ingress private key (PEM)" help={c.apps_key_pem === MASK ? 'Stored. Paste again to replace.' : ''}><textarea value={c.apps_key_pem === MASK ? '' : c.apps_key_pem} onChange={e => setC('apps_key_pem', e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" /></Field>
            </div>
            <Field label="Issuing CA (PEM, optional)" help="Added to the cluster-wide trust bundle so in-cluster components trust the new certificates (needed for a private CA)."><textarea style={{ minHeight: 70 }} value={c.ca_pem} onChange={e => setC('ca_pem', e.target.value)} /></Field>
            <div className="toolbar"><span className="spacer" /><button className="primary" onClick={() => run('/certs/custom', {}, 'Install the pasted certificates? The API server restarts one master at a time (about 10 minutes).')}>Install certificates</button></div>
          </div>
        )}
        {cs.mode === 'acme' && (
          <div style={{ marginTop: 14 }}>
            <div className="grid3">
              <Field label="ACME account e-mail"><Text value={a.email} onChange={v => setA('email', v)} placeholder="you@example.com" /></Field>
              <Field label="Environment"><Select value={a.staging ? 'staging' : 'production'} onChange={v => setA('staging', v === 'staging')} options={[{ value: 'production', label: 'Production (trusted)' }, { value: 'staging', label: 'Staging (test, untrusted)' }]} /></Field>
              <Field label="DNS provider"><Select value={a.provider} onChange={v => setA('provider', v)} options={[{ value: 'cloudflare', label: 'Cloudflare' }, { value: 'route53', label: 'AWS Route 53' }, { value: 'rfc2136', label: 'RFC 2136 dynamic DNS (BIND, Windows DNS with TSIG)' }]} /></Field>
              {a.provider === 'cloudflare' && <Field label="Cloudflare API token" help={a.cloudflare_token === MASK ? 'Stored. Type to replace.' : 'Zone:DNS:Edit on the base domain zone'}><Text type="password" value={a.cloudflare_token === MASK ? '' : a.cloudflare_token} onChange={v => setA('cloudflare_token', v)} /></Field>}
              {a.provider === 'route53' && <>
                <Field label="AWS access key ID"><Text value={a.aws_access_key} onChange={v => setA('aws_access_key', v)} /></Field>
                <Field label="AWS secret access key" help={a.aws_secret_key === MASK ? 'Stored. Type to replace.' : ''}><Text type="password" value={a.aws_secret_key === MASK ? '' : a.aws_secret_key} onChange={v => setA('aws_secret_key', v)} /></Field>
                <Field label="Region"><Text value={a.aws_region} onChange={v => setA('aws_region', v)} /></Field>
                <Field label="Hosted zone ID (optional)"><Text value={a.aws_hosted_zone_id} onChange={v => setA('aws_hosted_zone_id', v)} /></Field></>}
              {a.provider === 'rfc2136' && <>
                <Field label="Nameserver" help="ip:port of the authoritative server accepting dynamic updates"><Text value={a.rfc2136_nameserver} onChange={v => setA('rfc2136_nameserver', v)} placeholder="192.168.1.10:53" /></Field>
                <Field label="TSIG key name"><Text value={a.rfc2136_tsig_key_name} onChange={v => setA('rfc2136_tsig_key_name', v)} placeholder="acme-key" /></Field>
                <Field label="TSIG secret" help={a.rfc2136_tsig_secret === MASK ? 'Stored. Type to replace.' : 'base64 secret from the key file'}><Text type="password" value={a.rfc2136_tsig_secret === MASK ? '' : a.rfc2136_tsig_secret} onChange={v => setA('rfc2136_tsig_secret', v)} /></Field>
                <Field label="TSIG algorithm"><Select value={a.rfc2136_tsig_algorithm} onChange={v => setA('rfc2136_tsig_algorithm', v)} options={['HMACSHA256', 'HMACSHA512', 'HMACSHA1', 'HMACMD5']} /></Field></>}
            </div>
            <p className="help">The base domain must be publicly resolvable for Let's Encrypt to validate the DNS-01 challenge, even if the cluster itself is private. Renewals happen automatically 30 days before expiry.</p>
            <div className="toolbar"><span className="spacer" /><button className="primary" onClick={() => run('/certs/acme', {}, `Request Let's Encrypt ${a.staging ? 'staging ' : ''}certificates for api.${dom} and *.apps.${dom}? This installs the cert-manager operator if needed.`)}>Request certificates</button></div>
          </div>
        )}
        {job && <div style={{ marginTop: 14 }}><JobLog cluster={p.name} jobId={job} onDone={done} /></div>}
      </div>
      <Footer {...p} next={null} />
    </div>
  )
}
