<#
.SYNOPSIS
  Toggles the TeleVault live archiver (main.py) on or off - standalone, no API server required.

.DESCRIPTION
  Desktop-shortcut convenience for local (Windows) development.
  Reads the same heartbeat file main.py itself writes (settings.heartbeat_path) to decide whether it's currently running:
    - If it IS running: stops it (kills the pid recorded in the heartbeat, then removes the heartbeat file).
    - If it's NOT running: launches `python main.py` in a new console window, exactly as if you'd typed it yourself from the project root.

  This deliberately talks to the heartbeat/status files directly rather than going through the API
  (see the earlier, API-based version of this script in git history) - the whole point is to work with nothing else running.

  Because of that, it also reimplements - rather than reuses - the same two checks api/process_utils.py's is_archiver_running() / is_backfill_running() perform,
  so a backfill-in-progress is caught here too, with an immediate, readable message, instead of relying on main.py's own guard
  (which would also catch it, but in a console window that can flash open and closed before you get a chance to read why).

.PARAMETER ProjectRoot
  Defaults to the parent of this script's own folder (scripts/..), i.e. the TeleVault project root.
  Override only if you've moved this script somewhere else.
#>

param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$HeartbeatPath = "data\televault.heartbeat",
    [string]$BackfillStatusPath = "data\backfill_status.json",
    [int]$StaleAfterSeconds = 60
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$heartbeatFile = Join-Path $ProjectRoot $HeartbeatPath
$backfillStatusFile = Join-Path $ProjectRoot $BackfillStatusPath

function Get-UnixTime {
    (Get-Date).ToUniversalTime().Subtract([datetime]"1970-01-01").TotalSeconds
}

function Test-ArchiverRunning {
    <#
    Returns the live pid if the heartbeat is fresh AND its process is actually still alive, or $null otherwise.
    Mirrors the self-healing shape of api/process_utils.py's is_archiver_running() - a stale heartbeat
    (crash, hard kill) must not be trusted just because the file exists.
    #>
    if (-not (Test-Path $heartbeatFile)) { return $null }
    try {
        $data = Get-Content $heartbeatFile -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
    if (-not $data.pid) { return $null }
    if ((Get-UnixTime) - $data.updated_at -ge $StaleAfterSeconds) { return $null }
    if (-not (Get-Process -Id $data.pid -ErrorAction SilentlyContinue)) { return $null }
    return $data.pid
}

function Test-BackfillRunning {
    <# Mirrors api/process_utils.py's is_backfill_running() the same way. #>
    if (-not (Test-Path $backfillStatusFile)) { return $false }
    try {
        $status = Get-Content $backfillStatusFile -Raw | ConvertFrom-Json
    } catch {
        return $false
    }
    if ($status.state -ne "running") { return $false }
    if (-not $status.pid) { return $false }
    return [bool](Get-Process -Id $status.pid -ErrorAction SilentlyContinue)
}

$runningPid = Test-ArchiverRunning

if ($runningPid) {
    Write-Host "Archiver is running (pid $runningPid) - stopping it..."
    Stop-Process -Id $runningPid -Force
    # main.py's own shutdown code (which deletes this file on a clean exit) never runs when killed like this - Windows has no real SIGTERM,
    # see main.py's own comments on this - so clean it up here ourselves, same reasoning as api/routes/telethon.py's stop_archiver().
    Remove-Item $heartbeatFile -ErrorAction SilentlyContinue
    Write-Host "Stopped." -ForegroundColor Green
    exit 0
}

# Not running - clear any stale leftover heartbeat before going further,
# so it can't confuse the web UI (or the next run of this script) while a fresh instance is about to start.
if (Test-Path $heartbeatFile) {
    Remove-Item $heartbeatFile -ErrorAction SilentlyContinue
}

if (Test-BackfillRunning) {
    Write-Host "A backfill is currently running. Stop it before starting the archiver -" -ForegroundColor Yellow
    Write-Host "Telethon sessions only support one active connection at a time." -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting archiver..."

# Use the project's own virtualenv interpreter if one exists at the conventional path (README's setup instructions create .venv here),
# rather than bare "python" - Start-Process resolves that via the system PATH,
# which is not guaranteed to be the same environment your own terminal has active when you type `python main.py` yourself.
# Running under the wrong interpreter (no telethon/sqlalchemy/etc. installed)
# produces a ModuleNotFoundError - falls back to bare "python" if no .venv is found, in case you're not using one.
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }

# Launched via `cmd /k` rather than the interpreter directly:
# Start-Process gives the launched command its own new console window,
# and that window closes itself the instant the process inside it exits - success OR crash.
# Without /k, a startup error flashes red text and the window is gone before it's readable.
# /k keeps the window open afterwards either way,
# until you close it yourself - matching the "closing the window manually works too" note below,
# which was already true for a successfully-running archiver, just not for one that fails immediately.
Start-Process -FilePath "cmd.exe" -ArgumentList "/k `"$pythonExe`" main.py" -WorkingDirectory $ProjectRoot | Out-Null
Write-Host "Started. A new console window should appear shortly - that window IS the archiver." -ForegroundColor Green
Write-Host "Running this shortcut again is the clean way to stop it (closing the window manually works too, but skips the heartbeat cleanup above)." -ForegroundColor Green