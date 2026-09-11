# 按 PID 停止后端与前端，并清理 PID 文件、确认端口释放
# 用法: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1
$ErrorActionPreference = "Continue"

$RootDir = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $RootDir ".run"

# 注意：变量名不能用 $pid —— $PID 是 PowerShell 只读自动变量，
# 赋值会失败并把 $pid 退化成当前 PowerShell 进程的 PID（等于杀掉自己）。
function Stop-ByPidFile([string]$Name) {
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "$Name 未在运行（无 PID 文件）"
        return
    }
    $raw = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $targetId = 0
    if (-not [int]::TryParse("$raw".Trim(), [ref]$targetId)) {
        Write-Host "$Name 的 PID 文件无有效内容，已清理：$pidFile"
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return
    }
    $proc = Get-Process -Id $targetId -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "停止 $Name PID=$targetId ..."
        # /T 连同子进程结束（npm.cmd 会派生 node/vite，只杀父进程会留下占用端口的孩子）
        & taskkill /T /F /PID $targetId 2>&1 | Out-Null
        Wait-Process -Id $targetId -Timeout 10 -ErrorAction SilentlyContinue
    } else {
        Write-Host "$Name 进程已不存在 PID=$targetId"
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

function Confirm-PortReleased([int]$Port) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "端口 $Port 已释放"
        return
    }
    $owners = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
    Write-Host "端口 $Port 仍被 PID $($owners -join ',') 占用，继续结束该进程..." -ForegroundColor Yellow
    foreach ($ownerId in $owners) {
        & taskkill /T /F /PID $ownerId 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "端口 $Port 仍未释放，请手动检查上面的 PID" -ForegroundColor Red
    } else {
        Write-Host "端口 $Port 已释放"
    }
}

Stop-ByPidFile "frontend"
Stop-ByPidFile "backend"

Confirm-PortReleased 5173
Confirm-PortReleased 8000

Write-Host "已停止全部服务"
