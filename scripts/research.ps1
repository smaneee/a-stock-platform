# 单标的投资研究（短期自用版）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\research.ps1 -Symbol 000333
#   powershell -ExecutionPolicy Bypass -File scripts\research.ps1 -Symbol 000333 -Question "为什么现在不建议行动"
#   powershell -ExecutionPolicy Bypass -File scripts\research.ps1 -Symbol 600519 -Growth 0.04 -Discount 0.09
#
# 说明：只是薄封装，真正的逻辑在 outputs\handoff\_research_cli.py。
# 增长率/现金流率/折现率必须自己给（默认值是示例估计，不是推荐值）。

param(
    [Parameter(Position = 0)][string]$Symbol = "000333",
    [string]$Question = "",
    [switch]$NoRefresh,
    [switch]$Portfolio,
    [double]$Growth = 0.06,
    [double]$FcfMargin = 0.09,
    [double]$Discount = 0.10,
    [double]$Terminal = 0.02,
    [int]$Years = 5
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RootDir "backend\.venv-311\Scripts\python.exe"
$Cli = Join-Path $RootDir "outputs\handoff\_research_cli.py"

if (-not (Test-Path $Python)) {
    Write-Error "找不到 Python 运行时：$Python"
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
$cliArgs = @(
    $Cli, $Symbol,
    "--growth", "$Growth",
    "--fcf-margin", "$FcfMargin",
    "--discount", "$Discount",
    "--terminal", "$Terminal",
    "--years", "$Years"
)
if ($Question -ne "") { $cliArgs += @("--question", $Question) }
if ($NoRefresh) { $cliArgs += "--no-refresh" }
if ($Portfolio) { $cliArgs += "--portfolio" }

& $Python @cliArgs
exit $LASTEXITCODE
