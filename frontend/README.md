# AutoPR — Control Plane (frontend)

The operator dashboard for AutoPR: a dark, single-page control plane for the
multi-agent PR review pipeline. It renders pipeline health, the job history, and
— the point of the whole thing — the **human-in-the-loop review queue** where a
maintainer approves or rejects what the pipeline wants to post to GitHub.

Built with **React 18 + Vite 5 + Tailwind CSS 3**. No component library — the
design system (cards, badges, buttons, icons) is hand-rolled in `src/components`
so the visual language stays consistent and the bundle stays lean.

---

## Quick start

Prerequisites: **Node 18+** and the AutoPR API running (see the repo root README
for the backend).

```bash
cd frontend
npm install
npm run dev
```

Then open <http://localhost:5173>.

The dev server proxies every `/api/*` request to the FastAPI backend on
`http://localhost:8000` and strips the `/api` prefix (see `vite.config.js`), so
the browser stays same-origin — no CORS preflight in development. Point the proxy
elsewhere with `AUTOPR_API_URL`:

```bash
AUTOPR_API_URL=http://127.0.0.1:9000 npm run dev
```

### See it with data

The dashboard is most convincing with data in it. From the **repo root** (so it
shares the same `./autopr.db` the API uses):

```bash
./.venv/Scripts/python.exe scripts/seed_demo.py   # Windows
# ./.venv/bin/python scripts/seed_demo.py          # macOS/Linux
```

That seeds jobs in every lifecycle state and a review queue with a medium- and a
high-risk decision pending, so every page lights up. It is safe to re-run — it
only touches demo rows (`acme/*`) and never real webhook data.

---

## Production build

```bash
npm run build     # emits static assets to dist/
npm run preview   # serve the built bundle locally
```

The build is a fully static SPA. For a real deployment, serve `dist/` from any
static host and either (a) reverse-proxy `/api` to the backend, or (b) build with
an absolute API origin:

```bash
VITE_API_BASE=https://autopr.example.com npm run build
```

`VITE_API_BASE` defaults to `/api` (the dev/proxy convention) when unset.

---

## How it's organized

```
src/
  api/client.js       One typed wrapper around the API. All fetch lives here.
  lib/format.js       The shared vocabulary: risk/status → label + color,
                      relative time, sha shortening. The UI speaks one language.
  hooks/
    usePolling.js     Poll-on-interval with tab-visibility pause + a manual
                      reload(); drives every live view.
    useToast.jsx      Toast provider + useToast() for action feedback.
  components/         The design system — Layout (sidebar shell), StatCard,
                      StatusBadge, RiskBadge, CommentPreview, EmptyState, …
  pages/
    Dashboard.jsx     KPIs, dry-run/live banner, pipeline & review
                      distributions, recent activity.
    ReviewQueue.jsx   The star: pending decisions with the exact comment
                      preview and Approve / Reject (optimistic + toasts).
    Jobs.jsx          Filterable job table with status, attempts, and results.
```

### A few decisions worth calling out

- **One API module, one polling hook.** Every page renders from `usePolling`
  over a function in `api/client.js`. There's no client state library because
  the server is the source of truth and a 5s poll is plenty for an ops console —
  simpler, and it can't drift out of sync.
- **Optimistic approve/reject.** Acting on a decision removes it from the queue
  immediately and shows a toast; the next poll confirms. A failed call surfaces
  an error toast and the row returns on the next poll.
- **The comment preview is safe by construction.** `CommentPreview` renders the
  bot's markdown by building React nodes (never `dangerouslySetInnerHTML`), so
  LLM-authored finding text can't inject markup — React escapes all of it.
- **Dark by design.** This is a developer control plane; the palette is a single
  committed dark theme rather than a theme toggle.
