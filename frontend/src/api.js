export const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000'

async function call(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok && res.status !== 429) throw new Error(data.detail || `${res.status} ${res.statusText}`)
  return data
}

export const api = {
  metrics: () => call('GET', '/v1/metrics'),
  timeseries: (since) => call('GET', `/v1/metrics/timeseries?since=${since}`),
  models: () => call('GET', '/v1/models'),
  setLimits: (model, rpm, tpm) => call('PUT', `/v1/models/${model}/limits`, { rpm, tpm }),
  batches: () => call('GET', '/v1/batches?limit=20'),
  batch: (id) => call('GET', `/v1/batches/${id}`),
  createBatch: (body) => call('POST', '/v1/batches', body),
  submit: (body) => call('POST', '/v1/requests', body),
  reset: () => call('POST', '/v1/admin/reset'),
}

export const fmt = (n) => {
  if (n === null || n === undefined) return '—'
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(1)}k`
  return n.toLocaleString()
}
