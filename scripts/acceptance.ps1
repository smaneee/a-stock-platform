# 一键验收（可双击运行）
#
# 把全量测试、文档状态扫描、JSON BOM 扫描、交付点自检、端到端探针、独立性验证
# 收成一条命令 —— 避免「记不全就漏项」，而漏掉的那项往往正是会出问题的那项
# （JSON BOM 就是这样漏了 26 轮的）。
#
# 用法：
#   .\scripts\acceptance.ps1            # 快速：测试 + 三项扫描 + 自检
#   .\scripts\acceptance.ps1 -Full      # 完整：再加端到端探针与独立性验证（约 4~6 分钟）
#
# 注意：本文件是 .ps1，**必须保存为 UTF-8 with BOM**（否则中文会乱码甚至语法错）。
# 若用编辑器改过导致 BOM 丢失，跑一次 scripts\fix_ps1_bom.ps1 修复。

[CmdletBinding()]
param(
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project "backend\.venv-311\Scripts\python.exe"
$Runner = Join-Path $Project "outputs\handoff\_acceptance_all.py"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "找不到解释器: $Python" -ForegroundColor Red
    Write-Host "请先运行 scripts\ensure_backend_venv.ps1" -ForegroundColor Yellow
    exit 2
}

$args = @($Runner)
if ($Full) { $args += "--full" }

Write-Host "=== 一键验收 ===" -ForegroundColor Cyan
Write-Host "解释器: $Python"
Write-Host "模式  : $(if ($Full) { '完整（含端到端与独立性）' } else { '快速' })"
Write-Host ""

& $Python @args
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "验收通过（退出码 0）" -ForegroundColor Green
} else {
    Write-Host "验收未通过（退出码 $code），详见 outputs\handoff\acceptance-all.json" -ForegroundColor Red
}
exit $code
