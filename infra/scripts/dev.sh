#!/usr/bin/env bash
# Local development: start or stop the whole stack.
#   infra/scripts/dev.sh up      containers, migrations, bucket, API, workers, web (logs in .dev/logs)
#   infra/scripts/dev.sh down    stop the app processes (containers keep running)
#   infra/scripts/dev.sh status  what is running
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN="$ROOT/.dev"
LOGS="$RUN/logs"
mkdir -p "$LOGS"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env: copy .env.example to .env first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

start() { # name, command...
  local name="$1"; shift
  if [[ -f "$RUN/$name.pid" ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then
    echo "  $name already running"
    return
  fi
  nohup "$@" >"$LOGS/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "  $name started (log: .dev/logs/$name.log)"
}

stop() { # name
  local name="$1" pid
  [[ -f "$RUN/$name.pid" ]] || return 0
  pid="$(cat "$RUN/$name.pid")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
    echo "  $name stopped"
  fi
  rm -f "$RUN/$name.pid"
}

PROCS=(api chat worker-ingest worker-agent worker-export web)

case "${1:-up}" in
  up)
    docker info >/dev/null 2>&1 || { open -a Docker 2>/dev/null || true; echo "Waiting for Docker..."; until docker info >/dev/null 2>&1; do sleep 2; done; }
    docker compose -f infra/docker/compose.yml up -d
    until pg_isready -h localhost -p 5432 -q 2>/dev/null; do sleep 1; done
    uv run --no-sync alembic -c database/alembic.ini upgrade head
    if [[ -n "${S3_ENDPOINT_URL:-}" ]]; then
      uv run --no-sync python -m storage.local_setup
    else
      echo "Using AWS S3 bucket ${S3_BUCKET} in ${S3_REGION:-$AWS_REGION} (profile ${AWS_PROFILE:-default})"
    fi
    start api uv run --no-sync uvicorn api.main:app --port 8000
    start chat uv run --no-sync uvicorn chat.main:app --port 8001
    start worker-ingest uv run --no-sync python -m worker.run --queue ingest --slots 1
    start worker-agent uv run --no-sync python -m worker.run --queue agent --slots 2
    start worker-export uv run --no-sync python -m worker.run --queue export --slots 1
    # Fixed port: the S3 bucket CORS rule allows this origin (WEB_ORIGIN).
    start web bash -c "cd frontend && exec npm run dev -- --port ${WEB_PORT:-3001}"
    echo "Waiting for the web app..."
    for _ in $(seq 1 60); do
      url="$(grep -oE 'http://localhost:[0-9]+' "$LOGS/web.log" | head -1 || true)"
      [[ -n "$url" ]] && curl -s -o /dev/null "$url/projects" && break
      sleep 2
    done
    echo "Clipora is up: ${url:-see .dev/logs/web.log}  (API docs: http://localhost:8000/api/docs)"
    ;;
  down)
    for p in "${PROCS[@]}"; do stop "$p"; done
    ;;
  status)
    for p in "${PROCS[@]}"; do
      if [[ -f "$RUN/$p.pid" ]] && kill -0 "$(cat "$RUN/$p.pid")" 2>/dev/null; then echo "  $p: running"; else echo "  $p: stopped"; fi
    done
    docker compose -f infra/docker/compose.yml ps --format '  {{.Service}}: {{.State}}'
    ;;
  *)
    echo "usage: infra/scripts/dev.sh up|down|status" >&2
    exit 2
    ;;
esac
