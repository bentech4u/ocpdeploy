import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

/** Fetch a per-cluster endpoint with reload + error state. */
export function useFetch(cluster, path, deps = []) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const reload = useCallback(() => {
    setBusy(true); setErr('')
    return api.get(`/api/clusters/${cluster}${path}`).then(setData).catch(e => setErr(e.message)).finally(() => setBusy(false))
  }, [cluster, path])
  useEffect(() => { reload() }, [reload, ...deps])
  return { data, err, busy, reload, setErr }
}

/** Start a job with an optional confirm prompt; returns a runner bound to setters. */
export function jobRunner(cluster, setJob, setErr, saveFirst) {
  return async (path, body, msg) => {
    setErr('')
    if (msg && !confirm(msg)) return false
    try {
      if (saveFirst) { const ok = await saveFirst(); if (ok === false) return false }
      const r = await api.post(`/api/clusters/${cluster}${path}`, body ?? {})
      if (r && r.job_id) setJob(r.job_id)
      return r
    } catch (e) { setErr(e.message); return false }
  }
}

export const fmtDate = (s) => s ? String(s).replace('T', ' ').slice(0, 16) : ''
