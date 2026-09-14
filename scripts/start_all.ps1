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
#
# -Lan：前端改为监听 0.0.0.0，手机/平板在同一 Wi-Fi 下用 http://<本机IPv4>:5173
# 访问（默认仍只监听 127.0.0.1）。局域网模式下会强制开启访问鉴权，访问口令写在
# .run\lan-access.txt（本机回环访问免密，手机需要输入该口令）。
#
# -Prod：正式部署模式。不再启动 Vite 开发服务，改由后端直接托管 frontend\dist
# 静态产物（SERVE_STATIC=true），只占用 8000 一个端口。缺 dist 时会自动构建。
# 与 -Lan 组合即为「手机访问正式版」，此时后端监听 0.0.0.0 且强制鉴权。
param(
    [switch]$Lan,
    [switch]$Prod
)

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
# 证据化：把解释器实际版本与 base prefix 打出来（启动器诊断面板会显示）
try {
    $pyInfo = & $venvPython -c "import sys;print(sys.version.split()[0]);print(sys.base_prefix)" 2>&1
    Write-Host ("Python: {0} (base {1})" -f $pyInfo[0], $pyInfo[1])
} catch {
    Write-Host "警告：无法读取解释器版本信息" -ForegroundColor Yellow
}

# 2. Port check
$backendPort = 8000
$frontendPort = 5173
$bindHost = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
function Test-PortInUse {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conns
}
function Get-LanIPv4 {
    # 取第一个非回环、非链路本地的 IPv4，用于打印手机可访问的地址
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.AddressState -eq "Preferred"
        }
    if ($ips) { return ($ips | Select-Object -First 1).IPAddress }
    return $null
}
if (Test-PortInUse $backendPort) {
    Write-Error "Backend port $backendPort is in use. Run stop_all.ps1 first."
    exit 1
}
if (-not $Prod -and (Test-PortInUse $frontendPort)) {
    Write-Error "Frontend port $frontendPort is in use. Run stop_all.ps1 first."
    exit 1
}

# ── 访问控制（P0-04）：局域网访问必须先有口令，否则拒绝启动 ──
# 后端在非回环监听 + REQUIRE_AUTH=false 时会直接抛错；这里提前生成口令，
# 避免用户双击后看到「启动失败」却不知道原因。
$lanAccessFile = Join-Path $PidDir "lan-access.txt"
$accessPassword = $env:ACCESS_PASSWORD
if ($Lan) {
    if (-not $accessPassword) {
        if (Test-Path $lanAccessFile) {
            $accessPassword = (Get-Content -LiteralPath $lanAccessFile -Encoding UTF8 | Select-Object -First 1).Trim()
        }
        if (-not $accessPassword) {
            # 32 位十六进制随机口令：只写本机 .run 目录，不进仓库、不进日志正文
            $bytes = New-Object byte[] 16
            [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
            $accessPassword = -join ($bytes | ForEach-Object { $_.ToString("x2") })
            Set-Content -LiteralPath $lanAccessFile -Value $accessPassword -Encoding UTF8
        }
    }
    $env:ACCESS_PASSWORD = $accessPassword
    $env:REQUIRE_AUTH = "true"
    if (-not $env:SESSION_SECRET) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $env:SESSION_SECRET = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    }
    Write-Host "局域网访问鉴权: 已开启（口令见 $lanAccessFile）" -ForegroundColor Yellow
}
$env:HOST = $bindHost
if ($Prod) {
    $env:SERVE_STATIC = "true"
    Write-Host "正式部署模式: 后端托管 frontend\dist，前端端口与后端合并为 $backendPort"
}

function Resolve-NpmPath {
    # 解析顺序（每一条都可被 NPM_PATH 覆盖）：
    #   1) 工作区自备独立 Node：E:\workbuddy work\runtime\node\<版本>\npm.cmd
    #      —— 为「将来卸载 WorkBuddy 后仍能构建前端」预留，不依赖任何私有运行时
    #   2) WorkBuddy 自带 Node（本机当前唯一可用版本 22.22.2）
    #   3) PATH
    #   4) 常见安装位置（Program Files\nodejs、nvm、Volta、%APPDATA%\npm）
    if ($env:NPM_PATH -and (Test-Path $env:NPM_PATH)) {
        return (Resolve-Path $env:NPM_PATH).Path
    }
    $patterns = @()
    $workspaceRuntime = Join-Path (Split-Path -Parent $RootDir) "runtime\node"
    $patterns += (Join-Path $workspaceRuntime "*\npm.cmd")
    if ($env:NODE_HOME) { $patterns += (Join-Path $env:NODE_HOME "npm.cmd") }
    $patterns += (Join-Path $HOME ".workbuddy\binaries\node\versions\*\npm.cmd")
    if ($env:APPDATA) { $patterns += (Join-Path $env:APPDATA "nvm\*\npm.cmd") }
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
    $direct += @(
        Get-Item $patterns -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $_.FullName }
    )
    foreach ($name in @("npm.cmd", "npm")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    foreach ($candidate in $direct) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    return $null
}

# 2.5 正式部署：确保前端静态产物存在（缺失则用国内镜像构建，不改变依赖版本）
if ($Prod) {
    $distIndex = Join-Path $FrontendDir "dist\index.html"
    if (-not (Test-Path $distIndex)) {
        Write-Host "frontend\dist 缺失，开始构建正式静态产物..."
        $npmBuild = Resolve-NpmPath
        if (-not $npmBuild) {
            Write-Error "未找到 npm，无法构建前端静态产物。请安装 Node.js >= 18 或用 NPM_PATH 指定。"
            exit 1
        }
        $env:PATH = "$(Split-Path -Parent $npmBuild);$env:PATH"
        if (-not (Test-Path (Join-Path $FrontendDir "node_modules\vite"))) {
            Push-Location $FrontendDir
            & $npmBuild install --registry=https://registry.npmmirror.com --no-audit --no-fund
            $installExit = $LASTEXITCODE
            Pop-Location
            if ($installExit -ne 0) { Write-Error "npm install 失败 (exit $installExit)"; exit 1 }
        }
        Push-Location $FrontendDir
        & $npmBuild run build
        $buildExit = $LASTEXITCODE
        Pop-Location
        if ($buildExit -ne 0 -or -not (Test-Path $distIndex)) {
            Write-Error "前端构建失败 (exit $buildExit)"
            exit 1
        }
        Write-Host "静态产物已生成: $distIndex"
    } else {
        Write-Host "复用已有静态产物: $distIndex"
    }
}

# 2.6 数据库护栏（实测事故：调用方 shell 里残留 DATABASE_URL=sqlite:///:memory:
# 会被子进程继承 → 后端连到空内存库，因缺表在启动阶段直接失败。
# 正式启动拒绝内存库，并把「实际会用哪个库文件」明确打印出来。
$dbUrl = $env:DATABASE_URL
if (-not $dbUrl) {
    Write-Host "DATABASE_URL: (未设置 → 使用 backend\.env 或默认 sqlite:///./a_stock.db)"
} else {
    Write-Host "DATABASE_URL: $dbUrl"
}
if ($dbUrl -and ($dbUrl -match ':memory:' -or $dbUrl -match 'mode=memory')) {
    Write-Error ("检测到 DATABASE_URL 指向内存库（$dbUrl）。这是测试配置，正式启动拒绝使用：" +
        "内存库没有表结构，后端会在启动阶段失败。请清空该环境变量（Remove-Item Env:\DATABASE_URL）后重试。")
    exit 1
}
if ($dbUrl -and $dbUrl -match '^sqlite') {
    $dbPath = ($dbUrl -replace '^sqlite(\+\w+)?:///', '') -replace '\?.*$', ''
    if ($dbPath -and $dbPath -ne ':memory:') {
        $resolved = if ([System.IO.Path]::IsPathRooted($dbPath)) { $dbPath } else { Join-Path $BackendDir $dbPath }
        if (Test-Path -LiteralPath $resolved) {
            $item = Get-Item -LiteralPath $resolved
            Write-Host ("数据库文件: {0} ({1:N1} MB, 最后写入 {2})" -f $resolved, ($item.Length / 1MB), $item.LastWriteTime)
        } else {
            Write-Host "警告：DATABASE_URL 指向的数据库文件不存在：$resolved" -ForegroundColor Yellow
        }
    }
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
    -ArgumentList "-m","uvicorn","app.main:app","--host",$bindHost,"--port","$backendPort" `
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

# 5.5 正式部署：前端由后端托管，不需要再起 Vite
if ($Prod) {
    Write-Host ""
    Write-Host "=== Launch OK (prod) ==="
    Write-Host "Web:     http://127.0.0.1:$backendPort"
    Write-Host "API:     http://127.0.0.1:$backendPort/api/health/ready"
    if ($Lan) {
        $lanIp = Get-LanIPv4
        if ($lanIp) {
            Write-Host "手机/平板（同一 Wi-Fi）: http://${lanIp}:$backendPort" -ForegroundColor Yellow
            Write-Host "  访问口令: $accessPassword" -ForegroundColor Yellow
            Write-Host "  （首次打不开时需在防火墙放行 TCP $backendPort）"
        } else {
            Write-Host "未探测到局域网 IPv4，请用 ipconfig 查看本机地址"
        }
    }
    Write-Host "Stop: powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1"
    exit 0
}

# 6. Resolve npm 定义在脚本前部（-Prod 构建与 dev 启动共用同一份解析逻辑）

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
    -ArgumentList "run","dev","--","--host",$bindHost,"--port","5173" `
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
if ($Lan) {
    $lanIp = Get-LanIPv4
    if ($lanIp) {
        $lanUrl = "http://${lanIp}:5173"
        Write-Host "手机/平板（同一 Wi-Fi）: $lanUrl"
        Write-Host "  - 首次使用若打不开：在 Windows 防火墙放行 TCP 5173，或用管理员 PowerShell 执行"
        Write-Host '    New-NetFirewallRule -DisplayName "A-Stock Frontend 5173" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5173'
    } else {
        Write-Host "手机/平板：未探测到局域网 IPv4，请用 ipconfig 查看本机地址后访问 http://<IPv4>:5173"
    }
    Write-Host "  - 注意：0.0.0.0 监听对同一局域网内所有设备可见，用完可跑 stop_all.ps1 关闭"
}
Write-Host "Stop: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1"
