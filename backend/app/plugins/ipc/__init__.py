"""插件进程隔离（§7 第 6 步第 48 项）的协议与代理层。

**四个模块，按角色分**
----------------------
* :mod:`app.plugins.ipc.protocol` —— **协议**（帧格式、消息类型、错误映射、
  字节信封）。纯数据与纯函数，两侧原样 import 同一份。
* :mod:`app.plugins.ipc.channel` —— Unix socket 上的帧通道。
* :mod:`app.plugins.ipc.declarations` / :mod:`app.plugins.ipc.signatures` /
  :mod:`app.plugins.ipc.values` —— **跨进程传的东西的形状**：声明（含唯一目标
  标识）、签名描述与重建、值的编解码（含类型标记）。
* :mod:`app.plugins.ipc.proxy_registrar` / :mod:`app.plugins.ipc.proxy_ctx` ——
  **worker 侧**的代理：插件照旧写 ``register(registrar, ctx)``，拿到的却是
  「记声明的 registrar」与「发 IPC 的 ctx」。
* :mod:`app.plugins.ipc.worker_main` —— **worker 进程**入口。
* :mod:`app.plugins.ipc.host` / :mod:`app.plugins.ipc.router` ——
  **宿主侧**：起 worker、派活、应答 ctx 调用；按声明重建 FastAPI 路由与
  钩子/任务。
* :mod:`app.plugins.ipc.ctx_dispatch` —— ctx 调用的**唯一一份**分发实现
  （白名单 + 「不许下划线属性」的结构性闸）。
* :mod:`app.plugins.ipc.shadow` —— shadow 模式：让同一份插件代码在 worker
  里也跑一遍，与进程内的声明逐条比对。

**三档隔离模式**（``PLUGIN_ISOLATION``）：``off``（进程内，默认）/
``shadow``（进程内 + 比对）/ ``on``（只在 worker 里，宿主经 IPC 派活）。
"""
