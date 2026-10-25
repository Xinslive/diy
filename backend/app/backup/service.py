"""备份编排：谁该备份、备份成什么、传到哪里、传完清什么。

一次运行的形状::

    for 每个账套（+ 可选的系统数据）:
        定级  ->  导出并 gzip  ->  算 SHA256  ->  传 OSS  ->  落一行运行记录
    按档位清理过期产物（月度不参与）

**一个账套失败不阻断其它账套。** 传某个账套时 OSS 超时，不该让其余账套也
这一期不备份 —— 那样单点故障会变成全局停摆，而且失败的那一个还会在记录里
和成功的混在一起，看不出范围。所以逐个 try，每个都落记录。

**但全部失败要吵。** 返回值里如果一条成功都没有，调用方（API / 脚本）会以
非零退出。静默的「全灭」是最坏的形态：界面刷新一下还是绿的。

**清理失败不是备份失败。**
一次真实事故：24 个目标全部上传成功，界面上却是「整轮失败」。原因是清理阶段
抛了一个 ``AttributeError``（oss2 列表接口给的 ``last_modified`` 是 int 纪元秒，
而判龄按 datetime 用），它冒到 API 层被兜底成「后台任务异常，见服务端日志」。
形态是最坏的一种 —— **看起来失败了，其实成功了**，而真实原因一个字都没留下。
现在清理的异常就地捕获，进 ``RunResult.prune_error`` 与审计日志，备份的成败不受
它影响；详见 ``_run_backup_with_bucket`` 里那段注释。
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.backup import config as cfg_mod
from app.backup import export as export_mod
from app.backup import oss_client, retention, tier as tier_mod
from app.core.schema_version import BACKEND_ROOT
from app.models import (SCOPE_BOOK, SCOPE_SYSTEM, STATUS_FAILED, STATUS_SUCCESS,
                        BackupRun, Book)

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    runs: list[BackupRun]
    deleted: list[retention.Deleted]
    #: 清理失败时的原因，None = 清理正常跑完（或压根没要求清理）。
    #:
    #: **单独一个字段，而不是把整轮判成失败。** 清理失败与「数据有没有传上去」
    #: 是两件事：把前者算成后者，得到的记录是「备份失败了」，而 Bucket 里的
    #: 产物一份不差 —— 用户于是去重跑备份，而真实原因（清理里的一个异常）
    #: 仍然只躺在服务端日志里。这个字段是那件事**唯一的去处**。
    prune_error: Optional[str] = None
    #: **提前停止时还有几个目标没备份**（0 = 正常跑完）。
    #:
    #: 提前停止的原因是**租约丢了**（这个备份任务已被另一个 worker 接管）。
    #: 那不是「成功」—— 剩下的目标确实没备份，必须让人看得见。
    stopped: int = 0

    @property
    def succeeded(self) -> list[BackupRun]:
        return [r for r in self.runs if r.status == STATUS_SUCCESS]

    @property
    def failed(self) -> list[BackupRun]:
        return [r for r in self.runs if r.status == STATUS_FAILED]

    @property
    def ok(self) -> bool:
        return bool(self.succeeded)


def _book_label(book: Optional[Book]) -> str:
    if book is None:
        return "系统数据（用户、授权、参数、全局基线）"
    return f"{book.company_name} / {book.name}"


def _workdir() -> Path:
    """临时落盘目录。

    产物只在这里停留到上传完成，随后删掉 —— 库里所有账的全量数据在服务器上
    留一份明文，既多占磁盘，又把「数据只在 OSS 上」这件事变成假的。

    **绝不回退到 ``/tmp``**（2026-10-24 性能审计改）。

    为什么不能回退：``cwq-api.service`` 开着 ``PrivateTmp=true``，
    于是服务看到的 ``/tmp`` 是一个**私有的 tmpfs —— 那是内存盘**。
    全量账务备份（gzip 后的 JSON，压完仍可能是几十 MB）落进去 =
    **直接吃 API 的内存，而且永不被换出**。容量峰值时它就是压垮进程的那一下，
    而症状是「备份跑到一半服务变慢/被杀」，看起来与备份毫无关系。

    另一台生产机（``/opt/cwq`` 布局）的 ``/tmp`` 是真磁盘，回退过去只是
    「明文备份落在系统临时目录」这个卫生问题；同一台机器上还有另外 11 个
    子域，明文落在共享的 ``/tmp`` 里也是不该发生的事。

    **没配置时落在应用自己的 ``backend/data/backup-work``**，理由：

    * 它是**磁盘**，不在 ``PrivateTmp`` 的射程之内（那只影响 /tmp、/var/tmp、/run）；
    * 它在 unit 的 ``ReadWritePaths=`` 白名单里（``/opt/cwq/backend/data``），
      一定可写；
    * 它已经是发票等运行时文件待的地方（``INVOICE_STORAGE_DIR=data/invoices``），
      权限与归属口径一致。

    显式配 ``CWQ_BACKUP_WORK_DIR`` 可以把它挪到别处（比如运维单独给的
    ``/www/wwwroot/acct.xins.live/backups/work``）—— 那是**推荐**做法，
    但缺省值本身是安全的，不该让「没配」变成「备份跑不起来」。
    """
    override = os.environ.get("CWQ_BACKUP_WORK_DIR", "").strip()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    p = Path(BACKEND_ROOT) / "data" / "backup-work"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _new_temp(workdir: Path, label: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="cwq-backup-", suffix=".json.gz", dir=workdir)
    os.close(fd)
    p = Path(name)
    # 备份里是全量账务数据（凭证、科目、金额）。显式收成 600，不猜调用方的 umask
    # —— 默认 022 会把它落成 644，服务器上任何本地账号都读得到。与
    # scripts/db_backup.py 的同一处处理是同一个理由。
    os.chmod(p, 0o600)
    return p


def _record(db: Session, run: BackupRun) -> None:
    db.add(run)
    db.commit()
    db.refresh(run)


def _prepare_upload(tmp: Path, workdir: Path, label: str) -> tuple[Path, str, int]:
    """把导出的 gzip 变成「**该上传的那份**」，返回 ``(路径, SHA256, 字节数)``。

    **配了密钥就加密，没配就照旧。** 加密用的是 AES-256-GCM（带认证），
    密钥来自 ``settings.backup_encryption_key``。

    * **返回的 SHA256 是密文的** —— OSS 上真正存着的那串字节。算明文的
      校验和会让恢复时的比对必然对不上（那是「校验和与明文不一致」的经典坑）。
    * **密钥为空 = 不加密**，但要**明确记一条告警**：默默保持明文而没人知道，
      比明文本身更危险。
    * **密钥必须与 OSS AccessKey 分开存放**（见
      ``export.encryption_key`` 的说明）：两者的爆炸半径不一样。

    加密后的文件落在同一个临时目录里，上传完就删（临时目录里不留明文）。

    **「密钥为空」这条分支现在只有一条路能走到**：
    ``run_backup`` 开头那道 ``require_encrypted_ready`` 已经拒掉了
    「配了 OSS 却没配加密密钥」的情形（界面配 OSS 的那条路也在内）。
    走到这里说明要么密钥是好的，要么运维显式开了
    ``BACKUP_ENCRYPTION_KEY_WARN_ONLY=true``（逃生口已登记在册）。
    换句话说：**明文上传是明示的取舍，不是默认行为**。
    """
    key = export_mod.encryption_key()
    if key is None:
        logger.warning(
            "OSS 备份未加密：没有配置 BACKUP_ENCRYPTION_KEY。"
            "备份制品将以**明文 gzip** 存放在 OSS 上（任何人拿到对象即可解压）。"
            "要加密请配置一把 base64 的 32 字节随机密钥。")
        return tmp, export_mod.file_sha256(tmp), tmp.stat().st_size
    cipher = _new_temp(workdir, label + "-enc")
    try:
        size = export_mod.encrypt_file(tmp, cipher)
    except Exception:
        cipher.unlink(missing_ok=True)
        raise
    return cipher, export_mod.file_sha256(cipher), size


def run_backup(db: Session, *, actor=None, book_ids: Optional[Iterable[int]] = None,
               force_tier: Optional[str] = None, prune: bool = True,
               now: Optional[datetime] = None,
               should_stop: Optional[Callable[[], bool]] = None) -> RunResult:
    """跑一轮备份。返回每个目标的运行记录与被清理的对象。

    **连接池用完就关**，理由写在 :func:`app.backup.oss_client.close_bucket`：
    不关的话 CLOSE_WAIT 的连接会一直占着 fd，日志里什么都看不出来，最后以
    ``Too many open files`` 的形式在不相干的地方炸掉。

    这里只做「建桶 + 收尾」，真正的活儿在 :func:`_run_backup_with_bucket` ——
    拆开是为了不用给那 60 行主体再套一层 try/finally（缩进一多，diff 就没法看了）。

    ``should_stop`` 是**尽力停止**的钩子：返回 True 就停止本轮（剩下的目标
    不备份）。调度器用它把「租约已丢、该让接管者接手」这件事落成动作。

    **这里要求「制品一定是密文」。** ``require_encrypted_ready`` 按**生效
    配置**判「配了 OSS 就必须配了加密密钥」—— 界面上配 OSS 的那条路
    （产品自己指给运维的那条）也在这道闸里，不只是 env 层那条。
    启动期的闸门只看得到 env 层（启动期没有库连接），所以运行期这道
    不可省。详见 ``app/backup/config.py::require_encrypted_ready``。
    """
    cfg = cfg_mod.require_encrypted_ready(db)
    bucket = oss_client.make_bucket(cfg)
    try:
        return _run_backup_with_bucket(
            db, cfg, bucket, actor=actor, book_ids=book_ids,
            force_tier=force_tier, prune=prune, now=now, should_stop=should_stop)
    finally:
        # finally 而不是「跑到最后一行关」：备份里最常见的结局是**抛异常**
        # （某一本网络失败、配置被改掉）。只在成功路径上关，等于异常路径永远漏。
        oss_client.close_bucket(bucket)


def _run_backup_with_bucket(db: Session, cfg: dict, bucket, *, actor=None,
                            book_ids: Optional[Iterable[int]] = None,
                            force_tier: Optional[str] = None, prune: bool = True,
                            now: Optional[datetime] = None,
                            should_stop: Optional[Callable[[], bool]] = None) -> RunResult:
    now_utc = now or datetime.now(timezone.utc)
    workdir = _workdir()

    targets: list[tuple[str, Optional[int], str, Optional[Book]]] = []
    wanted = set(book_ids) if book_ids is not None else None
    for book in db.scalars(select(Book).order_by(Book.id)).all():
        if wanted is not None and book.id not in wanted:
            continue
        targets.append((SCOPE_BOOK, book.id, _book_label(book), book))
    if book_ids is None and cfg_mod.include_system(db):
        targets.append((SCOPE_SYSTEM, None, _book_label(None), None))

    if not targets:
        raise export_mod.ExportRefused(
            "没有可备份的对象：既没有账套，指定范围也是空的。"
            "先建一个账套，或检查 book_ids 过滤条件。")

    runs: list[BackupRun] = []
    stopped = 0
    for index, (scope, book_id, label, book) in enumerate(targets):
        if should_stop is not None and should_stop():
            # **租约丢了**（这个备份已被另一个 worker 接管）：本轮到此为止。
            # 已上传的产物仍然有效（幂等键会挡住接管者重复上传），没备份到的
            # 等下一个周期 —— 但这件事必须**如实记下来**，不能报成「成功」。
            stopped = len(targets) - index
            logger.warning("OSS 备份提前停止：还有 %d 个目标未备份（租约已丢失）", stopped)
            break
        tier = force_tier or tier_mod.classify(db, scope, book_id, now_utc)
        if tier not in ("daily", "weekly", "monthly"):
            raise ValueError(f"未知备份档位 {tier!r}，可选：daily/weekly/monthly")
        moment = tier_mod.local_now(now_utc)
        key = tier_mod.object_key(cfg["prefix"], tier, scope, book_id, moment)
        tmp = _new_temp(workdir, label)
        cipher_path = None
        started = datetime.now(timezone.utc)
        run = BackupRun(
            book_id=book_id, scope=scope, book_label=label, tier=tier,
            status=STATUS_FAILED, oss_key="", started_at=started,
        )
        try:
            stats = export_mod.export_scope(
                db, scope=scope, book_id=book_id, book_label=label,
                out_path=tmp, now=now_utc)
            upload_path, checksum, size_bytes = _prepare_upload(tmp, workdir, label)
            cipher_path = upload_path if upload_path != tmp else None
            # **按文件上传，不按内存。** 备份任务跑在 API 进程里，与在线请求
            # 共享同一个进程的内存：把整份 gzip 读进内存再上传，一本大账套的
            # 快照就能把在线请求挤到 OOM。oss2 自己分片读文件。
            oss_client.put_file(bucket, key, str(upload_path))
            run.status = STATUS_SUCCESS
            run.oss_key = key
            run.size_bytes = size_bytes
            run.row_count = stats.rows
            # 校验和是**密文**的 SHA256 —— OSS 上真正存着的那串字节。
            run.checksum = checksum
            run.message = f"{len(stats.tables)} 张表 / {stats.rows} 行"
        except Exception as exc:                      # noqa: BLE001 - 逐个隔离
            # **失败也要落记录，而且要全文。** 「上传失败」四个字在排障时等于
            # 没写：同一个报错可能来自网络、权限、桶不存在、Bucket 在别的地域。
            run.status = STATUS_FAILED
            run.oss_key = ""
            run.message = f"{type(exc).__name__}: {exc}"
        finally:
            tmp.unlink(missing_ok=True)
            if cipher_path is not None:
                cipher_path.unlink(missing_ok=True)
            run.finished_at = datetime.now(timezone.utc)
            _record(db, run)
            runs.append(run)

    # **清理失败不许把整轮拖下水。**
    #
    # 清理原来写在逐账套那个 try/except 之外，于是它抛出去的异常一路冒到
    # API 层，被兜底成一行「（整轮）failed / 后台任务异常，见服务端日志」。
    # 那个形态是最坏的一种：数据全传上去了，界面说失败，而真实原因一个字都
    # 没有。这不是理论 —— 见本模块开头那段事故记述。
    #
    # 现在分成两件事记：备份的成功仍然是成功（它确实成功了），清理的失败落到
    # ``RunResult.prune_error`` 与审计日志里。后者不是可有可无的日志噪音：
    # 「清理不工作」意味着退路窗口在悄悄缩短，而界面上一片绿。
    deleted: list[retention.Deleted] = []
    prune_error: Optional[str] = None
    if prune:
        try:
            deleted = retention.apply_retention(
                bucket, cfg["prefix"], cfg_mod.retention_days(db), now=now_utc)
        except Exception as exc:                       # noqa: BLE001
            prune_error = f"{type(exc).__name__}: {exc}"
            logger.warning("OSS 备份已完成，但清理过期产物失败：%s",
                           prune_error, exc_info=True)

    ok = sum(1 for r in runs if r.status == STATUS_SUCCESS)
    detail = f"OSS 备份 {ok}/{len(runs)} 成功，清理 {len(deleted)} 份过期产物"
    if prune_error:
        detail += f"；⚠ 清理失败：{prune_error}"
    if stopped:
        detail += f"；⚠ 提前停止：还有 {stopped} 个目标未备份（租约已丢失）"
    log(
        db,
        action="BACKUP_RUN",
        entity="backup_run",
        entity_id=now_utc.strftime("%Y%m%d-%H%M%S"),
        user=actor,
        detail=detail,
        after={"succeeded": ok, "total": len(runs), "deleted": len(deleted),
               "prune_error": prune_error or "", "stopped": stopped},
    )
    db.commit()
    return RunResult(runs=runs, deleted=deleted, prune_error=prune_error, stopped=stopped)


def recent_runs(db: Session, limit: int = 50) -> list[BackupRun]:
    limit = min(max(limit, 1), 200)
    return db.scalars(select(BackupRun).order_by(BackupRun.id.desc())
                      .limit(limit)).all()
