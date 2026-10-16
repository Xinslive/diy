"""Excel 导入的容量边界与载荷留存（审计 P2-02）。

审计指出的两件事：

**一、没有容量上限。** 上传接口是 ``file.file.read()`` —— 无参数，
意味着「有多少读多少」。解析器再把每张凭证、每条分录、每条错误都累积进
列表，最后整包塞进 ``ImportBatch.payload``。Nginx 的 50m 限的是**请求体**，
而 xlsx 是个 zip：一份几百 KB 的文件解压后可以是几百 MB。
于是一次上传就足以吃掉一个进程的内存。

**二、完整载荷长期留存。** 批次确认后只把状态改成 ``COMMITTED``，
作废后只改成 ``DISCARDED``，payload 一直留着 —— 里面是整份工作簿的
科目编码、名称、金额、摘要。列表接口的 ``LIMIT 100`` 只限制返回条数，
不会删除任何东西。

所以这里钉四件事：字节上限、解压体积预检、行数与凭证数上限、
以及「确认/作废之后正文必须消失、摘要必须还在」。
"""
from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from app.accounting import importer
from app.models import ImportBatch

HEADER = ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"]


def _xlsx(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _rows(n_vouchers: int) -> list[list]:
    """n 张配平的凭证，每张两行。"""
    rows = []
    for i in range(n_vouchers):
        day = 1 + (i % 28)
        rows.append([f"2026-01-{day:02d}", f"记-{i}", "收租金", "1002", 100, None])
        rows.append([None, None, "收租金", "5001", None, 100])
    return rows


def _upload(client, auth, book, data: bytes, name="导入.xlsx"):
    return client.post(f"/api/books/{book.id}/vouchers/import", headers=auth,
                       files={"file": (name, data,
                                      "application/vnd.openxmlformats-officedocument."
                                      "spreadsheetml.sheet")})


# --------------------------------------------------------------------- 字节上限


def test_超过字节上限的请求被拒(client, auth, book, monkeypatch):
    """不能无限制 read()：文件多大就读多少进内存，然后交给解析器。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_bytes", 2048, raising=False)
    data = _xlsx(HEADER, _rows(30))
    assert len(data) > 2048, "前置：这份表要超过上限"

    r = _upload(client, auth, book, data)
    assert r.status_code == 413, f"超限请求被放行了：{r.status_code} {r.text}"
    assert "上限" in r.json()["detail"]


def test_超限被拒时不会留下暂存批次(client, db, auth, book, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_bytes", 2048, raising=False)
    _upload(client, auth, book, _xlsx(HEADER, _rows(30)))

    assert db.scalars(select(ImportBatch).where(ImportBatch.book_id == book.id)).all() == []


def test_正常大小的表不该被误伤(client, auth, book):
    r = _upload(client, auth, book, _xlsx(HEADER, _rows(3)))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3


# --------------------------------------------------------------------- 解压体积


def test_解压后体积超限被拒(db, book, monkeypatch):
    """按 zip 目录里声明的大小预检，不真正解压。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_unzipped_bytes", 1024, raising=False)
    data = _xlsx(HEADER, _rows(5))

    with pytest.raises(ValueError, match="解压后"):
        importer.parse_workbook(data, book.id, db)


def test_不是zip的文件跳过解压预检():
    """预检只对 zip 生效：不是 zip 的文件原样放行，
    由解析器自己报「无法读取 Excel 文件」。"""
    assert importer.check_archive_size(b"not-a-zip-at-all") is None


def test_不是zip的文件在解析时报原有错误(db, book):
    """不能因为加了一道容量检查就把原有的报错口径改了。"""
    with pytest.raises(ValueError, match="无法读取 Excel 文件"):
        importer.parse_workbook(b"not-a-zlsx-file", book.id, db)


# --------------------------------------------------------------------- 行数 / 凭证数


def test_超过行数上限被拒(client, auth, book, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_rows", 20, raising=False)
    data = _xlsx(HEADER, _rows(30))   # 60 行数据 + 表头

    r = _upload(client, auth, book, data)
    # 413：三道容量闸统一用 413（审计 P1-4 把闸抽成共享工具之后）。
    # 原来只有「读取字节数」那一道是 413，行数与凭证数是 400 —— 同一个含义
    # （这份文件太大了，换个小的再来）却有两个状态码，前端只能按字节那道特判。
    assert r.status_code == 413, f"超行数的表被放行了：{r.status_code} {r.text}"
    assert "行" in r.json()["detail"]


def test_超过凭证数上限被拒(db, book, monkeypatch):
    """行数合规但凭证数失控时，逐行计数这道兜底必须生效 ——
    一张凭证几十行分录是很正常的放大倍率。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_vouchers", 5, raising=False)
    monkeypatch.setattr(settings, "import_max_rows", 100_000, raising=False)
    data = _xlsx(HEADER, _rows(10))

    with pytest.raises(ValueError, match="凭证"):
        importer.parse_workbook(data, book.id, db)


def test_解析中止后不留下暂存批次(client, db, auth, book, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "import_max_rows", 20, raising=False)
    _upload(client, auth, book, _xlsx(HEADER, _rows(30)))

    assert db.scalars(select(ImportBatch).where(ImportBatch.book_id == book.id)).all() == []


# --------------------------------------------------------------------- 载荷留存


def _pending_batch(client, auth, book):
    r = _upload(client, auth, book, _xlsx(HEADER, _rows(2)))
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def test_确认入库后payload正文被清空(client, db, auth, book):
    """凭证已经在 voucher/entry 表里了，暂存表不必再留一份完整的。"""
    batch_id = _pending_batch(client, auth, book)
    r = client.post(f"/api/books/{book.id}/vouchers/import/{batch_id}/commit", headers=auth)
    assert r.status_code == 200, r.text

    db.expire_all()
    batch = db.get(ImportBatch, batch_id)
    assert batch.payload.get("vouchers") in (None, []), "凭证正文还留在暂存表里"
    assert batch.payload.get("cleared") is True


def test_确认后仍能查清这批导了多少张(client, db, auth, book):
    """清空不能连复盘能力一起清掉。"""
    batch_id = _pending_batch(client, auth, book)
    client.post(f"/api/books/{book.id}/vouchers/import/{batch_id}/commit", headers=auth)

    db.expire_all()
    batch = db.get(ImportBatch, batch_id)
    assert batch.payload.get("total") == 2
    assert batch.payload.get("ok") == 2
    assert batch.committed_count == 2, "批次元数据（committed_count）不能丢"


def test_作废后payload正文也被清空(client, db, auth, book):
    """作废最该清：没人要这批数据了，但界面上看不见、库里一直在。"""
    batch_id = _pending_batch(client, auth, book)
    r = client.post(f"/api/books/{book.id}/vouchers/import/{batch_id}/discard", headers=auth)
    assert r.status_code == 200, r.text

    db.expire_all()
    batch = db.get(ImportBatch, batch_id)
    assert batch.payload.get("vouchers") in (None, [])
    assert batch.status == "DISCARDED"


def test_摘要里保留校验错误(client, db, auth, book):
    """错误摘要要留着 —— 「哪一行错了」比「原始行内容」重要得多。"""
    data = _xlsx(HEADER, [
        ["2026-01-05", "记-1", "借不平", "1002", 100, None],
        [None, None, "借不平", "5001", None, 90],
    ])
    r = _upload(client, auth, book, data)
    batch_id = r.json()["batch_id"]
    client.post(f"/api/books/{book.id}/vouchers/import/{batch_id}/discard", headers=auth)

    db.expire_all()
    batch = db.get(ImportBatch, batch_id)
    errors = batch.payload.get("errors") or []
    assert errors and errors[0]["line"] == 2, f"错误摘要没留下：{batch.payload}"


# --------------------------------------------------------------------- 过期清理


def test_过期的未确认批次会被清掉(client, db, auth, book):
    """传完就忘了点确认的批次会永远停在 PENDING、payload 永远完整。"""
    from datetime import datetime, timedelta, timezone

    batch_id = _pending_batch(client, auth, book)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    db.execute(
        ImportBatch.__table__.update().where(ImportBatch.id == batch_id)
        .values(created_at=old)
    )
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(ImportBatch, batch_id) is None, "过期的未确认批次没被清理"


def test_没过期的批次不该被清(client, db, auth, book):
    batch_id = _pending_batch(client, auth, book)
    client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)

    db.expire_all()
    assert db.get(ImportBatch, batch_id) is not None


def test_已入库的批次元数据要留着(client, db, auth, book):
    """「谁在什么时候导了多少张」是有价值的追溯记录，
    而它的 payload 已经清空了，留着不占什么空间。"""
    from datetime import datetime, timedelta, timezone

    batch_id = _pending_batch(client, auth, book)
    client.post(f"/api/books/{book.id}/vouchers/import/{batch_id}/commit", headers=auth)
    db.execute(
        ImportBatch.__table__.update().where(ImportBatch.id == batch_id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=30))
    )
    db.commit()

    client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)
    db.expire_all()
    assert db.get(ImportBatch, batch_id) is not None, "已入库批次的元数据被一起清掉了"


def test_清理不影响别的账套(client, db, auth, admin_user, book):
    import uuid

    from app.accounting.periods import create_periods
    from app.models import Book, UserBook

    from datetime import datetime, timedelta, timezone

    tag = uuid.uuid4().hex[:8]
    other = Book(company_name=f"别家公司{tag}", name=f"别家{tag}",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()

    b = ImportBatch(book_id=other.id, filename="别家的.xlsx", status="PENDING",
                    total=0, ok_count=0, error_count=0, committed_count=0,
                    payload={"vouchers": [{"x": 1}]}, message="", created_by="admin",
                    created_at=datetime.now(timezone.utc) - timedelta(days=30))
    db.add(b)
    db.commit()

    r = client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(ImportBatch, b.id) is not None, "清理越权到了别的账套"


# --------------------------------------------------------------------- 只读 GET 的副作用
# 审计 P2-22：``GET /vouchers/import`` 是个**只读**接口，却会 DELETE + COMMIT
# （``purge_stale_batches``）。原来清理排在响应构造之前：序列化批次一旦抛异常，
# 用户拿到 500，而那一批过期批次**已经被删掉了** —— 一个失败的请求留下了
# 它不该有的副作用，而且没有任何记录说明删了什么。


def test_清理失败不能把导入历史这一页搞成500(client, auth, book, monkeypatch):
    """清理失败不该让「看导入历史」整页报错。

    过期批次留在库里，下次进来还会再清一次 —— 那是可重试的状态；
    而 500 不可重试，用户只会以为系统坏了。

    只接**数据库类**失败（``SQLAlchemyError``）：代码本身的 bug 仍然要 500，
    不能因为「反正只是清理」就把真正的错误一起吞掉。
    """
    from sqlalchemy.exc import OperationalError

    from app.accounting import importer

    def boom(*a, **kw):
        raise OperationalError("DELETE ...", {}, Exception("数据库正好在忙"))

    monkeypatch.setattr(importer, "purge_stale_batches", boom)
    r = client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_响应先算好_再清理():
    """顺序就是这条要求的全部：序列化抛异常时，清理还没开始。

    用桩会话把「序列化必然失败」这件事做成确定事件 ——
    靠真实数据去撞一个偶发异常，测试就变成碰运气。
    """
    from app.api import vouchers as api
    from app.models import ImportBatch

    calls: list[str] = []

    class _Rows:
        def all(self):
            # created_at 为 None → 序列化时 .isoformat() 抛 AttributeError
            return [ImportBatch(id=1, filename="a.xlsx", status="PENDING", total=0,
                                ok_count=0, error_count=0, committed_count=0,
                                message="", created_by="admin", created_at=None)]

    class _Boom:
        def scalars(self, *a, **kw):
            return _Rows()

    original = api.svc_import.purge_stale_batches
    api.svc_import.purge_stale_batches = lambda *a, **kw: calls.append("purge")
    try:
        with pytest.raises(AttributeError):
            api.list_imports(book=object(), db=_Boom())
    finally:
        api.svc_import.purge_stale_batches = original

    assert calls == [], f"序列化还没成功就先把批次删了：{calls}"


def test_正常路径上清理仍然会被触发(client, auth, book, monkeypatch):
    """把清理拿掉等于没人负责清过期批次 —— 顺序调整不能顺手把它删了。"""
    from app.accounting import importer

    calls: list[str] = []
    original = importer.purge_stale_batches
    importer.purge_stale_batches = lambda *a, **kw: calls.append("purge")
    try:
        r = client.get(f"/api/books/{book.id}/vouchers/import", headers=auth)
    finally:
        importer.purge_stale_batches = original

    assert r.status_code == 200, r.text
    assert calls == ["purge"], "导入历史页不再触发过期清理了"
