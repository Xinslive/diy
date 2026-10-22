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
├── launcher.py            壳的全部逻辑（纯标准库，536 行）
├── launcher.spec          PyInstaller 打包描述
├── version_info.txt       VERSIONINFO（CompanyName/ProductName/…）
├── make_icon.py           纯标准库生成 .ico（7 个尺寸）
├── smoke_test.py          对构建产物跑冒烟验证
├── build_windows.ps1      Windows 本地构建（含安装包）
├── installer.iss          Inno Setup 安装包脚本
├── requirements-build.txt 构建期依赖（只有 PyInstaller）
├── shell.json.example     配置示例
├── shell.ico              图标（生成物，也入库，方便换品牌图标）
└── tests/test_launcher.py 单元测试（40 条）
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
2. 按 `requirements-build.txt` 定版装 PyInstaller
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

### 4.4 跑测试

```bash
# 零依赖
python3 -m unittest discover -s tools/desktop-shell/tests -t tools/desktop-shell/tests -v

# 或用仓库那套 pytest
python3 -m pytest tools/desktop-shell/tests
```

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

---

## 9. 相关文档

- [`../../docs/Windows打包exe可行性研究.md`](../../docs/Windows打包exe可行性研究.md)
  —— 完整技术分析：为什么是 L3、为什么不内嵌 WebView、L1/L2 的代价对比、
  以及本仓库里另外两条路（本地实例、内嵌 PostgreSQL）要做的话有多重。
