# A 股量化平台 · 桌面启动器
#
# 双击即用的一键启停面板。只用 .NET 自带组件（WinForms），不引入任何第三方
# 依赖，也不依赖 backend 的 venv —— venv 坏掉时依然能启停服务、打开日志。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\launcher.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\launcher.ps1 -SelfTest
#     （只构建界面并打印自检信息，不进入消息循环，供测试/CI 使用）
#
# 约定：
#   - 子脚本一律用 Windows PowerShell 5.1 启动（与本项目脚本的验证环境一致）
#   - 所有后台进程用 -WindowStyle Hidden，避免弹出多余的黑框
#   - 本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会把中文读成乱码
[CmdletBinding()]
param(
    [switch]$SelfTest,
    # 只打印诊断信息并退出（供脚本/自动化验收使用），不弹窗
    [switch]$Diagnose,
    # 无界面执行一次动作（start|stop|restart）并打印结果后退出。
    # 用途：自动化验收「双击快捷方式 → 一键启动」这条动作链路，
    # 走的是与按钮完全相同的 Start-BackgroundScript / Get-FinishMessage 代码路径。
    [ValidateSet("start", "stop", "restart")]
    [string]$Action = "",
    [int]$ActionTimeoutSec = 240
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$RootDir = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $RootDir ".run"
$LauncherLog = Join-Path $RunDir "launcher.log"
$LauncherErrLog = Join-Path $RunDir "launcher.err.log"

$BackendPort = 8000
$FrontendPort = 5173
$FrontendUrl = "http://127.0.0.1:$FrontendPort"
$DocsUrl = "http://127.0.0.1:$BackendPort/docs"
$ReadyUrl = "http://127.0.0.1:$BackendPort/api/health/ready"
$BackendBase = "http://127.0.0.1:$BackendPort"

# 子脚本固定用 Windows PowerShell 5.1；本启动器自己在哪个宿主下运行都可以
$WinPs = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path $WinPs)) { $WinPs = "powershell.exe" }

$Palette = @{
    Bg     = [System.Drawing.Color]::FromArgb(8, 13, 26)
    Panel  = [System.Drawing.Color]::FromArgb(15, 23, 42)
    Border = [System.Drawing.Color]::FromArgb(30, 41, 59)
    Text   = [System.Drawing.Color]::FromArgb(226, 232, 240)
    Muted  = [System.Drawing.Color]::FromArgb(148, 163, 184)
    Ok     = [System.Drawing.Color]::FromArgb(52, 211, 153)
    Warn   = [System.Drawing.Color]::FromArgb(251, 191, 36)
    Bad    = [System.Drawing.Color]::FromArgb(248, 113, 113)
    Accent = [System.Drawing.Color]::FromArgb(56, 189, 248)
}

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

# ──────────────── 状态探测 ────────────────

function Test-PortListening {
    param([int]$Port, [int]$TimeoutMs = 400)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-BackendReady {
    try {
        $resp = Invoke-WebRequest -Uri $ReadyUrl -UseBasicParsing -TimeoutSec 2
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Read-LauncherLog {
    $parts = @()
    foreach ($file in @($LauncherLog, $LauncherErrLog)) {
        if (-not (Test-Path -LiteralPath $file)) { continue }
        try {
            # 子进程重定向输出用的是本机 ANSI 代码页（简体中文即 GBK）
            $tail = Get-Content -LiteralPath $file -Encoding Default -Tail 200 -ErrorAction Stop
        } catch {
            continue
        }
        if ($tail) { $parts += $tail }
    }
    return ($parts -join [Environment]::NewLine).Trim()
}

# ──────────────── 诊断（P0-04：版本 / 数据库 / 端口 / 最后数据时间 / 错误日志） ────────────────

function Resolve-GitExe {
    # 不假设 PATH 上有 git：本机 git 由 GitHub Desktop 内嵌提供
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA "GitHubDesktop\app-*\resources\app\git\cmd\git.exe"),
        "C:\Program Files\Git\cmd\git.exe",
        "C:\Program Files (x86)\Git\cmd\git.exe"
    )
    foreach ($pattern in $patterns) {
        $hit = Get-Item $pattern -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-VersionText {
    $git = Resolve-GitExe
    if (-not $git) { return "未知（未找到 git，可作为纯运行时环境）" }
    try {
        $branch = (& $git -C $RootDir rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
        $head = (& $git -C $RootDir log -1 --format='%h %ad %s' --date=short 2>$null | Select-Object -First 1)
        $dirty = @(& $git -C $RootDir status --porcelain 2>$null).Count
        return "$branch @ $head（未提交改动 $dirty 项）"
    } catch {
        return "未知（git 调用失败：$($_.Exception.Message)）"
    }
}

function Get-DatabaseText {
    $db = Join-Path $RootDir "backend\a_stock.db"
    if (-not (Test-Path $db)) { return "缺失：$db" }
    $item = Get-Item $db
    $gb = [math]::Round($item.Length / 1GB, 2)
    return "$($item.Name) $gb GB · 最后写入 $($item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
}

function Get-FreshnessText {
    if (-not (Test-BackendReady)) { return "后端未就绪，无法读取数据时间" }
    try {
        $session = Invoke-RestMethod -Uri "$BackendBase/api/market/session" -TimeoutSec 5
        $today = if ($session.is_trading_day) { "今日交易日" } else { "今日休市" }
        return "$today · 当日 $($session.day) · 最近交易日 $($session.last_trading_day) · 下一交易日 $($session.next_trading_day)（日历 $($session.calendar_total) 天）"
    } catch {
        return "读取失败：$($_.Exception.Message)"
    }
}

function Get-ErrorLogText {
    $parts = @()
    foreach ($name in @("backend.err.log", "frontend.err.log", "launcher.err.log")) {
        $file = Join-Path $RunDir $name
        if (-not (Test-Path $file)) { continue }
        $lines = @(Get-Content -LiteralPath $file -Encoding Default -ErrorAction SilentlyContinue |
            Where-Object { "$_".Trim() -ne "" })
        if ($lines.Count -gt 0) {
            $parts += "$name：$($lines.Count) 行，末行 → $($lines[-1])"
        }
    }
    if ($parts.Count -eq 0) { return "无（err 日志为空）" }
    return ($parts -join "`r`n")
}

function Get-DiagnosticsLines {
    $venvPython = Join-Path $RootDir "backend\.venv-311\Scripts\python.exe"
    $pyText = "未找到 backend\.venv-311"
    if (Test-Path $venvPython) {
        try {
            $info = & $venvPython -c "import sys;print(sys.version.split()[0]+' base '+sys.base_prefix)" 2>$null
            $pyText = "$info"
        } catch { $pyText = "读取失败" }
    }
    $backendUp = Test-PortListening -Port $BackendPort
    $frontendUp = Test-PortListening -Port $FrontendPort
    $ready = $false
    if ($backendUp) { $ready = Test-BackendReady }
    return @(
        "版本      : $(Get-VersionText)",
        "解释器    : $pyText",
        "数据库    : $(Get-DatabaseText)",
        "端口      : 8000=$backendUp（ready=$ready） / 5173=$frontendUp",
        "数据时间  : $(Get-FreshnessText)",
        "错误日志  : $(Get-ErrorLogText)",
        "诊断包    : $(Get-DiagnosticsDir)"
    )
}

function Get-DiagnosticsDir {
    return (Join-Path $RootDir "outputs\diagnostics")
}

function Export-Diagnostics {
    $dir = Get-DiagnosticsDir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $file = Join-Path $dir "diag-$stamp.txt"
    $lines = @(
        "A 股量化平台 · 启动器诊断包",
        "生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
        "工作目录: $RootDir",
        ""
    ) + (Get-DiagnosticsLines) + @(
        "",
        "── backend.err.log 末 30 行 ──"
    )
    foreach ($name in @("backend.err.log", "backend.log")) {
        $logFile = Join-Path $RunDir $name
        $lines += "── $name ──"
        if (Test-Path $logFile) {
            $lines += @(Get-Content -LiteralPath $logFile -Encoding Default -Tail 30 -ErrorAction SilentlyContinue)
        } else {
            $lines += "(不存在)"
        }
    }
    # 与卸载/迁移相关的路径事实也一并导出，便于离线排查
    $lines += "── 运行时路径 ──"
    $lines += "venv: $(Join-Path $RootDir 'backend\.venv-311')"
    $lines += "node: $(Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions')"
    $lines += "python311: C:\Users\ASUS\AppData\Local\Programs\Python\Python311\python.exe"
    Set-Content -LiteralPath $file -Value ($lines -join "`r`n") -Encoding UTF8
    return $file
}

# ──────────────── 动作 ────────────────

function Get-BusyProcess {
    if ($script:actionProc -and -not $script:actionProc.HasExited) {
        return $script:actionProc
    }
    return $null
}

function Start-BackgroundScript {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Get-BusyProcess) {
        [System.Windows.Forms.MessageBox]::Show(
            "上一个操作（$($script:actionLabel)中）还没结束，请稍候。",
            "启动器", "OK", "Information"
        ) | Out-Null
        return
    }

    $full = Join-Path $PSScriptRoot $ScriptName
    if (-not (Test-Path -LiteralPath $full)) {
        [System.Windows.Forms.MessageBox]::Show(
            "找不到脚本：$full", "启动器", "OK", "Error"
        ) | Out-Null
        return
    }

    foreach ($file in @($LauncherLog, $LauncherErrLog)) {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }

    $script:actionLabel = $Label
    $script:actionStartedAt = Get-Date

    # 正式部署优先：有 frontend\dist 静态产物且是「启动/重启」时，直接以 -Prod 启动，
    # 不再拉 Vite 开发服务（研发计划 P0-04：不以 Vite 开发服务作为正式部署）。
    # 没有静态产物时自动退回开发模式，并在日志里写明原因，不静默改变行为。
    $scriptArgs = @()
    if ($ScriptName -in @("start_all.ps1", "restart_all.ps1")) {
        $distIndex = Join-Path $RootDir "frontend\dist\index.html"
        if (Test-Path -LiteralPath $distIndex) {
            $scriptArgs += "-Prod"
        } else {
            Add-Content -LiteralPath $LauncherLog -Encoding UTF8 `
                -Value "[launcher] 未找到 frontend\dist\index.html，回退到 Vite 开发模式（如需正式模式请先运行 npm run build）"
        }
    }

    # Start-Process -ArgumentList 只是把数组用空格拼起来，不会补引号；
    # 仓库路径本身就可能带空格（如 "E:\workbuddy work\..."），必须自己包一层，
    # 否则 -File 会拿到被空格截断的路径。
    $quotedScript = '"' + $full + '"'
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $quotedScript) + $scriptArgs
    try {
        $script:actionProc = Start-Process -FilePath $WinPs `
            -ArgumentList $argList `
            -WorkingDirectory $RootDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput $LauncherLog `
            -RedirectStandardError $LauncherErrLog `
            -PassThru -ErrorAction Stop
    } catch {
        $script:actionProc = $null
        $script:actionLabel = ""
        [System.Windows.Forms.MessageBox]::Show(
            "启动失败：$($_.Exception.Message)", "启动器", "OK", "Error"
        ) | Out-Null
        return
    }
    Update-Ui
}

function Invoke-StartPlatform {
    $running = (Test-PortListening -Port $BackendPort) -or (Test-PortListening -Port $FrontendPort)
    if (-not $running) {
        Start-BackgroundScript -ScriptName "start_all.ps1" -Label "启动"
        return
    }
    $choice = [System.Windows.Forms.MessageBox]::Show(
        "检测到服务已在运行。`n`n是：先停止再重新启动`n否：取消",
        "启动器", "YesNo", "Question"
    )
    if ($choice -eq [System.Windows.Forms.DialogResult]::Yes) {
        Start-BackgroundScript -ScriptName "restart_all.ps1" -Label "重启"
    }
}

function Invoke-StopPlatform {
    if (-not (Test-PortListening -Port $BackendPort) -and
        -not (Test-PortListening -Port $FrontendPort)) {
        [System.Windows.Forms.MessageBox]::Show(
            "当前没有服务在运行。", "启动器", "OK", "Information"
        ) | Out-Null
        return
    }
    Start-BackgroundScript -ScriptName "stop_all.ps1" -Label "停止"
}

function Get-FinishMessage {
    param([string]$Label)

    # 不能依赖子进程退出码：Start-Process -PassThru 在 Windows PowerShell 5.1
    # 下即使进程已退出也取不到 ExitCode。改为用可观测结果判定成败。
    $backendUp = Test-PortListening -Port $BackendPort
    $frontendUp = Test-PortListening -Port $FrontendPort

    if ($Label -eq "停止") {
        if (-not $backendUp -and -not $frontendUp) {
            return "停止完成：端口 8000 / 5173 均已释放。"
        }
        return "停止可能未完全生效：仍有端口在监听，详情见下方日志。"
    }
    if ($backendUp -and (Test-BackendReady)) {
        # 正式模式（-Prod）下前端由后端托管，只有 8000 一个端口；
        # 旧逻辑要求 8000 与 5173 同时监听，会把正常启动误报成「可能没成功」。
        if ($frontendUp) {
            return "$Label完成：后端已就绪、前端已运行（开发模式，5173），可以点「打开界面」。"
        }
        return "$Label完成：后端已就绪并在 8000 端口托管前端静态产物（正式模式），可以点「打开界面」。"
    }
    if ($backendUp) {
        return "$Label完成：端口已监听，后端仍在初始化，几秒后再看状态卡片。"
    }
    return "$Label可能没成功：端口 8000 未就绪，详情见下方日志。"
}

function Get-PlatformUrl {
    # 当前可用的界面地址：正式模式是 8000（后端托管静态产物），开发模式是 5173。
    if (Test-PortListening -Port $FrontendPort) { return $FrontendUrl }
    if ((Test-PortListening -Port $BackendPort) -and (Test-BackendReady)) { return $BackendBase }
    return $FrontendUrl
}

function Open-Url {
    param([string]$Url, [string]$NeedPort)
    if ($NeedPort -and -not (Test-PortListening -Port ([int]$NeedPort))) {
        [System.Windows.Forms.MessageBox]::Show(
            "服务还没起来，请先点「一键启动」。", "启动器", "OK", "Information"
        ) | Out-Null
        return
    }
    Start-Process $Url | Out-Null
}

function Show-ShortcutResult {
    $script = Join-Path $PSScriptRoot "create_desktop_shortcut.ps1"
    try {
        $output = & $WinPs -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
        [System.Windows.Forms.MessageBox]::Show(
            ($output | Out-String).Trim(), "启动器 · 桌面快捷方式", "OK", "Information"
        ) | Out-Null
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            "创建失败：$($_.Exception.Message)", "启动器", "OK", "Error"
        ) | Out-Null
    }
}

# ──────────────── 界面 ────────────────

function New-UiButton {
    param(
        [string]$Text,
        [int]$X, [int]$Y, [int]$Width = 146, [int]$Height = 34,
        [System.Drawing.Color]$Back,
        [System.Drawing.Color]$Fore
    )
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object System.Drawing.Point($X, $Y)
    $button.Size = New-Object System.Drawing.Size($Width, $Height)
    $button.FlatStyle = "Flat"
    $button.BackColor = $Back
    $button.ForeColor = $Fore
    $button.FlatAppearance.BorderColor = $Palette.Border
    $button.FlatAppearance.BorderSize = 1
    $button.Cursor = [System.Windows.Forms.Cursors]::Hand
    $button.UseVisualStyleBackColor = $false
    return $button
}

function New-StatusCard {
    param([string]$Title, [int]$X, [int]$Y)

    $card = New-Object System.Windows.Forms.Panel
    $card.Location = New-Object System.Drawing.Point($X, $Y)
    $card.Size = New-Object System.Drawing.Size(296, 72)
    $card.BackColor = $Palette.Panel

    $dot = New-Object System.Windows.Forms.Label
    $dot.Text = [char]0x25CF          # ●
    $dot.Font = New-Object System.Drawing.Font("Segoe UI", 12)
    $dot.ForeColor = $Palette.Bad
    $dot.Location = New-Object System.Drawing.Point(12, 8)
    $dot.Size = New-Object System.Drawing.Size(20, 20)
    $dot.BackColor = [System.Drawing.Color]::Transparent

    $name = New-Object System.Windows.Forms.Label
    $name.Text = $Title
    $name.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $name.ForeColor = $Palette.Text
    $name.Location = New-Object System.Drawing.Point(32, 10)
    $name.Size = New-Object System.Drawing.Size(252, 20)
    $name.BackColor = [System.Drawing.Color]::Transparent

    $detail = New-Object System.Windows.Forms.Label
    $detail.Text = "检测中…"
    $detail.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
    $detail.ForeColor = $Palette.Muted
    $detail.Location = New-Object System.Drawing.Point(32, 34)
    $detail.Size = New-Object System.Drawing.Size(252, 30)
    $detail.BackColor = [System.Drawing.Color]::Transparent

    $card.Controls.AddRange(@($dot, $name, $detail))
    return [pscustomobject]@{ Panel = $card; Dot = $dot; Detail = $detail }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "A 股量化平台 · 启动器"
$form.ClientSize = New-Object System.Drawing.Size(640, 616)
$form.MinimumSize = New-Object System.Drawing.Size(656, 656)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "Sizable"
$form.MaximizeBox = $false
$form.BackColor = $Palette.Bg
$form.ForeColor = $Palette.Text
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)

$header = New-Object System.Windows.Forms.Panel
$header.Location = New-Object System.Drawing.Point(0, 0)
$header.Size = New-Object System.Drawing.Size(640, 62)
$header.BackColor = $Palette.Panel
$header.Anchor = "Top,Left,Right"

$title = New-Object System.Windows.Forms.Label
$title.Text = "A 股量化平台"
$title.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 14, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = $Palette.Text
$title.Location = New-Object System.Drawing.Point(18, 10)
$title.AutoSize = $true
$title.BackColor = [System.Drawing.Color]::Transparent

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "股票池 · 历史入库 · 选股 · 回测 · 模拟盘　　仅用于研究，不构成投资建议"
$subtitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$subtitle.ForeColor = $Palette.Muted
$subtitle.Location = New-Object System.Drawing.Point(20, 38)
$subtitle.AutoSize = $true
$subtitle.BackColor = [System.Drawing.Color]::Transparent

$header.Controls.AddRange(@($title, $subtitle))

$backendCard = New-StatusCard -Title "后端服务（:8000）" -X 16 -Y 78
$frontendCard = New-StatusCard -Title "前端界面（:5173）" -X 328 -Y 78

$form.Controls.AddRange(@($header, $backendCard.Panel, $frontendCard.Panel))

$script:backendCard = $backendCard
$script:frontendCard = $frontendCard

$script:btnStart = New-UiButton -Text "一键启动" -X 16 -Y 162 `
    -Back ([System.Drawing.Color]::FromArgb(8, 145, 178)) -Fore ([System.Drawing.Color]::White)
$script:btnStop = New-UiButton -Text "停止全部" -X 170 -Y 162 `
    -Back ([System.Drawing.Color]::FromArgb(127, 29, 29)) -Fore ([System.Drawing.Color]::FromArgb(254, 202, 202))
$script:btnOpen = New-UiButton -Text "打开界面" -X 324 -Y 162 -Back $Palette.Panel -Fore $Palette.Text
$script:btnDocs = New-UiButton -Text "API 文档" -X 478 -Y 162 -Back $Palette.Panel -Fore $Palette.Text
$script:btnRestart = New-UiButton -Text "重启服务" -X 16 -Y 204 -Back $Palette.Panel -Fore $Palette.Text
$script:btnLogs = New-UiButton -Text "查看日志" -X 170 -Y 204 -Back $Palette.Panel -Fore $Palette.Text
$script:btnShortcut = New-UiButton -Text "创建桌面快捷方式" -X 324 -Y 204 -Back $Palette.Panel -Fore $Palette.Text
$script:btnRefresh = New-UiButton -Text "刷新状态" -X 478 -Y 204 -Back $Palette.Panel -Fore $Palette.Text
$script:btnDiag = New-UiButton -Text "导出诊断包" -X 16 -Y 246 -Back $Palette.Panel -Fore $Palette.Text
$script:btnDiagDir = New-UiButton -Text "打开诊断目录" -X 170 -Y 246 -Back $Palette.Panel -Fore $Palette.Text

$diagLabel = New-Object System.Windows.Forms.Label
$diagLabel.Text = "系统诊断（版本 / 数据库 / 端口 / 数据时间 / 错误日志 / 一键诊断位置）"
$diagLabel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$diagLabel.ForeColor = $Palette.Muted
$diagLabel.Location = New-Object System.Drawing.Point(16, 290)
$diagLabel.AutoSize = $true
$diagLabel.BackColor = [System.Drawing.Color]::Transparent

$script:diagBox = New-Object System.Windows.Forms.TextBox
$script:diagBox.Multiline = $true
$script:diagBox.ReadOnly = $true
$script:diagBox.ScrollBars = "Vertical"
$script:diagBox.WordWrap = $false
$script:diagBox.BackColor = [System.Drawing.Color]::FromArgb(2, 6, 23)
$script:diagBox.ForeColor = [System.Drawing.Color]::FromArgb(186, 230, 253)
$script:diagBox.BorderStyle = "FixedSingle"
$script:diagBox.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$script:diagBox.Location = New-Object System.Drawing.Point(16, 308)
$script:diagBox.Size = New-Object System.Drawing.Size(608, 92)
$script:diagBox.Anchor = "Top,Left,Right"

$logLabel = New-Object System.Windows.Forms.Label
$logLabel.Text = "运行日志（最近 200 行）"
$logLabel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$logLabel.ForeColor = $Palette.Muted
$logLabel.Location = New-Object System.Drawing.Point(16, 410)
$logLabel.AutoSize = $true
$logLabel.BackColor = [System.Drawing.Color]::Transparent

$script:logBox = New-Object System.Windows.Forms.TextBox
$script:logBox.Multiline = $true
$script:logBox.ReadOnly = $true
$script:logBox.ScrollBars = "Vertical"
$script:logBox.WordWrap = $false
$script:logBox.BackColor = [System.Drawing.Color]::FromArgb(2, 6, 23)
$script:logBox.ForeColor = [System.Drawing.Color]::FromArgb(203, 213, 225)
$script:logBox.BorderStyle = "FixedSingle"
$script:logBox.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$script:logBox.Location = New-Object System.Drawing.Point(16, 430)
$script:logBox.Size = New-Object System.Drawing.Size(608, 150)
$script:logBox.Anchor = "Top,Bottom,Left,Right"

$script:statusBar = New-Object System.Windows.Forms.Label
$script:statusBar.Text = "就绪"
$script:statusBar.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$script:statusBar.ForeColor = $Palette.Muted
$script:statusBar.Location = New-Object System.Drawing.Point(16, 588)
$script:statusBar.Size = New-Object System.Drawing.Size(608, 26)
$script:statusBar.Anchor = "Bottom,Left,Right"
$script:statusBar.BackColor = [System.Drawing.Color]::Transparent

$form.Controls.AddRange(@(
    $script:btnStart, $script:btnStop, $script:btnOpen, $script:btnDocs,
    $script:btnRestart, $script:btnLogs, $script:btnShortcut, $script:btnRefresh,
    $script:btnDiag, $script:btnDiagDir, $diagLabel, $script:diagBox,
    $logLabel, $script:logBox, $script:statusBar
))

# ──────────────── 状态刷新 ────────────────

function Set-CardState {
    param($Card, [System.Drawing.Color]$Color, [string]$Detail)
    $Card.Dot.ForeColor = $Color
    $Card.Detail.Text = $Detail
}

function Update-Ui {
    $busy = Get-BusyProcess

    $backendUp = Test-PortListening -Port $BackendPort
    $frontendUp = Test-PortListening -Port $FrontendPort
    $ready = $false
    if ($backendUp) { $ready = Test-BackendReady }

    if ($ready) {
        Set-CardState $script:backendCard $Palette.Ok "已就绪 · /api/health/ready 返回 200"
    } elseif ($backendUp) {
        Set-CardState $script:backendCard $Palette.Warn "端口已监听，正在初始化数据库 / 交易日历…"
    } else {
        Set-CardState $script:backendCard $Palette.Bad "未运行"
    }

    if ($frontendUp) {
        Set-CardState $script:frontendCard $Palette.Ok "已运行 · $FrontendUrl"
    } else {
        Set-CardState $script:frontendCard $Palette.Bad "未运行"
    }

    $text = Read-LauncherLog
    if ($text -ne $script:lastLogText) {
        $script:lastLogText = $text
        $script:logBox.Text = $text
        $script:logBox.SelectionStart = $script:logBox.TextLength
        $script:logBox.ScrollToCaret()
    }

    # 诊断区块：首次立即刷新，之后每 20 秒一次（git / HTTP 探针不适合每 1.5 秒跑）
    $now = Get-Date
    if (-not $script:diagLastUpdate -or ($now - $script:diagLastUpdate).TotalSeconds -gt 20) {
        $script:diagLastUpdate = $now
        $script:diagBox.Text = (Get-DiagnosticsLines) -join "`r`n"
    }

    foreach ($button in @($script:btnStart, $script:btnStop, $script:btnRestart, $script:btnRefresh)) {
        $button.Enabled = -not $busy
    }

    if ($busy) {
        $elapsed = [int]((Get-Date) - $script:actionStartedAt).TotalSeconds
        $script:statusBar.Text = "$($script:actionLabel)中… 已用 $elapsed 秒（首次启动要装依赖 + 跑迁移，可能 1~2 分钟）"
        $script:statusBar.ForeColor = $Palette.Warn
    } else {
        $script:statusBar.Text = $script:message
        $script:statusBar.ForeColor = $Palette.Muted
    }
}

function Invoke-Tick {
    $proc = $script:actionProc
    if ($proc -and $proc.HasExited) {
        $label = $script:actionLabel
        $script:actionProc = $null
        $script:actionLabel = ""
        $script:message = Get-FinishMessage -Label $label
    }
    Update-Ui
}

# ──────────────── 事件绑定 ────────────────

$script:message = "就绪。点「一键启动」拉起后端与前端。"
$script:lastLogText = $null
$script:diagLastUpdate = $null
$script:actionProc = $null
$script:actionLabel = ""
$script:actionStartedAt = Get-Date

$script:btnStart.Add_Click({ Invoke-StartPlatform })
$script:btnStop.Add_Click({ Invoke-StopPlatform })
$script:btnRestart.Add_Click({
    if (-not (Test-PortListening -Port $BackendPort) -and
        -not (Test-PortListening -Port $FrontendPort)) {
        Start-BackgroundScript -ScriptName "start_all.ps1" -Label "启动"
    } else {
        Start-BackgroundScript -ScriptName "restart_all.ps1" -Label "重启"
    }
})
$script:btnOpen.Add_Click({ Open-Url -Url (Get-PlatformUrl) -NeedPort $BackendPort })
$script:btnDocs.Add_Click({ Open-Url -Url $DocsUrl -NeedPort $BackendPort })
$script:btnLogs.Add_Click({ Start-Process explorer.exe $RunDir | Out-Null })
$script:btnShortcut.Add_Click({ Show-ShortcutResult })
$script:btnRefresh.Add_Click({ $script:message = "已刷新状态。"; $script:diagLastUpdate = $null; Update-Ui })
$script:btnDiag.Add_Click({
    try {
        $file = Export-Diagnostics
        $script:message = "诊断包已导出：$file"
    } catch {
        $script:message = "导出诊断包失败：$($_.Exception.Message)"
    }
    Update-Ui
})
$script:btnDiagDir.Add_Click({
    $dir = Get-DiagnosticsDir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Start-Process explorer.exe $dir | Out-Null
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({ Invoke-Tick })
$timer.Start()

Update-Ui

if ($Action) {
    # 无界面动作模式：与按钮点击走同一条代码路径（Start-BackgroundScript + Get-FinishMessage），
    # 但把交互式确认换成明确的行为，便于自动化验收。
    $scriptName = switch ($Action) {
        "start" { "start_all.ps1" }
        "stop" { "stop_all.ps1" }
        "restart" { "restart_all.ps1" }
    }
    $label = switch ($Action) {
        "start" { "启动" }
        "stop" { "停止" }
        "restart" { "重启" }
    }
    Write-Output "ACTION $Action -> scripts\$scriptName"
    $timer.Stop()
    $form.Dispose()

    $actLog = Join-Path $RunDir "launcher-action-$Action.log"
    $actErr = Join-Path $RunDir "launcher-action-$Action.err.log"
    # 与按钮路径保持一致的正式模式判定
    $actArgs = ""
    if ($Action -in @("start", "restart")) {
        $distIndex = Join-Path $RootDir "frontend\dist\index.html"
        if (Test-Path -LiteralPath $distIndex) { $actArgs = " -Prod" }
    }
    $proc = Start-Process -FilePath $WinPs `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$RootDir\scripts\$scriptName`"$actArgs" `
        -WorkingDirectory $RootDir -WindowStyle Hidden `
        -RedirectStandardOutput $actLog -RedirectStandardError $actErr -PassThru
    Write-Output "子进程 PID=$($proc.Id)"
    $finished = $proc.WaitForExit($ActionTimeoutSec * 1000)
    Write-Output "子进程已退出=$finished"

    # 等可观测结果稳定（端口/就绪探针），而不是依赖退出码
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($Action -eq "stop") {
            if (-not (Test-PortListening -Port $BackendPort) -and -not (Test-PortListening -Port $FrontendPort)) { $ok = $true; break }
        } else {
            if ((Test-PortListening -Port $BackendPort) -and (Test-BackendReady)) { $ok = $true; break }
        }
        Start-Sleep -Seconds 2
    }
    $message = Get-FinishMessage -Label $label
    Write-Output "RESULT $message"
    Write-Output "OBSERVED backend_listening=$(Test-PortListening -Port $BackendPort) backend_ready=$(Test-BackendReady) frontend_listening=$(Test-PortListening -Port $FrontendPort) stable=$ok"
    if ($Action -ne "stop") {
        Write-Output "UI_URL $(Get-PlatformUrl)"
    }
    Write-Output "--- 动作日志（尾部）---"
    if (Test-Path $actLog) { Get-Content $actLog -Encoding UTF8 | Select-Object -Last 12 }
    if (Test-Path $actErr) {
        $errLines = Get-Content $actErr -Encoding Default -ErrorAction SilentlyContinue | Where-Object { "$_".Trim() -ne "" }
        if ($errLines) { Write-Output "--- 动作 stderr（尾部）---"; $errLines | Select-Object -Last 6 }
    }
    if ($ok) { exit 0 } else { exit 1 }
}

if ($Diagnose) {
    foreach ($line in (Get-DiagnosticsLines)) { Write-Output $line }
    $timer.Stop()
    $form.Dispose()
    return
}

if ($SelfTest) {
    $controls = $form.Controls.Count
    $buttons = @($form.Controls | Where-Object { $_ -is [System.Windows.Forms.Button] }).Count
    Write-Output "SELFTEST OK"
    Write-Output "  工作目录       : $RootDir"
    Write-Output "  顶层控件数     : $controls"
    Write-Output "  按钮数         : $buttons"
    Write-Output "  后端端口监听   : $(Test-PortListening -Port $BackendPort)"
    Write-Output "  前端端口监听   : $(Test-PortListening -Port $FrontendPort)"
    Write-Output "  后端就绪探针   : $(Test-BackendReady)"
    Write-Output "  诊断区块控件   : $($script:diagBox -ne $null)"
    Write-Output "--- 诊断 ---"
    foreach ($line in (Get-DiagnosticsLines)) { Write-Output "  $line" }
    $timer.Stop()
    $form.Dispose()
    return
}

[void]$form.ShowDialog()
$timer.Stop()
$form.Dispose()
