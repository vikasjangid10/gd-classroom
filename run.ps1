<#
.SYNOPSIS
    Start, check, reset and test the AI GD Classroom.

.DESCRIPTION
    One entry point, so none of the traps in this stack have to be remembered:

      * Docker Desktop is started and waited for, rather than failing with a pipe error;
      * `up` recreates the API container, because `docker compose restart` does NOT
        re-read .env — a changed setting would otherwise be silently ignored;
      * it waits for the API to report healthy instead of racing it;
      * `reset` closes discussions nobody ended, which otherwise hold their participants
        out of every new classroom until the janitor sweeps them.

.EXAMPLE
    .\run.ps1              # start everything and print where to go
    .\run.ps1 status       # what is running, and which AI providers are live
    .\run.ps1 reset        # free everyone up for a fresh discussion
    .\run.ps1 test         # the whole flow end to end, no browser needed
    .\run.ps1 logs         # follow the API log
    .\run.ps1 stop
#>
[CmdletBinding()]
param(
    [ValidateSet('up', 'status', 'reset', 'test', 'browser-test', 'logs', 'stop', 'down')]
    [string]$Command = 'up'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Say([string]$text, [string]$colour = 'White') { Write-Host $text -ForegroundColor $colour }
function Step([string]$text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Good([string]$text) { Write-Host "  OK    $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "  !     $text" -ForegroundColor Yellow }

function Test-Engine {
    docker version --format '{{.Server.Version}}' 2>$null | Out-Null
    return $?
}

function Start-Engine {
    if (Test-Engine) { return $true }

    Step 'Docker Desktop is not running — starting it'
    $exe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $exe)) {
        Warn "Docker Desktop not found at $exe. Start it yourself, then run this again."
        return $false
    }
    Start-Process $exe

    $deadline = (Get-Date).AddMinutes(4)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 8
        if (Test-Engine) { Good 'engine up'; return $true }
        Write-Host '  .' -NoNewline
    }
    Warn 'Docker did not come up in four minutes.'
    return $false
}

function Wait-Api {
    # The API applies migrations, seeds, and fetches the Piper voice on first boot, so
    # the first start of the day is legitimately slow.
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        $state = (docker compose ps --format '{{.Service}} {{.Status}}' 2>$null) -join "`n"
        if ($state -match 'api\s+Up.*healthy') { return $true }
        Start-Sleep -Seconds 5
        Write-Host '  .' -NoNewline
    }
    return $false
}

function Show-Ports {
    Step 'Running'
    $ports = @{ api = '8000  (API)'; web = '5173  (open this)'; db = '55432 (Postgres)' }
    docker compose ps --format '{{.Service}}|{{.Status}}' | ForEach-Object {
        $name, $state = $_ -split '\|', 2
        Write-Host ('  {0,-5} {1,-22} {2}' -f $name, $state, $ports[$name])
    }
}

function Show-Ready {
    $json = docker compose exec -T api curl -s http://localhost:8000/api/v1/readyz 2>$null
    if (-not $json) { Warn 'the API did not answer /readyz'; return }
    try { $r = $json | ConvertFrom-Json } catch { Warn "unreadable /readyz: $json"; return }

    $d = $r.data
    Step 'AI providers'
    Write-Host ("  mode          {0}" -f $d.ai_provider)
    foreach ($port in 'stt', 'llm', 'tts') {
        $ok = $d.checks.$port
        $colour = if ($ok) { 'Green' } else { 'Yellow' }
        Write-Host ("  {0,-13} {1}" -f $port, $(if ($ok) { 'ready' } else { 'NOT READY' })) -ForegroundColor $colour
    }
    Write-Host ("  live rooms    {0}" -f $d.live_sessions)
}

switch ($Command) {

    'up' {
        if (-not (Start-Engine)) { exit 1 }

        Step 'Starting the stack'
        # --force-recreate on the API: compose only reads .env when a container is
        # created, so a restart would keep serving yesterday's settings.
        docker compose up -d --force-recreate api | Out-Null
        docker compose up -d | Out-Null

        Step 'Waiting for the API (first run downloads a ~60 MB voice)'
        if (-not (Wait-Api)) {
            Warn 'the API never became healthy. Its log:'
            docker compose logs api --tail 40
            exit 1
        }
        Write-Host ''
        Good 'API healthy'

        Show-Ports
        Show-Ready

        Step 'Open this'
        Say '  http://localhost:5173' 'White'
        Write-Host ''
        Say '  Everyone signs in with the password  Password123!' 'Gray'
        Write-Host '    host          super@gdclassroom.io'
        Write-Host '    participants  priya@  arjun@  meera@  dev@  sana@  rahul@gdclassroom.io'
        Write-Host ''
        Say '  Each person needs their own browser profile: Chrome, Chrome incognito,' 'Gray'
        Say '  Edge, Edge InPrivate, Firefox. All incognito windows share one session.' 'Gray'
        Write-Host ''
        Say '  Sign 2 to 4 participants in and leave them on Invitations. As the host,' 'Gray'
        Say '  pick a topic and those people; the invitation appears with no reload.' 'Gray'
        Write-Host ''
        Say '  Stuck on "already in another live discussion"?   .\run.ps1 reset' 'DarkGray'
        Say '  No microphone? Join without one - you will still hear the host.' 'DarkGray'
    }

    'status' {
        if (-not (Test-Engine)) { Warn 'Docker is not running. Try:  .\run.ps1 up'; exit 1 }
        Show-Ports
        Show-Ready
    }

    'reset' {
        if (-not (Test-Engine)) { Warn 'Docker is not running. Try:  .\run.ps1 up'; exit 1 }
        Step 'Closing discussions nobody ended'
        docker compose exec -T api python -m scripts.reset_rooms
        Good 'everyone is free to be invited again'
    }

    'test' {
        if (-not (Test-Engine)) { Warn 'Docker is not running. Try:  .\run.ps1 up'; exit 1 }
        Step 'Running the whole flow over HTTP'
        Say '  create, invite, accept, discuss, summarise - with the real model.' 'Gray'
        Say '  Takes a few minutes: the host speaks at the speed of real speech.' 'DarkGray'
        docker compose exec -T api python -m scripts.e2e_demo
    }

    'browser-test' {
        if (-not (Test-Engine)) { Warn 'Docker is not running. Try:  .\run.ps1 up'; exit 1 }
        Step 'Driving five real browsers through the whole flow'
        Say '  Screenshots land in frontend/e2e/shots/' 'Gray'
        docker run --rm --shm-size=1g --network gd-classroom_default `
            -v "${PSScriptRoot}/frontend/e2e:/e2e" -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright `
            mcr.microsoft.com/playwright/python:v1.47.0-jammy `
            sh -c "pip install -q playwright==1.47.0 && python /e2e/ui_smoke.py"
    }

    'logs' { docker compose logs -f api }

    { $_ -in 'stop', 'down' } {
        Step 'Stopping'
        docker compose down
        Good 'stopped (your database and the Piper voice are kept)'
    }
}
