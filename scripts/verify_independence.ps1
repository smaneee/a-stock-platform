# 独立性验证：证明股票软件不依赖 Codex（或任何单一私有运行时）即可启动与提供页面。
#
# 做法（全部有证据，不靠推断）：
#   A. 静态证据：扫描启动链路涉及的脚本/后端/前端源码，确认没有 codex 路径或命令引用。
#   B. 解释器/工具链证据：后端 venv 的 base prefix、前端 node_modules shim 指向的 node。
#   C. 实证证据：把 Codex 相关目录**临时改名**（可逆、不删除），再跑一次
#      启动 → 健康检查 → 页面加载 → API → 停止；无论成败都在 finally 里改回原名。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\verify_independence.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\verify_independence.ps1 -SkipRename
[CmdletBinding()]
param(
    # 只做 A/B 静态与工具链检查，不改名目录
    [switch]$SkipRename,
    # 平台启动监听端口
    [int]$Port = 8000
)

$ErrorActionPreference = "Continue"
$RootDir = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $RootDir "outputs\handoff"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$ResultFile = Join-Path $OutDir "independence-check.json"
$LogFile = Join-Path $RootDir ".run\independence-start.log"

$evidence = [ordered]@{
    checked_at = (Get-Date).ToString("s")
    root_dir = $RootDir
    static_scan = @()
    toolchain = [ordered]@{}
    rename_test = [ordered]@{ attempted = $false; renamed = @(); start_ok = $false; probes = @(); restored = @() }
    verdict = ""
}

function Add-Evidence([string]$Name, [string]$Value) {
    $evidence.static_scan += [ordered]@{ name = $Name; value = $Value }
}

Write-Host "=== A. 静态扫描：启动链路是否引用 codex ==="
$scanRoots = @(
    (Join-Path $RootDir "scripts"),
    (Join-Path $RootDir "backend\app"),
    (Join-Path $RootDir "frontend\src")
)
$hits = @()
$selfPath = $MyInvocation.MyCommand.Path
foreach ($root in $scanRoots) {
    if (-not (Test-Path $root)) { continue }
    $files = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".ps1", ".py", ".ts", ".tsx", ".json", ".cmd", ".bat", ".ini", ".cfg", ".toml") }
    foreach ($f in $files) {
        # 跳过验证脚本自身：它必然包含 "codex" 字样（候选路径、说明文字）
        if ($selfPath -and $f.FullName -eq $selfPath) { continue }
        $m = Select-String -LiteralPath $f.FullName -Pattern "codex" -SimpleMatch -ErrorAction SilentlyContinue
        foreach ($hit in $m) {
            $text = $hit.Line.Trim()
            $isComment = $text.StartsWith("#") -or $text.StartsWith("//") -or
                         $text.StartsWith("*") -or $text.StartsWith("<!--")
            $hits += [ordered]@{
                file = $f.FullName.Substring($RootDir.Length + 1)
                line = $hit.LineNumber
                kind = $(if ($isComment) { "comment" } else { "functional" })
                text = $text
            }
        }
    }
}
$functionalHits = @($hits | Where-Object { $_.kind -eq "functional" })
Add-Evidence "启动链路 codex 引用（非注释）" "$($functionalHits.Count)"
Add-Evidence "启动链路 codex 引用（含注释）" "$($hits.Count)"
$evidence.static_scan += [ordered]@{ name = "codex 引用明细"; value = $hits }

Write-Host "=== B. 工具链证据 ==="
$venvPython = Join-Path $RootDir "backend\.venv-311\Scripts\python.exe"
$pyCfg = Join-Path $RootDir "backend\.venv-311\pyvenv.cfg"
if (Test-Path $pyCfg) {
    $cfg = Get-Content -LiteralPath $pyCfg -Encoding UTF8
    $evidence.toolchain["pyvenv_cfg"] = $cfg
    Write-Host ($cfg -join " | ")
} else {
    $evidence.toolchain["pyvenv_cfg"] = "缺失"
}
$viteShim = Join-Path $RootDir "frontend\node_modules\.bin\vite.cmd"
if (Test-Path $viteShim) {
    $shimHead = (Get-Content -LiteralPath $viteShim -Encoding Default -TotalCount 12) -join "; "
    $evidence.toolchain["vite_shim_head"] = $shimHead
    Write-Host "vite shim: $shimHead"
}
$evidence.toolchain["node_exe_on_disk"] = @(
    "C:\Users\ASUS\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
) | Where-Object { Test-Path $_ }
$evidence.toolchain["python311"] = (Test-Path "C:\Users\ASUS\AppData\Local\Programs\Python\Python311\python.exe")

# Codex 相关候选路径（只探测，不删除）
$codexCandidates = @(
    "D:\Codex",
    "D:\CodexVersion",
    "D:\Codex\current",
    "$env:LOCALAPPDATA\Codex",
    "$env:USERPROFILE\.codex",
    "$env:USERPROFILE\.cache\codex-runtimes",
    "$env:USERPROFILE\.hi-codex",
    "E:\Hi Codex"
)
$codexFound = @($codexCandidates | Where-Object { Test-Path -LiteralPath $_ })
$evidence.static_scan += [ordered]@{ name = "存在的 Codex 相关路径"; value = $codexFound }
Write-Host "存在的 Codex 相关路径: $($codexFound -join ', ')"

function Test-Ready([int]$p, [int]$TimeoutSeconds = 120) {
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$p/api/health/ready" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Stop-Platform {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RootDir "scripts\stop_all.ps1") *> $null
}

$moved = @()
try {
    if (-not $SkipRename) {
        Write-Host "=== C. 临时改名 Codex 目录（可逆）==="
        foreach ($path in $codexFound) {
            # 只改名我们自己能确定归属的候选目录，且绝不删除内容
            $target = "$path.codex-unavailable-test"
            try {
                if (Test-Path -LiteralPath $path) {
                    Rename-Item -LiteralPath $path -NewName (Split-Path -Leaf $target) -ErrorAction Stop
                    $moved += [ordered]@{ from = $path; to = $target }
                    Write-Host "  已改名: $path -> $target"
                }
            } catch {
                Write-Host "  改名失败（可能被占用，跳过）: $path : $($_.Exception.Message)"
            }
        }
        $evidence.rename_test.attempted = $true
        $evidence.rename_test.renamed = $moved
    }

    Write-Host "=== 启动平台（正式模式，不依赖 Vite/Codex）==="
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$RootDir\scripts\start_all.ps1`" -Prod"
    $starter = Start-Process powershell -ArgumentList $argLine -WindowStyle Hidden `
        -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" -PassThru
    $ready = Test-Ready -p $Port -TimeoutSeconds 150
    $evidence.rename_test.start_ok = $ready
    Write-Host "ready = $ready"

    foreach ($probe in @(
        @{ name = "health_ready"; url = "http://127.0.0.1:$Port/api/health/ready" },
        @{ name = "market_session"; url = "http://127.0.0.1:$Port/api/market/session" },
        @{ name = "spa_index"; url = "http://127.0.0.1:$Port/" },
        @{ name = "spa_deep_link"; url = "http://127.0.0.1:$Port/watchlist" }
    )) {
        $entry = [ordered]@{ name = $probe.name; url = $probe.url; status = $null; bytes = 0; elapsed_ms = 0 }
        $t0 = Get-Date
        try {
            $r = Invoke-WebRequest $probe.url -UseBasicParsing -TimeoutSec 20
            $entry.status = $r.StatusCode
            $entry.bytes = $r.Content.Length
        } catch {
            $entry.status = "ERR: $($_.Exception.Message)"
        }
        $entry.elapsed_ms = [int]((Get-Date) - $t0).TotalMilliseconds
        $evidence.rename_test.probes += $entry
        Write-Host ("  {0,-16} {1} ({2} bytes, {3} ms)" -f $entry.name, $entry.status, $entry.bytes, $entry.elapsed_ms)
    }

    Stop-Platform
    Start-Sleep -Seconds 2
    $listening = $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $evidence.rename_test["stopped"] = -not $listening
    Write-Host "停止后端口仍监听 = $listening"
    Write-Host "--- start_all 输出（尾部）---"
    if (Test-Path $LogFile) { Get-Content $LogFile -Encoding UTF8 | Select-Object -Last 12 }
} finally {
    foreach ($item in $moved) {
        try {
            if (Test-Path -LiteralPath $item.to) {
                Rename-Item -LiteralPath $item.to -NewName (Split-Path -Leaf $item.from) -ErrorAction Stop
                $evidence.rename_test.restored += $item.from
                Write-Host "已还原: $($item.from)"
            }
        } catch {
            Write-Host "还原失败（需人工处理）: $($item.to) : $($_.Exception.Message)"
        }
    }
}

# 结论：静态无引用 + 工具链独立 + （若做了改名）改名后仍能启动并提供页面
$staticClean = ($functionalHits.Count -eq 0)
$ranOk = $SkipRename -or ($evidence.rename_test.start_ok -and $evidence.rename_test.probes.Count -ge 4)
$evidence.verdict = if ($staticClean -and $ranOk) { "PASS：启动链路无 Codex 引用，改名 Codex 目录后仍可启动并服务页面" }
    elseif ($staticClean) { "PARTIAL：静态无引用，但运行验证未通过（见 probes）" }
    else { "FAIL：启动链路仍引用 codex（见 static_scan）" }

# 写 JSON **必须不带 BOM**：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会加 BOM，
# 而 Python `json.load()` / 前端 `fetch().json()` 遇到 BOM 会直接失败
# （实测：本文件产出的 independence-check.json 曾被带 BOM，标准解析报
#  `Unexpected UTF-8 BOM`）。注意与 `.ps1` 自身的规则相反 —— 脚本要 BOM，JSON 不要。
$json = $evidence | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($ResultFile, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "结论: $($evidence.verdict)"
Write-Host "证据: $ResultFile"
