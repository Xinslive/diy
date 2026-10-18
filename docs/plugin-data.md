# 数据接口：从文件到整批凭证

插件获得的是用户使用权。用户在插件自己的页面选择目标账套；一次导入只对应一本账。文件格式、业务规则、科目映射和页面交互由插件实现，宿主提供文件留存、持久预览、会计校验、人工确认、原子提交和来源追溯。

本文对应 SDK **1.1.0**。完整示例在 `docs/plugin-import-example/`，样例文件为 `docs/plugin-import-sample.csv`；签名安装、真实 CSV 解析和配置变更测试在 `backend/tests/test_plugin_import_example.py`，事务与权限测试在 `backend/tests/test_plugin_imports.py`。

## SDK 能力

未声明的 `ctx` 属性不存在。每个助手在请求内重新检查当前用户、插件使用权和账套权限；`can_write`/`canWrite` 只是页面提示。

| capability | Python SDK | 作用 |
|---|---|---|
| `book:read` | `book_list()`、`book_read(book_id)` | 当前用户可访问账套及 `can_write`，指定账套基本资料 |
| `book:write` | `book_write(book_id)` | 检查 OWNER / ACCOUNTANT 权限 |
| `subject:read` | `subject_read(book_id)` | 全部科目，含上级和停用科目，便于明确报告映射错误 |
| `ledger:read` | `ledger_read(book_id, period_id)` | 期间余额，保留宿主账套/期间校验 |
| `import:rw` | `imports.*` | 文件、批次、预览、确认、整批提交、取消和来源查询 |
| `voucher:write` | `voucher_create`、`voucher_batch_create` | 基础凭证写入；`imports.submit` 也必须声明此能力 |
| `plugin_data:rw` | `data.read/insert/update/delete`、`config.get/put` | 自有表 CRUD、book-scoped 配置与版本控制 |
| `upload:read` | `upload_read(file, limit=None)` | 较低层的受限文件读取，不持久化，不替代导入批次 |
| `audit:write` | `audit_write(action)` | 记录自定义事件；批次/配置审计由宿主自动记录 |
| `settings:rw` | `settings.get/put` | 插件全局短配置；写入要求管理员 |

SDK 不公开数据库连接或任意 SQL。一般文件转凭证插件应使用 `imports` 流程。`voucher_batch_create` 保留兼容，但它与 `data.*` 的事务彼此独立，不能代替带批次状态、幂等和人工确认的导入工作流；循环调用 `voucher_create` 会形成多次独立提交。

## 一条完整工作流

1. 页面调用 `host.services.books()`，让用户选择有写权限的账套。
2. 页面调用 `host.services.imports.upload(bookId, files)`。宿主保存源文件并返回 `UPLOADED` 批次及修订号。
3. 插件后端读取 `ctx.imports.read(id)`、`ctx.imports.source(id, file_id)` 和本账套配置，自行解析文件。
4. 插件调用 `ctx.imports.preview(id, vouchers, revision=revision, configuration=snapshot)`。宿主校验并保存预览；这一步不产生正式凭证。
5. 用户检查预览，必要时在插件页面修改，调用 `host.services.imports.edit(id, revision, vouchers)` 保存修改。任何修改都会使原确认失效。
6. 用户点击确认，调用 `host.services.imports.confirm(id, revision)`；宿主校验当前插件版本、配置版本、账套角色、期间和科目。
7. 用户点击整批提交，调用 `host.services.imports.submit(id, revision)`；宿主在同一数据库事务里创建全部凭证、来源关联、审计和 `SUBMITTED` 结果。任何一张失败，整批凭证回滚。

页面刷新后通过 `imports.list(bookId, {before, limit})` 和 `imports.read(id)` 恢复。插件页不会因宿主顶部账套/期间切换而重新挂载；插件自己的目标账套由页面控制。期间笔记等跟随顶部账套的插件仍可使用 `host.watch(() => host.bookId(), ...)`。

## 账套配置存在哪里

规则写在插件代码里；账套差异配置写在插件声明的 `scope: "book"` 自有表里。标准配置助手要求表含 `id`、非空 `book_id`、`key`、`value(JSONB)`、`version(Integer)`，以及 `(book_id, key)` 唯一约束。迁移示例见 `plugin-import-example/migrations/versions/0001_config.py`。

```python
snapshot = ctx.config.get(TABLE, "mapping", book_id=book_id)
# 不存在时 version=0, value={}，并返回内容 sha256。
saved = ctx.config.put(TABLE, "mapping", {"debit_code": "1001", "credit_code": "6001"},
                       book_id=book_id, expected_version=snapshot["version"])
```

普通账套记账用户可以写；VIEWER 只能读，不需要给用户管理员身份。每项配置最大 64 KiB。`expected_version` 必须匹配，两个页面并发保存时只有一个成功，另一个返回 `409 / CONFIG_REVISION_CONFLICT`。写入记录版本和摘要审计，不把完整配置复制进配置修改日志。

解析时把配置引用存入批次：

```python
return ctx.imports.preview(ident, vouchers, revision=payload["revision"],
    configuration={"rules": "csv-v1", "refs": [snapshot]})
```

`refs` 最多 32 项；每项有 `table/key/version/sha256`，可带 `value`。宿主在预览、确认、提交时核对实际配置，旧配置返回 `409 / IMPORT_CONFIGURATION_CHANGED`，需要重新解析、检查并确认。标准 `config.put` 与批次提交共用账套事务锁，避免检查后提交前被标准配置接口改写。遵循此契约的配置应统一通过 `config.put` 修改；普通 `data.*` 不承诺配置版本语义。

## 凭证草稿契约

`vouchers` 为列表；金额传十进制字符串，避免 JavaScript 浮点舍入：

```json
[{
  "voucher_date": "2026-03-02",
  "remark": "收款.csv:2",
  "attachments": 0,
  "entries": [
    {"subject_id": 47, "direction": "DEBIT", "amount": "12.50", "summary": "收款"},
    {"subject_id": 83, "direction": "CREDIT", "amount": "12.50", "summary": "收款"}
  ]
}]
```

按 `subject_read` 将科目编码转换成当前账套的 `subject_id`。宿主复用正常制证校验：账套归属、末级且启用、金额精度、借贷平衡、期间存在且未结账等。批次强制 `source=IMPORT`、自动编号，不允许指定凭证号或插入位置；附件数只能表示单据数量，尚不提供通用凭证附件存储服务。

`original_preview` 保留首次解析提交的预览；`preview` 是当前修订。草稿校验错误可持久保存，`validation_errors` 最多列出 100 个错误，带凭证序号、可用时的分录行和字段。文件解析错误由插件返回文件名和行号；可在 `configuration.locations` 中保存凭证到源文件行的映射。错误预览不能确认。

## 状态、修订和失败恢复

| 状态 | 含义与操作 |
|---|---|
| `UPLOADED` | 文件已留存，等待插件解析 |
| `PREVIEW_READY` | 首份预览通过校验，可以确认 |
| `EDITING` | 已调整或有校验错误；修正后保存并确认 |
| `CONFIRMED` | 用户确认当前修订，可整批提交；重解析/编辑会使确认失效 |
| `SUBMITTING` | 只存在于提交事务内；进程中断会回滚到提交前状态 |
| `SUBMITTED` | 正式凭证、来源关联和结果已原子提交，不能改写或取消 |
| `FAILED` | 可预期账务错误，整批已回滚，保存错误供修正；重新解析/编辑后再次确认 |
| `CANCELLED` | 草稿关闭，立即清除原文件内容、预览及完整配置快照，可重新上传 |

修改、确认、提交、取消必须带精确整数 `revision`。旧页面返回 `409 / IMPORT_REVISION_CONFLICT`，重新读取后展示新预览，不能静默覆盖。插件升级后旧预览返回 `409 / IMPORT_PLUGIN_CHANGED`，必须用当前插件重新解析。批次只能由创建者使用，仍须保有插件和目标账套权限；管理员可分配使用权，但不因此取得他人的批次。

同一批次并发/重复提交至多生成一批凭证；成功后重放返回原提交结果。预期的账务失败返回 `422 / IMPORT_SUBMIT_FAILED`，包含凭证序号和请求号；异常返回带请求号的 500，整个事务回滚，重新读取状态再决定重试。权限撤销在下一请求生效，已经开始的事务不承诺中途取消。

## 去重和追溯

去重范围为**同一个插件、同一本账**，不绑定用户。文件内容 SHA-256 排序后组成批次指纹，部分文件与其他非取消批次重叠也拒绝。同一创建者重复上传完整同批文件返回既有批次；其他用户得到 `IMPORT_DUPLICATE`，部分重叠为 `IMPORT_FILE_OVERLAP`。换文件名不会绕过内容摘要。跨插件去重需要业务规则，不做自动推断。

失败批次可原位修正、重解析、确认和重试；取消会释放该批次去重占位。已提交批次即使原文件到期仍保留摘要和去重记录，删除正式凭证也不自动解除占位，避免误导入。需重制时先按宿主会计流程处理，并设计显式业务重制规则。

`ctx.imports.origin(voucher_id)` 或 `host.services.imports.origin(voucherId)` 返回当前用户、当前插件有权读取的来源批次、凭证位置和文件摘要；凭证删除后仍保留原凭证 id 和号作为历史证据。批次记录操作者、插件版本/digest、规则/配置快照摘要、确认时间、提交结果和请求号；不修改宿主凭证页面。

## 容量、留存与备份

| 设置 | 默认 | 约束 |
|---|---|---|
| `IMPORT_MAX_BYTES` | 20 MiB | 一批文件合计及预览 JSON 的最大内容；读取同时检查实际大小和压缩展开预算 |
| `IMPORT_MAX_ROWS` | 20,000 | 一批分录上限；示例每行两分录 |
| `IMPORT_MAX_VOUCHERS` | 5,000 | 一批凭证上限 |
| `PLUGIN_IMPORT_MAX_FILES` | 20 | 一批文件数 |
| `PLUGIN_IMPORT_MAX_PENDING` | 50 | 每用户、每账套未完成批次，跨插件计算 |
| `PLUGIN_IMPORT_STORAGE_MAX_BYTES` | 1 GiB | 每账套留存源文件容量，跨插件计算 |
| `IMPORT_BATCH_TTL_DAYS` | 7 天 | 草稿和源文件可用期 |
| `PLUGIN_IMPORT_RETENTION_DAYS` | 30 天 | 提交成功后完整预览、配置和源文件保留期 |

`ctx.imports.limits()` / `host.services.imports.limits()` 返回解析需要的上限。源文件仅通过当前批次文件标识读取/下载，下载强制 attachment、nosniff、private/no-store；SDK 不接受任意宿主文件路径。

运维可在 backend 目录运行：

```bash
python -m app.plugins.imports_cleanup         # 只统计，不修改
python -m app.plugins.imports_cleanup --apply # 每次最多清理 500 批，重复执行至无到期内容
```

清理器跳过正在操作的批次行；到期草稿转为取消，成功批次保留状态、摘要、结果、来源关联和审计，只清除完整内容。没有内置定时调度；需部署方安排此命令。源文件在过期后即禁止下载，即使清理命令尚未执行。

宿主批次、源文件和来源关联，以及插件自己的 book-scoped 配置均参加账套备份恢复；全局插件授权仍按原有 SYSTEM 备份处理。迁移 `0027_plugin_imports` 必须先部署。备份可能含未到期的原文件和配置，按账套备份保护与留存策略保管。

## 前端与宿主服务接口

`host.services` 固定请求当前插件命名空间，统一携带登录凭据。对应路径前缀是 `/api/plugins/{id}/_host`；插件不能注册覆盖保留路径，通配参数路由也排在宿主服务之后。服务端按 manifest 决定实际注册哪些接口。

| 方法与后缀 | 前端入口 |
|---|---|
| GET `/books` | `services.books()` |
| GET / PUT `/config/{table}/{key}?book_id=` | `services.config.get(table,key,bookId)` / `.put(table,key,bookId,expectedVersion,value)` |
| GET `/import-limits` | `services.imports.limits()` |
| POST `/imports?book_id=`，multipart `files` | `services.imports.upload(bookId,files)` |
| GET `/imports?book_id=&before=&limit=` | `services.imports.list(bookId,params)`，返回 `{items,next}`，最多 100 条 |
| GET `/imports/{id}` | `services.imports.read(id)` |
| PUT `/imports/{id}/preview` | `services.imports.edit(id,revision,vouchers)` |
| POST `/imports/{id}/confirm`、`/submit`、`/cancel` | 同名方法 `(id,revision)` |
| GET `/imports/{id}/files/{fileId}` | `services.imports.download(id,fileId)`，返回 Blob |
| GET `/vouchers/{voucherId}/origin` | `services.imports.origin(voucherId)` |

解析需要插件规则，因此由插件自己的 `/parse/{id}` handler 调用 `ctx.imports.preview`，宿主页面接口只允许编辑已存在的预览。标准确认接口表达用户在页面上的明确操作；同源受信任插件仍需审查其界面是否确实让人检查确认，不是对恶意插件的强制人机证明。

`host.api` 仍提供 `get/post/put/patch/delete`、`upload(url,files,params)` 和 `download(url)`。基础 `data.read` 最多 500 行、没有通用过滤和游标，基础 `settings.put` 只对管理员开放；二者不是大规模业务查询与用户账套配置的替代接口。通用期间/凭证查询、附件、后台任务、更多领域事件和每插件执行资源隔离列入 [plugin-roadmap.md](plugin-roadmap.md)，不能当作本版已提供的能力。
