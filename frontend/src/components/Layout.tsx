import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { api } from '../api/client'
import { ActivityIcon, DashboardIcon, InboxIcon, LogoMark, ShieldIcon, BoltIcon } from './Icons'
import type { IconComponent } from './Icons'
import type { OutletContext, StatsConfig } from '../types'

interface NavItemDef {
  to: string
  label: string
  icon: IconComponent
  end?: boolean
  badge?: 'pending'
}

const NAV: NavItemDef[] = [
  { to: '/', label: 'Overview', icon: DashboardIcon, end: true },
  { to: '/reviews', label: 'Review Queue', icon: InboxIcon, badge: 'pending' },
  { to: '/jobs', label: 'Pipeline Jobs', icon: ActivityIcon },
]

function NavItem({ item, pending }: { item: NavItemDef; pending: number }) {
  const badge = item.badge === 'pending' ? pending : 0
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        `group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors ${
          isActive
            ? 'bg-white/[0.06] text-ink'
            : 'text-ink-dim hover:bg-white/[0.03] hover:text-ink'
        }`
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full bg-gradient-to-b from-gold-300 to-gold-500" />
          )}
          <item.icon
            className={`h-[18px] w-[18px] ${isActive ? 'text-gold-400' : 'text-ink-faint group-hover:text-ink-dim'}`}
          />
          <span className="flex-1">{item.label}</span>
          {badge > 0 && (
            <span className="tnum inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-brand-500 px-1.5 text-[11px] font-semibold text-white">
              {badge}
            </span>
          )}
        </>
      )}
    </NavLink>
  )
}

function ModeChip({ config }: { config: StatsConfig | undefined }) {
  if (!config) return null
  const live = config.live_github && config.github_dry_run === false
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-line bg-surface-2 px-3 py-2.5">
      {live ? (
        <BoltIcon className="h-4 w-4 shrink-0 text-risk-medium" />
      ) : (
        <ShieldIcon className="h-4 w-4 shrink-0 text-risk-low" />
      )}
      <div className="min-w-0 leading-tight">
        <p className="text-xs font-semibold text-ink">{live ? 'Live mode' : 'Dry-run mode'}</p>
        <p className="truncate text-[11px] text-ink-faint">
          {live ? 'Posting to GitHub' : 'Actions recorded, not sent'}
        </p>
      </div>
    </div>
  )
}

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <LogoMark className="h-10 w-10" />
      <div className="leading-tight">
        <p className="font-display text-[19px] font-semibold tracking-tight text-ink">AutoPR</p>
        <p className="text-[10px] font-semibold uppercase tracking-[0.24em] text-gold-500/80">
          Control Plane
        </p>
      </div>
    </div>
  )
}

export default function Layout() {
  const location = useLocation()
  const { data: stats, refreshing, updatedAt, reload } = usePolling(api.stats, 5000)
  const pending = stats?.reviews?.pending ?? 0
  const config = stats?.config

  const context: OutletContext = {
    stats,
    statsRefreshing: refreshing,
    statsUpdatedAt: updatedAt,
    reloadStats: reload,
    routeKey: location.pathname,
  }

  return (
    <div className="min-h-full bg-canvas">
      {/* Ambient background: faint gilded grid + a royal aurora (amethyst
          bloom top-left, a whisper of gold top-right). */}
      <div className="pointer-events-none fixed inset-0 bg-grid-faint [background-size:46px_46px]" />
      <div className="pointer-events-none fixed inset-0 bg-royal-aurora" />

      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-line bg-surface/70 px-4 py-5 backdrop-blur-xl md:flex">
        <Brand />
        <nav className="mt-8 flex flex-1 flex-col gap-1">
          {NAV.map((item) => (
            <NavItem key={item.to} item={item} pending={pending} />
          ))}
        </nav>
        <ModeChip config={config} />
      </aside>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-surface/80 px-4 py-3 backdrop-blur-xl md:hidden">
        <Brand />
        <nav className="ml-auto flex items-center gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `relative rounded-lg p-2 ${isActive ? 'bg-white/[0.07] text-gold-400' : 'text-ink-faint'}`
              }
              aria-label={item.label}
            >
              <item.icon className="h-5 w-5" />
              {item.badge === 'pending' && pending > 0 && (
                <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-brand-500 ring-2 ring-surface" />
              )}
            </NavLink>
          ))}
        </nav>
      </header>

      {/* Main content */}
      <main className="relative md:pl-64">
        <div className="mx-auto max-w-6xl px-5 py-7 sm:px-8">
          <Outlet context={context} />
        </div>
      </main>
    </div>
  )
}
