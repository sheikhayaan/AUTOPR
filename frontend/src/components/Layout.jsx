import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling.js'
import { api } from '../api/client.js'
import {
  ActivityIcon,
  DashboardIcon,
  InboxIcon,
  LogoMark,
  ShieldIcon,
  BoltIcon,
} from './Icons.jsx'

const NAV = [
  { to: '/', label: 'Overview', icon: DashboardIcon, end: true },
  { to: '/reviews', label: 'Review Queue', icon: InboxIcon, badge: 'pending' },
  { to: '/jobs', label: 'Pipeline Jobs', icon: ActivityIcon },
]

function NavItem({ item, pending }) {
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
            <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full bg-brand-500" />
          )}
          <item.icon
            className={`h-[18px] w-[18px] ${isActive ? 'text-brand-400' : 'text-ink-faint group-hover:text-ink-dim'}`}
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

function ModeChip({ config }) {
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
    <div className="flex items-center gap-2.5">
      <LogoMark className="h-9 w-9" />
      <div className="leading-tight">
        <p className="text-[15px] font-semibold tracking-tight text-ink">AutoPR</p>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-ink-faint">
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

  return (
    <div className="min-h-full bg-canvas">
      {/* Ambient background: faint grid + brand sheen top-left. */}
      <div className="pointer-events-none fixed inset-0 bg-grid-faint [background-size:44px_44px]" />
      <div className="pointer-events-none fixed inset-0 bg-brand-sheen" />

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
                `relative rounded-lg p-2 ${isActive ? 'bg-white/[0.07] text-brand-400' : 'text-ink-faint'}`
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
          <Outlet
            context={{
              stats,
              statsRefreshing: refreshing,
              statsUpdatedAt: updatedAt,
              reloadStats: reload,
              routeKey: location.pathname,
            }}
          />
        </div>
      </main>
    </div>
  )
}
