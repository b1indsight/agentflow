#!/usr/bin/env bash
# CI-only disposable stack; never point this at an existing deployment project.
set -euo pipefail
: "${FLOWTOOL_SMOKE_SERVE_IMAGE:=flowtool-serve:test}"
: "${FLOWTOOL_SMOKE_WORKER_IMAGE:=flowtool-worker:test}"
command -v docker >/dev/null
project_root=$(pwd)
cache_day=$(date +%F)
mkdir -p "$project_root/.cache/$cache_day"
smoke_root=$(mktemp -d "$project_root/.cache/$cache_day/docker-smoke.XXXXXX")
project_name="flowtool-smoke-${RANDOM}-${RANDOM}"
export COMPOSE_PROJECT_NAME="$project_name"
export FLOWTOOL_SERVE_IMAGE="$FLOWTOOL_SMOKE_SERVE_IMAGE"
export FLOWTOOL_WORKER_IMAGE="$FLOWTOOL_SMOKE_WORKER_IMAGE"
export FLOWTOOL_DEPLOY_DIR="$smoke_root"
# Explicit subnet lets the regression reserve the old serve address deterministically.
printf 'networks:\n  control:\n    ipam:\n      config:\n        - subnet: 10.203.%s.0/24\n' "$((RANDOM % 256))" > "$smoke_root/network.yaml"
compose=(docker compose -f deploy/docker/compose.yaml -f "$smoke_root/network.yaml" --profile database)
cleanup() {
  docker rm -f "$project_name-ip-holder" >/dev/null 2>&1 || true
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
mkdir "$smoke_root/secrets"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=worker-gateway \
  -addext subjectAltName=DNS:worker-gateway \
  -keyout "$smoke_root/secrets/tls.key" -out "$smoke_root/secrets/tls.crt" >/dev/null 2>&1
openssl genrsa -out "$smoke_root/secrets/app.pem" 2048 >/dev/null 2>&1
printf '%s' smoke-password > "$smoke_root/secrets/db-password"
docker run --rm --entrypoint cat "$FLOWTOOL_WORKER_IMAGE" /etc/ssl/certs/ca-certificates.crt \
  > "$smoke_root/secrets/ca-bundle.pem"
cat "$smoke_root/secrets/tls.crt" >> "$smoke_root/secrets/ca-bundle.pem"
chmod 444 "$smoke_root"/secrets/*
cat > "$smoke_root/serve.env" <<'ENV'
FLOWTOOL_DATABASE_URL=postgresql://flowtool:smoke-password@postgres:5432/flowtool
FLOWTOOL_GITHUB_APP_ID=1
FLOWTOOL_GITHUB_WEBHOOK_SECRET=smoke-only
FLOWTOOL_WORKER_TOKENS={"worker-1":"smoke-worker"}
ENV
cat > "$smoke_root/worker.env" <<'ENV'
FLOWTOOL_WORKER_ID=worker-1
FLOWTOOL_WORKER_TOKEN=smoke-worker
FLOWTOOL_TRUSTED_HOST_EXECUTION=false
ENV
"${compose[@]}" config --quiet
for role in serve worker; do
  if [ "$role" = serve ]; then image="$FLOWTOOL_SERVE_IMAGE"; else image="$FLOWTOOL_WORKER_IMAGE"; fi
  test "$(docker run --rm --entrypoint id "$image" -u)" = 10001
  docker run --rm "$image" --help >/dev/null
  docker run --rm --entrypoint python "$image" -c \
    "from pathlib import Path; assert not Path('/opt/flowtool/.env').exists(); assert not Path('/opt/flowtool/.git').exists()"
done
docker run --rm --env-file deploy/docker/worker.env.example --entrypoint sh "$FLOWTOOL_WORKER_IMAGE" -ec 'test -n "$CODEX_API_KEY"'
docker run --rm --entrypoint codex "$FLOWTOOL_WORKER_IMAGE" --version
docker run --rm --entrypoint jj "$FLOWTOOL_WORKER_IMAGE" --version
"${compose[@]}" up -d postgres serve worker-gateway worker
# Read the full epoch from the isolated fixture DB, not truncated CLI output or
# a persisted online row which can outlive its socket when serve disappears.
wait_online_epoch() {
  local previous="$1" current attempt
  for attempt in $(seq 1 60); do
    current=$("${compose[@]}" exec -T postgres psql -U flowtool -d flowtool -Atc \
      "SELECT connection_epoch FROM workers WHERE worker_id='worker-1' AND status='online'" 2>/dev/null) || current=''
    if [ -n "$current" ] && [ "$current" != "$previous" ]; then
      printf '%s\n' "$current"
      return 0
    fi
    sleep 2
  done
  echo 'Worker did not establish a fresh online connection epoch' >&2
  return 1
}
initial_epoch=$(wait_online_epoch '')
boundary_check='test ! -e /run/secrets/app_key; test ! -e /run/secrets/db_password; touch /var/lib/flowtool/persisted'
"${compose[@]}" exec -T worker sh -ec "$boundary_check"
for forbidden in app_key db_password; do
  if "${compose[@]}" run --rm --no-deps --entrypoint sh \
    -v "$smoke_root/secrets/app.pem:/run/secrets/$forbidden:ro" worker -ec "$boundary_check"; then
    echo "Boundary assertion failed to reject $forbidden" >&2
    exit 1
  fi
done
"${compose[@]}" stop worker
"${compose[@]}" rm -f worker
"${compose[@]}" up -d worker
"${compose[@]}" exec -T worker test -f /var/lib/flowtool/persisted
old_epoch=$(wait_online_epoch "$initial_epoch")
worker_id=$("${compose[@]}" ps -q worker)
worker_process=$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}} {{.State.Running}}' "$worker_id")
serve_id=$("${compose[@]}" ps -q serve)
control_network="${project_name}_control"
old_ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$control_network\").IPAddress}}" "$serve_id")
gateway_id=$("${compose[@]}" ps -q worker-gateway)
"${compose[@]}" stop serve
"${compose[@]}" rm -f serve
docker run -d --name "$project_name-ip-holder" --network "$control_network" --ip "$old_ip" \
  --entrypoint sleep "$FLOWTOOL_SERVE_IMAGE" 180 >/dev/null
"${compose[@]}" up -d --no-deps serve
new_ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$control_network\").IPAddress}}" "$("${compose[@]}" ps -q serve)")
test "$old_ip" != "$new_ip"
test "$gateway_id" = "$("${compose[@]}" ps -q worker-gateway)"
new_epoch=$(wait_online_epoch "$old_epoch")
test "$worker_id" = "$("${compose[@]}" ps -q worker)"
test "$worker_process" = "$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}} {{.State.Running}}' "$worker_id")"
printf 'Live worker reconnected: epoch %s -> %s; worker process unchanged.\n' "$old_epoch" "$new_epoch"
printf 'Docker role/TLS/volume/reconnect smoke passed (no live model or GitHub task).\n'
