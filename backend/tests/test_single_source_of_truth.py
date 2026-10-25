"""「同一个量只允许有一个权威实现」—— 防审计 §4.5 那张表重新长出来。

审计 §4.5 的第三个系统性结论：同一个量在多个地方各算一遍，
每处的修复都是「删掉一处、调用另一处」，工作量以小时计，
却能一次性消掉一整类未来缺陷。

本文件是审计 §7 **第 7 步**要的那条**结构性防回归规则**：
它**不改业务代码**，只断言「这几个量各自只有一处实现」。
一旦有人又写了第二份，这里立刻红 —— 而那时候改动还很小。

这里守的是三个**后端**的量（金额格式化那一条在前端另有一份：
``frontend/tests/single_source.test.mjs``）：

1. **角色归一化** → 权威实现 ``app/core/deps.py::normalize_role``
2. **凭证号解析** → 权威实现 ``app/accounting/organize.py::normalize_no`` / ``require_no``
3. **凭证字推导** → 权威实现 ``app/accounting/organize.py::period_prefix`` / ``split_no``

外加两条同源的护栏：

4. **凭证字不许写死** —— ``period_prefix`` 的默认值「记」是个陷阱，
   谁在新建凭证时写死它，那个期间就混用了两个凭证字（审计 §3.18）。
5. **凭证版本令牌的格式** —— 令牌是客户端与服务端之间的一枚硬币，
   两边格式差一个时区写法，「带了令牌」就会一律判成「版本过期」（审计 §3.15）。
6. **审计的实体标识长度** —— §4.5 那张表里「其余 6 处都截断、就一处整段塞入」
   的那一行（审计 §3.21）。它不算「第二个实现」，但属于同一类病：
   同一个量在一处收敛了、在另一处没收敛。

**为什么用 AST 而不是正则扫源码**：正则会把注释里写的反面教材
（「原来这里是 ``if link.role == "VIEWER"``」）也当成实现 ——
而那些注释恰恰是本仓最有价值的部分。AST 只看**真正在执行的代码**。

**判据为什么都这么窄。** 第一版的「凭证字推导」判据是
「函数里出现过 voucher_no 和 prefix 就查一遍」—— 结果
``next_voucher_no`` / ``_prefix_seq_expr`` 这些**正当的调用方**全被拖进来
（它们拿 ``prefix`` 当**参数**，不是自己算凭证字）。
那种判据等于没有判据：误报一多就没人看它了。
所以现在每条规则的判据都是「**赋值/调用这个具体形状**」，
而不是「这个文件里大概出现过这个词」。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

#: 角色字面量（判定时必须先归一化，见 deps.normalize_role）。
ROLE_LITERALS = {"owner", "editor", "viewer"}

#: 「有角色权限」那几个值在库里的原样写法。
ROLE_NAMES = {"OWNER", "EDITOR", "VIEWER"}


def _py_files():
    return sorted(APP_ROOT.rglob("*.py"))


def _tree(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:      # pragma: no cover - 源码坏了由别的测试负责报
        return None


def _rel(path: Path) -> str:
    return str(path.relative_to(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# 1. 角色归一化：判定角色不许拿字面量比
# ---------------------------------------------------------------------------

def test_角色判定不许拿字面量比():
    """除了权威实现以外，任何地方都**不许**拿 role 字面量直接比。

    以前有两套实现：``app/core/deps.py`` 用 ``normalize_role``（吃大小写与
    首尾空白），而 ``app/api/reports.py`` 与 ``app/api/printing.py`` 用
    ``link.role != "OWNER"`` —— 库里存着 ``owner`` 时，
    一个放行、一个拒绝，合法人被误拒（审计 §3.12）。

    这条规则把「口径分裂」变成**编译不过**的事实：谁想比角色，
    先去 ``normalize_role``。
    """
    authority = APP_ROOT / "core" / "deps.py"
    offenders = []
    for path in _py_files():
        if path == authority:
            continue
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                # 只看「一边是 role、另一边是角色字面量」的比较
                if not (isinstance(comparator, ast.Constant)
                        and isinstance(comparator.value, str)
                        and comparator.value.upper() in ROLE_NAMES):
                    continue
                if not _looks_like_role(node.left):
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                 f"{ast.unparse(node)}")
    assert not offenders, (
        "角色判定出现了第二套实现（拿字面量比大小写）。\n"
        "请改用 core.deps.normalize_role —— 它吃大小写与首尾空白，"
        "库里存着 'owner' 时不该换个含义。\n  "
        + "\n  ".join(offenders))


def _looks_like_role(node: ast.AST) -> bool:
    """这个表达式是不是「某个角色」。

    只认明显的写法（``link.role`` / ``user_book.role`` / ``role``），
    免得把无关的比较也拖进来。
    """
    if isinstance(node, ast.Name):
        return node.id == "role" or node.id.endswith("_role")
    if isinstance(node, ast.Attribute):
        return node.attr == "role" or node.attr.endswith("_role")
    return False


# ---------------------------------------------------------------------------
# 2. 凭证号解析：只有 organize 一份
# ---------------------------------------------------------------------------

def test_凭证号的解析只有一份实现():
    """凭证号「凭证字 + 序号」的解析**只有** organize 里的那一份。

    权威实现是 ``app/accounting/organize.py`` 的 ``normalize_no`` /
    ``require_no`` / ``split_no``，配套常量 ``NO_WIDTH`` / ``_NO_SPLIT`` /
    ``_NO_SEPARATOR``。

    任何别处再写一个「凭证号形状」的正则，都是同一个量算两遍 ——
    而这两个口径一旦分叉，撞号判断就跟着错（审计 §3.17、§3.20）。
    """
    authority = APP_ROOT / "accounting" / "organize.py"
    offenders = []
    for path in _py_files():
        if path == authority:
            continue
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "re.compile" or not node.args:
                continue
            pattern = node.args[0]
            if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)):
                continue
            # 「凭证号形状」的正则有个很显眼的指纹：它给前缀与序号分了组。
            if "(?P<prefix>" in pattern.value and "(?P<seq>" in pattern.value:
                offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                 f"又写了一份凭证号形状的正则")
    assert not offenders, (
        "凭证号的解析出现了第二份实现。\n"
        "请调用 accounting.organize 的 normalize_no / require_no / split_no。\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# 3. 凭证字推导：调用点必须显式传前缀
# ---------------------------------------------------------------------------

def test_发号必须显式传凭证字():
    """``next_voucher_no`` 的每个调用点都必须**显式**传前缀。

    它的前缀参数有个默认值「记」。一旦某个调用点不传，那个期间在用的
    凭证字（导入进来的「银」之类）就被忽略了 —— 新建的凭证带着「记」，
    期间立刻变成「记、银 混用」，``assert_tidy`` 判为 blocking，
    **那个月从此既不能记账也不能结账**（审计 §3.18）。

    所以默认值是**陷阱**：这里断言「没有一个调用点靠它」。
    """
    offenders = []
    for path in _py_files():
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "next_voucher_no":
                continue
            has_prefix_kw = any(kw.arg == "prefix" for kw in node.keywords)
            if len(node.args) >= 3 or has_prefix_kw:
                continue
            offenders.append(f"{_rel(path)}:{node.lineno} —— "
                             f"next_voucher_no({', '.join(ast.unparse(a) for a in node.args)}) "
                             f"没传凭证字")
    assert not offenders, (
        "有调用点没传凭证字 —— 那个期间在用的凭证字会被忽略。\n"
        "请用 accounting.organize.period_prefix(db, period, fallback=...) "
        "取出该期的凭证字再传进来。\n  "
        + "\n  ".join(offenders))


#: 「凭证字」是**中文单字**（记 / 银 / 收 / 付 / 转 …）。
#:
#: 用它来把「凭证字」与别的 ``*prefix*`` 变量区分开 ——
#: ``backup.config.DEFAULT_PREFIX = "acct-backup"``、
#: ``core.config.oss_prefix = "acct-backup"``、
#: ``mcp.auth.TOKEN_PREFIX = "mcp_"`` 都不是凭证字。
VOUCHER_PREFIX_CHARS = set("记银收付转")


def _is_voucher_prefix_literal(value: ast.AST) -> bool:
    """这个字面量长得像「凭证字」吗？

    判据：**一~两个汉字**，且每个字都在 ``VOUCHER_PREFIX_CHARS`` 里。

    收紧到这个程度是有原因的：判据太宽会把 ``acct-backup`` /
    ``CWQ_LOG_RETENTION_`` 这些**完全无关**的前缀也拖进来 ——
    那不是「同一个量算两遍」，只是名字里带了 prefix。
    """
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        return False
    text = value.value
    return 1 <= len(text) <= 2 and all(ch in VOUCHER_PREFIX_CHARS for ch in text)


def test_凭证字不许写死():
    """凭证字**不许**以字面量的形式出现在代码里。

    审计 §4.5 第四行：凭证的「凭证字」有两处实现 —— 跨期改号用
    ``organize.period_prefix``（取该期在用的那个），新建却**写死「记」**。
    两个口径一旦分叉，期间就混用了两个凭证字，那个月的「整理就绪」体检
    报 blocking，**既不能记账也不能结账**（审计 §3.18）。

    权威实现是 ``app/accounting/organize.py::period_prefix``。
    这条规则盯的是「字面量」这种最直接的写死 ——
    ``prefix = "记"`` 一旦出现，那处就没有问过「这个期间在用哪个凭证字」。
    """
    offenders = []
    for path in _py_files():
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None or not _is_voucher_prefix_literal(value):
                continue
            for target in targets:
                name = _target_name(target)
                if not name or "prefix" not in name.lower():
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                 f"{name} = {value.value!r}（凭证字写死了）")
    assert not offenders, (
        "凭证字被写死了 —— 新建凭证会带着「记」，而那个期间在用的可能是「银」。\n"
        "请调用 accounting.organize.period_prefix(db, period, fallback=...) "
        "取出该期在用的凭证字。\n  "
        + "\n  ".join(offenders))


def _target_name(target: ast.AST) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def test_从凭证号推凭证字必须走权威实现():
    """「从凭证号推出凭证字」必须调用 organize 里的那一份。

    权威实现有两把钥匙：
      · ``period_prefix(db, period, fallback=...)`` —— 问「这个**期间**在用哪个」；
      · ``split_no(no)`` / ``normalize_no(no)`` —— 问「这个**号**的字是什么」。

    **判据是赋值，不是函数。** 早先一版按「函数里出现过 voucher_no 和
    prefix 就查一遍」—— 结果 ``next_voucher_no`` / ``_prefix_seq_expr``
    这些**正当的调用方**全被拖进来（它们拿 ``prefix`` 当**参数**，
    不是自己算凭证字）。那种判据等于没有判据：误报一多就没人看它了。

    现在只盯真正危险的那种形状：
    **一个名字含 ``prefix`` 的变量，它的值是从 ``voucher_no`` 上切出来的**
    —— 而切法不是权威实现里的那两把钥匙。

    现在仓库里合法的写法长这样（都过）::

        prefix, seq = svc_organize.split_no(canon or voucher.voucher_no)   # 用权威实现
        prefix = svc_organize.period_prefix(db, period, fallback="记")       # 用权威实现

    要被这条规则拦下的是长这样的::

        prefix = voucher.voucher_no[:2]      # 自己切一刀 = 第二个口径
    """
    authority = APP_ROOT / "accounting" / "organize.py"
    allowed = {"period_prefix", "split_no", "normalize_no", "require_no"}
    offenders = []
    for path in _py_files():
        if path == authority:
            continue
        tree = _tree(path)
        if tree is None:
            continue
        for func in _scopes(tree):
            called = {c.func.attr if isinstance(c.func, ast.Attribute)
                      else getattr(c.func, "id", "")
                      for c in ast.walk(func) if isinstance(c, ast.Call)}
            if called & allowed:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    name = _target_name(target)
                    if not name or "prefix" not in name.lower():
                        continue
                    if not _mentions_voucher_no(node.value):
                        continue
                    offenders.append(
                        f"{_rel(path)}:{node.lineno} —— "
                        f"{name} 自己从凭证号切了一段（{ast.unparse(node.value)}），"
                        f"没调用 {sorted(allowed)}")
    assert not offenders, (
        "「从凭证号推凭证字」出现了第二份实现。\n"
        "请调用 accounting.organize.period_prefix / split_no / normalize_no。\n  "
        + "\n  ".join(offenders))


def _scopes(tree: ast.AST):
    """模块级 + 每个函数体，各算一个「作用域」。"""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _mentions_voucher_no(value: ast.AST) -> bool:
    """这个表达式里读到了凭证号吗？"""
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr == "voucher_no":
            return True
        if isinstance(node, ast.Name) and node.id == "voucher_no":
            return True
    return False


# ---------------------------------------------------------------------------
# 5. 凭证版本令牌：格式只有一份
# ---------------------------------------------------------------------------

def test_版本令牌的格式只有一份():
    """乐观锁令牌是**客户端与服务端之间的一枚硬币**（审计 §3.15）。

    客户端把它读到的值原样送回来，服务端拿库里此刻的值算一个同样的东西
    来比 —— 两边的格式必须**一模一样**。

    差一个时区写法、差一个 ``Z``，比对就**永远不相等**：
    于是「带了令牌」会一律判成「版本过期」——
    用户每保存一次就被拒一次，而原因他完全看不见（那串时间戳对不上，
    但界面上一句提示都没有）。

    权威实现是 ``app/accounting/vouchers.py::version_token``。

    **判据是「作为 version 字段产出」这个形态，不是「把 updated_at 转成文本」。**
    后者在本仓是个**通用排版写法**（``mcp_admin.py`` 把令牌表的几个时间戳
    转成 ISO 文本给人看），那不是这个量 —— 第一版按前者判，
    立刻把 ``app/api/mcp_admin.py`` 拖了进来。
    """
    authority = APP_ROOT / "accounting" / "vouchers.py"
    offenders = []
    for path in _py_files():
        if path == authority:
            continue
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            # 形态一：``{"version": <x.updated_at.isoformat()>}``
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if not (isinstance(key, ast.Constant) and key.value == "version"):
                        continue
                    if _is_updated_at_isoformat(value):
                        offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                         f"version 字段自己转了一份 ISO 文本")
            # 形态二：``version = <x.updated_at.isoformat()>``
            #   与 ``out["version"] = <x.updated_at.isoformat()>``（下标赋值）
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if _is_version_target(target) and _is_updated_at_isoformat(node.value):
                        offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                         f"version 字段自己转了一份 ISO 文本")
    assert not offenders, (
        "版本令牌的格式出现了第二份实现。\n"
        "令牌是客户端与服务端之间的一枚硬币 —— 两边格式必须逐字一致，"
        "差一个时区写法就会「带了令牌也永远判过期」。\n"
        "请调用 accounting.vouchers.version_token。\n  "
        + "\n  ".join(offenders))


def _is_version_target(target: ast.AST) -> bool:
    """这个赋值目标是「version 字段」吗？

    两种写法都算：
      · ``version = …`` —— 名字里带 version；
      · ``out["version"] = …`` —— 下标赋值，键是 ``version``。
    """
    if isinstance(target, ast.Subscript):
        key = target.slice
        return isinstance(key, ast.Constant) and key.value == "version"
    name = _target_name(target)
    return bool(name) and "version" in name.lower()


def _is_updated_at_isoformat(node: ast.AST) -> bool:
    """这个表达式是「某个 updated_at 上的 isoformat()」吗？"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not (isinstance(func, ast.Attribute) and func.attr == "isoformat"):
            continue
        base = func.value
        if isinstance(base, ast.Attribute) and base.attr == "updated_at":
            return True
    return False


# ---------------------------------------------------------------------------
# 4. 审计实体标识的长度收敛（§4.5 同一张表的第四行）
# ---------------------------------------------------------------------------

def test_审计的实体标识长度都收敛过():
    """``audit_log.entity_id`` 是 ``String(64)``，写入点必须**收敛**长度。

    同一个仓里其余写入点都做了收敛（``plugin_id[:64]``、
    ``sha256(...)[:32]``、``username[:64]``），只有一处把整段塞进去 ——
    库层 22001 → 整个事务回滚 → **开关被静默回滚**，连审计行都没留下
    （审计 §3.21）。

    这条规则盯的是「有没有把一个可能很长的值直接写进去」。
    """
    offenders = []
    for path in _py_files():
        tree = _tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "log":
                continue
            for kw in node.keywords:
                if kw.arg != "entity_id":
                    continue
                value = kw.value
                if _entity_id_is_bounded(value):
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno} —— "
                                 f"entity_id 没有收敛长度：{ast.unparse(value)}")
    assert not offenders, (
        "审计的 entity_id 没有收敛长度。\n"
        "audit_log.entity_id 是 String(64)：直接塞一个可能很长的值进去，"
        "库层会回 22001 并把整个事务回滚（审计 §3.21）。\n"
        "请照 scheduler.service._marker_id 的口径：sha256(...)[:32]。\n  "
        + "\n  ".join(offenders))


def _entity_id_is_bounded(value: ast.AST) -> bool:
    """这个 entity_id 的值**一定**不超过 64 字符吗？

    判据按「值的形态」而不是「值的类型」（静态看不出类型）：
      · 字面量        → 长度已知（超 64 就不该写）；
      · 切片          → 已经截断过；
      · 散列/格式化   → 定长（sha256.hexdigest 64 位、strftime 定长格式）；
      · ``*.id``      → 整型 id，转成字符串最多 19 位；
      · 全大写的常量  → 配置键名，长度由人控制（都在几十字符内）；
      · f-string      → 拼出来的，由各段决定；不比「整段塞入」更危险。
    剩下的是**整段塞入的字符串**（``task_key``、``plugin_id``、``username``…）——
    那才是 §3.21 那种 22001 的来源。
    """
    if isinstance(value, ast.Constant):
        return not isinstance(value.value, str) or len(value.value) <= 64
    if isinstance(value, ast.Subscript):
        return True
    if isinstance(value, ast.JoinedStr):
        return True
    if isinstance(value, ast.BoolOp):
        # ``x or "ALL"`` / ``a and b``：**每一支**都有界，整体才有界。
        return all(_entity_id_is_bounded(v) for v in value.values)
    if isinstance(value, ast.Call):
        fname = value.func
        name = fname.attr if isinstance(fname, ast.Attribute) else getattr(fname, "id", "")
        if name in {"sha256", "hexdigest", "md5", "sha1", "strftime", "format", "hex"}:
            return True
        # str(...) / int(...) 之类：内容有界与否取决于里面那个值，
        # 交给「整段塞入」那一档去判。
        if name in {"str", "int"}:
            return _entity_id_is_bounded(value.args[0]) if value.args else False
        return False
    if isinstance(value, ast.Attribute):
        last = value.attr
        if last.lower().endswith("id"):
            return True
        if last.isupper():
            return True
        return False
    if isinstance(value, ast.Name):
        if value.id.isupper():
            return True
        if value.id.lower().endswith("_id"):
            return True
        return False
    return False
