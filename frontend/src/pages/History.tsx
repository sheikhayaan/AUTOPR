// Decision history: the full routing ledger, searchable and paged.
//
// `/reviews` (the server-side ledger) existed with a pager-friendly shape but no
// consumer — the Review Queue only ever showed PENDING rows, so an operator
// could never answer "what did AutoPR do last week?". This page reads it with
// server-side filtering so the history stays cheap as it grows.
import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { useDebounced } from '../hooks/useDebounced'
import { api } from '../api/client'
import PageHeader from '../components/PageHeader'
import RiskBadge from '../components/RiskBadge'
import StatusBadge from '../components/StatusBadge'
import EmptyState from '../components/EmptyState'
import { Pager, SearchBox } from '../components/ListControls'
import { SkeletonRows } from '../components/Skeleton'
import { AlertIcon, ExternalLinkIcon, InboxIcon } from '../components/Icons'
import { shortSha, relativeTime, absTime, actionLabel, pluralize } from '../lib/format'
import type { Decision, DecisionStatus, OutletContext } from '../types'

const PAGE_SIZE = 20

const STATUS_FILTERS: Array<'all' | DecisionStatus> = [
  'all',
  'pending',
  'executed',
  'rejected',
  'failed',
  'approved',
]

function DecisionRow({
  decision,
  webUrl,
  expanded,
  onToggle,
}: {
  decision: Decision
  webUrl: string
  expanded: boolean
  onToggle: () => void
}) {
  const prUrl = `${webUrl}/${decision.repo}/pull/${decision.pr_number}`
  return (
    <div className="rounded-xl border border-line bg-surface/60">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <div className="min-w-0 flex-1">
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="group inline-flex items-center gap-1.5 text-sm font-medium text-ink hover:text-brand-300"
          >
            <span className="truncate">{decision.repo}</span>
            <span className="text-ink-faint">#{decision.pr_number}</span>
            <ExternalLinkIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint opacity-0 transition-opacity group-hover:opacity-100" />
          </a>
          <p className="truncate text-xs text-ink-faint">
            {actionLabel(decision.action)} ·{' '}
            <span className="font-mono">{shortSha(decision.commit_sha)}</span>
            {decision.created_at && (
              <span title={absTime(decision.created_at)}>
                {' '}
                · {relativeTime(decision.created_at)}
              </span>
            )}
          </p>
        </div>
        <RiskBadge risk={decision.risk} />
        <StatusBadge status={decision.status} size="sm" />
        {decision.result_url && (
          <a
            href={decision.result_url}
            target="_blank"
            rel="noreferrer"
            className="btn-ghost btn-sm"
          >
            View
            <ExternalLinkIcon className="h-3.5 w-3.5" />
          </a>
        )}
        <button onClick={onToggle} className="btn-ghost btn-sm" aria-expanded={expanded}>
          {expanded ? 'Hide' : 'Details'}
        </button>
      </div>
      {expanded && (
        <div className="border-t border-line px-4 py-3">
          {decision.title && <p className="text-sm font-medium text-ink">{decision.title}</p>}
          <p className="mt-1 text-xs text-ink-faint">
            reason: <span className="font-mono">{decision.reason || '—'}</span>
            {decision.last_error && (
              <span className="text-risk-high"> · error: {decision.last_error}</span>
            )}
          </p>
          {decision.body && (
            <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-canvas/60 p-3 text-xs text-ink-dim">
              {decision.body}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

export default function History() {
  const { stats } = useOutletContext<OutletContext>()
  const [status, setStatus] = useState<'all' | DecisionStatus>('all')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})
  const q = useDebounced(search, 300)

  const { data, error, loading, refreshing, updatedAt, reload } = usePolling(
    () =>
      api.reviews({
        limit: PAGE_SIZE,
        offset,
        q,
        status: status === 'all' ? undefined : status,
      }),
    8000,
    [offset, q, status],
  )

  useEffect(() => {
    setOffset(0)
  }, [q, status])

  const webUrl = stats?.config?.github_web_url ?? 'https://github.com'
  const rows = data?.reviews ?? []
  const total = data?.total ?? 0
  const counts: Record<string, number> = {
    all: stats?.reviews?.total ?? total,
    pending: stats?.reviews?.pending ?? 0,
    approved: stats?.reviews?.approved ?? 0,
    executed: stats?.reviews?.executed ?? 0,
    rejected: stats?.reviews?.rejected ?? 0,
    failed: stats?.reviews?.failed ?? 0,
  }

  const header = (
    <PageHeader
      title="Decision History"
      subtitle="Every disposition AutoPR reached — auto-posted, queued, approved or rejected"
      updatedAt={updatedAt}
      refreshing={refreshing}
      onRefresh={reload}
    >
      <SearchBox
        label="Search decisions"
        value={search}
        onChange={setSearch}
        placeholder="repo, sha, title…"
      />
    </PageHeader>
  )

  if (loading) {
    return (
      <>
        {header}
        <div className="mt-6">
          <SkeletonRows rows={4} className="h-20" />
        </div>
      </>
    )
  }

  if (error) {
    return (
      <>
        {header}
        <div className="mt-6 card">
          <EmptyState
            tone="error"
            icon={AlertIcon}
            title={error.offline ? 'Cannot reach the API' : 'Failed to load history'}
            description={error.message}
            action={
              <button onClick={reload} className="btn-primary btn-sm">
                Try again
              </button>
            }
          />
        </div>
      </>
    )
  }

  return (
    <>
      {header}

      <div className="mt-5 flex flex-wrap items-center gap-2">
        {STATUS_FILTERS.map((f) => {
          const n = counts[f] || 0
          if (f !== 'all' && n === 0) return null
          const active = status === f
          return (
            <button
              key={f}
              onClick={() => setStatus(f)}
              aria-pressed={active}
              className={`chip px-3 py-1.5 text-xs capitalize transition-colors ${
                active
                  ? 'border-brand-500/30 bg-brand-500/12 text-brand-300'
                  : 'border-line bg-white/[0.02] text-ink-dim hover:text-ink'
              }`}
            >
              {f === 'all' ? 'All' : f}
              <span className="tnum ml-1.5 text-ink-faint">{n}</span>
            </button>
          )
        })}
      </div>

      {rows.length === 0 ? (
        <div className="mt-4 card">
          <EmptyState
            icon={InboxIcon}
            title={search || status !== 'all' ? 'No matching decisions' : 'No decisions yet'}
            description={
              search || status !== 'all'
                ? 'Try a different search or status filter.'
                : 'Once the pipeline reviews a PR or diagnoses a CI failure, the disposition lands here.'
            }
          />
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-2">
          {rows.map((d) => (
            <DecisionRow
              key={d.id}
              decision={d}
              webUrl={webUrl}
              expanded={expanded[d.id] ?? false}
              onToggle={() => setExpanded((e) => ({ ...e, [d.id]: !e[d.id] }))}
            />
          ))}
          <Pager
            total={total}
            limit={PAGE_SIZE}
            offset={offset}
            onOffset={setOffset}
            unit="decisions"
          />
          <p className="mt-1 px-1 text-xs text-ink-faint">
            Showing {pluralize(rows.length, 'decision')}
            {status !== 'all' && ` · ${status}`}
            {search && ` · matching “${search}”`}.
          </p>
        </div>
      )}
    </>
  )
}
