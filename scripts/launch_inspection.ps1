<#
.SYNOPSIS
  Inspection live launcher (Windows native, Omnigent 0.16.0). Separate from ..\launch.ps1.

.DESCRIPTION
  -Setup     prepare ONE fresh live root with the core API
             (prepare(root, campaign_root, seed=9800, budget=120, max_reviews=4)) using -Python,
             then render the two MCP sidecars (absolute interpreter/root paths, gitignored).
             Requires -CampaignRoot (verified frozen v3 campaign); -LiveRoot must not exist.
             Never runs uv sync: the existing interpreter's inspection dependencies are kept.
  -Validate  parse + validate inspection_agent\ with the installed Omnigent package
  -Server    start a hidden loopback server on port 6771 with inspection_agent\ plus a hidden host;
             both are PID-tracked with start time, executable and command line
  -Status    show the tracked processes and the 6771 health
  -Stop      stop ONLY the tracked processes whose identity still matches (PID reuse safe)
  -Live      one bounded supervisor session via scripts\run_inspection_live.py (coordinator-operated)
  -Recover   re-attach to -SessionId named by the live root's proof; creates and sends nothing

  All Omnigent state goes to .inspection-live-runtime\ inside this project. The existing
  server on port 6767, its runtime and other user sessions are never stopped or reconfigured;
  there is no global `omni stop`. API-key variables are removed from THIS process only, so
  the claude-sdk harness uses the existing Claude Code login.
#>
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Validate,
    [switch]$Server,
    [switch]$Status,
    [switch]$Stop,
    [switch]$Live,
    [switch]$Recover,
    [string]$SessionId = '',
    [string]$CampaignRoot = '',
    [string]$LiveRoot = 'runs\inspection-live\live-9800',
    [string]$Python = '',
    [string]$RuntimeDir = '.inspection-live-runtime'
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Port = 6771
$Url = "http://127.0.0.1:$Port"
$OmniVersion = '0.16.0'
$OmniPython = Join-Path $env:APPDATA 'uv\tools\omnigent\Scripts\python.exe'
$Agent = Join-Path $Root 'inspection_agent'
$Runner = Join-Path $Root 'scripts\run_inspection_live.py'
function Resolve-Abs([string]$p) { if ([IO.Path]::IsPathRooted($p)) { [IO.Path]::GetFullPath($p) } else { [IO.Path]::GetFullPath((Join-Path $Root $p)) } }
$LiveRootAbs = Resolve-Abs $LiveRoot
$Runtime = Resolve-Abs $RuntimeDir
$PidFile = Join-Path $Runtime 'inspection-pids.json'
# Only processes whose recorded command line targets this launcher's port count as ours.
$OwnMarker = "(--port`"?\s+`"?$Port\b|127\.0\.0\.1:$Port\b)"

# --- process-scoped environment (never persisted) ---------------------------
New-Item -ItemType Directory -Force $Runtime | Out-Null
$ignore = Join-Path $Runtime '.gitignore'
if (-not (Test-Path $ignore)) { [IO.File]::WriteAllText($ignore, "*`n", (New-Object Text.UTF8Encoding($false))) }
$env:OMNIGENT_DATA_DIR = $Runtime
$env:OMNIGENT_CONFIG_HOME = $Runtime
$env:OMNIGENT_NO_UPDATE_CHECK = '1'
$env:OMNIGENT_HOST_NO_OPEN = '1'
$env:OMNIGENT_DISABLE_TELEMETRY = '1'
$env:DO_NOT_TRACK = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Keep claude.ai account connectors out of the agents' Claude CLI tool surface.
$env:ENABLE_CLAUDEAI_MCP_SERVERS = 'false'
$env:OMNIGENT_RUNNER_ENV_PASSTHROUGH = 'ENABLE_CLAUDEAI_MCP_SERVERS'
# Hide the npm global bin (codex CLI) from Omnigent's native-Codex model probe.
$NpmBin = Join-Path $env:APPDATA 'npm'
$env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and ($_.TrimEnd('\') -ne $NpmBin) }) -join ';'
foreach ($k in 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY') {
    Remove-Item "Env:$k" -ErrorAction SilentlyContinue
}

function Assert-Omnigent {
    $v = (& omni --version) 2>&1 | Out-String
    if ($v -notmatch [regex]::Escape("omnigent $OmniVersion")) { throw "Expected omnigent $OmniVersion, got: $v" }
}

function Get-Identity([int]$Id) {
    $p = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    $c = Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue
    try { $ticks = $p.StartTime.ToUniversalTime().Ticks } catch { return $null }
    return [ordered]@{ pid = $Id; start_ticks = [int64]$ticks; path = [string]$p.Path; command = [string]$c.CommandLine }
}

function Test-Owned($Rec) {
    if (-not $Rec -or -not $Rec.pid) { return $false }
    $now = Get-Identity ([int]$Rec.pid)
    if (-not $now) { return $false }
    return ([int64]$now.start_ticks -eq [int64]$Rec.start_ticks) -and ($now.path -eq [string]$Rec.path) -and
           ($now.command -eq [string]$Rec.command) -and ([string]$Rec.command -match $OwnMarker)
}

function Read-Pids {
    if (-not (Test-Path $PidFile)) { return @() }
    $j = Get-Content $PidFile -Raw | ConvertFrom-Json
    return @($j.processes)
}

function Write-Pids($Recs) {
    $obj = [ordered]@{ url = $Url; processes = @($Recs) }
    [IO.File]::WriteAllText($PidFile, ($obj | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
}

function Stop-Owned {
    $left = @()
    foreach ($rec in (Read-Pids)) {
        if (Test-Owned $rec) {
            # The identity matches what -Server recorded: stop it and its own child processes.
            & taskkill.exe /PID ([int]$rec.pid) /T /F | Out-Null
            $until = (Get-Date).AddSeconds(10)
            while ((Get-Process -Id ([int]$rec.pid) -ErrorAction SilentlyContinue) -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 200 }
            Write-Host "stopped $($rec.role) pid $($rec.pid)"
        } elseif (Get-Process -Id ([int]$rec.pid) -ErrorAction SilentlyContinue) {
            Write-Host "pid $($rec.pid) ($($rec.role)) no longer matches the recorded identity; left untouched"
        } else {
            Write-Host "pid $($rec.pid) ($($rec.role)) already gone"
        }
        if (Test-Owned $rec) { $left += $rec }
    }
    if ($left.Count -eq 0) { Remove-Item $PidFile -ErrorAction SilentlyContinue } else { Write-Pids $left; throw 'some tracked processes are still running' }
}

function Write-Sidecar([string]$AgentName, [string]$Role, [string[]]$Tools, [string]$Py) {
    $dir = Join-Path $Agent "agents\$AgentName\tools\mcp"
    New-Item -ItemType Directory -Force $dir | Out-Null
    $f = { param($p) $p -replace '\\', '/' }
    $yaml = @"
# GENERATED by scripts/launch_inspection.ps1 -Setup (absolute local paths; gitignored, never committed).
name: inspection
transport: stdio
command: "$(& $f $Py)"
args: ["-m", "inspection_live.mcp_server"]
env:
  INSPECTION_LIVE_ROOT: "$(& $f $LiveRootAbs)"
  INSPECTION_MCP_ROLE: "$Role"
  PYTHONPATH: "$(& $f $Root)"
  PYTHONUTF8: "1"
timeout: 120
tools: [$($Tools -join ', ')]
"@
    [IO.File]::WriteAllText((Join-Path $dir 'inspection.yaml'), $yaml + "`n", (New-Object Text.UTF8Encoding($false)))
}

if ($Setup) {
    if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
    if (-not (Test-Path $Python)) { throw "interpreter $Python not found; pass -Python <existing venv python with the inspection dependencies>" }
    if (-not $CampaignRoot) { throw 'pass -CampaignRoot <verified frozen v3 campaign>' }
    if (Test-Path $LiveRootAbs) { throw "live root $LiveRootAbs exists; prepare requires a fresh root" }
    $PyAbs = (Resolve-Path $Python).Path
    $env:PYTHONPATH = $Root
    $code = 'import json, sys; from inspection_live import prepare; print(json.dumps(prepare(sys.argv[1], sys.argv[2], seed=9800, budget=120, max_reviews=4)))'
    New-Item -ItemType Directory -Force (Split-Path -Parent $LiveRootAbs) | Out-Null
    Push-Location $Root
    try { & $PyAbs -c $code $LiveRootAbs (Resolve-Abs $CampaignRoot); if ($LASTEXITCODE) { throw 'core prepare failed; nothing rendered' } }
    finally { Pop-Location }
    Write-Sidecar 'inspection_analyst' 'analyst' @('analyze_and_plan', 'read_result') $PyAbs
    Write-Sidecar 'inspection_experimenter' 'experimenter' @('review_site', 'read_result') $PyAbs
    Write-Host "Setup done. Live root $LiveRootAbs; sidecars under inspection_agent\agents\*\tools\mcp\inspection.yaml. Restart -Server to load them."
}

if ($Validate) {
    Assert-Omnigent
    & $OmniPython $Runner --validate-bundle
    if ($LASTEXITCODE) { throw 'inspection agent validation failed' }
}

if ($Server) {
    Assert-Omnigent
    if (@(Read-Pids | Where-Object { Test-Owned $_ }).Count -gt 0) { throw "tracked inspection server already running; use -Stop first" }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "port $Port is already in use by another process; not touching it" }
    foreach ($a in 'inspection_analyst', 'inspection_experimenter') {
        if (-not (Test-Path (Join-Path $Agent "agents\$a\tools\mcp\inspection.yaml"))) { throw "$a sidecar missing; run -Setup first" }
    }
    $omniExe = (Get-Command omni).Source
    $recs = @()
    $srvLog = Join-Path $Runtime 'server.log'
    $s = Start-Process -FilePath $omniExe -WindowStyle Hidden -PassThru `
        -ArgumentList @('server', '--host', '127.0.0.1', '--port', "$Port", '--agent', "`"$Agent`"") `
        -RedirectStandardOutput $srvLog -RedirectStandardError "$srvLog.err"
    Start-Sleep -Milliseconds 500
    $id = Get-Identity $s.Id
    if (-not $id) { throw "server exited immediately; see $srvLog(.err)" }
    $id.role = 'server'; $recs += $id; Write-Pids $recs
    $deadline = (Get-Date).AddSeconds(90)
    do { Start-Sleep -Seconds 2; $ok = (curl.exe -s "$Url/health") -match '"ok"' } until ($ok -or (Get-Date) -gt $deadline)
    if (-not $ok) { Stop-Owned; throw "server did not become healthy; see $srvLog(.err)" }
    $hostLog = Join-Path $Runtime 'host.log'
    $h = Start-Process -FilePath $omniExe -WindowStyle Hidden -PassThru `
        -ArgumentList @('host', '--server', $Url, '--no-open', '--non-interactive') `
        -RedirectStandardOutput $hostLog -RedirectStandardError "$hostLog.err"
    Start-Sleep -Milliseconds 500
    $hid = Get-Identity $h.Id
    if (-not $hid) { Stop-Owned; throw "host exited immediately; see $hostLog(.err)" }
    $hid.role = 'host'; $recs += $hid; Write-Pids $recs
    Write-Host "UI $Url  server pid $($s.Id)  host pid $($h.Id)  (tracked in $PidFile)"
}

if ($Status) {
    foreach ($rec in (Read-Pids)) { Write-Host "$($rec.role) pid $($rec.pid) owned=$(Test-Owned $rec)" }
    $health = (curl.exe -s "$Url/health") 2>$null
    Write-Host "health $Url : $health"
}

if ($Stop) { Stop-Owned }

if ($Live) {
    Assert-Omnigent
    & $OmniPython $Runner --server $Url --live-root $LiveRootAbs
    if ($LASTEXITCODE) { throw "live session did not complete (exit $LASTEXITCODE); see $LiveRootAbs\omnigent\session-proof.json" }
}

if ($Recover) {
    Assert-Omnigent
    if (-not $SessionId) { throw 'pass -SessionId <id from the live root proof>' }
    & $OmniPython $Runner --server $Url --live-root $LiveRootAbs --recover-session $SessionId
    if ($LASTEXITCODE) { throw "recovery did not complete (exit $LASTEXITCODE)" }
}
