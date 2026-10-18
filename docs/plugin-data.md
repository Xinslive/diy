# 数据接口：插件怎么读写账上的数

[plugin-guide.md](plugin-guide.md) 教你把插件装起来，这篇讲装好之后它怎么拿到数据、怎么写数据。

读到这里你多半是为了写一个**从文件批量生成凭证**的插件 —— 那正是宿主能力里最绕的一段，所以这篇用这个场景从头讲到尾。文中的代码不是示意，是 [backend/tests/test_plugin_public_api.py](../backend/tests/test_plugin_public_api.py) 里跑过的那一份。

## 八个能力，没别的

插件能用的一切就是下面这些。没声明的属性**不存在**，调用会直接 `AttributeError` —— 这是设计，不是疏漏。

| 权限 | ctx 上的东西 | 干什么 |
|---|---|---|
| `book:read` | `book_read(book_id)` | 账套存在吗、我在不在 |
| `book:write` | `book_write(book_id)` | 我在这个账套能不能写（OWNER/ACCOUNTANT） |
| `subject:read` | `subject_read(book_id)` | 科目表，用于把**科目编码**换成 `subject_id` |
| `ledger:read` | `ledger_read(book_id, period_id)` | 某期间余额 |
| `voucher:write` | `voucher_create(book_id, payload)`<br>`voucher_batch_create(book_id, payloads)` | 建一张 / 整批建多张 |
| `plugin_data:rw` | `data.read/insert/update/delete` | 插件**自己声明的表** |
| `audit:write` | `audit_write(action)` | 记一条审计事件（1–32 字符） |
| `settings:rw` | `settings.get/put` | 插件私有配置，写入要管理员 |

没有的东西同样重要：**没有 engine，没有 Session，没有拼 SQL 字符串的接口，没有直写核心表的接口**。想写凭证只能走 `voucher_create` 或 `voucher_batch_create`，它们会经过宿主全部会计校验；想存数据只能写自己 manifest 里声明的那几张表。

每次调用都重新读数据库里已提交的当前用户与授权，不缓存。管理员撤销授权，下一次请求就失效。

## 第一段：把科目编码换成 subject_id

`voucher_create` 的分录要的是 `subject_id` —— 科目表的主键，比如 `47`。而你的文件里写的是 `1001`。要换就得先读一张科目表：

```python
def chart(book_id: int):
    return ctx.subject_read(book_id)
```

返回当前账套的**全部**科目：

```json
[{"id": 47, "code": "1001", "name": "库存现金", "level": 1,
  "category": "ASSET", "direction": "DEBIT", "is_leaf": true, "active": true}]
```

注意它**不过滤**。宿主自己的 `/subjects/flat` 默认只给末级且启用的，这里刻意不给。原因是插件要能把三种坏情况分开报错：

- 编码查不到 → 「科目 9999 不在这个账套的科目表里」
- 查到了但 `is_leaf` 为假 → 「科目 1001 不能记账」（它是上级科目）
- 查到了但 `active` 为假 → 「科目 1001 不能记账」（已停用）

只给可用集合的话，这三种都塌成一句「找不到」，用户拿到之后得自己去翻科目表。筛选是你自己的判断，宿主不替你做。

## 第二段：整批建凭证

**别用 `voucher_create` 循环。**

这不是风格问题。`voucher_create` 每调用一次就自己提交一次，所以循环 500 行、第 300 行失败时，**前 299 张已经在账上了**，剩下 201 行根本没轮到。

这个仓库里实测过这个形状：循环三张（好、坏、好），接口返回 400、提示也是对的，但数据库里的凭证数从 0 变成 **1** —— 半批留在账上，第三张还没轮到。放大到 500 行就是 299 张躺在账上、要靠人一张张去删，删漏一张账就不平了。

正确做法是 `voucher_batch_create`：整批一个事务，要么全成，要么一张都不留。错误信息会告诉你是第几张。

下面这份是完整可用的（和上面那段合起来了，`chart` 就在里面）：

```python
from fastapi import HTTPException


def register(registrar, ctx):
    def chart(book_id: int):
        return ctx.subject_read(book_id)

    def batch(book_id: int, payload: dict):
        rows = payload.get("rows") or []
        by_code = {s["code"]: s for s in ctx.subject_read(book_id)}
        made = []
        for r in rows:
            entries = []
            for e in r["entries"]:
                s = by_code.get(e["code"])
                if s is None:
                    raise HTTPException(400, "科目 %s 不在这个账套的科目表里" % e["code"])
                if not s["is_leaf"] or not s["active"]:
                    raise HTTPException(400, "科目 %s 不能记账" % e["code"])
                entries.append({"subject_id": s["id"], "direction": e["direction"],
                                "amount": e["amount"], "summary": e.get("summary", "")})
            made.append({"voucher_date": r["date"], "entries": entries,
                         "remark": r.get("remark", ""), "source": "IMPORT"})
        return ctx.voucher_batch_create(book_id, made)

    registrar.get("/chart", chart)
    registrar.post("/batch", batch)
```

请求长这样：

```json
{"rows": [{"date": "2026-03-01", "remark": "三月备用金",
           "entries": [{"code": "1001", "direction": "DEBIT",  "amount": "100.00", "summary": "备用金"},
                       {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": "备用金"}]}]}
```

`direction` 只能是 `"DEBIT"` 或 `"CREDIT"`；`source` 可选 `"MANUAL"` / `"IMPORT"` / `"INVOICE"`，默认 `"MANUAL"`，从文件来的通常该写 `"IMPORT"`。

响应的每一项和 `POST /api/books/{id}/vouchers` 返回的形状一样，所以凭证号、合计、状态你都能直接拿到。

三件事由宿主兜底，不用你在插件里再写一遍：借贷不平会被拒；上级科目不能记账；已结账的期间不能新增。撞号也一样会被拦，并翻译成「凭证号已经有一张了，请改一个号」这种能看懂的话。

### 一批能传多少张

上限跟宿主导入同一个配置项 `import_max_vouchers`，默认 **5000**。超了会明确报错让你拆批，不会截断。凭证是逐张 `flush` 后由外层统一提交的，所以 500 张是一笔事务，中途失败整批回滚。

## 第三段：同一个文件重跑不重复

批量是原子的，所以**失败之后库里一张凭证都没有**，重跑不会重复。这是 `voucher_batch_create` 带来的直接好处。

它管不了的是另一件事：**同一个文件被成功导入两次**。宿主没给插件「查一下这个摘要+金额+日期是不是已经有」的能力，也不打算给 —— 那等于把所有账套的凭证内容开放给插件。

所以去重要插件自己在自己的表里记水位。这不需要任何新权限，`plugin_data:rw` 已经有了。

先在迁移里加一张表（表名必须用 `plugin_<sha256(id)[:12]>_` 前缀）：

```python
from alembic import op
import sqlalchemy as sa

revision = "0001_import"
down_revision = None


def upgrade():
    op.create_table("plugin_<你的12位前缀>_import",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(8), nullable=False))
    op.create_unique_constraint("uq_import_digest", "plugin_<你的12位前缀>_import",
                                ["book_id", "digest"])


def downgrade():
    op.drop_table("plugin_<你的12位前缀>_import")
```

`digest` 是文件内容的 SHA-256，`status` 取 `PENDING` / `DONE` / `FAILED`。`book_id + digest` 有唯一约束。下面代码里的 `IMPORT_TABLE` 就是这个表名。

那个 `status` 不是装饰。去重最容易写成「先查有没有，再插一条」—— 查和插之间有缝，并发两个请求都能查到「没有」，然后都插进去。真正兜住的是 `uq_import_digest` 这个唯一约束：第二个插入会被数据库挡下来。代价是它以数据库异常的形式冒出来，插件那边看到的是 500 而不是一句人话 —— 所以别指望这条路径能给出友好提示，正常的重复导入在上面的 `DONE` 分支早就被拦住了，能走到并发插入的只可能是同一文件被同时传两次。

流程应该是这样：

```python
import hashlib


def batch(book_id: int, payload: dict, raw: bytes = None):
    digest = hashlib.sha256(raw).hexdigest()

    seen = None
    for row in ctx.data.read(IMPORT_TABLE, book_id=book_id, limit=500):
        if row["digest"] == digest:
            seen = row
            break
    if seen is not None:
        if seen["status"] == "FAILED":
            pass                                  # 上次是真失败，允许重跑
        elif seen["status"] == "DONE":
            raise HTTPException(400, "这个文件已经导入过了，没有重复创建")
        else:
            raise HTTPException(400, "这个文件上次导入没跑完。请先到账上确认这一期"
                                      "有没有凭证，再决定要不要重跑。")

    row_id = seen["id"] if seen else ctx.data.insert(
        IMPORT_TABLE, {"digest": digest, "status": "PENDING"}, book_id=book_id)["id"]
    try:
        made = ctx.voucher_batch_create(book_id, build_rows(payload, ctx, book_id))
    except Exception:
        ctx.data.update(IMPORT_TABLE, row_id, {"status": "FAILED"}, book_id=book_id)
        raise
    ctx.data.update(IMPORT_TABLE, row_id, {"status": "DONE"}, book_id=book_id)
    ctx.audit_write("batch_imported")
    return made
```

`build_rows` 是把上面那个 `batch` 里的「读科目表 → 换 subject_id → 组 payload」抽出来的一个函数，签名随意，重点是它返回的就是 `voucher_batch_create` 要的那串字典。

三件事要知道：

1. **先落 `PENDING`，再建凭证，最后改 `DONE`。** 顺序反过来的话，最后那次 `data.update` 失败就等于没记水位，重跑会静默造出第二批一样的凭证。

2. **`PENDING` 卡住不自动重跑。** 崩在「凭证已经建好」和「改成 DONE」之间的时候，宿主分辨不出来 —— 插件读不到凭证，只能凭自己那张表判断。这时宁可拦住让人去账上看一眼，也不要静默重复。

3. **这张表要定期清。** `data.read` 最多返回 500 行，而它没有按列过滤的条件。老记录太多会被挤出查找窗口，一个老文件因此「看起来没导过」。成功导入后删掉几个月前的记录即可：

```python
ctx.data.delete(IMPORT_TABLE, ident=old_id, book_id=book_id)
```

## 别把三件事混在一起

- **宿主核心数据**（凭证、科目、期间）只能通过 `voucher_create` / `subject_read` 走，没有别的口子。
- **插件自己的表**通过 `data.*` 读写，只有 manifest 里声明过的那几张。book 作用域的表每行必须带 `book_id`，global 表的写入要管理员。
- **审计**通过 `audit_write(action)`，`action` 是 1–32 个字符的事件标识，宿主会自动带上用户、插件 id 和 digest。

`data.*` 的四个助手各自一个短事务，**不提供跨助手的事务承诺** —— 别指望「插表 + 建凭证 + 改表」是一笔。要原子性就用 `voucher_batch_create`，它自己那一批是真原子的。

## 边界

- 写凭证要账套写权限。VIEWER 调用一律 403。
- 凭证能建不能删：SDK 没有删除能力，也没有改凭证的能力。要改只能走宿主接口。
- 已记账的凭证、已结账的期间，宿主会直接拒，插件绕不过去。
- 每次调用重读当前授权。撤销后下一次请求即失效，但不会撤回已经返回的响应，也不会中断已经开始的那一笔事务。

## 下一步

- 怎么打包安装：[plugin-guide.md](plugin-guide.md)
- 完整规范（签名、迁移、备份、恢复、前端运行时）：[plugin-spec.md](plugin-spec.md)
- 一个真实的期间笔记插件源码：[plugin-example/](plugin-example/)
