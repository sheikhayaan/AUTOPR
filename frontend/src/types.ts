// Typed models for the AutoPR API surface. These mirror the backend
// serializers exactly (`app/main.py`: `_job_view`, `_decision_view`, `/stats`),
// so the dashboard speaks one vocabulary and a field rename on either side is a
// compile error rather than a silent `undefined` at runtime.

// --- Enums (mirror the backend enums) ---------------------------------------
export type JobStatus = 'pending' | 'queued' | 'processing' | 'done' | 'dead'
export type DecisionStatus = 'pending' | 'approved' | 'executed' | 'rejected' | 'failed'
export type RiskLevel = 'trivial' | 'low' | 'medium' | 'high'

// --- Resources ---------------------------------------------------------------
// A PR job: one webhook, processed exactly once (see `_job_view`).
export interface Job {
  id: number
  repo: string
  pr_number: number
  commit_sha: string
  author: string | null
  event: string
  status: JobStatus
  attempts: number
  last_error: string | null
  summary: string | null
  created_at: string | null
  updated_at: string | null
}

// A routing decision the pipeline produced (see `_decision_view`). `body` is the
// exact comment AutoPR would post — the point of the review queue.
export interface Decision {
  id: number
  repo: string
  pr_number: number
  commit_sha: string
  action: string
  risk: RiskLevel
  reason: string
  title: string | null
  body: string | null
  status: DecisionStatus
  result_url: string | null
  // Deep link to the PR's review screen. In hand-off mode this is the primary
  // CTA: the reviewer approves / edits under their own GitHub account.
  review_url: string | null
  last_error: string | null
  created_at: string | null
  updated_at: string | null
}

// --- /stats ------------------------------------------------------------------
export interface StatsConfig {
  github_dry_run: boolean
  live_github: boolean
  auto_comment_max_risk: string
  // Web origin for PR links, derived server-side from the API base so the UI
  // never hard-codes github.com (supports GitHub Enterprise too).
  github_web_url: string
  // Hand-off mode: AutoPR never writes to GitHub; every review is routed to a
  // human who acts via `review_url` under their own account.
  handoff_mode: boolean
}

export interface JobCounts {
  total: number
  pending: number
  queued: number
  processing: number
  done: number
  dead: number
}

export interface ReviewCounts {
  total: number
  pending: number
  approved: number
  executed: number
  rejected: number
  failed: number
}

export interface Stats {
  jobs: JobCounts
  reviews: ReviewCounts
  config: StatsConfig
}

// --- Envelope responses ------------------------------------------------------
export interface JobsResponse {
  count: number
  jobs: Job[]
}

export interface PendingResponse {
  count: number
  pending: Decision[]
}

export interface ReviewsResponse {
  count: number
  reviews: Decision[]
}

// approve/reject return a status verb plus the updated decision; approve also
// carries the posted comment `url` when it reached GitHub.
export interface ActionResponse {
  status: string
  url?: string | null
  detail?: string
  decision?: Decision
}

// --- Router outlet context (Layout -> pages via <Outlet context=…>) ----------
export interface OutletContext {
  stats: Stats | null
  statsRefreshing: boolean
  statsUpdatedAt: number | null
  reloadStats: () => void
  routeKey: string
}
