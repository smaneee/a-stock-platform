# 在桌面创建「A 股量化平台」启动器快捷方式（缺图标时自动生成）。
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts/create_desktop_shortcut.ps1
# 可选: -Name "A 股量化平台"  -Force（重新生成图标与快捷方式）
#
# 本文件必须保存为 UTF-8 with BOM。
[CmdletBinding()]
param(
    [string]$Name = "A 股量化平台",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$RootDir = Split-Path -Parent $PSScriptRoot
$LauncherPs1 = Join-Path $PSScriptRoot "launcher.ps1"
$IconPath = Join-Path $PSScriptRoot "launcher.ico"

if (-not (Test-Path -LiteralPath $LauncherPs1)) {
    throw "找不到启动器脚本：$LauncherPs1"
}

# ──────────────── 图标 ────────────────

function New-LauncherIcon {
    # 注意：参数不能叫 $Path —— PowerShell 变量名不区分大小写，会与下面的
    # $roundPath 之外任何 $path 写法冲突，且 [string] 类型会把 GraphicsPath 转成字符串。
    param([string]$OutFile)

    $size = 256
    $bmp = New-Object System.Drawing.Bitmap(
        $size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    )
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # 圆角底板
    # 注意：逗号在 PowerShell 里比算术运算符结合得更紧，括号里不能直接写
    # "$size - 2 * $inset" 这类表达式（会被当成数组做大数运算），先算好再传。
    $inset = 8
    $inner = $size - 2 * $inset
    $rect = New-Object System.Drawing.Rectangle -ArgumentList @($inset, $inset, $inner, $inner)
    $d = 88
    $arcRight = $rect.Right - $d
    $arcBottom = $rect.Bottom - $d
    $roundPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $roundPath.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
    $roundPath.AddArc($arcRight, $rect.Y, $d, $d, 270, 90)
    $roundPath.AddArc($arcRight, $arcBottom, $d, $d, 0, 90)
    $roundPath.AddArc($rect.X, $arcBottom, $d, $d, 90, 90)
    $roundPath.CloseFigure()

    $bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        $rect,
        [System.Drawing.Color]::FromArgb(255, 17, 34, 64),
        [System.Drawing.Color]::FromArgb(255, 6, 12, 26),
        45.0
    )
    $g.FillPath($bg, $roundPath)

    # 走势折线的坐标点
    $pts = @(
        (New-Object System.Drawing.PointF(48, 184)),
        (New-Object System.Drawing.PointF(92, 142)),
        (New-Object System.Drawing.PointF(126, 166)),
        (New-Object System.Drawing.PointF(164, 104)),
        (New-Object System.Drawing.PointF(210, 60))
    )

    # 折线下方的渐变面积
    $areaPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $areaPath.AddLines($pts)
    $areaPath.AddLine($pts[-1].X, $pts[-1].Y, $pts[-1].X, 214)
    $areaPath.AddLine($pts[-1].X, 214, $pts[0].X, 214)
    $areaPath.CloseFigure()
    $areaBrush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Point(0, 40)),
        (New-Object System.Drawing.Point(0, 214)),
        [System.Drawing.Color]::FromArgb(150, 56, 189, 248),
        [System.Drawing.Color]::FromArgb(0, 56, 189, 248)
    )
    $g.FillPath($areaBrush, $areaPath)

    # 折线本体
    $linePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 56, 189, 248), 13)
    $linePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $linePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $linePen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $g.DrawLines($linePen, $pts)

    # 末端高亮点
    $g.FillEllipse(
        (New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 52, 211, 153))),
        198, 48, 26, 26
    )

    $stream = New-Object System.IO.MemoryStream
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $stream.ToArray()

    $file = [System.IO.File]::Create($OutFile)
    $writer = New-Object System.IO.BinaryWriter($file)
    try {
        # ICONDIR
        $writer.Write([UInt16]0)          # reserved
        $writer.Write([UInt16]1)          # type = icon
        $writer.Write([UInt16]1)          # 图像数量
        # ICONDIRENTRY（宽高写 0 表示 256）
        $writer.Write([Byte]0)
        $writer.Write([Byte]0)
        $writer.Write([Byte]0)            # 调色板数
        $writer.Write([Byte]0)            # reserved
        $writer.Write([UInt16]1)          # color planes
        $writer.Write([UInt16]32)         # bits per pixel
        $writer.Write([UInt32]$png.Length)
        $writer.Write([UInt32]22)         # 图像数据偏移 = 6 + 16
        $writer.Write($png)
    } finally {
        $writer.Flush()
        $writer.Dispose()
        $file.Dispose()
        $stream.Dispose()
        $g.Dispose()
        $bmp.Dispose()
    }
}

$iconStatus = "复用已有图标"
if ($Force -or -not (Test-Path -LiteralPath $IconPath)) {
    New-LauncherIcon -OutFile $IconPath
    $iconStatus = "已生成图标"
}

# ──────────────── 快捷方式 ────────────────

$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
    throw "找不到桌面目录，无法创建快捷方式。"
}
$linkPath = Join-Path $desktop "$Name.lnk"

$winPs = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path $winPs)) { $winPs = "powershell.exe" }

$shell = New-Object -ComObject WScript.Shell
try {
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $winPs
    # -WindowStyle Hidden：只显示启动器窗口，不额外弹一个控制台黑框
    $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $LauncherPs1 + '"'
    $link.WorkingDirectory = $RootDir
    $link.IconLocation = "$IconPath,0"
    $link.Description = "A 股量化平台 · 一键启停（后端 :8000 / 前端 :5173）"
    $link.WindowStyle = 1
    $link.Save()
} finally {
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

if (-not (Test-Path -LiteralPath $linkPath)) {
    throw "快捷方式写入失败：$linkPath"
}

Write-Output "$iconStatus：$IconPath"
Write-Output "快捷方式已创建：$linkPath"
Write-Output "启动器脚本：$LauncherPs1"
Write-Output "工作目录：$RootDir"
