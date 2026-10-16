"""上传入口的容量矩阵（审计 P1-4）。

``tests/test_viewer_matrix.py`` 把 ``.../openings/parse`` 按「只解析不落库」
列进写接口豁免清单 —— 这件事本身是对的：它确实不改数据，只读账号也该能预览
自己账上的期初表。

**但豁免的是写权限，不是容量。** 而期初解析曾经是全项目唯一「一个请求能
把进程内存吃光」的入口，却因为在豁免清单里，守卫测试也照不到它：

    raw = await file.read()                      # 无上限，整体读进内存
    grid = [list(r) for r in ws.iter_rows(...)]  # 整个工作表一次性物化

只读账号就能打。同形态的凭证导入有三道闸（提交 00276b0 加的），这里一道都没有。

所以这里写两条互补的：

1. ``test_每个上传入口都调了共享容量闸`` —— 遍历路由表，把**声明了 UploadFile**
   的接口全找出来，逐个确认它调的是 ``app.core/uploads`` 里的共享闸。
   以后新增上传入口忘了调，这条直接红。它守的是「别忘了加」。
2. ``test_期初解析与凭证导入共用同一套闸`` —— 确认两处真的是**同一个函数**
   （不是两处代码写得一样）。共用必须靠「同一个对象」保证，
   否则三道闸迟早会各自漂移。

单点断言（「期初解析超限返回 413」）只覆盖那一条；矩阵那条才是防线。
"""
from __future__ import annotations

import ast
import inspect

from fastapi.routing import APIRoute

from app.core import uploads


def _all_routes() -> list[APIRoute]:
    """摊平 app 的路由表（``include_router`` 会多包一层，见 viewer_matrix 的同名函数）。"""
    from app.main import app

    out: list[APIRoute] = []
    seen: set[int] = set()
    stack = list(app.routes)
    while stack:
        r = stack.pop()
        if id(r) in seen:
            continue
        seen.add(id(r))
        if isinstance(r, APIRoute):
            out.append(r)
            continue
        for attr in ("routes", "original_router"):
            inner = getattr(r, attr, None)
            inner = getattr(inner, "routes", None) if inner is not None else None
            stack.extend(inner or [])
    return out


def _upload_routes() -> list[APIRoute]:
    """所有接收上传文件的路由（按参数类型认，不靠路径前缀猜）。"""
    out = []
    for r in _all_routes():
        for p in inspect.signature(r.endpoint).parameters.values():
            ann = p.annotation
            # 注解可能是字符串形式（from __future__ import annotations）
            if "UploadFile" in str(ann) or "File" in str(ann):
                out.append(r)
                break
    return out


def test_能找到上传入口_否则这条测试本身就是假绿():
    """前置守卫。认不出 UploadFile 的话下面所有断言都在空集上跑。"""
    paths = sorted(r.path for r in _upload_routes())
    assert paths, "一条上传接口都没认出来 —— 下面的矩阵等于没跑"


def test_每个上传入口都调了共享容量闸():
    missing = []
    for r in _upload_routes():
        src = inspect.getsource(r.endpoint)
        # 认「调用了哪个函数」，不认源码里有没有出现过某个名字 ——
        # 注释里提一句不算数。
        calls = {
            node.func.id for node in ast.walk(ast.parse(inspect.cleandoc(src)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        gated = {"read_upload", "check_archive_size", "RowBudget"} & calls
        if not gated:
            missing.append(f"{sorted(set(r.methods))[0]:6} {r.path}")
    assert not missing, (
        "这些接口接收上传文件却没有调用 app.core.uploads 的容量闸：\n"
        + "\n".join(missing)
        + "\n读字节数（read_upload）、解压体积（check_archive_size）、"
          "行数（RowBudget）三道闸至少要有对应的那些；"
          "新增上传入口请照抄 app/api/vouchers.py 的 upload_import。")


def test_期初解析与凭证导入共用同一套闸():
    """两处是**同一个函数对象**，不是两份长得一样的代码。"""
    from app.accounting import importer

    # 1) 字节数 / 解压体积 / 行数 走的是同一个模块
    assert importer.check_archive_size is uploads.check_archive_size
    assert importer.read_upload is uploads.read_upload

    # 2) 阈值同源：期初解析没有自己的一份上限配置
    from app.core.config import settings
    assert settings.import_max_bytes == settings.import_max_bytes
    assert uploads.unzipped_limit() == settings.import_max_unzipped_bytes


# --------------------------------------------------------------------- 动态


def _opening_xlsx(rows: int) -> bytes:
    """一份「科目编码 / 科目名称 / 方向 / 期初余额」四列的表。"""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["科目编码", "科目名称", "方向", "期初余额"])
    for i in range(rows):
        ws.append(["1002", "银行存款", "借", 100])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _post_openings(client, auth, book, data: bytes):
    return client.post(f"/api/books/{book.id}/openings/parse", headers=auth,
                       files={"file": ("期初.xlsx", data,
                                       "application/vnd.openxmlformats-officedocument."
                                       "spreadsheetml.sheet")})


def test_期初解析_超过字节上限被拒(client, auth, book, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_bytes", 2048, raising=False)
    data = _opening_xlsx(200)
    assert len(data) > 2048, "前置：这份表要超过上限"

    r = _post_openings(client, auth, book, data)
    assert r.status_code == 413, f"超限上传被放行了：{r.status_code} {r.text}"
    assert "上限" in r.json()["detail"]


def test_期初解析_解压后体积超限被拒(client, auth, book, monkeypatch):
    """几百 KB 的 xlsx 解压成几百 MB —— 光看上传字节数挡不住。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_unzipped_bytes", 1024, raising=False)

    r = _post_openings(client, auth, book, _opening_xlsx(20))
    assert r.status_code == 413, f"解压炸弹被放行了：{r.status_code} {r.text}"
    assert "解压后" in r.json()["detail"]


def test_期初解析_超过行数上限被拒(client, auth, book, monkeypatch):
    """这一道是唯一伪造不了的：zip 目录里的声明大小可以随便写，行数不会。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_rows", 10, raising=False)

    r = _post_openings(client, auth, book, _opening_xlsx(40))
    assert r.status_code == 413, f"超行数的表被放行了：{r.status_code} {r.text}"
    assert "行" in r.json()["detail"]


def test_正常大小的期初表不被误伤(client, auth, book):
    r = _post_openings(client, auth, book, _opening_xlsx(3))
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 3


def test_容量闸对只读账号同样生效(client, db, book, plain_user, plain_auth):
    """豁免的是写权限，不是容量。只读账号同样能打爆内存，所以同样要 413。"""
    from app.core.config import settings
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    monkey = settings.import_max_rows
    settings.import_max_rows = 10
    try:
        r = _post_openings(client, plain_auth, book, _opening_xlsx(40))
    finally:
        settings.import_max_rows = monkey
    assert r.status_code == 413, f"只读账号绕过了容量闸：{r.status_code} {r.text}"
