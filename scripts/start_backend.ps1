# Start backend script for A-stock platform
# Usage:  powershell -ExecutionPolicy Bypass -File scripts/start_backend.ps1
$ErrorActionPreference = "Stop"

$BackendDir = Join-Path (Split-Path -Parent $PSScriptRoot) "backend"
Set-Location $BackendDir

Write-Host "=== A-stock backend startup ==="

# 1. Prepare .env
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example"
    } else {
        Write-Host "Warning: .env.example not found"
    }
}

# 2. 复用已有 venv 或按 >=3.11 解释器新建（含国内镜像安装依赖）
$venvPython = & (Join-Path $PSScriptRoot "ensure_backend_venv.ps1")
if ($LASTEXITCODE -ne 0 -or -not $venvPython -or -not (Test-Path $venvPython)) {
    Write-Error "backend 解释器不可用：ensure_backend_venv.ps1 未返回可用的 python.exe"
    exit 1
}
Write-Host "Interpreter: $venvPython"

# 3. Run database migrations
& $venvPython -m alembic upgrade head

# 4. Start server
Write-Host "Starting uvicorn on http://127.0.0.1:8000 ..."
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
