# Builds the frontend into backend/pool_manager/static. Paths are relative to this script.
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..\frontend")
try {
  npm ci --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
} finally {
  Pop-Location
}
