# start_all.ps1 - one-click launcher (migration first)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1
#
# Launcher priority:
#   1) py launcher (Get-Command py): try py -3.13 / py -3.12 / py -3.11
#   2) System python: requires >=3.11
#   3) Neither found: exit 1 (no silent fallback)
#
# Launcher command and version arg MUST be kept separate (never "py-3.12").
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$PidDir = Join-Path $RootDir ".run"
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

Write-Host "=== A-stock platform launcher ==="

# 1. Python launcher detection
function Test-PyLauncherExists {
    $cmd = Get-Command "py" -ErrorAction SilentlyContinue
    return $null -ne $cmd
}

function Get-PyVersionForLauncher {
    param([string]$Version)
    # Launcher (py) and version arg (-X.Y) are separate, never "py-X.Y"
    $out = & py "-$Version" --version 2>&1
    return ($out | Out-String).Trim()
}

function Resolve-PythonLauncher {
    if (Test-PyLauncherExists) {
        foreach ($ver in @("3.13", "3.12", "3.11")) {
            $verOutput = Get-PyVersionForLauncher $ver
            if ($verOutput -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 11) {
                    Write-Host "Using py launcher py -$ver ($verOutput)"
                    return @{ Launcher = "py"; Version = $ver }
                }
            }
        }
    }
    $cmd = Get-Command "python" -ErrorAction SilentlyContinue
    if ($cmd) {
        $verOutput = (& python --version 2>&1) | Out-String
        if ($verOutput -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                Write-Host ("Using system python ({0})" -f $verOutput.Trim())
                return @{ Launcher = "python"; Version = "" }
            }
        }
    }
    $hasLauncher = Test-PyLauncherExists
    $hasPython = $null -ne (Get-Command "python" -ErrorAction SilentlyContinue)
    Write-Error "Need Python >=3.11. No suitable launcher/py launcher/python found."
    Write-Error ("Detection: py launcher = {0}; python = {1}" -f $hasLauncher, $hasPython)
    exit 1
}

$PyInfo = Resolve-PythonLauncher
$PyLauncher = $PyInfo.Launcher
$PyVersionArg = if ($PyInfo.Version) { "-" + $PyInfo.Version } else { "" }

function Invoke-PyCommand {
    param([string[]]$PyArgs)
    if ($PyVersionArg) {
        & $PyLauncher $PyVersionArg @PyArgs
    } else {
        & $PyLauncher @PyArgs
    }
    return $LASTEXITCODE
}

# 2. Backend venv (reuse existing)
$venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "backend\.venv not found, creating..."
    $exitCode = Invoke-PyCommand -PyArgs @("-m", "venv", (Join-Path $BackendDir ".venv"))
    if ($exitCode -ne 0) {
        Write-Error "venv create failed (exit $exitCode)"
        exit 1
    }
    Write-Host "venv created, installing deps..."
    $exitCode = & $venvPython -m pip install `
        --disable-pip-version-check `
        --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" `
        -r (Join-Path $BackendDir "requirements.txt")
    if ($exitCode -ne 0) {
        Write-Error "pip install failed (exit $exitCode)"
        exit 1
    }
    Write-Host "deps installed"
} else {
    Write-Host "Reusing existing venv: $venvPython"
}

# 3. Port check
$backendPort = 8000
$frontendPort = 5173
function Test-PortInUse {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conns
}
if (Test-PortInUse $backendPort) {
    Write-Error "Backend port $backendPort is in use. Run stop_all.ps1 first."
    exit 1
}
if (Test-PortInUse $frontendPort) {
    Write-Error "Frontend port $frontendPort is in use. Run stop_all.ps1 first."
    exit 1
}

# 4. Database migration
Write-Host "Running alembic upgrade..."
Set-Location $BackendDir
& $venvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Error "alembic upgrade head failed"
    Set-Location $RootDir
    exit 1
}
Set-Location $RootDir

# 5. Start backend
$backendLog = Join-Path $PidDir "backend.log"
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError (Join-Path $PidDir "backend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "backend.pid") -Value $backend.Id
Write-Host "Backend PID=$($backend.Id), waiting for readiness..."

# 6. Wait for /api/health/ready
$readyOk = $false
$lastError = ""
for ($i = 1; $i -le 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health/ready" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            $readyOk = $true
            break
        }
    } catch {
        $lastError = $_.Exception.Message
    }
    if ($backend.HasExited) {
        Write-Error ("Backend exited (code {0})" -f $backend.ExitCode)
        Get-Content $backendLog -Tail 30 | Write-Host
        exit 1
    }
    Start-Sleep -Seconds 1
}
if (-not $readyOk) {
    Write-Error ("Backend not ready in 60s: {0}" -f $lastError)
    Get-Content $backendLog -Tail 30 | Write-Host
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "Backend ready: http://127.0.0.1:8000/api/health/ready"

# 7. Start frontend
$frontendLog = Join-Path $PidDir "frontend.log"
$npm = "npm.cmd"
$frontend = Start-Process -FilePath $npm `
    -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError (Join-Path $PidDir "frontend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "frontend.pid") -Value $frontend.Id
Write-Host "Frontend PID=$($frontend.Id), waiting for readiness..."

# 8. Wait for frontend
$frontendOk = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:5173" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            $frontendOk = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $frontendOk) {
    Write-Error "Frontend not ready in 30s"
    Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host ""
Write-Host "=== Launch OK ==="
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "Backend: http://127.0.0.1:8000"
Write-Host "Stop: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1"