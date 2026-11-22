#!/usr/bin/env bash
# phx-code: wasaly
# phx-name: Wasaly
# phx-dir: /opt/wasaly-api
# phx-health: https://phoenixsystems.online/wasaly-api/health
# Deploy plugin for Wasaly API, run by phx-deploy (owned by PCC): detaching, locking, retry and rollback are done by PCC, not here.
# Exits non-zero when the deploy is not healthy.
set -euo pipefail
START=$(date +%s)
DIR=/opt/wasaly-api
REPO=sameh0el0sayed/Wasaly-App-Api
BRANCH=master
HEALTH=https://phoenixsystems.online/wasaly-api/health
TOKEN_FILE=/root/.gh_token

cd "$DIR"
# 1) source: fetch with the token and align the deploy clone to origin/master (this dir is deploy-only; .env, volumes and DB are untouched)
AUTH=$(printf 'x-access-token:%s' "$(cat "$TOKEN_FILE")" | base64 -w0)
git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $AUTH" fetch --quiet "https://github.com/$REPO.git" "$BRANCH"
git reset --hard FETCH_HEAD >/dev/null
echo "1.6.$(git rev-list --count HEAD)" > VERSION   # same scheme as the workflow (+1 per commit)
echo "source: $(git rev-parse --short HEAD) version $(cat VERSION)"

# 2) backup first (keep last 14); any failure stops the deploy
mkdir -p /opt/wasaly-backups
docker compose exec -T db pg_dump -U transport -d transport | gzip > "/opt/wasaly-backups/pre-deploy-$(date +%Y%m%d-%H%M%S).sql.gz"
test -s "$(ls -t /opt/wasaly-backups/*.sql.gz | head -1)"
ls -t /opt/wasaly-backups/*.sql.gz | tail -n +15 | xargs -r rm -f

# 3) build, migrate with the new image BEFORE the new API starts, then start
docker compose build api
docker compose run --rm --no-deps api alembic upgrade head   # prints "Running upgrade X -> Y" lines for PCC
# wait for db, redis and api healthchecks (fails on unhealthy)
docker compose up -d --build --remove-orphans --wait --wait-timeout 180 db redis api   # --remove-orphans removes the retired osrm container
docker compose exec -T api python -m scripts.import_translations
docker compose ps
docker compose exec -T api alembic current

# 4) nginx no-cache on /control/ (never fails the deploy)
bash scripts/nginx_control_nocache.sh || true

# 5) health: one line, non-zero exit on failure
BODY=$(mktemp)
code=$(curl -s -o "$BODY" -w "%{http_code}" "$HEALTH" || true)
if [ "$code" = "200" ]; then
  ver=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version",""))' "$BODY" 2>/dev/null || true)
  echo "wasaly: OK ${ver:-$(cat VERSION)}"
  echo "runtime: $(( $(date +%s) - START ))s"
else
  echo "wasaly: FAIL $code"
  docker compose logs api --tail=40
  exit 1
fi
