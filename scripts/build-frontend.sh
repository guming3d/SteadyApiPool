#!/bin/sh
# Builds the frontend into backend/pool_manager/static. Paths are relative to this script.
set -eu
cd "$(dirname "$0")/../frontend"
npm ci --no-audit --no-fund
npm run build
