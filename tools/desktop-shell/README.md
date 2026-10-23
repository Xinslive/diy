# 桌面壳（CWQ.exe）

把 https://acct.xins.live 开成一个**独立窗口**的 Windows 程序。

```
双击 CWQ.exe
   └─ 弹出一个没有地址栏、没有标签页的窗口
      里面就是财务记账系统
```

---

## 1. 它是什么，不是什么

| 是 | 不是 |
|---|---|
| 一个 exe，双击打开线上站点 | 桌面版（不带离线数据） |
| 后端、数据库、部署全在服务器上 | 改了前端逻辑的东西 |
| 线上站点更新了，壳里的内容跟着更新 | 需要用户自己装浏览器的安装包 |
| 零运行时第三方依赖（只用标准库） | 一个需要长期维护的分支 |

**它不碰业务代码。** `backend/` 和 `frontend/` 一行都不用改。

---

## 2. 为什么是这个形态，而不是别的

这个壳的形态不是随手挑的，几条约束互相咬合：

### 2.1 不用内嵌 WebView 壳（pywebview / Electron / Tauri）

看起来「内嵌自己的窗口」更像一个正经桌面程序，而且不要求用户装浏览器。
但**本项目的凭证打印走的是** `window.open()` + `w.print()`
（`frontend/src/api/print.js` 的 `openPrintWindow` / `printWindow`），
而 **WebView2 里 `window.print()` 不弹打印对话框**（浏览器闪一下就结束），
`window.open` 也默认被拦截、必须自己接 `NewWindowRequested`。

换句话说：**换成 WebView 壳，凭证打印这个核心功能直接废掉。**
要救回来就得把打印链路改成「前端调 IPC、主进程调 `ShowPrintUI`」——
那已经不是封装，是重做。

所以这里用的是**真正的浏览器内核**（Edge/Chrome 的 `--app=` 模式）：
打印、下载、多标签一切照旧。

完整分析见 [`../../docs/Windows打包exe可行性研究.md`](../../docs/Windows打包exe可行性研究.md)。

### 2.2 用 `--app=` 而不是裸的 `webbrowser.open()`

裸 `webbrowser.open()` 会打开用户平时的浏览器，带地址栏、有标签页——
用户会说「这不就是个快捷方式吗」。`--app=` 模式开出来的是**独立窗口**：
无地址栏、无标签页，看起来就是个本地程序。

### 2.3 用**独立**的浏览器 profile

`--user-data-dir` 指向 `%LOCALAPPDATA%\财务记账系统\browser-profile`，
不挂用户日常浏览器的 profile。三个理由：

1. **登录态。** JWT 存在 localStorage，而 localStorage 属于 profile。
   独立 profile 里登录一次，之后一直有效。
2. **不打扰用户。** 挂在日常 profile 上的应用窗口，浏览器可能顺手弹
   「恢复上次会话」，或者让这个窗口跟着用户的其它标签页一起退出。
3. **可预期。** 壳的行为不取决于用户平时用什么浏览器、装了什么扩展。

用 `LOCALAPPDATA` 而不是 `APPDATA`：前者是「应用私有、不随域账号漫游」
的那一半。profile 跟着漫游会出问题。

### 2.4 默认单文件，可切目录模式

单文件（`--onefile`）的交付物就是一个 exe，用户双击即用。
但单文件每次启动都解包到临时目录，既慢，又是杀软「内存写入」启发式的靶子。

所以留了开关：被拦了、或者用户反馈启动慢，改一个环境变量就能切成目录模式
重来（`CWQ_SHELL_ONEDIR=1`），不用改 spec 文件。

### 2.5 只用标准库

这个壳的全部价值是「不拖累」。多一个第三方依赖就多一份供应链面。
浏览器发现、参数拼装、拉进程，标准库全都干得了；图标也是手写 PNG/ICO
编码器生成的（`make_icon.py`）。

---

## 3. 文件结构

```
tools/desktop-shell/
├── launcher.py            壳的全部逻辑（纯标准库，631 行）
├── launcher.spec          PyInstaller 打包描述
├── version_info.txt       VERSIONINFO（CompanyName/ProductName/…）
├── make_icon.py           纯标准库生成 .ico（7 个尺寸）
├── smoke_test.py          对构建产物跑冒烟验证
├── build_windows.ps1      Windows 本地构建（含安装包）
├── installer.iss          Inno Setup 安装包脚本
├── requirements-build.txt 构建期依赖（只有 PyInstaller，版本钉死）
├── requirements-build.lock 构建期依赖的**哈希锁定版**（CI 与本地都用它）
├── shell.json.example     配置示例
├── shell.ico              图标（生成物，也入库，方便换品牌图标）
└── tests/test_launcher.py 单元测试（66 条）
```

---

## 4. 构建

### 4.1 Windows 本地构建

需要 Python 3.11+ 与 [Inno Setup 6](https://jrsoftware.org/isdl.php)。

```powershell
cd tools/desktop-shell
.\build_windows.ps1
```

脚本按顺序做六件事，任一步失败立刻停：

1. 建/复用构建虚拟环境 `.venv-build`
2. 按 `requirements-build.lock` **哈希锁定**装 PyInstaller
3. 生成图标
4. **跑单元测试** —— 构建前就红，好过打完包才发现逻辑坏了
5. PyInstaller 打包 → `dist/CWQ.exe`
6. **冒烟验证** —— 真跑一次 exe，逐条断言它的行为

产物：

```
tools/desktop-shell/dist/CWQ.exe        单文件（默认）
dist-installer/CWQ-Setup.exe            安装包（装了 Inno Setup 才有）
```

目录模式：`.\build_windows.ps1 -Onedir`

### 4.2 CI

`.github/workflows/desktop-shell.yml`，两道闸：

- **单元测试**（ubuntu）：40 条，平台无关的纯逻辑
- **Windows 构建 + 冒烟**（windows-latest）：真出 exe、真跑一遍、真断言

产物作为 artifact 上传（`cwq-desktop-shell-exe`）。

### 4.3 冒烟验证在验什么

`smoke_test.py` 不是「编译通过就算绿」——它把 exe **跑起来**，用 `--dry-run`
让壳把「打算怎么做」写成 JSON，再逐条核对：

- exe 是个真的 Windows PE（`MZ` 头）
- 退出码 0、JSON 可解析
- 目标地址是 `https://acct.xins.live`
- 应用模式下参数里有 `--app=` 与 `--user-data-dir=`
- `--no-app-mode` 退回默认浏览器且参数为空
- `--browser <显式路径>` 走自定义分支
- profile 目录是**绝对路径**
- 非 http/https 的地址被拒（退出码 2）

本地也能手动跑：

```bash
python3 tools/desktop-shell/smoke_test.py tools/desktop-shell/dist/CWQ.exe
```

### 4.4 在 macOS / Linux 上交叉出 Windows exe（Wine）

**PyInstaller 不支持交叉编译** —— 它要的是目标平台的 bootloader。所以「在 macOS
上直接 `pyinstaller`」出来的永远是 Mach-O，不是 PE。

但**可以用 Wine 把整条 Windows 工具链跑起来**，从而在 macOS/Linux 上产出真
PE。2026-10-23 实测跑通，产物与 CI 出的同源：

```bash
# 1. Wine（Apple Silicon 用 osx64 构建，走 Rosetta；Linux 用官方包）
curl -fL -o wine.tar.xz \
  "https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.0_1/wine-stable-11.0_1-osx64.tar.xz"
tar xf wine.tar.xz
export WINEPREFIX=/tmp/wine-prefix
"wine-stable/Wine Stable.app/Contents/Resources/wine/bin/wine" wineboot -u

# 2. Windows 版 CPython（NuGet 的便携包，解压即用，无需安装器）
curl -fL -o python.nupkg "https://www.nuget.org/api/v2/package/python/3.11.9"
unzip -q python.nupkg -d py && cp -R py/tools/. "$WINEPREFIX/drive_c/Python311/"

# 3. 装 PyInstaller —— 用**同一个哈希锁文件**，走的是 Windows 的 pip，
#    解析出来的 wheel 与 CI 完全一致
W="wine-stable/Wine Stable.app/Contents/Resources/wine/bin/wine"
"$W" 'C:\Python311\python.exe' -m pip install --require-hashes --only-binary=:all: \
    -r 'C:\cwq\requirements-build.lock'

# 4. 打包（spec 用 SPECPATH 定位自身，目录层级随意）
"$W" 'C:\Python311\python.exe' -m PyInstaller --noconfirm --clean \
    --distpath 'C:\cwq\dist' --workpath 'C:\cwq\build' 'C:\cwq\launcher.spec'

# 5. 冒烟 —— **在 Wine 里跑**，跑的是真的 PE
"$W" 'C:\Python311\python.exe' 'C:\cwq\smoke_test.py' 'C:\cwq\dist\CWQ.exe'
```

三个必须知道的坑：

- **Wine 里没有 X11 也行**，但 `WINEDLLOVERRIDES="mshtml,mscoree="` 要设上 ——
  否则任何程序想装 Gecko/Mono 时会弹交互框，在非交互环境里**永久挂住**。
- **Wine 默认 locale 是 cp936**，pip 会用系统默认编码读 requirements 文件，
  中文注释直接 `UnicodeDecodeError`。锁文件第一行的 `# -*- coding: utf-8 -*-`
  （PEP 263）就是为此存在的，**不能删、也不能挪到第三行往后**。
- **spec 里的死代码会咬人**：原先有一行 `REPO_ROOT = SPEC_DIR.parents[1]`，
  目录一浅就 `IndexError`，报错还指向 spec 第 44 行 —— 与「打包」毫无关系。
  已删。

### 4.5 跑测试

```bash
# 零依赖
python3 -m unittest discover -s tools/desktop-shell/tests -t tools/desktop-shell/tests -v

# 或用仓库那套 pytest
python3 -m pytest tools/desktop-shell/tests
```

### 4.6 验证「真的把浏览器拉起来」

**为什么 `--dry-run` 不够**：它在 `main()` 里是**提前返回**的，压根走不到
`spawn()`。也就是说「CI 的 dry-run 全绿」并不等于「用户双击能开窗」——
中间那一段本来是没有闸的。

2026-10-23 在 Wine 下用「替身浏览器」把这段补上了，方法是：

1. 用 PyInstaller 打一个**真 exe** 替身（必须是真 exe：Windows 会校验 PE 头，
   `.bat` 改名不行），让它把自己的 `sys.argv` 写成 UTF-8 JSON；
2. 把它放到**真实的 Edge 安装位置**
   `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；
3. **不带任何参数**运行 exe（就是双击那一下），看替身有没有收到参数。

实测结果（Wine + 真 Windows Python 3.11.9 + 真 PE）：

- `find_browser()` 在真实安装路径上找到了 Edge，**真的 spawn 成功**，退出码 0；
- 替身收到的参数与 dry-run 计划**逐字一致**，其中
  `--user-data-dir=C:\users\home\AppData\Local\财务记账系统\browser-profile`
  的**中文路径完整无损** —— 没有被 ANSI 编码破坏；
- 壳的日志落到了 `%LOCALAPPDATA%\财务记账系统\logs\shell.log`
  （中文目录名正常）。

对应地，仓库里补了 `SpawnTest` / `MainLaunchTest` 两组用例，把
「参数怎么交给 `Popen`」和「实际 spawn 的参数 == dry-run 计划」钉死。
`spawn()` 是整个壳唯一的副作用出口，它坏掉的症状是「双击什么也不发生」——
没有报错、没有弹窗，比崩溃还难查。

> 边界：Wine ≠ Windows 内核。CI 在 `windows-latest` 上跑的是 `--dry-run`，
> **并没有**验真浏览器；上面这个实验验到的是「参数确实被交出去了」。
> `MessageBoxW`、真实杀软行为仍属推断。

---

## 5. 配置

`shell.json` 放在 **CWQ.exe 旁边**，格式见 `shell.json.example`：

```json
{
  "url": "https://acct.xins.live",
  "window_size": "1360,880",
  "browser_args": ["--lang=zh-CN"]
}
```

所有字段都可省略。优先级：**命令行 > 配置文件 > 内置默认**。

> ⚠️ 只有**紧挨着 exe** 的那一份会被读。不放进用户目录、不放进开始菜单——
> 放进可被第三方改写的地方，等于允许别人改这个壳要打开哪个站点。
> 临时换个地址用 `--url` 参数，别改这个文件。

### 命令行参数

| 参数 | 作用 |
|---|---|
| `--url <地址>` | 要打开的站点（只允许 http/https） |
| `--no-app-mode` | 不用应用模式窗口，改用系统默认浏览器 |
| `--browser <路径>` | 指定浏览器可执行文件 |
| `--window-size <宽,高>` | 应用窗口尺寸 |
| `--browser-arg <参数>` | 追加给浏览器的原始参数，可重复 |
| `--dry-run` | 只打印「打算怎么做」的 JSON，不真的打开 |
| `--plan-out <文件>` | 把 dry-run 的 JSON 写到文件 |

---

## 6. 运行时落盘的东西

| 位置 | 内容 |
|---|---|
| `%LOCALAPPDATA%\财务记账系统\browser-profile\` | 浏览器独立 profile（含登录态） |
| `%LOCALAPPDATA%\财务记账系统\logs\shell.log` | 壳的日志 |

**不写注册表、不写 Program Files、不需要管理员权限。**

---

## 7. 分发与签名

### 7.1 当前已有的分发点

产物放在生产机的独立下载目录里，**不随发布目录被覆盖**：

```
生产机（8.155.171.57）：/www/wwwroot/acct.xins.live/downloads/<随机段>/CWQ.exe
公网地址：              https://acct.xins.live/downloads/<随机段>/CWQ.exe
```

- `<随机段>` 是一段 32 位十六进制随机串，**每次换产物都换**。
- nginx 侧是 `deploy/nginx/acct.xins.live.conf` 里的 `location /downloads/`，
  **`autoindex` 是关的** —— 开着索引等于把随机段公布出去，那随机路径就白设了。
  目录本身返回 403，按完整路径取文件正常。
- 生产 vhost 是**手工同步**的（发布脚本不碰它），换主机时记得同步这一段，
  并按目标机的目录布局改 `alias` 的路径。

见 [`../../docs/Windows打包exe可行性研究.md`](../../docs/Windows打包exe可行性研究.md) 的
「打包与分发」一节。三个要点：

1. **签代码签名证书。** 微软 2024 年起已明确：EV 证书**不再**提供 SmartScreen
   立即放行，现在基于文件哈希 + 签名者信誉累积。OV 证书 + 固定发布者身份
   才是现实解。签名要带 SHA256 + RFC 3161 时间戳，exe 和安装包都要签。
2. **填 VERSIONINFO。** 杀软误报申诉会检查 `CompanyName` / `FileDescription` /
   `FileVersion` —— `version_info.txt` 里已经填好，**发布前请把 CompanyName
   换成真实的发行主体**。
3. **提前准备误报申诉通道**（Defender WDSI / Avast / ESET / Symantec），
   并在干净的 Win10/11 VM 上**从浏览器下载**实测（内网共享目录不触发
   SmartScreen 判定）。

---

## 8. 已知限制

- **Windows only。** 逻辑本身跨平台（macOS/Linux 上也能跑，会用默认浏览器），
  但打包与验证只覆盖 Windows。
- **没有自动更新。** 壳指向的是线上站点，网站更新立刻生效；
  但**壳本身**的更新要靠重新分发新 exe。
- **任务栏图标是浏览器的图标，不是 CWQ 的。** `--app=` 模式开出来的窗口
  归浏览器管，Windows 任务栏上显示的是 Edge/Chrome 的图标。
  Chromium 在页面提供了 favicon 时会用 favicon 代替 —— 而当前的
  `frontend/index.html` **没有声明 favicon**，所以看到的是浏览器默认图标。
  要修的话是在 `frontend/index.html` 里加一行
  `<link rel="icon" href="/favicon.svg">` 并放一个图标文件；
  那属于改网站，不属于「封装」，所以这里没动，只记在这里。
- **断网时看到的是浏览器自己的错误页。** 壳不做预检连接 ——
  预检会引入误报（DNS、代理问题），而浏览器那句「无法访问此网站」
  用户本来就认识。
- **图标是占位的。** 正式发布前换成设计给的品牌图标：
  替换 `tools/desktop-shell/shell.ico` 即可（`make_icon.py` 会在缺失时重新生成，
  所以**别删掉它**，除非同时删掉 spec 里那段自动生成逻辑）。
- **`version_info.txt` 里的 CompanyName 是占位值**，发布前必须改。
- **「浏览器真的被拉起来」只在 Wine 下验过**（见 4.6），没在真实 Windows
  内核上验过。`MessageBoxW`、真实杀软行为仍属推断。

---

## 9. 相关文档

- [`../../docs/Windows打包exe可行性研究.md`](../../docs/Windows打包exe可行性研究.md)
  —— 完整技术分析：为什么是 L3、为什么不内嵌 WebView、L1/L2 的代价对比、
  以及本仓库里另外两条路（本地实例、内嵌 PostgreSQL）要做的话有多重。
