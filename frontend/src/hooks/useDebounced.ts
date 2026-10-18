import { useEffect, useState } from 'react'

/**
 * Debounce a rapidly-changing value (e.g. a search box) so the dependent fetch
 * fires once the operator pauses, not on every keystroke.
 *
 * Returning `value` unchanged while `delayMs` elapses keeps the caller's render
 * cheap: only the settled value triggers a new request.
 */
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [settled, setSettled] = useState(value)

  useEffect(() => {
    if (value === settled) return
    const id = setTimeout(() => setSettled(value), delayMs)
    return () => clearTimeout(id)
    // `settled` is intentionally not a dependency: including it would restart the
    // timer every time the value settles, which is a no-op loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, delayMs])

  return settled
}
