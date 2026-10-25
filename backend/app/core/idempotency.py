"""幂等键的抢与交。

审计 §3.28：新建凭证的 POST 没有幂等键 —— 请求发出去了、响应在网络上丢了，
客户端不知道自己那一次到底成没成，于是再发一次，库里就多出一张几乎一样的凭证。

**这里只做两件事**：

1. :func:`claim` —— **抢键**。抢到的那个请求负责把这次创建做完；
   没抢到的那个，说明已经有人做过了，它拿既有结果回去。
2. :func:`complete` —— **交结果**。把建出来的凭证 id 填回占位行。

**为什么是「抢」而不是「查了再写」**：查了再写有两次请求都查不到、
于是都去创建的时间窗 —— 库里就多一张。「查了再写」还得靠锁把那个窗关掉，
而锁的范围（整个创建过程）长到不合适。抢键只靠一条唯一约束，
**胜负在数据库里当场分出来**，不依赖应用层的时序。

**为什么占位行与凭证同事务**：见 ``app/models/idempotency.py`` 的说明。
一句话：请求失败时占位跟着回滚，键不会被「烧掉」—— 失败恰恰是最需要重试的时候。
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.idempotency import VoucherIdempotency

#: 幂等键的合法形状：1~128 个字母、数字、``._:-``。
#:
#: 前端生成的是 ``vch-<uuid>``（见 ``VoucherEdit.vue::newIdempotencyKey``）。
#: 限制形状是为了让这张表**不可能**被塞进任意长度的文本 ——
#: ``idem_key`` 是 ``varchar(128)``，来一个 10KB 的头会被数据库截断或报错，
#: 截断之后两把不同的键可能撞成同一把（那就把去重变成了误杀）。
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class InvalidIdempotencyKey(ValueError):
    """幂等键不合法 —— 调用方应当回 400。"""


def validate_key(key: str) -> str:
    """校验并归一化幂等键。"""
    normalized = (key or "").strip()
    if not KEY_PATTERN.match(normalized):
        raise InvalidIdempotencyKey("幂等键格式不合法（只允许字母、数字与 . _ : -，最长 128）")
    return normalized


def request_hash(payload) -> str:
    """请求体的 sha256。

    **它不参与去重判定**（去重只看键）—— 它只用来分辨
    「同一把键配了不同内容」这种情况，好让日志里能看出来。
    """
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim(db: Session, *, book_id: int, key: str, request_hash: str
          ) -> tuple[bool, VoucherIdempotency]:
    """抢一把幂等键。

    返回 ``(是不是我抢到的, 占位行)``：

    · ``(True, row)`` —— 键是新的，这一行归你；
      **记得在同一个事务里**把它建出来的凭证 id 填回去（:func:`complete`）。
    · ``(False, row)`` —— 键已经有人用过了，``row.voucher_id`` 就是那次的结果。

    **并发下会发生什么**：两个请求拿同一把键同时到，
    后到的那个 ``INSERT`` 会**等**在先到的那次事务上 ——
    先到的提交，它就拿到 0 行（键已存在）；先到的回滚，它就抢到。
    两种结果都是对的：**库里永远只有一张凭证**。

    注意这里没有用 ORM 的 ``add()``：那一套会先 SELECT 再 INSERT，
    那个 SELECT-INSERT 之间的窗就是重复创建的来源。
    """
    stmt = (
        pg_insert(VoucherIdempotency)
        .values(book_id=book_id, idem_key=key, request_hash=request_hash)
        .on_conflict_do_nothing(index_elements=["book_id", "idem_key"])
        .returning(VoucherIdempotency.id)
    )
    row_id = db.execute(stmt).scalar_one_or_none()
    if row_id is not None:
        # 抢到了。把这一行取出来交给调用方（它要在同一个事务里填 voucher_id）。
        row = db.get(VoucherIdempotency, row_id)
        return True, row
    # 没抢到 —— 键已经存在。上面那个 INSERT 等过了对方的事务，
    # 所以这一行现在一定看得见。
    row = db.execute(
        select(VoucherIdempotency).where(
            VoucherIdempotency.book_id == book_id,
            VoucherIdempotency.idem_key == key,
        )
    ).scalar_one_or_none()
    if row is None:      # pragma: no cover - 只可能在对端事务回滚时发生
        # 对端回滚了 → 键应该重新可用。这里再抢一次。
        db.execute(
            pg_insert(VoucherIdempotency)
            .values(book_id=book_id, idem_key=key, request_hash=request_hash)
            .on_conflict_do_nothing(index_elements=["book_id", "idem_key"])
            .returning(VoucherIdempotency.id)
        )
        row = db.execute(
            select(VoucherIdempotency).where(
                VoucherIdempotency.book_id == book_id,
                VoucherIdempotency.idem_key == key,
            )
        ).scalar_one_or_none()
        if row is None:      # pragma: no cover
            raise RuntimeError("幂等键占位行既抢不到也读不到 —— 数据层状态异常")
        return True, row
    return False, row


def complete(db: Session, row: VoucherIdempotency, voucher_id: int) -> None:
    """把建出来的凭证 id 填回占位行。

    用 Core 的 UPDATE 而不是改 ORM 属性：占位行是用 ``INSERT … RETURNING``
    插进去的，ORM 并不认为它是「一个待持久化的对象」，
    改它的属性会走 INSERT 路径 —— 那会把这一行插第二遍。
    """
    db.execute(
        update(VoucherIdempotency)
        .where(VoucherIdempotency.id == row.id)
        .values(voucher_id=voucher_id)
    )
