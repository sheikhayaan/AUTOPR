// A KPI tile: tinted icon medallion, big tabular number, label, and an optional
// footnote. `accent` picks the medallion color.
const ACCENTS = {
  brand: 'text-brand-400 bg-brand-500/10 ring-brand-500/20',
  green: 'text-risk-low bg-risk-low/10 ring-risk-low/20',
  amber: 'text-risk-medium bg-risk-medium/10 ring-risk-medium/20',
  rose: 'text-risk-high bg-risk-high/10 ring-risk-high/20',
  slate: 'text-ink-dim bg-white/[0.06] ring-line',
}

export default function StatCard({ icon: Icon, label, value, footnote, accent = 'brand', highlight = false }) {
  return (
    <div
      className={`card relative overflow-hidden p-5 transition-colors ${
        highlight ? 'ring-1 ring-brand-500/30' : ''
      }`}
    >
      {highlight && (
        <div className="pointer-events-none absolute inset-0 bg-brand-sheen opacity-70" />
      )}
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">{label}</p>
          <p className="tnum mt-2 text-3xl font-semibold text-ink">{value}</p>
          {footnote && <p className="mt-1.5 text-xs text-ink-faint">{footnote}</p>}
        </div>
        {Icon && (
          <div className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ring-1 ${ACCENTS[accent]}`}>
            <Icon className="h-5 w-5" />
          </div>
        )}
      </div>
    </div>
  )
}
