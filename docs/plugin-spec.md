# Manifest、权限与运行时参考

本页是插件开发时需要反复查阅的契约。当前版本为 **SDK 1.1.0 / CWP 1**；示例包中的 `plugin.json`、后端入口、迁移和前端入口可以直接作为最小模板。

## Manifest 结构

源目录根部必须有 `plugin.json`。签名打包后，工具会在 manifest 中补齐 `files` 和 `signature`；开发者只维护下面的源字段。

| 字段 | 必填 | 说明 |
|---|---:|---|
| `format_version` | 是 | 当前固定为 `1` |
| `id` | 是 | 小写分段标识，例如 `com.example.periodnote` |
| `name` | 是 | 展示名称 |
| `version` | 是 | SemVer 版本号 |
| `api_version` | 是 | SDK 兼容范围，例如 `>=1.1.0 <2.0.0` |
| `description` | 否 | 插件简介 |
| `permissions` | 是 | 只声明实际使用的 capability |
| `access` | 是 | 当前固定为 `{ "assignment": "admin_users", "default": "none" }` |
| `contributes` | 是 | 后端入口、可选前端入口和菜单 |
| `hooks` | 否 | 当前支持 `on_period_close` |
| `tables` | 是 | 插件迁移创建并由宿主登记的数据表 |

一个带页面的插件至少需要下面这些入口字段：

```json
{
  "contributes": {
    "backend_entry": "backend/main.py",
    "frontend_entry": "frontend/index.js",
    "menu": {
      "module": "extensions",
      "title": "期间工作笔记",
      "icon": "Document",
      "route": "/plugin/com.example.periodnote",
      "order": 10
    }
  }
}
```

`backend_entry` 必须位于 `backend/` 下；`frontend_entry` 必须位于 `frontend/` 下。配置了前端入口就必须配置菜单。菜单路由只能是 `/plugin/{id}` 或它的小写子路径，图标使用平台白名单中的 `Calendar`、`Grid`、`Document`、`DataAnalysis`、`Tools`、`Coin`、`Folder`。

## 权限 capability

未声明的能力不会出现在 `ctx` 或宿主服务中。权限应按最小需要声明：

| capability | 后端能力 | 典型用途 |
|---|---|---|
| `book:read` | `ctx.book_read()`、`ctx.book_list()` | 读取账套基本资料、列出用户可访问账套 |
| `book:write` | `ctx.book_write()` | 要求目标账套具有 OWNER 或 ACCOUNTANT 写权限 |
| `ledger:read` | `ctx.ledger_read()` | 读取指定账套和期间的余额数据 |
| `subject:read` | `ctx.subject_read()` | 读取科目并把外部科目编码映射为 `subject_id` |
| `voucher:write` | `ctx.voucher_create()`、`ctx.voucher_batch_create()` | 创建凭证；导入提交也需要此能力 |
| `plugin_data:rw` | `ctx.data.*`、`ctx.config.*`、前端 `services.config` | 访问插件自有表和账套级配置 |
| `audit:write` | `ctx.audit_write()` | 写入插件自定义审计事件 |
| `settings:rw` | `ctx.settings.*` | 管理员维护插件级短配置 |
| `upload:read` | `ctx.upload_read()` | 在当前请求中读取插件接口收到的文件 |
| `import:rw` | `ctx.imports.*`、前端 `services.imports` | 持久文件、预览、确认、整批提交和来源追溯 |

`import:rw` 只提供导入批次能力；正式提交还必须声明 `voucher:write`。`plugin_data:rw` 只允许访问 manifest 声明的插件表，不能用来读取宿主业务表。

## 包与签名

`.cwp` 是一个 ZIP 包，根目录必须包含唯一的 `plugin.json`。所有 Python、JavaScript、迁移、样式和图片都会进入签名文件清单。打包工具使用 Ed25519 签名；服务端按文件路径、大小和 SHA-256 重新核验，任何成员被修改都会使验签失败。

签名工具负责：

- 生成并排序文件清单；
- 计算文件摘要和包摘要；
- 生成 `signature` 字段；
- 拒绝路径穿越、链接文件、重复路径和超出容量的包。

私钥只放在受控的打包环境中。可信公钥由运行环境配置，插件包本身不携带可替代可信配置的公钥。签名证明包来自可信发布者且内容未被修改，不代表业务逻辑已经正确，也不提供代码沙箱。

前端必须是随包提供的自包含 ESM。不能运行时加载 CDN、远程模块、SFC 编译器或未打包依赖。

## Python SDK

后端入口固定为：

```python
def register(registrar, ctx):
    ...
```

`registrar` 注册的是插件自己的相对路径，宿主会自动加上 `/api/plugins/{id}` 前缀：

| 方法 | 用途 |
|---|---|
| `registrar.get(path, handler)` | 注册 GET |
| `registrar.post(path, handler)` | 注册 POST |
| `registrar.add(path, handler, methods)` | 注册多个 HTTP 方法 |
| `registrar.route(router)` | 挂载受限路由 |
| `registrar.hook(name, handler)` | 注册已声明的 hook |

`ctx` 的能力由 manifest 决定。handler 内部使用普通类型、字典和宿主支持的上传对象；不要依赖宿主的 FastAPI app、Request、Response、Session 或 WebSocket。

凭证写入有两种事务语义：`voucher_create` 每次调用单独提交；`voucher_batch_create` 把整批放在同一个事务里，任意一张失败时整批回滚。需要文件、人工预览、幂等和来源追溯时，使用 `import:rw` 的导入流程，不要循环调用单张创建。

数据助手只操作插件自有表：

```python
rows = ctx.data.read(TABLE, book_id=book_id, limit=100)
created = ctx.data.insert(TABLE, {"label": "月末检查"}, book_id=book_id)
changed = ctx.data.update(TABLE, row_id, {"label": "已完成"}, book_id=book_id)
removed = ctx.data.delete(TABLE, row_id, book_id=book_id)
```

`data.read` 最多返回 500 行；`update` 和 `delete` 要求表有 `id` 主键，不能修改 `id` 或 `book_id`。这些助手彼此不提供跨调用事务；跨步骤的文件导入应使用宿主导入服务。

## 前端运行时

前端模块默认导出组件工厂：

```javascript
export default function createPlugin(host) {
  return { name: 'MyPlugin', setup() { /* 返回渲染函数 */ } }
}
```

`host` 提供以下能力：

| 能力 | 说明 |
|---|---|
| `h`、`ref`、`computed`、`watch` | 使用宿主提供的 Vue 运行时 |
| `onMounted`、`onUnmounted` | 组件生命周期 |
| `bookId()`、`periodId()` | 当前宿主选择 |
| `books()` | 当前用户可访问账套及 `canWrite` 提示 |
| `api.get/post/put/patch/delete` | 请求当前插件命名空间 |
| `api.upload(url, files, params)` | 上传文件到当前插件接口 |
| `api.download(url)` | 获取当前插件命名空间下的 Blob |
| `services` | 标准账套、配置和导入服务 |

`host.api` 会自动携带登录凭据，插件不需要创建 Axios 实例或手工拼接 Bearer。`put(url, data, params)` 的第三个参数用于 query。前端可以根据 `canWrite` 调整界面，但后端提交时仍必须重新检查账套权限。

宿主不会因为顶部账套或期间切换而重新挂载插件页面。跟随顶部选择的插件可以监听 `host.bookId()`；需要用户独立选择目标账套的插件应使用 `host.services.books()` 并在自己的页面保存选择。

## 迁移与数据表

插件迁移位于 `migrations/versions/*.py`，必须是单根、单头的线性 revision。宿主为每个插件维护独立的版本记录；插件迁移不会改变宿主主迁移链。

- `scope: "book"` 的表必须有非空 `book_id`，并且外键指向 `book.id`；
- `scope: "global"` 只适合配置或参考数据，不能存账套业务行；
- 表名必须使用 `plugin_<sha256(id)[:12]>_` 前缀；例如 `com.example.periodnote` 的前缀是 `plugin_e3efc9984ace_`；
- 不能修改宿主表、其他插件的表或跨插件外键；
- 表结构、约束、索引和触发器必须与已登记的迁移结果一致。

把配置写入插件自己的 book-scoped 表，并通过 `ctx.config.get/put` 做版本控制。不要把账套配置写入全局 settings，也不要在服务启动时执行隐式 DDL。

## 安装、升级和卸载

安装会先校验 manifest、签名、文件和迁移，再把插件登记为待重启状态。安装完成不代表任何用户已经获得使用权；管理员还需要显式授权并按提示重启。

升级必须使用更高的插件版本。宿主会重新检查签名、迁移和已登记表结构；升级成功后插件通常保持禁用，启用新版本前需要再次重启。插件应通过新增 revision 迁移数据，不要依赖降级或重放旧 revision。

卸载会撤销用户授权并隔离插件代码，但默认保留插件数据表，避免把业务数据和卸载操作绑在一起。删除表是独立的管理员操作，只有在确认不再需要数据并完成备份后才执行。

## 运行时权限结果

插件接口常见的结果如下：

| 情况 | 结果 |
|---|---|
| 未登录 | `401` |
| 已登录但没有插件使用权 | `404`，不暴露插件是否存在 |
| 有插件使用权但没有目标账套权限 | `403` |
| 插件 worker 尚未就绪 | `503` |
| 插件代码抛出未处理异常 | `500`，响应带请求标识 |

撤销授权会影响下一次请求；已经完成的响应不会被回溯撤回，正在进行的事务也不会被中途强行取消。
