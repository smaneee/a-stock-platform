# 确保 backend 的 Python 虚拟环境可用，并把解释器绝对路径写到标准输出。
# 被 start_backend.ps1 / test_backend.ps1 复用。
#
# 顺序：
#   1) 复用已有 venv（.venv → .venv-311 → .venv-312）
#   2) 用 py 启动器（3.13 → 3.12 → 3.11）或 PATH 上 >=3.11 的 python 新建 .venv
#   3) 新建时用清华 PyPI 镜像安装 requirements.txt
# 找不到 >=3.11 解释器时明确失败，不用旧版本静默建出坏环境。
$ErrorActionPreference = "Stop"

$BackendDir = Join-Path (Split-Path -Parent $PSScriptRoot) "backend"
$Requirements = Join-Path $BackendDir "requirements.txt"
$VenvDir = Join-Path $BackendDir ".venv"
$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

function Get-BasePython {
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        foreach ($version in @("3.13", "3.12", "3.11")) {
            & py "-$version" --version *> $null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$version") }
        }
    }
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

foreach ($candidate in @(".venv", ".venv-311", ".venv-312")) {
    $candidatePython = Join-Path $BackendDir "$candidate\Scripts\python.exe"
    if (Test-Path $candidatePython) {
        Write-Host "Reusing existing venv: $candidate"
        Write-Output $candidatePython
        exit 0
    }
}

$base = Get-BasePython
if ($null -eq $base) {
    Write-Host "ERROR: 需要 Python >= 3.11（py 启动器或 PATH 上的 python）。" -ForegroundColor Red
    Write-Host "请先安装 Python 3.11+，或直接运行 scripts/start_all.ps1（它自带解释器探测）。" -ForegroundColor Red
    exit 1
}

Write-Host ("Creating backend\.venv with {0} {1}" -f $base[0], $base[1])
if ($base[1]) {
    & $base[0] $base[1] -m venv $VenvDir
} else {
    & $base[0] -m venv $VenvDir
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: venv 创建失败" -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
Write-Host "Installing dependencies from $Mirror ..."
& $venvPython -m pip install --disable-pip-version-check --index-url $Mirror -r $Requirements
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: 依赖安装失败" -ForegroundColor Red
    exit 1
}

Write-Output $venvPython
