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

**这份清单单独用是不够的（不要再把它当闭环）**
----------------------------------------------------
上面那次「漏掉 posix」的事故**并没有因为加了白名单就结束**。审计 §3.1 实测：
静态扫描**看不见字符串常量**，于是
``operator.attrgetter("__globals__")(f)["__builtins__"]["__import__"]("os")``
这条链**扫描判 CLEAN、运行期拿到宿主任意能力**，后果与当初一模一样
（把普通账号提成管理员）。

根因不是「又漏了一个等价入口」，而是**静态扫描按形态判定、而字符串是形态的
盲区**。所以：

- 这里的规则是**纵深防御的一层**，不是边界；
- 边界在 :mod:`app.plugins.sandbox`（运行期裁剪内建能力与导入面）：
  插件拿不到 ``getattr``/``__import__``/``exec``/``open``，
  于是「字符串 → 属性访问」这条路**结构上不存在**；
- 真正的边界仍然只有一条：**插件后端代码应当跑在独立进程/容器里**，
  只经 IPC 暴露受限 API（见审计 §7 第 6 步）。

新增模块或放宽规则时，请同时问一句：这条规则在**运行期**是否也成立？
只有静态一层、运行期放行，等于没写。

扫描器本身在 :mod:`app.plugins.security`。这里是**策略**，那里是**判定**。
"""
from __future__ import annotations

import ast
import functools
import os
import sys
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
    symbol_wrap: ``((符号名, 包装工厂), ...)``，用于「该符号要换成**更窄**的
        实现」的情形（例如 ``openpyxl.load_workbook`` 只收内存字节流）。
        工厂签名是 ``factory(原符号) -> 新符号``。
    requires_any: 需要 manifest 声明其中之一 capability 才允许；空 = 不需要。
    """

    reason: str
    symbols: frozenset | None = None
    denied: frozenset = frozenset()
    symbol_denied: tuple = ()
    symbol_wrap: tuple = ()
    requires_any: tuple = ()

    def denies(self, symbol: str | None, attribute: str) -> bool:
        if symbol is None:
            return attribute in self.denied
        for name, attributes in self.symbol_denied:
            if name == symbol:
                return attribute in attributes
        return False


def _rule(reason, symbols=None, denied=(), symbol_denied=(), symbol_wrap=(), requires_any=()) -> ModuleRule:
    return ModuleRule(
        reason=reason,
        symbols=None if symbols is None else frozenset(symbols),
        denied=frozenset(denied),
        symbol_denied=tuple((name, frozenset(items)) for name, items in symbol_denied),
        symbol_wrap=tuple((name, factory) for name, factory in symbol_wrap),
        requires_any=tuple(requires_any),
    )


# ---------------------------------------------------------------------------
# 插件后端代码（backend/** 及其它非迁移 Python 文件）允许 import 的模块
# ---------------------------------------------------------------------------
def _bytes_only_workbook(func):
    """把 ``load_workbook`` 收窄成「只吃内存里的字节」。

    能力清单给 openpyxl 写的理由是「只吃内存里的字节，**不自己打开文件**」，
    但 ``load_workbook`` 实际能接**路径**：传路径它就自己去开那个文件。
    于是插件多了一条受限的文件读 —— 能读宿主上任意 xlsx/zip，
    连目录结构都能从 openpyxl 的报错信息里读出来。

    这个包装把「理由」变成**强制**：只收文件对象，路径一律拒。
    插件的正常用法（``load_workbook(io.BytesIO(content))``）不受影响。
    """

    @functools.wraps(func)
    def wrapper(filename, *args, **kwargs):
        if isinstance(filename, (str, bytes, os.PathLike)):
            raise TypeError("load_workbook 只接受内存里的字节流，不接受路径")
        return func(filename, *args, **kwargs)

    return wrapper


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
    "operator": _rule(
        "标准运算符；**反射类原语（attrgetter / attrsetter / methodcaller）不给** —— "
        "它们是「字符串 → 属性访问」的执行器："
        "``operator.attrgetter(\"__globals__\")(f)`` 拿到的就是函数的作用域字典，"
        "而属性规则只认 ``x.__globals__`` 这种字面量形态，看不见字符串里藏的那个",
        denied={"attrgetter", "attrsetter", "methodcaller"},
    ),
    "re": _rule("正则表达式"),
    "statistics": _rule("统计函数"),
    "string": _rule(
        "字符串常量；**Formatter 不给** —— 它的 ``get_field`` 会拿插件给的字段名"
        "去做 ``getattr``（``\"0.__class__\"`` 这种写法就绕过了属性规则）",
        denied={"Formatter"},
    ),
    "textwrap": _rule("文本排版"),
    "typing": _rule("类型注解"),
    "unicodedata": _rule("Unicode 字符属性"),
    "uuid": _rule("UUID 生成：不读写任何外部系统"),

    # —— 内存缓冲：文件打开由属性规则单独拒绝（io.open）——
    #
    # **denied 里的 ``open`` 不是重复劳动**（审查 S-01）。属性规则只认
    # ``io.open(...)`` 这种**属性形态**；而 ``from io import open as fh``
    # 之后拿到的是一个**裸名字**，属性规则永远碰不到它，``DENIED_NAMES_ON_LOAD``
    # 也只判「读取这个名字」，看不到它被 import 时就换了名字。
    # 于是这一行曾经是完全放行的：签名插件可以 ``from io import open as fh``
    # 然后 ``fh('/etc/hostname').read()`` —— 宿主 worker 的整个文件系统对它敞开。
    # 现在它**在 import 这一步**就被拒，理由写在这里而不是只写在扫描器里。
    #
    # ``FileIO`` 与 ``open_code`` 是同一条通道上的另外两个门（审计 §3.1 实测
    # ``io.FileIO(path).read()`` 读到了 ``/etc/hosts`` 与 ``~/.ssh/config``）：
    # 它们不经过 ``io.open``，所以**光禁 open 是不够的**。三个一起禁，
    # ``io`` 就只剩纯内存的 BytesIO / StringIO 了。
    "io": _rule(
        "内存缓冲（BytesIO/StringIO）：磁盘文件入口在 import 阶段就拒绝，"
        "别名导入（from io import open as fh）同样拒绝",
        denied={"open", "open_code", "FileIO"},
    ),

    # —— 框架与第三方库：插件路由和用户上传文件解析确实需要 ——
    "fastapi": _rule(
        "插件路由需要的 HTTP 异常、上传对象与路由类型；不暴露 Request/Response/Depends，"
        "因为注册器本来就拒绝这些宿主对象",
        symbols={"HTTPException", "File", "UploadFile", "Form", "Body", "Query", "APIRouter", "status"},
    ),
    "openpyxl": _rule(
        "解析用户上传的 xlsx：只吃内存里的字节，不自己打开文件。"
        "**这条理由由 symbol_wrap 强制**：``load_workbook`` 原本能接路径，"
        "传路径它就自己去开那个文件 —— 插件等于多了一条受限的文件读"
        "（能读宿主上任意 xlsx/zip，目录结构还能从报错信息里读出来）。"
        "包装后只收文件对象，路径一律拒。",
        symbols={"load_workbook"},
        symbol_wrap=(("load_workbook", _bytes_only_workbook),),
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
# 导入面的根名清单
# ---------------------------------------------------------------------------
#
# 「模块能力清单」是按**全名**写的（``collections.abc`` 单独一条），
# 而 ``import`` 语句只认**根名**。所以运行期的导入闸门需要一张根名表 ——
# 它是能力清单的派生量，不是另一份清单。
#
# 放在这里而不是沙箱模块里：扫描器（判定「哪个属性名是逃逸模块」）
# 和运行期沙箱（判定「能不能 import」）都要用它，两边必须是同一张。
ALLOWED_IMPORT_ROOTS = frozenset(
    name.split(".")[0] for name in (*BACKEND_MODULES, *MIGRATION_MODULES)
)


# ---------------------------------------------------------------------------
# 导入面分两档：后端代码 / 插件迁移
# ---------------------------------------------------------------------------
#
# 静态扫描按**文件所在位置**分档（``migrations/`` 前缀才额外允许
# alembic / sqlalchemy），运行期必须判**同一件事**。
#
# 以前运行期一律按**并集**判，于是后端插件在运行期也能
# ``import sqlalchemy`` / ``import alembic`` —— 扫描期（按路径判）报的是
# PY_IMPORT，运行期却放行。同一个量有两个答案，正是 §4.5 说的
# 「同一个量只允许一个权威实现」。
#
# 分档差异是**实质**的：迁移要拿宿主连接做 DDL，能力面本来就更大；
# 后端代码不该有它。
#: 每一档的「模块 → 能力规则」映射。
#:
#: 扫描器按路径取这里的表，运行期按 flavor 取这里的表 ——
#: **同一张表**，两处读法，不允许各写一份。
RULES_BY_FLAVOR = {
    "backend": dict(BACKEND_MODULES),
    "migration": {**BACKEND_MODULES, **MIGRATION_MODULES},
}

#: 每一档允许 import 的**根名**集合（从上面的规则表派生，不另写一份）。
#:
#: 键是 flavor 名：``backend`` ↔ 不在 ``migrations/`` 下的插件文件，
#: ``migration`` ↔ 在 ``migrations/`` 下的插件文件。
IMPORT_FLAVORS = {
    flavor: frozenset(name.split(".")[0] for name in rules)
    for flavor, rules in RULES_BY_FLAVOR.items()
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

# ---------------------------------------------------------------------------
# 双下划线名字：默认**一律拒绝**（审查 S-01）
#
# **为什么是「一律」而不是逐个列举。** 逐个列举过一个名字，下一个就会
# 从别的缝里漏进来 —— 这次漏的是 ``__builtins__``：
#
#     ev = __builtins__["eval"]
#     ev("__import__('os').system('id')")
#
# 它是**裸 Name**（不是属性），所以「属性以 __ 开头就拒」那条规则看不见它；
# 而 ``DENIED_NAMES_ON_LOAD`` 是逐个名字写的，``__builtins__`` 不在里面。
# 扫出来是 CLEAN，运行时却在宿主进程里执行了任意命令。
#
# 同一族里还有一整串等价入口，每一个都能拿到内建能力：
#   ``__builtins__``  —— 内建命名空间（eval/exec/open/__import__）
#   ``__loader__``    —— 模块加载器，能再拿一次 ``__builtins__``
#   ``__spec__``      —— 同上（``.loader`` 属性本身不以 __ 开头，躲过了属性规则）
#   ``__dict__``      —— 模块全局字典，里面就有 ``__builtins__``
#   ``__globals__`` / ``__class__`` / ``__subclasses__`` / ``__reduce__`` …
#
# 所以规则是**按形态**而不是按名字：插件代码里任何以 ``__`` 开头的名字，
# 除下面这张极小的白名单外，全部拒绝。这样加新名字不需要改这份清单。
# ---------------------------------------------------------------------------
ALLOWED_DUNDER_NAMES = frozenset({
    # 模块被解释器自动注入的四个只读元信息，插件用它们做日志/自述是合理的，
    # 且都**不指向任何能力**（拿不到内建命名空间、拿不到加载器）。
    "__name__", "__file__", "__doc__", "__package__",
})


# ---------------------------------------------------------------------------
# 字符串常量：静态扫描唯一的形态盲区
# ---------------------------------------------------------------------------
# 上面两条规则都按**形态**判定（属性以 __ 开头、裸名字命中黑名单），
# 而字符串是形态的盲区 —— ``operator.attrgetter("__globals__")`` 里那个
# ``"__globals__"`` 看起来只是一段普通文本，扫描器看不出它将来会变成属性访问。
#
# 下面两条规则是**纵深防御**：运行期那层（:mod:`app.plugins.sandbox`）已经
# 让「字符串 → 属性访问」这条路结构上不存在，这里再挡一道，是为了让
# 「哪天有人把 getattr 放回内建白名单」这件事在**扫描期**就被看见。
#
# 判定刻意做得很窄，避免误伤正常代码：
#   1. **整个字符串**正好是一个 dunder 形状的名字 → 拒绝。
#      「正好是」而不是「包含」：中文文档里写「参见 obj.__init__」不会命中，
#      而一个只写着 "__class__" 的字符串常量，除了拿来反射没有别的用途。
#   2. **整个字符串**正好等于一个被禁的内建名 → 拒绝。
#      同样不做子串匹配（否则 "open" 这种普通词会到处误伤）。

#: 整个字符串正好是这些形状之一时拒绝（双下划线标识符）。
#:
#: 判定用「整个字符串相等」而不是「字符串里包含」：模块 docstring 里
#: 出现 ``__init__`` 这类词是正常叙述，子串匹配会把它也拦下来。
DENIED_STRING_DUNDER_NAMES = frozenset({
    "__builtins__", "__globals__", "__loader__", "__spec__", "__dict__",
    "__class__", "__bases__", "__base__", "__mro__", "__subclasses__",
    "__init__", "__reduce__", "__reduce_ex__", "__getattribute__",
    "__getattr__", "__setattr__", "__delattr__", "__code__", "__closure__",
    "__func__", "__self__", "__module__", "__qualname__", "__import__",
    "__build_class__", "__debug__", "__file__",
}) - ALLOWED_DUNDER_NAMES

#: 整个字符串正好等于这些名字时拒绝（拿内建能力本身的那几个）。
#:
#: 刻意**不含** ``open``：它太常作为普通词出现（"打开"、字典的键），
#: 而 open 这条能力已经被三处挡住 —— 内建白名单里没有它、
#: ``io`` 模块的 denied 里有它、``operator.attrgetter`` 也不给。
DENIED_STRING_CAPABILITY_NAMES = frozenset({
    "__import__", "eval", "exec", "compile",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
})


# ---------------------------------------------------------------------------
# 「双下划线形态」的判据：扫描器与运行期沙箱共用同一份
# ---------------------------------------------------------------------------
#
# 为什么要共用：安装期的静态扫描（:mod:`app.plugins.security`）和装载期的
# 运行期复核（:mod:`app.plugins.sandbox`）都在判「这段代码是不是在反射宿主」。
# 两边一旦分叉，就会出现两种坏结果：
#
#   · 扫描放行、运行期拒绝 —— 插件装得上却装不进（用户看到的是莫名失败）；
#   · 扫描拒绝、运行期放行 —— 运行期比扫描期松，那就是漏洞。
#
# 所以判据只能有一份，放在这里，两边都调它。
#
# 规则（审查 S-01 的口径，两边一致）：
#
# 1. **属性形态**：``x.__class__`` —— 属性名以 ``__`` 开头就违规，**没有例外**。
#    ``__loader__`` / ``__spec__`` / ``__dict__`` / ``__globals__`` 每一个都能
#    把真内建重新引回来。
# 2. **裸名字形态**：``__builtins__`` —— 只有 ``__name__`` / ``__file__`` /
#    ``__doc__`` / ``__package__`` 四个只读元信息例外。
#
# 「读取」才算违规：``__slots__ = ('x',)`` 这种赋值不给出任何能力，
# ``def __init__(self)`` 是函数名也不是属性访问 —— 两者都必须放行。
def reflection_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """找出源码里所有「双下划线形态」的违规点。

    返回 ``[(行号, 形态), ...]``；形态取 ``"attribute"``（属性形态）
    或 ``"name"``（裸名字形态）。

    已经命中 :data:`DENIED_NAMES_ON_LOAD` 的名字（``__import__``）不在这里报 ——
    它们由扫描器按 ``PY_CALL`` 报（那一档是「读取即拒绝」），
    运行期也拿不到（内建白名单里没有；``__import__`` 那个位置放的是受控导入器）。
    两边报同一件事会让错误信息变得没有指向。
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                violations.append((node.lineno, "attribute"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id.startswith("__") and node.id not in ALLOWED_DUNDER_NAMES:
                if node.id in DENIED_NAMES_ON_LOAD:
                    continue
                violations.append((node.lineno, "name"))
    return violations


# ---------------------------------------------------------------------------
# 「模块值属性」的判据
# ---------------------------------------------------------------------------
#
# 标准库很多模块把**别的模块**当普通属性挂着：``dataclasses.sys`` /
# ``contextlib.os`` / ``uuid.os`` / ``json.codecs`` / ``typing.types`` /
# ``dataclasses.builtins`` ……
#
# 这些属性名本身既不是双下划线、也不在黑名单里，所以静态扫描**看不见**它们；
# 但拿到 ``dataclasses.sys`` 就等于拿到 ``sys.modules``，于是
# ``dataclasses.sys.modules['os'].getpid()`` 一路通到宿主。
# 审计 §3.1 实测过这条链：扫描判 CLEAN，运行期拿到真实 pid。
#
# 判据用**解释器自己给的标准库模块名单**（``sys.stdlib_module_names``，
# Python 3.10+ 自带），而不是手写黑名单 —— 手写黑名单是无穷枚举
# （本模块开头已经记过 18 个漏网的等价入口）。
#: 标准库顶层模块名（解释器自带，不手写）。
_STDLIB_MODULE_NAMES = frozenset(sys.stdlib_module_names)


def is_escape_module_name(name: str) -> bool:
    """属性名是不是「不在能力清单里的标准库模块」。

    能力清单里的根名直接放行：拿到一个**本来就能 import** 的模块不构成越权
    （``csv.re`` / ``collections.abc`` 都在清单里）。
    """
    if name not in _STDLIB_MODULE_NAMES:
        return False
    return name.split(".")[0] not in ALLOWED_IMPORT_ROOTS

