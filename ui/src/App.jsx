import { Routes, Route, Navigate, Link } from 'react-router-dom'
import Clusters from './pages/Clusters.jsx'
import Cluster from './pages/Cluster.jsx'

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">ocpdeploy</Link>
        <span className="muted">OpenShift on vSphere · install console</span>
      </header>
      <Routes>
        <Route path="/" element={<Clusters />} />
        <Route path="/clusters/:name/*" element={<Cluster />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </div>
  )
}
