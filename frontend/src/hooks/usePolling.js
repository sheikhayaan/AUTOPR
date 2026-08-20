import { useCallback, useEffect, useRef, useState } from 'react'

// Poll an async function on an interval, pausing while the tab is hidden.
//
// Returns { data, error, loading, refreshing, reload }:
//   - loading:    true only on the very first load (drives skeletons)
//   - refreshing: true during background/manual refreshes (drives the live dot)
//   - reload():   force an immediate silent refresh (manual refresh button)
export function usePolling(fn, intervalMs = 5000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [updatedAt, setUpdatedAt] = useState(null)

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
      setError(e)
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
