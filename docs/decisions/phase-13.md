# Phase 13 — Hand-off mode (tokenless "route to people, they act")

## Context
Operators want to run AutoPR against a real repo **without giving it a GitHub
write token**. The ask, verbatim: "just a URL that sends the approval request to
people's own GitHub, where if they approve they can do review and changes."

GitHub's security model forbids the literal version of that: nothing can post a
comment, submit a review, or push into someone's notifications without a
credential. You cannot act *as* a user without their auth. So the honest
implementation is a **hand-off**: AutoPR reads + reasons, then routes each
decision to a human who acts under their **own** account.

## Decision
Add `AUTOPR_HANDOFF_MODE` (default off). When on:

1. **Reads go anonymous.** `get_github_reader()` returns a real `HttpGitHubReader`
   with an empty token; `_headers()` omits `Authorization`, so public-repo diffs
   fetch with no credential. (A configured token still takes precedence — needed
   for private repos — and does *not* re-enable writes.)
2. **Writes are impossible.** `get_github_client()` returns the no-op
   `FakeGitHubClient` *unconditionally* — even if a token is set and dry-run is
   off — so no code path can mutate GitHub.
3. **Everything is human-gated.** `_route_pr` forces `auto = False`; no review is
   ever auto-posted, regardless of `auto_comment_max_risk`.
4. **The queue hands off.** Each decision carries a `review_url` deep link to the
   PR's review screen. Approve records `handed_off` (no GitHub call, marks the row
   EXECUTED with the review URL); the UI offers "Review & approve on GitHub ↗" and
   "Copy review".

The mechanism is cheap because the write boundary was already a single factory
(`get_github_client`) returning a no-op fake without a token, and the review body
is already generated and stored. Hand-off mode is three gate flips plus a link.

## Rejected alternatives
- **GitHub App / OAuth flow.** Correct for a multi-tenant product, but it *is* a
  write credential (an installation/user token) and a whole callback + token-store
  surface — the opposite of "no token," and over-engineered for a single
  operator. Kept as the documented upgrade path if AutoPR ever needs to notify
  reviewers inside GitHub.
- **Posting from a dedicated bot account.** Still a token; still AutoPR speaking,
  not the human. Defeats the "they act under their own account" requirement.

## Corners cut (honest limits)
- **Public repos only** in the tokenless path. Private repos need a read token
  (`AUTOPR_GITHUB_TOKEN`); writes stay disabled by hand-off mode.
- **Anonymous GitHub reads are rate-limited to 60/hour** per IP. Fine for a demo
  or low-volume operator; set a read token for 5000/hour without enabling writes.
- **No auto-notification.** AutoPR can't ping a reviewer inside GitHub without a
  credential; the hand-off is the dashboard link + copy button. Email/Slack
  surfacing is a future add.
- **`handed_off` reuses the EXECUTED status** rather than adding a new enum value,
  to avoid a schema migration; the audit log distinguishes it (`outcome=handed_off`).
