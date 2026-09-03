import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'

interface FakeInit {
  ok?: boolean
  status?: number
  body?: string
}

// A minimal stand-in for Response — the client only touches ok/status/text().
function fakeRes({ ok = true, status = 200, body = '' }: FakeInit): Response {
  return { ok, status, text: async () => body } as unknown as Response
}

function mockFetch(fn: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  const f = vi.fn(fn)
  vi.stubGlobal('fetch', f)
  return f
}

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('api client', () => {
  it('parses JSON on success', async () => {
    mockFetch(async () => fakeRes({ body: JSON.stringify({ status: 'ok' }) }))
    await expect(api.health()).resolves.toEqual({ status: 'ok' })
  })

  it('flags network failures as offline', async () => {
    mockFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    await expect(api.stats()).rejects.toMatchObject({ offline: true })
  })

  it('surfaces the server detail + status on an HTTP error', async () => {
    mockFetch(async () =>
      fakeRes({ ok: false, status: 404, body: JSON.stringify({ detail: 'not found' }) }),
    )
    await expect(api.reject(9)).rejects.toMatchObject({ status: 404, message: 'not found' })
  })

  it('sends a bearer token from localStorage', async () => {
    localStorage.setItem('autopr_api_token', 'sekret')
    const f = mockFetch(async () => fakeRes({ body: '{}' }))
    await api.approve(1)
    const init = f.mock.calls[0][1] as RequestInit
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer sekret')
  })

  it('omits the Authorization header when no token is set', async () => {
    const f = mockFetch(async () => fakeRes({ body: '{}' }))
    await api.pending()
    const init = f.mock.calls[0][1] as RequestInit
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('targets the right path with a limit', async () => {
    const f = mockFetch(async () => fakeRes({ body: JSON.stringify({ count: 0, jobs: [] }) }))
    await api.jobs(25)
    expect(f.mock.calls[0][0]).toBe('/api/jobs?limit=25')
  })
})
