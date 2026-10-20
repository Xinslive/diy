"""插件静态扫描的白名单清单 —— 能力面收敛的唯一事实来源。

为什么是白名单而不是黑名单：黑名单是**无穷枚举**。已核实至少 18 个等价入口
绕过了旧黑名单（``posix``/``pty``/``signal``/``asyncio``/``smtplib``/``ftplib``/
``ssl``/``runpy``/``pkgutil``/``code``/``sqlalchemy``/``psycopg2``/``httpx``/
``urllib3``/``inspect``/``tempfile``/``base64``/``codecs``/``zlib``）。只要漏一个，
「扫描用于拦截明显危险行为」就是空话；实测漏掉 ``posix`` 之后，签名插件可以
``__import__("app.core.db")`` 取到宿主 SessionLocal，直接读写 ``sys_user`` 并把
普通账号提成管理员。

因此这里的规则是：**不在清单里的模块一律不许 import**。每条都写明为什么允许；
新增条目必须在这里给出理由（宿主评审后改这一份清单，不要在别处加特例）。

扫描器本身在 :mod:`app.plugins.security`。这里是**策略**，那里是**判定**。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleRule:
    """一个可 import 的模块，以及它的能力边界。

    reason: 为什么允许这个模块（审计清单的正文）。
    symbols: ``from X import name`` 时允许的符号名；``None`` 表示不限制符号。
        非 ``None`` 时**必须**用 from 形式导入，裸 ``import X`` 会被拒绝。
    denied: 该模块命名空间上禁止的属性名，同时也是禁止 from-import 的名字。
    symbol_denied: ``((符号名, 禁止属性集合), ...)``，用于「模块里的某个对象
        自身带危险方法」的情形（例如 alembic 的 ``op.execute``）。
    requires_any: 需要 manifest 声明其中之一 capability 才允许；空 = 不需要。
    """

    reason: str
    symbols: frozenset | None = None
    denied: frozenset = frozenset()
    symbol_denied: tuple = ()
    requires_any: tuple = ()

    def denies(self, symbol: str | None, attribute: str) -> bool:
        if symbol is None:
            return attribute in self.denied
        for name, attributes in self.symbol_denied:
            if name == symbol:
                return attribute in attributes
        return False


def _rule(reason, symbols=None, denied=(), symbol_denied=(), requires_any=()) -> ModuleRule:
    return ModuleRule(
        reason=reason,
        symbols=None if symbols is None else frozenset(symbols),
        denied=frozenset(denied),
        symbol_denied=tuple((name, frozenset(items)) for name, items in symbol_denied),
        requires_any=tuple(requires_any),
    )


# ---------------------------------------------------------------------------
# 插件后端代码（backend/** 及其它非迁移 Python 文件）允许 import 的模块
# ---------------------------------------------------------------------------
BACKEND_MODULES = {
    # —— 编译器指令：不产生任何运行时能力 ——
    "__future__": _rule("编译器指令（from __future__ import annotations）"),

    # —— 纯计算/容器：没有 I/O、没有进程、没有网络、没有反射面 ——
    "abc": _rule("抽象基类：纯类型工具"),
    "collections": _rule("容器类型"),
    "collections.abc": _rule("抽象容器类型"),
    "contextlib": _rule("上下文管理器工具"),
    "csv": _rule("CSV 解析：只处理内存里的文本"),
    "dataclasses": _rule("数据类：结构化数据"),
    "datetime": _rule("日期时间运算"),
    "decimal": _rule("金额精确计算"),
    "difflib": _rule("文本差异比较"),
    "enum": _rule("枚举"),
    "fractions": _rule("分数运算"),
    "functools": _rule("函数工具"),
    "itertools": _rule("迭代器工具"),
    "json": _rule("JSON 编解码：只处理内存里的数据"),
    "math": _rule("数学函数"),
    "operator": _rule("标准运算符"),
    "re": _rule("正则表达式"),
    "statistics": _rule("统计函数"),
    "string": _rule("字符串常量"),
    "textwrap": _rule("文本排版"),
    "typing": _rule("类型注解"),
    "unicodedata": _rule("Unicode 字符属性"),
    "uuid": _rule("UUID 生成：不读写任何外部系统"),

    # —— 内存缓冲：文件打开由属性规则单独拒绝（io.open）——
    "io": _rule("内存缓冲（BytesIO/StringIO）：磁盘文件由 io.open 打开，已被属性规则拒绝"),

    # —— 框架与第三方库：插件路由和用户上传文件解析确实需要 ——
    "fastapi": _rule(
        "插件路由需要的 HTTP 异常、上传对象与路由类型；不暴露 Request/Response/Depends，"
        "因为注册器本来就拒绝这些宿主对象",
        symbols={"HTTPException", "File", "UploadFile", "Form", "Body", "Query", "APIRouter", "status"},
    ),
    "openpyxl": _rule(
        "解析用户上传的 xlsx：只吃内存里的字节，不自己打开文件",
        symbols={"load_workbook"},
        requires_any=("upload:read", "import:rw"),
    ),
}


# ---------------------------------------------------------------------------
# 插件迁移（migrations/**）额外允许的模块
#
# 迁移由宿主 alembic 以**宿主连接**执行，因此它的能力面比后端代码大：
# 只有 op 的 DDL 操作与列类型构造器是开放的，任何「自己再开一条连接」或
# 「执行任意 SQL」的入口都被拒绝（见 denied / symbol_denied）。
# ---------------------------------------------------------------------------
MIGRATION_MODULES = {
    "alembic": _rule(
        "迁移 DDL 操作入口：只开放 op 的建表/改表能力",
        symbols={"op"},
        symbol_denied=(("op", {"execute", "get_bind", "get_context"}),),
    ),
    "sqlalchemy": _rule(
        "迁移列类型构造器（sa.Column/sa.Integer/...）；自己开连接与裸 SQL 入口被拒绝",
        denied={
            "create_engine", "create_async_engine", "create_mock_engine", "engine_from_config",
            "create_pool_from_url", "create_connection", "connect", "sessionmaker",
            "scoped_session", "text", "exec_driver_sql", "make_url",
        },
    ),
    "sqlalchemy.dialects.postgresql": _rule(
        "PostgreSQL 专有列类型（JSONB/UUID/...）",
        symbols={"JSONB", "UUID", "ARRAY", "INET", "HSTORE"},
    ),
}


# ---------------------------------------------------------------------------
# 任何模块命名空间上都不允许出现的属性名
#
# 这些名字只会出现在「拿到宿主能力」之后才有意义：进程执行、文件系统改写、
# 打开数据库连接、提交事务。因为白名单已经挡掉了提供它们的模块，这里是
# 第二道：万一某个允许的模块间接暴露了同名方法，也不能被调用。
# ---------------------------------------------------------------------------
DENIED_ATTRIBUTES = frozenset({
    # 打开文件：io.open / Path.open —— 插件的字节一律由宿主以参数交给它
    "open",
    # 进程执行
    "execv", "execve", "execl", "execvp", "spawn", "spawnv", "spawnl", "fork", "forkpty",
    "system", "popen",
    # 信号：可以杀掉宿主 worker
    "kill", "raise_signal", "alarm",
    # 文件系统改写
    "unlink", "rmdir", "removedirs", "rename", "chmod", "chown", "listdir", "scandir",
    "walk", "mkdir", "makedirs", "symlink", "link", "truncate", "write_text", "write_bytes",
    # 数据库连接与事务
    "connect", "load_library", "commit", "rollback", "autocommit_block", "execution_options",
})


# ---------------------------------------------------------------------------
# 内置危险名：按「Name 引用形态」判定，而不是只在调用形态判定
#
# 旧扫描器只在 ``ast.Call`` 且 func 是 ``ast.Name`` 时才看这些名字，于是
# ``imp = __import__`` / ``e = eval`` / ``f = open`` 之后 ``imp(...)`` 全部放行。
# 下面两档分别对应「任何读取都拒绝」和「只在调用位置拒绝」：
# ---------------------------------------------------------------------------
# 读取即拒绝：这些名字在插件里只有一种用法 —— 拿内建能力本身。
DENIED_NAMES_ON_LOAD = frozenset({
    "__import__", "eval", "exec", "compile", "getattr", "setattr", "delattr",
    "globals", "locals", "open",
})

# 只在调用位置拒绝：这些名字也常被当作普通变量名（``input``、``vars``），
# 按读取拒绝会误伤；按调用拒绝既能挡住 ``input()`` 读 stdin 卡住 worker，
# 也能挡住 ``bp = breakpoint`` 之后 ``bp()``。
DENIED_NAMES_ON_CALL = frozenset({
    "input", "breakpoint", "vars",
})
