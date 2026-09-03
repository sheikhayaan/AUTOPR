import type { ReactNode } from 'react'
import type { IconComponent } from './Icons'

// A KPI tile: tinted icon medallion, big gilded serif numeral, label, and an
// optional footnote. `accent` picks the medallion color; the numeral itself is
// gold across the row for a cohesive, ceremonial read.
type Accent = 'brand' | 'gold' | 'green' | 'amber' | 'rose' | 'slate'

const ACCENTS: Record<Accent, string> = {
  brand: 'text-brand-400 bg-brand-500/10 ring-brand-500/20',
  gold: 'text-gold-400 bg-gold-500/10 ring-gold-500/25',
  green: 'text-risk-low bg-risk-low/10 ring-risk-low/20',
  amber: 'text-risk-medium bg-risk-medium/10 ring-risk-medium/20',
  rose: 'text-risk-high bg-risk-high/10 ring-risk-high/20',
  slate: 'text-ink-dim bg-white/[0.06] ring-line',
}

interface StatCardProps {
  icon?: IconComponent
  label: string
  value: number | string
  footnote?: ReactNode
  accent?: Accent
  highlight?: boolean
}

export default function StatCard({
  icon: Icon,
  label,
  value,
  footnote,
  accent = 'brand',
  highlight = false,
}: StatCardProps) {
  return (
    <div
      className={`card group relative overflow-hidden p-5 transition-all duration-200 hover:-translate-y-0.5 ${
        highlight ? 'ring-1 ring-gold-500/30' : ''
      }`}
    >
      {highlight && (
        <div className="pointer-events-none absolute inset-0 bg-brand-sheen opacity-70" />
      )}
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">{label}</p>
          <p className="tnum mt-2.5 font-display text-[34px] font-semibold leading-none text-gold">
            {value}
          </p>
          {footnote && <p className="mt-2 text-xs text-ink-faint">{footnote}</p>}
        </div>
        {Icon && (
          <div
            className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl ring-1 ${ACCENTS[accent]}`}
          >
            <Icon className="h-5 w-5" />
          </div>
        )}
      </div>
    </div>
  )
}
