# Test backend script for A-stock platform
# Usage:  powershell -ExecutionPolicy Bypass -File scripts/test_backend.ps1
$ErrorActionPreference = "Stop"

$BackendDir = Join-Path (Split-Path -Parent $PSScriptRoot) "backend"
Set-Location $BackendDir

Write-Host "=== A-stock backend tests ==="

# Create venv if missing
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install `
        --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" `
        -r requirements.txt
}

Write-Host "Running pytest..."
& ".venv\Scripts\python.exe" -m pytest -v
