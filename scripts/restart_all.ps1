# 重启后端与前端：先按 PID 文件停止（端口兜底），再完整启动。
# 用法: powershell -ExecutionPolicy Bypass -File scripts/restart_all.ps1 [-Lan] [-Prod]
#   -Lan : 前端监听 0.0.0.0，允许同 Wi-Fi 的手机/平板访问
#   -Prod: 正式部署（后端托管 frontend\dist，只占 8000 一个端口）。
#          实测踩坑：启动器以 -Prod 启动后若走「重启服务」，旧版本重启脚本不转发
#          -Prod，会把 Vite 开发服务又拉起来（5173 被占用）。这里必须原样转发。
#
# 本文件必须保存为 UTF-8 with BOM。
param(
    [switch]$Lan,
    [switch]$Prod
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
Write-Host "=== 重启 A 股量化平台 ==="

& (Join-Path $ScriptDir "stop_all.ps1")

Write-Host ""
& (Join-Path $ScriptDir "start_all.ps1") -Lan:$Lan -Prod:$Prod
