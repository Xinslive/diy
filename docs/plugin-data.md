# 数据导入 API：从文件到整批凭证

SDK 1.1.0 提供一条完整的文件导入链路：**上传 → 插件解析 → 持久预览 → 人工调整 → 确认 → 整批提交**。

插件负责文件格式、业务规则、科目映射和页面交互；宿主负责文件留存、账套权限、会计校验、事务、去重和凭证来源追溯。每个批次只对应一本账套。

## 需要声明的 capability

文件导入插件通常需要：

- `book:read`：列出用户可以访问的账套；
- `subject:read`：把文件里的科目编码映射为当前账套的科目 id；
- `import:rw`：使用标准上传、预览和批次服务；
- `voucher:write`：允许整批提交正式凭证；
- `plugin_data:rw`：保存插件自己的账套配置时使用。

每次请求仍会重新检查用户授权和目标账套角色。前端收到的 `canWrite` 只是界面提示，不是提交凭据。

## 一条完整工作流

### 1. 让用户选择目标账套

前端可以使用 `host.services.books()`：

```javascript
const books = await host.services.books()
const writable = books.filter(book => book.canWrite)
```

如果用户选择只读账套，页面可以允许查看历史批次，但上传和提交按钮应当禁用。后端在上传、解析、确认和提交时都会再次检查。

### 2. 上传文件

```javascript
const batch = await host.services.imports.upload(bookId, files)
```

上传成功后返回 `UPLOADED` 批次、文件摘要和 `revision`。宿主保存原始字节并计算 SHA-256；插件不需要也不能访问宿主文件路径。

### 3. 由插件解析文件

解析 handler 读取批次和源文件：

```python
def parse(ident: str, payload: dict):
    batch = ctx.imports.read(ident)
    source = batch["files"][0]
    content = ctx.imports.source(ident, source["id"])["content"]
    vouchers = parse_csv(content)
    return ctx.imports.preview(
        ident,
        vouchers,
        revision=payload["revision"],
        configuration={"rules": "csv-v1"},
    )
```

`ctx.imports.preview` 会运行宿主凭证校验并保存预览，但不会创建正式凭证。解析错误应包含文件名和行号；宿主校验错误会包含凭证序号，插件可以用自己的位置映射把它展示回源文件。

### 4. 让用户检查并保存调整

页面读取批次：

```javascript
const current = await host.services.imports.read(batch.id)
const saved = await host.services.imports.edit(
  batch.id,
  current.revision,
  editedVouchers,
)
```

金额使用十进制字符串，例如 `"12.50"`，不要先转成 JavaScript `number`。预览必须满足账套归属、末级且启用、期间开放、金额精度和借贷平衡等宿主规则；有校验错误的预览不能确认。

### 5. 确认并提交

```javascript
const confirmed = await host.services.imports.confirm(batch.id, batch.revision)
const result = await host.services.imports.submit(batch.id, confirmed.revision)
```

确认代表用户看过当前预览。提交时宿主会在同一个数据库事务里创建全部凭证、来源关联、结果和审计记录；任何一张凭证失败，整批回滚。提交成功后不能通过批次接口改写或取消正式凭证。

## 批次状态

| 状态 | 含义 | 下一步 |
|---|---|---|
| `UPLOADED` | 文件已留存，等待解析 | 调用插件解析接口 |
| `PREVIEW_READY` | 预览通过宿主校验 | 让用户检查并确认 |
| `EDITING` | 用户已调整，或预览有错误 | 保存修正后的预览 |
| `CONFIRMED` | 当前修订已经确认 | 提交整批凭证 |
| `SUBMITTING` | 只存在于提交事务期间 | 不要重复创建请求 |
| `SUBMITTED` | 凭证、来源和结果已提交 | 展示结果和来源 |
| `FAILED` | 整批回滚并保存可预期错误 | 修正、保存、重新确认 |
| `CANCELLED` | 批次关闭，完整内容被清理 | 重新上传文件 |

所有编辑、确认、提交和取消操作都必须携带读取到的整数 `revision`。页面过期时返回 `409 / IMPORT_REVISION_CONFLICT`，应重新读取批次并让用户决定是否继续，不能静默覆盖新版本。

## 账套配置与版本

插件的科目映射等差异化配置应保存在自己的 `scope: "book"` 表中，并使用标准配置服务：

```javascript
const snapshot = await host.services.config.get(TABLE, 'mapping', bookId)
const saved = await host.services.config.put(
  TABLE,
  'mapping',
  bookId,
  snapshot.version,
  { debit_code: '1001', credit_code: '6001' },
)
```

`expected_version` 使用比较并更新语义。两个页面同时保存时，只有一个能成功，另一个收到配置版本冲突。解析时把配置版本和摘要放入批次配置引用；确认和提交会再次核对。配置发生变化时，批次必须重新解析并确认。

配置表通常包含 `id`、`book_id`、`key`、`value`、`version`，并对 `(book_id, key)` 建立唯一约束。

## 凭证草稿格式

`vouchers` 是列表；每个金额为十进制字符串，分录使用当前账套的 `subject_id`：

```json
[
  {
    "voucher_date": "2026-03-02",
    "remark": "收款.csv:2",
    "attachments": 0,
    "entries": [
      { "subject_id": 47, "direction": "DEBIT", "amount": "12.50", "summary": "收款" },
      { "subject_id": 83, "direction": "CREDIT", "amount": "12.50", "summary": "收款" }
    ]
  }
]
```

批次使用宿主自动编号，因此不能指定 `voucher_no` 或插入位置。附件数量可以记录单据数，但不等于通用附件已经上传；文件导入使用批次文件服务。

## 去重、幂等与来源追溯

去重范围是“同一个插件、同一本账套”。宿主按文件内容摘要计算批次指纹：

- 同一创建者重复上传完整同批文件，会返回已有批次；
- 其他用户上传同一批文件，会得到 `IMPORT_DUPLICATE`；
- 只重叠部分文件，会得到 `IMPORT_FILE_OVERLAP`；
- 取消批次会释放去重占位，提交成功的批次会保留摘要。

同一批次的并发或重复提交最多创建一批凭证；提交成功后的重试返回原结果。`host.services.imports.origin(voucherId)` 可以返回当前用户有权查看的来源批次、文件和位置。

## 标准服务方法

| 前端方法 | 作用 |
|---|---|
| `services.imports.limits()` | 读取当前宿主的文件、凭证和留存上限 |
| `services.imports.upload(bookId, files)` | 创建上传批次 |
| `services.imports.list(bookId, params)` | 分页读取当前用户的批次 |
| `services.imports.read(id)` | 读取批次详情和当前预览 |
| `services.imports.edit(id, revision, vouchers)` | 保存人工调整后的预览 |
| `services.imports.confirm(id, revision)` | 确认当前修订 |
| `services.imports.submit(id, revision)` | 原子提交整批凭证 |
| `services.imports.cancel(id, revision)` | 取消草稿并释放占位 |
| `services.imports.download(id, fileId)` | 下载批次中的源文件 Blob |
| `services.imports.origin(voucherId)` | 查询正式凭证的来源 |

对应的后端 `ctx.imports` 方法名称相同。标准服务位于当前插件的 `/_host` 命名空间，插件不能覆盖这些路径。

## 容量与留存

默认上限如下；实际运行环境可以进一步收紧，解析时应使用 `limits()` 返回的值：

| 项目 | 默认值 |
|---|---:|
| 单批文件数 | 20 |
| 单批文件和预览内容 | 20 MiB |
| 单批凭证数 | 5,000 |
| 单批分录数 | 20,000 |
| 草稿内容留存 | 7 天 |
| 提交成功后的完整内容留存 | 30 天 |

批次到期后，来源摘要、提交结果和审计信息可以保留，完整源文件和预览不保证永久可下载。大文件应拆成多个批次，并在页面中展示当前限制。

## 常见错误处理

| 错误 | 页面处理 |
|---|---|
| `IMPORT_REVISION_CONFLICT` | 重新读取批次，提示用户当前页面已过期 |
| `IMPORT_PLUGIN_CHANGED` | 使用当前插件重新解析，不能复用旧预览 |
| `IMPORT_CONFIGURATION_CHANGED` | 重新读取配置、解析并确认 |
| `IMPORT_DUPLICATE` / `IMPORT_FILE_OVERLAP` | 展示已有批次或冲突文件，不要改名重试 |
| `IMPORT_SUBMIT_FAILED` | 展示凭证序号和错误，修正后重新确认 |
| 500 + request id | 重新读取批次，再决定是否重试，并保留 request id |

解析错误应尽早在插件内返回；宿主错误不要被吞掉。用户需要知道哪一个文件、哪一行或哪一张凭证需要处理。
