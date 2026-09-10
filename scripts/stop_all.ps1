# 按 PID 停止后端与前端，并清理 PID 文件
# 用法: powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1
$ErrorActionPreference = "Continue"

$RootDir = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $RootDir ".run"

function Stop-ByPidFile([string]$Name) {
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "$Name 未在运行（无 PID 文件）"
        return
    }
    $pid = Get-Content $pidFile
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "停止 $Name PID=$pid ..."
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "$Name 进程已不存在 PID=$pid"
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

Stop-ByPidFile "frontend"
Stop-ByPidFile "backend"

Write-Host "已停止全部服务"
