$ErrorActionPreference = "Stop"
$project = "E:\AI\Hunyuan3D-2.1"
$python = "$project\.venv\Scripts\python.exe"
$pythonw = "$project\.venv\Scripts\pythonw.exe"
$app = "$project\majd_studio_3d_v9.py"
$updater = "$project\majd_studio_3d\updater.py"
$pending = "$project\majd_v9\updates\pending.json"
$url = "http://127.0.0.1:7864"
$logDir = "$project\majd_v9\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "launcher.log"

function Log-Message([string]$message) {
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) $message"
}

function Wait-For-Studio([System.Diagnostics.Process]$process) {
    for ($i = 0; $i -lt 150; $i++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) { return $false }
        try {
            $listener = Get-NetTCPConnection -LocalPort 7864 -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.OwningProcess -eq $process.Id }
            if ($listener) {
                $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
                if ($response.StatusCode -eq 200) { return $true }
            }
        } catch {}
    }
    return $false
}

function Start-MajdStudio {
    $running = Get-NetTCPConnection -LocalPort 7864 -State Listen -ErrorAction SilentlyContinue
    if ($running) {
        Start-Process $url
        return 0
    }

    # A previous launch may have ended before it could confirm an update.
    if (Test-Path $pending) {
        & $python $updater rollback | ForEach-Object { Log-Message "Updater: $_" }
        if ($LASTEXITCODE -ne 0) {
            Log-Message "Could not roll back unconfirmed update"
            return 1
        }
    }

    $updateStatus = 0
    if (Test-Path $updater) {
        try {
            $updateOutput = & $python $updater check 2>&1
            foreach ($line in $updateOutput) { Log-Message "Updater: $line" }
            $updateStatus = $LASTEXITCODE
            if ($updateStatus -ne 0) { Log-Message "Update check failed; starting installed version" }
        } catch {
            $updateStatus = 1
            Log-Message "Update check failed: $_"
        }
    }
    if ((Test-Path $pending) -and $updateStatus -ne 0) {
        & $python $updater rollback | ForEach-Object { Log-Message "Updater: $_" }
        if ($LASTEXITCODE -ne 0) {
            Log-Message "Could not roll back incomplete update"
            return 1
        }
    }

    $process = Start-Process -FilePath $pythonw -ArgumentList "`"$app`"" -WorkingDirectory $project -WindowStyle Hidden -PassThru
    $healthy = Wait-For-Studio $process
    if ($healthy -and (Test-Path $pending)) {
        & $python $updater confirm | ForEach-Object { Log-Message "Updater: $_" }
        if ($LASTEXITCODE -ne 0 -or (Test-Path $pending)) {
            Log-Message "Update confirmation failed; rolling back application files"
            $healthy = $false
        }
    }
    if ($healthy) {
        Start-Process $url
        return 0
    }

    Log-Message "Studio failed to start or confirm update"
    if (Test-Path $pending) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        & $python $updater rollback | ForEach-Object { Log-Message "Updater: $_" }
        if ($LASTEXITCODE -eq 0) {
            Log-Message "Rolled back failed update"
            $process = Start-Process -FilePath $pythonw -ArgumentList "`"$app`"" -WorkingDirectory $project -WindowStyle Hidden -PassThru
            if (Wait-For-Studio $process) {
                Start-Process $url
                return 0
            }
        }
    }
    Log-Message "Studio remains unavailable; check majd_v9\logs\v9.log"
    return 1
}

$launchMutex = [System.Threading.Mutex]::new($false, "Local\MajdStudio3DLaunch")
$ownsMutex = $false
$status = 1
try {
    try {
        $ownsMutex = $launchMutex.WaitOne(360000)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if ($ownsMutex) {
        $status = Start-MajdStudio
    } else {
        Log-Message "Timed out waiting for another Majd Studio launch"
    }
} catch {
    Log-Message "Launcher error: $_"
} finally {
    if ($ownsMutex) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
exit $status
