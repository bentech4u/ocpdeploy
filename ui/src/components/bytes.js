export const gib = (b) => b == null ? '—' : (b / 2 ** 30 >= 100 ? (b / 2 ** 30).toFixed(0) : (b / 2 ** 30).toFixed(1)) + ' GiB'
export const cores = (c) => c == null ? '—' : (c >= 10 ? c.toFixed(0) : c.toFixed(2).replace(/\.?0+$/, '')) + ''
export const pct = (x) => x == null ? '—' : `${Math.round(x * 100)}%`
export const tone = (x) => x == null ? 'var(--muted)' : x >= 0.95 ? 'var(--fail)' : x >= 0.85 ? 'var(--warn)' : 'var(--ok)'
