// Paging + search controls shared by the history views (Jobs, Reviews).
//
// The API returns `total` (pre-paging) alongside `count`, so a pager can be
// rendered from one response. Both components are deliberately dumb: they take
// values and callbacks, which makes them trivial to test and keeps URL/query
// state ownership with the page.
import { useEffect, useState } from 'react'
import { SearchIcon, ChevronDownIcon } from './Icons'

export function SearchBox({
  value,
  onChange,
  placeholder = 'Search…',
  label,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  label: string
}) {
  // Local mirror so typing stays instant while the parent debounces its fetch.
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  return (
    <div className="relative">
      <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint" />
      <input
        type="search"
        aria-label={label}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value)
          onChange(e.target.value)
        }}
        className="input w-full py-1.5 pl-8 pr-3 text-xs sm:w-64"
      />
    </div>
  )
}

export function Pager({
  total,
  limit,
  offset,
  onOffset,
  unit,
}: {
  total: number
  limit: number
  offset: number
  onOffset: (next: number) => void
  unit: string
}) {
  if (total <= limit) return null
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  const last = Math.max(0, Math.floor((total - 1) / limit) * limit)
  return (
    <nav className="mt-2 flex items-center gap-2 px-1 text-xs text-ink-faint" aria-label="Paging">
      <span className="tnum">
        {from}–{to} of {total} {unit}
      </span>
      <div className="ml-auto flex items-center gap-1.5">
        <button
          className="btn-ghost btn-sm"
          onClick={() => onOffset(Math.max(0, offset - limit))}
          disabled={offset === 0}
        >
          Previous
        </button>
        <button
          className="btn-ghost btn-sm"
          onClick={() => onOffset(Math.min(last, offset + limit))}
          disabled={to >= total}
        >
          Next
          <ChevronDownIcon className="h-3.5 w-3.5 -rotate-90" />
        </button>
      </div>
    </nav>
  )
}
