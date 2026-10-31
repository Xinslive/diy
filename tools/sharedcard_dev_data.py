#!/usr/bin/env python3
"""把生产上「共享卡对账」插件用到的账套数据搬进本机 dev 库。

为什么要有这个脚本
------------------
本地调试共享卡插件需要**真实的数据规模** —— 生产的 859 行流水、
7 个成员账套、16845 条分录。手抄几行样例看不出「读次数随账套数涨」
这类问题，而这类问题**只在真实规模上才显形**。

分两步走（生产库只读）
----------------------
生产 PostgreSQL 只认本机连接（pg_hba 拒了外网），所以不走「本机直连」，
而是：

1. ``sql`` —— 在**生产机上**用 psql 跑，把要搬的表 ``COPY TO`` 成 CSV。
   这是**只读**的（一条写语句都没有，写的是文件不是表）。
2. ``import`` —— 在本机把 CSV 读进 dev 库（客户端 COPY，整批一个事务）。

用法::

    # ① 在生产机上生成导出脚本并执行（写 /tmp，不碰应用目录）
    python3 tools/sharedcard_dev_data.py sql > /tmp/sc_export.sql
    scp /tmp/sc_export.sql root@43.138.178.174:/tmp/
    ssh root@43.138.178.174 'mkdir -p /tmp/sc_export && su - postgres -c "psql -d cwq -f /tmp/sc_export.sql"'
    scp -r root@43.138.178.174:/tmp/sc_export /tmp/

    # ② 在本机导进 dev 库
    cd backend
    ../tools/sharedcard_dev_data.py import --from /tmp/sc_export

id 怎么处理
-----------
本机 dev 库已经有自己的账套数据，**id 区间会撞**：生产的科目 id
3107..21341 与本地的 3234..3527 重叠；期间 id 77..648 与本地 301..324
也重叠。撞上的那几张表**在导出时就整体平移一个固定偏移**，指向它们的
外键一起平移：

======================  ==========================  ======================
表                      平移                        外键
======================  ==========================  ======================
``subject``             ``id``                     ``parent_id``
``period``              ``id``                     ``voucher.period_id``
                                               ``period_opening.period_id``
``book_year``           ``id``                     （没有外键指向它）
``period_opening``      （不撞）                   ``subject_id``
``voucher``             （不撞）                   ``period_id``
``entry``               （不撞）                   ``subject_id``
======================  ==========================  ======================

**账套 id 不平移**：生产的账套 id（12..17、24）与本机的（18）不重叠，
原样搬过来 —— 插件表里那些 ``book_id`` 也就跟着对上了。

**分录 id 不平移**：生产的分录 id 最大 39307，本机最小 63904，不重叠。
这一条很要紧：核对记录里存着**真实分录 id**（核对组快照），分录 id 一旦
平移，那些核对组立刻全部变成「来源已变化」。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg

#: 插件**用到的**账套：成员表里那 7 本（13..17、24 加上存原件的 12）。
BOOKS = [12, 13, 14, 15, 16, 17, 24]

#: 冲突时整体平移的偏移。取一个「本机现有 id 永远够不到」的数：
#: 本机最大的 id 是 64729，一百万足够远，又不会溢出 int32。
OFFSET = 1_000_000

#: CSV 里代表 NULL 的标记。CSV 本身分不清「NULL」与「空字符串」，
#: 不标一下，导入时会把空字符串当成 NULL —— 那是**改数据**，不是搬数据。
NULL_MARKER = "__NULL__"

#: 期间表里「结账」那三列 —— 导入时要**先当没结账**，装完凭证再恢复。
#:
#: 为什么：生产上这些凭证是在**结账之前**写进去的（凭证表上有 BEFORE INSERT
#: 的守卫，``fn_voucher_guard``：期间已结账就不许新增凭证）。本地导入是
#: 「把历史搬过来」，搬来的凭证当然落在已结账的期间里 —— 守卫会拒绝。
#:
#: 所以顺序是：**期间按未结账插入 → 装凭证与分录 → 再把结账状态恢复回去**。
#: 最终数据与生产逐字一致，守卫全程都在（没有关掉它）。
PERIOD_LOCK_COLUMNS = ("locked", "closed_at", "closed_by")

#: 凭证表里「过账」那一列 —— 导入时也要**先当没过账**，装完分录再恢复。
#:
#: 为什么：分录表上有 BEFORE INSERT 的守卫（``fn_entry_lock_guard``）：
#: 「已记账凭证的分录不许动」（审计报告 §3.15 的 ②）。生产上这些分录是在
#: 凭证**过账之前**写进去的；本地导入是「把历史搬过来」，搬来的分录当然
#: 落在已过账的凭证里 —— 守卫会拒绝。
#:
#: 所以顺序是：**凭证按未过账插入 → 装分录 → 把过账状态恢复回去**。
#: 恢复这一步守卫是**允许**的：``fn_posted_voucher_guard`` 只认
#: 「未记账 → 已记账」这一个方向。
VOUCHER_STATUS_COLUMN = "status"
VOUCHER_UNPOSTED = "UNPOSTED"

#: 每张表**自己的** id 要平移。
SHIFT_ID = {
    "subject": True,
    "period": True,
    "book_year": True,
}

#: 指向别的表的外键，按目标表的偏移平移。
SHIFT_FOREIGN_KEYS = {
    "subject": {"parent_id": "subject"},
    "period_opening": {"period_id": "period", "subject_id": "subject"},
    "voucher": {"period_id": "period"},
    "entry": {"subject_id": "subject"},
}

PLUGIN_TABLES = [
    "plugin_275b391c351a_card",
    "plugin_275b391c351a_member",
    "plugin_275b391c351a_rule",
    "plugin_275b391c351a_stmt",
    "plugin_275b391c351a_row",
    "plugin_275b391c351a_alloc",
    "plugin_275b391c351a_recon",
]

HOST_TABLES = ["book", "book_year", "subject", "period", "period_opening", "voucher", "entry"]

#: 授权表也要搬：**``sort_order`` 决定账套在用户账套列表里的次序**，
#: 而插件的成员顺序、账面侧的计算顺序都跟着它走 —— 不搬，界面上的
#: 顺序就和生产不一样（数字一样，看着却不是同一份）。
GRANT_TABLES = ["sys_user_book"]

#: 这些表的 ``id`` **不搬**：本机已经有自己的行（开发库里那个 admin ↔ 宏发商贸
#: 的授权就是 id=1），照搬生产的主键会撞上。授权行的主键没有对外意义，
#: 让本机序列自己发。
SKIP_COLUMNS = {"sys_user_book": ("id",)}

#: 顺序有讲究：先父后子，外键才不会在导入中途找不到人。
IMPORT_ORDER = HOST_TABLES + PLUGIN_TABLES + GRANT_TABLES

#: 各表在生产侧要搬哪些行。
def where_clause(table: str) -> str:
    books = ",".join(str(b) for b in BOOKS)
    if table == "book":
        return f"id IN ({books})"
    if table in ("book_year", "subject", "period", "period_opening", "voucher", "entry"):
        return f"book_id IN ({books})"
    if table == "sys_user_book":
        # 只搬管理员（user_id=1）对这些账套的授权 —— 本机调试用的就是管理员。
        return f"user_id = 1 AND book_id IN ({books})"
    if table in ("plugin_275b391c351a_card", "plugin_275b391c351a_rule"):
        return "TRUE"
    if table == "plugin_275b391c351a_row":
        # 流水行表**没有 card_id**（只有 stmt_id）—— 它靠对账单挂到卡上。
        return ("stmt_id IN (SELECT id FROM plugin_275b391c351a_stmt "
                "WHERE card_id IN (SELECT id FROM plugin_275b391c351a_card))")
    return "card_id IN (SELECT id FROM plugin_275b391c351a_card)"


def shifted(column: str) -> str:
    """``col + OFFSET``，但 NULL 仍然是 NULL（别把空父科目变成 1000000）。"""
    return f"CASE WHEN {column} IS NULL THEN NULL ELSE {column} + {OFFSET} END"


def select_columns(table: str, columns: list[str]) -> list[str]:
    """按目标表的列顺序给出 SELECT 里的表达式（id 与外键的平移都在这儿）。"""
    exprs = []
    for column in columns:
        if SHIFT_ID.get(table) and column == "id":
            exprs.append(f"{shifted(column)} AS {column}")
        elif column in SHIFT_FOREIGN_KEYS.get(table, {}):
            exprs.append(f"{shifted(column)} AS {column}")
        else:
            exprs.append(column)
    return exprs


def cmd_sql(args) -> int:
    """生成「在生产机上跑」的导出脚本（COPY TO 文件，只读表）。"""
    out = Path(args.out)
    database_url = args.database_url or os.environ.get(
        "DEV_DATABASE_URL", "postgresql://cwq:cwq123@localhost:15432/cwq")
    print(f"-- 由 tools/sharedcard_dev_data.py 生成：把共享卡插件用到的账套数据导成 CSV")
    print(f"-- 目标目录：{out}（psql 以 postgres 身份跑，需要能写这个目录）")
    with psycopg.connect(database_url) as local:
        for table in IMPORT_ORDER:
            columns = table_columns(local, table)
            exprs = ", ".join(select_columns(table, columns))
            # NULL 写成 __NULL__：CSV 分不清「NULL」与「空字符串」，
            # 导入时会把空字符串当成 NULL（那是**改数据**，不是搬数据）。
            print(f"COPY (SELECT {exprs} FROM {table} WHERE {where_clause(table)}) "
                  f"TO '{out}/{table}.csv' CSV HEADER NULL '__NULL__';")
    return 0


def table_columns(local, table: str) -> list[str]:
    """列顺序 —— **从本机 dev 库读**（不写死）。

    写死的列清单迟早会跟迁移漂移；漂移了要么导入错列，要么对不上。
    这里读的是本机库的**实际**列顺序，导出与导入用的是同一份。
    """
    with local.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
            (table,))
        columns = [row[0] for row in cur.fetchall()]
    skip = set(SKIP_COLUMNS.get(table, ()))
    columns = [c for c in columns if c not in skip]
    if not columns:
        raise SystemExit(f"本机库里没有表 {table} —— 先装插件再来搬数据。")
    return columns


def clear_imported_scope(local) -> list[str]:
    """把**已经搬进来的那一批**清掉，好让同一段脚本能反复跑。

    **顺序有讲究**（每一步都被库里的守卫盯着）：

    1. **先解锁期间** —— 期间已结账时，凭证连改都改不了（``fn_voucher_guard``）；
    2. **凭证退回未记账** —— 已记账的凭证不许删（``fn_posted_voucher_guard``）；
    3. **先子后父删**：分录 → 凭证 → 期初 → 期间 → 科目 → 年度 → 账套；
    4. 插件自有表按卡删；授权行按「管理员 + 这几本账」删。
    """
    books = ",".join(str(b) for b in BOOKS)
    done: list[str] = []
    with local.cursor() as cur:
        cur.execute(f"UPDATE period SET locked=false, closed_at=NULL, closed_by=NULL "
                    f"WHERE book_id IN ({books})")
        done.append(f"期间解锁 {cur.rowcount} 个")
        cur.execute(f"UPDATE voucher SET status='UNPOSTED' WHERE book_id IN ({books}) "
                    f"AND status <> 'UNPOSTED'")
        done.append(f"凭证退回未记账 {cur.rowcount} 张")
        for sql, label in (
            (f"DELETE FROM entry WHERE book_id IN ({books})", "分录"),
            (f"DELETE FROM voucher WHERE book_id IN ({books})", "凭证"),
            (f"DELETE FROM period_opening WHERE book_id IN ({books})", "期初余额"),
            (f"DELETE FROM period WHERE book_id IN ({books})", "会计期间"),
            (f"DELETE FROM subject WHERE book_id IN ({books})", "科目"),
            (f"DELETE FROM book_year WHERE book_id IN ({books})", "会计年度"),
        ):
            cur.execute(sql)
            done.append(f"删{label} {cur.rowcount} 行")
        # 插件自有表**先删**：它们的外键指着账套（book_id），账套还在时删不掉。
        card_scope = "card_id IN (SELECT id FROM plugin_275b391c351a_card)"
        for table in ("alloc", "row", "stmt", "member", "rule", "recon"):
            if table == "row":
                cur.execute("DELETE FROM plugin_275b391c351a_row WHERE "
                            "stmt_id IN (SELECT id FROM plugin_275b391c351a_stmt WHERE "
                            + card_scope + ")")
            else:
                cur.execute(f"DELETE FROM plugin_275b391c351a_{table} WHERE " + card_scope)
            done.append(f"删插件表 {table} {cur.rowcount} 行")
        cur.execute("DELETE FROM plugin_275b391c351a_card")
        done.append(f"删插件表 card {cur.rowcount} 行")
        cur.execute(f"DELETE FROM book WHERE id IN ({books})")
        done.append(f"删账套 {cur.rowcount} 本")
        cur.execute(f"DELETE FROM sys_user_book WHERE user_id=1 AND book_id IN ({books})")
        done.append(f"删授权 {cur.rowcount} 行")
    return done


def cmd_import(args) -> int:
    source = Path(args.source)
    database_url = args.database_url or os.environ.get(
        "DEV_DATABASE_URL", "postgresql://cwq:cwq123@localhost:15432/cwq")
    with psycopg.connect(database_url) as local:
        missing = missing_tables(local)
        if missing:
            print("本机 dev 库还没有插件的表（插件没装上去）：")
            for name in missing:
                print(f"  - {name}")
            print("先装插件（tools/install_plugin_local.py），再来搬数据。")
            return 3
        if args.replace:
            print("== 清掉上一次搬进来的那一批")
            for line in clear_imported_scope(local):
                print(f"  {line}")
        total = 0
        period_locks: list[tuple] = []
        voucher_statuses: list[tuple] = []
        for table in IMPORT_ORDER:
            path = source / f"{table}.csv"
            if not path.exists():
                print(f"  {table:34s} 跳过（没有 {path.name}）")
                continue
            rows = load_csv(local, table, path, table_columns(local, table),
                            period_locks, voucher_statuses)
            total += rows
            with local.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {table}")
                total_rows = cur.fetchone()[0]
            print(f"  {table:34s} 搬入 {rows} 行，库里共 {total_rows} 行")
        # 分录装完了：先恢复凭证的过账状态（守卫只认「未记账 → 已记账」），
        # 再恢复期间的结账状态（结账之后就不许改凭证了，所以顺序不能反）。
        posted = restore_voucher_status(local, voucher_statuses)
        print(f"  凭证过账状态                    恢复 {posted} 张")
        restored = restore_period_locks(local, period_locks)
        print(f"  期间结账状态                    恢复 {restored} 个")
        for line in reset_sequences(local):
            print(f"  序列 {line}")
        local.commit()
    print(f"== 合计搬入 {total} 行")
    return 0


def missing_tables(local) -> list[str]:
    """本机还缺哪些表 —— 插件没装上去时，插件表根本不存在。"""
    with local.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name = ANY(%s)", (PLUGIN_TABLES,))
        present = {row[0] for row in cur.fetchall()}
    return [t for t in PLUGIN_TABLES if t not in present]


def order_parents_first(table: str, columns: list[str], rows: list[list[str]]) -> list[list[str]]:
    """科目表：**先父后子**地插。

    为什么非这么排不可：科目表上有 BEFORE INSERT 的触发器
    ``fn_subject_parent_year_guard`` —— 它在插入那一刻去**表里**查父科目的
    年度。父行还没插进来时查出来是 NULL，于是报
    「上级科目属于 <NULL> 年度，与本科目（2025 年度）不在同一张科目表上」。

    生产上这些行是**早就**在库里的（守卫是后加的），所以本地导入要自己
    把顺序排对 —— 报错信息看着像数据有问题，其实是**插入顺序**问题。
    """
    if table != "subject":
        return rows
    id_index = columns.index("id")
    parent_index = columns.index("parent_id")
    by_id = {row[id_index]: row for row in rows}
    children: dict[str, list[list[str]]] = {}
    roots: list[list[str]] = []
    for row in rows:
        parent = row[parent_index]
        if parent is None or parent == NULL_MARKER or parent not in by_id:
            roots.append(row)
        else:
            children.setdefault(parent, []).append(row)
    ordered: list[list[str]] = []
    stack = list(reversed(roots))
    while stack:
        row = stack.pop()
        ordered.append(row)
        for child in reversed(children.get(row[id_index], [])):
            stack.append(child)
    if len(ordered) != len(rows):
        # 有环或者父行不在这一批里：剩下的按原顺序补在末尾（让约束去报错，
        # 别在这里悄悄丢行）。
        seen = {row[id_index] for row in ordered}
        ordered.extend(row for row in rows if row[id_index] not in seen)
    return ordered


def load_csv(local, table: str, path: Path, columns: list[str],
             period_locks: list[tuple] | None = None,
             voucher_statuses: list[tuple] | None = None) -> int:
    """把一个 CSV 灌进一张表（客户端 COPY，整批在同一个事务里）。

    **用文本格式 COPY，不是 CSV 格式**：psycopg 的 ``write_row`` 按**文本**
    格式编码（制表符分隔 + 反斜杠转义），而 CSV 格式得自己拼字节 ——
    两者混起来会把整行当成一个字段。文本格式由 psycopg 负责转义，
    引号、换行、反斜杠都不用我们操心。

    ``period_locks`` / ``voucher_statuses`` —— 期间与凭证的「后来才发生」
    的状态（结账、过账）先收走，装完凭证与分录再恢复回去。
    """
    copied = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != columns:
            raise SystemExit(
                f"{path.name} 的列与本机不一致 —— 导出与导入的列顺序必须一致。\n"
                f"  文件：{header}\n  本机：{columns}")
        rows = [row for row in reader]
    rows = order_parents_first(table, columns, rows)
    if table == "period":
        rows, locks = unlock_periods(columns, rows)
        if period_locks is not None:
            period_locks.extend(locks)
    if table == "voucher":
        rows, statuses = unpost_vouchers(columns, rows)
        if voucher_statuses is not None:
            voucher_statuses.extend(statuses)
    with local.cursor() as cur:
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as writer:
            for row in rows:
                writer.write_row([None if value == NULL_MARKER else value for value in row])
                copied += 1
    return copied


def unpost_vouchers(columns: list[str], rows: list[list[str]]) -> tuple[list[list[str]], list[tuple]]:
    """凭证按**未过账**插进去；把生产的过账状态收下来，分录装完再恢复。"""
    id_index = columns.index("id")
    status_index = columns.index(VOUCHER_STATUS_COLUMN)
    statuses: list[tuple] = []
    out = []
    for row in rows:
        status = row[status_index]
        if status != VOUCHER_UNPOSTED:
            statuses.append((row[id_index], status))
            row = list(row)
            row[status_index] = VOUCHER_UNPOSTED
        out.append(row)
    return out, statuses


def restore_voucher_status(local, statuses: list[tuple]) -> int:
    """把过账状态恢复回去（守卫只认「未记账 → 已记账」这一个方向）。"""
    if not statuses:
        return 0
    with local.cursor() as cur:
        cur.executemany(
            "UPDATE voucher SET status = %s WHERE id = %s",
            [(status, int(voucher_id)) for voucher_id, status in statuses])
    return len(statuses)


def unlock_periods(columns: list[str], rows: list[list[str]]) -> tuple[list[list[str]], list[tuple]]:
    """期间按**未结账**插进去；把生产的结账状态收下来，凭证装完再恢复。"""
    id_index = columns.index("id")
    lock_index = columns.index("locked")
    closed_at_index = columns.index("closed_at")
    closed_by_index = columns.index("closed_by")
    locks: list[tuple] = []
    out = []
    for row in rows:
        locked, closed_at, closed_by = row[lock_index], row[closed_at_index], row[closed_by_index]
        if locked == "t":
            locks.append((row[id_index], locked, closed_at, closed_by))
            row = list(row)
            row[lock_index] = "f"
            row[closed_at_index] = NULL_MARKER
            row[closed_by_index] = NULL_MARKER
        out.append(row)
    return out, locks


def restore_period_locks(local, locks: list[tuple]) -> int:
    """把结账状态恢复回去 —— 最终数据与生产一致。"""
    if not locks:
        return 0
    with local.cursor() as cur:
        cur.executemany(
            "UPDATE period SET locked = %s::boolean, closed_at = %s::timestamptz, "
            "closed_by = %s WHERE id = %s",
            [(locked,
              None if closed_at == NULL_MARKER else closed_at,
              None if closed_by == NULL_MARKER else closed_by,
              int(period_id))
             for period_id, locked, closed_at, closed_by in locks])
    return len(locks)


def reset_sequences(local) -> list[str]:
    """把序列推到当前最大 id 之后 —— 不然本地新建的行会撞主键。"""
    done = []
    with local.cursor() as cur:
        for table in IMPORT_ORDER:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
            seq = cur.fetchone()[0]
            if not seq:
                continue
            cur.execute(
                f"SELECT setval(%s, COALESCE((SELECT MAX(id) FROM {table}), 1), true)", (seq,))
            cur.execute("SELECT last_value FROM " + seq)
            done.append(f"{table}（现值 {cur.fetchone()[0]}）")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sql = sub.add_parser("sql", help="生成在生产机上跑的导出脚本")
    sql.add_argument("--out", default="/tmp/sc_export", help="CSV 输出目录")
    sql.add_argument("--database-url", default=None, help="本机 dev 库连接串（读列顺序用）")
    sql.set_defaults(func=cmd_sql)
    imp = sub.add_parser("import", help="把 CSV 导进本机 dev 库")
    imp.add_argument("--from", dest="source", required=True, help="CSV 目录")
    imp.add_argument("--database-url", default=None)
    imp.add_argument("--replace", action="store_true",
                      help="先清掉上一次搬进来的那一批（重新同步用）")
    imp.set_defaults(func=cmd_import)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
