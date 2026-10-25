"""幂等键：把「同一个请求重发」变成「同一个结果」。

审计 §3.28：新建凭证的 POST 没有幂等键 —— 请求发出去了、响应在网络上丢了，
客户端不知道自己那一次到底成没成，于是再发一次，库里就多出一张几乎一样的凭证。

这张表记的是**请求占位**，不是业务数据：

  · 一行 = 一个 (账套, 幂等键) 的占位；
  · **谁先抢到这行，谁负责把这次请求做完**（抢 = 一次
    ``INSERT … ON CONFLICT DO NOTHING``）；
  · 做完之后把 ``voucher_id`` 填上 —— 后来的同键请求直接拿这张凭证的响应回去。

**为什么占位与凭证在同一个事务里。** 这是整套去重语义的关键：

  · 第一个请求提交 → 占位行与凭证**一起**可见 → 后来的同键请求看到
    ``voucher_id``，返回那张凭证，**不重复创建**；
  · 第一个请求失败回滚 → 占位行**跟着没了** → 后来的同键请求抢到占位，
    正常创建 —— **失败不会把键「烧掉」**。

如果占位行单独提交，第一个请求失败时键就被烧掉了：后来的重试拿到一个
「已完成但没有结果」的键，只能报错 —— 而失败恰恰是最需要重试的时候。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class VoucherIdempotency(Base):
    """一次「新建凭证」请求的幂等占位。

    ``voucher_id`` 为空表示「这次请求还没做完」—— 正常情况下看不到这种行
    （占位与凭证同事务提交），它只在**并发重试**的那一瞬间存在：
    后到的请求会等在 ``INSERT … ON CONFLICT`` 上，直到先到的那次提交或回滚。
    """

    __tablename__ = "voucher_idempotency"
    __table_args__ = (
        # 同一账套内一把键只能占一行 —— 这条唯一约束就是去重的全部机制。
        Index("uq_voucher_idempotency_book_key", "book_id", "idem_key", unique=True),
        # 清理用：占位行只活到凭证被删为止，按 created_at 兜底清残留。
        Index("ix_voucher_idempotency_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    #: 客户端给的幂等键。长度上限 128（库里是 varchar(128)，超长直接被拒）。
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 请求体的 sha256 —— 只用于**分辨「同一把键配了不同内容」**，
    #: 不参与去重判定（去重只看键）。
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 这次请求建出来的那张凭证。
    #:
    #: ``ondelete="CASCADE"``：凭证没了，这把键也就没有意义了 ——
    #: 留着它只会让「同一个键」永远指向一张不存在的凭证。
    voucher_id: Mapped[int | None] = mapped_column(
        ForeignKey("voucher.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
