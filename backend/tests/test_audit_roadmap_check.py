"""``tools/audit_roadmap_check.py`` 这条闸门**自己**的反向测试。

**为什么要有这个文件。**

终审脚本在 CI 里是**当闸门跑的**（backend job 的最后一步，
``python3 tools/audit_roadmap_check.py``，退出码就是结论）。但闸门本身
**没有人盯着** —— 于是「把判据写松了」这件事没人会发现，而它造成的后果
是「修复被删掉了，流水线照样绿」。

真实踩过：2026-10-30 把「记一次口令失败」的实现从 ``api/auth.py`` 搬进
``core/deps.py``（那条规则有三个执行点了，规则只有一份），终审脚本的第 24
项判据还钉在旧位置上，于是**流水线红了**。

修的时候有个陷阱：**只把判据的路径改掉是不够的**。那样「实现被复制回
``api/auth.py``」照样 PASS —— 而那正是要防的事（两份实现迟早对不上）。
所以判据改成了两件事**同时**成立才算过：core 里有行锁机制，**且**
``api/auth.py`` 里不许再有第二份。

而「两件事同时成立」这种写法最容易出的错是**判据其实没有牙** ——
比如正则 `with_for_update` 会匹配到 ``.with_for_update_disabled()``
（子串！）。第一次写反向测试时就是这么翻车的：变异只是把方法**改名**，
脚本照样 PASS，于是「测过了」的结论是假的。

所以这一组用例会**真的把源码改坏**再跑一遍脚本，断言它必须红；
改回去再跑一遍，断言它必须绿。**断言的是脚本的输出里那一条 24 的判定行**，
不是笼统的「退出码非零」—— 沙箱里别的项本来就该红（文件没拷全），
只看退出码会分不清是哪一项在管事。

判据本身**没有放宽**：它现在管的是「core/deps.py 里有行锁机制，
api/auth.py 里没有」—— 这仍然覆盖原意（§3.7 的「失败计数用行锁串行化」），
而且比原来多管了一件事（不许出现第二份实现）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "audit_roadmap_check.py"
AUTH_SRC = REPO_ROOT / "backend" / "app" / "api" / "auth.py"
DEPS_SRC = REPO_ROOT / "backend" / "app" / "core" / "deps.py"

#: 第 24 项在输出里的那一行（``check()`` 打的 ``[PASS] 24.`` / ``[FAIL] 24.``）。
ITEM = "24"


def _fake_repo(tmp_path: Path, auth_src: str, deps_src: str) -> Path:
    """搭一个**只有第 24 项需要的那几个文件**的假仓库。

    脚本的仓库根是从**它自己所在位置**推出来的（``dirname(dirname(__file__))``），
    所以把它放进 ``<root>/tools/`` 就等于把根换成了 ``<root>``。

    别的项会因「文件没拷全」而 FAIL —— **这是预期的**，所以下面断言的是
    第 24 项那一行，不是退出码。
    """
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "tools" / "audit_roadmap_check.py")
    for rel, content in (("backend/app/api/auth.py", auth_src),
                          ("backend/app/core/deps.py", deps_src)):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    # 脚本第 205 行 ``os.listdir(.../app/models)`` —— 这个**目录必须存在**，
    # 否则脚本在第 0 步就崩了，根本走不到第 24 项（崩了的话输出里压根没有
    # 第 24 项那一行，断言会变成「找不到判定行」而不是「判定为红」，
    # 两种失败混在一起会让人看不出是哪个错）。
    (root / "backend" / "app" / "models").mkdir(parents=True, exist_ok=True)
    return root


def _run(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "audit_roadmap_check.py")],
        capture_output=True, text=True, cwd=root,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _item_line(output: str) -> str:
    """取出第 24 项的判定行；没有就是空串。"""
    for line in output.splitlines():
        if line.startswith(f"[PASS] {ITEM}.") or line.startswith(f"[FAIL] {ITEM}."):
            return line
    return ""


# --------------------------------------------------------------------- 用例

def test_终审脚本在真仓库上必须全绿():
    """**闸门本身在当前仓库上是绿的** —— 这一条兜住「改坏了脚本自己」。

    它跑的就是 CI 里那一步（同一个脚本、同样的退出码语义）。
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"终审脚本在真仓库上不是全绿（退出码 {proc.returncode}）：\n"
        + "\n".join(l for l in out.splitlines() if l.startswith("[FAIL]"))
    )
    assert _item_line(out).startswith("[PASS] "), \
        f"第 {ITEM} 项没通过：{_item_line(out)!r}"


def test_行锁机制被删掉时终审必须红(tmp_path):
    """**把 core 里那份行锁机制删掉 ⇒ 第 24 项必须 FAIL。**

    变异是**真删那一行**，不是改个名 —— 正则 ``with_for_update`` 是
    **子串**，把它改成 ``with_for_update_disabled()`` 仍然会被匹配到，
    那种变异骗得过判据（第一版反向测试就是这么翻的车）。
    """
    deps = DEPS_SRC.read_text(encoding="utf-8")
    assert "with_for_update" in deps, \
        "变异前提不成立：core/deps.py 里本来就没有 with_for_update"

    mutated = deps.replace("        .with_for_update()\n", "")
    assert mutated != deps, "变异没生效（那一行没找到）"
    assert "with_for_update" not in mutated, \
        "变异之后源码里还留着 with_for_update 字样 —— 这一版变异没有意义"

    root = _fake_repo(tmp_path, AUTH_SRC.read_text(encoding="utf-8"), mutated)
    code, out = _run(root)
    line = _item_line(out)
    assert line.startswith("[FAIL] "), \
        f"core 里的行锁机制被删了，第 {ITEM} 项居然还 PASS：{line!r}"
    assert code != 0, "退出码还是 0 —— 闸门对外的结论没跟着变"


def test_第二份实现被复制回api时终审必须红(tmp_path):
    """**在 ``api/auth.py`` 里再放一份行锁实现 ⇒ 第 24 项必须 FAIL。**

    这一条守的是「判据只跟着实现走、却不再管『不许有第二份』」那个坑：
    只改路径的话，这一种破坏照样 PASS。
    """
    auth = AUTH_SRC.read_text(encoding="utf-8")
    assert "with_for_update" not in auth, \
        "变异前提不成立：api/auth.py 里本来就有 with_for_update"

    anchor = "    return record_password_failure(db, user)"
    assert anchor in auth, "变异前提不成立：找不到 _record_failure 的薄封装那行"
    mutated = auth.replace(
        anchor,
        "    _probe = select(SysUser).where(SysUser.id == user.id).with_for_update()\n"
        + anchor,
    )
    assert mutated != auth, "变异没生效"
    assert "with_for_update" in mutated, "变异之后源码里没有 with_for_update 了"

    root = _fake_repo(tmp_path, mutated, DEPS_SRC.read_text(encoding="utf-8"))
    code, out = _run(root)
    line = _item_line(out)
    assert line.startswith("[FAIL] "), \
        f"api/auth.py 里多了一份实现，第 {ITEM} 项居然还 PASS：{line!r}"
    assert code != 0, "退出码还是 0 —— 闸门对外的结论没跟着变"


def test_改回去之后必须重新变绿(tmp_path):
    """**两个反向测试之后，原样必须重新通过。**

    没有这一条，前两条可能是因为「脚本整体坏掉」而红的 ——
    那样的红证明不了判据有牙。
    """
    root = _fake_repo(tmp_path,
                      AUTH_SRC.read_text(encoding="utf-8"),
                      DEPS_SRC.read_text(encoding="utf-8"))
    code, out = _run(root)
    line = _item_line(out)
    assert line.startswith("[PASS] "), \
        f"原样的源码第 {ITEM} 项没有通过：{line!r}\n{out[-1500:]}"
    # 别的项在沙箱里该红（文件没拷全），所以**不看退出码**，只看这一行。
