"""凭证：增删改查、批量、记账/反记账。

**为什么每条 except 都写着 ``(ValueError, ArithmeticError)``**（审计 P1-21）：
``decimal.InvalidOperation`` 的祖先链是 ``InvalidOperation → DecimalException
→ ArithmeticError → Exception`` —— 它**不是 ValueError 的子类**。而
``accounting.util.money`` 对 ``1e30`` 这类量级正是抛它。原来这些路由只写
``except ValueError``，于是一张凭证提交 ``amount: 1e30`` 就得到 500：
用户填错一个数，服务端崩给他一个「服务器错误」，而且整张凭证的校验全部中断。

``ArithmeticError`` 是 Decimal 整个异常族的公共祖先，列在这里等于「Decimal
侧的任何算术异常都算 400」。服务层的 ``money()`` 也已经把同类异常翻译成
ValueError 了，这里是第二道。
"""
from __future__ import annotations

import io
import logging
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import (APIRouter, Depends, File, HTTPException, Query, Request,
                     Response, UploadFile)
from pydantic import BaseModel, Field
from sqlalchemy import func, literal, or_, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.accounting import importer as svc_import
from app.accounting import organize as svc_organize
from app.accounting import vouchers as svc
from app.accounting.util import constraint_name, find_period, money
from app.api.schemas import BatchIds, VoucherIn
from app.core import idempotency as idem
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
# 本地别名保持函数名 ``read_upload`` 不变：``tests/test_upload_capacity_matrix.py``
# 按这个名字守「每个上传入口都调了共享容量闸」，而这里要的其实是它的同步版
# ``uploads.read_upload_sync``（端点已改成同步 ``def``，不能再 await）。
from app.core.uploads import UploadTooLarge, read_upload_sync as read_upload
from app.models import (Book, Entry, ImportBatch, Period, Subject, SysUser,
                     UserBook, Voucher)

log = logging.getLogger("app.api.vouchers")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 凭证号在期间内唯一。撞号时数据库只会说「违反唯一约束」，
#: 看到的人不知道该改哪个字段 —— 所以把它翻成人话时要认得出**是哪一条**约束。
#:
#: 与 ``accounting/vouchers.py``、``accounting/organize.py`` 里的那份同名常量
#: 是**同一个约束**（三处各写一份是本仓的既有做法：它们分属不同层，
#: 互相 import 会成环）。认约束而不是「凡 IntegrityError 都当撞号」——
#: 别的违例（外键指向不存在、字段非空、库层 CHECK 护栏）撞上时，
#: 报「凭证号重复」是**假话**，用户会照着改一个根本没问题的字段。
_NO_UNIQUE = "uq_voucher_period_no"


def _filename(name: str) -> str:
    import urllib.parse

    return "attachment; filename*=UTF-8''" + urllib.parse.quote(name)


# ---------------------------------------------------------------------------
# **数据库异常不再在这里就地收口**（审查 L-01 / P1-2）
# ---------------------------------------------------------------------------
#
# 原来这里有一个 ``_diag(exc)`` 辅助函数：从 psycopg 的
# ``diag.message_primary`` 里取原文，塞进 ``HTTPException`` 的 detail。
# 它把 PG 的原始违例文案交给了客户端 —— 里面是表名、列名、约束名，
# 唯一冲突时还会带出重复键的值。
#
# 之所以曾经「看起来没问题」：我们自己写的护栏文案（``db_init.sql`` 里的
# ``RAISE EXCEPTION``）是中文，本来就是给人看的；但 **PG 自己的**违例
# 文案是英文的，里面带着表名与约束名。同一段代码分不清这两者。
#
# 现在这一层**整体交给** ``app/main.py`` 的全局处理器（``dbapi_error_handler``）：
# 它按「文案是不是我们自己写的中文」区分 —— 我们写的中文护栏原文照旧回给
# 客户端（那是给人看的提示），PG 自带的英文文案一律换成**脱敏文案 + 请求号**
# （表名/列名/约束名只进服务端日志）。口径只有一份，不必两处各判一次。
#
# 后果：约束类违例仍然是 **4xx**（23505/23503/23502/23514/22001 → 400），
# 其它数据库异常是脱敏的 **500**。也就是说这条路由**没有**把 4xx 降级成 500。

router = APIRouter(prefix="/api/books/{book_id}/vouchers", tags=["凭证"])


def _book_subjects(db: Session, book_id: int) -> dict[int, Subject]:
    """一次取回这个账套的全部科目，按 id 建索引。

    序列化一张凭证要显示科目编码与名称，而序列化 200 张凭证如果每张查一次，
    就是一个 200 页的列表接口发 200 条同样的 SELECT（审计 P3-12）。
    """
    return {s.id: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book_id)).all()}


def _book_periods(db: Session, book_id: int, period_ids: set[int]) -> dict[int, Period]:
    """一次取回这些期间，按 id 建索引。

    与 ``_book_subjects`` 同一个道理：列表页一次序列化几十张凭证，
    每张查一次期间就是几十条同样的 SELECT。
    """
    if not period_ids:
        return {}
    rows = db.scalars(select(Period).where(
        Period.book_id == book_id, Period.id.in_(period_ids))).all()
    return {p.id: p for p in rows}


def _serialize(db: Session, v: Voucher, subs: dict[int, Subject] | None = None,
               periods: dict[int, Period] | None = None,
               version: bool = False) -> dict:
    entries = sorted(v.entries, key=lambda e: e.line_no)
    # 列表页由调用方一次取好传进来；单张场景（建/改/删后回显）这里兜底。
    subs = _book_subjects(db, v.book_id) if subs is None else subs
    # **年度由调用方给**：``periods`` 是「本次要序列化的这些凭证所属的期间」的索引。
    #
    # 这里**绝不按 id 自己去查** —— 序列化函数里逐张查库就是 N+1：
    # 打印层一次最多序列化 5000 张凭证（page_size=5000），满月的账就是 5000 条
    # 同样的 SELECT，与当初审计发现的严重 2 是同一个坑（那里的科目一次取好就是
    # 为此修的）。所以年度要么由调用方**一次取好**传进来，要么这个字段就没有 ——
    # 绝不为了凑一个字段把打印拖慢。
    #
    # 年度是「这张凭证属于哪一年」，取自它自己那一期 —— 凭证页要按它去取
    # **那一年**的科目表（科目表每个年度各有一份，迁移 0032），拿顶栏那一年的话，
    # 往年凭证上的 subject_id 在新年度那张表里根本不存在，格子里挂的科目就全空。
    period = periods.get(v.period_id) if periods else None
    # 合计走 side_totals：按列求和、负数照减 —— 跟账上（触发器、账套引擎）同一口径
    dr_total, cr_total = svc.side_totals(
        [{"direction": e.direction, "amount": e.amount} for e in entries])
    out = {
        "id": v.id, "voucher_no": v.voucher_no, "voucher_date": v.voucher_date.isoformat(),
        "status": v.status, "source": v.source, "remark": v.remark,
        "attachments": v.attachments, "created_by": v.created_by,
        "posted_by": v.posted_by,
        "posted_at": v.posted_at.isoformat() if v.posted_at else None,
        "period_id": v.period_id,
        "total_debit": str(dr_total),
        "total_credit": str(cr_total),
        "entries": [
            {
                "id": e.id, "line_no": e.line_no, "subject_id": e.subject_id,
                "subject_code": subs[e.subject_id].code if e.subject_id in subs else "",
                "subject_name": subs[e.subject_id].name if e.subject_id in subs else "",
                "direction": e.direction, "amount": str(e.amount), "summary": e.summary,
            }
            for e in entries
        ],
    }
    # 年度只在真的知道的时候才带上（调用方给过期间索引）——
    # 不知道就**没有这个键**，不留一个 None 让人以为「这一张没有年度」。
    if period is not None:
        out["year"] = period.year
    # **乐观锁令牌**（审计 §3.15）：客户端改这张凭证时把它原样送回来，
    # 服务端拿库里此刻的值算一个同样的令牌来比 —— 不一致就 409。
    #
    # **只在「单张」响应里带**（``version=True``），列表与打印不带：
    # 一次 5000 张的列表/打印，每个格子多 40 字节就是 200KB ——
    # 而列表页不做行内编辑（编辑走单张 GET），根本用不上这个令牌。
    #
    # 格式由 ``accounting.vouchers.version_token`` 独家提供 ——
    # 令牌是客户端与服务端之间的一枚硬币，两边格式必须一模一样
    # （审计 §4.5：同一个量只允许一个权威实现）。
    if version:
        out["version"] = svc.version_token(v.updated_at)
    return out


def _book_period(db: Session, book: Book, period_id: int | None, label: str) -> Period | None:
    """按 id 取期间，并核对它**属于本账套**（审计 P2-1）。

    跨账套的 ``period_id`` 不能放行：``ord`` 是**账套内相对序号**
    （``(year - start_year) * 12 + month``），两个账套的同一个 ord 含义
    完全不同。放行的后果是「**结果是错的而不是空的**」——

      · 外账套的 ord=1   → 本账套返回「ord >= 1」的全部凭证；
      · 外账套的 ord=2400 → 本账套返回空集。

    用户以为在看「某个期间之后」，实际拿到的是完全另一个区间。
    同一套参数在 ``api/ledger.py`` 的三个端点里都是 404（那里是正确口径），
    这里跟过去才是全系统一致。

    消息沿用 ledger.py 的说法（「起始期间不存在」），免得同一件事有两种措辞。
    """
    if not period_id:
        return None
    p = db.get(Period, period_id)
    if p is None or p.book_id != book.id:
        raise HTTPException(404, f"{label}期间不存在")
    return p


@router.get("")
def query(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
          from_period_id: Optional[int] = None, to_period_id: Optional[int] = None,
          date_from: Optional[date] = None, date_to: Optional[date] = None,
          status: Optional[str] = None, source: Optional[str] = None,
          keyword: Optional[str] = None, min_amount: Optional[Decimal] = None,
          max_amount: Optional[Decimal] = None, subject_id: Optional[int] = None,
          sort: str = Query(svc.DEFAULT_SORT, pattern="^(no_asc|no_desc|date_asc|date_desc)$"),
          page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    fp = _book_period(db, book, from_period_id, "起始")
    tp = _book_period(db, book, to_period_id, "结束")
    rows, total = svc.query_vouchers(
        db, book.id, from_period=fp, to_period=tp, date_from=date_from, date_to=date_to,
        status=status, source=source, keyword=keyword, min_amount=min_amount,
        max_amount=max_amount, subject_id=subject_id, page=page, page_size=page_size,
        sort=sort,
    )
    # 科目一次取好，逐张序列化共用（审计 P3-12：200 行的页原来发 200 条同样的查询）
    subs = _book_subjects(db, book.id)
    # 期间同样一次取好：每张凭证都要报出自己所属的会计年度（见 _serialize 的说明）
    periods = _book_periods(db, book.id, {v.period_id for v in rows})
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_serialize(db, v, subs, periods) for v in rows]}


@router.post("", status_code=201)
def create(payload: VoucherIn, request: Request, response: Response,
           book: Book = Depends(get_my_book), db: Session = Depends(get_db),
           _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    # ------------------------------------------------------------------
    # 幂等键（审计报告 §3.28）
    # ------------------------------------------------------------------
    # 没有它的时候，这条路的失败场景是：
    #   新凭证 POST 发出 → 服务端已入库 → 响应在网络上丢了（30s 超时）
    #   → 客户端以为自己没存上 → 再敲一个字 → 自动保存又发一次 POST
    #   → **库里两张内容几乎相同的凭证**。
    #
    # 客户端「先查一下有没有存上」是**不够的**：查和写之间仍有时间窗
    # （两次自动保存几乎同时到，两边都查不到、都去写）。
    # 所以胜负必须由数据库当场分出来 —— 一把键对应一行占位，
    # 抢到的人负责做完，后来的人拿既有结果回去。
    claim_row = None
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key is not None:
        try:
            key = idem.validate_key(idem_key)
        except idem.InvalidIdempotencyKey as exc:
            raise HTTPException(400, str(exc))
        digest = idem.request_hash(payload.model_dump())
        claimed, row = idem.claim(db, book_id=book.id, key=key, request_hash=digest)
        if not claimed:
            # 这把键已经有人用过了 —— **那次请求的结果就是答案**。
            #
            # 回放而不是重建：回放的那张里存的是**上一次**那一版内容，
            # 而用户此刻在屏幕上可能又敲了几个字 —— 客户端收到回放标志后
            # 会**补一次 PUT** 把当前内容收敛上去（见
            # ``frontend/src/views/voucher/VoucherEdit.vue``）。
            v = db.get(Voucher, row.voucher_id) if row.voucher_id else None
            if v is None:
                # 占位在、凭证不在：上一次请求要么还在跑、要么建失败了。
                # **不能**当成「没发生过」重建一张 —— 那正是重复凭证的来源。
                raise HTTPException(409, "同一张凭证正在创建中，请稍后重试")
            # 回放不是新建，所以状态码是 200 而不是 201：
            # 监控里数 201 会把一张凭证数成两张。
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
            return _serialize(db, v, periods=_book_periods(db, book.id, {v.period_id}),
                               version=True)
        claim_row = row
    try:
        # ``commit=False``：占位行、凭证、占位行里的 voucher_id
        # 必须在**同一个事务**里落定 ——
        # 失败了占位跟着回滚，键不会被「烧掉」（失败恰恰是最需要重试的时候）；
        # 成功了占位与凭证一起可见，后来的同键请求立刻能回放。
        v = svc.create_voucher(
            db, book_id=book.id, user=user, voucher_date=payload.voucher_date,
            entries=[e.model_dump() for e in payload.entries], remark=payload.remark,
            attachments=payload.attachments, source=payload.source,
            # ``voucher_no`` 刻意**不传**。用户 2026-10-19 定的口径是凭证号
            # 一律自动生成：新增页、插入页、修改页三处都只读。号由服务端决定
            # —— 插入走 ``insert_slot``（占参照的号或参照号 +1），普通新增走
            # ``next_voucher_no``。客户端传什么都不采信，光改前端等于没关。
            #
            # 导入是**另一个入口**（``importer.commit`` 直接调服务层，不经过
            # 这里），那里的号来自 Excel，经 ``require_no`` 归一后入库。
            voucher_no=None,
            insert_after_id=payload.insert_after,
            insert_position=payload.insert_position,
            # ``enforce_date_order``：**只有界面上这张单张新建的表单**才查
            # 「日期不能早于本期已有的最后一张」（审计报告 §3.18）。
            #
            # 新凭证的号排在本期号段**末尾**，而「整理就绪」体检要求号序与
            # 日期同向 —— 号大的那张日期反而更早，就判「时序不对」，
            # 这个月从此既不能记账也不能结账。用户当场改个日期就行，
            # 比月底被体检拦下再说好得多。
            #
            # **插入（``insert_after``）不算**：那个新号落在号段**中间**，
            # 它本来就该允许比最后一张早 —— 那正是「插入凭证」的意义。
            enforce_date_order=payload.insert_after is None,
            # ``internal`` 刻意**不传**：它是「系统自己生成的凭证」的唯一入口
            # （结转损益 / 年度结转在 periods.py 里以 internal=True 调本函数）。
            # 这里传下去的任何东西都来自请求体，所以 source 只能取
            # ``VoucherIn`` 那个封闭的 Literal —— CARRY_FORWARD 已经不在里面，
            # 客户端提交它会直接 422。更新走 PUT 的那条路同样改不了 source：
            # ``update_voucher`` 只认日期、分录、备注、附单据数。
            commit=False,
        )
        if claim_row is not None:
            idem.complete(db, claim_row, v.id)
        db.commit()
        db.refresh(v)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    # 期间一次取好（一条查询），年度随凭证一起出去（见 _serialize 的说明）
    return _serialize(db, v, periods=_book_periods(db, book.id, {v.period_id}),
                               version=True)


def _target_period_code(db: Session, v: Voucher, voucher_date: date | None) -> str | None:
    """改凭证日期跨期时，**目标期间的编码**（没有改期时返回 None）。

    供并发兜底那条路径拼消息用（撞了唯一约束时，得说清是撞在哪一期）。

    以前这里还查「目标期间有没有同号的凭证」，跨期改日期会被它挡下来
    （422「那个期间已经有凭证号 X，不能把凭证改到那一期」）。可凭证号又不许人
    手工改了（用户 2026-10-19 定的），于是「改个日期」在跨期时**根本走不通** ——
    而改日期恰恰是最自然的动作（用户 2026-10-24）。

    现在号由服务端**跟着目标期间重新发**（该期现有最大号 + 1，见
    ``accounting.vouchers.update_voucher``），撞号这件事压根不会发生；
    真正还可能撞上的只有并发（别人同一刻把那个号占了），那条由下面
    IntegrityError 兜底翻成人话。
    """
    if not voucher_date:
        return None
    target = find_period(db, v.book_id, voucher_date)
    if target.id == v.period_id:
        return None
    return target.code


@router.put("/{voucher_id}")
def update(voucher_id: int, payload: VoucherIn, book: Book = Depends(get_my_book),
           db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
           user: SysUser = Depends(get_current_user)):
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    # 撞号检查放在服务层调用**之前**（人话在这里出，而不是在 flush 之后
    # 让数据库的英文原文冒出来）。find_period 抛的 ValueError 与服务层
    # 那条同义，所以这里翻成 400，行为不变。
    try:
        target_code = _target_period_code(db, v, payload.voucher_date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        data = payload.model_dump()
        data["entries"] = [e.model_dump() for e in payload.entries]
        # ``version`` 是**可选**的乐观锁令牌（服务层拿它与库里的
        # ``updated_at`` 比，不一致就抛版本冲突 → 下面翻成 409）。
        #
        # **这层是通电的，不是摆设。** 请求体模型 ``VoucherIn``
        # （那份 schema 在 ``app/api/schemas.py``）里**有** ``version`` 字段，
        # 前端也**真的带**：``frontend/src/views/voucher/VoucherEdit.vue``
        # 保存时送 ``version: voucherVersion.value || null``，并且专门处理
        # 409（``e?.response?.status === 409`` 时提示「已被他人改动，请刷新」）。
        # 用例钉在 ``tests/test_audit_fix_voucher_version.py``
        # （``test_带着过期的令牌回409``）。
        #
        # **不带 = 不比**（老客户端、以及刚建出来还没拿到令牌的那一趟）：
        # 那条路上的正确性靠服务层的**行锁**兜着 —— 并发改同一张凭证时，
        # 后一个拿到锁会读到最新的 status / 分录，不会拿陈旧的那份去覆盖。
        #
        # 令牌由 ``accounting.vouchers.version_token`` 独家生成，
        # 格式与那份**逐字一致** —— 差一个时区写法就会「带了也永远判过期」。
        svc.update_voucher(db, v, user, data,
                           expected_version=data.get("version"))
    except svc.VoucherVersionConflict as e:
        # 409：你手上的版本过期了，请刷新后重试。
        # **必须排在 ``except ValueError`` 之前** —— 版本冲突是 ValueError 的
        # 子类，顺序反了就变成 400（前端会当成「你参数写错了」而不是
        # 「别人改过了」）。与 ``periods.PeriodVersionConflict`` 同一条规矩。
        db.rollback()
        raise HTTPException(409, str(e))
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except IntegrityError as exc:
        # **只把「凭证号撞车」这一种翻成人话**（审计报告 §3.20）。
        #
        # 原来这里是**裸** ``except IntegrityError``，于是**所有**完整性违例
        # 一律被说成「凭证号刚被另一张凭证占用，请再保存一次」——
        # 而 23514 CheckViolation（库层护栏）说的是完全另一回事。
        # 最典型的一次：把 2026 年的凭证改期到 2027 年、分录还挂在 2026 年的
        # 科目上 → 库层 ``fn_entry_subject_year_guard`` 报的是
        # 「分录的科目属于 2026 年度，不能记在 2027 年度的凭证上」，
        # 那句**本来就该给用户看**的中文被换成了一句「凭证号被占用，
        # 请再保存一次」—— 用户被指引去重试一个**永远不可能成功**的操作，
        # 真正的病因（年度不一致）一次也没出现在界面上。
        #
        # 外键违例、非空违例同理：把它们说成「凭证号被占用」是**假话**，
        # 用户会照着去改一个根本没问题的字段。
        #
        # 判据认**约束名**（``util.constraint_name``）—— 与
        # ``accounting/vouchers.py``、``accounting/organize.py`` 同一份口径。
        db.rollback()
        if constraint_name(exc) == _NO_UNIQUE:
            where = f"{target_code} " if target_code else "目标期间 "
            raise HTTPException(
                422,
                f"凭证号 {v.voucher_no} 在{where}刚被另一张凭证占用"
                f"（可能刚被另一条记录占用）。请再保存一次，系统会重新发号。"
            )
        # 其余的**不吞**：原样抛回全局处理器（``app/main.py`` 的
        # ``dbapi_error_handler``）—— 我们自己写的中文护栏原文会照实透出，
        # PG 自带的英文原文会被换成脱敏文案 + 请求号。这条路由本来就不该
        # 把 4xx 降级成 500（见文件头那段说明），所以原样抛出即可。
        raise
    # 期间一次取好（一条查询），年度随凭证一起出去（见 _serialize 的说明）
    return _serialize(db, v, periods=_book_periods(db, book.id, {v.period_id}),
                       version=True)


def _assert_no_dead_gap(db: Session, book: Book, rows: list[Voucher]) -> None:
    """删掉这些凭证之后，号段会不会变成**「整理凭证」也补不回来**的死路
    （审计 P1-3）。

    死路长这样：期间里有「记0001（未记账） + 记0003（已记账）」，中间那张
    被删了 → 记0002 空出来。整理只给**未记账**的凭证重新发号，0003 已经
    记账、动不了，于是整理**改 0 张**、洞一直在原地。而
    ``accounting/organize.assert_tidy`` 又拦着断号不让记账、不让结账 ——
    该月在界面上就无路可走了（以前那句报错还让人「去整理凭证」，
    正是那个无效动作）。

    所以在**删除的入口**就把它拦下来，并说清真正的出路：先反结账，
    把已记账的凭证退回未记账，整理才排得动。

    删一张和删一批走同一道闸（批量那几行更要命：一次删 3 张能一次性
    把整段号打乱）。判据在 ``accounting/organize.irreparable_gaps`` 里，
    与整理自己用的是**同一份**号段语义。

    **判据的口径是「整理跑完之后这个号还是不是一个断号」**（不是「断号的
    序号大不大于整理发出去的最大序号」那个启发式 —— 审计报告 §3.17 的
    真旁路：号段顶端那张若是未记账的凭证，整理会把整段压短、那些高位空号
    因此不再是断号，整理一次就补上了；用启发式判会把这种状态误判成死路，
    **误拦一次本来合法的删除**）。

    **报错里「已记账的那些号动不了」这句话有保证是真的**：这个闸只有在
    「整理补不上」时才拦，而整理补不上的断号**必然**有已记账的凭证占在
    它后面（已记账的号原样留着，把整段的上限撑着）—— 所以这里不存在
    「其实没有已记账凭证、却让人去反结账」的死路建议。
    """
    by_period: dict[int, list[Voucher]] = {}
    for v in rows:
        # 已记账的凭证本来就删不掉（服务层会先拒），那时的号段分析没有意义 ——
        # 而且该给用户看的是「已记账的凭证不能删除」，不是号段那一套。
        if v.status == "POSTED":
            return
        by_period.setdefault(v.period_id, []).append(v)
    for period_id, victims in by_period.items():
        period = db.get(Period, period_id)
        if period is None:
            continue
        gaps = svc_organize.irreparable_gaps(
            db, book.id, period, {v.id for v in victims})
        if not gaps:
            continue
        raise HTTPException(
            400,
            f"删掉之后 {period.code} 的号段会断在 {'、'.join(gaps)}，"
            f"而这个洞「整理凭证」补不上（它只给未记账的凭证重新发号，"
            f"已记账的那些号动不了）—— 那样这个月就既不能记账也不能结账了。"
            f"请先到「期末结账」对 {period.code} 执行「反结账」"
            f"（把已记账的凭证退回未记账），再删这几张。"
        )


@router.delete("/{voucher_id}")
def remove(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
           _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    _assert_no_dead_gap(db, book, [v])
    try:
        svc.delete_voucher(db, v, user)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/post")
def post_many(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        n = svc.post_vouchers(db, book.id, payload.ids, user)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"posted": n}


@router.post("/unpost")
def unpost_many(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        n = svc.unpost_vouchers(db, book.id, payload.ids, user)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"unposted": n}


@router.post("/batch-delete")
def batch_delete(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    rows = list(db.scalars(select(Voucher).where(
        Voucher.id.in_(payload.ids), Voucher.book_id == book.id)).all())
    # ID 集合必须解析完整。少了哪张就整批拒绝，不做「删掉找得到的那些」。
    # 原来这里直接把查询结果拿去删：传 3 个 id、其中 1 个不存在（或属于别的
    # 账套），返回 {"deleted": 2} 且不置一词 —— 用户以为自己删了 3 张，
    # 实际 2 张，那 1 张还好好地躺在列表里。
    # 列表页状态过期、并发删除、跨账套误选都会走到这条路。
    wanted = sorted(set(payload.ids))
    found = {v.id for v in rows}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise HTTPException(
            400, f"凭证不存在或不属于当前账套，已整批拒绝："
                 f"{', '.join(str(i) for i in missing[:10])}"
                 + ("…" if len(missing) > 10 else ""))
    # 先整批体检再删：批量选中 20 张、其中 1 张已记账时，
    # 逐张删到那张才报错的话，用户只知道「删失败」却不知道是哪一张。
    posted = [v.voucher_no for v in rows if v.status == "POSTED"]
    if posted:
        raise HTTPException(
            400, f"已记账的凭证不能删除，请到「期末结账」反结账后再删：{'、'.join(posted[:10])}"
                 + ("…" if len(posted) > 10 else ""))
    _assert_no_dead_gap(db, book, rows)
    try:
        # 一批要么全删要么一张不删。逐张各自 commit 的话，删到第 5 张失败时
        # 前 4 张已经落在库里，回滚也回滚不回来 —— 用户收到 400，
        # 账上却少了 4 张，且没有「哪几张成功了」的信息。
        for v in rows:
            svc.delete_voucher(db, v, user, commit=False)
        db.commit()
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"deleted": len(rows)}


# ===================================================================== 整理凭证


class OrganizeIn(BaseModel):
    period_id: Optional[int] = None      # 为空表示整个账套逐期整理
    strategy: Literal["RENUM", "SORT_DATE"] = "SORT_DATE"


@router.post("/organize/preview")
def organize_preview(payload: OrganizeIn, book: Book = Depends(get_my_book),
                     db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    """先看差异再确认。计划阶段只算，不碰数据。"""
    try:
        plans = svc_organize.plan_organize(
            db, book_id=book.id, period_id=payload.period_id,
            strategy=payload.strategy)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e))
    out = [p.to_dict() for p in plans]
    return {
        "strategy": payload.strategy,
        "periods": out,
        "total_changed": sum(p["changed_count"] for p in out),
        "total_gaps": sum(len(p["gaps"]) for p in out),
    }


@router.post("/organize")
def organize(payload: OrganizeIn, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
             _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        result = svc_organize.apply_organize(
            db, book_id=book.id, user=user, period_id=payload.period_id,
            strategy=payload.strategy)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    # **数据库异常不在这里就地吞掉**（审查 L-01）：原来这条分支把
    # ``diag.message_primary`` 原样放进 detail，客户端因此能读到 PG 的
    # 违例原文（表名、列名、约束名，唯一冲突时还会带出重复键的值）。
    # 现在它上抛给 ``app/main.py`` 的全局处理器：约束类仍是 **4xx**
    # （脱敏文案 + ``X-Request-ID``），其它是脱敏的 500。
    return result


# ===================================================================== Excel 导入


@router.get("/import-template.xlsx")
def import_template(book: Book = Depends(get_my_book)):
    """下载标准导入模板，表头固定，前端设计器/帮助页直接引用。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "凭证导入"
    headers = ["日期", "凭证号", "摘要", "科目编码", "科目名称", "借方金额", "贷方金额",
               "附单据数", "备注"]
    ws.append(headers)
    fill = PatternFill("solid", fgColor="DCE6F1")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGHI", (12, 12, 24, 14, 20, 14, 14, 10, 24)):
        ws.column_dimensions[col].width = w
    ws.append(["2026-01-15", "记001", "收到租金", "1001", "库存现金", 5000, "", 1, "示例行，导入前请删除"])
    ws.append(["", "", "收到租金", "6001", "主营业务收入", "", 5000, "", ""])
    ws.append(["2026-01-20", "", "支付水电费", "6602", "管理费用/水电费", 800, "", 2, "日期为空表示接上一张"])
    ws.append(["", "", "支付水电费", "1002", "银行存款", "", 800, "", ""])
    ws.append([])
    ws.append(["说明：1) 一张凭证占多行，第二行起日期与凭证号留空"])
    ws.append(["      2) 科目编码或科目名称填一个即可，会自动匹配本账套科目"])
    ws.append(["      3) 借方/贷方金额只填有金额的一侧"])
    ws.append(["      4) 凭证号留空由系统按期间自动编号"])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(content=buf.getvalue(), media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename("凭证导入模板.xlsx")})


@router.post("/import")
def upload_import(file: UploadFile = File(...), book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                  user: SysUser = Depends(get_current_user)):
    """上传并解析，暂存待确认。不写任何凭证。

    **同步端点，刻意如此**（审计严重 1）：全仓 100+ 个端点里只有它和
    ``openings/parse`` 是 ``async def``。``async def`` 端点直接跑在事件循环线程上，
    而这里是**同步**的 openpyxl（``svc_import.stage`` → ``load_workbook``）加
    **同步**的 psycopg Session —— 一次 20MB / 2 万行的导入能把整个 worker 的
    事件循环堵死数秒，期间该 worker 上所有请求（含所有同步端点的依赖解析）一起排队，
    而 CPU 占用并不高。改成 ``def`` 之后 FastAPI 把它丢进 anyio 线程池，
    阻塞的是线程池线程，全仓其余端点本来就是这么跑的。
    """
    # .xls 是用友等旧系统导出的 OLE 复合文档，openpyxl 读不了 —— service 层按
    # 文件内容把它分流给 xlrd（和科目表、期初余额表共用同一个分流器）。这里只
    # 挡明显不是表格的后缀，真正读不读得开由解析器说了算。
    if not (file.filename or "").lower().endswith((".xls", ".xlsx", ".xlsm")):
        raise HTTPException(400, "只支持 .xls 或 .xlsx 格式的凭证表（用友导出的 .xls 可直接导入）")
    # 容量闸住在共享工具里（审计 P1-4 / P2-02）。原来是 ``file.file.read()`` ——
    # 无参数意味着「有多少读多少」，Nginx 那条 50m 只是最后一道闸，而它是**请求体**
    # 限制：压缩过的上传、拆成多次的请求都能绕过，而且解析器拿到之后
    # 会把每一行都累积进内存。
    #
    # 第二、三道闸（zip 中央目录声明的解压体积、遍历过程中的行数）在 service 层，
    # 与期初余额解析共用同一套 —— 两处曾是同一种入口的两种长法。
    try:
        data = read_upload(file)
        batch = svc_import.stage(db, book.id, user, file.filename, data)
    except UploadTooLarge as e:
        # 413 而不是 400：文件太大是「换个更小的再来」，不是参数写错了。
        raise HTTPException(413, str(e))
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e))
    return {
        "batch_id": batch.id, "filename": batch.filename, "status": batch.status,
        "total": batch.total, "ok_count": batch.ok_count, "error_count": batch.error_count,
        "vouchers": batch.payload.get("vouchers", []),
        "errors": batch.payload.get("errors", []),
        "unknown_subjects": batch.payload.get("unknown_subjects", {}),
        "unknown_headers": batch.payload.get("unknown_headers", []),
        # 这份表是不是按用友序时账的排版读的、有没有跳过表尾的合计行。
        # 「合计」行被跳过时用户该知道 —— 否则他会以为表里没这行数据。
        "journal": bool(batch.payload.get("journal")),
        "footer_rows": int(batch.payload.get("footer_rows") or 0),
    }


@router.get("/import")
def list_imports(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    # P2-22：这是**只读**接口，却会 DELETE + COMMIT（``purge_stale_batches``）。
    # 原来清理排在返回构造**之前**：一旦下面序列化批次时抛异常（比如 payload
    # 里混进了非 JSON 的值），用户拿到 500，而那一批过期批次**已经被删掉了** ——
    # 一个失败的请求留下了它不该有的副作用，且没有任何记录说明删了什么。
    #
    # 顺序反过来：先把响应算出来，再清理。序列化失败时什么都没删，
    # 清理失败也不影响这一页已经算好的结果。
    #
    # 清理本身**不取消** —— 「传完没确认的过期批次」要有人负责清，而这是
    # 唯一保证一定会被触发的地方（没人会为了清理去跑定时任务）。
    rows = db.scalars(
        select(ImportBatch).where(ImportBatch.book_id == book.id)
        .order_by(ImportBatch.id.desc()).limit(100)
    ).all()
    items = [{"id": b.id, "filename": b.filename, "status": b.status, "total": b.total,
              "ok_count": b.ok_count, "error_count": b.error_count,
              "committed_count": b.committed_count, "message": b.message,
              "created_by": b.created_by, "created_at": b.created_at.isoformat()}
             for b in rows]
    try:
        svc_import.purge_stale_batches(db, book.id)
    except SQLAlchemyError:
        # 清理失败不该让「看导入历史」这一页 500。过期批次留在库里，
        # 下次进来还会再清一次 —— 那是可重试的状态，500 不可重试。
        db.rollback()
        log.warning("清理过期导入批次失败（账套 %s），本次只返回列表", book.id,
                    exc_info=True)
    return items


@router.post("/import/{batch_id}/commit")
def commit_import(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    """确认入库。**批次行必须锁住再读状态** —— 这是并发提交的唯一闸门。

    原来这里是裸的 ``db.get(ImportBatch, batch_id)``：无锁，读出来的
    ``status`` 是**那一刻**的快照。两个并发的 ``POST /import/{id}/commit``
    （用户手快按了两次双击；或者一个人提交、另一个人同时点了确认）会双双读到
    PENDING，双双通过 ``importer.commit`` 里的 ``status != "PENDING"`` 检查。

    表里**带凭证号**时，``uq_voucher_period_no`` 会把第二边顶回去（响亮地失败，
    至少不会脏）；但工作簿常常不带号（``importer`` 里是
    ``row_no or None``），这时两边各自 ``next_voucher_no`` 取「本期最大号 +1」。
    READ COMMITTED 下第二个事务看得见第一个刚提交的凭证，于是拿到**另一个**号
    —— 约束不触发，两批凭证安静地重复入库，而 ``committed_count`` 还各自
    报「成功导入 N 张」。用户点一次确认，账上多出两份。

    ``with_for_update()`` 把两个提交在**同一个事务里**串起来：后到的那个
    一直等到前一个提交完，锁释放时重新读到 COMMITTED，
    ``importer.commit`` 抛的 ValueError 由下面的 except 翻成 400
    「该导入批次已是「COMMITTED」状态，不能重复提交」。锁一直持有到
    ``importer.commit`` 内部的 ``db.commit()``（失败路径是 ``db.rollback()``），
    正是本路由稍后提交的那一个事务 —— 锁提前释放或释放得太晚都不对。

    与 ``periods.py`` / ``organize.py`` 的写法一致：先锁后算。
    """
    batch = db.scalar(
        select(ImportBatch)
        .where(ImportBatch.id == batch_id, ImportBatch.book_id == book.id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(404, "导入批次不存在")
    try:
        result = svc_import.commit(db, batch, user)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    batch.message = result["message"]
    db.commit()
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return result


@router.post("/import/{batch_id}/discard")
def discard_import(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "导入批次不存在")
    try:
        svc_import.discard(db, batch)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ===================================================================== 重排分录顺序




@router.get("/next")
def next_voucher(period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                 user: SysUser = Depends(get_current_user)):
    """新开一张时页面上要预填的两个值：下一个凭证号、这个期间最后一张的日期。

    号码直接调 `svc.next_voucher_no` —— **跟落库时是同一个函数**。
    预览号和实际发号必须是同一套口径，所以宁可去后端问，也不在这儿照着列表
    猜一个「最大号 +1」（期间里号是可以回收复用的，猜错就是白填一次）。

    **凭证字也必须是同一个口径**（审计报告 §3.18）：这个期间在用的凭证字
    未必是「记」（Excel 导入的用友凭证是「银」），落库时走
    ``organize.period_prefix`` 取那个字，预览这里也必须取同一个 ——
    否则页面上预填「记0001」、存下去却是「银0004」，用户看到的与存进去的
    不是一回事；而真按预填的号去改，客户端又不采信手工号。
    """
    period = db.get(Period, period_id) if period_id else find_period(db, book.id, date.today())
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    last = db.scalar(
        select(func.max(Voucher.voucher_date))
        .where(Voucher.book_id == book.id, Voucher.period_id == period.id))
    return {
        "voucher_no": svc.next_voucher_no(
            db, period,
            prefix=svc_organize.period_prefix(db, period, fallback="记")),
        "voucher_date": last.isoformat() if last else None,
        "period_id": period.id,
        "locked": period.locked,
    }


@router.get("/insert-preview")
def insert_preview(after_id: int, position: str = "before",
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    """「插入凭证」/「增加凭证」页面要预填的东西：插在谁前面或后面、这张拿哪个号、谁要顺位。

    跟 `/vouchers/next` 一个道理 —— 号码口径只有后端一份，页面自己猜就一定会猜错。
    这里只算不改：顺延跟建凭证在同一个事务里，空着退出不会顶走谁的号。

    ``position`` ：``before`` = 向前插入（占参照自己的号），``after`` = 向后插入
    （拿参照号 +1，只有更大的号顺位）。
    """
    v = db.get(Voucher, after_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "参照凭证不存在")
    period = db.get(Period, v.period_id)
    try:
        slot = svc.insert_slot(db, v, position)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e))
    return {
        "anchor_id": v.id,
        "anchor_no": v.voucher_no,
        "position": slot["position"],
        "voucher_no": slot["no"],
        "voucher_date": v.voucher_date.isoformat(),
        "period_id": v.period_id,
        # **这张新凭证要落的那一年度**：插在参照凭证那一期里，
        # 科目就得挂在那一年度的科目表上（前端据此取那张表）。
        "year": period.year if period is not None else None,
        "locked": bool(period and period.locked),
        "anchor_posted": v.status == "POSTED",
        "moved": slot["moved"],
    }


@router.get("/period-check")
def period_check(period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    """某个期间的「整理就绪」体检：断号 + 时序。全程只读。

    「断号检查」不再是整理凭证里的一个选项了 —— 用户不该为了看一眼有没有断号
    先想明白该选哪个「方式」。这里把体检单独开一个口：凭证列表页顶上说一声，
    整理凭证页拿它决定「按当前顺序重新编号」能不能用。
    """
    period = db.get(Period, period_id) if period_id else find_period(db, book.id, date.today())
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    return svc_organize.check_period(db, period)


def _filtered_scope(db: Session, book_id: int, *, keyword: Optional[str] = None,
                    min_amount: Optional[Decimal] = None,
                    max_amount: Optional[Decimal] = None) -> tuple:
    """``neighbour`` 的筛选条件，与 ``svc.query_vouchers`` 的同名参数**逐条对齐**。

    对齐是硬要求：这里翻的是「列表筛出来的那个结果集」，两处口径一旦漂移，
    用户在列表里看到的顺序和「前一张/后一张」跳过去的就不一样。
    测试 ``tests/test_perf_api_layer.py`` 里有一条按 ``query_vouchers`` 的
    count 对齐 ``total`` 的用例盯着这件事。

    （口径相同不等于共用函数：``query_vouchers`` 还会拼排序与分页，
    这里只需要 WHERE 条件。改动 ``query_vouchers`` 的筛选时两处要一起看。）
    """
    conds = [Voucher.book_id == book_id]
    if keyword:
        like = f"%{keyword}%"
        conds.append(or_(
            Voucher.voucher_no.ilike(like),
            Voucher.remark.ilike(like),
            Voucher.id.in_(select(Entry.voucher_id).where(Entry.summary.ilike(like))),
        ))
    if min_amount is not None or max_amount is not None:
        # 「金额」筛的是**某一条分录**的金额，不是整张凭证的合计（与列表一致）。
        amt = select(Entry.voucher_id).where(Entry.book_id == book_id)
        if min_amount is not None:
            amt = amt.where(Entry.amount >= money(min_amount))
        if max_amount is not None:
            amt = amt.where(Entry.amount <= money(max_amount))
        conds.append(Voucher.id.in_(amt))
    return tuple(conds)


@router.get("/{voucher_id}/neighbour")
def neighbour(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user),
              keyword: Optional[str] = None, min_amount: Optional[Decimal] = None,
              max_amount: Optional[Decimal] = None):
    """前一张 / 后一张 —— 按**凭证号顺序**在列表那个结果集里前后挪。

    顺序键是 ``(期间先后, 号段数字, id)``，跟列表「凭证号顺序」那一档
    **完全同一套**（``svc.order_key``）：

      * 同一张期间内：号小的在前，号大的在后 —— 记005 的「前一张」是记004；
      * 跨期间：月份大的在后 —— 12 月那张的「前一张」是 11 月的最后一张。

    用户 2026-10-24 的原话：「同一个月里，凭证号大的在后，点击前一张，
    应该跳到凭证号小一号的」「不同月里，月份越大的越后」。
    这里原来按 ``(日期, id)`` 排，于是同一月里**日期靠前的那张被当成了第一张**：
    号是 记003 的那张日期比 记001 还早，点「前一张」就跳去了号更大的那边 ——
    顺序跟用户在列表里看到的完全对不上。

    **翻的范围就是列表给用户看的那一批**，跟列表页那条规则同源
    （``VoucherList.vue`` 的 ``hasFilter``）：

      * **带筛选条件**（凭证号/摘要、分录金额区间）→ 在**筛选结果集**里挪，
        集合可以跨月跨年（筛「凭证号含 16」每月一张时，8 月那张的前一张就是 7 月那张）；
      * **不带筛选条件** → 范围是**这张凭证所属的那个月**，到月底就停住，
        「后一张」变灰。

    这里原来不带条件时范围是**全账套**、「不在月份边界上停住」，理由是
    「停住等于把一条顺序劈成两半，跨月那一下反而不连续」—— 那条理由自己站不住：
    跟列表页脱节的那一刻顺序就已经劈成两半了（列表停在 12 月，翻却能翻到 1 月），
    用户在列表里看到的和翻到的对不上。2026-10-25 用户打回：
    「某月最后一张凭证点下一张，居然能跳后一个月第一张凭证。这不对」。
    列表页早就是这么干的（「一个筛选条件都没填 → 就看顶栏定下的那个月；
    填了任一条件 → 放宽到全部期间」），前后张只是没跟上。

    全程只读，只发 4 条业务 SQL：一条取「这张 + 它的顺序位置」、一次聚合问清
    「结果集多大、这张在不在里面、它前面有几个」，前后各一条 ``LIMIT 1`` 取邻票。
    """
    # 一条查询把「这张凭证」和「它在顺序键里的位置」一起取回来：
    # 凭证归属（校验是不是这本账的）、所属期间、期间 ord、号段数字。
    # 合成一条是因为这里要守住「一次「前一张/后一张」只发 4 条业务查询」
    # （tests/test_perf_api_layer.py）—— 拆成「取凭证 + 取期间 + 取号段」三条就变 6 条了。
    #
    # ``v_period_id`` 一并取回来：范围收敛到本月时要拿它当过滤条件，
    # 在这里搭车取比再发一条查询便宜。
    row = db.execute(
        select(
            Voucher.book_id.label("book_id"),
            Voucher.period_id.label("v_period_id"),
            Period.ord.label("p_ord"),
            svc.no_seq().label("p_seq"),
        ).select_from(Voucher).join(Period, Voucher.period_id == Period.id)
        .where(Voucher.id == voucher_id)
    ).first()
    if row is None or row.book_id != book.id:
        raise HTTPException(404, "凭证不存在")

    # 这张自己在顺序键里的位置：期间 ord + 号段数字。
    # 两个值都问数据库要，不在 Python 里抠 —— 抠数字的判据只有一处
    # （``svc.no_seq``），Python 再写一份早晚会跟它分道扬镳。
    v_ord, v_seq = row.p_ord, row.p_seq
    if v_ord is None:
        # 期间行不在（正常不会发生：period_id 有外键约束）。没有先后可言，
        # 两头都不指 —— 总比指一张错的好。
        return {"prev_id": None, "next_id": None, "scope": "period",
                "index": -1, "total": 0}

    key = svc.order_key()
    cur = tuple_(literal(v_ord), literal(v_seq), literal(voucher_id))

    if keyword or min_amount is not None or max_amount is not None:
        conds = list(_filtered_scope(
            db, book.id, keyword=keyword, min_amount=min_amount, max_amount=max_amount))
        scope = "filtered"
    else:
        # 不带条件 → 范围是**这张所属的那个月**，跟列表页「没填条件只看顶栏
        # 那个月」那条规则一致。月内最后一张的「后一张」到此为止（= None）。
        #
        # 用 ``period_id`` 而不是「期间 ord」当条件：ord 在账套内唯一，
        # 两者指向同一行，但 period_id 是凭证上本来就有的外键列，
        # 走它能直接命中 ``ix_voucher_period_no`` 那条索引的前缀。
        conds = [Voucher.book_id == book.id, Voucher.period_id == row.v_period_id]
        scope = "period"

    # 一次聚合问清三件事：结果集多大、这张在不在里面、它前面有几个。
    # 「在不在里面」不能拿「前后都查不到」来代替 —— 结果集的第一张就是
    # prev=None 却 index=0，靠「查不到邻票」判断会把它误判成不在结果集里。
    total, hits, before = db.execute(
        select(
            func.count(),
            func.count().filter(Voucher.id == voucher_id),
            func.count().filter(key < cur),
        ).select_from(Voucher).join(Period, Voucher.period_id == Period.id).where(*conds)).one()
    if not hits:                          # 这张不在结果集里（比如筛选条件刚改过）
        return {"prev_id": None, "next_id": None, "scope": scope,
                "index": -1, "total": total}

    # 选 **id** 而不是整行：``db.scalar()`` 对 ORM 实体查询返回的是那个实体本身
    # （不是它的 id），返回体里会塞进一整个 Voucher 对象。
    base = select(Voucher.id).join(Period, Voucher.period_id == Period.id).where(*conds)
    prev_id = db.scalar(
        base.where(key < cur)
        .order_by(Period.ord.desc(), svc.no_seq().desc(), Voucher.id.desc()).limit(1))
    next_id = db.scalar(
        base.where(key > cur)
        .order_by(Period.ord.asc(), svc.no_seq().asc(), Voucher.id.asc()).limit(1))
    # index 是「这套顺序里的位置」（0 基）—— 跟列表「凭证号顺序」是同一个口径。
    return {"prev_id": prev_id, "next_id": next_id, "scope": scope,
            "index": before, "total": total}


# ===================================================================== 拆分凭证


class SplitIn(BaseModel):
    """拆分的入参。

    ``at_line`` = 第一张凭证保留到第几条分录（1 基）。也就是「在第 N 条分录
    之后切一刀」——前半张留在原凭证，后半张另起一张新凭证。

    下限 1 在这里挡；上限（分录数 - 1）只有服务端知道，由服务层判。
    """
    at_line: int = Field(ge=1)


@router.get("/{voucher_id}/split-preview")
def split_preview(voucher_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db),
                  _link: UserBook = Depends(require_book_write),
                  user: SysUser = Depends(get_current_user)):
    """拆分的预览：所有候选分界处、两半各自平不平、拆完各拿哪个号。**只算不改**。

    跟 ``/insert-preview``、``/organize/preview`` 同一个道理：号码口径只有
    后端一份，页面自己猜就一定会猜错；而拆分会改掉原凭证及后面同期的号，
    先看清再确认。
    """
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    try:
        return svc.split_preview(db, v)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e))


@router.post("/{voucher_id}/split")
def split(voucher_id: int, payload: SplitIn, book: Book = Depends(get_my_book),
          db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
          user: SysUser = Depends(get_current_user)):
    """在第 ``at_line`` 条分录之后把一张凭证拆成两张。

    两张各自必须借贷平衡；有一张不平就拆不成（400，且把两边的差额说清楚）。
    **原凭证继承它自己的号**，新凭证往后拿一个（= 原号 +1），后面同期的号顺延 +1。
    """
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    try:
        result = svc.split_voucher(db, v, user, payload.at_line)
    except (ValueError, ArithmeticError) as e:
        db.rollback()
        raise HTTPException(400, str(e))
    subs = _book_subjects(db, book.id)
    # 两张的期间一次取好（一条查询），年度随凭证一起出去
    periods = _book_periods(db, book.id,
                            {result["original"].period_id, result["created"].period_id})
    return {
        "original": _serialize(db, result["original"], subs, periods, version=True),
        "created": _serialize(db, result["created"], subs, periods, version=True),
        "moved": result["moved"],
    }


@router.get("/{voucher_id}")
def detail(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    # 期间一次取好（一条查询），年度随凭证一起出去（见 _serialize 的说明）
    return _serialize(db, v, periods=_book_periods(db, book.id, {v.period_id}),
                               version=True)
