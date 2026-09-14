# 多视口真机渲染截图（P0-04 移动端验收）。
#
# 踩坑记录（必须保留）：
#   1) 项目路径含空格，`--screenshot=<path>` 不加引号会被 Chrome 当成多个 target，
#      报 "Multiple targets are not supported in headless mode" 且不产生 PNG。
#   2) 需要 --no-sandbox；用 Start-Process + WaitForExit 串行截图，不要用 `& chrome`。
#   3) 每张截图有硬超时，超时即杀进程并记录。
[CmdletBinding()]
param(
    [string]$Base = "http://127.0.0.1:8000",
    [string[]]$Paths = @("/", "/realtime-picks", "/universe"),
    [int]$VirtualTimeMs = 6000,
    [int]$TimeoutSec = 60
)
$ErrorActionPreference = "Continue"
$RootDir = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $RootDir "outputs\quality\viewports"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$viewports = @(
    @{ name = "360x800"; w = 360; h = 800 },
    @{ name = "390x844"; w = 390; h = 844 },
    @{ name = "768x1024"; w = 768; h = 1024 },
    @{ name = "1440x900"; w = 1440; h = 900 }
)
$browser = $null
foreach ($candidate in @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)) { if (Test-Path $candidate) { $browser = $candidate; break } }
if (-not $browser) { Write-Error "未找到 Chrome/Edge，无法做真实渲染验收"; exit 1 }
Write-Host "browser: $browser"
try {
    $ready = Invoke-WebRequest "$Base/api/health/ready" -UseBasicParsing -TimeoutSec 10
    Write-Host "ready: $($ready.StatusCode)"
} catch { Write-Error "服务不可用：$($_.Exception.Message)"; exit 1 }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$results = @()
foreach ($path in $Paths) {
    $slug = if ($path -eq "/") { "dashboard" } else { ($path.Trim("/") -replace "[^A-Za-z0-9\-]", "_") }
    foreach ($vp in $viewports) {
        $file = Join-Path $OutDir "$slug-$($vp.name)-$stamp.png"
        Remove-Item -LiteralPath $file -ErrorAction SilentlyContinue
        $profile = Join-Path $env:TEMP "astock-ck-$($vp.name)"
        $errFile = Join-Path $env:TEMP "astock-ck-$($vp.name).err"
        $argStr = "--headless=new --no-sandbox --disable-gpu --hide-scrollbars " +
                  "--no-first-run --no-default-browser-check " +
                  "--screenshot=`"$file`" --window-size=$($vp.w),$($vp.h) " +
                  "--virtual-time-budget=$VirtualTimeMs --user-data-dir=`"$profile`" " +
                  "$Base$path"
        $entry = [ordered]@{ path = $path; viewport = $vp.name; width = $vp.w; height = $vp.h
                             file = $file; bytes = 0; status = "missing"; elapsed_ms = 0 }
        $t0 = Get-Date
        $proc = Start-Process -FilePath $browser -ArgumentList $argStr -PassThru -WindowStyle Hidden `
            -RedirectStandardError $errFile -RedirectStandardOutput (Join-Path $env:TEMP "astock-ck.out")
        $exited = $proc.WaitForExit($TimeoutSec * 1000)
        if (-not $exited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; $entry.status = "timeout" }
        $entry.elapsed_ms = [int]((Get-Date) - $t0).TotalMilliseconds
        if (Test-Path -LiteralPath $file) {
            $entry.bytes = (Get-Item -LiteralPath $file).Length
            if (-not $exited) { $entry.status = "timeout_but_captured" }
            else { $entry.status = $(if ($entry.bytes -gt 5000) { "captured" } else { "suspiciously_small" }) }
        }
        if ($entry.status -eq "missing" -and (Test-Path $errFile)) {
            $entry["stderr"] = ((Get-Content $errFile -ErrorAction SilentlyContinue | Select-Object -Last 2) -join " | ")
        }
        $results += $entry
        Write-Host ("{0,-16} {1,-9} {2,9} bytes {3,7} ms  {4}" -f $path, $vp.name, $entry.bytes, $entry.elapsed_ms, $entry.status)
    }
}
$summary = [ordered]@{ generated_at = (Get-Date).ToString("s"); base = $Base; browser = $browser
                       virtual_time_ms = $VirtualTimeMs; results = $results }
$viewportsJson = $summary | ConvertTo-Json -Depth 5
# JSON 产物**必须不带 BOM**（与 `.ps1` 自身的规则相反）：`Set-Content -Encoding UTF8`
# 在 PowerShell 5.1 下会加 BOM，导致 Python `json.load()` / `fetch().json()` 解析失败。
$viewportsPath = Join-Path $OutDir "viewports-$stamp.json"
[System.IO.File]::WriteAllText($viewportsPath, $viewportsJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "写入: $viewportsPath"
