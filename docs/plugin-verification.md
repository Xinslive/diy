# 插件实施与验收记录

日期：2026-10-18。最新一轮为 2026-10-25 的审计修复轮（§3.1 逃逸封堵 · 逃生口收口 · SDK 契约归一 · **插件隔离模式阶段 2：宿主侧改走 IPC**），对应 [plugin-roadmap.md](plugin-roadmap.md) 与审计报告 `output/AUDIT-REPORT-2026-10-25.md`（该报告在 `output/`，不进版本库）；下方保留 SDK 1.2 定时调度、SDK 1.1 导入闭环和最初 `plan.md` P0–P4 的验收历史。各轮结果分别记录，历史表中的 SDK、迁移和测试数量不代表当前版本。本次未部署生产、未修改业务数据库和生产配置，插件及插件定时任务仍默认关闭。实际发布者及实际插件包的人工风险接受见 [plugin-risk.md](plugin-risk.md)。

## 2026-10-25 审计修复轮验收

对应审计报告 `output/AUDIT-REPORT-2026-10-25.md` 的 §3.1 与 §7 第 6 步（第 48/49/50 项）。

### §3.1：四条逃逸路径的**重新取证**

复核意见在 2026-10-25 给出四条主张。本轮**按它给的具体行号逐条重新取证**，结论是
**四条在当前代码上都不成立** —— 它读到的是修好之前的快照。两处可独立核对：它引用的
`sandbox.py:304-308` 在当前文件里是 `_make_symbol_guard.__getattr__` 而**不是** `_sanitize`；
它统计的工作区变更 90 个（57 改 + 33 新）也早于本轮当时的 98 个（61 改 + 37 新）。

| 复核主张 | 当前实测 | 位置 |
|---|---|---|
| `_sanitize(os) is os → True`（不在白名单的子模块原样返回） | `_sanitize(os)` **抛 ImportError**：「插件不允许使用模块：os」 | `backend/app/plugins/sandbox.py:435` |
| `scan_file()` 对命名空间走私判 CLEAN | `import dataclasses` + `dataclasses.sys` → 报 **PY_MODULE_ESCAPE**；`uuid.os`、`contextlib.os` 同 | `backend/app/plugins/security.py` |
| `__builtins__['__import__'].__globals__['builtins'].__import__('os')` 在运行期拿到真 os | **装载期即拒**：「插件代码使用了被禁用的双下划线反射（第 N 行）」 | `backend/app/plugins/sandbox.py::_refuse_reflection` |
| 没有任何用例覆盖「允许模块的命名空间把危险子模块带进来」 | 覆盖**存在**：20 组参数化（`dataclasses.sys` / `contextlib.os` / `uuid.os` / `json.codecs` / `typing.types` / `statistics.random` …）+ 7 组「清单内模块属性被重新包了一层」 | `backend/tests/test_plugin_sandbox.py:616` / `:636` |

运行期实测输出（探针在插件沙箱里逐条尝试）：

```
dataclasses_sys      → 'blocked: sys 对插件不可用'
uuid_os              → 'blocked: os 对插件不可用'
contextlib_os        → 'blocked: os 对插件不可用'
statistics_random    → 'blocked: random 对插件不可用'
typing_types         → 'blocked: types 对插件不可用'
__globals__ 链      → 拒装：插件代码使用了被禁用的双下划线反射（第 2 行）
__builtins__ 直取    → 拒装：插件代码使用了被禁用的双下划线反射（第 2 行）
```

反向验证：逐条拆掉防线（扫描规则改回放行、沙箱代理改回原样返回、装载期复核去掉），
对应用例**全部如期变红**；其中两次「仍然全绿」是**注入点没拆掉防线**（探针问题），
不是用例弱。

### 复审第二轮：主张仍然是旧快照，但**对抗性复现挖出两个真缺口**

复审又提了同一组主张（引的行号仍属旧快照）。按主张原话逐条实测，四条依旧不成立。
但这次把探针**往「静态扫描的盲区」方向**推了一步 —— 静态扫描**按形态判定**，
形态本身就是它的盲区，于是问了两条旧用例没问过的问题：

| 动态形态 | 静态扫描 | 装载 | 运行期 |
|---|---|---|---|
| `operator.attrgetter("sys")(dataclasses)`（属性名**无双下划线**） | `PY_ATTRIBUTE`（报的是执行器，不是逃逸属性） | 通过 | **AttributeError: attrgetter 对插件不可用** |
| `getattr(dataclasses, "s" + "ys")`（属性名**拼出来**） | `PY_CALL`（报的是被禁的名字） | 通过 | **NameError: name 'getattr' is not defined** |

两条在扫描期都判不到「逃逸模块属性」→ **运行期那一层是唯一防线**。

**真缺口一**：旧用例只测**字面量形态**，动态拼名形态没被钉住。
已补 `test_动态拼名形态的走私拿不到宿主模块`（6 组参数化）+ 
`test_动态拼名在扫描期不被当成逃逸_所以运行期那一层不能省`。

**真缺口二**：**「`_sanitize` 必须失败即闭」这条不变量没有任何用例**。
实测把 `_sanitize` 改回旧行为（`return module`），整个测试文件**照样全绿**。
已补 `test_沙箱的模块清洗对白名单外的模块失败即闭`，
以及「白名单内的模块拿到的必须是**代理**不是原模块」。

反向验证（三条注入都要先确认「真的拆掉了防线」才算数）：

| 注入 | 防线确认 | 结果 |
|---|---|---|
| `_sanitize` 还原成 `return module` | 注入后模块仍可导入 ✓ | **1 条红** |
| 运行期不再拒 `operator.attrgetter` | 注入后模块仍可导入 ✓ | **4 条红** |
| `getattr` 真正变可用（内建白名单**与**黑名单一起拆） | 两处一起拆后模块才导入成功 ✓ | **3 条红** |

> 第三条踩了坑：只把 `getattr` 塞进内建白名单时，`sandbox.py:187` 的
> 「内建白名单与黑名单冲突」检查**先一步**把模块拦下了 ——
> 那次注入**根本没生效**（模块都没加载），不算一次有效验证。
> **注入没生效的「绿」不能算数。**
> 同理，拆「模块代理拒白名单外模块属性」时，第一版改的是**第二道**闸
> （符号守卫），第一道（模块属性清洗）还在 → 照样全绿；
> 改对地方（还原成旧行为「原样返回」）才是 **28 条红**。

### 一条命令就能复核（不用人读代码）

上面的取证与变异证明都固化在仓库里：

```bash
python3 tools/audit_plugin_sandbox_recheck.py            # 四条主张的探针（快）
python3 tools/audit_plugin_sandbox_recheck.py --mutate   # 加跑变异证明（慢）
```

退出码 0 = 四条主张在当前代码上**都不成立**且**都被用例钉住**；非 0 = 至少一条成立。
脚本用**后端那套虚拟环境**自举（沙箱依赖 fastapi / pydantic，系统 python 里没有）。

| 主张 | 探针结果 | 变异证明（改回旧行为） |
|---|---|---|
| A `_sanitize` 原样返回 | 抛 ImportError（失败即闭） | **1 条红** |
| B `__import__.__globals__` 链 | 装载期即拒 | **2 条红** |
| C 命名空间走私无用例覆盖 | 覆盖存在（运行期参数化 + 静态扫描 + 动态拼名形态） | **28 条红** |
| D §7 第 48 项未实施 | 已实现（默认 `off`，首选形态可切 `on`） | **6 条红** |

### 逃生口收口：找到两个**未登记**的部署侧逃生口

复核指出「§3.8 / §3.44 的收紧各带一个显式豁免开关，默认是阻断，条目算修好，但生产
姿态目标会被这些口子削弱」。顺着这条查下去，发现**权威清单本身漏了两个**：

| 名字 | 性质 | 漏登记的后果 |
|---|---|---|
| `CWQ_ALLOW_ANY_HOST` | 跳过「本机是不是那台被授权的生产机」整道守卫 | 不在任何检查覆盖范围内 |
| `CWQ_HOST_IP` | **注入**守卫判本机身份用的那个 IP（看着像参数，其实是逃生口） | 同上；注成被授权的 IP 就绕过了守卫 |

原先 CI 那条断言只扫 `CWQ_DEPLOY_*` 前缀，两个都不带这个前缀 —— 正好从缝里漏过去。
现在改成扫 **deploy.sh 里出现的每个 `CWQ_*` 名字**（共 15 个），每个都必须登记为
逃生口 / 非逃生口开关 / 参数；`CWQ_HOST_IP` 另在脚本里**打一条 stderr 告警**
（豁免是设计成安静放行的，而这一条安静不得）。

**顺带修掉一个自己踩的坑**：告警最初写的是 `log`（stdout），而
`this_ip="$(host_public_ip)"` 是**命令替换** —— 告警行被一起捕获进 `this_ip`，
判据变成「告警文字 + IP」，标记文件比较必然不符，**生产机被误拦**。
目标机守卫那 4 条用例当场变红（症状正是「生产机被误拦了」），改走 stderr 后恢复。

### §7 第 6 步第 48 项：阶段 0 与阶段 1（进程隔离的第一段）

审计 §3.1 的定性是「修扫描器语义不够，**必须换隔离模型**」，而第 49 项在报告里
标注为「次选（**若必须进程内**）」。所以这一段做的是**真的换隔离模型**的第一段。

**阶段 0（共享模块 + 协议层）** 与 **阶段 1（worker 进程 + shadow 比对）** 已实施。
**对外行为一点没变** —— 插件的业务请求仍由进程内那份代码处理；阶段 1 只是让同一份
插件后端代码**在另一个进程里也跑一遍**，然后逐条比对两边声明了什么。

| 落地物 | 位置 | 验收 |
|---|---|---|
| SDK 契约的唯一一份 | `app/plugins/sdk_rules.py` | `tests/test_plugin_loader.py` |
| IPC 协议（帧/消息/错误映射/字节信封） | `app/plugins/ipc/protocol.py` | `tests/test_plugin_ipc_protocol.py` **39 条** |
| 序列化登记表（worker 也要读，所以抽成纯数据模块） | `app/plugins/ctx_surface.py` | `tests/test_plugin_sdk_rules.py` **9 条** |
| 签名描述与重建 | `app/plugins/ipc/signatures.py` | `tests/test_plugin_worker_isolation.py` |
| 声明形状（两侧同一份） | `app/plugins/ipc/declarations.py` | 同上 |
| worker 侧 registrar / ctx 代理 | `app/plugins/ipc/proxy_registrar.py`、`proxy_ctx.py` | 同上 |
| **插件后端代码的独立进程** | `app/plugins/ipc/worker_main.py` | 同上 **10 条** |
| 宿主侧起 worker + 逐条比对 | `app/plugins/ipc/shadow.py` | 同上 |

阶段 1 的四条验收各自证明**一件不同的事**：

1. **worker 的 PID ≠ 宿主的 PID** —— 插件代码真的不在宿主进程里。
2. **worker 只加载了 `app.plugins.*`** —— **没有** `app.core.db`、没有 `app.core.config`。
   这一条与「插件导不进来」是**两件事**：后者是**沙箱**的功劳，前者是**进程边界**的功劳 ——
   后者才是隔离模型的意义。
3. **插件在 worker 里 `import app.core.db` 失败** —— 沙箱那一层仍在（第 49 项没被删）。
4. **shadow 差异为空** —— 同一份插件代码，两种执行方式，声明**逐条相同**
   （路径、方法、handler 名、**签名**、任务/钩子名）。签名是阶段 2 的命门：
   宿主要靠它重建 FastAPI 路由，而 FastAPI 靠 `__signature__` 解析请求体。

**反向验证**（逐条拆掉防线，对应用例必须变红）：worker 不再报进程号 / 不再报加载了哪些
模块 / 声明里丢掉签名 / worker 侧路径记错 / ctx 代理不再发调用 —— **五处全部如期变红**。

> 其中「声明里丢掉签名」这一处**第一轮没变红**：当时只断言了「两边一致」，
> 而两边**同时**丢信息也满足「一致」。补上「签名确实有内容、且是插件原本写的那份」
> 之后才变红 —— 这是「覆盖性/一致性测试证明不了内容是对的」的又一个实例。

**实施时挖出来的一个真东西**：沙箱里的插件代码，注解在运行期是**字符串**。
原因是 `sandbox.py` 自己有 `from __future__ import annotations`，而 `compile()` 默认
**继承调用方的 future 标志** —— 插件源码也按 PEP 563 编译。这不是 bug（FastAPI 自己用
`get_type_hints` 解析），但描述层必须认得它，否则阶段 2 重建出来的签名全是 `Any`。

### 新增的两道结构性闸
1. **插件自有表不许有二进制列**（`validate_tables` 拒绝 `BYTEA` / `BYTEA[]`，
   错误码 `INVALID_TABLE_COLUMN`）。没有这道闸时实测：声明 `sa.LargeBinary` 列的
   插件表会被照常接受，`ctx.data.read` 的返回值就可能带二进制。
2. **`ctx` 返回值的序列化登记表**（`public_api.CTX_METHOD_SERIALIZATION`，28 项：
   26 项 `json`、2 项 `bytes`），覆盖性由 `tests/test_plugin_sdk_rules.py` 强制
   （**恰好**等于插件面上每一个可调用叶子路径，无缺无多）；两个 `bytes` 项有
   **行为级证据**（真调用一次看返回值是不是真的 `bytes`）。

### 阶段 2：宿主侧改走 IPC（`PLUGIN_ISOLATION=on`）

隔离模型的**最后一块**：插件的业务请求不再由宿主进程里的那份代码处理，
而是由 **worker 进程**里的那份代码处理，宿主只经 Unix socket 派活。

| 取值 | 插件后端代码在哪 | 对外行为 |
|---|---|---|
| `off`（默认） | 宿主进程内 | 与历史一致 |
| `shadow` | 宿主进程内 + 另起一个 worker 跑同一份代码 | 只比对声明差异，不改对外行为 |
| `on` | **只在 worker 进程里** | 与 `off` 一致（逐条比对见下） |

**「行为不变」是逐条比出来的**，不是「看起来应该一样」。`on` 与 `off` 跑同一份
插件源码、同样的请求，比对状态码与响应体：GET 查询参数、POST JSON body、
POST 路径参数、不存在的路由 —— 四种形态**逐字相同**。

| 验收 | 证据 |
|---|---|
| 插件代码**不在宿主进程里** | 宿主一次也没调过 `exec_plugin_module`；worker 自报的进程号 ≠ 宿主进程号；宿主 `sys.modules` 里没有插件模块 |
| 对外行为与进程内**逐字相同** | GET / POST body / 路径参数 / 404 四种形态的 `(状态码, 响应体)` 两模式相等 |
| `ctx.*` 能往返 | 插件在 worker 里调 `ctx.book_list()`，宿主真的执行了（嵌套：宿主派活 → worker 要能力 → 宿主执行 → worker 应答） |
| 错误映射逐档相同 | `HTTPException(422, …)` → 422 且 detail 一致；普通异常 → 500 且**响应里没有异常内容** |
| 钩子与定时任务**也走 IPC** | `runtime.hooks` / `runtime.tasks` 里装的是转发函数，调用直达 worker |
| 重建不了的签名 → **拒绝装载** | 注解宿主重建不了时报 `PLUGIN_ISOLATION_UNSUPPORTED`，而不是塞 `Any` 让行为悄悄变掉 |
| worker 被 `kill -9` | 宿主检测到它没了 → 插件接口 **503**（不是 500），**宿主本身照常**（`/api/health` 200） |
| worker 自己上了限制且**可观测** | `hello` 带回**实际生效**的限制清单：`RLIMIT_CORE=0`（core dump 里可能有账套数据与令牌）、`RLIMIT_NOFILE` 上限；Linux 上再加 `PR_SET_NO_NEW_PRIVS` |
| **示例插件零改动** | `plugins/com.cwq.reimburse` 与 CSV 示例插件在 `on` 模式下不改一行就通过（含上传、字节返回、解析、导入批次） |

**446 条插件测试在 `on` 与 `shadow` 两种模式下分别全绿**（默认模式下同样全绿）。

反向验证（mutation）：**本阶段 6 处注入已固化成用例**（逐条对应上面某条断言，
注入后**该断言必须变红**）—— 签名重建改成「一律 Any」（行为等价断言变红）、
宿主装载插件模块（那条断言变红）、内部错误档带上异常细节、
ctx 白名单被拆掉（下划线属性就能被点名）、声明目标标识重复（两个 `lambda` 串路由）、
值编码丢掉类型标记（`Decimal` 变 `float`）。

阶段 1 的 5 处注入（worker 不报 PID / 不报 modules / 声明丢签名 / worker 路径记错 /
ctx 代理不发调用）是**一轮性的变异实验**：worker 跑在**另一个进程**里，
测试进程改不到它的代码，那些注入是临时改源码做的 —— 结果记在本轮记录里，
但**没有固化成用例**（要固化得给 worker 加一个测试专用的注入口，
那等于在生产代码里留一个后门，不做）。阶段 1 的结论本身由常驻用例守着 ✓。

### 实施时改掉的六处设计

1. **`invoke` 从「路由 + 方法」改成「目标标识」**。早先拿 handler 名字当标识是有洞的：
   两个路由都注册 `lambda` 时名字一样（`"<lambda>"`），后者把前者顶掉，
   **第一条路由会调到第二个函数**。现在标识由「种类 + 路径/名字 + 方法」拼成，
   与 handler 名字无关。
2. **「每个方向一个未完成调用」，不是「每条连接一个」**。宿主派活时 worker 可能反过来
   要一个宿主能力 —— 那是**嵌套**不是并发；一个方向一个未完成正好装得下。
3. **值要带类型标记**。`ctx.data.read` 返回的行里金额是 `Decimal`、时间是 `datetime`：
   直接 `json.dumps` 会**抛异常**；退而用 `float` 顶替则 `Decimal("1.50")` 变成 `1.5`，
   **插件写进账里的金额与用户看到的对不上**。现在这些类型带标记跨进程，对端还原成
   同一个 Python 类型。
4. **签名重建是严格的**。重建不出来就拒绝装载插件，而不是降级成 `Any`（`Any` 与原注解
   对 FastAPI 的意义不同：`Any` 少一层校验，错的注解则按错的方式解析请求体）。
   能原样重建的类型：`int`/`str`/`float`/`bool`/`bytes`/`dict`/`list`/`Any`、
   `Optional`/`Union` 与常见泛型、`datetime`/`date`/`Decimal`/`UUID`、带 `File(...)` 的 `UploadFile`。
5. **async 路由处理函数要支持**。进程内模式里 FastAPI 会 await 它；隔离模式不能因此
   变成「不支持异步路由」（那会让一份能跑的插件在隔离模式下直接 500）。worker 用一个
   临时事件循环跑它。
6. **`ctx` 分发是白名单 + 结构性闸**。插件现在可以**点名**让宿主执行什么 ——
   这是新出现的攻击面。白名单来自**从真实 ctx 推导**的表面说明（不是手写的），
   另有一道「不许下划线开头的属性」的**结构性**闸（不依赖白名单的内容）。

### 顺手修掉的一个真 bug：插件不存在的路由返 500 而不是 404

写隔离模式的「行为等价」用例时发现的：同一个请求，插件路由**不存在**时，
返回的是 **500 `PLUGIN_EXECUTION_FAILED`**，不是 404。

根因是异常层级：`fastapi.HTTPException` 是 `starlette.exceptions.HTTPException` 的
**子类**，而插件网关接的是 `except fastapi.HTTPException`。Starlette 路由未命中时
抛的是**父类** `starlette.HTTPException` → 网关接不住 → 落到 `except Exception`
→ 变成 500。

改成接**父类**（`starlette.exceptions.HTTPException`），两种都接得住。

这是**既有缺陷**（与隔离模式无关，`off` 模式下同样如此），但它正好落在
「插件接口的对外契约」这条边界上：调用方把「路径写错了」当成「服务端崩了」，
监控上是一堆假 500。顺带修掉。

### 本轮回归

| 检查 | 本轮结果 |
|---|---|
| 后端全量 `pytest -o addopts="" -n 6` | **3482 项通过，2 项跳过，0 失败**（带 PG 客户端；缺客户端时 3478 通过 / 6 跳过，差的那 4 条是备份恢复的真实往返） |
| 插件沙箱专项 `tests/test_plugin_sandbox.py` | **111 项通过**（含 20 组命名空间走私 + 6 组动态拼名 + 失败即闭不变量） |
| 插件扫描专项 `tests/test_plugin_security_scan.py` | **87 项通过** |
| 逃生口清单 `tests/test_escape_hatches.py` | **12 项通过** |
| SDK 契约与序列化登记表 `tests/test_plugin_sdk_rules.py` | **9 项通过** |
| IPC 协议 `tests/test_plugin_ipc_protocol.py` | **40 项通过** |
| worker 进程隔离 `tests/test_plugin_worker_isolation.py` | **10 项通过** |
| **隔离模式验收 `tests/test_plugin_isolation_modes.py`** | **22 项通过**（含 6 处反向验证注入） |
| **插件专项在 `PLUGIN_ISOLATION=on` 下重跑** | **466 项通过，0 失败** |
| **插件专项在 `PLUGIN_ISOLATION=shadow` 下重跑** | **466 项通过，0 失败** |
| 前端全量 `npm test` | **228 项通过，0 失败**（75 个测试文件）|
| 前端语法自检 `npm run check` | **175 个文件通过** |
| 部署脚本演练 `bash deploy/tests/test_deploy.sh` | **216 项通过，0 失败** |
| 开发脚本演练 `bash deploy/tests/test_dev_sh.sh` | **29 项通过，0 失败** |

### 未实施

- **§7 第 6 步第 48 项的 systemd / 容器形态（阶段 3 的其余部分）未实施**。
  已落地的是**不依赖 systemd 的那一半**：worker 自己上 `RLIMIT_CORE=0` /
  `RLIMIT_NOFILE` 上限 / Linux `PR_SET_NO_NEW_PRIVS`，并把生效结果报回宿主。
  systemd unit（`ProtectSystem=strict` / `PrivateTmp` /
  `RestrictAddressFamilies=AF_UNIX` / `TasksMax=`）与容器形态要求**部署侧**把
  worker 放进独立 unit —— 本机是 macOS，**写上去也验不了**，不写。
  所以现在的准确表述是：**插件的后端代码已经跑在独立进程里，但进程隔离不是
  安全边界** —— worker 与宿主仍是同一个操作系统用户、同一套文件权限。
  落地方案与阶段清单见 `output/PLUGIN-ISOLATION-PLAN-2026-10-25.md`。
- 默认仍是 `PLUGIN_ISOLATION=off`：**切过去会改变插件的执行位置**（多一次 IPC、
  同一插件的并发请求被串行化），这类变更不该由一次升级悄悄带上线。
- 第 49 项（进程内注入裁剪 `__builtins__` + 禁非 `.py` exec）已实施，
  第 50 项（修正 `scan_policy.py` 的模块说明）已完成。第 49 项在进程隔离
  落地之后降级为**纵深的一层**（那一步属阶段 4）。
- **第 48 项路线的选择不是人工确认的**：此前 `ask_user` 三次均在 60 秒超时后
  自动选中推荐项。本轮是按**审计报告原文**（「必须换隔离模型」）推进的，
  不再依赖那次超时选择。
- 本轮未重跑真实浏览器演练（`backend/scripts/plugin_e2e.py`）与 Docker 镜像构建；
  未启用任何生产插件、未修改业务数据库与生产配置。

## 2026-10-20 插件系统审查修复轮验收

本轮针对《项目审查报告_2026-10-20》「六、插件系统与扩展能力」逐条修复。API 版本仍为 1.2.0，CWP 格式保持 1；**本轮没有新增或修改任何主迁移**（当前 head 为 `0031_subject_guard_year`，由本轮之外的提交引入）。本轮未部署生产、未修改业务数据库，插件平台仍默认关闭。

| 检查 | 本轮结果 |
|---|---|
| 后端全量 `pytest tests/ -p no:warnings -n 2` | **2367 项通过，4 项跳过，0 失败**（退出码 0；4 项跳过由本地环境触发，见该文件原有说明） |
| 插件专项（`tests/test_plugin_*.py`：安全扫描、撤销、备份、配额、维护、公开 API、资产、加载、迁移、导入、worker、supervisor、打包 CLI） | **195 项通过，0 失败，0 跳过** |
| 前端全量 `node --test` | **77 项中 76 项通过、1 项失败**；失败项是 `tests/nav_tabs.test.mjs`「管理员侧栏是 18 项」，实际 19 —— 由本轮之外的提交 `47ab3c6`（新增二次验证设置页菜单项）造成，与插件改动无关，修复方式是把该断言的期望数改成 19 |
| 前端生产构建 `npx vite build` | 通过 |

### 逐条结果

| 报告条目 | 处理 | 验收证据 |
|---|---|---|
| [P1-1] 静态扫描黑名单遗漏等价入口 | 改为**导入白名单**（新增 `app/plugins/scan_policy.py`，每条写明允许理由）；按 Name 引用、Subscript 调用、属性访问三种形态判定危险能力 | `test_plugin_security_scan.py`：报告实测表里的每个模块（`posix`/`pty`/`signal`/`code`/`runpy`/`sqlalchemy`/`httpx`/`inspect`/`base64` 等 33 项）全部被拒；`imp = __import__`、`e = eval`、`f = open`、`func["eval"](...)` 全部被拒；`os`/`subprocess`/`socket` 仍被拒；两个真实插件与两个文档示例的源码通过 |
| [P1-2] 撤销对已安装插件无效 | `verify_signature` 的 `historical` 只放行历史密钥，**撤销一律拒**；加载期 `verify_directory` 与资产路由同样判定；新增 `POST /api/system/plugins/revoke-key` 一键封禁并停用 | `test_plugin_security_scan.py`：撤销后 `verify_directory` 抛 `KEY_REVOKED`，重启后插件状态 `UNAVAILABLE/KEY_REVOKED`；管理动作把插件置为停用并落盘运行时封禁名单 |
| [P2-1] 文档承诺了没兑现的隔离 | 公开文档页改写为据实描述：白名单是第一道防线、不是沙箱；插件迁移是例外且只开放 `op` 的 DDL | `docs/plugin-overview.md`「后端代码到底能拿到什么」 |
| [P2-2] 备份/隔离区计入配额 | 配额只管插件代码；备份与隔离区单独计量、单独额度；新增清理入口，只删无引用的备份 | `test_plugin_maintenance.py`：备份区撑满时安装仍可用；`prune` 删掉无引用备份、保留被 registry 引用的备份 |
| [P2-3] 备份 catalog 全局单点 | 漂移插件被跳过并在归档头留 `drifted` 记录，其余照常备份；恢复路径仍严格拒绝 | `test_plugin_backup.py`、`test_plugin_maintenance.py`：漂移后账套与系统备份都能导出，含漂移记录的归档恢复被拒 |
| [P2-4] `registrar.route(router)` 必崩 | **实现**而非改文档：`namespace` 变为可选，默认取插件自己的 id；显式传入且不符仍拒绝 | `test_plugin_security_scan.py`：`registrar.route(router)` 注册的路由返回 200 |
| [P2-5] 缺 body 返回 500 | 载体形参纳入必填判定：缺 body 或空对象返回 422「缺少请求体：payload」 | `test_plugin_security_scan.py`：无 body → 422，`{}` → 422，正常 body → 200 |
| [P3-1] 规范缺管理 API 清单、README 死链 | `docs/plugin-spec.md` 新增「管理 API」清单与「备份与恢复」一节（README 指向的锚点由此接上） | 文档 |
| [P3-2] 管理页没有 reconcile / 诊断入口 | 管理页新增「恢复 / 诊断」：执行 reconcile、查看安装尝试记录、清理备份区、一键封禁密钥 | 前端构建通过；`GET /api/system/plugins/attempts`、`POST /api/system/plugins/reconcile`、`POST /api/system/plugins/maintenance/prune` |
| [P3-3] 公开页不内联风险与验收 | `docs/plugin-overview.md` 内联「安全边界速查」与「验收登记册（摘要）」 | 文档 |
| [P3-4] 资产每次重算整包哈希 + 匿名可读 | 按 digest 缓存核验结果（键含目录状态，文件被改动即失效）；资产改为凭**短期票据**访问，匿名请求 401 | `test_plugin_assets.py`、`test_plugin_security_scan.py` |
| [P3-5] `ctx.data.read` 只能前缀范围 + 500 行 | 补**受限过滤 + 分页 + 计数**：`where`（eq/ne/lt/lte/gt/gte/in/like/is_null）、`limit`/`offset`/`order_by`，另加 `ctx.data.count`；账套范围仍由宿主强制附加 | `test_plugin_public_api.py`：过滤、计数、分页排序、非法条件 400、跨账套过滤无效 |

### 本轮新增与变更的接口

| 方法与路径 | 用途 |
|---|---|
| `POST /api/system/plugins/revoke-key` | 按 key_id 封禁签名密钥并停用受影响插件 |
| `GET /api/system/plugins/attempts` | 安装尝试记录（状态、错误码、迁移 head、备份路径） |
| `POST /api/system/plugins/maintenance/prune` | 清理不再被引用的备份与过期隔离目录 |

### 重跑命令

```bash
cd backend && .venv/bin/python -m pytest tests/ -p no:warnings -n 2
cd backend && .venv/bin/python -m pytest tests/test_plugin_*.py -p no:warnings -n 2
cd frontend && node --test
```

### 未计入本轮范围

- 本轮未重跑真实浏览器演练（`backend/scripts/plugin_e2e.py`）、Docker 镜像构建与远程 CI；插件前端资产改为凭票据访问，浏览器演练需要重跑一次才能作为证据（CI 的 `plugin-browser` job 会覆盖）。
- 本轮未启用任何生产插件、未修改业务数据库与生产配置。


## SDK 1.2 定时调度追加验收

当前 API 版本为 1.2.0，CWP 格式保持 1；主迁移 head 为 `0030_admin_email_mfa`。宿主提供受密钥保护的 `GET /api/scheduler/tick?key=...`，供外部服务每分钟调用，统一领取 OSS 备份、插件导入清理和插件声明的到期任务。插件新增 `tasks` 声明与 `registrar.task()` 注册能力，管理员开启任务后，按当前有效授权逐用户执行，继续使用原有账套权限。

| 检查 | 本轮结果 |
|---|---|
| 后端既有测试及调度器回归 | **1830 项通过，4 项跳过**；排除工作区尚未纳入版本管理的 `test_plugin_periodic.py`，原因见下文 |
| 最终调度、插件契约、加载与迁移专项 | **91 项通过**；最后一次并发领取刷新与同分钟重复运行保护修改后重新执行 |
| 前端全量 | **57 项通过，0 失败、0 跳过** |
| 前端生产构建 | 通过；仓库指南、规范和验收文档继续内联到应用文档 |
| 部署脚本演练 | **60 项通过，0 失败**；没有实际部署 |

四项跳过均由本地环境触发：缺少真实 `pg_dump` / `psql` 导致两项真实 dump/恢复往返及对应开发环境分支跳过，另有一项性能数据检查因开发库未达到所需迁移版本跳过。本轮未重跑 PostgreSQL 全库 dump/恢复往返；既有账套逻辑备份及备份任务的宿主调用测试包含在已通过范围内。

### 调度与多用户行为

- 两个并发首次 tick 不会重复发现或领取同一任务；同一 cron 分钟内关闭再开启也不会重建已完成的运行。
- 外部密钥由管理员生成，数据库与审计仅留摘要或操作信息；轮换时当前及上一把密钥均可用，撤销上一把后立即拒绝。普通用户不能操作任务、历史或密钥。
- tick 的查询参数在进入下游处理前清除，提供的两份 Nginx 配置关闭该路径及末尾斜杠形式的访问日志；返回 `202` 表示已领取，结果通过管理员运行记录查询。
- 使用真实签名插件及真实授权记录验证逐用户执行；VIEWER 无法写账，另一个有权用户仍可执行，最终保留逐用户结果及整轮失败状态。
- 用户停用、插件撤权后被排除，重新授权后自动参加后续周期；没有有效用户时记为 `SKIPPED`。插件版本变化自动关闭任务，worker 临时未就绪只延后领取。
- 租约过期后复用原 `run_id`，领取次数递增，旧租约持有者不能覆盖新结果；停用、移除或升级的旧运行不重放。已关闭 OSS 备份的旧运行也会跳过。
- 内置任务调用既有备份及导入过期清理服务，备份部分失败会使调度运行标记失败。备份时间为北京时间 03:47，继续受数据安全设置约束。

任务采用至少一次执行语义。崩溃重领可能再次调用已成功用户，插件必须使用 `run_id:user_id` 幂等键；共享账套仍需按账套、业务规则和期间持久去重。当前不提供对受信任插件函数的强制超时或线程终止，普通失败等待下一周期。详细上下文与边界见 [插件规范](plugin-spec.md#定时任务)。

### 重跑与未计入范围

```bash
# 仓库根目录；只允许使用专用测试库，pytest 会重建隔离库 schema。
backend/.venv/bin/python -m pytest -n 3 backend/tests -ra --tb=short
backend/.venv/bin/python -m pytest -n 3 backend/tests/test_scheduler.py backend/tests/test_plugin_manifest.py backend/tests/test_plugin_loader.py backend/tests/test_schema_version.py backend/tests/test_migration_guardrails.py -ra --tb=short
npm --prefix frontend test
npm --prefix frontend run build
bash deploy/tests/test_deploy.sh
```

`backend/tests/test_plugin_periodic.py` 直接跑，不用再 `--ignore`：插件源码在 `plugins/`（该目录被 gitignore），干净 checkout 里没有源码，**用例会整体跳过**而不是失败；本机有源码时它会连真实插件一起装进隔离测试库，走 HTTP 验 `/variables`。

尚未纳入版本管理的周期记账插件测试已单独诊断：创建规则请求返回 `422`（缺少 query 中的 `book_id`），随后 fixture 清理插件表遇到数据库锁等待。这发生在插件业务接口阶段，未计作调度器通过证据，也未覆盖该工作区插件的业务实现。**2026-10-26 更正**：该用例已重写并纳入版本管理（22 条，含端到端安装与 HTTP 调用），上面的诊断不再适用。

本轮未重跑真实浏览器、Docker 镜像构建或远程 CI，也未创建外部一分钟调用任务。上线需先迁移到 `0030_admin_email_mfa`，生成调度密钥并配置外部调用；旧 `cwq-oss-backup.timer` 不应同时启用，既有本地 pg_dump 定时器继续保持关闭。部署步骤见 [README](../README.md#启用定时调度)。

## SDK 1.1 导入闭环追加验收

该阶段 API 版本为 1.1.0，CWP 格式保持 1；主迁移 head 为 `0027_plugin_imports`。本阶段完成用户选择目标账套、标准文件留存、持久预览、人工确认、整批原子提交、幂等重试、来源追溯和插件自有账套配置。SDK 后续扩展见优化清单，未计作本阶段已实现功能。

| 检查 | 本轮结果 |
|---|---|
| 后端全量 `pytest -q -n 3 -ra --tb=short` | **1810 项通过，1 项跳过**；唯一跳过项是“缺少 PostgreSQL 客户端”的环境分支，因为本轮已提供真实客户端；没有跳过备份恢复 |
| PostgreSQL 真实恢复 | 使用本机 PostgreSQL 16 容器内的真实 `pg_dump` / `psql`，经仅允许隔离测试库的本地启动器调用，并设 `CWQ_REQUIRE_PG_CLIENT=1`；验证完整 dump、空库恢复、结构/行数及实际标记数据 |
| 导入与配置集成测试 | `test_plugin_imports.py` 和 `test_plugin_import_example.py` 共 24 项，全部通过，包含真实数据库事务、并发、CSV 解析和 BOOK 备份恢复 |
| 迁移、schema 与打包专项 | 50 项通过；涵盖追加迁移、head/触发器清单、历史 stamp 重放、签名示例打包和 Finder 元数据排除 |
| 前端全量 `node --test tests/*.test.mjs` | **57 项通过，0 失败、0 跳过**；文档目录、SDK 和插件页面生命周期检查均通过 |
| 前端生产构建 | 通过；应用内文档直接内联仓库中的指南、数据接口、规范、风险、验证和优化清单 |
| 部署脚本演练 | **47 项通过，0 失败**；没有实际部署 |
| 真实浏览器、生产构建/CSP、两个 OS worker | 通过：签名安装、用户授权、CSV 配置、真实 multipart 上传、修改并保存预览、顶栏切换保留插件目标账套及未保存调整、刷新恢复、人工确认、两张凭证整批提交、重复提交返回同一结果；同时复验撤权、降级、备份恢复和保留数据卸载 |

浏览器截图为 `output/playwright/plugin-csv-submitted.png`。本轮发现并修复了用户资料刷新误触发身份变化、导致已加载插件样式被移除的问题；脚本在首次打开、刷新以及提交后均断言样式仍生效，再进行截图检查。

### 一致性和失败路径

- 同批四个并发提交请求只创建一组凭证和来源关联；提交成功后的重试返回原结果。
- 第二张凭证发生预期错误时，第一张及其凭证审计、来源均回滚，批次保留 `FAILED` 和错误，修正后可重试；意外异常回滚至 `CONFIRMED`，不留下半批凭证。
- 预览不平衡、停用科目、非末级科目、期间不合法、schema 错误、手工凭证号及负附件数均不能确认；错误持久化，不生成正式凭证。
- 修订冲突、插件升级、解析后配置变化、确认后配置变化均拒绝陈旧结果；首次配置并发保存只允许一个成功。用户授权或账套写权限撤销后下一请求被拒绝。
- 覆盖同批重复、部分文件交叠、容量上限、取消释放占位、原文件到期、清理后保留证据，以及包含源文件字节和提交来源的账套备份恢复。

### 本阶段重跑

先安装与本机 PostgreSQL 服务端匹配的真实客户端工具，使 `pg_dump` / `psql` 可在 PATH 找到，或设置 `CWQ_PG_DUMP`、`CWQ_PSQL` 为实际路径。以下命令只允许使用专用测试库；浏览器脚本另使用 `*_plugin_browser_test` 数据库。

```bash
# 仓库根目录；数据库连接遵循下文的测试库护栏。
CWQ_REQUIRE_PG_CLIENT=1 backend/.venv/bin/python -m pytest -q -n 3 -ra --tb=short
npm --prefix frontend test
npm --prefix frontend run build
bash deploy/tests/test_deploy.sh
backend/.venv/bin/python backend/scripts/plugin_e2e.py
```

本轮没有重新执行 Docker 镜像构建，也未执行远程 CI；下方 Docker 记录属于初始宿主阶段，使用的是当时的迁移 head。正式使用本阶段功能需要先迁移到 `0027_plugin_imports` 并重新构建前端。导入完整内容的到期清理 CLI 已交付，自动调度仍待部署方配置。

## 初始宿主已交付范围（历史记录）

| 阶段 | 实现与验收入口 |
|---|---|
| P0 契约及六项原型 | 整包 Ed25519/RFC8785、真实两个 OS worker、生产构建下 native import/CSP、独立迁移、backup v2、按用户授权；每项都有失败断言 |
| P1 注册与包管理 | 主迁移仅追加 `0026`；上传容量/路径/类型/签名/扫描门禁；registry/attempt/grant 审计；staging/journal/reconcile |
| P2 受控后端加载 | 每 worker 独立预加载；受限 registrar/context；共享 generation、租约及全 worker 屏障；请求前实时授权；异常降级 |
| P3 前端与管理页 | 当前用户 manifest；同源只读资产；动态路由/导航/tabs；用户搜索、选择、保存与撤销；身份切换和失败清理 |
| P4 数据、SDK、部署及文档 | 每插件 Alembic、实时表 metadata、backup v2/v1 reader、窄 SDK、签名示例、持久目录及 supervisor、CI 浏览器作业 |

下表记录 SDK 1.0 初始宿主的组合验收，保留作历史基线；本阶段最新结果以上方 SDK 1.1 追加验收为准。没有生产上线或人工批准记录。

## 初始宿主检查结果（SDK 1.0 历史记录）

| 检查 | 结果 |
|---|---|
| 后端全量 pytest，真实 PostgreSQL、pg_dump 和 psql，`-n 3 -rA` | 1783 项通过、1 项按环境跳过；其中 75 项插件测试全部通过 |
| 故障恢复及 worker 定向检查 | 11 项通过；包括进程硬退出、实际数据库连接终止、两个 OS worker 和代次交叠 |
| 前端 `node --test tests/*.test.mjs` | 43 项通过，0 失败；磁盘上 27 个测试文件全部被发现 |
| 前端生产构建 | 通过 |
| `deploy/tests/test_deploy.sh` | 47 项通过，0 失败；含屏障失败阻断、运行目录保留 |
| 真实浏览器演练 | 通过：管理员安装、零默认授权、用户选择、两 worker 重启、CSP/资产/API 鉴权、撤权/再授权、页面失败降级、数据读写、v2 恢复、停用重启、保留表卸载 |
| Docker | 真实构建及默认 CMD 启动通过：非 root、可写持久目录、主迁移到 `0026`、Gunicorn 两 worker、health/ready、插件状态检查；临时开启模式验证两个实际 worker 共享 generation |
| 主迁移不变性 | 前 25 条迁移保持原样；插件安装/升级前后主 `alembic_version` 不变 |
| CI | 已添加真实浏览器作业并保留全量测试、测试发现数、数据库护栏、构建与部署检查；远程 CI 需推送后执行 |

测试使用独立的本机 PostgreSQL 测试库和临时签名密钥、包目录。pytest 的 schema 重建只针对测试库；浏览器演练只允许 loopback 且库名匹配 `[a-z][a-z0-9_]*_plugin_browser_test`。本机 PostgreSQL 服务端是 16；数据库备份测试调用匹配的真实 16 版 `pg_dump`/`psql`，并强制 `CWQ_REQUIRE_PG_CLIENT=1`，没有用 stub 跳过恢复。

## P0 成功与失败证据

| 原型 | 成功路径 | 失败路径及对应文件 |
|---|---|---|
| 完整包签名 | 示例离线构建、文件清单核验、canonical payload 摘要及验签 | Python/JS/迁移/资源篡改、未知/撤销密钥、缺签名、重复/穿越/链接、容量和危险代码被拒绝；`test_plugin_security.py`、`test_plugin_manifest.py`、`test_plugin_package_cli.py` |
| 多 worker | 同 generation 两个独立 OS 进程均可处理已授权请求 | 第二 worker 预加载故障时两个端口均稳定返回 503；旧 generation 拒绝，新 cohort 等待旧 lease；`test_plugin_workers.py`、`test_plugin_supervisor.py` |
| 浏览器资产 | 生产构建及现有同源 CSP 下原生 ESM 加载不带 Bearer，API 带 Bearer | 匿名 API 401、未授权 404；错误模块显示扩展降级页，核心仍可访问；`test_plugin_assets.py`、`tools/uitest/plugin_e2e.mjs` |
| 独立迁移 | 插件独立 version table；升级只执行新增 revision 并保留原行；主链不变 | 迁移异常/越界、非法 scope/FK、真实约束/触发器漂移均拒绝；启动不执行 DDL；`test_plugin_migration.py`、`test_plugin_guardrails.py`、`test_plugin_lifecycle.py`、`test_plugin_loader.py` |
| backup v2 | 实际插件行导出、dry-run、FK 顺序恢复、序列重置；book/global 闭包 | 未知插件、缺代码、digest/head/version/metadata 不同、越界行和缺父行在写入前拒绝；v1 历史 reader 保留；`test_plugin_backup.py` 及既有 backup 测试 |
| 用户授权闭环 | 管理 UI 搜索/勾选/保存，旧 token 在 grant 后无需重启即可使用 | 安装者及 admin 默认无 grant；其他用户拒绝；revoke 后下一请求拒绝且 manifest/routes/tabs/styles 清理；`test_plugin_user_access.py`、前端 plugin 测试及真实浏览器脚本 |

## 生命周期和崩溃恢复

`test_plugin_reconcile.py` 的实际子进程在迁移已提交后，于 rename 前、rename 后、registry 提交后直接 `os._exit(71)`，不会执行 Python `finally`。父进程验证 session advisory lock 和文件锁均释放，journal 保留；连续两次 reconcile 后只存在同一安装结果和同一插件 head，没有重复 DDL。另一个用例通过 `pg_terminate_backend()` 实际终止 registry 提交连接，核对恢复后的 digest、attempt、active 目录一致。

`test_plugin_lifecycle.py` 覆盖“先停用并完整重启”的硬要求、低版本/重复版本拒绝、升级前可验证备份、升级保留授权且保持禁用、新 revision 保留数据。浏览器脚本再用签名示例走 HTTP 安装与用户管理，启动整个两 worker supervisor，完成业务读写、真实 v2 数据恢复，再经 HTTP 停用、重启、卸载：保留两条插件业务行、撤销全部用户授权、旧 token 被拒绝、备份位置可查，最后显式 reconcile。

卸载后的普通 restore 仍要求恢复完全匹配的安装代码及 registry 现场。内部“保留表备份结构核验”只允许 dry-run，不能用于写入。没有自动 downgrade，也没有把文件系统 rename 和数据库 commit 描述成一个原子事务。

## 重跑方式

以下命令会重建隔离测试库的 public schema，只能指向专用测试数据库。先准备 PostgreSQL、匹配服务端的客户端工具和仓库锁定依赖。

```bash
# 仓库根目录；按本机实际端口填写，只能使用专用测试库。
export CWQ_TEST_DATABASE_URL='postgresql+psycopg://cwq:cwq123@127.0.0.1:15432/cwq_plugin_test'
export CWQ_REQUIRE_PG_CLIENT=1
backend/.venv/bin/python -m pytest -n 3 -rA
pnpm --dir frontend test
pnpm --dir frontend build
bash deploy/tests/test_deploy.sh
```

真实浏览器演练需要根目录 `npm ci`、`pnpm --dir frontend install --frozen-lockfile` 和 `npx playwright install chromium`，使用单独的数据库。运行前确保该库没有需要保留的数据。

```bash
CWQ_PLUGIN_E2E_DATABASE_URL='postgresql+psycopg://cwq:cwq123@127.0.0.1:15432/cwq_plugin_browser_test' \
  backend/.venv/bin/python backend/scripts/plugin_e2e.py
docker build -t cwq-api:plugin-check backend
```

脚本临时生成 Ed25519 测试密钥、构建完整签名示例包，启动生产前端构建及 CSP 同源代理，先在浏览器安装并授权，再重启两个后端 worker，最后完成恢复及卸载。退出时终止测试服务、清理临时包及测试 schema。截图写入 `output/playwright/`（已忽略，不进入发布包）：

- `plugin-install.png`：安装状态及可使用用户管理。
- `plugin-authorized.png`：授权用户的示例插件页面。
- `plugin-fallback.png`：失败插件降级及宿主页面。

示例包构建命令、完整 manifest/签名契约、SDK 及安装运维步骤见 [plugin-spec.md](plugin-spec.md) 和仓库 README。

## 验收边界

所有通过结果是契约行为的工程证据，不能证明任意 Python/同源 JavaScript 安全；该架构没有沙箱。签名、扫描、受限 SDK 也不能阻止可信代码主动退出进程、越过 SDK 或消耗资源。已读取的数据及浏览器模块缓存无法撤回，撤权约束下一次请求。租约是共享数据库上的就绪证据，依赖数据库和时钟；它不强制终止旧进程。

生产开启需运营方审阅实际包、配置可信公钥、保存匹配代码与备份，并接受 [plugin-risk.md](plugin-risk.md) 中的风险。工程交付保持 `PLUGIN_ENABLED=false`，未填造人工批准结论。
