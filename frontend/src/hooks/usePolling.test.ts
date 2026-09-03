import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { usePolling } from './usePolling'

// Drive document.visibilityState (usePolling pauses polling when hidden).
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('usePolling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('loads once on mount and exposes the data', async () => {
    const fn = vi.fn().mockResolvedValue({ ok: true })
    const { result } = renderHook(() => usePolling(fn, 1000))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(fn).toHaveBeenCalledTimes(1)
    expect(result.current.data).toEqual({ ok: true })
    expect(result.current.loading).toBe(false)
  })

  it('polls again on the interval while the tab is visible', async () => {
    const fn = vi.fn().mockResolvedValue(1)
    renderHook(() => usePolling(fn, 1000))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(fn).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('pauses polling while the tab is hidden', async () => {
    const fn = vi.fn().mockResolvedValue(1)
    renderHook(() => usePolling(fn, 1000))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(fn).toHaveBeenCalledTimes(1)

    setVisibility('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    // No further polls fired while hidden.
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('captures errors without throwing', async () => {
    const boom = new Error('nope')
    const fn = vi.fn().mockRejectedValue(boom)
    const { result } = renderHook(() => usePolling(fn, 1000))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(result.current.error).toBe(boom)
    expect(result.current.loading).toBe(false)
  })
})
