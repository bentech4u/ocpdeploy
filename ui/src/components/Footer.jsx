import { useNavigate } from 'react-router-dom'

/** Save / navigation bar shared by wizard steps. */
export default function Footer({ name, prev, next, save, saving, dirty, extra }) {
  const nav = useNavigate()
  return (
    <div className="wizard-footer">
      <div>{prev && <button onClick={() => nav(`/clusters/${name}/${prev}`)}>← Back</button>}</div>
      <div className="row">
        {extra}
        <button onClick={() => save()} disabled={saving || !dirty}>Save</button>
        {next && <button className="primary" onClick={() => save(next)} disabled={saving}>Save & continue →</button>}
      </div>
    </div>
  )
}
