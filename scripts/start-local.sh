#!/usr/bin/env bash
# Starts the backend (uvicorn) with the browser frontend (Vite dev server) for local development.
# Checks the machine first (scripts/doctor.py), installs dependencies when they are missing or
# out of date, then runs until Ctrl+C.
# Without Node.js 22+ the backend serves the UI already built into backend/pool_manager/static
# (the release package has it; --prebuilt does the same with Node.js installed).
#
# Local development uses your Azure identity (the app's sign-in, or az / azd / PowerShell)
# against a real gateway (Azure API Management or LiteLLM) and Foundry. Writes are live.
#
# Usage: scripts/start-local.sh [--prebuilt] [--backend-port 8000] [--frontend-port 5174]
#                               [--skip-install] [--skip-checks]
set -euo pipefail

BACKEND_PORT=8000
FRONTEND_PORT=5174
SKIP_INSTALL=0
SKIP_CHECKS=0
PREBUILT=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --backend-port) BACKEND_PORT="${2:?--backend-port needs a value}"; shift 2 ;;
    --frontend-port) FRONTEND_PORT="${2:?--frontend-port needs a value}"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    --prebuilt) PREBUILT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'error: unknown option %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
DOCTOR="$ROOT/scripts/doctor.py"
STATIC_INDEX="$BACKEND/pool_manager/static/index.html"

step() { printf '\033[36m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

# --- Prerequisites -------------------------------------------------------------------

python_help() {
  local need="Python 3.11 or later is required"
  case "$(uname -s)" in
    Darwin) echo "$need: brew install python@3.13, or https://www.python.org/downloads/macos/" ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        echo "$need: sudo apt install python3 python3-venv (Ubuntu 22.04 and Debian 11: python3.11 python3.11-venv)"
      elif command -v dnf >/dev/null 2>&1; then echo "$need: sudo dnf install python3.12"
      elif command -v zypper >/dev/null 2>&1; then echo "$need: sudo zypper install python312"
      elif command -v pacman >/dev/null 2>&1; then echo "$need: sudo pacman -S python"
      else echo "$need: https://www.python.org/downloads/"
      fi ;;
    *) echo "$need: winget install -e --id Python.Python.3.13, or https://www.python.org/downloads/" ;;
  esac
}

PYTHON=""
OLD_PYTHON=""
# Newest first: distributions with an old python3 often have python3.11+ installed beside it.
for candidate in python3.13 python3.12 python3.11 python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  # The version check also rejects the Windows Store "python" stub, which exits non-zero.
  if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
  if [ -z "$OLD_PYTHON" ] && "$candidate" -c pass >/dev/null 2>&1; then OLD_PYTHON="$candidate"; fi
done
if [ -z "$PYTHON" ]; then
  # An older Python can still run the doctor, which names the install command for this OS.
  [ -z "$OLD_PYTHON" ] || "$OLD_PYTHON" "$DOCTOR" --brief || true
  fail "$(python_help)"
fi

if [ "$SKIP_CHECKS" -eq 0 ]; then
  # Prints only problems: an old Node.js, no network to Azure, proxy or TLS inspection trouble.
  "$PYTHON" "$DOCTOR" --brief || exit 1
fi

# The browser UI: the Vite dev server with Node.js 22+, else the build the backend serves itself.
USE_VITE=0
NODE_MAJOR=0
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
fi
if [ "$PREBUILT" -eq 0 ] && [ -f "$FRONTEND/package.json" ] && [ "$NODE_MAJOR" -ge 22 ]; then
  USE_VITE=1
elif [ ! -f "$STATIC_INDEX" ]; then
  fail "The browser UI needs Node.js 22 or later to build (https://nodejs.org/), or the release package, which has it built in."
elif [ "$PREBUILT" -eq 0 ] && [ -f "$FRONTEND/package.json" ]; then
  step "Node.js 22+ not found: serving the prebuilt UI from backend/pool_manager/static"
elif [ "$PREBUILT" -eq 0 ]; then
  step "Serving the UI built into this package"
fi

# --- Configuration -------------------------------------------------------------------

ENV_FILE="$BACKEND/.env"
if [ ! -f "$ENV_FILE" ]; then
  # Nothing to fill in: the app's setup page deploys, chooses or connects the gateway.
  cp "$BACKEND/.env.example" "$ENV_FILE"
  step "Created backend/.env from .env.example"
fi
# .env may have Windows line endings and quoted values, which pydantic-settings unquotes.
env_value() {
  tr -d '\r' < "$ENV_FILE" | sed -n "s/^$1=//p" | tail -n 1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

SUBSCRIPTION="${POOL_MANAGER_SUBSCRIPTION_ID:-$(env_value POOL_MANAGER_SUBSCRIPTION_ID)}"
RESOURCE_GROUP="${POOL_MANAGER_RESOURCE_GROUP:-$(env_value POOL_MANAGER_RESOURCE_GROUP)}"
APIM_NAME="${POOL_MANAGER_APIM_NAME:-$(env_value POOL_MANAGER_APIM_NAME)}"
GATEWAY_NAME="${POOL_MANAGER_GATEWAY_NAME:-$(env_value POOL_MANAGER_GATEWAY_NAME)}"
GATEWAY_URL="${POOL_MANAGER_GATEWAY_URL:-$(env_value POOL_MANAGER_GATEWAY_URL)}"
AUTH_MODE="${POOL_MANAGER_AUTH_MODE:-$(env_value POOL_MANAGER_AUTH_MODE)}"
# The gateway is optional here (the setup page chooses one). An APIM service or a LiteLLM
# Container App needs its subscription, resource group and name; a LiteLLM gateway elsewhere
# needs its name and URL.
NAME="$APIM_NAME$GATEWAY_NAME"
if [ -n "$APIM_NAME" ] && [ -n "$GATEWAY_NAME$GATEWAY_URL" ]; then
  fail "backend/.env names both an APIM service and a LiteLLM gateway; keep one."
elif [ -n "$GATEWAY_URL" ]; then
  [ -n "$GATEWAY_NAME" ] || fail "POOL_MANAGER_GATEWAY_URL needs POOL_MANAGER_GATEWAY_NAME in backend/.env."
elif [ -n "$SUBSCRIPTION$RESOURCE_GROUP$NAME" ] \
  && { [ -z "$SUBSCRIPTION" ] || [ -z "$RESOURCE_GROUP" ] || [ -z "$NAME" ]; }; then
  fail "Set POOL_MANAGER_SUBSCRIPTION_ID, POOL_MANAGER_RESOURCE_GROUP and POOL_MANAGER_APIM_NAME (or POOL_MANAGER_GATEWAY_NAME) in backend/.env, or remove them to choose the gateway in the app."
fi
if [ "$SUBSCRIPTION" = "00000000-0000-0000-0000-000000000000" ] \
  || [ "$RESOURCE_GROUP" = "my-apim-rg" ] || [ "$APIM_NAME" = "my-apim" ] \
  || [ "$RESOURCE_GROUP" = "my-gateway-rg" ] || [ "$GATEWAY_NAME" = "my-gateway" ]; then
  fail "backend/.env still has the example gateway values; set real ones or remove them to choose the gateway in the app."
fi
# Locally there is no App Service Authentication, so easy_auth would answer 401 to everything.
[ "$AUTH_MODE" = "disabled" ] || fail "Set POOL_MANAGER_AUTH_MODE=disabled in backend/.env for local development."

# --- Dependencies --------------------------------------------------------------------

if [ -x "$BACKEND/.venv/Scripts/python.exe" ]; then
  VENV_PYTHON="$BACKEND/.venv/Scripts/python.exe"
else
  VENV_PYTHON="$BACKEND/.venv/bin/python"
fi

if [ "$SKIP_INSTALL" -eq 0 ]; then
  if [ ! -x "$VENV_PYTHON" ]; then
    step "Creating backend/.venv"
    "$PYTHON" -m venv "$BACKEND/.venv" \
      || fail "Could not create backend/.venv; '$PYTHON scripts/doctor.py' says why."
    if [ -x "$BACKEND/.venv/Scripts/python.exe" ]; then VENV_PYTHON="$BACKEND/.venv/Scripts/python.exe"; fi
  fi
  STAMP="$BACKEND/.venv/.requirements.cksum"
  CHECKSUM="$(cksum < "$BACKEND/requirements.txt")"
  if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$CHECKSUM" ]; then
    step "Installing backend requirements"
    "$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet -r "$BACKEND/requirements.txt" \
      || fail "pip install failed; '$PYTHON scripts/doctor.py' checks the network, proxy and TLS."
    printf '%s' "$CHECKSUM" > "$STAMP"
  fi
  # npm ci writes node_modules/.package-lock.json; a newer lockfile means dependencies changed.
  if [ "$USE_VITE" -eq 1 ] && { [ ! -f "$FRONTEND/node_modules/.package-lock.json" ] \
    || [ "$FRONTEND/package-lock.json" -nt "$FRONTEND/node_modules/.package-lock.json" ]; }; then
    step "Installing frontend dependencies"
    (cd "$FRONTEND" && npm ci --no-audit --no-fund)
  fi
else
  [ -x "$VENV_PYTHON" ] || fail "backend/.venv is missing; run without --skip-install."
  if [ "$USE_VITE" -eq 1 ]; then
    [ -d "$FRONTEND/node_modules" ] || fail "frontend/node_modules is missing; run without --skip-install."
  fi
fi

# --- Run -----------------------------------------------------------------------------

# Job control puts each server in its own process group, so stopping one stops its children
# (npm → Vite, uvicorn's reloader → worker).
set -m
BACKEND_PID=""
FRONTEND_PID=""
stop_tree() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  if [ -r "/proc/$pid/winpid" ] && command -v taskkill >/dev/null 2>&1; then
    # Git Bash: kill cannot reach native Windows children, so stop the tree by Windows PID.
    taskkill //T //F //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1 || true
  else
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
}
cleanup() {
  trap - EXIT INT TERM
  stop_tree "$FRONTEND_PID"
  stop_tree "$BACKEND_PID"
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

assert_port_free() {
  # Usually a server left over from an earlier run, so name the process that holds the port.
  local port="$1" option="$2" pid
  if command -v lsof >/dev/null 2>&1; then
    # lsof exits 1 when nothing listens, which pipefail and set -e would turn into a silent exit.
    pid="$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    [ -z "$pid" ] || fail "Port $port is in use by PID $pid ($(ps -o command= -p "$pid" 2>/dev/null)). Stop it, or pass $option."
  elif command -v netstat >/dev/null 2>&1 && command -v taskkill >/dev/null 2>&1; then
    # Git Bash has no lsof; Windows netstat lists the owning PID in the last column.
    pid="$(netstat -ano -p TCP | awk -v p=":$port" '$4 == "LISTENING" && $2 ~ p "$" { print $5; exit }' || true)"
    [ -z "$pid" ] || fail "Port $port is in use by Windows PID $pid. Stop it with 'taskkill //T //F //PID $pid', or pass $option."
  fi
}
assert_port_free "$BACKEND_PORT" --backend-port
[ "$USE_VITE" -eq 0 ] || assert_port_free "$FRONTEND_PORT" --frontend-port

step "Starting backend on http://127.0.0.1:$BACKEND_PORT"
(cd "$BACKEND" && exec "$VENV_PYTHON" -m uvicorn pool_manager.app:app \
  --host 127.0.0.1 --port "$BACKEND_PORT" --reload --reload-dir pool_manager) &
BACKEND_PID=$!

if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 60); do
    curl --silent --fail --output /dev/null "http://127.0.0.1:$BACKEND_PORT/health" && break
    kill -0 "$BACKEND_PID" 2>/dev/null || fail "The backend exited during startup (is port $BACKEND_PORT in use?)."
    sleep 0.5
  done
fi

APP_URL="http://127.0.0.1:$BACKEND_PORT"
if [ "$USE_VITE" -eq 1 ]; then
  step "Starting frontend on http://localhost:$FRONTEND_PORT"
  (cd "$FRONTEND" && API_PROXY_TARGET="http://127.0.0.1:$BACKEND_PORT" \
    exec npm run dev -- --port "$FRONTEND_PORT" --strictPort) &
  FRONTEND_PID=$!
  APP_URL="http://localhost:$FRONTEND_PORT"
fi

printf '\n  App:     \033[1m%s\033[0m\n' "$APP_URL"
printf '  API:     http://127.0.0.1:%s/api\n' "$BACKEND_PORT"
if [ -n "$APIM_NAME" ]; then
  printf '  \033[33mAPIM:    %s (resource group %s). Changes are live.\033[0m\n' "$APIM_NAME" "$RESOURCE_GROUP"
elif [ -n "$GATEWAY_URL" ]; then
  printf '  \033[33mGateway: %s (%s). Changes are live.\033[0m\n' "$GATEWAY_NAME" "$GATEWAY_URL"
elif [ -n "$GATEWAY_NAME" ]; then
  printf '  \033[33mGateway: %s (resource group %s). Changes are live.\033[0m\n' "$GATEWAY_NAME" "$RESOURCE_GROUP"
else
  printf "  \033[33mGateway: deployed, chosen or connected on the app's setup page. Changes are live.\033[0m\n"
fi
if [ "$USE_VITE" -eq 0 ]; then
  printf '  Press Ctrl+C to stop the server.\n\n'
  wait "$BACKEND_PID" || true
  fail "The backend stopped."
fi
printf '  Press Ctrl+C to stop both servers.\n\n'

# `wait -n` needs bash 4.3; macOS ships 3.2, so poll instead.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
kill -0 "$BACKEND_PID" 2>/dev/null || fail "The backend stopped; stopping the frontend."
fail "The frontend stopped (is port $FRONTEND_PORT in use?); stopping the backend."
