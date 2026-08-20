import { riskMeta } from '../lib/format.js'

// A risk pill: colored dot + label, tinted to the risk level.
export default function RiskBadge({ risk, size = 'md' }) {
  const m = riskMeta(risk)
  const pad = size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs'
  return (
    <span
      className={`chip ${pad} border-transparent ${m.soft} ${m.text} ring-1 ${m.ring}`}
      title={`Risk: ${m.label}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  )
}
