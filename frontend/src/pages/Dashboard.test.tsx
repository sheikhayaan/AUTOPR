import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import Dashboard from './Dashboard'
import type { Job, OutletContext, Stats } from '../types'

const { jobsMock } = vi.hoisted(() => ({ jobsMock: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { jobs: jobsMock, stats: vi.fn(), pending: vi.fn(), reviews: vi.fn(), health: vi.fn() },
}))

const STATS: Stats = {
  jobs: { total: 40, pending: 0, queued: 3, processing: 2, done: 34, dead: 1 },
  reviews: { total: 12, pending: 2, approved: 1, executed: 8, rejected: 1, failed: 0 },
  config: {
    github_dry_run: false,
    live_github: true,
    auto_comment_max_risk: 'low',
    github_web_url: 'https://github.com',
    handoff_mode: false,
  },
}

function ctx(stats: Stats | null = STATS): OutletContext {
  return {
    stats,
    statsRefreshing: false,
    statsUpdatedAt: Date.now(),
    reloadStats: vi.fn(),
    routeKey: '/',
  }
}

function renderDashboard(context: OutletContext = ctx()) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/" element={<Dashboard />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

const job: Job = {
  id: 1,
  repo: 'octo/repo',
  pr_number: 7,
  commit_sha: 'abcdef1',
  author: 'octocat',
  event: 'pull_request',
  status: 'done',
  attempts: 1,
  last_error: null,
  summary: 'review pr octo/repo#7: risk=low files=2 action=executed',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('Dashboard', () => {
  it('renders the four KPIs from /stats', async () => {
    jobsMock.mockResolvedValue({ count: 1, total: 1, limit: 6, offset: 0, jobs: [job] })
    renderDashboard()

    await screen.findByText('Awaiting approval')
    // Scope each assertion to its KPI tile: the same numbers also appear in the
    // distribution legends below.
    // Labels can also appear in the distribution legends below, so take the
    // first occurrence — the KPI row is rendered before them.
    const kpi = (label: string) =>
      within(screen.getAllByText(label)[0].closest('.card') as HTMLElement)
    expect(kpi('Awaiting approval').getByText('2')).toBeInTheDocument() // reviews.pending
    expect(kpi('In flight').getByText('5')).toBeInTheDocument() // queued + processing
    expect(kpi('Reviews executed').getByText('8')).toBeInTheDocument()
    expect(kpi('Dead-letter').getByText('1')).toBeInTheDocument()
    expect(screen.getByText(/2 processing · 3 queued/)).toBeInTheDocument()
  })

  it('warns loudly in live mode and stays calm in dry-run', async () => {
    jobsMock.mockResolvedValue({ count: 0, total: 0, limit: 6, offset: 0, jobs: [] })
    renderDashboard()
    expect(await screen.findByText('Live GitHub mode')).toBeInTheDocument()
    expect(screen.getByText(/Approved actions are posted to GitHub/)).toBeInTheDocument()
  })

  it('says dry-run when writes are disabled', async () => {
    jobsMock.mockResolvedValue({ count: 0, total: 0, limit: 6, offset: 0, jobs: [] })
    renderDashboard(
      ctx({ ...STATS, config: { ...STATS.config, github_dry_run: true, live_github: false } }),
    )
    expect(await screen.findByText('Dry-run mode')).toBeInTheDocument()
    expect(screen.getByText(/nothing is sent to GitHub/)).toBeInTheDocument()
  })

  it('shows recent activity from /jobs and an empty state when there is none', async () => {
    jobsMock.mockResolvedValueOnce({ count: 1, total: 1, limit: 6, offset: 0, jobs: [job] })
    renderDashboard()
    expect(await screen.findByText(/action=executed/)).toBeInTheDocument()

    jobsMock.mockResolvedValue({ count: 0, total: 0, limit: 6, offset: 0, jobs: [] })
    renderDashboard()
    expect(await screen.findByText('No jobs yet')).toBeInTheDocument()
  })

  it('renders skeletons (not zeroes) before /stats arrives', () => {
    renderDashboard(ctx(null))
    expect(screen.getByText('Overview')).toBeInTheDocument()
    // No KPI labels yet — showing "0" for unknown data would be a lie.
    expect(screen.queryByText('Awaiting approval')).not.toBeInTheDocument()
  })
})
