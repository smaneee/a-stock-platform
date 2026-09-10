# 一键启动（迁移优先）：数据库迁移 -> 后端 -> 前端
# 用法: powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$PidDir = Join-Path $RootDir ".run"
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

Write-Host "=== A-stock 平台一键启动 ==="

# 1. 后端虚拟环境
if (-not (Test-Path (Join-Path $BackendDir ".venv"))) {
    Write-Host "创建后端虚拟环境..."
    python -m venv (Join-Path $BackendDir ".venv")
    & (Join-Path $BackendDir ".venv\Scripts\python.exe") -m pip install `
        --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" `
        -r (Join-Path $BackendDir "requirements.txt")
}

# 2. 数据库迁移（启动前先迁移）
Write-Host "执行数据库迁移..."
Set-Location $BackendDir
& ".venv\Scripts\python.exe" -m alembic upgrade head
Set-Location $RootDir

# 3. 启动后端
$backendLog = Join-Path $PidDir "backend.log"
$backend = Start-Process -FilePath (Join-Path $BackendDir ".venv\Scripts\python.exe") `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError (Join-Path $PidDir "backend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "backend.pid") -Value $backend.Id
Write-Host "后端已启动 PID=$($backend.Id) http://127.0.0.1:8000"

# 4. 启动前端（vite dev）
$frontendLog = Join-Path $PidDir "frontend.log"
$npm = "npm.cmd"
$frontend = Start-Process -FilePath $npm `
    -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError (Join-Path $PidDir "frontend.err.log") `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $PidDir "frontend.pid") -Value $frontend.Id
Write-Host "前端已启动 PID=$($frontend.Id) http://127.0.0.1:5173"

Write-Host ""
Write-Host "访问 http://127.0.0.1:5173 使用界面"
Write-Host "停止: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1"
