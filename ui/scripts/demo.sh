#!/usr/bin/env bash
# Bring the whole product up on a laptop and drive the §12 demo through the UI.
#
# No Slack workspace, no Jira site, no model API key: the fixture corpus stands
# in for the first two and the offline embedder for the third. What is real is
# everything in between — migrations, the permission filter, the approval gate,
# the write-back executor, and every page.
#
#   ./ui/scripts/demo.sh            # seed, serve, drive, tear down
#   ./ui/scripts/demo.sh --serve    # seed and serve, then leave it running
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="${HIPPO_DEMO_DB:-hippo_demo}"
DSN="${HIPPO_DEMO_DSN:-postgresql://localhost:5432/${DB}}"
API_PORT="${HIPPO_DEMO_API_PORT:-8100}"
UI_PORT="${HIPPO_DEMO_UI_PORT:-3100}"
PG_BIN="${HIPPO_PG_BIN:-}"
PYTHON="${ROOT}/.venv/bin/python"

pg() { if [ -n "$PG_BIN" ]; then "${PG_BIN}/$1" "${@:2}"; else "$1" "${@:2}"; fi }

cleanup() {
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "${UI_PID:-}" ] && kill "$UI_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> database"
pg dropdb --if-exists "$DB" 2>/dev/null || true
pg createdb "$DB"
(cd "$ROOT" && "$PYTHON" -m deploy.demo.seed "$DSN")

echo "==> api on :${API_PORT}"
HIPPO_DATABASE_URL="$DSN" HIPPO_LOG_LEVEL=WARNING \
  "$PYTHON" -m uvicorn "api.main:create_app" --factory --port "$API_PORT" --log-level warning \
  >/tmp/hippo-demo-api.log 2>&1 &
API_PID=$!

echo "==> ui on :${UI_PORT}"
(cd "${ROOT}/ui" && HIPPO_API_URL="http://localhost:${API_PORT}" PORT="$UI_PORT" \
  npm run start >/tmp/hippo-demo-ui.log 2>&1) &
UI_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${API_PORT}/healthz" >/dev/null 2>&1 &&
     curl -fsS "http://localhost:${UI_PORT}/login" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [ "${1:-}" = "--serve" ]; then
  echo
  echo "Hippo is up:  http://localhost:${UI_PORT}"
  echo "Sign in as    alice@example.com  or  carol@example.com"
  echo "Password      hippo-demo-password"
  echo
  echo "Ask 'what is blocking the Acme renewal?' as each of them."
  echo "Ctrl-C to stop."
  wait "$UI_PID"
  exit 0
fi

echo "==> driving the demo"
export HIPPO_DEMO_EXECUTOR="$PYTHON -m deploy.demo.execute $DSN"
node "${ROOT}/ui/scripts/smoke.mjs" \
  "http://localhost:${UI_PORT}" alice@example.com hippo-demo-password
