"""启动前预检（``scripts/preflight.py``）—— 2026-10-17 那次故障的直接产物。

**这些用例守护的不是「脚本能跑」，而是「闸门在正确的位置」。**

那次后端「又」没了响应。根因链是：``uvicorn --reload`` 的 **reloader 父进程**
自己 bind 住 :8000 的 LISTEN 套接字并把它交给 worker 子进程；worker 的
``startup()`` 里 schema 闸门拦人 → 子进程退出；**父进程还活着、还握着那个
套接字、且阻塞在 waitpid**，于是连接能建、永远没人 accept、SIGTERM 也杀不掉。

所以闸门长在 uvicorn startup 里，每拦一次人就产出一次「端口在监听但完全不
响应」。把闸门前移到进程起来之前，是这件事唯一有效的修法。下面的用例钉住
前移之后的行为：

  1. 版本对得上 → 0（能起）
  2. 版本对不上 → **3**，且消息自带可执行命令
  3. 数据库连不上 → **4**，且与「版本对不上」分开报（处置动作完全不同）
  4. 连不上时打印的连接串**不含口令**
  5. 预检自己**不执行任何 DDL**（静态守卫）
  6. preflight 与 dev.sh / systemd 都调同一个入口（静态守卫，防止改了一处忘了另一处）
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy.exc import OperationalError

from app.core import schema_version
from app.core.schema_version import SchemaVersionError

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PREFLIGHT = BACKEND / "scripts" / "preflight.py"
DEV_SH = BACKEND.parent / "dev.sh"
API_UNIT = BACKEND.parent / "deploy" / "systemd" / "cwq-api.service"


def _run_main(monkeypatch, *, ready_impl, env_patch: dict | None = None) -> int:
    """跑 preflight.main()，把 assert_schema_ready 换成指定实现。

    为什么不 ``import scripts.preflight`` 后直接调：那样会拿到真引擎，
    用例就只能依赖「测试库恰好是哪个版本」。这里换成假实现，于是三条退出码
    路径都能被确定性地钉住，不受迁移历史影响。
    """
    monkeypatch.setattr(schema_version, "assert_schema_ready", ready_impl)
    if env_patch is not None:
        monkeypatch.setitem(sys.modules["app.core.config"].settings, "database_url",
                            env_patch["database_url"])
    import importlib
    preflight = importlib.import_module("scripts.preflight")
    return preflight.main(["--quiet"])


# ------------------------------------------------------------ 1. 能起

def test_版本对得上时退出0(monkeypatch, capsys):
    rc = _run_main(monkeypatch, ready_impl=lambda: "0025_backup_run")
    assert rc == 0, "版本对得上却不让起 —— 这条红了说明预检比启动闸门还严"
    capsys.readouterr()


def test_成功时把版本号打出来(monkeypatch, capsys):
    """要报的是**数据库当前的版本**，运维一眼能对上 alembic history。

    静默通过是另一种「看起来在工作但其实没生效」：人只会记得
    「预检过了」，不记得它验的是哪一版。
    """
    import importlib
    preflight = importlib.import_module("scripts.preflight")
    monkeypatch.setattr(schema_version, "assert_schema_ready",
                        lambda: "0025_backup_run")
    assert preflight.main([]) == 0
    assert "0025_backup_run" in capsys.readouterr().out


# ------------------------------------------------- 2. 版本对不上：退出码 3

def test_版本对不上时退出3_不是1(monkeypatch, capsys):
    def boom():
        raise SchemaVersionError("数据库停在 0003，代码要求 0025\n"
                                 "  执行：cd /opt/cwq/backend && .venv/bin/alembic upgrade head")

    rc = _run_main(monkeypatch, ready_impl=boom)
    err = capsys.readouterr().err
    # 必须是 3：systemd 和 dev.sh 都靠「非零」拦停，运维靠「3」认这是迁移问题
    assert rc == 3, f"期望 3（schema 版本），实际 {rc}"
    assert "数据库停在 0003" in err, "原始报错必须原样透出，否则运维要回去翻源码"
    assert "alembic upgrade head" in err, "报错必须自带该执行的命令"


def test_版本对不上时不要碰数据库():
    """schema 不对时**不该**再去 SELECT 一下确认。

    真实故障现场数据库是好的，额外查询只会把一条清楚的报错稀释成两段。
    而且这条钉住「预检是纯检查」的形状：它只调 assert_schema_ready 一次。
    """
    calls = []

    def boom():
        calls.append(1)
        raise SchemaVersionError("库比代码旧")

    import importlib
    preflight = importlib.import_module("scripts.preflight")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(schema_version, "assert_schema_ready", boom)
        assert preflight.main(["--quiet"]) == 3
    assert calls == [1], "预检对闸门的多余调用会让报错时机变得不可预测"


# ---------------------------------------------- 3. 数据库连不上：退出码 4

def test_数据库连不上时退出4_且与版本问题分开报(monkeypatch, capsys):
    def boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    rc = _run_main(monkeypatch, ready_impl=boom)
    err = capsys.readouterr().err
    # 关键在「分开」：版本不对 → 跑 alembic；连不上 → 先看数据库活着没。
    # 混成一句话就会把人引到错误的路上（跑完 alembic 发现还是不通，更懵）。
    assert rc == 4, f"期望 4（数据库不可达），实际 {rc}"
    assert "alembic upgrade" not in err, \
        "数据库连不上时提 alembic 会把人引向错误的处置动作"
    assert "数据库" in err


# --------------------------------------------- 4. 连接串脱敏

def test_数据库报错里的连接串不含口令(monkeypatch, capsys):
    """连接串带口令是这仓库反复强调的红线（config.py 的生产校验同款）。"""
    secret = "SuperSecret-Pg-Passw0rd"

    def boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    import importlib
    from app.core.config import settings
    preflight = importlib.import_module("scripts.preflight")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(schema_version, "assert_schema_ready", boom)
        mp.setattr(settings, "database_url",
                   f"postgresql+psycopg://cwq:{secret}@db.example:5432/cwq")
        assert preflight.main(["--quiet"]) == 4
    err = capsys.readouterr().err
    assert secret not in err, "口令被原样打进 stderr 了"
    # host/port/库名必须留着 —— 「连不上」和「连错库」是两回事
    assert "db.example" in err and "cwq" in err


# ------------------------------------------ 5. 静态守卫：预检不做 DDL

def test_预检里没有任何DDL():
    """预检只做版本检查。

    它要是偷偷 ``alembic upgrade head``，就等于让「启动时自动改表结构」这条
    已经付过学费的教训重新发生一次 —— 而且这次更隐蔽，因为预检跑在
    systemd 的 ``ExecStartPre`` 里，看起来像部署步骤的一部分。
    """
    tree = ast.parse(PREFLIGHT.read_text(encoding="utf-8"), filename=str(PREFLIGHT))
    banned = {"create_all", "drop_all", "execute", "exec_driver_sql", "upgrade"}
    offenders = [
        ast.dump(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr in banned
    ]
    offenders += [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id in banned
    ]
    assert not offenders, f"预检里出现了改表结构的调用：{offenders}"


def test_预检不import主应用模块():
    """不 import ``app.main``。

    理由与 oss2 的延迟导入一样：预检要在**应用起不来**的场景下运行，
    它自己可不能依赖应用能 import。``app.main`` 会拉起全部 router。
    """
    tree = ast.parse(PREFLIGHT.read_text(encoding="utf-8"), filename=str(PREFLIGHT))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("app.main"), \
                    "预检 import 了 app.main —— 起不来的场景下它自己也会炸"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "") != "app.main"


# ------------------------- 6. 静态守卫：三条启动路径调的是同一个入口

def test_devsh在起进程之前先跑预检():
    """闸门必须**在 spawn uvicorn 之前**。

    这条是整件事的核心约束：只要 uvicorn 先起来了，预检再怎么准也只是
    多打一行字，而那正是「端口在监听但没人应答」的成因。
    """
    text_ = DEV_SH.read_text(encoding="utf-8")
    assert "preflight.py" in text_, "dev.sh 没有调用预检"
    i_pre = text_.index("preflight.py")
    i_uvicorn = text_.index("uvicorn app.main:app")
    assert i_pre < i_uvicorn, \
        "预检必须排在 uvicorn 之前；排在后面等于没拦"


def test_systemd的ExecStartPre调预检():
    unit = API_UNIT.read_text(encoding="utf-8")
    pres = [ln for ln in unit.splitlines() if ln.startswith("ExecStartPre=")]
    assert any("preflight.py" in ln for ln in pres), \
        "生产单元没有 ExecStartPre 预检 —— 生产没有 --reload 挡在前面，\n" \
        "闸门只能靠这里"
    # 必须在 ExecStart 之前
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=/opt/cwq")


def test_预检脚本本身能执行():
    """能跑，且失败时非零退出。

    ``dev.sh`` 靠退出码决定起不起 uvicorn，所以「脚本崩了但退出 0」和
    「脚本崩了」同样致命 —— systemd 那一侧更糟：ExecStartPre 失败会被
    记成启动失败并无限重试，而真正的原因藏在脚本的 traceback 里。
    """
    src = PREFLIGHT.read_text(encoding="utf-8")
    assert "raise SystemExit(main())" in src, \
        "退出码没有传给 shell —— 调用方分不出成功与失败"
    compile(src, str(PREFLIGHT), "exec")  # 语法层面的冒烟


# ============================ dev.sh 的形状守卫（静态）
#
# 这些不是「跑一下看看对不对」，而是**形状**上的约束。两次真实的坑都出在
# 形状上，而形状是可以静态钉死的：等到运行时才发现，代价是一次假的启动成功。


def test_devsh的wait_live调用都传够三个参数():
    """``wait_live <名字> <url> <超时秒>`` —— 少传/多传都不会报错。

    踩过：前端那行写成了 ``wait_live frontend vite "$VITE_URL" "$T"``，
    于是 ``$3`` 是 URL 字符串，``deadline=$(( $(date +%s) + timeout ))``
    在算术上下文里把 ``http`` 当成了变量名 → ``http: unbound variable``。
    前端探测**永远失败**，而失败信息指向一个跟前端毫无关系的名字。

    注意它不会让 shell 报错退出：``wait_live`` 在 ``if !`` 里调用，整条命令
    的失败被当成「没就绪」，于是表现是「dev.sh 说不好了」—— 一个只靠跑一遍
    很难定位到根因的错误。
    """
    import re

    text_ = DEV_SH.read_text(encoding="utf-8")
    calls = [ln for ln in text_.splitlines()
             if re.search(r"\bwait_live\b", ln)
             and not ln.lstrip().startswith("#")
             and not re.search(r"\bwait_live\s*\(\s*\)", ln)]  # 排除函数定义本身
    assert calls, "dev.sh 里没有 wait_live 调用 —— 守卫本身失效了"
    for ln in calls:
        # 去掉 `|| return 0` 之类尾巴后数顶层参数
        args = ln.split("wait_live", 1)[1]
        args = args.split("||")[0].split(";")[0].split(")")[0]
        args = args.split("&&")[0]
        n = len([a for a in args.split() if a])
        assert n == 3, f"wait_live 调用给了 {n} 个参数，应为 3：{ln.strip()}"


def test_仓库里的sh脚本没有变量紧跟非ASCII字符():
    """``$p）`` 这类写法在 UTF-8 locale 下会被解析成变量名 ``p）``。

    为什么这是真会咬人的：bash 用 ``isalnum()`` 判断变量名还能继续，
    而 macOS 上 ``LC_CTYPE=C.UTF-8`` 时全角字符的 isalnum 为真 ——
    所以 ``echo "pid $p）"`` 里的 ``$p`` **不是** ``$p``，而是一个叫 ``p）``
    的未定义变量。脚本带着 ``set -u`` 会在那一行直接退出：

        dev.sh: line 172: p?: unbound variable

    而那一行正是「SIGTERM 被吞了、已 KILL 整个进程组」这条消息 ——
    也就是说**清理动作做完了，脚本却在报错的下一行崩掉**。踩过一次，记在这里。
    规则很简单：变量后面紧跟任何非 ASCII 字符，一律写 ``${p}``。
    """
    import re

    # $VAR 后面紧跟一个非 ASCII 的、非 { 的字符
    pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?![\x00-\x7f{])")
    offenders = []
    root = DEV_SH.parent
    for sh in sorted(root.rglob("*.sh")):
        # ``output/`` 是**本地构建产物目录**（.gitignore 里有），里面是从
        # 证书脚本复制出来的副本 —— 它们不在版本库里，却会被 rglob 扫到。
        # 一台机器上跑过一次证书流程，这个用例就永远红着，而仓库本身是干净的。
        # 与 node_modules / dist 同一类：不是源码，就不该参与源码的护栏。
        if any(x in sh.parts for x in ("node_modules", ".venv", ".run", "dist", "output")):
            continue
        for i, line in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
            for m in pat.finditer(line):
                nxt = line[m.end():m.end() + 1]
                if nxt:  # 行尾不算
                    offenders.append(f"{sh.relative_to(root)}:{i} ${m.group(0)[1:]}+{nxt!r}")
    assert not offenders, "这些 $变量 紧跟非 ASCII 字符，会被并进变量名：\n  " + \
                          "\n  ".join(offenders)
