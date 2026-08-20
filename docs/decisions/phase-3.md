# Phase 3 — Decisions Log

Sandboxed fix verification: the Fix Agent now *proves* its patch before anyone
trusts it. A proposed diff is applied to a repo snapshot inside a locked-down
Docker container, the check that matches the diagnosed failure is re-run, and a
failed proof feeds back into a bounded re-attempt. Each entry states what was
chosen, the rejected alternative, and why.

---

## 1. Verification is a separate node, not folded into the Fix Agent

**Chosen.** A distinct `fix_verifier` node runs after `fix_agent`. The agent
*proposes* (`proposed_fix`); the verifier *proves* (`fix_verified`,
`verification_reason`, `verification_output`). They are different
responsibilities with different trust levels and different dependencies (one
needs an LLM, the other needs Docker).

**Why.** Separation keeps each node single-purpose and independently testable:
the agent's parsing logic is covered by mocked-LLM tests, the verifier's verdict
logic by a `FakeSandbox`, with no overlap. It also puts the verify/retry cycle
in the *graph* where routing is declarative and unit-testable, rather than
burying a loop inside the agent. And it mirrors reality — proposing a fix and
proving it are genuinely separate acts.

**Rejected — the Fix Agent applies and checks its own patch.** Conflates "can
write a diff" with "can run Docker," makes the node untestable without a daemon,
and hides the retry loop inside imperative agent code instead of the graph.

---

## 2. Real Docker isolation, hardened by run flags (not a tempdir + subprocess)

**Chosen.** `DockerSandbox` runs the check inside a container with:
`--network none`, `--read-only` root + RAM-backed `--tmpfs /work`, `--memory` /
`--memory-swap` (equal ⇒ no swap) / `--cpus` / `--pids-limit` caps,
`--cap-drop ALL`, `--security-opt no-new-privileges`, the context mounted
read-only, and a non-root image user. A host-side `timeout` plus the caps mean a
hostile or runaway patch is bounded on every axis: no egress, no host writes, no
fork bomb, no infinite hang.

**Why.** The patch is *untrusted code we are about to execute*. A tempdir +
subprocess shares the host kernel, network, filesystem, and user — a malicious
`conftest.py` or `setup.py` would run with the worker's privileges the moment
pytest imports it. Since Docker is now installed on the machine, real container
isolation is the honest choice, and the flags turn a container (which is not a
security boundary by default) into a usefully locked-down one.

**Rejected — tempdir + `subprocess` on the host.** Was the fallback plan when no
daemon was available. Simple, but it runs arbitrary code from an untrusted patch
directly on the host with no network/fs/resource boundary. Unacceptable once a
real option exists.

## 3. Patch application happens *inside* the container via `git apply`

**Chosen.** The container entrypoint copies the read-only snapshot into the
tmpfs, `git init`s a throwaway repo, `git apply`s the patch, and only then
`exec`s the verification command. A rejected patch exits with a dedicated
sentinel code (`PATCH_FAILED_EXIT = 3`) that the policy maps to a distinct
`patch_apply_failed` verdict.

**Why.** Applying inside the container keeps the host completely out of the patch
path — the host never runs `git apply` on untrusted input, and the whole
apply+check happens in one disposable place. The distinct sentinel matters for
routing quality: "the patch didn't even apply" (malformed diff, wrong offsets)
is a different failure than "the patch applied but the check still fails," and
the Fix Agent gets told which one so its retry can respond correctly.

**Rejected — apply the diff on the host in Python, mount the result in.** Pulls
untrusted-diff handling back onto the host and needs a robust unified-diff
applier in Python (or host `git`, which may not exist). `git apply` inside the
image is both safer and simpler.

---

## 4. Per-failure-type verification policy, with honest "not verifiable"

**Chosen.** `policy.verification_command` maps each `failure_type` to the check
that would have caught it: `lint → ruff check` (scoped to changed .py files),
`type_error → mypy` (scoped), `test → pytest` (whole suite), `import → pytest
--collect-only`. `dependency` and `unknown` return `None` — no offline
verification exists — and the verifier reports `not_verifiable` and escalates.

**Why.** Verifying with the *same class of check* that failed is what makes a
green result meaningful. Scoping lint/type checks to changed files avoids failing
a good fix on unrelated pre-existing debt; running the *whole* test suite is
deliberate the other way — a patch that greens the target test but reddens
another has not fixed the build. Returning `None` for `dependency` is the honest
answer: the fix needs `pip install`, which needs network, which the sandbox
forbids by design — so we say "human, please," rather than fake a pass.

**Rejected — always run the full `pytest` suite regardless of failure type.**
Slower, and it mis-attributes: a lint fix "verified" by pytest tells you nothing
about the lint error. Match the check to the failure.

---

## 5. Bounded verify → retry-with-feedback loop in the graph

**Chosen.** `fix_verifier` routes via `_after_fix_verifier`: verified ⇒ END;
a *retryable* failure (`check_failed`, `patch_apply_failed`, `timeout`) with
`fix_attempts < max_fix_attempts` ⇒ back to `fix_agent`, which is now
retry-aware and receives the captured sandbox output as "your previous patch
failed, here's why — don't repeat it"; anything else ⇒ END (escalate). The
attempt counter is the loop bound.

**Why.** A single-shot fix wastes the most useful signal available — the exact
error the attempt produced. Feeding it back lets the model correct course, which
is how a human debugs. But an LLM loop must be *bounded*: LangGraph permits
cycles, so the termination guarantee has to be ours. `max_fix_attempts` caps LLM
spend and guarantees the run ends. Non-retryable reasons (`sandbox_error`,
`not_verifiable`, `no_snapshot`, `no_fix`) skip the loop entirely — retrying
wouldn't change them.

**Rejected — retry until verified.** An unfixable failure would loop forever,
burning Groq quota and never escalating. **Rejected — no retry, one shot.**
Throws away the verification output that makes the second attempt likely to
succeed.

<!-- CONTINUE_P3 -->

---

## 6. The sandbox never touches the real repo — verdict only

**Chosen.** The verifier's only authority is to write a verdict to the state. It
does not apply the fix to the real repo, push a branch, or comment on the PR.
Promotion of a verified fix is a later, human-gated action (Phase 4).

**Why.** Verification and promotion are different trust decisions. A green
sandbox says "this patch resolves the diagnosed failure" — it does *not* say
"ship it unreviewed." Keeping the verifier side-effect-free means a bug here can
at worst mislabel a verdict, never mutate a real repository.

**Rejected — auto-commit/push on a green verdict.** Removes the human from a
code-changing action on the strength of one automated check. Wrong default for a
system whose credibility depends on not making bad changes autonomously.

---

## Corners cut / simplifying assumptions — be ready to explain these

Deliberate and known. None affect the correctness the tests prove; they are
scope boundaries for Phase 3.

1. **The repo snapshot has no live producer yet.** The verifier applies the patch
   to `state["repo_snapshot"]` — a list of `(path, content)`. Nothing yet fetches
   the real repo tree from GitHub at the changed commit to populate it, and
   `build_graph` is not yet invoked from the worker (Phase 1's `default_handler`
   is still the worker body). Tests inject a snapshot directly. Wiring the worker
   to build the graph and fetch the tree is the Phase 3→4 integration seam.

2. **Verification tooling is Python-only.** `ruff`/`mypy`/`pytest` in the sandbox
   image assume a Python repo. A polyglot target would need per-language images
   and policy entries. Scoped intentionally to this project's domain.

3. **`test`/`import` verification needs the repo's own deps in the image.** The
   sandbox image ships ruff/mypy/pytest but not an arbitrary project's
   dependencies, and `--network none` blocks installing them. So `test`/`import`
   verification is fully honest only for repos whose deps are already present
   (or baked into a project-specific sandbox image). `lint`/`type_error` (with
   `--ignore-missing-imports`) work without project deps. Flagged as the main
   fidelity boundary; the fix is a per-repo image built with deps at ingest time.

4. **mypy runs with `--ignore-missing-imports`.** Without the project's deps
   installed (see #3), strict mypy would drown in import errors. The flag keeps
   it focused on the changed files' own type correctness. A per-repo image would
   let us drop the flag.

5. **Live Docker test is skip-gated, and covers `lint` only.**
   `test_sandbox_docker.py` proves the real container path end-to-end (good fix
   verifies, non-fix rejected) but skips when no daemon/image is present, and
   exercises the `lint` policy path only. The other policy paths are covered by
   pure unit tests plus the `FakeSandbox`; a full live matrix per failure type is
   the pre-Phase-4 acceptance step.

6. **Container isolation is hardened but not a hard multi-tenant boundary.** The
   run flags (no network, read-only, dropped caps, no-new-privileges, resource
   caps) defend against the realistic threat — a buggy or opportunistic patch. A
   determined kernel-exploit attacker is out of scope; production would add
   gVisor/Kata or a microVM. Stated plainly rather than overclaimed.

---

## Two bugs the live run caught that the FakeSandbox could not

Worth stating because they are the argument *for* keeping a real-daemon test
even though it is slow and skip-gated: both were invisible to every mocked test
and only appeared the first time a patch ran in an actual container.

1. **tmpfs mount was root-owned 0755, but the container runs as non-root.**
   Passing explicit tmpfs options (`rw,exec,size=…`) makes Docker drop its
   default `1777` mode, so `/work` came up owned by root at 0755 and the
   uid-10001 `sandbox` user could not `cp` the snapshot in — every run failed
   with `Permission denied`. Fixed by pinning `mode=1777` (a real `/tmp`'s mode)
   on both tmpfs mounts.

2. **Windows bind mount handed every file exec bits, tripping ruff EXE002.**
   `/src` is a bind mount from a Windows host; Docker Desktop cannot express
   Windows ACLs as Unix modes, so every file arrived 0755. ruff then flagged
   each plain `.py` as "executable but no shebang" (EXE002) and honest fixes
   failed verification — the sandbox was not faithful to a real Linux checkout
   (0644). Fixed by normalizing modes to 0644 (`find /work -type f -exec chmod
   0644`) after the copy. Note this is host-specific: the symptom appears because
   development is on Windows, but the normalization is correct on any host and
   keeps the sandbox's file modes reproducible regardless of where it runs.

Both are now covered by the live test (2 passed, no longer skipped, with the
image built). The lesson logged for interview defensibility: a mock proves your
*logic*; only the real boundary proves your *environment assumptions*.
