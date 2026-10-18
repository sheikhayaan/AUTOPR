import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import Jobs from './Jobs'
import type { Job, OutletContext } from '../types'

const { jobsMock } = vi.hoisted(() => ({ jobsMock: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { jobs: jobsMock, stats: vi.fn(), pending: vi.fn(), reviews: vi.fn(), health: vi.fn() },
}))

function job(over: Partial<Job> = {}): Job {
  return {
    id: 1,
    repo: 'octo/repo',
    pr_number: 7,
    commit_sha: 'abcdef1234567',
    author: 'octocat',
    event: 'pull_request',
    status: 'done',
    attempts: 1,
    last_error: null,
    summary: 'review pr octo/repo#7: risk=low files=2 action=executed',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function page(jobs: Job[], total = jobs.length, offset = 0) {
  return { count: jobs.length, total, limit: 25, offset, jobs }
}

const ctx: OutletContext = {
  stats: {
    jobs: { total: 60, pending: 0, queued: 3, processing: 1, done: 55, dead: 1 },
    reviews: { total: 4, pending: 1, approved: 0, executed: 3, rejected: 0, failed: 0 },
    config: {
      github_dry_run: true,
      live_github: false,
      auto_comment_max_risk: 'low',
      github_web_url: 'https://github.com',
      handoff_mode: false,
    },
  },
  statsRefreshing: false,
  statsUpdatedAt: null,
  reloadStats: vi.fn(),
  routeKey: '/jobs',
}

function renderJobs(context: OutletContext = ctx) {
  return render(
    <MemoryRouter initialEntries={['/jobs']}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/jobs" element={<Jobs />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('Jobs', () => {
  it('renders rows from the API and links to the PR', async () => {
    jobsMock.mockResolvedValue(page([job()]))
    renderJobs()

    expect(await screen.findByText('octo/repo')).toBeInTheDocument()
    expect(screen.getByText('#7')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /octo\/repo/ })
    expect(link).toHaveAttribute('href', 'https://github.com/octo/repo/pull/7')
    // The ledger summary is the operator's only view into what the run did.
    expect(screen.getByText(/action=executed/)).toBeInTheDocument()
  })

  it('asks the API for the first page and pages forward', async () => {
    jobsMock.mockResolvedValue(page([job()], 60, 0))
    renderJobs()
    await screen.findByText('octo/repo')

    // First call: no filter, no search, offset 0.
    expect(jobsMock).toHaveBeenCalledWith({ limit: 25, offset: 0, q: '', status: undefined })

    await userEvent.click(screen.getByRole('button', { name: /next/i }))
    await waitFor(() =>
      expect(jobsMock).toHaveBeenLastCalledWith({
        limit: 25,
        offset: 25,
        q: '',
        status: undefined,
      }),
    )
    expect(await screen.findByText(/26–50 of 60 jobs/)).toBeInTheDocument()
  })

  it('filters by status server-side using the /stats totals for chip counts', async () => {
    jobsMock.mockResolvedValue(page([job({ status: 'dead', last_error: 'boom' })]))
    renderJobs()
    await screen.findByText('octo/repo')

    // Chip counts come from stats (whole table), not just this page.
    const dead = screen.getByRole('button', { name: /dead/i })
    expect(dead).toHaveTextContent('1')

    await userEvent.click(dead)
    await waitFor(() =>
      expect(jobsMock).toHaveBeenLastCalledWith({
        limit: 25,
        offset: 0,
        q: '',
        status: 'dead',
      }),
    )
    expect(dead).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('boom')).toBeInTheDocument()
  })

  it('debounces the search box into a q= request and resets paging', async () => {
    jobsMock.mockResolvedValue(page([job()], 60, 25))
    renderJobs()
    await screen.findByText('octo/repo')

    await userEvent.type(screen.getByLabelText('Search jobs'), 'deadbeef')
    await waitFor(
      () =>
        expect(jobsMock).toHaveBeenLastCalledWith({
          limit: 25,
          offset: 0, // paging resets when the query changes
          q: 'deadbeef',
          status: undefined,
        }),
      { timeout: 2000 },
    )
  })

  it('shows an empty state with a clear-filters action when a search misses', async () => {
    jobsMock.mockResolvedValue(page([]))
    renderJobs()
    await screen.findByText('No jobs yet')

    await userEvent.type(screen.getByLabelText('Search jobs'), 'zzz')
    expect(await screen.findByText('No matching jobs')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /clear filters/i }))
    expect(await screen.findByText('No jobs yet')).toBeInTheDocument()
  })

  it('surfaces an error state with retry when the API is unreachable', async () => {
    jobsMock.mockRejectedValue(
      Object.assign(new Error('Cannot reach the AutoPR API. Is the server running?'), {
        offline: true,
      }),
    )
    renderJobs()
    expect(await screen.findByText('Cannot reach the API')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
