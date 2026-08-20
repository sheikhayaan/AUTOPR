# AutoPR fix-verification sandbox image.
#
# This is NOT the app image — it is the throwaway environment the Fix Verifier
# runs an untrusted patch inside. It carries exactly the tools the verification
# policy invokes (git to apply the patch; ruff/mypy/pytest to re-check it) and
# nothing else. Kept minimal on purpose: less surface for a hostile patch.
#
# Build once (the runner references it by tag, default autopr-sandbox:latest):
#     docker build -f docker/sandbox.Dockerfile -t autopr-sandbox:latest .
#
# At run time the container is further locked down by the DockerSandbox flags
# (--network none, --read-only, tmpfs work dir, dropped caps, pids/mem/cpu
# limits). This image only needs to provide the toolchain and a non-root user.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git: to `git apply` the proposed patch. The rest: the verification tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ruff mypy pytest

# Non-root. The work dir is a tmpfs mounted at run time and owned by this user.
RUN useradd --create-home --uid 10001 sandbox
USER sandbox

# No CMD/ENTRYPOINT: the runner supplies `sh -c <entrypoint>` per invocation.
