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
#
# **按 requirements-build.lock 装（哈希锁定），不是按 requirements-build.txt。**
#
# 仓库的规矩是「依赖一律 --require-hashes」：只钉版本号钉不住「钉的是哪一份
# 字节」—— 同一个版本号的 wheel 可以被重新打包上传，「pin 了 6.22.3」不等于
# 「装到的是审过的那份 6.22.3」。--only-binary=:all: 是 --require-hashes 的
# 必要搭配（哈希是按 wheel 文件本身算的，sdist 解包后才算得出来）。
#
# 锁文件里的哈希是按 **Windows x64 + CPython 3.11** 的 wheel 算的，换 Python
# 小版本就会对不上而直接失败 —— 这是故意的，构建环境必须与被审过的那份一致。
Say "装打包依赖（按 requirements-build.lock 哈希锁定）"
& $py -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip 自升级失败" }
& $py -m pip install --quiet --require-hashes --only-binary=:all: `
    -r (Join-Path $ScriptDir "requirements-build.lock")
if ($LASTEXITCODE -ne 0) { Fail "装 PyInstaller 失败（哈希校验没过就是装错了字节，见锁文件头部）" }
# 钉版本本身也要有断言：锁文件装对了，不等于装的是预期的那个版本。
$pyiVer = (& $py -m PyInstaller --version | Out-String).Trim()
if ($pyiVer -ne "6.22.3") {
    Fail "PyInstaller 版本不是锁定的 6.22.3（实际 '$pyiVer'）"
}
Write-Host "PyInstaller $pyiVer"

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

# ---------------------------------------------------------------- 7. 真实启动路径
#
# 冒烟验证只跑 --dry-run，而 --dry-run 在 main() 里是**提前返回**的 ——
# 它压根走不到 spawn()。所以「冒烟全绿」不等于「用户双击能开窗」：
# 中间那一段本来是没有闸的。
#
# 这一步**真的**跑一次不带参数的 exe（= 双击那一下），用一个「替身浏览器」
# 真 exe 装在**真实的 Edge 安装位置**接住参数。替身收到了参数 =
# 「发现 → 拉起 → 参数」整条路径都通了。
#
# 替身用**同一个** PyInstaller 打（版本与打包产物一致），打完由探针先验它
# 是不是真 PE —— .bat 改名会被 Windows 拒绝执行（错误 193）。
#
# 沙箱是**环境变量级**的（ProgramFiles(x86) / LOCALAPPDATA 指向临时目录），
# 所以不会碰到这台机器上真的 Edge，也不会写脏真的用户目录。
Say "验证「真的把浏览器拉起来」（真 spawn）"
$stubDir = Join-Path $ScriptDir ".probe-stub"
New-Item -ItemType Directory -Force -Path $stubDir | Out-Null
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name msedge `
    --distpath (Join-Path $stubDir "dist") `
    --workpath (Join-Path $stubDir "build") `
    --specpath $stubDir `
    (Join-Path $ScriptDir "stub_browser.py")
if ($LASTEXITCODE -ne 0) { Fail "打替身浏览器失败" }
$stub = Join-Path $stubDir "dist\msedge.exe"
if (-not (Test-Path $stub)) { Fail "替身浏览器没打出来：$stub" }
& $py (Join-Path $ScriptDir "launch_path_probe.py") --exe $exe --stub $stub
if ($LASTEXITCODE -ne 0) { Fail "真实启动路径验证没过 —— 别把这份产物发出去" }

# ---------------------------------------------------------------- 8. 安装包
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
