import type { ReactNode } from 'react'
import type { IconComponent } from './Icons'

interface EmptyStateProps {
  icon?: IconComponent
  title: string
  description?: string
  action?: ReactNode
  tone?: 'default' | 'error'
}

// Centered empty / error state with an icon medallion.
export default function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  tone = 'default',
}: EmptyStateProps) {
  const ring =
    tone === 'error'
      ? 'text-risk-high ring-risk-high/20 bg-risk-high/10'
      : 'text-brand-400 ring-brand-500/20 bg-brand-500/10'
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      {Icon && (
        <div className={`mb-1 grid h-14 w-14 place-items-center rounded-2xl ring-1 ${ring}`}>
          <Icon className="h-7 w-7" />
        </div>
      )}
      <h3 className="text-base font-semibold text-ink">{title}</h3>
      {description && <p className="max-w-sm text-sm text-ink-dim">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
