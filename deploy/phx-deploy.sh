#!/usr/bin/env bash
# phx-code: wasaly
# phx-name: Wasaly
# phx-dir: /opt/wasaly-api
# phx-health: https://phoenixsystems.online/wasaly-api/health
# phx-rollback: yes
# Deploy plugin for Wasaly API, run by phx-deploy (owned by PCC): detaching, locking, renice, retry and cleanup are done by PCC, not here.
# Usage: phx-deploy wasaly            -> deploy origin/master
#        phx-deploy wasaly rollback   -> put the previous API image back (wasaly-api:previous), DB untouched
# Only this project's services are touched; only changed services are recreated. Exits non-zero when not healthy.
set -euo pipefail
START=$(date +%s)
DIR=/opt/wasaly-api
REPO=sameh0el0sayed/Wasaly-App-Api
BRANCH=master
HEALTH=https://phoenixsystems.online/wasaly-api/health
TOKEN_FILE=/root/.gh_token
IMAGE=wasaly-api

cd "$DIR"

health() {
  local body code ver
  body=$(mktemp)
  code=$(curl -s -o "$body" -w "%{http_code}" "$HEALTH" || true)
  if [ "$code" = "200" ]; then
    ver=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version",""))' "$body" 2>/dev/null || true)
    echo "wasaly: OK ${ver:-$(cat VERSION 2>/dev/null)}"
    echo "runtime: $(( $(date +%s) - START ))s"
  else
    echo "wasaly: FAIL $code"
    docker compose logs api --tail=40
    exit 1
  fi
}

if [ "${1:-}" = "rollback" ]; then
  docker image inspect "$IMAGE:previous" >/dev/null 2>&1 || { echo "wasaly: FAIL no $IMAGE:previous image"; exit 1; }
  docker tag "$IMAGE:previous" "$IMAGE:latest"
  docker compose up -d --no-deps --no-build --wait --wait-timeout 180 api
  echo "rollback: $IMAGE:previous is live"
  health
  exit 0
fi

# 1) source: fetch with the token and align the deploy clone to origin/master (this dir is deploy-only; .env, volumes and DB are untouched)
AUTH=$(printf 'x-access-token:%s' "$(cat "$TOKEN_FILE")" | base64 -w0)
git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $AUTH" fetch --quiet "https://github.com/$REPO.git" "$BRANCH"
git reset --hard FETCH_HEAD >/dev/null
echo "1.6.$(git rev-list --count HEAD)" > VERSION   # same scheme as the workflow (+1 per commit)
echo "source: $(git rev-parse --short HEAD) version $(cat VERSION)"

# 2) backup first (keep last 14); any failure stops the deploy
mkdir -p /opt/wasaly-backups
docker compose exec -T db nice -n 19 pg_dump -U transport -d transport | nice -n 19 gzip > "/opt/wasaly-backups/pre-deploy-$(date +%Y%m%d-%H%M%S).sql.gz"
test -s "$(ls -t /opt/wasaly-backups/*.sql.gz | head -1)"
ls -t /opt/wasaly-backups/*.sql.gz | tail -n +15 | xargs -r rm -f

# 3) build first (the running API keeps serving); keep the current image as :previous for rollback
docker image inspect "$IMAGE:latest" >/dev/null 2>&1 && docker tag "$IMAGE:latest" "$IMAGE:previous" || true
if ! nice -n 19 docker compose build api; then
  docker image inspect "$IMAGE:previous" >/dev/null 2>&1 && docker tag "$IMAGE:previous" "$IMAGE:latest" || true
  echo "wasaly: FAIL build (running API untouched)"
  exit 1
fi

# 4) db / redis: recreated only when their compose config changed (no-op otherwise)
docker compose up -d --no-deps --no-build --remove-orphans --wait --wait-timeout 180 db redis

# 5) migrate with the new image BEFORE the new API starts, then recreate only the API
docker compose run --rm --no-deps api alembic upgrade head   # prints "Running upgrade X -> Y" lines for PCC
docker compose up -d --no-deps --no-build --wait --wait-timeout 180 api
docker compose exec -T api python -m scripts.import_translations
docker compose ps
docker compose exec -T api alembic current

# 6) health: one line, non-zero exit on failure
health
