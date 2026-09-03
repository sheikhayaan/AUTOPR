import { useCallback, useEffect, useRef, useState } from 'react'
import type { ApiError } from '../api/client'

// The shape usePolling returns. Generic over the fetched payload so callers get
// a fully-typed `data` with no casting at the call site.
export interface PollingState<T> {
  data: T | null
  error: ApiError | null
  loading: boolean
  refreshing: boolean
  updatedAt: number | null
  reload: () => void
}

// Poll an async function on an interval, pausing while the tab is hidden.
//
//   - loading:    true only on the very first load (drives skeletons)
//   - refreshing: true during background/manual refreshes (drives the live dot)
//   - reload():   force an immediate silent refresh (manual refresh button)
export function usePolling<T>(fn: () => Promise<T>, intervalMs = 5000): PollingState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)

  // Keep the latest fn in a ref so the polling effect never re-subscribes when
  // a parent passes a fresh closure each render.
  const fnRef = useRef(fn)
  fnRef.current = fn
  const inflight = useRef(false)

  const load = useCallback(async (silent = false) => {
    if (inflight.current) return
    inflight.current = true
    if (silent) setRefreshing(true)
    try {
      const d = await fnRef.current()
      setData(d)
      setError(null)
      setUpdatedAt(Date.now())
    } catch (e) {
      setError(e as ApiError)
    } finally {
      setLoading(false)
      setRefreshing(false)
      inflight.current = false
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') load(true)
    }, intervalMs)
    const onVisible = () => {
      if (document.visibilityState === 'visible') load(true)
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs])

  return { data, error, loading, refreshing, updatedAt, reload: () => load(true) }
}
