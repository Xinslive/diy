"""分级：每天一次、每周一次、每月一次，并决定这份产物按哪档保留。

**规则一句话：每次运行都出一份产物，这一份归到「它够格的最高那一档」。**

    月份里还没成功过月度  -> monthly（永久）
    本周里还没成功过周备份 -> weekly （1 年）
    其余                 -> daily  （30 天）

为什么是「够格的最高一档」而不是「三份各来一次」。经典 GFS 会把日/周/月当成
三次独立备份，各跑一遍。对本系统那是三倍的 dump、三倍的传输，而**信息量为零** ——
同一时刻的库状态，导三次完全一样。于是取最高档：月度那次把那个月整月兜住了
（本来就永久保留），不需要同一天再补一份 30 天后被删的日备份。

**判重按「成功过」而不是「跑过」。** 一次失败的月度备份不占月度名额 ——
判重只看 ``status='success'``。否则月初那次上传超时就再也不会被重试到，
那个月将永远没有月度备份，而界面上一行成功记录都没有，看着像配好了。

**时区。** 「月份」「周」是**业务日历**的分界，不是时间点上的一个瞬间：
一个 8 月 31 日 23:50 的备份，归的是 8 月还是 9 月，取决于公司用哪个日历。
这里显式用东八区，**不依赖部署机的 TZ** —— 代码库其余部分一律 UTC（见
``app/core/config.py`` 里被删掉的 ``default_timezone`` 那段说明），那里是对的，
因为那是「什么时候发生的瞬间」；而这里是「这是哪个月的账」，跟服务器漂在哪
个时区不该有关系。用 ``TZ=UTC`` 的机器跑，会在每月 1 日凌晨 8 点之前把月初那
几个小时的备份归到上个月去。

（这条**不**恢复成一个配置项：配置项正是当年被删掉的那个 —— 定义了但没人
设、或有人设了却没人记得它影响什么。一个只在这一处用到的常量，注释写清楚
就够了。）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (STATUS_SUCCESS, TIER_DAILY, TIER_MONTHLY, TIER_WEEKLY, BackupRun)

# 「这是哪个月的账」用的业务日历。见模块说明为什么它不跟随部署机的 TZ。
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def local_now(now_utc: Optional[datetime] = None) -> datetime:
    """把「此刻」换算到业务日历时区（naive）。"""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(BUSINESS_TZ).replace(tzinfo=None)


def period_start(tier: str, moment: datetime) -> datetime:
    """该 tier 的「本周期」起点（业务日历、naive）。

    周的起点取**周一**：财务口径以周一开始（ISO-8601），与
    ``date.isocalendar()`` 一致。周日起算也能自洽，但一跨年就说清了：
    12 月 29 日（周日起算的第 1 天）后面跟着的 1 月 1 日已经是下一年的第 1 周，
    于是「年终最后一周」和「年初第一周」会共用一次周备份 —— 年底那几天反倒退化成
    天备份。周一起算没有这个缝。
    """
    if tier == TIER_MONTHLY:
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if tier == TIER_WEEKLY:
        start_of_week = moment - timedelta(days=moment.weekday())
        return start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _book_filter(book_id: Optional[int]):
    """账套筛选。``None`` 必须走 ``IS NULL`` 而不是 ``== NULL``。

    后者会被 SQLAlchemy 渲染成 ``book_id = NULL``，恒不成立，于是系统级备份
    的判重永远看到「没有任何记录」，于是一个月里每个周期都被判成「还没成功过」
    —— 月度档每天都被判成新的一档，永久保留的对象一天一个。
    """
    return BackupRun.book_id.is_(None) if book_id is None else BackupRun.book_id == book_id


def succeeded_since(db: Session, scope: str, book_id: Optional[int],
                    tier: str, since: datetime) -> bool:
    """本周期内该 (scope, book_id, tier) 是否**成功**过。"""
    since_utc = since.replace(tzinfo=BUSINESS_TZ).astimezone(timezone.utc)
    found = db.scalar(
        select(func.count())
        .select_from(BackupRun)
        .where(
            BackupRun.scope == scope,
            _book_filter(book_id),
            BackupRun.tier == tier,
            BackupRun.status == STATUS_SUCCESS,
            BackupRun.created_at >= since_utc,
        )
    )
    return bool(found)


def classify(db: Session, scope: str, book_id: Optional[int],
             now_utc: Optional[datetime] = None) -> str:
    """这一份产物该归到哪一档。见模块开头的规则。

    **判「成功过」用的是当前时刻，而不是补跑时刻之前。** 手动补跑一次 3 个月前
    的备份不该被当成「那 3 个月已经备份过了」—— 但也正因如此，手动补跑在今天
    只会得到一份 daily：今天已经有产物了。这是刻意的：**分级描述的是「这个周期
    的兜底有没有到位」，不是「能不能往回补历史」**。往回补的需求由
    ``--force-tier`` 参数显式表达，不靠猜。
    """
    moment = local_now(now_utc)
    for tier in (TIER_MONTHLY, TIER_WEEKLY):
        if not succeeded_since(db, scope, book_id, tier, period_start(tier, moment)):
            return tier
    return TIER_DAILY


def scope_dir(scope: str, book_id: Optional[int]) -> str:
    """对象键里的作用域段。

    **纯 ASCII。** 不把公司名/账套名写进键：非 ASCII 键在 OSS 上是合法的，
    但它在浏览器控制台、``ossutil``、各种第三方工具和某些 SDK 的日志里反复出问题
    （编码不一致时看起来像乱码，甚至被当成不同的 key）。可读的名字放在快照内容
    和 ``sys_backup_run.book_label`` 里 —— 要看是哪个账套，界面上一行就够了，
    不用在对象键里找。
    """
    return "system" if book_id is None else f"book-{book_id}"


def stamp(moment: datetime) -> str:
    """对象键里的时间戳。统一形状，不按档位变 —— 变了就多一条要测的分支。"""
    return moment.strftime("%Y%m%d-%H%M%S")


def object_key(prefix: str, tier: str, scope: str, book_id: Optional[int],
               moment: datetime) -> str:
    """拼出产物对象键。

        acct-backup/monthly/book-3/20261001-031700.json.gz

    **tier 必须是第一层目录**：``retention.py`` 的清理完全靠前缀分隔，
    「按档位删」和「按目录删」是同一件事。所以这里不接受一个拼好的完整前缀 ——
    那样将来某个人改目录布局时，清理会跟着改到别的地方去。
    """
    parts = [p for p in ((prefix or "").strip("/"), tier, scope_dir(scope, book_id)) if p]
    return "/".join(parts + [f"{stamp(moment)}.json.gz"])


def tier_prefix(prefix: str, tier: str) -> str:
    parts = [p for p in ((prefix or "").strip("/"), tier) if p]
    return "/".join(parts) + "/"
