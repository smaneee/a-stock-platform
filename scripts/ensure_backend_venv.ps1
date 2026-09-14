# 确保 backend 的 Python 虚拟环境可用，并把解释器绝对路径写到标准输出。
# 被 start_backend.ps1 / test_backend.ps1 / start_all.ps1 复用。
#
# 解析顺序（与研发计划「固定路径与边界」一致）：
#   1) 项目内 backend\.venv-311  —— 本机固定环境，永远优先
#   2) backend\.venv-312 / .venv —— 兼容历史环境，仅在 .venv-311 缺失时使用
#   3) 都没有时用「独立 Python 3.11」新建 backend\.venv-311（不再新建 .venv，
#      避免同一个项目出现两套依赖），并从清华 PyPI 镜像安装 requirements.txt
# 找不到 >=3.11 解释器时明确失败，不用旧版本静默建出坏环境。
#
# 参数：
#   -Rebuild   把当前 .venv-311 改名成 .venv-311.broken-<时间戳> 后重建
#              （只做改名，不删除；重建前请确认已有数据库备份）
#   -Quiet     只输出解释器路径
[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$BackendDir = Join-Path (Split-Path -Parent $PSScriptRoot) "backend"
$Requirements = Join-Path $BackendDir "requirements.txt"
$PrimaryVenv = Join-Path $BackendDir ".venv-311"
$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 用户明确要求的独立 Python 3.11（与会话/Codex 运行时无关）
$PinnedPython = "C:\Users\ASUS\AppData\Local\Programs\Python\Python311\python.exe"

function Write-Info([string]$Message) {
    if (-not $Quiet) { Write-Host $Message }
}

function Test-VenvPython([string]$ExePath) {
    if (-not (Test-Path $ExePath)) { return $false }
    try {
        $out = & $ExePath -c "import sys;print(sys.version_info[0],sys.version_info[1])" 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        $parts = "$out".Trim().Split(" ")
        return ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)
    } catch {
        return $false
    }
}

function Get-BasePython {
    # 1) 钉死的独立 Python 3.11
    if (Test-VenvPython $PinnedPython) { return @($PinnedPython, "") }
    # 2) py 启动器（3.11 优先，避免建出 3.13 环境带来依赖差异）
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        foreach ($version in @("3.11", "3.12", "3.13")) {
            & py "-$version" --version *> $null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$version") }
        }
    }
    # 3) PATH 上的 python
    if (Get-Command "python" -ErrorAction SilentlyContinue) {
        $output = (& python --version 2>&1) | Out-String
        if ($output -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) { return @("python", "") }
        }
    }
    return $null
}

function New-Venv([string[]]$Base) {
    Write-Info ("Creating backend\.venv-311 with {0} {1}" -f $Base[0], $Base[1])
    if ($Base[1]) {
        & $Base[0] $Base[1] -m venv $PrimaryVenv
    } else {
        & $Base[0] -m venv $PrimaryVenv
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: venv 创建失败" -ForegroundColor Red
        exit 1
    }
    $venvPython = Join-Path $PrimaryVenv "Scripts\python.exe"
    Write-Info "Installing dependencies from $Mirror ..."
    & $venvPython -m pip install --disable-pip-version-check --index-url $Mirror -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: 依赖安装失败" -ForegroundColor Red
        exit 1
    }
}

$primaryPython = Join-Path $PrimaryVenv "Scripts\python.exe"

if ($Rebuild -and (Test-Path $PrimaryVenv)) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $broken = "$PrimaryVenv.broken-$stamp"
    Write-Info "Rebuild: 把现有 .venv-311 改名为 $(Split-Path -Leaf $broken)（不删除）"
    Move-Item -LiteralPath $PrimaryVenv -Destination $broken -Force
}

# 1) 固定复用 .venv-311
if (Test-VenvPython $primaryPython) {
    Write-Info "Reusing existing venv: .venv-311"
    Write-Output $primaryPython
    exit 0
}
if (Test-Path $primaryPython) {
    Write-Host "警告：.venv-311 存在但不可用（Python 版本或解释器损坏）。" -ForegroundColor Yellow
    Write-Host "      可用 -Rebuild 重建：powershell -File scripts\ensure_backend_venv.ps1 -Rebuild" -ForegroundColor Yellow
}

# 2) 兼容历史环境
foreach ($candidate in @(".venv-312", ".venv")) {
    $candidatePython = Join-Path $BackendDir "$candidate\Scripts\python.exe"
    if (Test-VenvPython $candidatePython) {
        Write-Host "警告：.venv-311 不可用，回退到 $candidate。建议尽快用 -Rebuild 统一到 .venv-311。" -ForegroundColor Yellow
        Write-Output $candidatePython
        exit 0
    }
}

# 3) 新建 .venv-311
$base = Get-BasePython
if ($null -eq $base) {
    Write-Host "ERROR: 需要 Python >= 3.11（优先 $PinnedPython，其次 py 启动器或 PATH 上的 python）。" -ForegroundColor Red
    Write-Host "请先安装 Python 3.11+，或运行 scripts/start_all.ps1（它自带解释器探测）。" -ForegroundColor Red
    exit 1
}

New-Venv $base
Write-Output $primaryPython
