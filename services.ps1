# Run: .\services.ps1 | .\services.ps1 -Stop | .\services.ps1 -Status
[CmdletBinding()]
param([switch]$Stop, [switch]$Status)

$ErrorActionPreference = 'Stop'
if ($Stop -and $Status) { throw 'Choose either -Stop or -Status.' }
$ProjectRoot = $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$VitePath = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
$LogRoot = Join-Path $ProjectRoot 'logs'
$CeleryApp = 'dataagent.engines.common.tasks.celery_app:celery_app'
$NodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$NodePath = if ($NodeCommand) { $NodeCommand.Source } else { '' }

$Services = @(
    @{ Name = 'embedding-server'; Module = 'scripts.serve_embedding'; Args = @('-m', 'scripts.serve_embedding'); Port = 7002; Url = 'http://127.0.0.1:7002/health'; Directory = $BackendRoot },
    @{ Name = 'backend-server'; Module = 'dataagent.main'; Args = @('-m', 'dataagent.main'); Port = 7000; Url = 'http://127.0.0.1:7000/openapi.json'; Directory = $BackendRoot },
    @{ Name = 'celery-worker'; Module = 'celery'; Role = 'worker'; Args = @('-m', 'celery', '--app', $CeleryApp, 'worker', '--pool=solo', '--concurrency=1', '-l', 'INFO'); Ready = 'celery@.+ ready\.'; Directory = $BackendRoot },
    @{ Name = 'celery-beat'; Module = 'celery'; Role = 'beat'; Args = @('-m', 'celery', '--app', $CeleryApp, 'beat', '-l', 'INFO', '--schedule', (Join-Path $ProjectRoot 'celerybeat-schedule')); Ready = 'beat: Starting'; Directory = $BackendRoot },
    @{ Name = 'frontend'; Args = @($VitePath, '--host', '127.0.0.1', '--port', '7001', '--strictPort'); Port = 7001; Url = 'http://127.0.0.1:7001/login'; Directory = $FrontendRoot }
)

function Get-ServiceRoots($Service) {
    # Match the project venv, not every Python process on this computer.
    @(Get-CimInstance Win32_Process | Where-Object {
        $command = ([string]$_.CommandLine).Replace('"', '')
        if ($Service.Name -eq 'frontend') {
            $_.Name -eq 'node.exe' -and
            $command.Replace('/', '\').Contains($FrontendRoot + '\') -and
            $command -match 'vite[\\/]bin[\\/]vite\.js'
        } else {
            $_.ExecutablePath -eq $PythonPath -and
            $command -match ('(?:^|\s)-m\s+' + [regex]::Escape($Service.Module) + '(?:\s|$)') -and
            ($Service.Module -ne 'celery' -or (
                $command.Contains($CeleryApp) -and $command -match ('\s' + $Service.Role + '(?:\s|$)')
            ))
        }
    })
}

function Stop-ServiceProcesses($Service) {
    $roots = @(Get-ServiceRoots $Service)
    if ($roots.Count -eq 0) { Write-Host "[stopped] $($Service.Name)"; return }
    $snapshot = @(Get-CimInstance Win32_Process)
    function Stop-ProjectTree($Process) {
        foreach ($child in $snapshot | Where-Object { $_.ParentProcessId -eq $Process.ProcessId -and $_.CreationDate -ge $Process.CreationDate }) {
            Stop-ProjectTree $child
        }
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.ProcessId)"
        if ($current -and $current.CreationDate -eq $Process.CreationDate) {
            Stop-Process -Id $current.ProcessId -Force -ErrorAction Stop
        }
    }
    foreach ($root in $roots) { Stop-ProjectTree $root }
    Write-Host "[stopped] $($Service.Name)"
}

function Wait-ServiceReady($Service) {
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-ServiceRoots $Service).Count -eq 0) {
            throw "$($Service.Name) exited. Check $LogRoot\$($Service.Name)*.log"
        }
        if ($Service.Url) {
            try {
                $response = Invoke-WebRequest -Uri $Service.Url -UseBasicParsing -TimeoutSec 3
                if ($response.StatusCode -eq 200) { return }
            } catch { }
        } else {
            foreach ($suffix in @('.log', '-error.log')) {
                $log = Join-Path $LogRoot ($Service.Name + $suffix)
                if ((Test-Path -LiteralPath $log) -and (Get-Content -LiteralPath $log -Tail 100 | Select-String -Pattern $Service.Ready)) { return }
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "$($Service.Name) was not ready within 180 seconds. Check $LogRoot."
}

if ($Status) {
    foreach ($service in $Services) {
        $roots = @(Get-ServiceRoots $service)
        $state = if ($roots.Count) { 'running' } else { 'stopped' }
        Write-Host ('[{0}] {1} {2}' -f $state, $service.Name, (($roots | ForEach-Object { $_.ProcessId }) -join ','))
    }
    exit 0
}
if ($Stop) {
    for ($index = $Services.Count - 1; $index -ge 0; $index--) { Stop-ServiceProcesses $Services[$index] }
    Write-Host 'Local services stopped. VM databases are managed separately.'
    exit 0
}

foreach ($path in @($PythonPath, $VitePath, (Join-Path $BackendRoot 'conf\.env'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing dependency/configuration: $path" }
}
if (-not $NodePath) { throw 'node.exe was not found in PATH.' }
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$started = [System.Collections.Generic.List[object]]::new()
$previousUtf8 = $env:PYTHONUTF8
try {
    $env:PYTHONUTF8 = '1'
    foreach ($service in $Services) {
        $roots = @(Get-ServiceRoots $service)
        if ($roots.Count -gt 1) { throw "Multiple instances of $($service.Name) found. Run -Stop first." }
        if ($roots.Count -eq 1) {
            Write-Host "[reuse] $($service.Name)"
            if ($service.Url) { Wait-ServiceReady $service }
            continue
        }
        if ($service.Port) {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $service.Port)
            try { $listener.Start() } catch { throw "Port $($service.Port) is occupied by another process." } finally { $listener.Stop() }
        }
        $executable = if ($service.Name -eq 'frontend') { $NodePath } else { $PythonPath }
        # Arguments are fixed switches or Windows file paths (which cannot contain quotes).
        $arguments = ($service.Args | ForEach-Object { '"' + $_ + '"' }) -join ' '
        $process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $service.Directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogRoot ($service.Name + '.log')) -RedirectStandardError (Join-Path $LogRoot ($service.Name + '-error.log'))
        $started.Add($service)
        Write-Host "[starting] $($service.Name) PID=$($process.Id)"
        Wait-ServiceReady $service
        Write-Host "[ready] $($service.Name)"
    }
    Write-Host "Ready: http://127.0.0.1:7001  Logs: $LogRoot"
    Write-Host 'Stop: .\services.ps1 -Stop'
} catch {
    for ($index = $started.Count - 1; $index -ge 0; $index--) { Stop-ServiceProcesses $started[$index] }
    throw
} finally {
    $env:PYTHONUTF8 = $previousUtf8
}
