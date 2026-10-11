"""测试库护栏（审计 P3-02）。

``conftest.py`` 里有 ``DROP SCHEMA public CASCADE``。它让测试可重复，
但也意味着**一次环境变量误配就当场删库**：

    CWQ_TEST_DATABASE_URL=postgresql+psycopg://cwq:cwq123@127.0.0.1:5432/cwq pytest

这不是理论场景 —— 变量名合法，而仓库根目录 ``.env.example`` 里就摆着
一条 ``.../cwq`` 的开发连接串，照着改个名就够了。

所以这些用例全部在**子进程**里跑：护栏是在 conftest import 期执行的，
而 conftest 一旦 import 就会连上库并 DROP SCHEMA。在本进程里测它等于
拿自己当试验品。而且护栏必须先于任何连接生效，所以也只能这么测。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CONFTEST_DIR = BACKEND / "tests"


def _run_conftest(url: str, extra_env: dict | None = None, timeout: float = 25.0):
    """把 conftest 当脚本 import 一次，返回 (returncode, stdout+stderr)。

    import 到一半失败是**预期结果**，所以不捕获异常 —— 让它自己抛，
    我们只看退出码和输出。
    """
    env = dict(os.environ)
    env["CWQ_TEST_DATABASE_URL"] = url
    env.pop("CWQ_ALLOW_REMOTE_TEST_DB", None)
    env.update(extra_env or {})
    code = (
        "import conftest\n"
        "print('GUARD_PASSED')\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(CONFTEST_DIR),
                           env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT（护栏没拦住，真的连上去了）"
    return r.returncode, r.stdout + r.stderr


def _assert_blocked(url: str, extra_env=None):
    code, out = _run_conftest(url, extra_env)
    assert code is not None, f"护栏没拦住，进程超时：{url}"
    assert code != 0, f"护栏放行了这个 URL：{url}\n{out}"
    assert "GUARD_PASSED" not in out, f"conftest 竟然 import 成功了：{url}"
    return out


# --------------------------------------------------------------------- 必须拦下


def test_开发库名被拦下():
    """``.env.example`` 里那条连接串，原样指过来就必须拒绝。"""
    out = _assert_blocked("postgresql+psycopg://cwq:cwq123@127.0.0.1:5432/cwq")
    assert "_test" in out, f"报错没有说清要求：{out}"


def test_生产库名被拦下():
    _assert_blocked("postgresql+psycopg://u:p@db.internal:5432/accounting")


def test_缺库名的_URL_被拦下():
    _assert_blocked("postgresql+psycopg://cwq:cwq123@127.0.0.1:5432")


def test_空库名被拦下():
    _assert_blocked("postgresql+psycopg://cwq:cwq123@127.0.0.1:5432/")


def test_生产主机名即便库名带test也拦下():
    """库名对了不代表安全 —— ``prod_db_test`` 这种名字是有人会起的。"""
    _assert_blocked("postgresql+psycopg://u:p@prod-db:5432/cwq_test")


def test_docker默认服务名被拦下():
    """docker-compose 里那个叫 ``db`` 的服务，是最常见的误连目标。"""
    _assert_blocked("postgresql+psycopg://cwq:pw@db:5432/cwq_test")


def test_远端测试库需要显式确认():
    """远端库多加一道：本地误配最多毁本机开发数据，远端可能就是生产。"""
    _assert_blocked("postgresql+psycopg://cwq:pw@10.0.0.5:5432/cwq_test")
    _assert_blocked("postgresql+psycopg://cwq:pw@ci-db.internal:5432/cwq_test",
                    {"CWQ_ALLOW_REMOTE_TEST_DB": "0"})


def test_报错信息里不含密码():
    """报错会被贴到 issue / CI 日志里，密码不该跟着出去。"""
    out = _assert_blocked("postgresql+psycopg://cwq:sup3rsecret@127.0.0.1:5432/cwq")
    assert "sup3rsecret" not in out, f"报错里漏了明文密码：{out}"
    assert "***" in out, f"应当把密码打码后显示目标：{out}"


# --------------------------------------------------------------------- 必须放行


def test_本地测试库正常放行():
    """护栏不能把正常工作流也拦下来 —— 那只会让人学会用 --force 绕过它。"""
    code, out = _run_conftest(
        "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_test")
    assert code == 0, f"合法的本地测试库被拦下了，这比误删更糟：\n{out}"
    assert "GUARD_PASSED" in out


def test_远端测试库显式确认后放行():
    code, out = _run_conftest(
        "postgresql+psycopg://cwq:pw@10.0.0.5:5432/cwq_test",
        {"CWQ_ALLOW_REMOTE_TEST_DB": "1"})
    assert code == 0, f"显式确认后仍被拦下：\n{out}"


def test_DROP_SCHEMA_之前会打印目标库():
    """事后唯一的线索：出事时日志里得能证明当时连的是哪个库。"""
    code, out = _run_conftest(
        "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_test")
    assert code == 0, out
    assert "cwq_test" in out, f"DROP SCHEMA 前没有打印目标库：\n{out}"
