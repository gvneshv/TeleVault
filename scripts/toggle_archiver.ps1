<#
.SYNOPSIS
  Toggles the TeleVault live archiver (main.py) on or off.

.DESCRIPTION
  Convenience shortcut for local (Windows) development:
  checks GET /api/telethon/status, then calls whichever of POST /api/telethon/start or POST /api/telethon/stop applies.

  Deliberately a thin client of the existing API rather than a reimplementation of process management - all the actual logic
  (single-instance guard, the "refuse to start while a backfill is running" check, heartbeat handling, etc.) already lives in api/routes/telethon.py and stays there.
  This script just calls it, so it can never drift out of sync with the real rules.

  Requires the API server to already be running - it's a client of it, not a replacement for it.
  Start it first if needed:
      uvicorn api.server:app --host 127.0.0.1 --port 8000

.PARAMETER BaseUrl
  Base URL of the running API server.
  Defaults to the local-dev address used throughout the README (http://127.0.0.1:8000).
  Override if you're running the API on a different port, or tunnelling to the VPS.

.EXAMPLE
  .\toggle_archiver.ps1
  .\toggle_archiver.ps1 -BaseUrl "http://127.0.0.1:9000"
#>

param(
    [string]$BaseUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

# Pulls the human-readable "message" field out of the API's own error payload (see api/routes/telethon.py's HTTPException bodies) when available,
# falling back to the raw response text if the shape is ever unexpected
# - better than surfacing a bare stack trace for something like "a backfill is currently running".
function Get-FriendlyError($errorRecord) {
    $raw = $errorRecord.ErrorDetails.Message
    if (-not $raw) { return $errorRecord.Exception.Message }
    try {
        $parsed = $raw | ConvertFrom-Json
        if ($parsed.detail.message) { return $parsed.detail.message }
    } catch {
        # Not JSON, or not shaped as expected - fall through to raw text.
    }
    return $raw
}

try {
    $status = Invoke-RestMethod -Uri "$BaseUrl/api/telethon/status" -Method Get
} catch {
    Write-Host "Could not reach the API server at $BaseUrl." -ForegroundColor Red
    Write-Host "Is it running? (uvicorn api.server:app --host 127.0.0.1 --port 8000)" -ForegroundColor Red
    exit 1
}

if ($status.running) {
    Write-Host "Archiver is running - stopping it..."
    try {
        Invoke-RestMethod -Uri "$BaseUrl/api/telethon/stop" -Method Post | Out-Null
        Write-Host "Stopped." -ForegroundColor Green
    } catch {
        Write-Host "Failed to stop: $(Get-FriendlyError $_)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Archiver is not running - starting it..."
    try {
        Invoke-RestMethod -Uri "$BaseUrl/api/telethon/start" -Method Post | Out-Null
        Write-Host "Started." -ForegroundColor Green
    } catch {
        # Most likely reason this fails: a backfill is currently running - see api/routes/telethon.py's start_archiver() for the exact guard.
        Write-Host "Failed to start: $(Get-FriendlyError $_)" -ForegroundColor Red
        exit 1
    }
}
