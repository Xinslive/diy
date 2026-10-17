"""把一个账套的数据导成一个压缩的、自带说明的快照文件。

产物格式（**先压缩，后上传** —— 压缩在这里做，因为压缩后的大小就是 OSS 上
真正占的钱，而分片上传前的落盘也要按压缩后算）：

    {format, version, scope, book_id, book_label, exported_at, tables:{表名:{columns,rows}}}

行是**行式字典**而不是列式数组：备份最怕的是「恢复时看不懂」。列式紧凑几个
百分点，但排障时面对 `[[1,"1001",...]]` 没人能确认第 5 列是金额还是余额 ——
而恢复路径本来就是「出事那天才第一次跑」的那段代码。gzip 已经把体积压下来了。

**为什么逐行流式写，而不是先攒成一个大 dict 再 dumps。** 账套备份的大小由凭证
量决定：几万张凭证的账套导出成 dict 就是几百 MB 的常驻内存，而备份这件事
本来就该在业务低峰跑、不该把 API 进程挤到 OOM。这里按批取（``yield_per``）
边取边写，内存与账套大小无关。

**读到的数据必须来自同一个时间点。** 导出要连着读十几张表，而每一张都可能
是几秒钟；按默认的 READ COMMITTED，凭证表读完之后科目表再读一次，这中间
别人录了一张凭证 —— 出来的快照是一个**从未存在过的状态**：分录在、凭证不在。
拼不出来的备份比没有更糟，因为它看起来是完整的。所以这里显式把事务提到
REPEATABLE READ（见 :func:`export_scope`）。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import SCOPE_BOOK, SCOPE_SYSTEM
from app.models.base import Base

FORMAT = "cwq-book-backup"
VERSION = 1

# 不进备份的表。
#
# **``sys_backup_run`` 是唯一的一个，而且它非进不可不可。**
# 它自己带 book_id，所以「所有带 book_id 的表」这条规则会把它一并捞进来 ——
# 于是这一期的备份里装着上一期的备份记录，上上期的还装着…，体积每一期都比上一期
# 大，而且没有任何东西会报错。这正是「递归的日志表」这类陷阱：它只会慢慢胀，
# 不会坏，你要在两年之后才发现 OSS 账单不对。
EXCLUDED_TABLES = frozenset({"sys_backup_run"})

# 每批取多少行就往 gzip 里写多少。1 千行是个平衡点：再大则一次 flush 的
# 内存峰值高，再小则 flush 次数多、gzip 窗口利用率低。
BATCH = 1000


class _HashingWriter:
    """转发写入，同时喂给 SHA256。

    **为什么是个类而不是个闭包。** 它一开始写成 ``class _Tee`` 定义在
    ``with`` 里面、闭包引用外层的 ``fh`` —— 而那个 ``fh`` 正是后面
    ``with gzip.GzipFile(...) as fh`` 要绑定的名字，于是构造它的那一刻
    ``fh`` 还不存在，导出第一行就 ``NameError``。一个只在真正导出时才炸的
    闭包捕食错误。类不闭包任何东西。
    """

    def __init__(self, target, digest):
        self._target = target
        self._digest = digest

    def write(self, b):
        self._digest.update(b)
        return self._target.write(b)

    def flush(self):
        self._target.flush()


@dataclass
class ExportStats:
    path: Path
    rows: int = 0
    raw_bytes: int = 0          # 压缩前
    size_bytes: int = 0         # 压缩后（= OSS 上占的字节）
    checksum: str = ""
    tables: dict[str, int] = field(default_factory=dict)


class ExportRefused(Exception):
    """拒绝产出这份备份。消息必须说清「为什么不备份」与「下一步」。"""


# ------------------------------------------------------------ 哪些表进哪份备份


@dataclass(frozen=True)
class TableScope:
    """一张表「属于哪个账套」的方式。"""
    table: Any
    # ``book``      —— 这张表就是账套本身（``book`` 表）
    # ``direct``    —— 自己带 ``book_id`` 列
    # ``indirect``  —— 自己没有 ``book_id``，但外键全部指向已判定为账套内的表
    # ``global``    —— 全系统一份（用户、参数、登录节流）
    kind: str
    # indirect 时：指向父表的那一列在本表里的名字
    fk_local: str | None = None
    fk_target: Any = None


def build_table_scopes() -> dict[str, TableScope]:
    """算出每张表归属账套的方式。**结果依赖 metadata，故按调用时算一次并缓存。**

    为什么不能只看「有没有 book_id 列」
    ---------------------------------
    报表模板的**表体**分在四张表上，其中三张 —— ``report_row`` / ``report_col`` /
    ``report_cell`` —— **自己没有 book_id**，只有 ``template_id`` 指向
    ``report_template``。只按 book_id 筛，这三张表在任何账套的备份里都是空的：
    备份照样成功、照样压缩、照样上传，界面上照样显示「已备份 12,345 行」，
    而用户精心设计的那套报表版式**从来就不在任何一份备份里**。

    这正是「漏一张表」最坏的形态：它不报错，只是安静地少备份了一类数据。

    所以规则是**沿外键递归到闭包**：一张表只要它所有的外键都指向「已在账套内」
    的表，它就在账套内。到不动点为止。

    「所有外键」而不是「任一外键」是刻意的反向约束 —— 任一的话，一张同时指向
    ``sys_user`` 与 ``book`` 的表会被算进账套，而它的用户数据就会跟着每本账备份
    一遍（冗余）。全指向才收，是宁可漏判（漏判的那张表仍会被系统级备份的
    ``book_id IS NULL`` 分支之外的情况漏掉 —— 所以下面那条自检测试是必须的）。
    """
    tables = [t for t in Base.metadata.sorted_tables if t.name not in EXCLUDED_TABLES]
    scopes: dict[str, TableScope] = {}

    for t in tables:
        if t.name == "book":
            scopes[t.name] = TableScope(t, "book")
        elif "book_id" in t.c:
            scopes[t.name] = TableScope(t, "direct")

    # 逐轮扩张，直到没有新表进来。
    changed = True
    while changed:
        changed = False
        for t in tables:
            if t.name in scopes:
                continue
            # 只要有**单列**外键就能走；复合外键（entry 的 (book_id, period_ord)
            # 指向 period）所在的那张表本来就自带 book_id，走的是 direct 分支。
            targets = []
            for fk in t.foreign_keys:
                if len(fk.constraint.columns) != 1:
                    continue
                target = fk.column.table
                if target.name in scopes and scopes[target.name].kind in (
                        "book", "direct", "indirect"):
                    targets.append((fk.parent.name, target))
            fk_list = list(t.foreign_keys)
            if fk_list and len(targets) == len(fk_list):
                scopes[t.name] = TableScope(t, "indirect",
                                            fk_local=targets[0][0],
                                            fk_target=targets[0][1])
                changed = True

    for t in tables:
        scopes.setdefault(t.name, TableScope(t, "global"))
    return scopes


_TABLE_SCOPES: dict[str, TableScope] = {}


def table_scopes() -> dict[str, TableScope]:
    global _TABLE_SCOPES
    if not _TABLE_SCOPES:
        _TABLE_SCOPES = build_table_scopes()
    return _TABLE_SCOPES


def tables_for(scope: str) -> list[TableScope]:
    """某一份备份要导哪些表（保持 FK 拓扑序，恢复时照这个顺序插）。"""
    out = []
    for t in Base.metadata.sorted_tables:
        ts = table_scopes().get(t.name)
        if ts is None:
            continue
        if scope == SCOPE_BOOK and ts.kind == "global":
            continue        # 用户/参数/登录节流不是账套的资产
        out.append(ts)
    return out


def _predicate(ts: TableScope, scope: str, book_id: Optional[int]):
    """这一行属不属于「这份备份的范围」。返回 None 表示全表。"""
    if ts.kind == "book":
        return ts.table.c.id == book_id if scope == SCOPE_BOOK else None
    if ts.kind == "global":
        return None
    if ts.kind == "direct":
        col = ts.table.c.book_id
        return col.is_(None) if scope == SCOPE_SYSTEM else col == book_id
    # indirect：父表在本周期范围内的那些行的子集
    parent = ts.fk_target
    parent_scope = table_scopes()[parent.name]
    if parent_scope.kind == "book":
        inner = parent.c.id == book_id
    elif parent_scope.kind == "direct":
        pcol = parent.c.book_id
        inner = pcol.is_(None) if scope == SCOPE_SYSTEM else pcol == book_id
    else:
        # 祖父也要再往上推一层（目前 schema 里没用到，留着免得将来一改就静默漏）。
        grand = _predicate(TableScope(parent, parent_scope.kind,
                                      parent_scope.fk_local, parent_scope.fk_target),
                           scope, book_id)
        if grand is None:
            return None
        inner = parent.c.id.in_(select(parent.c.id).where(grand))
    pk = list(parent.primary_key.columns)
    if len(pk) != 1:
        raise ExportRefused(f"表「{parent.name}」是复合主键，账套级导出暂不支持。")
    return ts.table.c[ts.fk_local].in_(select(pk[0]).where(inner))


def rows_of(conn, ts: TableScope, scope: str,
            book_id: Optional[int]) -> Iterator[dict]:
    """按批取一行行编码后的记录。"""
    stmt = select(*ts.table.c)
    where = _predicate(ts, scope, book_id)
    if where is not None:
        stmt = stmt.where(where)
    for row in conn.execute(stmt.execution_options(yield_per=BATCH)):
        yield {col.name: encode(row._mapping[col.name]) for col in ts.table.c}


# ------------------------------------------------------------ 值编码


def encode(value: Any) -> Any:
    """Python 值 → 可安全进 JSON 的形态（**无损**）。

    金额必须走文本，这是本仓库的一贯口径（``audit_helper._jsonable`` 的理由）：
    ``Numeric(20,2)`` 的上界 999999999999999999.99 有 20 位有效数字，
    float 只有 15~17 位 —— 一旦越过千万级，转成 float 就已经不是原来那个数了。
    备份里的金额变成一个「差一点点」的数字，比没有备份更难发现。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return str(value)


def _coerce(value: Any, coltype: Any) -> Any:
    """按**目标列的类型**把归档里的值变回 Python 值。

    **用目标库的类型，不把类型写进归档。** 归档里存一份「这列当年是 Numeric(20,2)」
    看着更完备，实则更脆：目标库已经迁移到 Numeric(24,4) 时，照归档的旧类型构造
    会让恢复失败在一个**与数据无关**的地方。目标库的 schema 才是恢复现场的事实。
    """
    import sqlalchemy as sa

    if value is None:
        return None
    try:
        if isinstance(coltype, sa.Numeric):
            return Decimal(str(value))
        if isinstance(coltype, sa.Boolean):
            return bool(value)
        if isinstance(coltype, sa.Integer):
            return int(value)
        if isinstance(coltype, sa.Float):
            return float(value)
        if isinstance(coltype, sa.DateTime):
            return datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(coltype, sa.Date):
            return date.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError, ArithmeticError):
        # 宁可原样交给 psycopg 去报错，也不要在这里猜一个值塞进去 ——
        # 一个被静默转成 0.00 的金额，是审计级的损失。
        return value
    # JSON / JSONB 列：值已经是 encode() 产出的可 JSON 形态（JSONB 由 psycopg
    # 自动再序列化一次），原样返回即可。
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "__b64__" in value:
        return base64.b64decode(value["__b64__"])
    return value


def export_scope(db: Session, *, scope: str, book_id: Optional[int],
                 book_label: str, out_path: Path,
                 now: Optional[datetime] = None) -> ExportStats:
    """导出一份快照并压缩落盘。

    **整个导出在一条 REPEATABLE READ 的连接里完成** —— 见下面那段注释，
    这是本函数唯一不能省的东西。``db`` 只用来取已经 commit 的数据，
    读走的是独立连接（也正因如此，它读不到 ``db`` 里未提交的改动）。

    ``scope``/``book_id`` 决定取哪些行；``book_label`` 只是写进归档头给人看的。
    """
    if scope == SCOPE_BOOK and book_id is None:
        raise ExportRefused("账套级备份必须给出 book_id。")

    tables = tables_for(scope)
    if not tables:
        raise ExportRefused("反射里一张表都没找到，这不像一个正常的数据库；"
                            "宁可什么都不备份，也不要上传一份空归档。")

    stats = ExportStats(path=out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # **导出走自己的连接，不复用调用方那个 Session。**
    #
    # 两个原因，第二个是要命的：
    #   1. 复用就得在别人可能已经开着的事务上改隔离级别。Session 早就为别的事
    #      连上了库，此时 ``execution_options(isolation_level=...)`` 会被**静默忽略**
    #      （SQLAlchemy 只发一条 SAWarning）—— 而那正是这里唯一不能失效的东西。
    #   2. 拿到一个**真正**的 REPEATABLE READ 快照，跨十几张表读出来的是同一个
    #      时间点的数据。默认的 READ COMMITTED 下，凭证表读完再读科目表，这中间
    #      别人录了一张凭证 —— 出来的是一份**从未存在过**的状态：分录在、凭证不在。
    #      它格式正确、能解开、能上传，看起来完全正常，而恢复它会得到一个插不进
    #      外键的半吊子库。拼不出来的备份比没有备份更糟。
    #
    # 代价是导出的读看不到调用方 Session 里**未提交**的改动 —— 而备份本来就该
    # 以「已落库的数据」为准，那正是出事时要拿回来的东西。
    from app.core.db import engine

    with engine.connect() as conn:
        with conn.begin():
            # 用 SET TRANSACTION 而不是 SQLAlchemy 的
            # ``execution_options(isolation_level=...)``：后者要改 autocommit，
            # 而连接此时已经 INTRANS，psycopg 直接抛
            # 「can't change 'autocommit' now」。SET TRANSACTION 只要是事务里的
            # 第一条语句就合法，而这里它确实是。
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            with open(out_path, "wb") as raw_fh:
                digest = hashlib.sha256()
                # 校验和对的是**压缩后**写进文件的那串字节 —— 那才是 OSS 上存着的
                # 东西，将来比对远端对象才有意义。中间这层 sink 就是为了拿到它。
                sink = _HashingWriter(raw_fh, digest)
                with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as gz:
                    # mtime=0：gzip 头默认写当前时间，那会让同一份数据两次导出得到
                    # 两个不同文件、SHA256 也就不一样 —— 于是这个校验和没法用来
                    # 判断两份备份内容是否相同。固定 0 之后它是内容寻址的。
                    header = {
                        "format": FORMAT, "version": VERSION, "scope": scope,
                        "book_id": book_id, "book_label": book_label,
                        "exported_at": (now or datetime.now()).isoformat(),
                    }
                    gz.write(b'{"header":'
                             + json.dumps(header, ensure_ascii=False).encode("utf-8")
                             + b',"tables":{')
                    first = True
                    for ts in tables:
                        table = ts.table
                        gz.write((b"" if first else b",")
                                 + json.dumps(table.name).encode("utf-8")
                                 + b':{"columns":'
                                 + json.dumps([c.name for c in table.c]).encode("utf-8")
                                 + b',"rows":[')
                        first = False
                        n = 0
                        for i, record in enumerate(rows_of(conn, ts, scope, book_id)):
                            if i:
                                gz.write(b",")
                            gz.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                            n += 1
                        gz.write(b"]}")
                        stats.tables[table.name] = n
                        stats.rows += n
                    gz.write(b"}}")

    stats.checksum = digest.hexdigest()
    stats.size_bytes = out_path.stat().st_size
    if stats.size_bytes == 0:
        raise ExportRefused(f"导出结果是 0 字节，已丢弃：{out_path}")
    return stats


def export_book(db: Session, book, out_path: Path,
                now: Optional[datetime] = None) -> ExportStats:
    label = f"{book.company_name} / {book.name}"
    return export_scope(db, scope=SCOPE_BOOK, book_id=book.id, book_label=label,
                        out_path=out_path, now=now)


def export_system(db: Session, out_path: Path,
                  now: Optional[datetime] = None) -> ExportStats:
    return export_scope(db, scope=SCOPE_SYSTEM, book_id=None,
                        book_label="系统数据（用户、授权、参数、全局基线）",
                        out_path=out_path, now=now)


# ------------------------------------------------------------ 恢复（退路本身）


def _ordered_rows(table, rows: list[dict]) -> list[dict]:
    """把同一张表的行排成「自引用父在前」。

    **为什么需要它。** 科目表自己指向自己（``subject.parent_id`` → ``subject.id``）。
    而 PostgreSQL 的外键检查是**逐行**做的：一条多行 INSERT 里，后面的行引用的
    父行如果还没插进去，当场就报 ``subject_parent_id_fkey`` 违反。

    表级拓扑排序救不了这个 —— 父子是**同一张表**，排序器看到的是自己对自己，
    只能原地不动。于是恢复一个三级科目表会在第二级就炸掉，而错误信息里只有
    一个外键名，完全看不出「其实是行顺序错了」。

    做法是对表内自引用的行做一次拓扑排序（Kahn）。引用了**不在本批里**的行
    （比如恢复一本账到已经有别的账的库里）不算阻塞 —— 那个父行已经在库里了。
    """
    pk = list(table.primary_key.columns)
    self_fks = [(fk.parent, fk.column) for fk in table.foreign_keys
                if fk.column.table is table and len(fk.constraint.columns) == 1]
    if len(pk) != 1 or not self_fks or len(rows) < 2:
        return rows

    key = pk[0].name
    by_id: dict = {}
    for i, r in enumerate(rows):
        if r.get(key) is not None:
            by_id[r[key]] = i

    indeg = [0] * len(rows)
    dependents: list[list[int]] = [[] for _ in rows]
    for i, r in enumerate(rows):
        for local, _target in self_fks:
            v = r.get(local.name)
            if v is not None and v in by_id and by_id[v] != i:
                indeg[i] += 1
                dependents[by_id[v]].append(i)

    ready = [i for i in range(len(rows)) if indeg[i] == 0]
    order: list[int] = []
    while ready:
        i = ready.pop()
        order.append(i)
        for d in dependents[i]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    if len(order) != len(rows):
        # 有环（自引用的环不该存在，但科目树要是真有了环，恢复不该静默少插几行）。
        seen = set(order)
        order.extend(i for i in range(len(rows)) if i not in seen)
    return [rows[i] for i in order]


def read_snapshot(path: Path) -> dict:
    """解开一份快照，先验后跑。

    和 ``db_backup.py::assert_restorable`` 同一个立场：恢复要执行的是归档里的
    内容，最坏的情况不是恢复失败，而是**恢复「成功」了**。所以解不开、版本不认识、
    根本不是这个格式的，一律拒绝，不给「尽力而为」留路。
    """
    try:
        with gzip.open(path, "rb") as fh:
            data = json.load(fh)
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        raise ExportRefused(f"归档解不开（{path.name}）：{exc}。它可能没传完，或已被截断。") from exc
    # 格式与版本都在 **header** 里（导出时是这么写的：{"header":{...},"tables":{...}}）。
    # 写成去顶层找就是「导出成功、恢复永远拒绝」—— 而且每一侧单看都成立，
    # 只有真跑一次往返才会暴露。
    header = data.get("header") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(header, dict) \
            or header.get("format") != FORMAT:
        got = header.get("format") if isinstance(header, dict) else "?"
        raise ExportRefused(f"{path.name} 不是本系统导出的备份（format={got}）。")
    if header.get("version") != VERSION:
        raise ExportRefused(f"{path.name} 的格式版本是 {header.get('version')}，"
                            f"本程序只认 {VERSION}。别用旧版本程序硬灌。")
    if not isinstance(data.get("tables"), dict) or not data["tables"]:
        raise ExportRefused(f"{path.name} 里一张表都没有，拒绝恢复。")
    return data


def restore(db: Session, path: Path, *, dry_run: bool = False) -> dict[str, int]:
    """把一份快照灌回数据库。**全有或全无**。

    先验后跑、整批走同一个事务：任何一张表失败就整体回滚。恢复出一半的库比
    不恢复更难收拾 —— 它既不是出事前的版本，也不是备份里的版本，而是一个
    两者都不是、外表却完全正常的东西。

    顺序按 ``metadata.sorted_tables``（父表在前），所以不会撞外键。
    最后重置序列：带显式 id 插进去之后，序列还停在旧位置，于是恢复完成后
    **第一张新凭证就会撞主键** —— 一个只在恢复之后才发作的故障。
    """
    data = read_snapshot(path)
    tables = data["tables"]
    counts: dict[str, int] = {}

    # 预检：列对不上就先说清楚，别插到一半才发现。
    known = {t.name: t for t in Base.metadata.tables.values()
             if t.name not in EXCLUDED_TABLES}
    for name, payload in tables.items():
        table = known.get(name)
        if table is None:
            raise ExportRefused(
                f"归档里有表「{name}」，而当前数据库没有这张表。"
                "多半是备份来自更新的版本；先升级程序，再恢复。")
        missing = [c for c in payload.get("columns", []) if c not in table.c]
        if missing:
            raise ExportRefused(
                f"表「{name}」在归档里有而当前库里没有的列：{'、'.join(missing)}。")

    if dry_run:
        for name, payload in tables.items():
            counts[name] = len(payload.get("rows", []))
        return counts

    for table in Base.metadata.sorted_tables:
        payload = tables.get(table.name)
        if payload is None:
            continue
        rows = payload.get("rows", [])
        if not rows:
            counts[table.name] = 0
            continue
        cols = [c.name for c in table.c]
        payloads = []
        for record in rows:
            values = {k: _coerce(_decode(v), table.c[k].type)
                      for k, v in record.items() if k in table.c}
            # 归档里有、当前库里也有的列才插；缺的那些列留给默认值。
            payloads.append({c: values[c] for c in cols if c in values})
        db.execute(table.insert(), _ordered_rows(table, payloads))
        counts[table.name] = len(payloads)
        _reset_sequence(db, table)

    return counts


def _reset_sequence(db: Session, table) -> None:
    """把自增序列推到已插入的最大 id 之后。

    **不做这一步，恢复出来的库是废的。** 带显式 id 插进去之后，序列仍停在
    出事前那个值，于是恢复后录入的**第一张凭证**就撞主键 —— 一个当场看不见、
    要等到有人真的开始记账才炸的故障。

    表名/列名一律通过**绑定参数**传，不拼进 SQL 字符串 —— 拼的话，一个带引号的
    表名就能改写这条语句，而这里处理的表名来自 metadata、引号来自将来可能更
    换的方言，不是可以随便假设的 ASCII。

    没有序列的表直接跳过：``pg_get_serial_sequence`` 返回 NULL。
    """
    pk = list(table.primary_key.columns)
    if len(pk) != 1:
        return
    ident = pk[0]
    seq = db.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": ident.name})
    if not seq:
        return
    biggest = db.scalar(select(func.coalesce(func.max(ident), 0)))
    db.execute(text("SELECT setval(:seq, :val, false)"),
               {"seq": seq, "val": int(biggest) + 1})
