import { useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import PageHeader from '../components/PageHeader'
import StatusBadge from '../components/StatusBadge'
import EmptyState from '../components/EmptyState'
import { SkeletonRows } from '../components/Skeleton'
import { AlertIcon, PullRequestIcon, ExternalLinkIcon, ActivityIcon } from '../components/Icons'
import { shortSha, relativeTime, absTime, statusMeta, pluralize } from '../lib/format'
import type { Job, OutletContext } from '../types'

const FILTERS = ['all', 'processing', 'queued', 'pending', 'done', 'dead']

function JobRow({ job, webUrl }: { job: Job; webUrl: string }) {
  const prUrl = `${webUrl}/${job.repo}/pull/${job.pr_number}`
  const failed = job.status === 'dead'
  return (
    <div className="grid grid-cols-1 items-center gap-3 rounded-xl border border-line bg-surface/60 px-4 py-3.5 transition-colors hover:bg-surface-2/60 md:grid-cols-[minmax(0,1fr)_120px_84px_minmax(0,1.3fr)_96px]">
      {/* PR */}
      <div className="flex min-w-0 items-center gap-3">
        <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/[0.04] text-ink-faint ring-1 ring-line">
          <PullRequestIcon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="group inline-flex items-center gap-1.5 text-sm font-medium text-ink transition-colors hover:text-brand-300"
          >
            <span className="truncate">{job.repo}</span>
            <span className="text-ink-faint">#{job.pr_number}</span>
            <ExternalLinkIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint opacity-0 transition-opacity group-hover:opacity-100" />
          </a>
          <p className="truncate text-xs text-ink-faint">
            <span className="font-mono">{shortSha(job.commit_sha)}</span>
            {job.author && <span> · {job.author}</span>}
            <span> · {job.event}</span>
          </p>
        </div>
      </div>

      {/* Status */}
      <div className="flex items-center md:justify-start">
        <StatusBadge status={job.status} size="sm" />
      </div>

      {/* Attempts */}
      <div className="text-xs text-ink-dim md:text-center">
        <span className="text-ink-faint md:hidden">Attempts: </span>
        <span
          className={`tnum font-medium ${job.attempts > 1 ? 'text-risk-medium' : 'text-ink-dim'}`}
        >
          {job.attempts ?? 0}
        </span>
      </div>

      {/* Summary / error */}
      <div className="min-w-0 text-xs">
        {failed && job.last_error ? (
          <span className="line-clamp-2 text-risk-high">{job.last_error}</span>
        ) : job.summary ? (
          <span className="line-clamp-2 text-ink-dim">{job.summary}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        )}
      </div>

      {/* Updated */}
      <div
        className="text-xs text-ink-faint md:text-right"
        title={absTime(job.updated_at || job.created_at)}
      >
        {relativeTime(job.updated_at || job.created_at)}
      </div>
    </div>
  )
}

export default function Jobs() {
  const { stats } = useOutletContext<OutletContext>()
  const { data, error, loading, refreshing, updatedAt, reload } = usePolling(
    () => api.jobs(100),
    5000,
  )
  const [filter, setFilter] = useState('all')

  // Derive the PR host from the API base (exposed via /stats) so links work
  // against GitHub Enterprise too. Falls back to github.com before stats load.
  const webUrl = stats?.config?.github_web_url ?? 'https://github.com'

  const jobs = useMemo(() => data?.jobs ?? [], [data])
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: jobs.length }
    for (const j of jobs) c[j.status] = (c[j.status] || 0) + 1
    return c
  }, [jobs])

  const visible = filter === 'all' ? jobs : jobs.filter((j) => j.status === filter)

  const header = (
    <PageHeader
      title="Pipeline Jobs"
      subtitle="Every webhook, processed exactly once — with full retry history"
      updatedAt={updatedAt}
      refreshing={refreshing}
      onRefresh={reload}
    />
  )

  if (loading) {
    return (
      <>
        {header}
        <div className="mt-6">
          <SkeletonRows rows={6} className="h-16" />
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
            title={error.offline ? 'Cannot reach the API' : 'Failed to load jobs'}
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

      {/* Filter chips */}
      <div className="mt-5 flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => {
          const n = counts[f] || 0
          const active = filter === f
          const label = f === 'all' ? 'All' : statusMeta(f).label
          if (f !== 'all' && n === 0) return null
          return (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`chip px-3 py-1.5 text-xs transition-colors ${
                active
                  ? 'border-brand-500/30 bg-brand-500/12 text-brand-300'
                  : 'border-line bg-white/[0.02] text-ink-dim hover:text-ink'
              }`}
            >
              {label}
              <span className="tnum ml-1.5 text-ink-faint">{n}</span>
            </button>
          )
        })}
      </div>

      {/* Column header (desktop) */}
      {visible.length > 0 && (
        <div className="mt-4 hidden grid-cols-[minmax(0,1fr)_120px_84px_minmax(0,1.3fr)_96px] gap-3 px-4 pb-1 md:grid">
          <span className="eyebrow">Pull request</span>
          <span className="eyebrow">Status</span>
          <span className="eyebrow text-center">Attempts</span>
          <span className="eyebrow">Result</span>
          <span className="eyebrow text-right">Updated</span>
        </div>
      )}

      {visible.length === 0 ? (
        <div className="mt-4 card">
          <EmptyState
            icon={filter === 'all' ? PullRequestIcon : ActivityIcon}
            title={
              filter === 'all' ? 'No jobs yet' : `No ${statusMeta(filter).label.toLowerCase()} jobs`
            }
            description={
              filter === 'all'
                ? 'Open or update a pull request on a connected repo — or run the demo seed — and it will show up here.'
                : 'Try a different filter.'
            }
            action={
              filter !== 'all' ? (
                <button onClick={() => setFilter('all')} className="btn-ghost btn-sm">
                  Show all jobs
                </button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <div className="mt-2 flex flex-col gap-2">
          {visible.map((j) => (
            <JobRow key={j.id} job={j} webUrl={webUrl} />
          ))}
          <p className="mt-2 px-1 text-xs text-ink-faint">
            Showing {pluralize(visible.length, 'job')}
            {filter !== 'all' && ` · ${statusMeta(filter).label.toLowerCase()}`}.
          </p>
        </div>
      )}
    </>
  )
}
