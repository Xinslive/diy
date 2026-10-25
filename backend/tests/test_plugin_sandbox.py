"""插件的**运行期**能力裁剪：静态扫描之外的那一层。

对应审计 §3.1（插件静态扫描可被绕过 → 宿主进程内任意代码执行 / 任意文件读写 /
直接提权）。

**这份测试盯的是「运行期」，不是「扫描期」**
--------------------------------------------
扫描期已经有 :mod:`tests.test_plugin_security_scan` 守着。审计 §3.1 实测的那条
逃逸链（``operator.attrgetter("__globals__")`` → 整份 ``__builtins__``）在
扫描期是 **CLEAN** —— 扫描器按形态判定，而字符串是形态的盲区。

所以这里钉死的是**另一条不变量**：即便有代码绕过了扫描器，插件在运行期也拿不到
宿主能力。判据分四档：

1. 插件拿不到 ``exec`` / ``eval`` / ``compile`` / ``open``；
2. 插件拿不到 ``getattr`` / ``setattr`` / ``globals`` / ``locals`` / ``vars``
   —— 于是「字符串 → 属性访问」这条路结构上不存在；
3. 插件只能 import 能力清单里的模块，且拿到的是**裁剪过**的模块对象；
4. 插件读不到宿主文件系统（``io`` 只剩内存缓冲）。

**只要有人把上面任何一条放回去，这里立刻红。**

写法说明：被裁掉的属性是**访问即抛**（AttributeError / NameError），而不是
「拿到 None」。所以插件侧的探针要用 try/except 逐个包，不能靠 getattr ——
getattr 本身也被裁掉了（这正是第 2 条）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.plugins.sandbox import (
    ALLOWED_BUILTINS,
    ALLOWED_IMPORT_ROOTS,
    RESTRICTED_BUILTINS,
    exec_plugin_module,
    sandboxed_module_loader,
)
from app.plugins.scan_policy import BACKEND_MODULES
from app.plugins.security import scan_file

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins"


def rules_of(path, source, permissions=None):
    return {finding["rule"] for finding in scan_file(path, source.encode(), permissions=permissions)}


def load(tmp_path: Path, source: str, name: str = "evil"):
    """把一段插件代码放进沙箱里执行，返回模块。"""
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return exec_plugin_module(name, path)


# ---------------------------------------------------------------------------
# 一、内建能力：插件拿不到的那些
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "exec", "eval", "compile", "open",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "input", "breakpoint",
])
def test_插件拿不到危险内建(name):
    """能力名单里的名字**必须**不在运行期内建里。

    这一条是整套东西的支点：属性访问要么是字面量形态（扫描期按形态判定，
    且无法用字符串绕过），要么是运行期形态（getattr / attrgetter）——
    后者被这一条堵死。
    """
    assert name not in RESTRICTED_BUILTINS, f"{name} 还在插件的内建能力里"
    assert name not in ALLOWED_BUILTINS


def test_导入器是受控版本而不是解释器原版():
    """``__import__`` 必须在，但必须是**受控**的那一份。

    导入语句（``import x``）编译成 ``IMPORT_NAME``，它找的是内建里的
    ``__import__``。拿掉它等于禁止插件 import 任何东西（包括能力清单里的
    decimal / datetime / fastapi）；所以换成一个**按能力清单放行**的版本。

    而且放进去的**不是** ``guarded_import`` 这个函数本身 —— 函数带
    ``__globals__``，那个字典里放着真正的 ``builtins`` 模块，
    插件顺着 ``__globals__['builtins']`` 就能回到解释器原版内建
    （审计 §3.1 实测过这条链）。所以这里断言的是「它身上没有 ``__globals__``」。
    """
    import builtins

    from app.plugins.sandbox import guarded_import

    importer = RESTRICTED_BUILTINS["__import__"]
    assert importer is not builtins.__import__
    assert not hasattr(importer, "__globals__"), "受控导入器身上不该有 __globals__"
    # 行为上仍然是受控版本：不在能力清单里的模块进不来。
    with pytest.raises(ImportError):
        importer("os")
    # 公开的受控导入函数本身仍然可用（测试与宿主内部都直接调它）。
    with pytest.raises(ImportError):
        guarded_import("os")


def test_受控导入器只放行能力清单里的模块():
    from app.plugins.sandbox import guarded_import

    with pytest.raises(ImportError):
        guarded_import("os")
    with pytest.raises(ImportError):
        guarded_import("app.core.db")
    with pytest.raises(ImportError):
        guarded_import("sys", level=1)


def test_内建白名单与扫描器黑名单不打架():
    """运行期拿掉的名字，扫描器本来就不许插件写 —— 两层说的是同一句话。

    两边一旦分叉就会出现「扫描放行但运行期没有」（插件运行期 NameError）
    或者「运行期还在但扫描器看不见」（后者是漏洞）。
    """
    from app.plugins.scan_policy import DENIED_NAMES_ON_CALL, DENIED_NAMES_ON_LOAD

    denied = DENIED_NAMES_ON_LOAD | DENIED_NAMES_ON_CALL
    assert not (denied & ALLOWED_BUILTINS), denied & ALLOWED_BUILTINS


# ---------------------------------------------------------------------------
# 二、审计 §3.1 的原始逃逸链，逐条复现
# ---------------------------------------------------------------------------

#: 审计报告 §3.1 原文给出的最小逃逸链。它在**扫描期是 CLEAN**，
#: 在运行期必须拿不到任何宿主能力。
ESCAPE_CHAIN = '''
import operator

def _anchor():
    return None

def escape():
    g = operator.attrgetter("__globals__")(_anchor)
    b = g["__builtins__"]
    if not isinstance(b, dict):
        b = operator.attrgetter("__dict__")(b)
    return b["__import__"]("os").getpid()
'''


def test_审计报告的逃逸链在运行期拿不到任何东西(tmp_path):
    """``operator.attrgetter("__globals__")`` 这条链必须拿不到 ``__builtins__``。

    审计 §3.1 的实测输出是「扫描器判定 CLEAN —— 放行」，随后在宿主进程里
    ``__import__("os").getpid()`` 成功。修复后这条链必须在**运行期**断掉。
    """
    module = load(tmp_path, ESCAPE_CHAIN)
    with pytest.raises(Exception) as excinfo:
        module.escape()
    # 断点可以是「attrgetter 不给」或「__builtins__ 里没有真的 __import__」，
    # 但**绝不能**是一个真的 pid。
    assert "getpid" not in str(excinfo.value)


def test_逃逸链的每一步都被挡住(tmp_path):
    """逐段验证：属性反射、模块代理、内建三道闸各自都在。"""
    module = load(tmp_path, ESCAPE_CHAIN)
    # 1. attrgetter 不给（模块代理按能力清单裁过）
    with pytest.raises(AttributeError):
        module.operator.attrgetter("__globals__")
    # 2. 插件的 __builtins__ 里没有 getattr —— 「字符串 → 属性访问」这条路不存在
    plugin_builtins = module.__dict__["__builtins__"]
    assert "getattr" not in plugin_builtins
    # 3. 插件的 __builtins__ 里的 __import__ 是受控版本，os 进不来
    with pytest.raises(ImportError):
        plugin_builtins["__import__"]("os")


def test_拿不到真内建的其它入口(tmp_path):
    """``globals`` / ``locals`` / ``vars`` / ``getattr`` 全部不存在。

    它们的共同点：任何一个都能把「模块命名空间」或「按名字取属性」交出去，
    而模块命名空间里有 ``__builtins__``。
    """
    module = load(tmp_path, '''
def probe():
    return {
        "globals": globals,
        "locals": locals,
        "vars": vars,
        "getattr": getattr,
    }
''')
    with pytest.raises(NameError):
        module.probe()


def test_导入面被限制在能力清单内(tmp_path):
    """``os`` / ``sys`` / ``subprocess`` / ``app.core.db`` 一个都导不进来。"""
    for module_name in ("os", "sys", "subprocess", "app", "importlib", "builtins"):
        assert module_name not in ALLOWED_IMPORT_ROOTS
        assert module_name.split(".")[0] not in ALLOWED_IMPORT_ROOTS

    # 模块级 import 直接失败
    with pytest.raises(ImportError):
        load(tmp_path, "import os\n", name="bad_os")

    # 函数里的 import 同样失败
    module = load(tmp_path, '''
def probe():
    import os
    return os
''', name="probe_os")
    with pytest.raises(ImportError):
        module.probe()


def test_执行期也不能再把能力拿回来(tmp_path):
    """``exec`` / ``eval`` / ``compile`` / ``open`` 一律不存在。"""
    module = load(tmp_path, '''
def probe():
    return {
        "exec": eval("1"),
        "eval": eval("1"),
        "compile": compile("1", "<s>", "eval"),
        "open": open("/etc/hostname"),
    }
''')
    with pytest.raises(NameError):
        module.probe()


def test_txt载荷变体走不通(tmp_path):
    """审计 §3.1 的第二条链：恶意体放在 ``payload.txt``，入口 .py 去读并 exec。

    原实测里 ``verify_archive`` 与 ``verify_directory`` **均通过**（安装期与
    加载期都扫，但只扫 .py 的 AST），``os.system`` 已经执行。
    运行期这一层必须让「读文件」和「执行代码」两件事都做不到。
    """
    payload = tmp_path / "payload.txt"
    payload.write_text("import os\nos.system('id')\n", encoding="utf-8")
    module = load(tmp_path, '''
def run():
    with open("payload.txt") as handle:
        exec(handle.read())
''')
    with pytest.raises(NameError):
        module.run()


# ---------------------------------------------------------------------------
# 三、文件读写
# ---------------------------------------------------------------------------

def test_io_只剩内存缓冲(tmp_path):
    """``io`` 模块的磁盘入口全部不给：``open`` / ``open_code`` / ``FileIO``。

    审计 §3.1 实测 ``io.FileIO(path).read()`` 读到了 ``/etc/hosts`` 与
    ``~/.ssh/config`` —— ``io`` 白名单当时只禁了 ``open``，这两个门没禁。
    """
    module = load(tmp_path, '''
import io

def probe():
    found = {}
    try:
        found["open"] = io.open
    except Exception:
        found["open"] = None
    try:
        found["open_code"] = io.open_code
    except Exception:
        found["open_code"] = None
    try:
        found["FileIO"] = io.FileIO
    except Exception:
        found["FileIO"] = None
    return found
''')
    probe = module.probe()
    for name in ("open", "open_code", "FileIO"):
        assert probe[name] is None, f"io.{name} 还能拿到：{probe[name]}"


def test_io的内存缓冲仍然可用(tmp_path):
    """``io`` 不是整个禁掉 —— BytesIO / StringIO 是插件真正需要的那部分。"""
    module = load(tmp_path, '''
import io

def roundtrip():
    buffer = io.BytesIO(b"hello")
    return buffer.read().decode()
''')
    assert module.roundtrip() == "hello"


# ---------------------------------------------------------------------------
# 四、反射原语
# ---------------------------------------------------------------------------

def test_operator不给反射类原语(tmp_path):
    """``operator.attrgetter`` / ``attrsetter`` / ``methodcaller`` 都不给。

    它们是「字符串 → 属性访问」的执行器：不给 ``getattr`` 却给 ``attrgetter``，
    等于把同一个能力换了个名字交出去。
    """
    module = load(tmp_path, '''
import operator

def probe():
    found = {}
    try:
        found["attrgetter"] = operator.attrgetter
    except Exception:
        found["attrgetter"] = None
    try:
        found["attrsetter"] = operator.attrsetter
    except Exception:
        found["attrsetter"] = None
    try:
        found["methodcaller"] = operator.methodcaller
    except Exception:
        found["methodcaller"] = None
    found["itemgetter"] = operator.itemgetter
    return found
''')
    probe = module.probe()
    for name in ("attrgetter", "attrsetter", "methodcaller"):
        assert probe[name] is None, f"operator.{name} 还能拿到"
    # itemgetter 只是下标取值，不涉及 getattr —— 留着它不损失安全性。
    assert probe["itemgetter"] is not None


def test_string的Formatter不给(tmp_path):
    """``string.Formatter.get_field`` 会拿插件给的字段名去做 getattr。

    ``"{0.__class__}".format(obj)`` 这条路径不需要 ``getattr`` 出现在插件代码里，
    所以只禁内建里的 getattr 不够 —— Formatter 自己就是那个执行器。
    """
    module = load(tmp_path, '''
import string

def probe():
    try:
        return string.Formatter
    except Exception:
        return None
''')
    assert module.probe() is None


# ---------------------------------------------------------------------------
# 五、扫描期：字符串常量与收紧后的模块
# ---------------------------------------------------------------------------

def test_扫描器认出了字符串里的危险名字():
    """字符串是形态的盲区 —— 扫描器现在也认它。"""
    assert "PY_STRING_DUNDER" in rules_of("backend/main.py", 'x = "__globals__"\n')
    assert "PY_STRING_DUNDER" in rules_of("backend/main.py", 'x = "__builtins__"\n')
    assert "PY_STRING_CAPABILITY" in rules_of("backend/main.py", 'x = "eval"\n')
    assert "PY_STRING_CAPABILITY" in rules_of("backend/main.py", 'x = "getattr"\n')


def test_扫描器没有误伤正常写法():
    """规则要窄：文档里出现 ``obj.__init__`` 不该被拦。"""
    assert rules_of("backend/main.py", '"""插件的 obj.__init__ 说明"""\n') == set()
    assert rules_of("backend/main.py", 'x = "__name__"\n') == set()
    assert rules_of("backend/main.py", 'x = "__file__"\n') == set()


def test_扫描器拦住了收紧后的模块入口():
    """``operator.attrgetter`` / ``io.FileIO`` / ``string.Formatter`` 都要拦。"""
    assert "PY_ATTRIBUTE" in rules_of("backend/main.py", "import operator\noperator.attrgetter('x')\n")
    assert "PY_IMPORT" in rules_of("backend/main.py", "from operator import attrgetter\n")
    assert "PY_ATTRIBUTE" in rules_of("backend/main.py", "import io\nio.FileIO('/etc/hosts')\n")
    assert "PY_IMPORT" in rules_of("backend/main.py", "from io import FileIO\n")
    assert "PY_ATTRIBUTE" in rules_of("backend/main.py", "import string\nstring.Formatter()\n")


def test_白名单里仍然没有危险模块():
    """能力清单本身不能被改松。"""
    for name in ("os", "sys", "subprocess", "socket", "importlib", "builtins", "pathlib", "shutil"):
        assert name not in BACKEND_MODULES, f"{name} 不该在能力清单里"


# ---------------------------------------------------------------------------
# 六、正常插件代码不能被误伤
# ---------------------------------------------------------------------------

def test_插件的正常写法照常工作(tmp_path):
    """能力裁剪砍的是「拿到能力的方式」，不是「插件能写什么」。"""
    module = load(tmp_path, '''
import io
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException

RULE = re.compile(r"^(\\d{4})-(\\d{1,2})$")
BUSINESS_TZ = timezone(timedelta(hours=8))

class Rule:
    """插件里定义类是最常见的事 —— __build_class__ 必须留着。"""
    def __init__(self, code):
        self.code = code
    def label(self):
        return self.code.upper()

def parse(text):
    matched = RULE.match(text)
    if not matched:
        raise HTTPException(422, "格式不正确")
    year, month = (int(part) for part in matched.groups())
    return date(year, month, 1)

def money(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01")))

def today():
    return datetime.now(BUSINESS_TZ).date()

def buffer():
    return io.BytesIO(b"x").read()
''')
    assert module.Rule("ab").label() == "AB"
    assert module.parse("2026-03").month == 3
    assert module.money("1.005") == "1.00"
    assert module.buffer() == b"x"
    assert module.today().year >= 2024


def test_仓库里真实的插件源码能在沙箱里加载():
    """仓库自带的两个示例插件（plugins/）必须在沙箱里跑得起来。

    这是「没有误伤」这条要求的最终验收：真插件跑不起来，别的都白说。
    """
    sources = sorted(PLUGIN_ROOT.glob("*/backend/main.py"))
    assert sources, "示例插件源码不在仓库里"
    for plugin_dir in sources:
        module = exec_plugin_module("cwq_test_" + plugin_dir.parent.parent.name, plugin_dir)
        assert callable(module.register), f"{plugin_dir} 没有 register"


def test_真实插件源码不触发新的扫描规则():
    """示例插件不该被新的字符串规则误伤。

    ``openpyxl`` 需要 ``upload:read`` / ``import:rw`` 能力，所以这里要带上
    权限参数 —— 否则它会在 import 那一档被拒（那是**既有**规则，与本次改动无关）。
    """
    for plugin_dir in sorted(PLUGIN_ROOT.glob("*/backend/main.py")):
        findings = scan_file("backend/main.py", plugin_dir.read_bytes(),
                             permissions={"upload:read", "import:rw"})
        assert findings == [], f"{plugin_dir}: {findings}"


# ---------------------------------------------------------------------------
# 七、迁移路径：同一条链的另一个入口
# ---------------------------------------------------------------------------

def test_插件迁移也走裁剪后的加载器(tmp_path):
    """插件迁移由宿主 alembic 执行 —— alembic 自己的加载器用**真内建**。

    审计 §3.1 的逃逸链在迁移文件里同样成立（静态扫描同样看不见字符串形态）。
    所以插件迁移执行的那一小段时间里，alembic 的加载器必须被换成裁剪版。
    """
    from alembic.util import pyfiles

    original = pyfiles.load_module_py
    benign = tmp_path / "benign_migration.py"
    benign.write_text("revision = '0001'\ndown_revision = None\n", encoding="utf-8")
    with sandboxed_module_loader():
        assert pyfiles.load_module_py is not original, "alembic 的加载器没被换掉"
        module = pyfiles.load_module_py("benign_migration", benign)
        assert module.revision == "0001"
    assert pyfiles.load_module_py is original, "出了沙箱必须还原 alembic 的加载器"


def test_迁移沙箱里读不到宿主能力(tmp_path):
    """在 alembic 加载器的沙箱里，恶意迁移文件拿不到 ``__import__``。"""
    from alembic.util import pyfiles

    evil = tmp_path / "evil_migration.py"
    evil.write_text(
        'import operator\n'
        'def _anchor():\n    return None\n'
        'def escape():\n'
        '    g = operator.attrgetter("__globals__")(_anchor)\n'
        '    return g["__builtins__"]["__import__"]("os").getpid()\n',
        encoding="utf-8",
    )
    original = pyfiles.load_module_py
    with sandboxed_module_loader():
        module = pyfiles.load_module_py("evil_migration", evil)
    assert pyfiles.load_module_py is original
    with pytest.raises(Exception) as excinfo:
        module.escape()
    assert "getpid" not in str(excinfo.value)


def test_非py文件不许参与exec(tmp_path):
    """**只执行 .py 源文件**（审查 §7 第 6 步第 49 项）。

    ``.pyc`` 是**字节码**：静态扫描器扫的是源码（``scan_file`` 只认
    ``TEXT_TYPES``，``.pyc`` 落到 ``RESOURCE_TYPE`` 而被拒），
    所以字节码里的东西**一道扫描都没经过** —— 它是整条防线里
    唯一完全裸奔的一段。

    现在 alembic 的 ``ScriptDirectory(sourceless=False)`` 默认不加载 ``.pyc``，
    但那是**别人的默认值**：哪天有人为了支持「无源码包」而打开
    ``sourceless=True``，``.pyc`` 就会直接被执行。
    所以这条要写成**代码**，而不是靠默认值。
    """
    from app.plugins.errors import PluginError

    pyc = tmp_path / "mod.pyc"
    pyc.write_bytes(b"\x00\x01")
    with pytest.raises(PluginError) as exc:
        exec_plugin_module("mod", pyc)
    assert "只执行 .py" in str(exc.value)

    # .py 源文件照常能跑
    good = tmp_path / "good.py"
    good.write_text("value = 42\n", encoding="utf-8")
    module = exec_plugin_module("good", good)
    assert module.value == 42


def test_迁移沙箱里也不许执行pyc(tmp_path):
    """alembic 的加载器换成裁剪版之后，``.pyc`` 同样被拒。

    这里是**两个入口**（alembic 的 ``load_python_file`` 会去找 ``.pyc``，
    插件自己的后端入口由 manifest 声明）——
    所以两边都要拒，不能只堵一个。
    """
    from alembic.util import pyfiles

    from app.plugins.errors import PluginError

    pyc = tmp_path / "0001_items.pyc"
    pyc.write_bytes(b"\x00\x01")
    original = pyfiles.load_module_py
    with sandboxed_module_loader():
        with pytest.raises(PluginError) as exc:
            pyfiles.load_module_py("0001_items", pyc)
    assert "只执行 .py" in str(exc.value)
    assert pyfiles.load_module_py is original


# ---------------------------------------------------------------------------
# 八、复审打回来的逃逸路径
# ---------------------------------------------------------------------------
#
# 「运行期裁剪」做出来之后，复审又实测出**四条**新的逃逸路径。
# 它们的共同点：**静态扫描判 CLEAN**，而运行期一路通到宿主能力。
#
#   1. ``dataclasses.sys.modules['os'].getpid()``
#      —— 白名单模块把**别的模块**当普通属性挂着（``dataclasses.sys`` /
#      ``contextlib.os`` / ``uuid.os`` / ``json.codecs`` …）。这些属性名
#      既不是双下划线、也不在黑名单里，扫描器看不见；
#      拿到 ``dataclasses.sys`` 就等于拿到 ``sys.modules``。
#   2. ``dataclasses._real.sys.modules['os'].getpid()``
#      —— 模块代理把**真模块**存在实例字典的 ``_real`` 下。那是个
#      **猜得到的普通名字**：不是双下划线、也不是模块名，扫描器同样看不见。
#   3. ``op._target.execute``
#      —— 符号守卫同理，真对象存在 ``_target`` 下。
#   4. ``__builtins__['__import__'].__globals__['builtins']…``
#      —— 双下划线形态在**运行期**没人拦（只有静态扫描拦），
#      而 ``__globals__`` 里放着真正的 ``builtins`` 模块。
#
# 这里把它们逐条钉死。**任何一条被放回去，下面立刻红。**
# ---------------------------------------------------------------------------

#: 白名单模块上挂着、但**不在**能力清单里的宿主模块属性。
#:
#: 这一组里的每一对都**确实存在于真模块上**（已逐个核实）——
#: 否则用例会因为「属性本来就不存在」而空转，证明不了任何事。
#:
#: 判据是**拿到的东西**的模块名（``value.__name__`` 的根名），
#: 不是属性名本身：属性名只是「插件想要什么」的信号，
#: 拿到的东西是不是清单内的模块才是「实际越没越权」。
HOST_MODULE_ATTRIBUTES = [
    ("dataclasses", "sys"),
    ("dataclasses", "builtins"),
    ("dataclasses", "types"),
    ("dataclasses", "inspect"),
    ("dataclasses", "copy"),
    ("contextlib", "os"),
    ("contextlib", "sys"),
    ("uuid", "os"),
    ("uuid", "sys"),
    ("datetime", "sys"),
    ("enum", "sys"),
    ("typing", "sys"),
    ("typing", "types"),
    ("typing", "warnings"),
    ("collections", "_sys"),
    ("fractions", "sys"),
    ("fractions", "numbers"),
    ("statistics", "random"),
    ("statistics", "sys"),
    ("json", "codecs"),
]

#: 清单**内**的模块值属性：这些不该被一刀切拒掉，
#: 但拿到的必须是**代理**，不是原模块。
WRAPPED_MODULE_ATTRIBUTES = [
    ("io", "_io"),
    ("re", "_compiler"),
    ("re", "_parser"),
    ("string", "_re"),
    ("json", "decoder"),
    ("json", "scanner"),
    ("collections", "abc"),
]


@pytest.mark.parametrize("module_name,attribute", HOST_MODULE_ATTRIBUTES)
def test_白名单模块上的宿主模块属性拿不到(tmp_path, module_name, attribute):
    """白名单模块把别的模块当属性挂着时，代理必须把那一条挡掉。

    复审实测：``dataclasses.sys.modules['os'].getpid()`` 在静态扫描里是
    **CLEAN**（``sys`` 既不是双下划线、也不是黑名单名字），运行期却直接
    拿到了宿主 pid。
    """
    module = load(tmp_path, f'''
import {module_name}

def probe():
    try:
        return {module_name}.{attribute}
    except AttributeError:
        return None
''')
    assert module.probe() is None


@pytest.mark.parametrize("module_name,attribute", WRAPPED_MODULE_ATTRIBUTES)
def test_白名单内的模块值属性也被重新包了一层(tmp_path, module_name, attribute):
    """清单内的模块属性**放行**，但拿到的必须是代理。

    原样返回就等于把真模块交出去 —— 即便这一次它是清单内的模块，
    下一层属性又可能指向清单外的模块（``re._compiler`` 这种）。
    """
    import importlib

    module = load(tmp_path, f'''
import {module_name}

def probe():
    return {module_name}.{attribute}
''')
    value = module.probe()
    real = getattr(importlib.import_module(module_name), attribute)
    assert value is not real, "拿到的是原模块，没有包代理"
    assert type(value).__module__ == "app.plugins.sandbox"


def test_白名单内模块的磁盘入口照样被拒(tmp_path):
    """``io._io`` 是 ``_io`` 那个 C 模块（它的 ``__name__`` 恰好也是 "io"）。

    里面同样有 ``open`` / ``open_code`` / ``FileIO`` ——
    代理必须照样拒，否则「清单内的模块」就成了磁盘入口的后门。
    """
    module = load(tmp_path, '''
import io

def probe():
    found = {}
    try:
        found["open"] = io._io.open
    except Exception:
        found["open"] = None
    try:
        found["open_code"] = io._io.open_code
    except Exception:
        found["open_code"] = None
    try:
        found["FileIO"] = io._io.FileIO
    except Exception:
        found["FileIO"] = None
    return found
''')
    found = module.probe()
    for name in ("open", "open_code", "FileIO"):
        assert found[name] is None, f"io._io.{name} 还能拿到：{found[name]}"


def test_模块代理不把真模块放在实例字典里(tmp_path):
    """``dataclasses._real`` 这种**猜得到的普通名字**不能把真模块交出去。

    复审实测：``dataclasses._real.sys.modules['os'].getpid()`` 静态扫描判
    **CLEAN**，运行期却拿到了宿主 pid —— 因为代理把真模块存在实例字典里。

    所以现在真模块只在**闭包**里，实例字典里除了解释器自己写的元信息
    什么都没有。
    """
    module = load(tmp_path, '''
import dataclasses

def probe():
    try:
        return dataclasses._real
    except AttributeError:
        return None
''')
    assert module.probe() is None

    proxy = module.dataclasses
    assert set(vars(proxy)) <= {"__name__", "__doc__", "__package__", "__loader__", "__spec__"}
    # 代理的字典里更不能有任何指向真模块的值。
    assert not [value for value in vars(proxy).values() if isinstance(value, type(sys))]


def test_符号守卫不把真对象放在实例字典里(tmp_path):
    """``op._target`` 不能把真对象交出去。

    复审实测：``op._target.execute`` 能直接拿到 alembic 的任意 SQL 执行入口 ——
    ``symbol_denied`` 那道闸（``op.execute``）被绕过了，因为它管的是
    **代理上的属性名**，而 ``_target`` 把真对象整个交了出去。

    ``alembic`` 只在**迁移**那一档给（后端代码那一档 import 它直接 ImportError），
    所以这里按迁移档装载。
    """
    path = tmp_path / "guard.py"
    path.write_text(
        "from alembic import op\n"
        "def probe():\n"
        "    try:\n"
        "        return op._target\n"
        "    except AttributeError:\n"
        "        return None\n",
        encoding="utf-8",
    )
    module = exec_plugin_module("guard_probe", path, flavor="migration")
    assert module.probe() is None
    # 更直接的一条：守卫身上**不该有实例字典**（``__slots__ = ()``）——
    # 没有字典，就没有任何键能被用来藏真对象。
    assert not hasattr(module.op, "__dict__"), "守卫身上不该有实例字典"


def test_代理的repr不泄露宿主路径(tmp_path):
    """插件 print 一个模块，不该看到宿主文件系统路径。

    模块默认的 ``repr`` 是 ``<module 'csv' from '/opt/homebrew/.../csv.py'>``。
    那不是能力，但属于「宿主内部结构」不该外泄的东西。
    """
    module = load(tmp_path, '''
import csv

def probe():
    return repr(csv)
''')
    text = module.probe()
    assert "csv" in text
    assert "/" not in text, text


def test_白名单内的模块值属性照常可用(tmp_path):
    """清单内的模块属性不能被一刀切掉 —— ``collections.abc`` 是合法用法。

    规则是「根名在清单里就放行，而且**再包一层**代理」，
    不是「所有模块值属性都拒绝」。
    """
    module = load(tmp_path, '''
import collections

def probe():
    return collections.abc.Iterable
''')
    assert module.probe() is not None


#: 含双下划线形态的插件源码 —— 装载期必须**整个模块拒掉**。
REFLECTION_SOURCES = [
    "def probe():\n    return (1).__class__\n",
    "def probe():\n    return tuple.__base__.__subclasses__()\n",
    "def probe():\n    return __builtins__\n",
    "def probe():\n    return __loader__\n",
    "def probe():\n    return __spec__\n",
    "def probe():\n    return __dict__\n",
    "def probe():\n    return __globals__\n",
    "def probe():\n    return __class__\n",
]


@pytest.mark.parametrize("source", REFLECTION_SOURCES)
def test_装载期拒绝双下划线形态(tmp_path, source):
    """含双下划线形态的插件代码**装都不许装**。

    上一版只靠静态扫描拦这一类，而静态扫描是**安装期**的一次性检查；
    运行期自己没有这道闸，于是 ``tuple.__base__.__subclasses__()``
    在插件里能拿到宿主的全部类。
    """
    from app.plugins.errors import PluginError

    with pytest.raises(PluginError) as exc:
        load(tmp_path, source, name="reflect")
    assert exc.value.code == "PLUGIN_REFLECTION_FORBIDDEN"


@pytest.mark.parametrize("source", REFLECTION_SOURCES)
def test_双下划线判据与扫描器是同一份(tmp_path, source):
    """扫描器与运行期沙箱必须用**同一份**判据。

    两边分叉的两种后果都不行：扫描放行而运行期拒绝（插件装得上装不进），
    扫描拒绝而运行期放行（运行期比扫描期松，那才是漏洞）。
    """
    from app.plugins.errors import PluginError

    assert "PY_REFLECTION" in rules_of("backend/main.py", source)
    with pytest.raises(PluginError):
        load(tmp_path, source, name="same")


def test_正常插件写法里的双下划线不误伤(tmp_path):
    """规则只拦「拿到能力」的那几种形态，不拦普通的类定义。

    ``def __init__`` 是函数名、``__slots__ = (...)`` 是赋值 ——
    两者都不给出任何能力，必须照常放行。
    """
    module = load(tmp_path, '''
class Rule:
    __slots__ = ("code",)

    def __init__(self, code):
        self.code = code

    def label(self):
        return self.code.upper()
''')
    assert module.Rule("ab").label() == "AB"


def test_扫描器认出了逃逸模块属性():
    """``dataclasses.sys`` 这种形状要在**安装期**就被看见。

    它既不是双下划线、也不在黑名单里 —— 旧扫描器判 CLEAN。
    现在按「属性名是白名单外的标准库模块」拒绝。
    """
    assert "PY_MODULE_ESCAPE" in rules_of("backend/main.py", "import dataclasses\ndataclasses.sys\n")
    assert "PY_MODULE_ESCAPE" in rules_of("backend/main.py", "import contextlib\ncontextlib.os\n")
    assert "PY_MODULE_ESCAPE" in rules_of("backend/main.py", "import uuid\nuuid.sys\n")


def test_逃逸模块规则不误伤正常写法():
    """清单内的模块、以及非模块的属性名，都不该被拦。"""
    # re 在清单里：拿到一个本来就能 import 的模块不构成越权。
    assert rules_of("backend/main.py", "import csv\ncsv.re\n") == set()
    assert rules_of("backend/main.py", "import collections\ncollections.abc\n") == set()
    # 属性名不是模块名：ctx.payload 这类访问照常。
    assert rules_of("backend/main.py", "import csv\ncsv.DictReader\n") == set()
    # 从模块 import 出来的**符号**不是模块，判了就是误伤。
    assert rules_of("backend/main.py", "from dataclasses import dataclass\ndataclass.sys\n") == set()


# ---------------------------------------------------------------------------
# 九、导入面分档：扫描期与运行期必须是同一档
# ---------------------------------------------------------------------------
#
# 静态扫描按**文件所在位置**分档（``migrations/`` 前缀才额外允许
# alembic / sqlalchemy），运行期以前一律按**并集**判 ——
# 于是后端插件在运行期也能 ``import sqlalchemy``：
# 扫描期（按路径判）报 PY_IMPORT，运行期却放行。
#
# **同一个量有两个答案，正是 §4.5 说的「同一个量只允许一个权威实现」。**
# 现在两档都从 :data:`RULES_BY_FLAVOR` 派生，扫描器与运行期读同一张表。
# ---------------------------------------------------------------------------


def test_后端代码那一档不含迁移专用的模块(tmp_path):
    """后端插件在**运行期**不该摸得到 alembic / sqlalchemy。

    扫描期已经按路径拒了（``PY_IMPORT``），运行期必须说同一句话。
    """
    module = load(tmp_path, '''
def probe():
    found = {}
    try:
        import sqlalchemy
        found["sqlalchemy"] = sqlalchemy
    except ImportError:
        found["sqlalchemy"] = None
    try:
        import alembic
        found["alembic"] = alembic
    except ImportError:
        found["alembic"] = None
    return found
''')
    found = module.probe()
    assert found["sqlalchemy"] is None, "后端代码那一档不该给 sqlalchemy"
    assert found["alembic"] is None, "后端代码那一档不该给 alembic"


def test_迁移那一档可以import迁移专用的模块(tmp_path):
    """迁移文件要拿宿主连接做 DDL —— 那一档必须给 alembic / sqlalchemy。"""
    path = tmp_path / "0001_x.py"
    path.write_text(
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "REVISION = '0001_x'\n",
        encoding="utf-8",
    )
    module = exec_plugin_module("migration_probe", path, flavor="migration")
    assert module.REVISION == "0001_x"
    assert module.sa is not None
    assert module.op is not None


def test_扫描器与运行期的导入面是同一张表():
    """两档必须从**同一张规则表**派生，不能各写一份。"""
    from app.plugins.scan_policy import IMPORT_FLAVORS, RULES_BY_FLAVOR

    assert set(IMPORT_FLAVORS) == set(RULES_BY_FLAVOR)
    for flavor, rules in RULES_BY_FLAVOR.items():
        assert IMPORT_FLAVORS[flavor] == frozenset(
            name.split(".")[0] for name in rules
        ), f"{flavor} 这一档的根名不是从规则表派生的"

    # 后端那档确实不含迁移专用的模块，迁移那档含。
    assert "sqlalchemy" not in IMPORT_FLAVORS["backend"]
    assert "alembic" not in IMPORT_FLAVORS["backend"]
    assert "sqlalchemy" in IMPORT_FLAVORS["migration"]
    assert "alembic" in IMPORT_FLAVORS["migration"]


def test_扫描器按路径分档与运行期一致():
    """同一个文件，扫描期与运行期必须判成**同一档**。"""
    from app.plugins.security import _rules_for
    from app.plugins.scan_policy import IMPORT_FLAVORS, RULES_BY_FLAVOR

    for path, flavor in (
        ("backend/main.py", "backend"),
        ("migrations/versions/0001_x.py", "migration"),
    ):
        rules = _rules_for(path)
        assert set(rules) == set(RULES_BY_FLAVOR[flavor]), path
        roots = frozenset(name.split(".")[0] for name in rules)
        assert roots == IMPORT_FLAVORS[flavor], path


def test_未知类别直接拒(tmp_path):
    """flavor 拼错必须**拒**，不能默默按某一档放行。"""
    from app.plugins.errors import PluginError

    path = tmp_path / "x.py"
    path.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(PluginError) as exc:
        exec_plugin_module("x", path, flavor="backend_and_migration")
    assert exc.value.code == "PLUGIN_INVALID_FLAVOR"


# ---------------------------------------------------------------------------
# 十、**动态拼名**形态：静态扫描的盲区，运行期必须自己站住脚
# ---------------------------------------------------------------------------
#
# 第八节钉的是**字面量形态**（``dataclasses.sys`` / ``operator.attrgetter("__globals__")``）。
# 但静态扫描按形态判定，**形态本身就是它的盲区**：
#
#   · ``operator.attrgetter("sys")(dataclasses)``
#     —— 属性名 ``sys`` 里**没有双下划线**，不是 PY_MODULE_ESCAPE 那一类；
#     扫描器只能靠「``operator.attrgetter`` 这个执行器不给」来报。
#   · ``getattr(dataclasses, "s" + "ys")``
#     —— 属性名是**拼出来的**，连字面量都不是。
#
# 这两条形态在扫描期都判不到「逃逸模块属性」，所以**必须**由运行期兜底。
# 本节逐条钉死：运行期要么不给那个执行器，要么不给那个属性。
# ---------------------------------------------------------------------------


#: 动态拼名形态的走私尝试：每一条都写明「怎么拼」和「期望被哪一道闸挡住」。
DYNAMIC_SMUGGLING_SOURCES = [
    # operator.attrgetter + **无双下划线**的属性名
    ('''
import operator
import dataclasses

def probe():
    return operator.attrgetter("sys")(dataclasses)
''', "attrgetter 执行器不给"),
    ('''
import operator
import contextlib

def probe():
    return operator.attrgetter("os")(contextlib)
''', "attrgetter 执行器不给"),
    ('''
import operator
import uuid

def probe():
    return operator.attrgetter("sys")(uuid)
''', "attrgetter 执行器不给"),
    # 两步套娃：先拿 modules，再从 modules 里取 os
    ('''
import operator
import dataclasses

def probe():
    s = operator.attrgetter("sys")(dataclasses)
    return operator.attrgetter("modules")(s)
''', "attrgetter 执行器不给"),
    # getattr + 拼出来的属性名
    ('''
import dataclasses

def probe():
    name = "s" + "ys"
    return getattr(dataclasses, name)
''', "内建里没有 getattr"),
    # getattr + 拼出来的模块名（连 import 都绕开）
    ('''
def probe():
    name = "o" + "s"
    return __import__(name)
''', "导入器按能力清单判"),
]


@pytest.mark.parametrize("source,blocked_by", DYNAMIC_SMUGGLING_SOURCES)
def test_动态拼名形态的走私拿不到宿主模块(tmp_path, source, blocked_by):
    """动态拼出来的属性名/模块名，**运行期**必须一样拿不到。

    静态扫描对这一类形态是盲的（它按形态判定），
    所以这里测的是**运行期**那一层 —— 任何一条被放回去，下面立刻红。
    """
    module = load(tmp_path, source)
    try:
        value = module.probe()
    except (AttributeError, NameError, ImportError):
        # 挡住的方式不重要（不给执行器 / 不给属性 / 导入器拒绝），
        # 重要的是**没拿到**。
        return
    # 真拿到了：那就是逃逸
    assert value is None or not hasattr(value, "modules"), (
        f"动态拼名形态拿到了宿主模块：{value!r}"
    )


def test_动态拼名在扫描期不被当成逃逸_所以运行期那一层不能省(tmp_path):
    """这几个形态在扫描期**判不到**逃逸模块属性。

    这不是缺陷，是「扫描按形态判定」的必然结果 —— 但它意味着
    **运行期那一层是唯一防线**，所以它必须一直在。
    """
    # operator.attrgetter 是执行器，扫描器按 PY_ATTRIBUTE 报它
    assert "PY_ATTRIBUTE" in rules_of(
        "backend/main.py", 'import operator\noperator.attrgetter("sys")(dataclasses)\n')
    # getattr 是被禁的名字，按 PY_CALL 报它
    assert "PY_CALL" in rules_of(
        "backend/main.py", 'import dataclasses\ngetattr(dataclasses, "s" + "ys")\n')
    # 但**都不是** PY_MODULE_ESCAPE —— 静态扫描看不见这个形态
    assert "PY_MODULE_ESCAPE" not in rules_of(
        "backend/main.py", 'import dataclasses\ngetattr(dataclasses, "s" + "ys")\n')


# ---------------------------------------------------------------------------
# 十一、运行期必须**失败即闭**：不在能力清单里的模块，一个都不许原样返回
# ---------------------------------------------------------------------------
#
# 旧版本里 ``_sanitize`` 对「不在白名单内的子模块」是**原样返回**的 ——
# 也就是把真模块直接交出去（当时实测 ``_sanitize(os) is os → True``）。
#
# 改成失败即闭（抛 ImportError）之后，这条不变量**没有用例钉着**：
# 有人把那一行改回 ``return module``，整个测试文件照样全绿。
# 这里把它钉死。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", [
    "os", "sys", "subprocess", "importlib", "builtins", "socket", "shutil", "pathlib",
])
def test_不在能力清单里的模块运行期拿不到(tmp_path, module_name):
    """插件在运行期 import 这些模块，一律 ImportError。"""
    module = load(tmp_path, f'''
def probe():
    import {module_name}
    return {module_name}
''')
    with pytest.raises(ImportError):
        module.probe()


def test_沙箱的模块清洗对白名单外的模块失败即闭():
    """``_sanitize`` 是运行期最后一道：**抛错**，不许把真模块原样返回。

    这一条是「原样返回 = 把宿主能力交出去」与「失败即闭」的分界。
    旧版本在这里是 ``return module`` —— 实测 ``_sanitize(os) is os → True``。
    """
    import os as real_os

    from app.plugins.sandbox import _sanitize

    with pytest.raises(ImportError) as exc:
        _sanitize(real_os, ALLOWED_IMPORT_ROOTS)
    assert "不允许使用模块" in str(exc.value)

    # 明确地：不是「返回了但被换了包装」，是**根本没有返回值**
    assert not hasattr(_sanitize, "__wrapped__")


def test_白名单内的模块拿到的是代理不是原模块():
    """在清单里的模块：拿到的必须是**代理**（不是原模块）。

    对照：不在清单里的模块直接抛错。两条合起来才是完整的口径 ——
    一条管「不该有的」，一条管「该有的也不是原物」。
    """
    import csv as real_csv

    from app.plugins.sandbox import _sanitize

    proxy = _sanitize(real_csv, ALLOWED_IMPORT_ROOTS)
    assert proxy is not real_csv, "白名单内的模块被原样交出去了"
    # 代理仍然能用（功能没坏）—— 属性是转发过去的
    assert proxy.DictReader is not None
    assert proxy.DictReader is real_csv.DictReader, "拿到的应当是同一个类，只是经由代理"


# ---------------------------------------------------------------------------
# 十二、openpyxl：能力清单那句「不自己打开文件」必须**强制**
# ---------------------------------------------------------------------------
#
# 清单给 openpyxl 的理由写的是「解析用户上传的 xlsx：只吃内存里的字节，
# **不自己打开文件**」—— 但 ``load_workbook`` 实际能接**路径**：传路径它就
# 自己去开那个文件。于是插件多了一条受限的文件读（能读宿主上任意 xlsx/zip，
# 连目录结构都能从 openpyxl 的报错信息里读出来）。
#
# 这条理由以前只是**注释**；现在由 ``symbol_wrap`` 变成**强制**。
# ---------------------------------------------------------------------------


def test_openpyxl只收内存字节流不收路径(tmp_path):
    """传路径必须被拒 —— 插件不该拿到「打开宿主文件」的能力。"""
    module = load(tmp_path, '''
import io
from openpyxl import load_workbook

def probe_path():
    try:
        load_workbook("/etc/hostname")
    except TypeError as exc:
        return "TypeError: " + str(exc)
    except Exception as exc:
        return "其它异常: " + str(exc)
    return "没拦住"

def probe_bytes():
    try:
        load_workbook(io.BytesIO(b"not-a-zip"))
    except Exception as exc:
        return "走到了 openpyxl: " + str(exc)
    return "没走到 openpyxl"
''')
    result = module.probe_path()
    assert "TypeError" in result, result
    assert "只接受内存里的字节流" in result, result
    # 内存字节流仍然照常送到 openpyxl（由它自己判内容对不对）。
    assert "走到了 openpyxl" in module.probe_bytes()


def test_openpyxl的包装来自能力清单而不是散在代码里():
    """包装必须声明在**能力清单**里 —— 同一个量只允许一个权威实现。"""
    from app.plugins.scan_policy import BACKEND_MODULES

    wraps = dict(BACKEND_MODULES["openpyxl"].symbol_wrap)
    assert "load_workbook" in wraps, "openpyxl 的 load_workbook 没有声明包装"
    # 包装是**收窄**：原符号还在，但路径进不来。
    assert callable(wraps["load_workbook"])
