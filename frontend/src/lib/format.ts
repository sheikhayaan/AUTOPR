// Presentation helpers + the semantic vocabulary (risk levels, statuses) that
// mirror the backend enums. Keeping the mapping here means the whole UI speaks
// one language for color and labels.

// Tones are the shared color vocabulary; badges, dots and bars all key off them.
export type Tone = 'slate' | 'indigo' | 'amber' | 'green' | 'rose'

export interface RiskMeta {
  label: string
  dot: string
  text: string
  ring: string
  soft: string
}

export interface StatusMeta {
  label: string
  tone: Tone
  pulse?: boolean
}

export function shortSha(sha: string | null | undefined): string {
  return (sha || '').slice(0, 7)
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const secs = Math.round((Date.now() - then) / 1000)
  if (secs < 5) return 'just now'
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

export function absTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

// Risk ladder — matches policy.RISK_ORDER (trivial < low < medium < high).
export const RISK_META: Record<string, RiskMeta> = {
  trivial: {
    label: 'Trivial',
    dot: 'bg-risk-trivial',
    text: 'text-risk-trivial',
    ring: 'ring-risk-trivial/30',
    soft: 'bg-risk-trivial/10',
  },
  low: {
    label: 'Low',
    dot: 'bg-risk-low',
    text: 'text-risk-low',
    ring: 'ring-risk-low/30',
    soft: 'bg-risk-low/10',
  },
  medium: {
    label: 'Medium',
    dot: 'bg-risk-medium',
    text: 'text-risk-medium',
    ring: 'ring-risk-medium/30',
    soft: 'bg-risk-medium/10',
  },
  high: {
    label: 'High',
    dot: 'bg-risk-high',
    text: 'text-risk-high',
    ring: 'ring-risk-high/30',
    soft: 'bg-risk-high/10',
  },
}
export function riskMeta(risk: string | null | undefined): RiskMeta {
  return (
    (risk && RISK_META[risk]) || {
      label: risk || 'Unknown',
      dot: 'bg-ink-faint',
      text: 'text-ink-dim',
      ring: 'ring-line',
      soft: 'bg-white/5',
    }
  )
}

// Job + decision statuses -> tone. Tones map to StatusBadge classes.
export const STATUS_META: Record<string, StatusMeta> = {
  // job statuses
  pending: { label: 'Pending', tone: 'slate' },
  queued: { label: 'Queued', tone: 'indigo' },
  processing: { label: 'Processing', tone: 'amber', pulse: true },
  done: { label: 'Done', tone: 'green' },
  dead: { label: 'Dead-letter', tone: 'rose' },
  // decision statuses
  approved: { label: 'Approved', tone: 'indigo' },
  executed: { label: 'Executed', tone: 'green' },
  rejected: { label: 'Rejected', tone: 'slate' },
  failed: { label: 'Failed', tone: 'rose' },
}
export function statusMeta(status: string | null | undefined): StatusMeta {
  return (status && STATUS_META[status]) || { label: status || 'Unknown', tone: 'slate' }
}

// Tone -> solid fill, for distribution bars and legend dots. Shared so the
// bars, badges, and dots stay in lockstep.
export const TONE_BG: Record<Tone, string> = {
  slate: 'bg-slate-400/70',
  indigo: 'bg-brand-500',
  amber: 'bg-risk-medium',
  green: 'bg-risk-low',
  rose: 'bg-risk-high',
}
export function toneBg(tone: string): string {
  return TONE_BG[tone as Tone] || 'bg-ink-faint'
}

// Human phrasing for policy `reason` codes.
export const REASON_LABELS: Record<string, string> = {
  low_risk_auto: 'Low-risk · auto-posted',
  elevated_risk_needs_approval: 'Elevated risk · needs approval',
  verified_fix_needs_promotion: 'Verified fix · awaiting promotion',
  no_diagnosis: 'No actionable diagnosis',
  unfixed: 'Diagnosed · not auto-fixed',
}
export function reasonLabel(reason: string | null | undefined): string {
  return (reason && REASON_LABELS[reason]) || (reason ? reason.replace(/_/g, ' ') : '—')
}

export const ACTION_LABELS: Record<string, string> = {
  comment_review: 'Comment review',
  propose_fix: 'Propose fix',
  escalate: 'Escalate',
  none: 'No action',
}
export function actionLabel(action: string | null | undefined): string {
  return (action && ACTION_LABELS[action]) || action || '—'
}

export function pluralize(n: number, one: string, many?: string): string {
  return `${n} ${n === 1 ? one : (many ?? `${one}s`)}`
}
