#!/usr/bin/env bash
# Unzips a release package and starts it the way the README tells people to (no Node.js, no
# git), then checks that the backend answers and serves the UI. CI runs it on Linux and macOS,
# and the release workflow runs it before publishing.
#
# Usage: scripts/smoke-package.sh dist/SteadyApiPool-<version>.zip [port]
set -euo pipefail

ZIP="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
PORT="${2:-8765}"
WORK="$(mktemp -d)"
LOG="$WORK/start.log"
RUNNER=""

cleanup() {
  # start-local.sh runs in its own process group (set -m below); stop it and its backend.
  if [ -n "$RUNNER" ]; then
    kill -TERM -- "-$RUNNER" 2>/dev/null || kill -TERM "$RUNNER" 2>/dev/null || true
    wait "$RUNNER" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$WORK"
unzip -q "$ZIP"
cd "$(basename "${ZIP%.zip}")"
set -m
./scripts/start-local.sh --backend-port "$PORT" > "$LOG" 2>&1 &
RUNNER=$!

for _ in $(seq 1 180); do
  if curl --silent --fail --output /dev/null "http://127.0.0.1:$PORT/health"; then
    if curl --silent --fail --output /dev/null "http://127.0.0.1:$PORT/"; then
      echo "The package starts and serves the UI on port $PORT"
      exit 0
    fi
    cat "$LOG"
    echo "error: the backend is up but serves no UI" >&2
    exit 1
  fi
  kill -0 "$RUNNER" 2>/dev/null || break
  sleep 1
done
cat "$LOG"
echo "error: scripts/start-local.sh did not bring the backend up" >&2
exit 1
