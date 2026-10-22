#Requires -Version 5.1
<#
.SYNOPSIS
    在 Windows 上本地构建 CWQ.exe（财务记账系统桌面壳）。

.DESCRIPTION
    这份脚本做六件事，每一步失败都立刻停：

      1. 建/复用构建虚拟环境（.venv-build）
      2. 按锁定版本装 PyInstaller
      3. 生成图标（纯标准库，无需额外依赖）
      4. 跑单元测试 —— **构建前就红**，好过打完包才发现逻辑坏了
      5. PyInstaller 打包
      6. 可选：用 Inno Setup 出安装包

    第 4 步不是形式：PyInstaller 对 Python 语法的错误很宽容，
    打包成功不代表跑得起来。测试放在打包前面，绿了再打。

.PARAMETER Onedir
    打成目录模式（dist\CWQ\）而不是单文件（dist\CWQ.exe）。
    单文件每次启动都解包到临时目录，是杀软「内存写入」启发式的靶子；
    真被拦了就用这个开关重来。

.PARAMETER SkipInstaller
    只出 exe，不出安装包（没装 Inno Setup 时用）。

.EXAMPLE
    .\build_windows.ps1
    .\build_windows.ps1 -Onedir
#>
[CmdletBinding()]
param(
    [switch]$Onedir,
    [switch]$SkipInstaller,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)

function Say([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host "!! $Message" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 1. 虚拟环境
$venv = Join-Path $ScriptDir ".venv-build"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Say "建构建虚拟环境：$venv"
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "建虚拟环境失败（python -m venv 退出码 $LASTEXITCODE）" }
} else {
    Say "复用构建虚拟环境：$venv"
}
$py = Join-Path $venv "Scripts\python.exe"

# ---------------------------------------------------------------- 2. 依赖
Say "装打包依赖（按 requirements-build.txt 定版）"
& $py -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip 自升级失败" }
& $py -m pip install --quiet -r (Join-Path $ScriptDir "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { Fail "装 PyInstaller 失败" }
& $py -m PyInstaller --version

# ---------------------------------------------------------------- 3. 图标
Say "生成图标"
& $py (Join-Path $ScriptDir "make_icon.py") (Join-Path $ScriptDir "shell.ico")
if ($LASTEXITCODE -ne 0) { Fail "生成图标失败" }

# ---------------------------------------------------------------- 4. 测试
Say "跑单元测试"
& $py -m unittest discover -s (Join-Path $ScriptDir "tests") -t (Join-Path $ScriptDir "tests")
if ($LASTEXITCODE -ne 0) { Fail "单元测试没过 —— 不打包装东西出来" }

# ---------------------------------------------------------------- 5. 打包
Say "PyInstaller 打包（$(if ($Onedir) { '目录模式' } else { '单文件模式' })）"
if ($Onedir) {
    $env:CWQ_SHELL_ONEDIR = "1"
} else {
    Remove-Item Env:CWQ_SHELL_ONEDIR -ErrorAction SilentlyContinue
}

# --distpath / --workpath 显式给死：不显式给的话它们相对于**当前工作目录**，
# 于是「在仓库根跑」和「在脚本目录跑」会产出到不同地方，
# 而下游（安装包脚本、冒烟验证、CI）都指望产物在固定位置。
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath (Join-Path $ScriptDir "dist") `
    --workpath (Join-Path $ScriptDir "build") `
    (Join-Path $ScriptDir "launcher.spec")
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 打包失败" }

$exe = Join-Path $ScriptDir "dist\CWQ.exe"
if (-not (Test-Path $exe)) {
    # 目录模式下主程序在 dist\CWQ\CWQ.exe
    $exe = Join-Path $ScriptDir "dist\CWQ\CWQ.exe"
}
if (-not (Test-Path $exe)) { Fail "打包结束但找不到 exe" }
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Say "产出：$exe （$sizeMb MB）"

# ---------------------------------------------------------------- 6. 冒烟
Say "冒烟验证（真跑一次 exe）"
& $py (Join-Path $ScriptDir "smoke_test.py") $exe
if ($LASTEXITCODE -ne 0) { Fail "冒烟验证没过 —— 别把这份产物发出去" }

# ---------------------------------------------------------------- 7. 安装包
if (-not $SkipInstaller) {
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $iscc) {
        Say "没找到 Inno Setup（ISCC.exe）—— 跳过安装包。装好 Inno Setup 6 后重跑即可。"
    } else {
        Say "用 Inno Setup 出安装包：$iscc"
        Push-Location $ScriptDir
        & $iscc "installer.iss"
        $rc = $LASTEXITCODE
        Pop-Location
        if ($rc -ne 0) { Fail "Inno Setup 编译失败（退出码 $rc）" }
        Say "安装包：$(Join-Path $RepoRoot 'dist-installer\CWQ-Setup.exe')"
    }
}

Say "完成。"
