import { useState } from 'react'
import { fmtDate } from './ops.js'

/** Certificate summary with an accept checkbox and a show/hide details area (chain, SANs, PEM). */
export default function CertView({ title, bundle, accepted, onAccept }) {
  const [open, setOpen] = useState(false)
  if (!bundle) return null
  const l = bundle.leaf
  const expired = new Date(l.not_after) < new Date()
  return (
    <div className={`cert ${accepted ? 'accepted' : ''}`}>
      <div className="row"><b>{title}</b><span className="help mono">{bundle.host}:{bundle.port}</span><span className="spacer" />
        <button type="button" className="small" onClick={() => setOpen(!open)}>{open ? 'Hide certificate' : 'View certificate'}</button></div>
      <dl className="kv">
        <dt>Subject</dt><dd className="mono">{l.subject}</dd>
        <dt>Issuer</dt><dd className="mono">{l.issuer}{l.self_signed ? ' (self-signed)' : ''}</dd>
        <dt>Valid until</dt><dd style={expired ? { color: 'var(--fail)' } : {}}>{fmtDate(l.not_after)}{expired ? ' — expired' : ''}</dd>
        <dt>SHA-256</dt><dd className="fp">{l.sha256}</dd>
      </dl>
      {open && (
        <div>
          {bundle.chain.map((c, i) => (
            <div key={i} style={{ marginTop: 10 }}>
              <div className="help"><b>{i === 0 ? 'Server certificate' : `Chain certificate ${i}`}</b></div>
              <dl className="kv">
                <dt>Subject</dt><dd className="mono">{c.subject}</dd>
                <dt>Issuer</dt><dd className="mono">{c.issuer}</dd>
                <dt>Serial</dt><dd className="mono">{c.serial}</dd>
                <dt>Valid</dt><dd>{fmtDate(c.not_before)} → {fmtDate(c.not_after)}</dd>
                {c.sans.length > 0 && <><dt>Names</dt><dd className="mono">{c.sans.join(', ')}</dd></>}
                <dt>SHA-256</dt><dd className="fp">{c.sha256}</dd>
                <dt>SHA-1</dt><dd className="fp">{c.sha1}</dd>
              </dl>
              <pre className="pem">{c.pem}</pre>
            </div>
          ))}
        </div>
      )}
      <label className="row"><input type="checkbox" checked={!!accepted} onChange={e => onAccept(e.target.checked)} /> I checked the fingerprint and trust this certificate</label>
    </div>
  )
}
