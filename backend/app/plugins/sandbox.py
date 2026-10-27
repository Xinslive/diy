"""插件后端代码的**运行期能力裁剪** —— 静态扫描之外的第二层。

静态扫描是第一层，这一层是第二层。即便扫描规则被绕过（字符串拼接、
``.txt`` 载荷等），插件拿到的仍然是**裁剪过的**内建能力与**受控的**导入面。

为什么必须做这一层
------------------
静态扫描按**形态**判定危险（属性以 ``__`` 开头、裸名字命中黑名单），
它看不见的是**字符串**。于是 ``operator.attrgetter("__globals__")``
这种「把字符串变成属性访问」的写法可以完全隐身 —— 扫描器判 CLEAN，
运行期却把整份 ``__builtins__`` 拿回了手（审计 §3.1 实测过这条链）。

这两层缺一不可：

- 只有扫描：字符串绕过（``attrgetter``、``.txt`` 载荷）能拿到宿主能力；
- 只有运行期裁剪：扫描规则永远在追「等价入口」的黑名单（已核实 18 个以上）。

本模块的不变量（也是它的验收标准）
--------------------------------
1. 插件拿不到 ``__import__`` / ``exec`` / ``eval`` / ``compile`` / ``open``；
2. 插件拿不到 ``getattr`` / ``setattr`` / ``delattr`` / ``globals`` /
   ``locals`` / ``vars`` —— 于是**「字符串 → 属性访问」这条路整个消失**；
3. 插件只能 import 能力清单里的模块，且拿到的是**裁剪过**的模块对象；
4. 插件读不到宿主文件系统（``io`` 只剩内存缓冲）；
5. **插件代码里不出现双下划线形态**（属性与裸名字）—— 装载期就拒，
   不是等到运行期才炸；
6. **模块代理不把真模块放在任何猜得到的名字下**（实例字典里没有真对象）。

第 2 条是整套东西的支点。属性访问只有两种形态：

- **字面量形态**（``x.__class__``）—— 静态扫描按形态判定，且**无法用字符串
  绕过**（没有字符串就没有别的形态）；
- **运行期形态**（``getattr(x, "__class__")``、``operator.attrgetter(...)``）——
  被第 2 条堵死。

于是「双下划线一律拒绝」这条规则第一次真正成立：它不再依赖扫描器
「恰好没漏掉某种写法」，而是**结构上没有第二条路**。

第 5 条把这条规则从「扫描期」延伸到「装载期」：即便有插件是**在规则加进来
之前**装上的、或者扫描器被绕过，装载时也会被拒（失败即闭）。
判据与扫描器共用同一份（:func:`app.plugins.scan_policy.reflection_violations`），
不另写一份 —— 两份判据必然分叉，分叉出来的那个方向就是漏洞。

第 6 条是被实测打回来的：模块代理原先把真模块存在实例字典的 ``_real`` 下，
于是 ``dataclasses._real.sys.modules['os'].getpid()`` 一路通到宿主 ——
而 ``_real`` 既不是双下划线、也不是模块名，静态扫描完全看不见它。
现在真对象只存在于**闭包**里，实例字典是空的。

诚实边界
--------
进程内的 Python 沙箱**不是**安全边界：它是纵深防御的一层，不是唯一一层。
审计 §3.1 的首选方案仍是「插件后端代码搬到独立进程/容器执行，只经 IPC 暴露
受限 API」。本模块把已知的逃逸链（``attrgetter`` 取 ``__globals__``、
``io.FileIO`` 读盘、``__import__("os")``、``.txt`` 载荷、
``dataclasses.sys.modules['os']``、``tuple.__base__.__subclasses__()``）
逐条封死，但只要插件与宿主同处一个解释器，就永远存在尚未想到的等价入口。
要把它当成**边界**来依赖，需要先做进程隔离。
"""
from __future__ import annotations

import ast
import builtins
import types
from contextlib import contextmanager
from pathlib import Path

from app.plugins.errors import PluginError

from app.plugins.scan_policy import (
    ALLOWED_IMPORT_ROOTS,
    BACKEND_MODULES,
    DENIED_NAMES_ON_CALL,
    DENIED_NAMES_ON_LOAD,
    IMPORT_FLAVORS,
    MIGRATION_MODULES,
    ModuleRule,
    reflection_violations,
)

# ---------------------------------------------------------------------------
# 一、内建能力：白名单
# ---------------------------------------------------------------------------
# 为什么用**白名单**而不是黑名单：黑名单是无穷枚举（见 scan_policy 的模块说明）。
# 内建能力就那么几十个，白名单能一次说清「插件能拿到什么」，而黑名单永远在
# 追「下一个等价入口」。
#
# 刻意**不给**的内建能力：
#   __import__ / exec / eval / compile / open —— 拿到任意代码或任意文件；
#   getattr / setattr / delattr —— 「字符串 → 属性访问」的三个入口；
#   globals / locals / vars —— 拿到模块命名空间，进而拿到 __builtins__；
#   input / breakpoint —— 读 stdin 卡住 worker，或在生产机留个断点。
#
# 这些名字与扫描器已有的黑名单（DENIED_NAMES_ON_LOAD / DENIED_NAMES_ON_CALL）
# **完全一致**：扫描器本来就不许插件写这些名字，运行期再拿掉它们，
# 两层说的是同一件事，不会出现「扫描放行但运行期没有」的错位。
ALLOWED_BUILTINS = frozenset({
    # —— 纯计算与容器 ——
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable",
    "chr", "complex", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "hash", "hex", "int", "isinstance", "issubclass", "iter", "len",
    "list", "map", "max", "min", "next", "oct", "ord", "pow", "print", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "type", "zip",
    # —— 异常：插件要抛普通错误，也要能被宿主捕获成 4xx/5xx ——
    #
    # 这里列的是**全部** Exception 子类（64 个），不是挑拣过的一小撮。
    #
    # 挑拣过的版本漏了 ``UnicodeError``，而真实插件的 csv 解析就是
    # ``except (UnicodeError, csv.Error)`` —— 少一个名字，插件在运行期
    # 直接 NameError，扫描器却是 CLEAN（静态扫描看不见内建名）。
    #
    # 异常类本身不带能力：能 ``raise`` 一个异常不等于能拿到宿主的资源。
    # 真正的风险在 **BaseException 里的非 Exception 三个**：
    # ``SystemExit`` 能终止宿主进程、``KeyboardInterrupt`` 能吃掉 Ctrl-C、
    # ``GeneratorExit`` 能搅乱协程 —— 这三个**故意不给**。
    "ArithmeticError", "AssertionError", "AttributeError", "BlockingIOError",
    "BrokenPipeError", "BufferError", "BytesWarning", "ChildProcessError",
    "ConnectionAbortedError", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "DeprecationWarning", "EOFError",
    "EncodingWarning", "EnvironmentError", "Exception", "ExceptionGroup",
    "FileExistsError", "FileNotFoundError", "FloatingPointError",
    "FutureWarning", "IOError", "ImportError", "ImportWarning",
    "IndentationError", "IndexError", "InterruptedError", "IsADirectoryError",
    "KeyError", "LookupError", "MemoryError", "ModuleNotFoundError",
    "NameError", "NotADirectoryError", "NotImplementedError", "OSError",
    "OverflowError", "PendingDeprecationWarning", "PermissionError",
    "ProcessLookupError", "RecursionError", "ReferenceError",
    "ResourceWarning", "RuntimeError", "RuntimeWarning", "StopAsyncIteration",
    "StopIteration", "SyntaxError", "SyntaxWarning", "SystemError",
    "TabError", "TimeoutError", "TypeError", "UnboundLocalError",
    "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError",
    "UnicodeTranslateError", "UnicodeWarning", "UserWarning", "ValueError",
    "Warning", "ZeroDivisionError",
    # —— 类与继承：``class Foo:`` 需要 __build_class__，少一个就语法层面崩 ——
    "__build_class__", "classmethod", "object", "property", "staticmethod", "super",
    "NotImplemented", "Ellipsis",
    # —— ``BaseException`` 本身：插件要能 ``except BaseException`` 兜住一切。
    # 它的三个「非 Exception」子类（SystemExit / KeyboardInterrupt /
    # GeneratorExit）**故意不在这里** —— 见上面的说明。
    "BaseException",
})


def _build_restricted_builtins(allowed_roots) -> dict:
    """按白名单裁出一份内建命名空间。

    注意 ``__build_class__`` 必须留着：``class X:`` 编译出来是
    ``LOAD_BUILD_CLASS``，它到 globals / builtins 里找这个名字 ——
    拿掉它等于禁止插件定义类，而插件的业务代码里类很常见。

    ``allowed_roots`` 是**这一档**允许 import 的根名集合
    （见 :data:`app.plugins.scan_policy.IMPORT_FLAVORS`）。
    """
    restricted = {name: getattr(builtins, name) for name in ALLOWED_BUILTINS if hasattr(builtins, name)}
    # 导入语句（``import x`` / ``from x import y``）编译成 ``IMPORT_NAME``，
    # 它找的是 builtins 里的 ``__import__``。这里换成本模块的**受控**版本：
    # 不在能力清单里的模块一律 ImportError。
    #
    # 放进去的**不是** ``guarded_import`` 这个函数本身，而是一个包装对象 ——
    # 函数带 ``__globals__``，那个字典里放着真正的 ``builtins`` 模块
    # （见 :func:`_make_guarded_importer` 的说明）。
    #
    # 导入器是**按档**造的：后端代码那一档不放行 alembic / sqlalchemy，
    # 迁移那一档才放行（与扫描器按路径分档逐档对应）。
    restricted["__import__"] = _make_guarded_importer(allowed_roots)
    return restricted


#: 运行期拿不到的内建能力名（= 扫描器的两张黑名单 + 导入器本身）。
#:
#: 这份清单和 :mod:`app.plugins.scan_policy` 里的黑名单**必须是同一件事**：
#: 扫描器本来就不许插件写这些名字，运行期再拿掉它们，两层说的是同一句话。
#: 两边一旦分叉，就会出现「扫描放行但运行期没有」（插件在运行期报错）
#: 或者「运行期还在但扫描器看不见」（后者是漏洞）。所以在这里断言一次。
_DENIED_BUILTIN_NAMES = frozenset({
    *DENIED_NAMES_ON_LOAD,
    *DENIED_NAMES_ON_CALL,
    "__import__",
    "__loader__",
    "__spec__",
    "__dict__",
})
_overlap = _DENIED_BUILTIN_NAMES & ALLOWED_BUILTINS
if _overlap:
    # 运行期拿掉了、扫描器却放行的名字：插件写出来会直接 NameError。
    raise RuntimeError(f"插件内建白名单与黑名单冲突：{sorted(_overlap)}")


# ---------------------------------------------------------------------------
# 二、导入面：受控的 __import__
# ---------------------------------------------------------------------------
# 导入面**按插件代码的种类分两档**，和静态扫描器的分档**逐档对应**：
#
#   · ``backend``   —— 插件后端代码（``backend/main.py``）
#   · ``migration`` —— 插件迁移（``migrations/versions/*.py``）
#
# 分档不是形式上的：迁移要拿宿主连接做 DDL，能力面本来就比后端代码大
# （alembic / sqlalchemy 只在迁移里给）。以前运行期一律按**并集**判，
# 于是后端插件在运行期也能 ``import sqlalchemy`` / ``import alembic`` ——
# 扫描期（按路径判）拒的是 PY_IMPORT，运行期却放行。
#
# **同一个量有两个答案，正是 §4.5 说的「同一个量只允许一个权威实现」。**
# 所以两档都定义在 :mod:`app.plugins.scan_policy`（那里也是扫描器按路径
# 分档的地方），运行期与扫描期读**同一张表**。
#
# 根名表 :data:`ALLOWED_IMPORT_ROOTS` 同样定义在那里
# （扫描器用它判断「某个属性名是不是逃逸模块」）—— 同一张表，两个用途，
# **不允许各写一份**。

#: 模块名 → 能力规则。
_MODULE_RULES: dict[str, ModuleRule] = {}
for _name, _rule in {**BACKEND_MODULES, **MIGRATION_MODULES}.items():
    _MODULE_RULES.setdefault(_name, _rule)


def _rule_for(module_name: str) -> ModuleRule | None:
    """按模块名找能力规则。

    先按全名找（``collections.abc`` 这种子模块有独立规则），
    找不到再按根名找（``openpyxl.load_workbook`` 归 ``openpyxl`` 管）。
    """
    rule = _MODULE_RULES.get(module_name)
    if rule is not None:
        return rule
    root = module_name.split(".")[0]
    return _MODULE_RULES.get(root)


# ---------------------------------------------------------------------------
# 三、模块代理：裁掉被禁用的属性
# ---------------------------------------------------------------------------
# 代理**不复制**原模块的 __dict__，而是把查找转发到真模块（``__getattr__``）——
# 这样 import 机制往真包上挂的子模块（``collections.abc``）照样看得见，
# 而被禁用的属性在这一层就被挡住。
#
# 被挡住的两类：
#   denied        —— 能力清单里写明的禁用属性名（``io.open``、``io.FileIO`` …）；
#   symbol_denied —— 某个符号**自身**带危险方法（``alembic.op.execute``）。

#: 代理仍然放行的元信息名字（都是只读元信息，不指向任何能力）。
_METADATA_NAMES = frozenset({"__name__", "__file__", "__doc__", "__package__"})


#: 运行期**故意**放行的符号属性：``(符号名, 属性名)``。
#:
#: ``alembic.op.execute`` 是唯一一条，理由分两层，缺一不可：
#:
#:  · **静态扫描在安装期就拒它。** ``scan_policy`` 里 alembic 规则的
#:    ``symbol_denied`` 含 ``("op", {"execute", "get_bind", "get_context"})``，
#:    而 ``security.py`` 第二遍按 ``rule.denies(symbol, attr)`` 判定 ——
#:    插件包里只要出现 ``op.execute``，**装都装不上**。
#:    能装上的插件里根本不会有这段代码。
#:  · **运行期放行，是为了让范围护栏有机会说话。** 插件迁移执行时，
#:    宿主会拿迁移前后的 schema 做对比，越界改动被判「迁移修改了宿主结构」
#:    并整体回滚（``app/plugins/migrations.py`` 的范围检查）。
#:    运行期直接抛 ``AttributeError`` 的话，那道护栏根本没机会执行 ——
#:    而仓库里 ``test_plugin_migration.py`` 与 ``test_plugin_guardrails.py``
#:    正是**靠它**来验「越界 DDL 被拦下」和「触发器真的建到库里」。
#:
#: 换句话说：这里放开的是**已经装好的插件**的运行期路径，
#: 装的时候那道门还是关着的。两道门各管一段，缺一道就出现「要么装不上、
#: 要么拦不住」。
#:
#: ``op.get_bind`` / ``op.get_context`` **仍然拒**：那两个交出的是宿主连接
#: 与迁移上下文本身，比一条 SQL 危险得多 —— 拿到连接就能对**任意**表下手，
#: 而 ``op.execute`` 至少还落在宿主自己的迁移事务与范围检查里。
RUNTIME_ALLOWED_SYMBOL_ATTRIBUTES = frozenset({("op", "execute")})


class _SandboxModuleBase(types.ModuleType):
    """模块代理的基类。

    **它不持有任何真实对象。** 这一条是硬要求，不是风格问题：

    代理原先把真模块存在实例字典的 ``_real`` 下，于是插件一句
    ``dataclasses._real.sys.modules['os'].getpid()`` 就把真模块拿回去了 ——
    ``_real`` 既不是双下划线、也不是模块名，静态扫描完全看不见它，
    运行期也没有对应的闸门（审计 §3.1 实测：这条链在扫描期是 CLEAN）。

    现在真模块只存在于**闭包**里：实例字典里只有解释器自己写的
    ``__name__`` / ``__doc__`` / ``__package__`` / ``__loader__`` / ``__spec__``，
    没有任何一个指向宿主能力。
    """

    __slots__ = ()


def _make_symbol_guard(target, denied, symbol):
    """造一个「只挡住指定属性名」的对象包装。

    用于 ``symbol_denied``（例如 ``alembic.op`` 上的 ``execute``）。
    被挡的属性访问直接抛 ``AttributeError`` —— 和「那个属性不存在」同义，
    调用方拿到的错误信息也就不会泄露「这里曾经有一道闸」。

    ``symbol`` 是这个对象在能力清单里的**符号名**（例如 ``"op"``），
    用来查 :data:`RUNTIME_ALLOWED_SYMBOL_ATTRIBUTES`。

    真对象 ``target`` 由**闭包**持有，不放在实例字典里 ——
    理由同 :class:`_SandboxModuleBase`：``guard._target`` 这种猜得到的
    普通名字会把真对象原样交出去（实测
    ``op._target.execute`` 能直接拿到 alembic 的任意 SQL 执行入口）。
    """

    class _Guard:
        __slots__ = ()

        def __getattr__(self, name):
            if name.startswith("__") and name not in _METADATA_NAMES:
                # 双下划线一律不转发：``__class__`` / ``__globals__`` /
                # ``__dict__`` 都能把真对象或真内建重新引回来。
                raise AttributeError(name)
            if name in denied:
                if symbol is None or (symbol, name) not in RUNTIME_ALLOWED_SYMBOL_ATTRIBUTES:
                    raise AttributeError(f"{name} 对插件不可用")
            return getattr(target, name)

        def __setattr__(self, name, value):
            raise AttributeError("插件不能改写宿主提供的对象")

        def __delattr__(self, name):
            raise AttributeError("插件不能改写宿主提供的对象")

        def __repr__(self):
            # 模块默认的 repr 会带上宿主文件系统路径（``<module 'csv' from
            # '/opt/homebrew/.../csv.py'>``）。插件 print 一个模块就把它拿到手了 ——
            # 那不是能力，但属于「宿主内部结构」不该外泄的东西，所以这里收窄。
            return f"<插件模块 {real.__name__}>"

    return _Guard()


def _make_sandbox_module(real, rule: ModuleRule | None, allowed_roots):
    """造一个能力清单约束下的模块视图。

    代理**不复制**原模块的 ``__dict__``，而是把查找转发到真模块
    （``__getattr__``）—— 这样 import 机制往真包上挂的子模块
    （``collections.abc``）照样看得见，而被禁用的属性在这一层就被挡住。

    真模块由**闭包**持有（理由见 :class:`_SandboxModuleBase`）。

    ``allowed_roots`` 是**这一档**允许的模块根名
    （见 :data:`app.plugins.scan_policy.IMPORT_FLAVORS`）——
    模块值属性也按它判，否则后端插件能通过「某个模块的子模块属性」
    摸到迁移那一档才有的模块。

    被挡住的几类：
      denied        —— 能力清单里写明的禁用属性名（``io.open``、``io.FileIO`` …）；
      symbol_denied —— 某个符号**自身**带危险方法（``alembic.op.execute``）；
      模块值属性    —— 见 :func:`_sanitize_module_attribute`。
    """
    denied = frozenset(rule.denied) if rule else frozenset()
    allowed = None if rule is None or rule.symbols is None else frozenset(rule.symbols)
    symbol_denied = dict(rule.symbol_denied) if rule else {}
    symbol_wrap = dict(rule.symbol_wrap) if rule else {}

    class _Proxy(_SandboxModuleBase):
        __slots__ = ()

        def __getattr__(self, name):
            # 只有「属性确实不在本对象上」时才会走到这里。
            if name.startswith("__") and name not in _METADATA_NAMES:
                # 双下划线一律不转发：``__loader__`` / ``__spec__`` / ``__dict__``
                # 都能把真模块或真内建重新引回来。
                raise AttributeError(name)
            if name in denied:
                raise AttributeError(f"{name} 对插件不可用")
            if allowed is not None and name not in allowed:
                raise AttributeError(f"{name} 对插件不可用")
            value = getattr(real, name)
            symbol_rules = symbol_denied.get(name)
            if symbol_rules:
                return _make_symbol_guard(value, symbol_rules, symbol=name)
            wrapper = symbol_wrap.get(name)
            if wrapper is not None:
                # 该符号要换成**更窄**的实现（能力清单里的 symbol_wrap）。
                # 收窄只能更严，不能更松 —— 所以这里不校验包装结果，
                # 但也不允许包装函数把原符号的「禁用属性」放回来：
                # 包装只作用在这一个符号上，其它的禁用规则照常生效。
                return wrapper(value)
            if isinstance(value, types.ModuleType):
                return _sanitize_module_attribute(value, name, allowed_roots)
            return value

        def __setattr__(self, name, value):
            # 插件改自己的模块变量是正常的；但不能通过代理往真模块上写。
            raise AttributeError("插件不能改写宿主提供的模块")

        def __delattr__(self, name):
            raise AttributeError("插件不能改写宿主提供的模块")

        def __repr__(self):
            # 同 :func:`_make_symbol_guard`：模块默认的 repr 会带出宿主路径。
            return f"<插件模块 {real.__name__}>"

    return _Proxy(real.__name__)


def _sanitize_module_attribute(value, name, allowed_roots):
    """处理「属性值是一个模块」的情况。

    标准库很多模块把**别的模块**当普通属性挂着：``dataclasses.sys`` /
    ``contextlib.os`` / ``uuid.os`` / ``json.codecs`` / ``typing.types`` /
    ``dataclasses.builtins`` …… 这些属性名既不是双下划线、也不在黑名单里，
    静态扫描**看不见**它们；但拿到 ``dataclasses.sys`` 就等于拿到
    ``sys.modules``，于是 ``dataclasses.sys.modules['os'].getpid()``
    一路通到宿主（审计 §3.1 实测：这条链扫描期判 CLEAN）。

    两条规则：
      1. 根名不在**这一档**的导入白名单里的模块 —— **直接拒绝**（AttributeError）；
      2. 在白名单里的模块 —— **再包一层代理**（不是原样返回）。

    第 2 条同样重要：原样返回就等于把真模块交出去 ——
    即便这一次它是白名单里的模块，下一层属性又可能指向不在白名单里的模块。
    """
    module_name = getattr(value, "__name__", "") or ""
    root = module_name.split(".")[0]
    if root not in allowed_roots:
        raise AttributeError(f"{name} 对插件不可用")
    rule = _rule_for(module_name)
    if rule is None:
        raise AttributeError(f"{name} 对插件不可用")
    return _make_sandbox_module(value, rule, allowed_roots)


def _sanitize(module, allowed_roots) -> object:
    """把真模块换成能力清单约束下的代理。

    不在能力清单里的模块**不再原样返回**：原样返回等于把真模块交出去，
    而「不在清单里」这件事本身就已经越权了。失败即闭（抛 ImportError）。
    """
    if not isinstance(module, types.ModuleType):
        return module
    rule = _rule_for(module.__name__)
    if rule is None:
        raise ImportError(f"插件不允许使用模块：{module.__name__}")
    return _make_sandbox_module(module, rule, allowed_roots)


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0, allowed_roots=None):
    """受控的 ``__import__``：只放行**这一档**能力清单里的模块。

    三道闸：
    1. 相对导入（``level != 0``）一律拒绝 —— 插件包内没有可导入的兄弟模块；
    2. 根名不在这一档的白名单里 → ``ImportError``（``os`` / ``sys`` /
       ``subprocess`` / ``app.core.db`` 全部在这一档）；
    3. 拿到的模块对象换成**裁剪过的代理**（见 :func:`_sanitize`）。

    ``allowed_roots`` 缺省是**并集**（两种 flavor 的合集）；
    实际给插件用的导入器是**按档**造的（见 :func:`_make_guarded_importer`）。
    """
    if level:
        raise ImportError(f"插件不支持相对导入：{name}")
    if not name:
        raise ImportError("插件导入的模块名为空")
    roots = ALLOWED_IMPORT_ROOTS if allowed_roots is None else allowed_roots
    root = name.split(".")[0]
    if root not in roots:
        raise ImportError(f"插件不允许导入模块：{name}")
    module = builtins.__import__(name, globals, locals, fromlist, level)
    return _sanitize(module, roots)


def _make_guarded_importer(allowed_roots):
    """把受控导入器包成一个**身上没有 ``__globals__`` 的可调用对象**。

    ``guarded_import`` 是个函数，而函数带 ``__globals__`` —— 那个字典里放着
    真正的 ``builtins`` 模块。插件只要拿到这个导入器，就能顺着
    ``__globals__['builtins']`` 回到解释器原版内建（审计 §3.1 实测过这条链）。

    现在放进内建白名单的是这个包装对象：它**没有** ``__globals__``，
    拿到的唯一路径是去问它的 ``__globals__`` 属性 —— 而那是双下划线属性，
    装载期就被拒了（见 :func:`_refuse_reflection`）。

    真导入器同样由闭包持有，不放在实例字典里。

    ``allowed_roots`` 是**这一档**允许 import 的根名 —— 导入器是**按档**造的，
    所以后端代码那一档拿不到 alembic / sqlalchemy（与扫描器按路径分档一致）。
    """

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        return guarded_import(name, globals, locals, fromlist, level, allowed_roots)

    class _Importer:
        __slots__ = ()

        def __call__(self, name, globals=None, locals=None, fromlist=(), level=0):
            return _import(name, globals, locals, fromlist, level)

        def __repr__(self):
            return "<插件受控导入器>"

    return _Importer()


#: 插件**后端代码**用的内建命名空间（导入面 = ``backend`` 那一档）。
RESTRICTED_BUILTINS: dict = _build_restricted_builtins(IMPORT_FLAVORS["backend"])

#: 插件**迁移**用的内建命名空间（导入面 = ``migration`` 那一档）。
#:
#: 迁移由宿主 alembic 以**宿主连接**执行，能力面本来就比后端代码大
#: （alembic / sqlalchemy 只在迁移里给）—— 这与静态扫描器按路径分档一致。
MIGRATION_BUILTINS: dict = _build_restricted_builtins(IMPORT_FLAVORS["migration"])


# ---------------------------------------------------------------------------
# 四、执行
# ---------------------------------------------------------------------------
def _refuse_reflection(target: Path, source: str) -> None:
    """装载期复核「双下划线形态」，命中即拒（失败即闭）。

    为什么**装载期**还要再判一遍：静态扫描是**安装期**的一次性检查，
    而插件包在装上之后仍然能被替换/篡改（``verify_directory`` 在加载期
    会重新验签，但验签管的是**完整性**，不是**形态**）。更重要的是 ——
    规则本身是**后来**才加的：在这条规则存在之前装上的插件，
    代码里可能就有 ``x.__class__`` 这类反射。

    运行期必须自己站住脚，不能靠「当初装它的时候有人看过一眼」。

    判据与静态扫描共用同一份（:func:`app.plugins.scan_policy.reflection_violations`），
    所以不会出现「扫描判 CLEAN、装载被拒」这种口径错位。

    这里**不做**运行期的属性拦截（改写 AST 把违规属性换成抛错的表达式）——
    那样插件可以在运行期把那个辅助名字重新绑定掉，闸门就形同虚设。
    直接在装载期**拒绝整个模块**，是唯一没有这种问题的做法。
    """
    try:
        tree = ast.parse(source, filename=str(target))
    except SyntaxError as exc:
        raise PluginError("PLUGIN_SYNTAX_ERROR", "插件代码语法错误", status=400) from exc
    violations = reflection_violations(tree)
    if not violations:
        return
    findings = [
        {"file": str(target), "line": line, "rule": "PY_REFLECTION"}
        for line, _kind in violations
    ]
    raise PluginError(
        "PLUGIN_REFLECTION_FORBIDDEN",
        f"插件代码使用了被禁用的双下划线反射（第 {violations[0][0]} 行）",
        status=400,
        findings=findings,
    )


def exec_plugin_module(name: str, path: Path, *, package: str = "", flavor: str = "backend"):
    """在**裁剪过的**能力面里执行一个插件 Python 文件。

    刻意**不用** ``importlib`` 的 ``spec.loader.exec_module``：那条路会用
    解释器的真内建（``__import__`` / ``exec`` / ``open`` 全在），
    等于把静态扫描当成了唯一防线。这里手工构造模块并把
    ``__builtins__`` 指向裁剪后的命名空间。

    ``__loader__`` / ``__spec__`` 刻意**不设**：它们能把真模块的
    ``__loader__`` 引回来，进而再拿一次真内建。

    **只接受 ``.py`` 源文件**（见 :func:`sandboxed_module_loader` 的说明）：
    ``.pyc`` 是字节码，静态扫描一道都没经过它。

    装载前先做**双下划线形态复核**（见 :func:`_refuse_reflection`）。

    ``flavor`` 决定**导入面**（``backend`` / ``migration``），
    与静态扫描器按文件位置分档一一对应（见
    :data:`app.plugins.scan_policy.IMPORT_FLAVORS`）。调用方**知道**自己
    加载的是哪一类文件，所以由它显式声明，不靠路径猜。
    """
    target = Path(path)
    if target.suffix != ".py":
        raise PluginError(
            "PLUGIN_SOURCE_TYPE",
            f"插件只执行 .py 源文件，不执行 {target.suffix or '无后缀'} 文件",
            status=400,
        )
    if flavor not in IMPORT_FLAVORS:
        raise PluginError(
            "PLUGIN_INVALID_FLAVOR",
            f"未知的插件代码类别：{flavor}",
            status=500,
        )
    source = target.read_text(encoding="utf-8")
    _refuse_reflection(target, source)
    code = compile(source, str(target), "exec")
    module = types.ModuleType(name)
    module.__dict__["__builtins__"] = (
        MIGRATION_BUILTINS if flavor == "migration" else RESTRICTED_BUILTINS
    )
    module.__dict__["__file__"] = str(target)
    module.__dict__["__name__"] = name
    module.__dict__["__package__"] = package
    module.__dict__["__doc__"] = None
    exec(code, module.__dict__)
    return module


@contextmanager
def sandboxed_module_loader():
    """让 alembic 的脚本加载器也走**裁剪后的**能力面。

    插件迁移由宿主 alembic 执行（``EnvironmentContext`` + 宿主连接），
    而 alembic 自己的 ``load_module_py`` 用的是**解释器真内建** ——
    于是迁移文件是同一个逃逸链的另一个入口：静态扫描同样看不见
    ``operator.attrgetter("__globals__")``。

    这里在**插件迁移执行的那一小段时间里**把 alembic 的加载器换成裁剪版，
    出了这段就还原。改的是 ``alembic.util.pyfiles.load_module_py``
    （alembic 1.20 的 ``load_python_file`` 在函数体里按模块全局查它，
    所以替换这一处就够）。

    **只执行 ``.py`` 源文件**（审查 §7 第 6 步第 49 项：
    「禁止包内非 ``.py`` 文件参与 exec」）。

    为什么这条要写成代码而不是靠 alembic 的默认行为：alembic 的
    ``ScriptDirectory(sourceless=False)``（默认）确实只加载 ``.py``，
    但那是**别人的默认值** —— 哪天有人为了支持「无源码包」而打开
    ``sourceless=True``，``.pyc`` 就会直接被执行。

    而 ``.pyc`` 是**字节码**：静态扫描器扫的是源码
    （``scan_file`` 只认 ``TEXT_TYPES``，``.pyc`` 落到 ``RESOURCE_TYPE``
    而被拒），所以字节码里的东西**一道扫描都没经过** ——
    它是整条防线里唯一完全裸奔的一段。

    所以这里显式地只放行 ``.py``：``.pyc`` / ``.pyo`` / 别的后缀，
    一律拒绝并说清楚原因。
    """
    from alembic.util import pyfiles

    original = pyfiles.load_module_py

    def guarded(module_id: str, path):
        # 迁移文件走**迁移那一档**的导入面：alembic / sqlalchemy 只在迁移里给。
        return exec_plugin_module(module_id, Path(path), flavor="migration")

    pyfiles.load_module_py = guarded
    try:
        yield
    finally:
        pyfiles.load_module_py = original
