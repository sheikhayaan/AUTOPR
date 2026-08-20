import { RefreshIcon } from './Icons.jsx'
import { relativeTime } from '../lib/format.js'
import { useEffect, useState } from 'react'

// Page-level header: title + subtitle on the left, a live indicator and manual
// refresh on the right. `updatedAt` is a timestamp (ms); it re-renders every
// few seconds so "updated Ns ago" stays honest.
export default function PageHeader({ title, subtitle, updatedAt, refreshing, onRefresh, children }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="flex flex-col gap-4 border-b border-line pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-ink-dim">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-3">
        {children}
        <div className="flex items-center gap-2 text-xs text-ink-faint">
          <span className="relative flex h-2 w-2">
            <span
              className={`absolute inline-flex h-full w-full rounded-full bg-risk-low opacity-75 ${
                refreshing ? 'animate-ping' : ''
              }`}
            />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-risk-low" />
          </span>
          <span className="hidden sm:inline">
            {updatedAt ? `Updated ${relativeTime(new Date(updatedAt).toISOString())}` : 'Live'}
          </span>
        </div>
        {onRefresh && (
          <button onClick={onRefresh} className="btn-ghost btn-sm" title="Refresh now">
            <RefreshIcon className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
            <span className="hidden sm:inline">Refresh</span>
          </button>
        )}
      </div>
    </div>
  )
}
