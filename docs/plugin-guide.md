# 快速开始：从零创建一个插件

这篇教程创建一个“期间工作笔记”插件。它包含一个账套级数据表、两个后端接口和一个简单的前端页面，适合先验证完整的开发、打包、安装和授权流程。

教程使用 SDK 1.2.0。命令中的路径按项目目录调整；签名私钥始终放在项目目录之外。

## 第 1 步：准备目录

插件源目录可以从下面的结构开始：

```text
period-note/
├── plugin.json
├── backend/
│   └── main.py
├── frontend/
│   ├── index.js
│   └── style.css
└── migrations/
    └── versions/
        └── 0001_note.py
```

`backend/main.py` 是必需入口；前端和迁移只在插件需要时添加。每个插件的迁移必须保持单根、单头的线性 revision 链。

## 第 2 步：写 manifest

把下面的内容保存为源目录根部的 `plugin.json`：

```json
{
  "format_version": 1,
  "id": "com.example.periodnote",
  "name": "期间工作笔记",
  "version": "1.0.0",
  "api_version": ">=1.2.0 <2.0.0",
  "description": "在当前账套中记录期间工作笔记。",
  "permissions": ["book:read", "plugin_data:rw", "audit:write"],
  "access": { "assignment": "admin_users", "default": "none" },
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
  },
  "hooks": [],
  "tables": [
    {
      "name": "plugin_e3efc9984ace_note",
      "schema": "public",
      "scope": "book"
    }
  ]
}
```

`id` 使用小写的分段标识；`version` 使用 SemVer；`api_version` 是 SDK 兼容范围。`route` 必须以 `/plugin/{id}` 开头，菜单图标使用平台支持的名称。

`tables` 中的表名必须使用插件 id 的哈希前缀。本教程的 `com.example.periodnote` 对应 `e3efc9984ace`，所以示例表名是 `plugin_e3efc9984ace_note`；如果你修改了插件 id，也要同步重新计算表名前缀。

源模板不要添加 `files` 和 `signature`。它们由签名打包工具生成，手工填写会导致验签失败。

## 第 3 步：写后端入口

后端入口固定导出 `register(registrar, ctx)`。在 handler 中先通过宿主的账套能力检查权限，再访问插件表：

```python
from fastapi import HTTPException

TABLE = "plugin_e3efc9984ace_note"


def register(registrar, ctx):
    def list_notes(book_id: int):
        ctx.book_read(book_id)
        return ctx.data.read(TABLE, book_id=book_id)

    def save_note(payload: dict):
        book_id = payload.get("book_id")
        label = payload.get("label")
        if type(book_id) is not int or not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
            raise HTTPException(400, "请选择账套并填写 1 到 120 字的笔记")
        ctx.book_read(book_id)
        result = ctx.data.insert(TABLE, {"label": label.strip()}, book_id=book_id)
        ctx.audit_write("note_saved")
        return result

    registrar.get("/notes", list_notes)
    registrar.post("/notes", save_note)
```

插件接口的实际路径会自动带上 `/api/plugins/{id}` 前缀，因此注册时只写相对路径。`register` 阶段还没有当前用户上下文，不要在模块导入或注册函数里读取账套数据；把鉴权和数据访问放在请求 handler 中。

### POST 处理函数的参数从哪里来

`GET` 惯例是查询串，但 SDK 客户端的 `host.api.post(url, data)` 提交的是 JSON 请求体。两种都支持，路由装配会替你判断：

```python
def save_note(payload: dict):
    ...

def save_rule(book_id: int, payload: dict):
    ...

def undo_run(book_id: int, run_id: int):
    ...
```

第一种写法里 `payload` 收到整个请求体；第二种同样合法，`payload` 收请求体而 `book_id` 走查询串或请求体都行；第三种全是标量，请求体整体由宿主接住。

三条规则：

- 标了 `dict` 的参数收到**整个请求体**，不是某个字段。
- **查询串优先、请求体兜底**。请求体里的 `book_id` 只能当数据字段，不会改写 `book_id` 指向的授权账套——`ctx.data.*` 会自行比对两者是否一致，不一致直接 400。
- 路径参数照常从 URL 取：`/rules/{rule_id}/delete` 里的 `rule_id`。

标量参数不会因为没写 `Body()` 就变成必填查询参数，所以前端不必再重复带一份 `book_id`。`UploadFile = File(...)` 这类文件上传路由不受这套规则影响。

## 可选：注册定时任务

需要周期处理时，在 manifest 中增加声明。使用这项能力的插件，其 `api_version` 范围必须包含 SDK 1.2.0：

```json
"tasks": [
  {"name": "sync_remote", "schedule": {"type": "interval", "minutes": 15}}
]
```

在上文的 `register` 函数内部增加处理函数及注册语句；任务名必须与 manifest 一致，每个已声明任务都必须注册：

```python
def register(registrar, ctx):
    # 此处保留前面的接口注册代码。
    def sync_remote(task):
        # 本例需要 audit:write；ctx 已绑定 task.user_id 对应的授权。
        ctx.audit_write("remote_sync")

    registrar.task("sync_remote", sync_remote)
```

也可将 schedule 改为 `{"type": "cron", "expression": "10 2 * * *", "timezone": "Asia/Shanghai"}`，表示每天北京时间 02:10。处理函数必须同步、接收一个任务上下文；不要在插件中另外启动循环或定时线程。

管理员通过 `PUT /api/system/scheduler/tasks/{plugin_id}/sync_remote` 提交 `{"enabled": true}` 启用。开关覆盖整个任务，每次到期时宿主会按当前有效的插件授权分别调用每个 active 用户。新增授权用户在下一周期自动加入；用户撤权、账号停用后被排除。处理函数中的 `ctx.book_list()`、账套读写和凭证能力仍按当前用户的账套角色实时校验。

`task.idempotency_key` 在同一次运行的重领中保持不变，可用于用户级外部写入。多个用户可能有权操作同一本账，记账插件还需要按“账套 + 业务规则 + 期间”等业务键做持久去重，避免给共享账套重复记账。网络请求应设置有限超时；普通失败等待下一周期，进程崩溃后的重领采用至少一次执行语义。全部上下文字段和失败规则见[插件规范](plugin-spec.md#定时任务)。

## 第 4 步：写迁移

`migrations/versions/0001_note.py` 创建插件自己的账套表：

```python
from alembic import op
import sqlalchemy as sa

revision = "0001_note"
down_revision = None


def upgrade():
    op.create_table(
        "plugin_e3efc9984ace_note",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
    )


def downgrade():
    op.drop_table("plugin_e3efc9984ace_note")
```

`scope: "book"` 的表必须有非空 `book_id`，并且只能引用宿主的 `book.id` 和插件允许的表。迁移只在安装或升级时执行，服务启动不会偷偷执行 DDL。

## 第 5 步：写前端入口

前端模块默认导出一个接收 `host` 的组件工厂。下面的代码只使用宿主提供的 Vue 运行时：

```javascript
export default function createPeriodNote(host) {
  return {
    name: 'PeriodNote',
    setup() {
      const notes = host.ref([])
      const label = host.ref('')
      const message = host.ref('')

      async function load() {
        const bookId = host.bookId()
        if (!bookId) {
          message.value = '请先选择账套'
          return
        }
        try {
          notes.value = await host.api.get('/notes', { book_id: bookId })
          message.value = ''
        } catch {
          message.value = '读取失败，请检查插件授权和账套权限'
        }
      }

      async function save() {
        try {
          await host.api.post('/notes', { book_id: host.bookId(), label: label.value })
          label.value = ''
          await load()
        } catch {
          message.value = '保存失败，请检查输入和账套权限'
        }
      }

      host.onMounted(load)
      host.watch(() => host.bookId(), load)
      return () => host.h('section', [
        host.h('h1', '期间工作笔记'),
        host.h('textarea', {
          value: label.value,
          maxlength: 120,
          'aria-label': '工作笔记',
          onInput: event => { label.value = event.target.value },
        }),
        host.h('button', { type: 'button', onClick: save }, '保存笔记'),
        message.value ? host.h('p', { role: 'alert' }, message.value) : null,
        host.h('ul', notes.value.map(note => host.h('li', { key: note.id }, note.label))),
      ])
    },
  }
}
```

`host.api` 会自动请求当前插件的命名空间并携带登录凭据。组件卸载时不要留下全局事件、定时器或样式；需要清理时使用 `host.onUnmounted`。

## 第 6 步：签名并验证

在项目的 `backend/` 目录生成密钥：

```bash
python scripts/plugin_package.py keygen \
  --private /secure/plugin-signing.pem \
  --key-id periodnote-2026
```

把命令输出的公钥加入运行环境的可信发布者配置，然后构建 `.cwp`：

```bash
python scripts/plugin_package.py build ../period-note \
  --private /secure/plugin-signing.pem \
  --key-id periodnote-2026 \
  --output /tmp/period-note.cwp
```

上传前先离线验证：

```bash
python scripts/plugin_package.py verify /tmp/period-note.cwp \
  --public-keys ./trusted-keys.json
```

工具会重新计算文件清单、Ed25519 签名和包摘要。私钥不进入源目录、插件包、服务器或 Git 仓库；输出包也不要放回源目录，否则下一次打包可能把它当成插件资产。

## 第 7 步：安装并授权

由管理员在扩展管理页面上传 `.cwp`。安装完成后，插件仍需要两件事才会对用户可用：

1. 在授权名单中加入需要使用插件的用户；
2. 按平台提示重启服务，让所有 worker 加载同一个插件版本。

新安装默认不授权任何用户，包括安装者和管理员。插件菜单是否出现由用户授权决定；用户在插件页面内选择账套，后端每次请求仍会重新检查该账套的权限。

## 第 8 步：确认插件可用

至少检查以下路径：

- 管理页面显示插件已就绪；
- 已授权用户能看到菜单并打开页面；
- 页面能读取当前账套并保存一条笔记；
- 只读账套不能执行写入；
- 未授权用户不能通过手写 URL 访问插件业务接口。

如果页面加载失败，先看“排错与错误码”，再检查签名 key、manifest、迁移和重启状态。不要用关闭验签或跳过迁移检查的方式排查问题。

## 跨账套的插件：共享卡对账

最简单的插件一个账套打转。**一张卡被多个账套共用**时（各账套的科目编码还不一样，
而这张卡只有一份对账单），要处理的东西多起来：谁认领这张卡、一行流水归哪本账、
各账套记到这张卡上的数加起来对不对得上那张卡。仓库里的
**`plugins/com.cwq.sharedcard`**（共享卡对账）是这一类的完整参考实现，几个要点：

- **表分两种 scope**：卡本身是**全局表**（跨账套的公司层面配置，只有管理员能
  写），成员、对账单、流水行、归属台账是**账套表**（各账套的业务数据，
  跟着账套走，账套级备份才覆盖得到）。
- **解析不自己写**：声明 `statement:parse`，用 `ctx.statement.parse()` 走宿主
  那一份解析器 —— 同一份银行文件不会有两个答案。
- **跨账套的账期可以不一样**：用 `ctx.period_list(book_id)` 拿各账套自己的
  期间，不要拿一份期间号套到别的账套上。
- **只对账，不制证**：插件**不替用户记账** —— 凭证在宿主那边怎么录、记到哪个
  对方科目是用户自己的账务决定。所以插件里没有「生成凭证」，manifest 里也不该
  声明 `voucher:write`（声明了就是给了记账的权限）。
- **不为「替谁」做选择题**：共享卡那份对账单是**为这张卡上传的**，不属于任何一本
  账；存在哪本账的表里只是**存储位置**（让账套级备份覆盖得到它），由后端挑这本
  卡的**成员账套**。把实现细节摆成「替哪本账上传」的下拉，用户就得替系统做一个
  它本来不需要做的决定。
- **别替用户猜归属**：一行流水归哪本账是**用户的判断**（插件不制证，就更不该替
  用户猜）。共享卡 1.2.0 起把归属规则整块下线了 —— 规则按对方户名猜，猜错就是
  把钱记到别的账上。**下线一整块功能也要留墓碑接口**（回 422 并说清「这个功能
  已经下线，每一行都要人工指定」），别让老页面拿到一个没有信息量的 404。
- **空态结论要带限定**：还有行没归属时，不能说「对得上、没有少记」——
  **没比过的数不能说成比过了**。同一屏上提到同一件事的两句话都要改，漏一处就
  还是半句话。
- **读表别每次都反射 schema**：宿主 `ctx.data.read` 每次读表都会
  `Table(..., autoload_with=...)` 重新反射一次（实测 21ms/次，复用同一表对象
  3.5ms）。插件一次请求读几十次表，加上 `PLUGIN_ISOLATION=off` 时这些同步读取
  发生在**宿主 worker 的事件循环里** —— 卡住的是整个宿主。宿主侧已经加了按
  `(插件 id, 表名)` 的**进程级表对象缓存**（迁移提交/清表时显式失效），插件侧
  也要**每张表只读一遍**、在内存里分组，别让读次数随卡数涨。
- **科目按「一级科目」判**：一张卡只允许挂在**一级科目是 1001 / 1002** 的科目上
  （拿 1012 其他货币资金承载银行卡，余额永远对不上）。`ctx.subject_read` 不给
  `parent_id`，一级科目就是 `level == 1` 的那些行 —— **在插件里算好
  `root_code` 再给前端**，前端不要自己拿编码前缀猜：两边口径不一致时会出现
  「下拉里选得到、一提交就被拒」。
- **核对分借贷两侧比，不比净额**：账上这个现金科目这一期的借方/贷方发生额
  （`ctx.ledger_read(book_id, period_id, include_unposted=True)` 的
  `period_debit` / `period_credit`）vs 按对账单这本账该有多少收款、多少支出。
  比净额的话，一笔 30 万的支出没记，差额是负数，界面上「少记 -30 万」没法读；
  分两侧比，「少记了 30 万的支出」没有歧义，**方向记反了也能看出来**。
  ⚠️ 净发生额的正负要看**科目自己的常态方向**（`Subject.direction`），**不能**
  看余额表那行的 `direction` —— 那行给的是「余额现在落在借方还是贷方」
  （银行存款透支时落在贷方），拿它定符号会把「本月少了 30 万」算成「多了 30 万」。
- **下线一个字段要显式接住它并报错**：别指望「schema 里删掉它」就够了 ——
  悄悄丢掉的话，接口回 200、界面回一句「已保存」，而用户以为设的东西根本没生效。
- **重解析不冲用户做过的事**：重新解析按「第几行」锚定，**就地改内容、行 id 不变**
  —— 用户手工指定的归属不会因为一次重解析被抹掉（行 id 一变，归属就得跟着改指向，
  一来一回最容易串行）。两条底线：**行数变了就不结转归属**（锚本身动了，按位置
  结转会把钱挂到别的行上）；**没归属的行按新解析刷新**（这才是重解析的目的）。

## 接下来做什么

完成最小插件后，阅读“Manifest、权限与运行时参考”了解完整 capability 和宿主 API；需要文件转凭证时，直接按“数据导入 API”的上传、预览、确认、提交顺序实现，并参考页面下方的 CSV 转凭证示例。
