# Phase 10 — Decisions Log

Making the **frontend portfolio-grade**. The pipeline was already secure
(Phase 7), durable (Phase 8), and observable (Phase 9); the dashboard that
watches it was the last thing still written in loose JavaScript — no types, no
tests, no error boundary, and two hardcoded `github.com` URLs that would break
against any GitHub Enterprise host. This phase migrates the whole SPA to
TypeScript, gives it a real test suite, and closes those gaps. Nothing about the
backend changes; this is entirely about making the operator surface as
defensible as the system behind it.

The through-line: **the UI speaks one typed vocabulary with the API, and a
field rename on either side is a compile error rather than a silent `undefined`
at runtime.** Everything else — the generic polling hook, the error boundary,
the derived PR host, the test surface — follows from taking that contract
seriously. The scale discipline of the earlier phases holds here too: a real
test suite and strict types, but jsdom integration rather than a browser grid,
and a proportionate number of tests rather than coverage theatre.

---

## 1. **Full TypeScript migration**, with response models that mirror the backend serializers

**Chosen.** Every `.jsx`/`.js` module became `.tsx`/`.ts` (21 modules), and
`src/types.ts` declares the API surface — `Job`, `Decision`, `Stats`,
`StatsConfig`, the envelope responses — to match `app/main.py`'s `_job_view`,
`_decision_view`, and `/stats` exactly. `src/api/client.ts` is generic
(`req<T>`) so each endpoint returns its typed shape, and the pages consume those
types through `useOutletContext<OutletContext>()`. `tsconfig.json` runs
`strict` plus `noUnusedLocals`, `noUnusedParameters`, and
`noFallthroughCasesInSwitch`.

**Why.** The dashboard and the API are one system split across a language
boundary; the only thing keeping them in agreement was, until now, careful
reading. Typing the response models turns that agreement into something the
compiler enforces: rename `pr_number` to `pull_number` in the serializer and
forget the client, and `tsc` fails the build instead of the field quietly
arriving as `undefined` in a template string. That is the entire value
proposition of TypeScript for a thin client over a JSON API — not the syntax,
but the *contract*. Strict mode is the point of the migration, not a nicety: a
non-strict TS migration would type the obvious and leave the nullable fields
(`author`, `summary`, `last_error`, every `*_at`) exactly as unsafe as the
JavaScript it replaced.

**Rejected — incremental `// @ts-check` on the existing JS.** Gets the editor
hints without the guarantees; nullable fields stay untyped and the contract is
still advisory. **Rejected — generate types from an OpenAPI schema.** FastAPI
does emit one, and at a larger surface that codegen is the right call — but for
~10 hand-writable models it adds a build step and a generator dependency to
maintain, and the hand-written `types.ts` doubles as readable documentation of
what the UI actually consumes. Flagged as the scale-out path.

---

## 2. `tsc` **type-checks, Vite builds** — bundler mode, `noEmit`, extensionless imports

**Chosen.** `tsconfig.json` sets `moduleResolution: "bundler"`,
`allowImportingTsExtensions`, `isolatedModules`, and `noEmit`. `tsc -b` never
produces JavaScript; it only reports type errors. Vite (esbuild/Rollup) owns the
actual transform and bundle. `npm run build` is `tsc -b && vite build` — the
type gate runs first and fails the build on any error before a bundle is ever
produced.

**Why.** Two tools that each try to emit is a recipe for drift — different
module output, different target lowering, two configs to keep in sync. Letting
Vite own emit and `tsc` own checking gives each tool the job it is best at:
esbuild transforms far faster than `tsc` emits, and `tsc` type-checks far more
thoroughly than esbuild (which does not type-check at all). `bundler` resolution
is what lets the source use extensionless imports (`./components/Layout`)
matching how Vite actually resolves them, instead of the `.js`-suffixed imports
that `moduleResolution: "node16"` would demand in `.ts` source. Sequencing the
type-check *before* the bundle in the `build` script means a type error is a
hard build failure in CI, not a warning that scrolls past.

**Rejected — `tsc` emits, no bundler.** No dev server, no HMR, no asset
pipeline; a non-starter for a React SPA. **Rejected — Babel/esbuild only, skip
`tsc`.** Fast, but esbuild strips types without checking them, so the entire
guarantee from Decision 1 evaporates — the types become comments.

---

## 3. Tests **import from `vitest` explicitly**; `globals: true` exists only for RTL auto-cleanup

**Chosen.** Every test file does `import { describe, it, expect, vi } from
'vitest'` rather than relying on ambient globals, and `tsconfig.json`
deliberately does **not** list `vitest/globals` in `types`. Yet the Vitest
config *does* set `globals: true`. The split is intentional: `globals: true` is
present because `@testing-library/react` registers its automatic
`afterEach(cleanup)` only when it detects a global test API, and
`restoreMocks: true` rides the same switch. The explicit imports are what the
source and the type-checker see.

**Why.** Ambient test globals are a real cost: `describe`/`it`/`expect` leak
into the type space of *every* file, so a typo like `expat(...)` in production
code might resolve to a global instead of erroring, and the reader loses the
"where does this come from" that an import gives. Importing them explicitly
keeps the test API scoped and greppable. But RTL's per-test DOM cleanup is
load-bearing — without it, one test's rendered tree bleeds into the next and
`getByRole` starts matching stale nodes — and RTL wires that cleanup off the
`globals` flag. So the flag is on for the runtime behaviour it unlocks, while
the code keeps the discipline of explicit imports. Having both is not
contradictory; they serve different layers (runtime harness vs. type surface).

**Rejected — `globals: true` + `vitest/globals` in tsconfig types, no
imports.** The conventional setup, and it works — but it pollutes the type
space of the whole project for the convenience of not typing one import line.
**Rejected — `globals: false`, manual `afterEach(cleanup)` in every file.**
Then forgetting the cleanup in one file is a subtle cross-test contamination bug
that surfaces as a flaky, order-dependent failure — exactly the kind of thing a
test suite is supposed to *not* have.

---

## 4. PR links **derive their host from `/stats`**, threaded through the outlet context

**Chosen.** The backend `/stats` config exposes `github_web_url` (derived
server-side from `github_api_url`: `api.github.com` → `github.com`, and an
Enterprise `…/api/v3` host strips to its web origin). `Layout` puts `stats` on
the router outlet context; `Jobs` and `ReviewQueue` read
`stats?.config?.github_web_url` and build `${webUrl}/${repo}/pull/${pr}`, with
`?? 'https://github.com'` as the pre-load fallback. The two hardcoded
`github.com` template literals are gone.

**Why.** A hardcoded `github.com` is a latent correctness bug the moment anyone
points AutoPR at a GitHub Enterprise instance — every PR link in the dashboard
would 404, silently, against the wrong host. The host is not the frontend's fact
to know; it is a property of which API the backend is configured to talk to, so
the backend is the correct place to derive it and the frontend's job is only to
consume it. Threading it through the existing outlet context (rather than a
second fetch or a prop drilled through every level) reuses the one stats poll
the layout already runs. The literal fallback keeps links sensible in the
sub-second window before the first `/stats` response lands, without a loading
flicker on every link.

**Rejected — keep `github.com` hardcoded.** Wrong against Enterprise, and
invisible until someone hits it. **Rejected — a `VITE_GITHUB_URL` build-time
env var.** Bakes the host into the bundle, so one build can't serve two
backends, and it can *disagree* with the backend's actual `github_api_url` —
deriving it server-side means there is exactly one source of truth.

---

## 5. A top-level **error boundary** turns a render crash into a recoverable screen

**Chosen.** `ErrorBoundary` (the one class component in the tree — boundaries
must be classes) wraps the entire app in `main.tsx`, above the router.
`getDerivedStateFromError` renders a royal fallback card (what happened, the
error message, a "Reload dashboard" button); `componentDidCatch` logs to the
console where a real deployment would report to Sentry. The comment states the
invariant plainly: this is display-only safety — no pipeline state is touched.

**Why.** Without a boundary, one unhandled exception in any component unmounts
the whole React tree to a blank white page — the least informative failure mode
a control plane can have, because the operator can't tell "the dashboard has a
bug" from "the backend is down" from "my network died." A boundary converts that
into an honest, recoverable screen that says *the dashboard failed to render,
your data is safe, reload*. Placing it above the router means a crash in any
route is caught, not just within a page. It is deliberately display-only: a
dashboard rendering fault must never be able to imply anything about the
pipeline's state, which lives entirely server-side behind the dry-run and
human-gating boundaries the earlier phases established.

**Rejected — no boundary (default React behaviour).** The blank-page failure
mode above. **Rejected — a fine-grained boundary per route/panel.** More
granular recovery, but for a dashboard of this size one top-level catch is the
proportionate weight; per-panel isolation is worth it when panels are
independently valuable enough that one crashing shouldn't blank the others —
noted, not needed here.

---

## 6. A **proportionate test surface**: unit the logic, mock the client at its boundary, one integration test

**Chosen.** 35 tests across five files: pure-logic units for
`lib/format` (labels, relative time with faked clock) and the `api/client`
(offline detection, HTTP-error shape, bearer-header logic — with `fetch`
stubbed); a behavioural test of the generic `usePolling` hook (loads once,
polls on interval, **pauses while the tab is hidden**, captures errors without
throwing — driven by fake timers and a faked `visibilityState`); the
`ErrorBoundary` (children render vs. fallback-on-throw); and one integration
test of `ReviewQueue` that mocks `../api/client` at the module boundary and
drives a real approve click through to the dry-run toast and the
`github_web_url`-derived link. CI runs `typecheck`, `lint`, `format`-check,
`test`, and `build`.

**Why.** The value of a test is proportional to how likely the thing it covers
is to break and how costly the break is. The `format` and `client` helpers are
pure, branchy, and used everywhere — cheap to test, high-value. `usePolling`'s
pause-on-hidden is the one piece of genuinely subtle behaviour in the frontend
(a bug there means the dashboard hammers the API from backgrounded tabs), so it
gets a focused behavioural test with the clock and visibility faked. `ReviewQueue`
is the only page that *mutates* (approve/reject), so it earns the one full
integration test — mocking the client at its module boundary keeps the test
about the component's behaviour, not the network. That is the whole surface that
matters; testing the read-only pages' markup would be asserting that JSX renders
JSX.

**Rejected — exhaustive per-page render tests.** High maintenance, low signal:
they mostly re-assert the markup and break on every cosmetic change without
catching a real defect. **Rejected — a frontend coverage gate (like the
backend's 80%).** A coverage number would pressure exactly the low-value markup
tests above to make the percentage; targeting the branchy logic and the one
mutating flow by hand is the more honest bar at this size. Flagged as a corner.

---

## Corners cut (flagged, deferred)

1. **jsdom integration, no end-to-end.** The `ReviewQueue` test renders in jsdom
   with a mocked client; there is no Playwright/Cypress run against a real
   browser and a live backend. jsdom can't catch a real CSS layout regression or
   a genuine cross-origin/proxy issue. For a single-operator dashboard the
   component-plus-mock level is the proportionate bar; a browser E2E grid is the
   scale-out path, deferred.

2. **No frontend coverage gate.** Unlike the backend's `--cov-fail-under=80`,
   the frontend CI job asserts green tests but not a coverage floor (Decision 6).
   The risk is that new code can land untested without a number complaining; the
   mitigation is that the high-value logic is covered by hand today. Flagged
   rather than papered over with a metric.

3. **Bearer token in `localStorage`.** `client.ts` reads an operator-pasted
   token from `localStorage['autopr_api_token']`, which is readable by any script
   that achieves XSS. The alternative (an httpOnly cookie) can't be read by the
   SPA that needs to send the header, and for a single-operator control plane the
   `localStorage` convenience is the accepted trade; the `VITE_AUTOPR_API_TOKEN`
   build-time path exists for deployments that prefer not to persist it at all.
   Noted as a known surface.

4. **`ApiError.cause` declared by hand.** The project targets ES2020, whose
   `Error` type predates the `cause` field, so `ApiError` declares `cause?:
   unknown` itself rather than bumping the whole `lib` to ES2022 and pulling in
   globals the app doesn't otherwise use. The runtime assignment is valid on
   every browser the app supports; this is purely a type-surface decision, called
   out so the manual declaration isn't mistaken for an oversight.

5. **SPA is not mounted by the backend.** The built `dist/` is served
   independently (any static host / the Vite preview); FastAPI does not mount it
   via `StaticFiles`. A single-container "one process serves API + UI" demo is a
   documented option but is off by default, because coupling the static serve to
   the API process is a deployment choice that belongs to the cloud phase, not a
   property the app should assume.

6. **React Router v7 future-flag warnings are left as-is.** The test run surfaces
   `v7_startTransition` / `v7_relativeSplatPath` deprecation notices. They are
   informational for a future major and change no behaviour today; opting into the
   flags (or upgrading) is a deliberate dependency bump, not something to fold
   silently into this phase.
