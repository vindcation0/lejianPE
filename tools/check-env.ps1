$ErrorActionPreference = 'SilentlyContinue'
$lines = New-Object System.Collections.Generic.List[string]

function Add-Line([string]$s) {
    $lines.Add($s)
    Write-Host $s
}

Add-Line "============================================"
Add-Line "  乐健抢课 - 环境自检"
Add-Line "============================================"
Add-Line ""

# ---------- 系统 ----------
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
Add-Line "[系统] $($os.Caption)  build $($os.BuildNumber)"
Add-Line "[内存] $([math]::Round($cs.TotalPhysicalMemory / 1GB, 1)) GB"
Add-Line ""

# ---------- Python ----------
Add-Line "[Python]"
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Add-Line "  路径: $($py.Source)"
    $ver = (& python --version 2>&1) -join ' '
    Add-Line "  版本: $ver"
} else {
    Add-Line "  未找到"
}
Add-Line ""

# ---------- 模拟器 ----------
Add-Line "[安卓模拟器]"
$emuDirs = @(
    "C:\Program Files\Netease\MuMuPlayer-12.0",
    "C:\Program Files\Netease\MuMu Player 12",
    "C:\Program Files\Netease\MuMuPlayer",
    "C:\LDPlayer\LDPlayer9",
    "C:\LDPlayer",
    "C:\Program Files\BlueStacks_nxt",
    "D:\Program Files\Netease\MuMuPlayer-12.0",
    "D:\LDPlayer\LDPlayer9"
)
$found = $false
foreach ($d in $emuDirs) {
    if (Test-Path $d) {
        Add-Line "  找到: $d"
        $found = $true
    }
}
if (-not $found) { Add-Line "  未检测到" }
Add-Line ""

# ---------- adb ----------
Add-Line "[adb]"
$adbPath = $null
$adbCmd = Get-Command adb -ErrorAction SilentlyContinue
if ($adbCmd) {
    $adbPath = $adbCmd.Source
} else {
    $roots = @("C:\Program Files\Netease", "C:\LDPlayer", "C:\Program Files\BlueStacks_nxt", "D:\Program Files\Netease", "D:\LDPlayer")
    foreach ($r in $roots) {
        if (Test-Path $r) {
            $hit = Get-ChildItem -Path $r -Filter adb.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($hit) { $adbPath = $hit.FullName; break }
        }
    }
}

if ($adbPath) {
    Add-Line "  路径: $adbPath"
    $adir = Split-Path $adbPath -Parent
    $ver = (& $adbPath version 2>&1) -join ' '
    Add-Line "  版本: $ver"
    Add-Line ""
    Add-Line "[连接状态]"
    $dev = (& $adbPath devices 2>&1) -join "`r`n"
    Add-Line $dev
    Add-Line ""
    Add-Line "  （出现 device 就是已连上；emulator-5554 这类也算）"
} else {
    Add-Line "  未找到"
    Add-Line "  （装模拟器后通常自带，届时重新运行本脚本即可）"
}
Add-Line ""

# ---------- 报告文件 ----------
Add-Line "============================================"
$report = Join-Path $PSScriptRoot "..\环境自检结果.txt"
$lines | Set-Content -Path $report -Encoding UTF8
Add-Line "  结果已保存: 环境自检结果.txt"
Add-Line "============================================"
