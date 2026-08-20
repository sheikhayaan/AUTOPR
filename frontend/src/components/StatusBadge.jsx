import { statusMeta } from '../lib/format.js'

const TONES = {
  slate: 'bg-white/[0.06] text-ink-dim border-line',
  indigo: 'bg-brand-500/12 text-brand-400 border-brand-500/25',
  amber: 'bg-risk-medium/12 text-risk-medium border-risk-medium/25',
  green: 'bg-risk-low/12 text-risk-low border-risk-low/25',
  rose: 'bg-risk-high/12 text-risk-high border-risk-high/25',
}
const DOTS = {
  slate: 'bg-ink-faint',
  indigo: 'bg-brand-400',
  amber: 'bg-risk-medium',
  green: 'bg-risk-low',
  rose: 'bg-risk-high',
}

// A status pill for job + decision lifecycle states. `processing` pulses.
export default function StatusBadge({ status, size = 'md' }) {
  const m = statusMeta(status)
  const tone = TONES[m.tone] || TONES.slate
  const pad = size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs'
  return (
    <span className={`chip ${pad} ${tone}`} title={`Status: ${m.label}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${DOTS[m.tone]} ${m.pulse ? 'animate-pulse-ring' : ''}`} />
      {m.label}
    </span>
  )
}
