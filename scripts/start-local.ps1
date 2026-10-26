<#
.SYNOPSIS
  Starts the backend (uvicorn) with the browser frontend (Vite dev server), or with the
  terminal UI (-Tui), for local development.

.DESCRIPTION
  Checks the machine first (scripts/doctor.py), installs dependencies when they are missing or
  out of date, then runs both in this console until Ctrl+C (or q in the TUI). Without Node.js 22+
  the backend serves the UI already built into backend/pool_manager/static (the release package
  has it). Local development uses your Azure identity (the app's sign-in, or az / azd /
  PowerShell) against a real APIM service. Writes are live.

  Where the execution policy blocks scripts, run scripts\start-local.cmd instead.

.EXAMPLE
  scripts/start-local.ps1
  scripts/start-local.ps1 -Tui
  scripts/start-local.ps1 -Prebuilt
  scripts/start-local.ps1 -BackendPort 8001 -FrontendPort 5175 -SkipInstall
#>
param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5174,
  # The terminal UI (tui/) instead of the browser one; Node.js isn't needed then.
  [switch]$Tui,
  # Serve the prebuilt UI from the backend, as without Node.js, even when Node.js is installed.
  [switch]$Prebuilt,
  [switch]$SkipInstall,
  # Skip scripts/doctor.py (Python, Node.js, network, proxy and TLS checks).
  [switch]$SkipChecks
)
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$TuiDir = Join-Path $Root "tui"
$Doctor = Join-Path $PSScriptRoot "doctor.py"
$StaticIndex = Join-Path $Backend "pool_manager/static/index.html"
# Windows PowerShell 5.1 has no $IsWindows and only runs on Windows.
$OnWindows = $PSVersionTable.PSEdition -ne "Core" -or $IsWindows

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { Write-Host "error: $Message" -ForegroundColor Red; exit 1 }
function Warn([string]$Message) { Write-Host "warning: $Message" -ForegroundColor Yellow }
function Test-Command([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }
function Test-Native([string]$Exe, [string[]]$Arguments) {
  # Windows PowerShell turns redirected native stderr into errors, which "Stop" would throw.
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Exe @Arguments 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
  } finally {
    $ErrorActionPreference = $saved
  }
}

# --- Prerequisites -------------------------------------------------------------------

$Python = $null
$OldPython = $null
foreach ($candidate in @(@("python3.13"), @("python3.12"), @("python3.11"), @("python"), @("python3"), @("py", "-3"))) {
  if (-not (Test-Command $candidate[0])) { continue }
  $extra = @($candidate | Select-Object -Skip 1)
  # The version check also rejects the Windows Store "python" stub, which exits non-zero.
  if (Test-Native $candidate[0] ($extra + @("-c", "import sys; sys.exit(sys.version_info < (3, 11))"))) { $Python = $candidate; break }
  if (-not $OldPython -and (Test-Native $candidate[0] ($extra + @("-c", "pass")))) { $OldPython = $candidate }
}
if (-not $Python) {
  # An older Python can still run the doctor, which names the install command for this OS.
  if ($OldPython) {
    $arguments = @($OldPython | Select-Object -Skip 1) + @($Doctor, "--brief")
    & $OldPython[0] @arguments
  }
  if ($OnWindows) {
    Fail "Python 3.11 or later is required: 'winget install -e --id Python.Python.3.13', or https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'). If 'python' opens the Microsoft Store, turn that alias off in Settings > Apps > Advanced app settings > App execution aliases."
  }
  Fail "Python 3.11 or later is required: https://www.python.org/downloads/ (macOS: brew install python@3.13)."
}
$PythonArgs = @($Python | Select-Object -Skip 1)

if (-not $SkipChecks) {
  # Prints only problems: an old Node.js, no network to Azure, proxy or TLS inspection trouble.
  $arguments = $PythonArgs + @($Doctor, "--brief") + $(if ($Tui) { @("--tui") } else { @() })
  & $Python[0] @arguments
  if ($LASTEXITCODE -ne 0) { exit 1 }
}

# The browser UI: the Vite dev server with Node.js 22+, else the build the backend serves itself.
$UseVite = $false
if (-not $Tui) {
  $nodeMajor = 0
  if ((Test-Command "node") -and (Test-Command "npm")) {
    $nodeMajor = [int](& node -p "process.versions.node.split('.')[0]")
  }
  $UseVite = -not $Prebuilt -and (Test-Path (Join-Path $Frontend "package.json")) -and $nodeMajor -ge 22
  if (-not $UseVite -and -not (Test-Path $StaticIndex)) {
    Fail "The browser UI needs Node.js 22 or later to build (https://nodejs.org/), or the release package, which has it built in. Or use -Tui for the terminal UI."
  }
  if (-not $UseVite -and -not $Prebuilt) {
    if (Test-Path (Join-Path $Frontend "package.json")) { Step "Node.js 22+ not found: serving the prebuilt UI from backend/pool_manager/static" }
    else { Step "Serving the UI built into this package" }
  }
}

# --- Configuration -------------------------------------------------------------------

$EnvFile = Join-Path $Backend ".env"
if (-not (Test-Path $EnvFile)) {
  # Nothing to fill in: the app's setup page chooses or creates the APIM service.
  Copy-Item (Join-Path $Backend ".env.example") $EnvFile
  Step "Created backend/.env from .env.example"
}
function Get-Setting([string]$Name) {
  # Environment variables override .env, as in pydantic-settings.
  $fromEnvironment = [Environment]::GetEnvironmentVariable($Name)
  if ($fromEnvironment) { return $fromEnvironment }
  $line = Get-Content $EnvFile | Where-Object { $_ -match "^$Name=" } | Select-Object -Last 1
  if ($line) { return ($line -replace "^$Name=", "").Trim().Trim('"', "'") }
  return ""
}
$Subscription = Get-Setting "POOL_MANAGER_SUBSCRIPTION_ID"
$ResourceGroup = Get-Setting "POOL_MANAGER_RESOURCE_GROUP"
$ApimName = Get-Setting "POOL_MANAGER_APIM_NAME"
# The APIM service is optional here (the setup page chooses one); if given, give all of it.
$TargetValues = @($Subscription, $ResourceGroup, $ApimName)
if (($TargetValues | Where-Object { $_ }).Count -notin @(0, 3)) {
  Fail "Set all of POOL_MANAGER_SUBSCRIPTION_ID, POOL_MANAGER_RESOURCE_GROUP and POOL_MANAGER_APIM_NAME in backend/.env, or remove them to choose the service in the app."
}
if ($Subscription -eq "00000000-0000-0000-0000-000000000000" -or $ResourceGroup -eq "my-apim-rg" -or $ApimName -eq "my-apim") {
  Fail "backend/.env still has the example APIM values; set real ones or remove them to choose the service in the app."
}
# Locally there is no App Service Authentication, so easy_auth would answer 401 to everything.
if ((Get-Setting "POOL_MANAGER_AUTH_MODE") -ne "disabled") {
  Fail "Set POOL_MANAGER_AUTH_MODE=disabled in backend/.env for local development."
}

# --- Dependencies --------------------------------------------------------------------

$VenvPython = if ($OnWindows) { Join-Path $Backend ".venv\Scripts\python.exe" } else { Join-Path $Backend ".venv/bin/python" }
$TuiPython = if ($OnWindows) { Join-Path $TuiDir ".venv\Scripts\python.exe" } else { Join-Path $TuiDir ".venv/bin/python" }
$Npm = if ($OnWindows) { "npm.cmd" } else { "npm" }
# Vite runs through node rather than npm.cmd, whose cmd.exe asks "Terminate batch job?" on Ctrl+C.
$Vite = Join-Path $Frontend "node_modules/vite/bin/vite.js"

if (-not $SkipInstall) {
  if (-not (Test-Path $VenvPython)) {
    Step "Creating backend/.venv"
    & $Python[0] @PythonArgs -m venv (Join-Path $Backend ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "Could not create backend/.venv; 'python scripts/doctor.py' says why." }
  }
  $requirements = Join-Path $Backend "requirements.txt"
  $stamp = Join-Path $Backend ".venv/.requirements.sha256"
  $hash = (Get-FileHash $requirements -Algorithm SHA256).Hash
  if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
    Step "Installing backend requirements"
    & $VenvPython -m pip install --disable-pip-version-check --quiet -r $requirements
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed; 'python scripts/doctor.py' checks the network, proxy and TLS." }
    Set-Content -Path $stamp -Value $hash -NoNewline
  }
  if ($Tui) {
    if (-not (Test-Path $TuiPython)) {
      Step "Creating tui/.venv"
      & $Python[0] @PythonArgs -m venv (Join-Path $TuiDir ".venv")
      if ($LASTEXITCODE -ne 0) { Fail "Could not create tui/.venv." }
    }
    $tuiRequirements = Join-Path $TuiDir "requirements.txt"
    $tuiStamp = Join-Path $TuiDir ".venv/.requirements.sha256"
    $tuiHash = (Get-FileHash $tuiRequirements -Algorithm SHA256).Hash
    if (-not (Test-Path $tuiStamp) -or (Get-Content $tuiStamp -Raw).Trim() -ne $tuiHash) {
      Step "Installing TUI requirements"
      & $TuiPython -m pip install --disable-pip-version-check --quiet -r $tuiRequirements
      if ($LASTEXITCODE -ne 0) { Fail "pip install failed; 'python scripts/doctor.py' checks the network, proxy and TLS." }
      Set-Content -Path $tuiStamp -Value $tuiHash -NoNewline
    }
  }
  # npm ci writes node_modules/.package-lock.json; a newer lockfile means dependencies changed.
  $installed = Join-Path $Frontend "node_modules/.package-lock.json"
  $lockfile = Join-Path $Frontend "package-lock.json"
  if (-not $UseVite) {
    # The TUI, or the prebuilt UI: no frontend dependencies.
  } elseif (-not (Test-Path $installed) -or (Get-Item $lockfile).LastWriteTime -gt (Get-Item $installed).LastWriteTime) {
    Step "Installing frontend dependencies"
    Push-Location $Frontend
    try {
      & $Npm ci --no-audit --no-fund
      if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
    } finally {
      Pop-Location
    }
  }
} else {
  if (-not (Test-Path $VenvPython)) { Fail "backend/.venv is missing; run without -SkipInstall." }
  if ($Tui -and -not (Test-Path $TuiPython)) { Fail "tui/.venv is missing; run without -SkipInstall." }
  if ($UseVite -and -not (Test-Path $Vite)) { Fail "frontend/node_modules is missing; run without -SkipInstall." }
}

# --- Run -----------------------------------------------------------------------------

function Stop-Tree([System.Diagnostics.Process]$Process) {
  if (-not $Process -or $Process.HasExited) { return }
  # npm and uvicorn's reloader start child processes; stop the whole tree.
  if ($OnWindows) { Test-Native "taskkill" @("/T", "/F", "/PID", "$($Process.Id)") | Out-Null }
  else { Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue }
}
function Assert-PortFree([int]$Port, [string]$Option) {
  # Usually a server left over from an earlier run, so name the process that holds the port.
  if (Test-Command "Get-NetTCPConnection") {
    $owner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
      Select-Object -First 1 -ExpandProperty OwningProcess
    if (-not $owner) { return }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
    $command = if ($process -and $process.CommandLine) { $process.CommandLine } elseif ($process) { $process.Name } else { "unknown" }
    Fail "Port $Port is in use by PID $owner ($command). Stop it with 'taskkill /T /F /PID $owner', or pass $Option."
  }
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
  try { $listener.Start() } catch { Fail "Port $Port is in use; stop the process holding it, or pass $Option." }
  finally { $listener.Stop() }
}

Assert-PortFree $BackendPort "-BackendPort"
if ($UseVite) { Assert-PortFree $FrontendPort "-FrontendPort" }

$backendProcess = $null
$frontendProcess = $null
$previousProxyTarget = $env:API_PROXY_TARGET
try {
  Step "Starting backend on http://127.0.0.1:$BackendPort"
  $backendArguments = @(
    "-m", "uvicorn", "pool_manager.app:app",
    "--host", "127.0.0.1", "--port", "$BackendPort", "--reload", "--reload-dir", "pool_manager"
  )
  if ($Tui) {
    # Request logs would draw over the TUI; they go to files instead.
    $backendLog = Join-Path ([System.IO.Path]::GetTempPath()) "apim-backend-pool-backend.log"
    $backendProcess = Start-Process -FilePath $VenvPython -WorkingDirectory $Backend -NoNewWindow -PassThru `
      -RedirectStandardOutput $backendLog -RedirectStandardError "$backendLog.err" -ArgumentList $backendArguments
  } else {
    $backendProcess = Start-Process -FilePath $VenvPython -WorkingDirectory $Backend -NoNewWindow -PassThru -ArgumentList $backendArguments
  }

  $healthy = $false
  for ($i = 0; $i -lt 60 -and -not $healthy; $i++) {
    if ($backendProcess.HasExited) { Fail "The backend exited during startup (is port $BackendPort in use?)." }
    try {
      Invoke-WebRequest "http://127.0.0.1:$BackendPort/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
      $healthy = $true
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  if (-not $healthy) { Fail "The backend did not answer on http://127.0.0.1:$BackendPort/health within 30 seconds." }

  if ($Tui) {
    Step "Starting the terminal UI (backend log: $backendLog)"
    # pool_tui is a package in tui/, not installed into the venv, so run it from there.
    Push-Location $TuiDir
    try {
      & $TuiPython -m pool_tui --api "http://127.0.0.1:$BackendPort"
    } finally {
      Pop-Location
    }
    return
  }

  $AppUrl = "http://127.0.0.1:$BackendPort"
  if ($UseVite) {
    Step "Starting frontend on http://localhost:$FrontendPort"
    $env:API_PROXY_TARGET = "http://127.0.0.1:$BackendPort"
    # Same as `npm run dev`, with the port overridden.
    $frontendProcess = Start-Process -FilePath "node" -WorkingDirectory $Frontend -NoNewWindow -PassThru -ArgumentList @(
      "`"$Vite`"", "--port", "$FrontendPort", "--strictPort"
    )
    $AppUrl = "http://localhost:$FrontendPort"
  }

  Write-Host ""
  Write-Host "  App:     $AppUrl" -ForegroundColor White
  Write-Host "  API:     http://127.0.0.1:$BackendPort/api"
  if ($ApimName) {
    Write-Host "  APIM:    $ApimName (resource group $ResourceGroup). Changes are live." -ForegroundColor Yellow
  } else {
    Write-Host "  APIM:    chosen or created on the app's setup page. Changes are live." -ForegroundColor Yellow
  }
  Write-Host "  Press Ctrl+C to stop $(if ($UseVite) { 'both servers' } else { 'the server' })."
  Write-Host ""

  while (-not $backendProcess.HasExited -and -not ($frontendProcess -and $frontendProcess.HasExited)) {
    Start-Sleep -Milliseconds 500
  }
  if ($backendProcess.HasExited) { Fail "The backend stopped$(if ($UseVite) { '; stopping the frontend' })." }
  Fail "The frontend stopped (is port $FrontendPort in use?); stopping the backend."
} finally {
  # Runs on Ctrl+C too.
  Stop-Tree $frontendProcess
  Stop-Tree $backendProcess
  $env:API_PROXY_TARGET = $previousProxyTarget
}
