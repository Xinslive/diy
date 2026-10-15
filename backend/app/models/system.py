"""系统域：用户、账套授权。

没有「公司」这一层：公司名是账套的一个文本属性，见 ``app.models.book.Book``。
"""
from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SysUser(Base, TimestampMixin):
    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    real_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 管理员标记。原来靠「用户名恰好叫 admin」来判断，测试里造不出管理员，
    # 而且用户名是能改的 —— 权限判据必须存在字段里。
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 登录失败计数与锁定截止时间（审计 P3-04 建议 4：登录接口增加限流与失败次数锁定）。
    #
    # 放**账号**上而不是内存里：多进程部署时每个进程各有一份内存计数，
    # 攻击者打满 N 个进程就能把 N 次额度用满，等于没锁。放内存还有一个更麻烦的问题 ——
    # 重启即清零，于是「慢速爆破」（每秒一次，攒够就停）根本不会触发阈值。
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # 令牌版本号（审计 P2-01）。
    #
    # JWT 是自签名的，服务端不存会话，所以「重置口令」过去只能改
    # ``password_hash`` —— 已经发出去的 bearer token 一律照常有效，直到自然过期
    # （普通用户 720 分钟）。口令泄露之后管理员把口令重置了，攻击者手里的
    # 旧令牌还能继续读写账套，最长还能用半天。
    #
    # 解法就这一个整数：签发时写进载荷，鉴权时与库里比一次，不一致就拒。
    # 撤销 = ``token_version += 1``，于是该账号**所有**在用令牌一次性作废，
    # 代价是这个人要重新登录一次 —— 这正是「改口令」该有的效果。
    #
    # 放在账号上而不是内存集合里：内存集合在多进程部署下每个进程各有一份，
    # 撤销只撤销了其中一份；重启即清零，等于没实现。
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")


class LoginIpThrottle(Base, TimestampMixin):
    """登录失败的 **IP 维度** 聚合状态（审计 P2-11 的另一半）。

    ``SysUser.failed_logins`` 只回答「这个账号被打了多少次」。而轮换用户名的
    攻击源压根不碰同一个账号两次：它对上万个账号各试 5 次，每次只锁一个账号，
    锁定机制几乎不生效，事后在审计里看到的也只是一堆互不相干的
    ``LOGIN_FAILURE`` —— 没有任何一行能说清「这些来自同一个源」。

    这张表就是那个「源」：每个来源 IP 一行，记滑动窗口内的失败总数。

    放库里而不是内存里的理由与 ``SysUser.failed_logins`` 完全相同，且更要紧：
    内存计数在多进程部署下每个进程各有一份（打满 N 个进程就有 N 次额度），
    而重启即清零（「每秒试一次、攒够就停」的慢速爆破根本触发不了阈值）。

    **它只是一张节流计分板，不是锁。** 超阈值不会写 ``locked_until``、
    不会改任何账号 —— 原因见 ``app/api/auth.py`` 里「为什么不做 IP 锁死」
    那一段：NAT 后面几百人共用一个出口 IP，按 IP 硬锁是把他们一起关在门外，
    而且这个动作**攻击者可以主动触发**（从公司网络里打满阈值即可让整个公司
    一段时间登不进来）。要开 IP 硬锁得显式置 ``LOGIN_IP_HARD_LOCK=true``。

    时间口径：三个时间列都是 **naive UTC**，与 ``SysUser.locked_until`` 一致，
    Python 侧一律走 ``auth.py::_utcnow()``。混用 aware/naive 会让窗口
    在东八区部署时凭空差 8 小时。
    """

    __tablename__ = "login_ip_throttle"
    __table_args__ = (
        # 唯一约束是这张表的全部并发正确性基础：ON CONFLICT (ip) 靠它把
        # 「读计数 + 加一」压成一条原子语句（auth.py::_bump_ip_failure）。
        # 少了它就只能先 SELECT 再 UPDATE，两个并发请求会各读到同一个旧值，
        # 于是失败数只涨 1 —— 攻击者并发打就把计数废掉了。
        UniqueConstraint("ip", name="uq_login_ip_throttle_ip"),
        # 清理入口（purge_expired_login_ip_state）按 last_failed_at 扫，
        # 没有索引就是每次清理一次全表扫，而清理是在登录路径上顺带做的。
        Index("ix_login_ip_throttle_last_failed", "last_failed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 45 = IPv6 最长文本长度。刻意不存 inet 类型：那会让本项目其余部分
    # （审计表里那个 String(45) 的 ip 字段）出现两种 IP 表达，比较时必踩坑。
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    # 窗口内的失败总数。
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    # 窗口起点（naive UTC）。窗口是**滑动**的：只有在连续 window 秒没有失败时
    # 才整体清零（见 auth.py::_bump_ip_failure），所以攻击持续时计数只涨不落。
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 仅在显式开启 LOGIN_IP_HARD_LOCK 时才会被写。默认永远为 NULL ——
    # 见本类 docstring 里「为什么不做 IP 锁死」。
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class SystemSetting(Base, TimestampMixin):
    """系统级键值配置 —— 目前只放第三方服务的凭据与端点。

    与 ``InvoiceConfig`` 那类账套配置分开：这里的东西**全系统一份**。PaddleOCR
    的服务账号不是按账套分的，换一本账也不该换一套 token。

    为什么要从 .env 搬进库：改一个 token 要重启后端、要改部署机上的文件、
    还要有人会 ssh 上去。凭据是会过期、要轮换的东西，让它只能靠重启来改，
    结果就是没人改 —— 于是扫描件这条路在某天悄悄失效，而配置「明明配过」。
    搬进库之后，管理员在界面上改完即生效，且留下一条「谁改的」。

    **关于明文存储。** token 必须能还原成原文才能去调对方接口，所以哈希掉没有
    用（对比 ``SysUser.password_hash``：那个只需要比对，不需要还原）。这里等价于
    .env 文件本身 —— 能读到这个数据库的人，本来也能读到那个文件。真正的防线是
    **不往外发**：接口只回 ``token_set`` 与末 4 位，明文一律不出现在任何响应里
    （见 ``app/api/system.py``）。

    没有引入加密库是刻意的：为一个 token 拉进 ``cryptography`` 这种重量级依赖，
    加密密钥最终仍得存在某处，威胁面没变小，复杂度却实打实涨了。真要多一层，
    正确做法是 KMS / secret manager，那是部署形态的决定，不是加个依赖。
    """

    __tablename__ = "sys_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 谁最后改的。凭据类配置必须能追溯到人：token 外泄时要查的不只是
    # 「现在配的是谁的」，还有「谁把它换掉的、什么时候换的」。
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class UserBook(Base, TimestampMixin):
    """账套授权。role 决定该用户在此账套内的权限范围。

    授权直接挂在账套上，没有公司这一层：同一家公司开多个账套（租金 / 管理费）时，
    每个账套各授权一次即可。
    """

    __tablename__ = "sys_user_book"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_user_book"),
        # 角色白名单下沉到数据库（审计 P2-12）。
        #
        # 应用层已经改成白名单语义（core/deps.py::is_read_only），但那只挡
        # 「走 API 的写入者」。CHECK 约束挡的是另一条路：绕过 API 直接改库的
        # 写入者 —— 那条路上根本没有应用层，原来 ``role == "VIEWER"`` 的
        # 字面量比较在那里形同虚设，写个 'viewer' 就拿到完整写权限。
        #
        # 约束名与表达式必须与迁移 `0005_db_guardrails`（经 db_init.sql 交付）
        # **逐字一致**：两边分成两套名字的话，Alembic 下次 autogenerate 会
        # 同时报「库里有库里没有的」和「模型有库里没有的」，把一次改名
        # 伪装成两条变更。存量脏数据由该迁移按 NOT VALID 挂上去，
        # 这里只声明结构、不重复处理那批数据。
        #
        # 大小写归一化交给应用层（deps.normalize_role）做：库里若出现
        # 'viewer' 这种值，判定侧一律按只读处理（fail-closed），
        # 所以不必在库这一层再叠一条大小写约束。
        CheckConstraint(
            "role IN ('OWNER', 'ACCOUNTANT', 'VIEWER')",
            name="ck_sys_user_book_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # OWNER / ACCOUNTANT(可制单记账) / VIEWER(只读)
    role: Mapped[str] = mapped_column(String(16), default="ACCOUNTANT", nullable=False)

    user: Mapped["SysUser"] = relationship()
    book: Mapped["Book"] = relationship()
