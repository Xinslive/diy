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
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.backup import config as cfg_mod
from app.backup import export as export_mod
from app.backup import oss_client, retention, tier as tier_mod
from app.models import (SCOPE_BOOK, SCOPE_SYSTEM, STATUS_FAILED, STATUS_SUCCESS,
                        BackupRun, Book)


@dataclass
class RunResult:
    runs: list[BackupRun]
    deleted: list[retention.Deleted]

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
    """
    override = os.environ.get("CWQ_BACKUP_WORK_DIR", "")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(tempfile.gettempdir())


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


def run_backup(db: Session, *, actor=None, book_ids: Optional[Iterable[int]] = None,
               force_tier: Optional[str] = None, prune: bool = True,
               now: Optional[datetime] = None) -> RunResult:
    """跑一轮备份。返回每个目标的运行记录与被清理的对象。

    **连接池用完就关**，理由写在 :func:`app.backup.oss_client.close_bucket`：
    不关的话 CLOSE_WAIT 的连接会一直占着 fd，日志里什么都看不出来，最后以
    ``Too many open files`` 的形式在不相干的地方炸掉。

    这里只做「建桶 + 收尾」，真正的活儿在 :func:`_run_backup_with_bucket` ——
    拆开是为了不用给那 60 行主体再套一层 try/finally（缩进一多，diff 就没法看了）。
    """
    cfg = cfg_mod.require_ready(db)
    bucket = oss_client.make_bucket(cfg)
    try:
        return _run_backup_with_bucket(
            db, cfg, bucket, actor=actor, book_ids=book_ids,
            force_tier=force_tier, prune=prune, now=now)
    finally:
        # finally 而不是「跑到最后一行关」：备份里最常见的结局是**抛异常**
        # （某一本网络失败、配置被改掉）。只在成功路径上关，等于异常路径永远漏。
        oss_client.close_bucket(bucket)


def _run_backup_with_bucket(db: Session, cfg: dict, bucket, *, actor=None,
                            book_ids: Optional[Iterable[int]] = None,
                            force_tier: Optional[str] = None, prune: bool = True,
                            now: Optional[datetime] = None) -> RunResult:
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
    for scope, book_id, label, book in targets:
        tier = force_tier or tier_mod.classify(db, scope, book_id, now_utc)
        if tier not in ("daily", "weekly", "monthly"):
            raise ValueError(f"未知备份档位 {tier!r}，可选：daily/weekly/monthly")
        moment = tier_mod.local_now(now_utc)
        key = tier_mod.object_key(cfg["prefix"], tier, scope, book_id, moment)
        tmp = _new_temp(workdir, label)
        started = datetime.now(timezone.utc)
        run = BackupRun(
            book_id=book_id, scope=scope, book_label=label, tier=tier,
            status=STATUS_FAILED, oss_key="", started_at=started,
        )
        try:
            stats = export_mod.export_scope(
                db, scope=scope, book_id=book_id, book_label=label,
                out_path=tmp, now=now_utc)
            oss_client.put_bytes(bucket, key, tmp.read_bytes())
            run.status = STATUS_SUCCESS
            run.oss_key = key
            run.size_bytes = stats.size_bytes
            run.row_count = stats.rows
            run.checksum = stats.checksum
            run.message = f"{len(stats.tables)} 张表 / {stats.rows} 行"
        except Exception as exc:                      # noqa: BLE001 - 逐个隔离
            # **失败也要落记录，而且要全文。** 「上传失败」四个字在排障时等于
            # 没写：同一个报错可能来自网络、权限、桶不存在、Bucket 在别的地域。
            run.status = STATUS_FAILED
            run.oss_key = ""
            run.message = f"{type(exc).__name__}: {exc}"
        finally:
            tmp.unlink(missing_ok=True)
            run.finished_at = datetime.now(timezone.utc)
            _record(db, run)
            runs.append(run)

    deleted: list[retention.Deleted] = []
    if prune:
        deleted = retention.apply_retention(
            bucket, cfg["prefix"], cfg_mod.retention_days(db), now=now_utc)

    ok = sum(1 for r in runs if r.status == STATUS_SUCCESS)
    log(
        db,
        action="BACKUP_RUN",
        entity="backup_run",
        entity_id=now_utc.strftime("%Y%m%d-%H%M%S"),
        user=actor,
        detail=f"OSS 备份 {ok}/{len(runs)} 成功，清理 {len(deleted)} 份过期产物",
        after={"succeeded": ok, "total": len(runs), "deleted": len(deleted)},
    )
    db.commit()
    return RunResult(runs=runs, deleted=deleted)


def recent_runs(db: Session, limit: int = 50) -> list[BackupRun]:
    limit = min(max(limit, 1), 200)
    return db.scalars(select(BackupRun).order_by(BackupRun.id.desc())
                      .limit(limit)).all()
