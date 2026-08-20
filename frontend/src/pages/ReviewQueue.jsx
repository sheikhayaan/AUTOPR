import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling.js'
import { api } from '../api/client.js'
import { useToast } from '../hooks/useToast.jsx'
import PageHeader from '../components/PageHeader.jsx'
import RiskBadge from '../components/RiskBadge.jsx'
import EmptyState from '../components/EmptyState.jsx'
import CommentPreview from '../components/CommentPreview.jsx'
import Spinner from '../components/Spinner.jsx'
import { SkeletonRows } from '../components/Skeleton.jsx'
import {
  CheckIcon,
  XIcon,
  CheckCircleIcon,
  AlertIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  ShieldIcon,
  CommentIcon,
} from '../components/Icons.jsx'
import { shortSha, relativeTime, absTime, reasonLabel, actionLabel } from '../lib/format.js'

function DecisionCard({ decision, expanded, onToggle, busy, onApprove, onReject, dryRun }) {
  const prUrl = `https://github.com/${decision.repo}/pull/${decision.pr_number}`
  const acting = Boolean(busy)
  return (
    <div className="card overflow-hidden">
      {/* Head */}
      <div className="flex flex-col gap-3 p-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <RiskBadge risk={decision.risk} />
            <a
              href={prUrl}
              target="_blank"
              rel="noreferrer"
              className="group inline-flex items-center gap-1.5 text-sm font-medium text-ink transition-colors hover:text-brand-300"
            >
              <span className="truncate">{decision.repo}</span>
              <span className="text-ink-faint">#{decision.pr_number}</span>
              <ExternalLinkIcon className="h-3.5 w-3.5 text-ink-faint opacity-0 transition-opacity group-hover:opacity-100" />
            </a>
          </div>
          <h3 className="mt-2 text-[15px] font-semibold leading-snug text-ink">
            {decision.title || actionLabel(decision.action)}
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint">
            <span className="chip border-line bg-white/[0.04] px-2 py-0.5 text-ink-dim">
              {reasonLabel(decision.reason)}
            </span>
            <span>{actionLabel(decision.action)}</span>
            <span className="text-line">·</span>
            <span className="font-mono">{shortSha(decision.commit_sha)}</span>
            <span className="text-line">·</span>
            <span title={absTime(decision.created_at)}>queued {relativeTime(decision.created_at)}</span>
          </div>
        </div>
      </div>

      {/* Comment preview toggle */}
      <div className="border-t border-line px-5">
        <button
          onClick={onToggle}
          className="flex w-full items-center gap-2 py-3 text-sm font-medium text-ink-dim transition-colors hover:text-ink"
        >
          <CommentIcon className="h-4 w-4 text-ink-faint" />
          {expanded ? 'Hide' : 'Show'} the comment AutoPR would post
          <ChevronDownIcon
            className={`ml-auto h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`}
          />
        </button>
        {expanded && (
          <div className="pb-4">
            <CommentPreview body={decision.body} />
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex flex-col gap-3 border-t border-line bg-white/[0.015] px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="flex items-center gap-1.5 text-xs text-ink-faint">
          <ShieldIcon className="h-3.5 w-3.5" />
          {dryRun
            ? 'Approving records the action (dry-run — nothing is sent to GitHub).'
            : 'Approving posts this to GitHub. Rejecting takes no action, ever.'}
        </p>
        <div className="flex items-center gap-2.5">
          <button
            onClick={onReject}
            disabled={acting}
            className="btn-danger btn-sm disabled:opacity-50"
          >
            {busy === 'reject' ? <Spinner /> : <XIcon className="h-4 w-4" />}
            Reject
          </button>
          <button
            onClick={onApprove}
            disabled={acting}
            className="btn-primary btn-sm disabled:opacity-50"
          >
            {busy === 'approve' ? <Spinner /> : <CheckIcon className="h-4 w-4" />}
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}

export default function ReviewQueue() {
  const { stats, reloadStats } = useOutletContext()
  const toast = useToast()
  const { data, error, loading, refreshing, updatedAt, reload } = usePolling(api.pending, 5000)

  const [expanded, setExpanded] = useState({}) // id -> bool
  const [busy, setBusy] = useState({}) // id -> 'approve' | 'reject'
  const [acted, setActed] = useState(() => new Set()) // optimistic removal

  const dryRun = stats?.config?.github_dry_run !== false

  const toggle = (id) => setExpanded((e) => ({ ...e, [id]: !e[id] }))

  async function act(decision, kind) {
    const id = decision.id
    setBusy((b) => ({ ...b, [id]: kind }))
    try {
      if (kind === 'approve') {
        const res = await api.approve(id)
        const posted = res?.url
        toast.success(
          dryRun ? 'Approved — recorded (dry-run)' : 'Approved — posted to GitHub',
          { title: `${decision.repo} #${decision.pr_number}`, ...(posted ? {} : {}) },
        )
      } else {
        await api.reject(id)
        toast.info('Review rejected — no action taken', {
          title: `${decision.repo} #${decision.pr_number}`,
        })
      }
      // Optimistically drop it from the queue; the next poll confirms.
      setActed((s) => new Set(s).add(id))
      reload()
      reloadStats?.()
    } catch (e) {
      toast.error(e?.message || 'Action failed — please retry', {
        title: `${decision.repo} #${decision.pr_number}`,
      })
    } finally {
      setBusy((b) => {
        const next = { ...b }
        delete next[id]
        return next
      })
    }
  }

  const all = data?.pending ?? []
  const items = all.filter((d) => !acted.has(d.id))

  const header = (
    <PageHeader
      title="Review Queue"
      subtitle="Decisions the pipeline is holding for human approval"
      updatedAt={updatedAt}
      refreshing={refreshing}
      onRefresh={reload}
    >
      {items.length > 0 && (
        <span className="chip border-brand-500/25 bg-brand-500/10 px-2.5 py-1 text-xs font-semibold text-brand-400">
          {items.length} pending
        </span>
      )}
    </PageHeader>
  )

  if (loading) {
    return (
      <>
        {header}
        <div className="mt-6">
          <SkeletonRows rows={3} className="h-44" />
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
            title={error.offline ? 'Cannot reach the API' : 'Failed to load the queue'}
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
      {items.length === 0 ? (
        <div className="mt-6 card">
          <EmptyState
            icon={CheckCircleIcon}
            title="Queue is clear"
            description="No decisions are waiting on a human. Low-risk reviews are auto-handled; anything riskier lands here for approval."
          />
        </div>
      ) : (
        <div className="mt-6 flex flex-col gap-4">
          {items.map((d) => (
            <DecisionCard
              key={d.id}
              decision={d}
              dryRun={dryRun}
              expanded={expanded[d.id] ?? true}
              onToggle={() => toggle(d.id)}
              busy={busy[d.id]}
              onApprove={() => act(d, 'approve')}
              onReject={() => act(d, 'reject')}
            />
          ))}
        </div>
      )}
    </>
  )
}
