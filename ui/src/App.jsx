import { useEffect, useState } from 'react'
import { Routes, Route, Navigate, Link, useLocation } from 'react-router-dom'
import { api } from './api.js'
import Clusters from './pages/Clusters.jsx'
import Cluster from './pages/Cluster.jsx'

function Logo() {
  return (
    <span className="logo" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2.5l8 4.6v9.8l-8 4.6-8-4.6V7.1z" />
        <path d="M12 12l8-4.6M12 12v9.5M12 12L4 7.4" />
      </svg>
    </span>
  )
}

export default function App() {
  const loc = useLocation()
  const [sys, setSys] = useState(null)
  useEffect(() => { api.get('/api/system').then(setSys).catch(() => {}) }, [])
  const parts = loc.pathname.split('/').filter(Boolean)
  const clusterName = parts[0] === 'clusters' ? parts[1] : null
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand"><Logo />ocpdeploy</Link>
        <span className="muted subtitle">OpenShift on vSphere</span>
        <nav className="crumbs" aria-label="breadcrumb">
          <Link to="/">Clusters</Link>
          {clusterName && <><span className="sep">›</span><span className="cur">{clusterName}</span></>}
        </nav>
        <div className="right">
          {sys?.hostname && <span className="chip" title="installer host">{sys.hostname}</span>}
          {sys?.app_version && <span className="chip">v{sys.app_version}</span>}
          <a className="chip coffee" href="https://buymeacoffee.com/bentech4u" target="_blank" rel="noreferrer" title="Support the project">☕ Buy me a coffee</a>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<Clusters />} />
        <Route path="/clusters/:name/*" element={<Cluster />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </div>
  )
}
