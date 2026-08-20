// Single source of truth for talking to the AutoPR API.
//
// Base is '/api' by default; in dev, Vite proxies that to the FastAPI backend
// (see vite.config.js). Override with VITE_API_BASE at build time to point a
// static build at an absolute API origin.
const BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function req(path, opts = {}) {
  let res
  try {
    res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    })
  } catch (networkErr) {
    const err = new Error('Cannot reach the AutoPR API. Is the server running?')
    err.cause = networkErr
    err.offline = true
    throw err
  }

  const text = await res.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }

  if (!res.ok) {
    const detail = data && (data.detail || data.status)
    const err = new Error(
      typeof detail === 'string' ? detail : `Request failed (HTTP ${res.status})`,
    )
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

export const api = {
  health: () => req('/healthz'),
  stats: () => req('/stats'),
  jobs: (limit = 50) => req(`/jobs?limit=${limit}`),
  pending: () => req('/reviews/pending'),
  reviews: (status, limit = 100) =>
    req(`/reviews?limit=${limit}${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  approve: (id) => req(`/reviews/${id}/approve`, { method: 'POST' }),
  reject: (id) => req(`/reviews/${id}/reject`, { method: 'POST' }),
}
