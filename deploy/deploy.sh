#!/usr/bin/env bash
# Deploy, verify, and roll back if the verification fails.
#
#   cd /opt/outreach && ./deploy/deploy.sh
#
# Manual, never automatic (§19). An auto-deploy on green CI is a fine thing to have once there
# is a staging environment to be wrong in first; until then the person running this is the last
# review step.
#
# What it does, in order: refuse if the working tree is dirty, record the image currently
# running, pull, build the new image, **run the migrations before starting anything new**, start
# the new containers, wait for /ready, and put the old image back if readiness never arrives.
#
# What it deliberately does not do:
#   * It never touches SEND_MODE. Going live is a separate, deliberate act by the owner
#     (hard rule 1), and a deploy script that could flip it would make every deploy a decision
#     about sending.
#   * It never runs `down -v`. That flag destroys the data volume, and no deploy needs it.
#   * It never writes a secret to stdout, because this output is what gets pasted into a chat
#     when a deploy goes wrong.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# `--project-directory` is pinned so the project name, the volume names and every relative path
# are the same whether this is run from the repository root or anywhere else. `env_file` does not
# need it — that resolves relative to the compose file — but a deploy that behaves differently
# depending on the caller's working directory is not worth the saving.
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/deploy/compose.prod.yml")
READY_URL="${READY_URL:-http://127.0.0.1:8000/health}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$ROOT/.env" ]] || die "no .env in $ROOT. Copy env.example and fill it in (docs/DEPLOY.md)."

# A dirty tree means the thing being deployed is not the thing in git, so nobody can say later
# what is running.
if [[ -n "$(git status --porcelain)" ]]; then
	die "working tree is not clean. Commit or stash first; a deploy must be a known commit."
fi

# The tag currently running, read off the container rather than stored in a file, so a manual
# `docker compose up` done at 2am does not leave this script lying about what it is replacing.
PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' outreach-api 2>/dev/null || true)"
PREVIOUS_TAG="${PREVIOUS_IMAGE##*:}"
[[ "$PREVIOUS_TAG" == "$PREVIOUS_IMAGE" ]] && PREVIOUS_TAG=""
PREVIOUS_SHA="$(git rev-parse --short HEAD)"

say "pulling"
git pull --ff-only

NEW_SHA="$(git rev-parse --short HEAD)"
export IMAGE_TAG="$NEW_SHA"
say "deploying ${NEW_SHA} (was ${PREVIOUS_SHA}${PREVIOUS_TAG:+, image ${PREVIOUS_TAG}})"

say "building"
"${COMPOSE[@]}" build --pull

# Migrations run in a throwaway container, before the new api and worker start, and before the
# old ones stop. Forward-only and backwards-compatible within a release (§19), so the still-
# running old code tolerates the new schema for the few seconds in between.
say "migrating"
"${COMPOSE[@]}" run --rm --no-deps -T api alembic upgrade head

say "starting"
"${COMPOSE[@]}" up -d --remove-orphans

say "waiting for the api (up to ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
until curl -fsS --max-time 5 "$READY_URL" >/dev/null 2>&1; do
	if (( $(date +%s) >= deadline )); then
		printf '\n'
		"${COMPOSE[@]}" logs --tail 60 api || true
		say "api never became healthy; rolling back"
		if [[ -n "$PREVIOUS_TAG" ]]; then
			# The schema is left migrated on purpose: rolling a migration back
			# automatically is how data gets dropped at the worst possible moment. The
			# migrations are written to be backwards-compatible for exactly this case.
			IMAGE_TAG="$PREVIOUS_TAG" "${COMPOSE[@]}" up -d --no-build api worker
			die "rolled back to ${PREVIOUS_TAG}. The schema is still at head; see docs/DEPLOY.md."
		fi
		die "no previous image to roll back to. Fix forward, or restore from a backup."
	fi
	printf '.'
	sleep 3
done
printf '\n'

# /ready is checked separately and is *not* fatal: it fails until the worker has written its
# first heartbeat, which takes up to a minute, and a worker that is still starting is not a
# reason to roll back an api that is serving.
say "readiness"
curl -fsS --max-time 5 "${READY_URL%/health}/ready" || true
printf '\n'

say "running"
"${COMPOSE[@]}" ps

cat <<'NOTE'

Deployed. Two things worth a look:

  * SEND_MODE is whatever .env says and this script did not change it. Check it with:
        docker compose --env-file .env -f deploy/compose.prod.yml exec api \
            python -m app.scripts.check_env
  * /ready stays 503 until the worker's first heartbeat (about a minute). If it is still 503
    after two minutes, the worker did not start:
        docker compose --env-file .env -f deploy/compose.prod.yml logs --tail 50 worker
NOTE
