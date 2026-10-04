# Isolated WaffleBench Omnigent diagnostic server. Does not touch ports 6767/6771.
[CmdletBinding()]
param([switch]$Setup,[switch]$Validate,[switch]$Server,[switch]$Live,[string]$CycleRoot='runs\discovery-cycle\cycle-20261004')
$ErrorActionPreference='Stop'
$Root=Split-Path -Parent $PSScriptRoot
$Cycle=[IO.Path]::GetFullPath((Join-Path $Root $CycleRoot))
$Runtime=Join-Path $Root '.discovery-live-runtime'
$Agent=Join-Path $Root 'discovery_agent'
$OmniPython=Join-Path $env:APPDATA 'uv\tools\omnigent\Scripts\python.exe'
$Python=Join-Path $Root '.venv\Scripts\python.exe'
$Url='http://127.0.0.1:6773'
New-Item -ItemType Directory -Force $Runtime | Out-Null
$env:OMNIGENT_DATA_DIR=$Runtime
$env:OMNIGENT_CONFIG_HOME=$Runtime
$env:OMNIGENT_NO_UPDATE_CHECK='1'
$env:OMNIGENT_HOST_NO_OPEN='1'
$env:OMNIGENT_DISABLE_TELEMETRY='1'
$env:DO_NOT_TRACK='1'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:ENABLE_CLAUDEAI_MCP_SERVERS='false'
$env:OMNIGENT_RUNNER_ENV_PASSTHROUGH='ENABLE_CLAUDEAI_MCP_SERVERS'
$NpmBin=Join-Path $env:APPDATA 'npm'
$env:PATH=(($env:PATH -split ';') | Where-Object {$_ -and $_.TrimEnd('\') -ne $NpmBin}) -join ';'
foreach($k in 'ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','OPENAI_API_KEY'){Remove-Item "Env:$k" -ErrorAction SilentlyContinue}
if($Setup){
  & $Python -c "from discovery_cycle import prepare; import sys,json; print(json.dumps(prepare(sys.argv[1],sys.argv[2])))" $Cycle (Join-Path $Root 'evidence\inspection-improvements-v3')
  if($LASTEXITCODE){throw 'Discovery prepare failed'}
  foreach($pair in @(@('','supervisor','read_context, finalize'),@('discovery_analyst','analyst','read_context, record_plan, record_update'),@('discovery_experimenter','experimenter','run_experiment'))){
    $Dir=if($pair[0]){Join-Path $Agent "agents\$($pair[0])\tools\mcp"}else{Join-Path $Agent 'tools\mcp'}
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $Py=$Python.Replace('\','/'); $Project=$Root.Replace('\','/'); $Target=$Cycle.Replace('\','/')
    $Text=@"
name: discovery
transport: stdio
command: "$Py"
args: ["-m", "discovery_cycle.mcp_server"]
env:
  DISCOVERY_CYCLE_ROOT: "$Target"
  DISCOVERY_CYCLE_ROLE: "$($pair[1])"
  PYTHONPATH: "$Project"
  PYTHONUTF8: "1"
timeout: 120
tools: [$($pair[2])]
"@
    [IO.File]::WriteAllText((Join-Path $Dir 'discovery.yaml'),$Text+"`n",[Text.UTF8Encoding]::new($false))
  }
}
if($Setup -or $Validate){
  & $OmniPython -c "from omnigent.spec import parse,validate; from pathlib import Path; import sys,json; s=parse(Path(sys.argv[1])); rows=[{'name':a.name,'valid':validate(a).valid,'errors':validate(a).errors} for a in [s,*s.sub_agents]]; print(json.dumps(rows)); assert all(r['valid'] for r in rows)" $Agent
  if($LASTEXITCODE){throw 'Discovery bundle validation failed'}
}
if($Server){
  if(Get-NetTCPConnection -LocalPort 6773 -State Listen -ErrorAction SilentlyContinue){throw 'Port6773 occupied; leaving it untouched'}
  $Exe=(Get-Command omni).Source
  $Srv=Start-Process -FilePath $Exe -ArgumentList @('server','--host','127.0.0.1','--port','6773','--agent',"`"$Agent`"") -WindowStyle Hidden -PassThru -RedirectStandardOutput "$Runtime\server.log" -RedirectStandardError "$Runtime\server.err"
  $Until=(Get-Date).AddSeconds(45)
  do{Start-Sleep -Milliseconds 500; $Healthy=(curl.exe -s "$Url/health") -match '"ok"'}until($Healthy -or (Get-Date) -gt $Until)
  if(-not $Healthy){throw 'Discovery server not healthy; see local runtime logs'}
  $HostProcess=Start-Process -FilePath $Exe -ArgumentList @('host','--server',$Url,'--no-open','--non-interactive') -WindowStyle Hidden -PassThru -RedirectStandardOutput "$Runtime\host.log" -RedirectStandardError "$Runtime\host.err"
  $Tracked=@($Srv,$HostProcess) | ForEach-Object { $Info=Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)"; @{pid=$_.Id;start_ticks=$_.StartTime.ToUniversalTime().Ticks;command=$Info.CommandLine;path=$_.Path} }
  [IO.File]::WriteAllText("$Runtime\owned-processes.json",(@{url=$Url;processes=@($Tracked)} | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
  Write-Host "Discovery server $Url (tracked locally)"
}
if($Live){
  & $OmniPython (Join-Path $Root 'scripts\run_discovery_cycle.py') --root $Cycle --server $Url
  if($LASTEXITCODE){throw 'Discovery cycle not verified; see preserved cycle records'}
}
