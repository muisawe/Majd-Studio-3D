# Exercises the launcher health check against a real venv on Windows (run by CI).
$ErrorActionPreference = "Stop"
$root = Join-Path $env:RUNNER_TEMP "majd-launcher"
New-Item -ItemType Directory -Force -Path $root | Out-Null
$env:MAJD_STUDIO_ROOT = $root
& python -m venv "$root\.venv"
if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }

. (Join-Path $PSScriptRoot "..\..\scripts\launch_windows.ps1")

# Stand-in studio started exactly like the launcher starts the real one.
$server = Start-Process -FilePath $pythonw -ArgumentList "-m http.server 7864 --bind 127.0.0.1" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 5
    Write-Host "Launcher process $($server.Id) exited: $($server.HasExited)"
    Write-Host "Process tree: $(@(Get-ProcessTreeIds $server.Id) -join ', ')"
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($server.Id)" |
        ForEach-Object { Write-Host "Child $($_.ProcessId): $($_.CommandLine)" }
    Get-NetTCPConnection -LocalPort 7864 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "Port 7864 $($_.State) owned by $($_.OwningProcess) ($($_.LocalAddress))" }
    try {
        $probe = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        Write-Host "HTTP $($probe.StatusCode)"
    } catch { Write-Host "HTTP probe failed: $_" }

    if (-not (Wait-For-Studio $server 60)) { throw "Wait-For-Studio did not recognise the running studio" }
    $owner = (Get-NetTCPConnection -LocalPort 7864 -State Listen | Select-Object -First 1).OwningProcess
    Write-Host "Launcher PID $($server.Id); listener PID $owner; redirector child: $($owner -ne $server.Id)"
} finally {
    Stop-ProcessTree $server
    if (Test-Path $logPath) { Get-Content $logPath | ForEach-Object { Write-Host "launcher.log: $_" } }
}
Start-Sleep -Seconds 2
if (Get-NetTCPConnection -LocalPort 7864 -State Listen -ErrorAction SilentlyContinue) {
    throw "Stop-ProcessTree left the studio listening"
}
Write-Host "Launcher health check OK"
