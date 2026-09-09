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

# 2. Create virtual environment if missing
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

# 3. Install dependencies
Write-Host "Installing dependencies..."
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\pip.exe" install -r requirements.txt

# 4. Run database migration (optional, dev uses create_all)
# & ".venv\Scripts\alembic.exe" upgrade head

# 5. Start server
Write-Host "Starting uvicorn on http://127.0.0.1:8000 ..."
& ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
