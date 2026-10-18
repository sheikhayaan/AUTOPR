import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import History from './History'
import type { Decision, OutletContext } from '../types'

const { reviewsMock } = vi.hoisted(() => ({ reviewsMock: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { reviews: reviewsMock, stats: vi.fn(), jobs: vi.fn(), pending: vi.fn(), health: vi.fn() },
}))

function decision(over: Partial<Decision> = {}): Decision {
  return {
    id: 5,
    repo: 'octo/repo',
    pr_number: 11,
    commit_sha: 'deadbeefcafe',
    action: 'propose_fix',
    risk: 'low',
    reason: 'verified_fix_needs_promotion',
    title: 'Verified fix ready for maintainer approval',
    body: '```diff\n--- a/app.py\n+++ b/app.py\n```',
    status: 'executed',
    result_url: 'https://github.com/octo/repo/pull/11#issuecomment-1',
    review_url: null,
    last_error: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function page(rows: Decision[], total = rows.length, offset = 0) {
  return { count: rows.length, total, limit: 20, offset, reviews: rows }
}

const ctx: OutletContext = {
  stats: {
    jobs: { total: 0, pending: 0, queued: 0, processing: 0, done: 0, dead: 0 },
    reviews: { total: 45, pending: 3, approved: 0, executed: 40, rejected: 2, failed: 0 },
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
  routeKey: '/history',
}

function renderHistory(context: OutletContext = ctx) {
  return render(
    <MemoryRouter initialEntries={['/history']}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/history" element={<History />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('History', () => {
  it('lists decisions with action, risk and status', async () => {
    reviewsMock.mockResolvedValue(page([decision()]))
    renderHistory()

    expect(await screen.findByText('octo/repo')).toBeInTheDocument()
    expect(screen.getByText(/Propose fix/)).toBeInTheDocument()
    expect(screen.getByText('Low')).toBeInTheDocument()
    expect(screen.getByText('Executed')).toBeInTheDocument()
    // The posted comment is one click away.
    expect(screen.getByRole('link', { name: /view/i })).toHaveAttribute(
      'href',
      'https://github.com/octo/repo/pull/11#issuecomment-1',
    )
  })

  it('expands a row to show the exact body that was posted', async () => {
    reviewsMock.mockResolvedValue(page([decision()]))
    renderHistory()
    await screen.findByText('octo/repo')

    // Collapsed by default: the (potentially long) comment body stays closed.
    expect(document.querySelector('pre')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /details/i }))
    expect(document.querySelector('pre')?.textContent).toContain('--- a/app.py')
    expect(screen.getByText(/verified_fix_needs_promotion/)).toBeInTheDocument()
  })

  it('pages through history with the server-reported total', async () => {
    reviewsMock.mockResolvedValue(page([decision()], 45, 0))
    renderHistory()
    await screen.findByText('octo/repo')

    expect(reviewsMock).toHaveBeenCalledWith({ limit: 20, offset: 0, q: '', status: undefined })
    expect(screen.getByText(/1–20 of 45 decisions/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /next/i }))
    await waitFor(() =>
      expect(reviewsMock).toHaveBeenLastCalledWith({
        limit: 20,
        offset: 20,
        q: '',
        status: undefined,
      }),
    )
  })

  it('filters by status and searches server-side', async () => {
    reviewsMock.mockResolvedValue(page([decision()]))
    renderHistory()
    await screen.findByText('octo/repo')

    await userEvent.click(screen.getByRole('button', { name: /rejected/i }))
    await waitFor(() =>
      expect(reviewsMock).toHaveBeenLastCalledWith({
        limit: 20,
        offset: 0,
        q: '',
        status: 'rejected',
      }),
    )

    await userEvent.type(screen.getByLabelText('Search decisions'), 'octo')
    await waitFor(
      () =>
        expect(reviewsMock).toHaveBeenLastCalledWith({
          limit: 20,
          offset: 0,
          q: 'octo',
          status: 'rejected',
        }),
      { timeout: 2000 },
    )
  })

  it('explains an empty history instead of showing a blank page', async () => {
    reviewsMock.mockResolvedValue(page([]))
    renderHistory()
    expect(await screen.findByText('No decisions yet')).toBeInTheDocument()
  })

  it('reports a failed error (e.g. 401 from a missing token) with a retry', async () => {
    reviewsMock.mockRejectedValue(
      Object.assign(new Error('Unauthorized'), { status: 401, data: { detail: 'Unauthorized' } }),
    )
    renderHistory()
    expect(await screen.findByText('Failed to load history')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})
