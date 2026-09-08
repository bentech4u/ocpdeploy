export function Badge({ s }) {
  return <span className={`badge ${s}`}>{s}</span>
}

export function Summary({ rows }) {
  const c = { pass: 0, warn: 0, fail: 0 }
  rows.forEach(r => { if (c[r.status] !== undefined) c[r.status]++ })
  return (
    <div className="summary">
      <span className="pill"><Badge s="pass" /> {c.pass}</span>
      <span className="pill"><Badge s="warn" /> {c.warn}</span>
      <span className="pill"><Badge s="fail" /> {c.fail}</span>
    </div>
  )
}

export default function CheckTable({ rows, showLevel }) {
  if (!rows || !rows.length) return <p className="muted">No results yet.</p>
  return (
    <table className="tbl">
      <thead><tr><th style={{ width: 70 }}>Status</th><th>Check</th>{showLevel && <th>Level</th>}<th>Expected</th><th>Actual</th><th>Hint</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td><Badge s={r.status} /></td>
            <td className="mono">{r.name}</td>
            {showLevel && <td>{r.level}</td>}
            <td className="mono">{r.expected}</td>
            <td className="mono">{r.actual}</td>
            <td className="help">{r.hint}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
