export function Field({ label, help, children }) {
  return (
    <label className="field">
      <span className="lbl">{label}</span>
      {children}
      {help && <span className="help">{help}</span>}
    </label>
  )
}

export function Text({ value, onChange, type = 'text', ...rest }) {
  return <input type={type} value={value ?? ''} onChange={e => onChange(e.target.value)} {...rest} />
}

export function Num({ value, onChange, ...rest }) {
  return <input type="number" value={value ?? ''} onChange={e => onChange(Number(e.target.value))} {...rest} />
}

export function Select({ value, onChange, options, placeholder }) {
  return (
    <select value={value ?? ''} onChange={e => onChange(e.target.value)}>
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map(o => typeof o === 'string'
        ? <option key={o} value={o}>{o}</option>
        : <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

export function Alert({ kind = 'info', children }) {
  if (!children) return null
  return <div className={`alert ${kind}`}>{children}</div>
}

export function RadioCards({ value, onChange, options }) {
  return (
    <div className="radio-cards">
      {options.map(o => (
        <div key={o.value} className={`radio-card ${value === o.value ? 'on' : ''}`} onClick={() => onChange(o.value)}>
          <b>{o.label}</b>
          <span className="help">{o.desc}</span>
        </div>
      ))}
    </div>
  )
}
