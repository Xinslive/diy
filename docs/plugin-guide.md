# 写一个插件：从零到装上

这一篇是**走通一遍的路径**：按顺序做，每步说明产出什么、哪一步会被拒。
逐字段的权威定义在「受信任插件平台规范」，本篇不复述那些约束，只讲顺序和后果。

两个前提得先说清楚，否则后面每一步都会白做：

- 插件平台**默认关闭**。代码里 `PLUGIN_ENABLED=false`，没打开的话管理页装不了、接口也不认。
- 这里**不是沙箱**。插件的 Python 在宿主进程里跑，JS 在同源页面里跑。窄 SDK、签名和扫描降低的是误装风险，
  不构成隔离。平台把每个包都当作**经管理员书面确认的受信任代码**。这条边界写在 plugin-risk.md 里，
  发布前必须由生产负责人签字，不是开发能自己消掉的。

下面拿示例包 `com.cwq.periodnote`（期间工作笔记）当全程主角，它的全部源码在本页「示例包」一节里。

## 第 1 步：生成签名密钥

在仓库的 `backend/` 目录下：

```bash
python scripts/plugin_package.py keygen \
  --private /secure/plugin-signing.pem \
  --key-id periodnote-2026
```

它做两件事：按 0600 权限写出 Ed25519 私钥，并把公钥打印到标准输出：

```json
{"periodnote-2026": "Pd4lnQRkNjNGIZqx4sCiIMR3I7V7lR3N2qu1ZMGzM7E="}
```

把这段 JSON 填进服务端 `.env` 的 `PLUGIN_TRUSTED_KEYS`，然后重启。**私钥只留在打包机上**：
它不进插件包、不进服务器、不进仓库。丢了这把钥匙，已装的包还能按历史策略启动，但你签不出新版本了。

## 第 2 步：写 plugin.json

放在源目录根下。它声明「装什么、能碰什么、菜单在哪」。

```json
{
  "format_version": 1,
  "id": "com.cwq.periodnote",
  "name": "期间工作笔记",
  "version": "1.0.0",
  "api_version": ">=1.0.0 <2.0.0",
  "permissions": ["book:read", "plugin_data:rw", "audit:write"],
  "access": { "assignment": "admin_users", "default": "none" },
  "contributes": {
    "backend_entry": "backend/main.py",
    "frontend_entry": "frontend/index.js",
    "menu": { "module": "extensions", "title": "期间工作笔记", "icon": "Document", "route": "/plugin/com.cwq.periodnote", "order": 10 }
  },
  "hooks": ["on_period_close"],
  "tables": [{ "name": "plugin_4b8db6655ff5_note", "schema": "public", "scope": "book" }]
}
```

几处最容易写错的：

- `permissions` 里**没有**的能力，运行时压根不存在 —— 拿不到不是报错，是 `AttributeError`。想用账套就先写 `book:read`。
- `access` 目前只能是 `{"assignment":"admin_users","default":"none"}`，写别的直接拒。
- `menu.module` 固定 `extensions`，`icon` 只认白名单里那几个，`route` 只能是 `/plugin/{id}` 及其小写子路径。
- 表名前缀是 `plugin_<sha256(id)[:12]>`。`com.cwq.periodnote` 的前缀就是 `4b8db6655ff5`，对上才装得上。

**`files` 和 `signature` 不要手写**。它们由第 6 步的工具补齐；源模板里写了反而会跟工具算出来的对不上。

## 第 3 步：写后端入口

`backend/main.py`，入口签名固定 `register(registrar, ctx)`：

```python
def register(registrar, ctx):
    def notes(book_id: int):
        ctx.book_read(book_id)          # 先鉴权，再碰数据
        return ctx.data.read(TABLE, book_id=book_id)

    registrar.get("/notes", notes)
```

顺序就是规范里那句：先 `ctx.book_read` / `ctx.book_write` 过宿主权限，再动数据。
SDK 不给你 engine、Session 或 SQL 字符串接口，`ctx.data` 只认你在 manifest 里声明过的表。

`register` 阶段还没有用户上下文，**那时调数据能力会失败** —— 鉴权要在 handler 里做。

## 第 4 步：写迁移

`migrations/versions/0001_note.py`，单根单头的线性 revision：

```python
revision = "0001_note"
down_revision = None

def upgrade():
    op.create_table("plugin_4b8db6655ff5_note",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False))
```

`scope` 写 `book` 的表必须有非空 `book_id` 指向 `book.id`，这一条是硬检查。
迁移只在你显式安装/升级时执行；启动只读版本表，**不跑 DDL**。

## 第 5 步：写前端入口（可选）

`frontend/index.js`，默认导出一个拿 `host` 的工厂：

```javascript
export default function createPeriodNote(host) {
  return { name: 'PeriodNote', setup() { /* 返回渲染函数 */ } }
}
```

`host` 提供 `h / ref / computed / watch / onMounted / api` 和 `bookId() / periodId()`。
`host.api` 固定打在当前插件的命名空间下并自动带上当前登录的 token，不用自己管鉴权头。

首版是预编译自包含 ESM：**不能**用运行时 SFC 编译器、不能 import 外部依赖、不能从 CDN 加载。
需要图标就用手写的 `host.h('span', '…')`，别引组件库。

## 第 6 步：签名打包

```bash
python scripts/plugin_package.py build ../docs/plugin-example \
  --private /secure/plugin-signing.pem \
  --key-id periodnote-2026 \
  --output /tmp/periodnote.cwp
```

输出：

```json
{"id": "com.cwq.periodnote", "version": "1.0.0", "plugin_digest": "6c94…7c93e", "files": 4}
```

工具会自己枚举源目录、算每个文件的 size 和 SHA-256、按 RFC 8785 规范化后签名，再把包打成 `.cwp`。
**私钥和输出包都不能放在源目录里**，放了就直接报错退出 —— 那会把私钥一起打进包里。

上传前可以自己先验一遍：

```bash
python scripts/plugin_package.py verify /tmp/periodnote.cwp --public-keys ./trusted-keys.json
```

`plugin_digest` 相同才说明是同一个包。ZIP 的时间戳、压缩级别、成员顺序变了都不会影响它。

## 第 7 步：上传安装

管理员进「扩展管理」（`/#/plugins`）上传 `.cwp`。这一道闸门最长：完整校验、静态扫描、
staging、迁移、原子 rename、登记。返回 `pending_restart`。

这个返回值的意思经常被读错：**期望状态已经是「启用」，但此刻没有任何代码在跑，也没有任何人有使用权。**
管理页不会显示热加载成功，也不会。这不是故障。

## 第 8 步：授权

新装**不授权任何人**，包括安装者和管理员自己。要用就得显式发：

- `GET /api/system/plugins/{id}/users` 看当前名单；
- `POST /api/system/plugins/{id}/users/{user_id}` 授予单个，或 `PUT .../users` 整体替换。

授权是**用户级**的，不是账套级。用户在哪个账套里能看到你的菜单，仍然由宿主自己的账套权限决定。

## 第 9 步：重启

```bash
python scripts/start_server.py
```

必须走这个入口：它生成一次随机 generation 和启动时刻，由所有子 worker 继承。
手工给每个 worker 单独生成会让 READY 永远不成立。

生产部署时脚本会接着跑 `plugin_status.py --wait 30` 等屏障就绪。
核心 `/api/ready` 绿**不代表**插件 ready，两者是分开的。

## 第 10 步：确认它真的起来了

三件事都对才算成：

1. 扩展管理里该插件是 READY，不是 FAILED；
2. 菜单出现在左侧「扩展」下面，能点开；
3. 未授权用户访问它的业务路径拿到的是 **404**（`PLUGIN_NOT_FOUND_OR_UNAVAILABLE`），不是 200 也不是 403。

第 3 条是刻意设计的：不向没授权的人泄露插件存不存在。

## 装不上时先看错误码

| 错误码 | 通常是什么 |
|---|---|
| `INVALID_MANIFEST` | 字段缺失、类型不对、枚举越界 |
| `UNSAFE_PATH` | 包里有绝对路径、`..`、反斜杠、大小写折叠重名 |
| `INVALID_SIGNATURE` / `UNTRUSTED_SIGNATURE` | 签名算错，或签名 key 不在 `PLUGIN_TRUSTED_KEYS` 里 |
| `TABLE_METADATA_MISMATCH` | 迁移后的真实结构和登记的不一致 |
| `MIGRATION_SCOPE_VIOLATION` | 迁移碰了宿主表或别的插件的表 |
| `DISABLE_AND_RESTART_REQUIRED` | 升级/卸载前的禁用重启没做完 |

扫描失败会带 findings，但**不会返回你的源码或密钥**。

## 升级不是覆盖

顺序是死的：先禁用 → 重启 → 确认所有 worker 都 DISABLED → 才能 `POST .../upgrade`。

升级会先做 SYSTEM + 全部 BOOK 的备份，保留授权，完成后**仍然是禁用状态**。
启用新版本后再重启一次。降级、版本重放、复用已卸载的 id，首版一律拒绝。

## 卸载不会删表

卸载会撤销全部授权、隔离代码，但**保留业务表和 ORPHANED 记录**。
真要删表得单独调 `POST .../{id}/tables/cleanup`，要求填完整插件 id 二次确认，并且会再备份一次。

这是有意的：卸载和删表是两件风险完全不同的事，不该被一个按钮捆在一起。

## 下一步读什么

- **想把文件里的数据写进账上**（批量生成凭证之类）：看 [plugin-data.md](plugin-data.md)，那里有八个能力各长什么样，以及整批回滚和防重复的做法
- 想改包结构、签名算法、限额：看「包和签名」
- 想写后端：看「Python SDK」和「独立迁移与数据库契约」
- 想写前端：看「前端运行时和公共资产」
- 要对外发布：先看 plugin-risk.md，那里有风险边界和需要人工签署的部分
