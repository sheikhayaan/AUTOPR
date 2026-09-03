// Single source of truth for talking to the AutoPR API.
//
// Base is '/api' by default; in dev, Vite proxies that to the FastAPI backend
// (see vite.config.ts). Override with VITE_API_BASE at build time to point a
// static build at an absolute API origin.
import type {
  ActionResponse,
  JobsResponse,
  PendingResponse,
  ReviewsResponse,
  Stats,
} from '../types'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

// The error shape the UI branches on: `offline` distinguishes "can't reach the
// server" from an HTTP error, and `status`/`data` carry the server's response
// when there was one.
export interface ApiError extends Error {
  status?: number
  data?: unknown
  offline?: boolean
  // The underlying network error, chained for debugging. Declared here because
  // the project targets ES2020, whose `Error` type predates the `cause` field.
  cause?: unknown
}

// Bearer token for the mutating/ops API. Resolution order:
//   1. localStorage['autopr_api_token'] — an operator can paste a token into the
//      dashboard at runtime, no rebuild needed.
//   2. VITE_AUTOPR_API_TOKEN — baked at build time for a fixed deployment.
// When neither is set, no Authorization header is sent — correct against a
// backend running with an empty AUTOPR_API_TOKEN (the documented dev no-op).
function authToken(): string {
  try {
    if (typeof localStorage !== 'undefined') {
      const t = localStorage.getItem('autopr_api_token')
      if (t) return t
    }
  } catch {
    // localStorage can throw (privacy mode / disabled) — fall through to env.
  }
  return import.meta.env.VITE_AUTOPR_API_TOKEN ?? ''
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((opts.headers as Record<string, string> | undefined) ?? {}),
  }
  const token = authToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let res: Response
  try {
    res = await fetch(BASE + path, { ...opts, headers })
  } catch (networkErr) {
    const err = new Error('Cannot reach the AutoPR API. Is the server running?') as ApiError
    err.cause = networkErr
    err.offline = true
    throw err
  }

  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }

  if (!res.ok) {
    const record = (data ?? {}) as Record<string, unknown>
    const detail = record.detail ?? record.status
    const err = new Error(
      typeof detail === 'string' ? detail : `Request failed (HTTP ${res.status})`,
    ) as ApiError
    err.status = res.status
    err.data = data
    throw err
  }
  return data as T
}

export const api = {
  health: () => req<{ status: string }>('/healthz'),
  stats: () => req<Stats>('/stats'),
  jobs: (limit = 50) => req<JobsResponse>(`/jobs?limit=${limit}`),
  pending: () => req<PendingResponse>('/reviews/pending'),
  reviews: (status?: string, limit = 100) =>
    req<ReviewsResponse>(
      `/reviews?limit=${limit}${status ? `&status=${encodeURIComponent(status)}` : ''}`,
    ),
  approve: (id: number) => req<ActionResponse>(`/reviews/${id}/approve`, { method: 'POST' }),
  reject: (id: number) => req<ActionResponse>(`/reviews/${id}/reject`, { method: 'POST' }),
}
