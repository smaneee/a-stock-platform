# Test backend script for A-stock platform
# Usage:  powershell -ExecutionPolicy Bypass -File scripts/test_backend.ps1
$ErrorActionPreference = "Stop"

$BackendDir = Join-Path (Split-Path -Parent $PSScriptRoot) "backend"
# 复用已有 venv 或按 >=3.11 解释器新建（含国内镜像安装依赖）
$venvPython = & (Join-Path $PSScriptRoot "ensure_backend_venv.ps1")
if ($LASTEXITCODE -ne 0 -or -not $venvPython -or -not (Test-Path $venvPython)) {
    Write-Error "backend 解释器不可用：ensure_backend_venv.ps1 未返回可用的 python.exe"
    exit 1
}
Set-Location $BackendDir

Write-Host "=== A-stock backend tests ==="
Write-Host "Interpreter: $venvPython"

Write-Host "Running pytest..."
& $venvPython -m pytest -v
