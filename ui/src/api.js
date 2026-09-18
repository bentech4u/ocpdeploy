async function call(method, path, body) {
  const r = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'content-type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await r.text()
  let data = text
  try { data = text ? JSON.parse(text) : null } catch { /* plain text */ }
  if (!r.ok) {
    // session ended (logout elsewhere, password change, expiry): send the app back to the login screen
    if (r.status === 401 && !path.startsWith('/api/auth/') && !path.startsWith('/api/imported/')) window.dispatchEvent(new Event('ocpdeploy:unauthorized'))
    const msg = (data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `${r.status} ${r.statusText}`
    throw new Error(msg)
  }
  return data
}
export const api = {
  get: (p) => call('GET', p),
  post: (p, b) => call('POST', p, b ?? {}),
  put: (p, b) => call('PUT', p, b),
  del: (p) => call('DELETE', p),
}
export const MASK = '********'
