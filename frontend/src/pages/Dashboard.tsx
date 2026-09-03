import { Link, useOutletContext } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import PageHeader from '../components/PageHeader'
import StatCard from '../components/StatCard'
import StatusBadge from '../components/StatusBadge'
import EmptyState from '../components/EmptyState'
import { SkeletonRows } from '../components/Skeleton'
import {
  InboxIcon,
  ActivityIcon,
  CheckCircleIcon,
  AlertIcon,
  ShieldIcon,
  BoltIcon,
  PullRequestIcon,
  ChevronDownIcon,
} from '../components/Icons'
import { statusMeta, toneBg, shortSha, relativeTime, pluralize, type Tone } from '../lib/format'
import type { Job, OutletContext, StatsConfig } from '../types'

interface Segment {
  key: string
  label: string
  value: number
  tone: Tone
}

// A stacked proportional bar with a legend — reads the whole pipeline at a glance.
function Distribution({
  title,
  hint,
  segments,
  total,
}: {
  title: string
  hint?: string
  segments: Segment[]
  total: number
}) {
  const live = segments.filter((s) => s.value > 0)
  return (
    <div className="card p-5">
      <div className="flex items-baseline justify-between">
        <h3 className="font-display text-[15px] font-semibold text-ink">{title}</h3>
        <span className="tnum text-xs text-ink-faint">{pluralize(total, 'total')}</span>
      </div>
      {hint && <p className="mt-1 text-xs text-ink-faint">{hint}</p>}

      <div className="mt-4 flex h-2.5 w-full overflow-hidden rounded-full bg-white/[0.05]">
        {total === 0 ? (
          <div className="h-full w-full bg-white/[0.04]" />
        ) : (
          live.map((s) => (
            <div
              key={s.key}
              className={`h-full ${toneBg(s.tone)} transition-[width] duration-500`}
              style={{ width: `${(s.value / total) * 100}%` }}
              title={`${s.label}: ${s.value}`}
            />
          ))
        )}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-3">
        {segments.map((s) => (
          <div key={s.key} className="flex items-center gap-2">
            <span className={`h-2 w-2 shrink-0 rounded-full ${toneBg(s.tone)}`} />
            <span className="text-xs text-ink-dim">{s.label}</span>
            <span className="tnum ml-auto text-xs font-semibold text-ink">{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ModeBanner({ config }: { config: StatsConfig }) {
  const live = config.live_github && config.github_dry_run === false
  const Icon = live ? BoltIcon : ShieldIcon
  return (
    <div
      className={`card relative overflow-hidden p-5 ${live ? 'ring-1 ring-risk-medium/25' : ''}`}
    >
      <div className="flex items-start gap-4">
        <div
          className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl ring-1 ${
            live
              ? 'bg-risk-medium/10 text-risk-medium ring-risk-medium/25'
              : 'bg-risk-low/10 text-risk-low ring-risk-low/25'
          }`}
        >
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-display text-[15px] font-semibold text-ink">
              {live ? 'Live GitHub mode' : 'Dry-run mode'}
            </h3>
            <span
              className={`chip px-2 py-0.5 text-[11px] ${live ? 'bg-risk-medium/12 text-risk-medium border-risk-medium/25' : 'bg-risk-low/12 text-risk-low border-risk-low/25'}`}
            >
              {live ? 'writes enabled' : 'safe'}
            </span>
          </div>
          <p className="mt-1 text-sm text-ink-dim">
            {live
              ? 'Approved actions are posted to GitHub. Elevated-risk reviews still require explicit human approval.'
              : 'The pipeline reviews PRs and records exactly what it would post — nothing is sent to GitHub. Every action is auditable before you flip the switch.'}
          </p>
          <p className="mt-2 text-xs text-ink-faint">
            Auto-comment ceiling:{' '}
            <span className="font-medium text-ink-dim">{config.auto_comment_max_risk}</span>
            <span className="mx-2 text-line">·</span>
            GitHub token:{' '}
            <span className="font-medium text-ink-dim">
              {config.live_github ? 'present' : 'not set'}
            </span>
          </p>
        </div>
      </div>
    </div>
  )
}

function ActivityRow({ job }: { job: Job }) {
  return (
    <Link
      to="/jobs"
      className="flex items-center gap-3 rounded-xl border border-transparent px-3 py-2.5 transition-colors hover:border-line hover:bg-white/[0.02]"
    >
      <PullRequestIcon className="h-4 w-4 shrink-0 text-ink-faint" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-ink">
          <span className="font-medium">{job.repo}</span>
          <span className="text-ink-faint"> #{job.pr_number}</span>
        </p>
        <p className="truncate text-xs text-ink-faint">
          {job.summary || `${job.event} · ${shortSha(job.commit_sha)}`}
        </p>
      </div>
      <div className="hidden shrink-0 sm:block">
        <StatusBadge status={job.status} size="sm" />
      </div>
      <span className="tnum w-14 shrink-0 text-right text-xs text-ink-faint">
        {relativeTime(job.created_at)}
      </span>
    </Link>
  )
}

export default function Dashboard() {
  const { stats, statsRefreshing, statsUpdatedAt, reloadStats } = useOutletContext<OutletContext>()
  const recent = usePolling(() => api.jobs(6), 5000)

  if (!stats) {
    return (
      <>
        <PageHeader title="Overview" subtitle="Pipeline health at a glance" />
        <div className="mt-6">
          <SkeletonRows rows={2} className="h-24" />
          <div className="mt-4">
            <SkeletonRows rows={4} />
          </div>
        </div>
      </>
    )
  }

  const { jobs, reviews, config } = stats
  const jobSegments: Segment[] = [
    {
      key: 'processing',
      label: 'Processing',
      value: jobs.processing,
      tone: statusMeta('processing').tone,
    },
    { key: 'queued', label: 'Queued', value: jobs.queued, tone: statusMeta('queued').tone },
    { key: 'pending', label: 'Pending', value: jobs.pending, tone: statusMeta('pending').tone },
    { key: 'done', label: 'Done', value: jobs.done, tone: statusMeta('done').tone },
    { key: 'dead', label: 'Dead-letter', value: jobs.dead, tone: statusMeta('dead').tone },
  ]
  const reviewSegments: Segment[] = [
    { key: 'pending', label: 'Pending', value: reviews.pending, tone: statusMeta('pending').tone },
    {
      key: 'executed',
      label: 'Executed',
      value: reviews.executed,
      tone: statusMeta('executed').tone,
    },
    {
      key: 'approved',
      label: 'Approved',
      value: reviews.approved,
      tone: statusMeta('approved').tone,
    },
    {
      key: 'rejected',
      label: 'Rejected',
      value: reviews.rejected,
      tone: statusMeta('rejected').tone,
    },
    { key: 'failed', label: 'Failed', value: reviews.failed, tone: statusMeta('failed').tone },
  ]

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Pipeline health at a glance"
        updatedAt={statsUpdatedAt}
        refreshing={statsRefreshing}
        onRefresh={reloadStats}
      />

      <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          icon={InboxIcon}
          label="Awaiting approval"
          value={reviews.pending}
          footnote={reviews.pending > 0 ? 'Needs a human decision' : 'Queue is clear'}
          accent="brand"
          highlight={reviews.pending > 0}
        />
        <StatCard
          icon={ActivityIcon}
          label="In flight"
          value={jobs.processing + jobs.queued}
          footnote={`${jobs.processing} processing · ${jobs.queued} queued`}
          accent="amber"
        />
        <StatCard
          icon={CheckCircleIcon}
          label="Reviews executed"
          value={reviews.executed}
          footnote="Posted to GitHub"
          accent="green"
        />
        <StatCard
          icon={AlertIcon}
          label="Dead-letter"
          value={jobs.dead}
          footnote={jobs.dead > 0 ? 'Exhausted retries' : 'None — healthy'}
          accent={jobs.dead > 0 ? 'rose' : 'slate'}
        />
      </div>

      <div className="mt-4">
        <ModeBanner config={config} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Distribution
          title="Job pipeline"
          hint="Every webhook becomes a job, processed exactly once."
          segments={jobSegments}
          total={jobs.total}
        />
        <Distribution
          title="Review outcomes"
          hint="What the router decided and where each decision landed."
          segments={reviewSegments}
          total={reviews.total}
        />
      </div>

      <div className="mt-4 card p-5">
        <div className="flex items-center justify-between">
          <h3 className="font-display text-[15px] font-semibold text-ink">Recent activity</h3>
          <Link to="/jobs" className="btn-ghost btn-sm">
            View all
            <ChevronDownIcon className="h-4 w-4 -rotate-90" />
          </Link>
        </div>
        <div className="mt-3">
          {recent.loading ? (
            <SkeletonRows rows={4} className="h-12" />
          ) : recent.data?.jobs?.length ? (
            <div className="flex flex-col gap-0.5">
              {recent.data.jobs.map((j) => (
                <ActivityRow key={j.id} job={j} />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={PullRequestIcon}
              title="No jobs yet"
              description="Open or update a pull request on a connected repo — or run the demo seed — and it will appear here."
            />
          )}
        </div>
      </div>
    </>
  )
}
