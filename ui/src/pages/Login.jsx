import { useState } from 'react'
import { api } from '../api.js'
import { Field, Text, Alert } from '../components/Field.jsx'

export function passwordRules(username, pw) {
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(rx => rx.test(pw)).length
  return [
    ['At least 12 characters', pw.length >= 12],
    ['Three of: lowercase, uppercase, digit, symbol', classes >= 3],
    ['Does not contain the username', !username || !pw.toLowerCase().includes(username.toLowerCase())],
    ['At least 6 different characters', new Set(pw).size >= 6],
  ]
}

export function Rules({ username, pw, confirm }) {
  const rules = passwordRules(username, pw)
  if (confirm !== undefined) rules.push(['Both passwords match', pw.length > 0 && pw === confirm])
  return (
    <ul className="rules">{rules.map(([t, ok]) => <li key={t} className={ok ? 'ok' : ''}>{ok ? '✓' : '○'} {t}</li>)}</ul>
  )
}

function Brand() {
  return (
    <div className="auth-brand">
      <span className="logo" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2.5l8 4.6v9.8l-8 4.6-8-4.6V7.1z" /><path d="M12 12l8-4.6M12 12v9.5M12 12L4 7.4" /></svg></span>
      <b>ocpdeploy</b>
    </div>
  )
}

export function Setup({ onDone }) {
  const [u, setU] = useState('admin')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const ok = passwordRules(u, pw).every(r => r[1]) && pw === pw2 && /^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$/.test(u)
  const submit = async (e) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try { await api.post('/api/auth/setup', { username: u, password: pw }); setPw(''); setPw2(''); onDone() } catch (x) { setErr(x.message) } finally { setBusy(false) }
  }
  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <Brand />
        <h1>Create the administrator account</h1>
        <p className="lead">This console can install, change and destroy clusters, so it needs a login. This first account is created once; manage accounts later with <span className="mono">ocpdeployctl user</span> on the installer host.</p>
        <Alert kind="error">{err}</Alert>
        <Field label="Username"><Text value={u} onChange={setU} autoComplete="username" /></Field>
        <Field label="Password"><Text type="password" value={pw} onChange={setPw} autoComplete="new-password" /></Field>
        <Field label="Repeat password"><Text type="password" value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        <Rules username={u} pw={pw} confirm={pw2} />
        <button className="primary block" type="submit" disabled={!ok || busy}>{busy ? 'Creating…' : 'Create account and sign in'}</button>
      </form>
    </div>
  )
}

export function Login({ onDone }) {
  const [u, setU] = useState('')
  const [pw, setPw] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try { await api.post('/api/auth/login', { username: u, password: pw }); setPw(''); onDone() } catch (x) { setErr(x.message) } finally { setBusy(false) }
  }
  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <Brand />
        <h1>Sign in</h1>
        <Alert kind="error">{err}</Alert>
        <Field label="Username"><Text value={u} onChange={setU} autoComplete="username" autoFocus /></Field>
        <Field label="Password"><Text type="password" value={pw} onChange={setPw} autoComplete="current-password" /></Field>
        <button className="primary block" type="submit" disabled={!u || !pw || busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        <p className="help" style={{ marginTop: 12 }}>Forgot the password? On the installer host run <span className="mono">ocpdeployctl user reset &lt;name&gt;</span>.</p>
      </form>
    </div>
  )
}

export function ChangePassword({ user, onClose }) {
  const [cur, setCur] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [done, setDone] = useState(false)
  const ok = cur && passwordRules(user, pw).every(r => r[1]) && pw === pw2
  const submit = async (e) => {
    e.preventDefault(); setErr('')
    try { await api.post('/api/auth/password', { current: cur, new: pw }); setCur(''); setPw(''); setPw2(''); setDone(true) } catch (x) { setErr(x.message) }
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={e => e.stopPropagation()} onSubmit={submit}>
        <h2>Change password</h2>
        {done ? <><Alert kind="ok">Password changed. Other sessions of {user} were signed out.</Alert><div className="row end"><button type="button" className="primary" onClick={onClose}>Close</button></div></> : <>
          <Alert kind="error">{err}</Alert>
          <Field label="Current password"><Text type="password" value={cur} onChange={setCur} autoComplete="current-password" /></Field>
          <Field label="New password"><Text type="password" value={pw} onChange={setPw} autoComplete="new-password" /></Field>
          <Field label="Repeat new password"><Text type="password" value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
          <Rules username={user} pw={pw} confirm={pw2} />
          <div className="row end"><button type="button" onClick={onClose}>Cancel</button><button className="primary" type="submit" disabled={!ok}>Change password</button></div>
        </>}
      </form>
    </div>
  )
}
