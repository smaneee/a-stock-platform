# 一键启动（迁移优先）：数据库迁移 -> 后端 -> 前端
# 用法: powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$PidDir = Join-Path $RootDir ".run"
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

Write-Host "=== A-stock 平台一键启动 ==="

# 1. 检测 Python launcher（优先 py -3.13/3.12/3.11）
function Get-PyLauncher {
    foreach ($ver in @("3.13", "3.12", "3.11")) {
        $cmd = Get-Command "py-$ver" -ErrorAction SilentlyContinue
        if ($cmd) {
            $versionOutput = & py -$ver --version 2>&1
            if ($versionOutput -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 11) {
                    Write-Host "使用 Python launcher py -$ver ($versionOutput)"
                    return "py-$ver"
                }
            }
        }
    }
    # 兜底：直接 python，要求 >=3.11
    try {
        $versionOutput = & python --version 2>&1
        if ($versionOutput -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                Write-Host "使用 python ($versionOutput)"
                return "python"
            }
        }
    } catch {
        Write-Error "无法检测到 Python 3.11+"
        exit 1
    }
    Write-Error "需要 Python >=3.11，当前未检测到合适版本"
    exit 1
}

$PyCmd = Get-PyLauncher

# 2. 后端虚拟环境
$venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "创建后端虚拟环境..."
    & $PyCmd -m venv (Join-Path $BackendDir ".venv")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "创建虚拟环境失败"
        exit 1
    }
    & $venvPython -m pip install `
        --disable-pip-version-check `
        --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" `
        -r (Join-Path $BackendDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install 失败（退出码 $LASTEXITCODE）"
        exit 1
    }
    Write-Host "依赖安装完成"
}

# 3. 端口检查
$backendPort = 8000
$frontendPort = 5173
function Test-PortInUse {
    param([int]$Port)
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $connections
}
if (Test-PortInUse $backendPort) {
    Write-Error "后端端口 $backendPort 已被占用，请先 stop_all.ps1 释放"
    exit 1
}
if (Test-PortInUse $frontendPort) {
    Write-Error "前端端口 $frontendPort 已被占用，请先 stop_all.ps1 释放"
    exit 1
}

# 4. 数据库迁移（启动前先迁移）
Write-Host "执行数据库迁移..."
Set-Location $BackendDir
& $venvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Error "alembic upgrade head 失败"
    Set-Location $RootDir
    exit 1
}
Set-Location $RootDir

# 5. 启动后端
$backendLog = Join-Path $PidDir "backend.log"
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError (Join-Path $PidDir "backend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "backend.pid") -Value $backend.Id
Write-Host "后端进程已启 PID=$($backend.Id)，等待就绪…"

# 6. 等待 /api/health/ready 返回 200
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
    # 进程还在跑？
    if ($backend.HasExited) {
        Write-Error "后端进程已退出（exit code $($backend.ExitCode)）"
        Get-Content $backendLog -Tail 30 | Write-Host
        exit 1
    }
    Start-Sleep -Seconds 1
}
if (-not $readyOk) {
    Write-Error "后端 60 秒内未通过 readiness 探针：$lastError"
    Write-Host "后端日志尾部："
    Get-Content $backendLog -Tail 30 | Write-Host
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "后端就绪：http://127.0.0.1:8000/api/health/ready"

# 7. 启动前端
$frontendLog = Join-Path $PidDir "frontend.log"
$npm = "npm.cmd"
$frontend = Start-Process -FilePath $npm `
    -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError (Join-Path $PidDir "frontend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "frontend.pid") -Value $frontend.Id
Write-Host "前端进程已启 PID=$($frontend.Id)，等待就绪…"

# 8. 等待前端 200
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
    Write-Error "前端 30 秒内未就绪"
    Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host ""
Write-Host "=== 启动成功 ==="
Write-Host "前端: http://127.0.0.1:5173"
Write-Host "后端: http://127.0.0.1:8000"
Write-Host "停止: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1"
