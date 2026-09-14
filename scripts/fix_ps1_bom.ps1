# 确保项目内所有 .ps1 都是 UTF-8 with BOM。
#
# 为什么必须这样：项目脚本固定由 Windows PowerShell 5.1 启动（launcher/快捷方式），
# 5.1 在没有 BOM 时按本机 ANSI 代码页（简体中文即 GBK）解析 .ps1，中文注释会被读成
# 乱码并直接引发语法错误。实测：编辑工具保存后 BOM 丢失 → 解析报 "Unexpected token"。
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts\fix_ps1_bom.ps1
[CmdletBinding()]
param([switch]$WhatIfOnly)

$RootDir = Split-Path -Parent $PSScriptRoot
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$fixed = 0
$ok = 0

foreach ($file in Get-ChildItem -LiteralPath $RootDir -Filter "*.ps1" -Recurse -File -ErrorAction SilentlyContinue) {
    if ($file.FullName -match "\\(node_modules|\.venv[^\\]*)\\]") { continue }
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if ($hasBom) {
        $ok++
        continue
    }
    if ($WhatIfOnly) {
        Write-Output "需要修复: $($file.FullName.Substring($RootDir.Length + 1))"
        continue
    }
    $text = [System.IO.File]::ReadAllText($file.FullName, [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText($file.FullName, $text, $utf8Bom)
    Write-Output "已修复 BOM: $($file.FullName.Substring($RootDir.Length + 1))"
    $fixed++
}

Write-Output "ps1 文件: 已有 BOM $ok 个，本次修复 $fixed 个"
