import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { ToastProvider } from '../hooks/useToast'
import ReviewQueue from './ReviewQueue'
import type { Decision, OutletContext } from '../types'

// Mock the API client. vi.hoisted lets the mock factory (hoisted to the top of
// the module) reference these spies safely.
const { pendingMock, approveMock, rejectMock } = vi.hoisted(() => ({
  pendingMock: vi.fn(),
  approveMock: vi.fn(),
  rejectMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    pending: pendingMock,
    approve: approveMock,
    reject: rejectMock,
    stats: vi.fn(),
    jobs: vi.fn(),
    reviews: vi.fn(),
    health: vi.fn(),
  },
}))

const decision: Decision = {
  id: 42,
  repo: 'octo/repo',
  pr_number: 7,
  commit_sha: 'abcdef1234567',
  action: 'comment_review',
  risk: 'medium',
  reason: 'needs_human',
  title: 'Add retry logic to the worker',
  body: 'Looks good overall. One nit: consider a jittered backoff.',
  status: 'pending',
  result_url: null,
  review_url: 'https://github.com/octo/repo/pull/7/files',
  last_error: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const ctx: OutletContext = {
  stats: {
    jobs: { total: 0, pending: 0, queued: 0, processing: 0, done: 0, dead: 0 },
    reviews: { total: 0, pending: 0, approved: 0, executed: 0, rejected: 0, failed: 0 },
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
  routeKey: '/reviews',
}

function renderQueue(context: OutletContext = ctx) {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/reviews']}>
        <Routes>
          <Route element={<Outlet context={context} />}>
            <Route path="/reviews" element={<ReviewQueue />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </ToastProvider>,
  )
}

// A context with hand-off mode on, reusing the base stats/counts.
const handoffCtx: OutletContext = {
  ...ctx,
  stats: {
    ...ctx.stats!,
    config: { ...ctx.stats!.config, handoff_mode: true },
  },
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('ReviewQueue', () => {
  it('shows the empty state when nothing is pending', async () => {
    pendingMock.mockResolvedValue({ count: 0, pending: [] })
    renderQueue()
    expect(await screen.findByText('Queue is clear')).toBeInTheDocument()
  })

  it('renders a pending decision with a host-derived PR link', async () => {
    pendingMock.mockResolvedValue({ count: 1, pending: [decision] })
    renderQueue()

    const link = await screen.findByRole('link', { name: /octo\/repo/ })
    expect(link).toHaveAttribute('href', 'https://github.com/octo/repo/pull/7')
    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument()
  })

  it('approves a decision and confirms with a dry-run toast', async () => {
    const user = userEvent.setup()
    pendingMock.mockResolvedValue({ count: 1, pending: [decision] })
    approveMock.mockResolvedValue({ status: 'approved' })
    renderQueue()

    await user.click(await screen.findByRole('button', { name: /approve/i }))

    expect(approveMock).toHaveBeenCalledWith(42)
    expect(await screen.findByText('Approved — recorded (dry-run)')).toBeInTheDocument()
  })

  it('surfaces the GitHub hand-off link (not Approve) in hand-off mode', async () => {
    pendingMock.mockResolvedValue({ count: 1, pending: [decision] })
    renderQueue(handoffCtx)

    // The primary CTA is the deep link to the PR's review screen, using the
    // backend-supplied review_url — the human acts under their own account.
    const handoff = await screen.findByRole('link', { name: /review & approve on github/i })
    expect(handoff).toHaveAttribute('href', 'https://github.com/octo/repo/pull/7/files')
    // No "Approve" button in hand-off mode; it's "Mark handled" instead.
    expect(screen.queryByRole('button', { name: /^approve$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /mark handled/i })).toBeInTheDocument()
  })
})
