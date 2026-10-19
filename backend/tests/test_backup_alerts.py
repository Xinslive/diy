"""备份脚本的告警通道（审计 P2-7 的整改，补上生产上真正在跑的那条路径）。

**这份测试盯的是一个曾经存在的空洞。**

邮件告警最早只挂在**应用内**的调度任务上：``system:oss_backup`` 失败会发
一封信。而生产上每天 03:17 跑的**另一条**备份 —— 本机 ``.sql`` 备份 ——
是 systemd timer 直接调 ``scripts/db_backup.py``（``cwq-backup.timer``，
``deploy.sh`` 默认就会装上并启用，除非运维放了
``/opt/cwq/.backup-timer-off`` 标记文件）。那条路径失败时只有两个去处：
进程退出码（systemd 收进 journal）与备份目录。

两者都要人主动去看 —— 而备份失败通常发生在凌晨没人看的时候。于是
「退路断了」这件事，可能要到下次有人想恢复时才发现。

（OSS 那条：生产上走的是**应用内调度器**（外部每分钟调 tick），
已经被最初的告警覆盖；旧的 ``cwq-oss-backup.timer`` 不应再启用，但
仓库里那个 unit 还在，谁手工装上就会跑到 ``backup_to_oss.py`` ——
所以那个脚本也要能发信。）

**所以脚本自己也要能发信。** 这份测试钉的就是这件事，以及三件不能被
它弄坏的东西：

1. 退出码**不变**（systemd 靠它判断成败）；
2. 告警发不出去**不能**把脚本搞崩（那会把「备份失败」变成「脚本异常」，
   两件事混在一起，处置动作完全不同）；
3. 恢复被拒**不告警**（那是校验起作用了，是好事）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.notify import events as notify_events

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _加载脚本(相对路径: str, 模块名: str):
    """按文件路径加载脚本。

    不走 ``from scripts import ...``：``scripts/`` 没有 ``__init__.py``，
    它是一堆独立运维脚本而不是一个包。
    """
    spec = importlib.util.spec_from_file_location(
        模块名, BACKEND_ROOT / 相对路径)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup_to_oss = _加载脚本("scripts/backup_to_oss.py", "cwq_backup_to_oss")
db_backup = _加载脚本("scripts/db_backup.py", "cwq_db_backup")


@pytest.fixture
def 发出的信(monkeypatch):
    """截住所有发出去的通知。

    拦在 ``dispatch`` 这一层而不是 ``send_mail``：因为脚本走的是**同步**
    投递（进程马上就要退出，异步线程会随进程一起死），拦住
    ``dispatch`` 才验得到「信真的被交出去了」这件事。
    """
    记录 = []

    def _fake_dispatch(event, subject, body, *, db=None):
        记录.append({"event": event, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(notify_events, "dispatch", _fake_dispatch)
    return 记录


class _假运行:
    """BackupRun 的替身。字段对齐 ``_print_run`` 用到的那几个。"""

    def __init__(self, status: str, book_label: str, message: str = ""):
        self.status = status
        self.book_label = book_label
        self.message = message
        self.tier = "daily"
        self.oss_key = "key"
        self.row_count = 10
        self.size_bytes = 1024


class _假结果:
    """RunResult 的替身。"""

    def __init__(self, runs, deleted=(), prune_error=None):
        self.runs = list(runs)
        self.deleted = list(deleted)
        self.prune_error = prune_error

    @property
    def succeeded(self):
        return [r for r in self.runs if r.status == "success"]

    @property
    def failed(self):
        return [r for r in self.runs if r.status != "success"]


@pytest.fixture
def 备份就绪(monkeypatch):
    """把 OSS 备份的配置与开关都做成"就绪"。

    只改这两个 —— 真正要验的是**失败时会不会发信**，不是配置校验。
    """
    monkeypatch.setattr(backup_to_oss.cfg_mod, "is_enabled", lambda _db: True)
    monkeypatch.setattr(backup_to_oss.cfg_mod, "require_ready", lambda _db: {})


# ------------------------------------------------------------------ OSS 备份


def test_OSS备份全部失败时发告警(备份就绪, 发出的信, monkeypatch):
    """退路全灭 → 必须有一封邮件。

    这是**最该被知道**的情况：不是"少备了几本"，是一份都没留下。
    """
    monkeypatch.setattr(
        backup_to_oss.service, "run_backup",
        lambda *a, **k: _假结果([_假运行("failed", "账套 A", "AccessKey 无效")]))

    退出码 = backup_to_oss.main([])

    assert 退出码 == 4, "全灭必须是非零退出码"
    assert len(发出的信) == 1, "备份全灭却没有发告警"
    信 = 发出的信[0]
    assert 信["event"] == "BACKUP_FAILED"
    assert "账套 A" in 信["body"], f"告警正文没说是哪本失败：{信['body']!r}"
    assert "AccessKey 无效" in 信["body"]


def test_OSS备份部分失败时也发告警(备份就绪, 发出的信, monkeypatch):
    """**部分失败也要告警** —— 哪怕退出码仍然是 0。

    退出码 0 的含义是"这轮有东西备份出去了"，它对"有 1 本账套没备上"
    一无所知。而没备上的那本账套是没人会主动去翻的 —— 它既不在失败清单
    里（清单只列全灭），也不会出现在"最近成功"里。
    """
    monkeypatch.setattr(
        backup_to_oss.service, "run_backup",
        lambda *a, **k: _假结果([
            _假运行("success", "账套 A"),
            _假运行("failed", "账套 B", "网络超时"),
        ]))

    退出码 = backup_to_oss.main([])

    assert 退出码 == 0, "部分失败时的退出码约定不该被这条测试改动"
    assert len(发出的信) == 1, "部分失败没有告警"
    assert "账套 B" in 发出的信[0]["body"]


def test_OSS备份全部成功时不发告警(备份就绪, 发出的信, monkeypatch):
    """成功时**一个字都不该多发**。

    告警通道的价值全在"异常时才响"上。一条每天准时到达的"一切正常"
    邮件，几周之后就没有人看了 —— 那时它和没有通道是一样的。
    """
    monkeypatch.setattr(
        backup_to_oss.service, "run_backup",
        lambda *a, **k: _假结果([_假运行("success", "账套 A")]))

    退出码 = backup_to_oss.main([])

    assert 退出码 == 0
    assert 发出的信 == []


def test_OSS备份配置不全时发告警(备份就绪, 发出的信, monkeypatch):
    """配置不全 = 备份**根本没跑**。

    这比"跑了一半失败"更严重：不是少备了几本，是一份都没留。
    """
    def _不完整(_db):
        raise backup_to_oss.cfg_mod.ConfigIncomplete("还没配 Bucket")

    monkeypatch.setattr(backup_to_oss.cfg_mod, "require_ready", _不完整)

    退出码 = backup_to_oss.main([])

    assert 退出码 == 2
    assert len(发出的信) == 1, "配置不全、备份没跑，却一声不吭"
    assert "还没配 Bucket" in 发出的信[0]["body"]


def test_清理失败单独告警且不改变退出码(备份就绪, 发出的信, monkeypatch):
    """清理失败是"退路窗口在悄悄缩短"，不是"这轮没备上"。

    所以它单独告警，但**不占用**退出码 4 —— 那个码的含义是"什么都没传
    上去"，而产物明明都在 Bucket 里。
    """
    monkeypatch.setattr(
        backup_to_oss.service, "run_backup",
        lambda *a, **k: _假结果([_假运行("success", "账套 A")],
                                prune_error="OSS 拒绝删除"))

    退出码 = backup_to_oss.main([])

    assert 退出码 == 0, "清理失败不该把成功的一轮报成失败"
    assert len(发出的信) == 1
    assert "OSS 拒绝删除" in 发出的信[0]["body"]


def test_告警发不出去不改变退出码也不让脚本崩(备份就绪, monkeypatch, capsys):
    """告警是**旁路**，它自己出问题不能盖掉真正的失败原因。

    退出码是 systemd 判断成败的唯一依据；让告警把它改掉，等于让
    "备份全灭" 变成 "脚本异常" —— 处置动作完全不同。
    """
    def _炸(*a, **k):
        raise RuntimeError("SMTP 挂了")

    monkeypatch.setattr(notify_events, "dispatch", _炸)
    monkeypatch.setattr(
        backup_to_oss.service, "run_backup",
        lambda *a, **k: _假结果([_假运行("failed", "账套 A", "AccessKey 无效")]))

    退出码 = backup_to_oss.main([])

    assert 退出码 == 4, "告警失败把退出码改了"
    assert "告警邮件发送失败" in capsys.readouterr().err


# ------------------------------------------------------------------ 本机备份


def test_本机备份失败时发告警(发出的信, monkeypatch, tmp_path):
    """本机 .sql 备份是**另一条**退路，断它也要有人知道。"""
    monkeypatch.setattr(db_backup, "dump_to", lambda _fh: 1)
    目标 = tmp_path / "cwq.sql"

    退出码 = db_backup.dump_to_file(目标)

    assert 退出码 == 1, "退出码要原样透出，systemd 靠它判断成败"
    assert len(发出的信) == 1, "本机备份失败却没有告警"
    assert 发出的信[0]["event"] == "LOCAL_BACKUP_FAILED"


def test_本机备份产出0字节时发告警(发出的信, monkeypatch, tmp_path):
    """0 字节的备份**长得像备份**。

    它被丢弃了（不留着冒充备份），但"这一轮其实什么也没留下"这件事
    同样要有人知道。
    """
    def _空的(_fh):
        return 0

    monkeypatch.setattr(db_backup, "dump_to", _空的)
    目标 = tmp_path / "cwq.sql"

    退出码 = db_backup.dump_to_file(目标)

    assert 退出码 == 4
    assert len(发出的信) == 1
    assert "0 字节" in 发出的信[0]["body"]


def test_本机备份成功时不发告警(发出的信, monkeypatch, tmp_path):
    def _写点东西(fh):
        fh.write(b"-- fake dump\n")
        return 0

    monkeypatch.setattr(db_backup, "dump_to", _写点东西)
    目标 = tmp_path / "cwq.sql"

    退出码 = db_backup.dump_to_file(目标)

    assert 退出码 == 0
    assert 发出的信 == []


def test_本机备份的告警与OSS备份的告警不共用限流桶(发出的信, monkeypatch, tmp_path):
    """两条退路各断各的，不该因为一边刷屏就把另一边一起压掉。

    事件类型不同 → 桶不同。共用一个桶的话，OSS 备份告警刷屏就能把
    本机备份告警一起压掉 —— 等于给了故障制造者一个"关掉另一半警报"
    的开关。
    """
    monkeypatch.setattr(db_backup, "dump_to", lambda _fh: 1)
    db_backup.dump_to_file(tmp_path / "cwq.sql")

    事件 = [信["event"] for 信 in 发出的信]
    assert 事件 == ["LOCAL_BACKUP_FAILED"]
