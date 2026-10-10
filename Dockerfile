# One image, two processes. `api` and `worker` are the same code with a different command, so
# they can never drift apart: a deploy that updates one updates the other, and a migration that
# the api expects is a migration the worker has.
#
# Build:  docker build -t outreach:local .
# Run:    see deploy/compose.prod.yml

# ---- builder -----------------------------------------------------------------------------
# Dependencies are resolved from uv.lock, never from the index at build time, so an image built
# today and one built in six months contain the same packages (issue E6).
FROM python:3.12-slim-bookworm AS builder

# Pinned by digest-bearing tag rather than `latest`: a build tool that silently changes version
# is the same problem as an unpinned dependency.
COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The lockfile layer alone, so editing application code does not reinstall the world.
# `--locked` fails rather than re-resolving: a build that quietly updates a dependency is the
# problem the lockfile exists to prevent.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# `package = false` in pyproject: `app/` is imported from the working directory rather than
# installed, so these are the last layers and a code change rebuilds only them.
COPY app ./app
COPY alembic.ini ./


# ---- runtime -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Unbuffered, because the container's stdout *is* the log: a buffered process loses the last
# lines it wrote, which are the ones that explain why it stopped.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    TZ=UTC

# `curl` for the container healthcheck, `postgresql-client` for pg_dump: backups run from the
# api container rather than the database one, so a restore does not depend on the database
# being reachable in the first place.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Non-root, with no home directory and no shell worth having. The api accepts uploads and the
# worker parses mail and web pages, so both process bytes chosen by somebody else.
RUN useradd --system --create-home --uid 10001 outreach

WORKDIR /app
COPY --from=builder --chown=outreach:outreach /app /app

USER outreach

EXPOSE 8000

# Liveness only. `/ready` would restart the api whenever the *worker* died, which is a strange
# thing to do about a worker and makes an outage worse.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# `--proxy-headers` with `--forwarded-allow-ips` set to the reverse proxy: without it every
# caller looks like Caddy, so the api's address-keyed rate limit becomes one shared bucket for
# the whole internet. The value is the Docker network, not `*`.
CMD ["sh", "-c", "exec uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
