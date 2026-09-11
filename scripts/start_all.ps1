# start_all.ps1 - one-click launcher (migration first)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1
#
# 解释器与 venv 由 ensure_backend_venv.ps1 统一处理（与 start_backend.ps1 /
# test_backend.ps1 共用）：
#   1) 复用已有 venv：.venv → .venv-311 → .venv-312
#   2) 都没有时用 py 启动器（3.13 → 3.12 → 3.11）或 PATH 上 >=3.11 的 python 新建 .venv
#   3) 新建时从清华 PyPI 镜像安装 requirements.txt
# 找不到 >=3.11 解释器时明确失败，不用旧版本静默建出坏环境。
# 这里不再自带一套探测逻辑，否则本机已有 .venv-311 时还会另建一个 .venv
# 把全部依赖重复下载一遍。
#
# 前端 npm 解析顺序：NPM_PATH → PATH → 常见安装位置（Program Files\nodejs、
# nvm-windows、Volta、~\.workbuddy\binaries\node）；frontend\node_modules
# 缺失时用 registry.npmmirror.com 自动安装，无需手动执行 npm install。
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$PidDir = Join-Path $RootDir ".run"
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

Write-Host "=== A-stock platform launcher ==="

# 1. Backend venv（复用/新建 + 国内镜像装依赖）
$venvPython = & (Join-Path $PSScriptRoot "ensure_backend_venv.ps1")
if ($LASTEXITCODE -ne 0 -or -not $venvPython -or -not (Test-Path $venvPython)) {
    Write-Error "backend 解释器不可用：ensure_backend_venv.ps1 未返回可用的 python.exe"
    exit 1
}
Write-Host "Interpreter: $venvPython"

# 2. Port check
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

# 3. Database migration
Write-Host "Running alembic upgrade..."
Set-Location $BackendDir
& $venvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Error "alembic upgrade head failed"
    Write-Host "提示：若 backend\a_stock.db 是早期遗留库（表结构已是最新但 alembic_version 落后），" -ForegroundColor Yellow
    Write-Host "可先用 scripts\db_backup.py 备份，再删除该文件后重跑本脚本，让迁移从零建表。" -ForegroundColor Yellow
    Set-Location $RootDir
    exit 1
}
Set-Location $RootDir

# 4. Start backend
$backendLog = Join-Path $PidDir "backend.log"
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError (Join-Path $PidDir "backend.err.log") `
    -WindowStyle Hidden -PassThru -ErrorAction Stop
if (-not $backend -or -not $backend.Id) {
    Write-Error "后端进程启动失败（Start-Process 未返回进程句柄）"
    exit 1
}
Set-Content -Path (Join-Path $PidDir "backend.pid") -Value $backend.Id
Write-Host "Backend PID=$($backend.Id), waiting for readiness..."

# 5. Wait for /api/health/ready
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

# 6. Resolve npm（NPM_PATH → PATH → 常见安装位置）
function Resolve-NpmPath {
    if ($env:NPM_PATH -and (Test-Path $env:NPM_PATH)) {
        return (Resolve-Path $env:NPM_PATH).Path
    }
    foreach ($name in @("npm.cmd", "npm")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    $direct = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) { $direct += (Join-Path $base "nodejs\npm.cmd") }
    }
    if ($env:LOCALAPPDATA) {
        $direct += (Join-Path $env:LOCALAPPDATA "Programs\nodejs\npm.cmd")
        $direct += (Join-Path $env:LOCALAPPDATA "Volta\bin\npm.cmd")
    }
    if ($env:APPDATA) {
        $direct += (Join-Path $env:APPDATA "npm\npm.cmd")
    }
    $patterns = @((Join-Path $HOME ".workbuddy\binaries\node\versions\*\npm.cmd"))
    if ($env:APPDATA) { $patterns += (Join-Path $env:APPDATA "nvm\*\npm.cmd") }
    $direct += @(
        Get-Item $patterns -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $_.FullName }
    )
    foreach ($candidate in $direct) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    return $null
}

function Stop-BackendProcess {
    if ($backend) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item (Join-Path $PidDir "backend.pid") -Force -ErrorAction SilentlyContinue
}

$npm = Resolve-NpmPath
if (-not $npm) {
    Write-Error "未找到 npm。请安装 Node.js >= 18，或把 node 目录加入 PATH，或用 NPM_PATH 指定 npm.cmd 路径。"
    Stop-BackendProcess
    exit 1
}
Write-Host "npm: $npm"
# npm.cmd 依赖同目录的 node.exe，vite 子进程也需要能在 PATH 上找到 node
$env:PATH = "$(Split-Path -Parent $npm);$env:PATH"

# 7. Frontend deps（缺失时用国内镜像安装）
if (-not (Test-Path (Join-Path $FrontendDir "node_modules\vite"))) {
    Write-Host "frontend\node_modules 缺失，使用 registry.npmmirror.com 安装依赖..."
    Push-Location $FrontendDir
    & $npm install --registry=https://registry.npmmirror.com --no-audit --no-fund
    $installExit = $LASTEXITCODE
    Pop-Location
    if ($installExit -ne 0) {
        Write-Error "npm install 失败 (exit $installExit)"
        Stop-BackendProcess
        exit 1
    }
}

# 8. Start frontend
$frontendLog = Join-Path $PidDir "frontend.log"
$frontendErrLog = Join-Path $PidDir "frontend.err.log"
$frontend = Start-Process -FilePath $npm `
    -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError $frontendErrLog `
    -WindowStyle Hidden -PassThru -ErrorAction Stop
if (-not $frontend -or -not $frontend.Id) {
    Write-Error "前端进程启动失败（Start-Process 未返回进程句柄）"
    Stop-BackendProcess
    exit 1
}
Set-Content -Path (Join-Path $PidDir "frontend.pid") -Value $frontend.Id
Write-Host "Frontend PID=$($frontend.Id), waiting for readiness..."

# 9. Wait for frontend
$frontendOk = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:5173" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            $frontendOk = $true
            break
        }
    } catch {}
    if ($frontend.HasExited) {
        Write-Error ("Frontend exited (code {0})" -f $frontend.ExitCode)
        Get-Content $frontendLog -Tail 30 -ErrorAction SilentlyContinue | Write-Host
        Get-Content $frontendErrLog -Tail 30 -ErrorAction SilentlyContinue | Write-Host
        Stop-BackendProcess
        exit 1
    }
    Start-Sleep -Seconds 1
}
if (-not $frontendOk) {
    Write-Error "Frontend not ready in 30s"
    Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue
    Get-Content $frontendLog -Tail 30 -ErrorAction SilentlyContinue | Write-Host
    Get-Content $frontendErrLog -Tail 30 -ErrorAction SilentlyContinue | Write-Host
    Stop-BackendProcess
    exit 1
}

Write-Host ""
Write-Host "=== Launch OK ==="
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "Backend: http://127.0.0.1:8000"
Write-Host "Stop: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1"
