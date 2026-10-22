; ===========================================================================
; 财务记账系统 桌面壳 —— Inno Setup 安装包脚本
;
; 装的是什么：一个 CWQ.exe。装完桌面上有个图标，双击打开
; https://acct.xins.live 到一个无地址栏的独立窗口里。
;
; 几个刻意的选择：
;
; **PrivilegesRequired=lowest + 装到 {localappdata}**
;     这个壳的体积以 MB 计，用户装它不需要管理员权限。要求提权会在
;     企业环境里多一道 IT 审批，而壳根本不写任何系统位置 ——
;     它的数据全在 %LOCALAPPDATA% 里。**不装到 Program Files**：
;     那里默认只读，profile 和日志写不进去。
;
; **AppId 必须固定**
;     Inno 用它判断「这台机器上是不是已经装过」。变了会被当成另一个产品，
;     于是旧版本装不上、卸载记录也乱。这里一旦定下就不要再改。
;
; **OutputBaseFilename 用 ASCII**
;     产物文件名保持 ASCII（CWQ-Setup.exe），中文只出现在用户可见的
;     AppName / Start Menu / 卸载列表里。中文文件名在 CI 的产物上传、
;     某些杀软与压缩工具上偶有玄学问题，没必要冒这个险。
;
; 编译：ISCC.exe installer.iss    （在 tools/desktop-shell 下）
; ===========================================================================

#define MyAppName "财务记账系统"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "财务记账系统"
#define MyAppExeName "CWQ.exe"

[Setup]
AppId={{8E39B5ED-4E2E-4216-B86A-F361874868DB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}

DefaultDirName={localappdata}\Programs\CWQ
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; 产物名保持 ASCII，中文名留给界面
OutputBaseFilename=CWQ-Setup
OutputDir=..\..\dist-installer
SetupIconFile=shell.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

; 压缩率优先：产物小一点，发给客户轻松些
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; 不需要管理员
PrivilegesRequired=lowest

; 只做 64 位。WebView2/Edge 在 64 位 Windows 11 上是系统的一部分，
; 而 32 位 Windows 早已停止支持 —— 不值得为它做一条分支。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; 整个 dist\ 目录照搬。
;
; 这样写而不是分别列 CWQ.exe 与 CWQ\ 目录，是因为**两种构建模式的产物形状
; 不同**：单文件模式是 dist\CWQ.exe 一个文件；目录模式是 dist\CWQ\CWQ.exe
; 加 dist\CWQ\_internal\。分别列就要加一个 Check 回调去判断当前是哪种，
; 而那份判断在构建脚本改一次模式时就可能失配。照搬整个目录则两种都对。
Source: "dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 桌面快捷方式
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; 开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即打开 {#MyAppName}"; Flags: nowait postinstall skipifsilent
