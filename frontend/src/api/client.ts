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
//   1. localStorage['autopr_api_token'] — an operator pastes a token into the
//      dashboard's Settings dialog at runtime, no rebuild needed.
//   2. VITE_AUTOPR_API_TOKEN — baked at build time for a fixed deployment.
// When neither is set, no Authorization header is sent — correct against a
// backend running with an empty AUTOPR_API_TOKEN (the documented dev no-op).
const TOKEN_KEY = 'autopr_api_token'

export type TokenSource = 'saved' | 'build' | 'none'

/** The token the API calls will use ('' when unauthenticated). */
export function getToken(): string {
  try {
    if (typeof localStorage !== 'undefined') {
      const t = localStorage.getItem(TOKEN_KEY)
      if (t) return t
    }
  } catch {
    // localStorage can throw (privacy mode / disabled) — fall through to env.
  }
  return import.meta.env.VITE_AUTOPR_API_TOKEN ?? ''
}

/** Where the active token came from — the UI says so instead of guessing. */
export function tokenSource(): TokenSource {
  try {
    if (typeof localStorage !== 'undefined' && localStorage.getItem(TOKEN_KEY)) return 'saved'
  } catch {
    /* ignore */
  }
  return import.meta.env.VITE_AUTOPR_API_TOKEN ? 'build' : 'none'
}

/** Persist a token for subsequent requests. Empty string clears it. */
export function setToken(token: string): void {
  try {
    if (typeof localStorage === 'undefined') return
    const trimmed = token.trim()
    if (trimmed) localStorage.setItem(TOKEN_KEY, trimmed)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Nothing we can do; the request will simply be unauthenticated.
  }
}

export function clearToken(): void {
  setToken('')
}

/**
 * Check a token against the API before the operator commits to it.
 *
 * Uses the read-only `/stats` endpoint, which is exactly as protected as the
 * dashboard's own reads: if this succeeds the token is good enough to load the
 * UI, and an approve/reject attempt (same header) will be treated the same way.
 * Resolves `{ ok: true }` or `{ ok: false, detail }` — it never throws, because a
 * failed probe is a normal outcome of typing a wrong token.
 */
export async function verifyToken(
  token: string,
): Promise<{ ok: true } | { ok: false; detail: string }> {
  const saved = getToken()
  setToken(token)
  try {
    await req<Stats>('/stats')
    return { ok: true }
  } catch (err) {
    const e = err as ApiError
    return { ok: false, detail: e.offline ? 'Cannot reach the API' : e.message }
  } finally {
    if (token.trim() !== saved) setToken(saved)
  }
}

function authToken(): string {
  return getToken()
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

export interface JobsQuery {
  limit?: number
  offset?: number
  q?: string
  status?: string
}

export type ReviewsQuery = JobsQuery

/** Build a `?a=b&c=d` string, dropping empty/undefined params. */
function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === '' || v === null) continue
    search.set(k, String(v))
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => req<{ status: string }>('/healthz'),
  stats: () => req<Stats>('/stats'),
  jobs: (params: number | JobsQuery = {}) => {
    // Accept a bare number for back-compat with existing callers/tests.
    const q = typeof params === 'number' ? { limit: params } : params
    return req<JobsResponse>(
      `/jobs${query({ limit: q.limit ?? 50, offset: q.offset, q: q.q, status: q.status })}`,
    )
  },
  pending: () => req<PendingResponse>('/reviews/pending'),
  reviews: (params: string | ReviewsQuery = {}, limit = 100) => {
    // Back-compat: reviews(statusString, limit).
    const q = typeof params === 'string' ? { status: params, limit } : params
    return req<ReviewsResponse>(
      `/reviews${query({
        limit: q.limit ?? limit,
        offset: q.offset,
        q: q.q,
        status: q.status,
      })}`,
    )
  },
  approve: (id: number) => req<ActionResponse>(`/reviews/${id}/approve`, { method: 'POST' }),
  reject: (id: number) => req<ActionResponse>(`/reviews/${id}/reject`, { method: 'POST' }),
}
