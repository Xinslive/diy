#!/usr/bin/env python
"""并发压测工具 —— 测出这套系统真实的 p50/p95/p99，而不是继续推断。

    .venv/bin/python scripts/loadtest.py --concurrency 8 --duration 30
    .venv/bin/python scripts/loadtest.py --scenario ledger-1 --scenario ledger-3
    .venv/bin/python scripts/loadtest.py --all-scales --json /tmp/baseline.json

**为什么不装 locust。** ``httpx`` 已经是 ``requirements-dev.lock`` 里的
依赖，而这套系统所有依赖都逐包定版、供应链面收得很紧。为一次压测引入
一个新依赖树，换来的只是并发模型与百分位统计 —— 后者这里 60 行就够。
工具在仓库内、可评审、可版本化，本身也是这条审计链的一部分。

**这个脚本测的是「真实并发下的延迟」，此前从未有人测过。**
``性能审计报告_2026-10-14.md`` 把「真实并发下的 p95/p99」明确列进
「待压测确认清单」，本脚本就是去把那一条划掉的。

**发起点与被测服务同机时，结果会系统性高估。** 所以每次都会同时
记录两侧的 CPU：``服务 CPU`` 是被测进程的，``发起点 CPU`` 是本脚本
自己的。两者相加接近机器总核数时，上面的延迟数字就不代表一台干净
机器的表现 —— 报告里会明确标出这一点，不要只看 RPS。

场景默认按真实业务权重混合；明细账刻意拆成 1/3/8 科目三档，
因为 ``0011`` 那条索引交叉点说的就是「3 个科目及以上规划器改走
覆盖索引并丢弃约 71% 的行」。三档分不开测，就看不到那个拐点。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import platform
import resource
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

IS_LINUX = sys.platform.startswith("linux")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024 if IS_LINUX else 4
TICKS = os.sysconf("SC_CLK_TCK") if IS_LINUX else 100


# --------------------------------------------------------------------- 统计


def pct(sorted_vals: list[float], p: float) -> float:
    """线性插值分位数。样本为空时返回 0 而不是抛错 —— 空样本本身就是结论。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


@dataclass
class Result:
    name: str
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    statuses: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, ms: float) -> None:
        self.latencies.append(ms)

    def fail(self, code: int = 0) -> None:
        self.errors += 1
        self.statuses[code] += 1

    @property
    def n_ok(self) -> int:
        return len(self.latencies)


# --------------------------------------------------------------------- 进程采样


class Sampler:
    """采样被测进程的 RSS 与 CPU。

    Linux 读 /proc，macOS 退回 ``ps``。**两条路都只在采样点取瞬时值**，
    所以结果是量级参考而不是精确计量 —— 这在压测里够用了，但不要拿它
    去对比两个相差 5% 的配置。
    """

    def __init__(self, pids: list[int], interval: float = 1.0):
        self.pids = [p for p in pids if p > 0]
        self.interval = interval
        self.rss_samples: list[float] = []
        self.cpu_samples: list[float] = []
        self._prev: dict[int, float] = {}
        self._t0 = 0.0
        self._task: asyncio.Task | None = None

    def _read(self, pid: int) -> tuple[float, float]:
        """返回 (rss_bytes, cpu_seconds)。读不到就返回 (0, 0)。"""
        try:
            if IS_LINUX:
                with open(f"/proc/{pid}/statm") as f:
                    rss = int(f.read().split()[1]) * PAGE_KB * 1024
                with open(f"/proc/{pid}/stat") as f:
                    parts = f.read().rsplit(")", 1)[1].split()
                # utime 是第 14 个字段（rsplit 之后 parts[11]），stime 是 parts[12]
                cpu = (int(parts[11]) + int(parts[12])) / TICKS
                return float(rss), cpu
            out = subprocess.run(
                ["ps", "-o", "rss=", "-o", "time=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5)
            if out.returncode != 0 or not out.stdout.strip():
                return 0.0, 0.0
            parts = out.stdout.split()
            rss_kb = float(parts[0])
            t = parts[-1]
            if t.count(":") == 2:
                h, m, s = t.split(":")
                cpu = int(h) * 3600 + int(m) * 60 + float(s)
            elif t.count(":") == 1:
                m, s = t.split(":")
                cpu = int(m) * 60 + float(s)
            else:
                cpu = float(t)
            return rss_kb * 1024, cpu
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return 0.0, 0.0

    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            dt = now - self._t0
            rss = cpu = 0.0
            for pid in self.pids:
                r, c = self._read(pid)
                rss += r
                prev = self._prev.get(pid)
                if prev is not None and dt > 0:
                    cpu += max(0.0, c - prev) / dt * 100
                self._prev[pid] = c
            self.rss_samples.append(rss / 1024 / 1024)
            self.cpu_samples.append(cpu)
            self._t0 = now
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def summary(self) -> dict:
        return {
            "rss_mb_max": round(max(self.rss_samples), 1) if self.rss_samples else 0,
            "rss_mb_avg": round(sum(self.rss_samples) / len(self.rss_samples), 1)
            if self.rss_samples else 0,
            "cpu_pct_max": round(max(self.cpu_samples), 1) if self.cpu_samples else 0,
            "cpu_pct_avg": round(sum(self.cpu_samples) / len(self.cpu_samples), 1)
            if self.cpu_samples else 0,
            "samples": len(self.rss_samples),
        }


def self_cpu_seconds() -> float:
    """本压测脚本自己烧掉的 CPU 时间（用户态 + 内核态）。"""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def find_server_pids(pattern: str) -> list[int]:
    """按命令行特征找出被测服务进程。

    刻意**同时取父进程和子进程**：uvicorn ``--workers N`` 会 fork 出
    N 个 worker，它们的命令行是 ``multiprocessing.spawn``，只有父进程
    带 ``uvicorn app.main:app``。只按父进程采样会把 2/3 的内存漏掉，
    报告出来的「服务内存」就是一个好看的假数字。
    """
    try:
        out = subprocess.run(["ps", "-eo", "pid,ppid,command"],
                             capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError:
        return []
    parents: set[int] = set()
    rows = out.stdout.splitlines()[1:]
    for line in rows:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts
        if pattern in cmd and "ps -eo" not in cmd:
            try:
                parents.add(int(pid))
            except ValueError:
                pass
    pids = set(parents)
    for line in rows:                       # 再收一遍 spawn 出来的 worker
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            if int(parts[1]) in parents and "spawn_main" in parts[2]:
                pids.add(int(parts[0]))
        except ValueError:
            pass
    return sorted(pids)


# --------------------------------------------------------------------- 场景


def _walk_tree(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (全部叶子, 有下级的父)。顺序与接口一致（按 code）。

    模块级而不是 discover 里的闭包：写场景也要用它选科目。
    """
    leaves, parents = [], []
    for n in nodes:
        kids = n.get("children") or []
        (parents if kids else leaves).append(n)
        l2, p2 = _walk_tree(kids)
        leaves += l2
        parents += p2
    return leaves, parents


@dataclass
class Scenario:
    name: str
    method: str
    path: str
    json_body: dict | None = None
    weight: int = 1


class Bench:
    def __init__(self, client: httpx.AsyncClient, token: str, concurrency: int):
        self.client = client
        self.token = token
        self.concurrency = concurrency
        self.results: dict[str, Result] = {}
        self._rr = 0

    async def discover(self, base: str, book_name: str, period_id: int | None) -> list[Scenario]:
        """登录并把场景参数**动态发现**出来。

        不写死 subject_id / book_id：那是造数脚本的内部 id，换个
        profile 就变了，写死会让压测在数据一改就悄悄打错地方 ——
        而「打错地方」的压测结果是全绿的，比没有压测更坏。
        """
        h = {"Authorization": f"Bearer {self.token}"}
        books = (await self.client.get(f"{base}/api/books", headers=h)).json()
        match = next((b for b in books if b["name"] == book_name), None)
        if match is None:
            raise RuntimeError(f"找不到账套「{book_name}」，现有：{[b['name'] for b in books]}")
        book_id = match["id"]

        subs = (await self.client.get(
            f"{base}/api/books/{book_id}/subjects", headers=h)).json()

        # 期间走正式接口取，不靠 ``period_id=0`` 的隐式兜底 ——
        # 那条兜底是 ``if period_id:`` 判出来的，0 恰好为假才落到
        # 「取最后一个期间」。压测脚本靠一个假值走对路，等哪天
        # 兜底改成 ``is None`` 就会静默地一直打 period 0。
        if period_id is None:
            pls = (await self.client.get(
                f"{base}/api/books/{book_id}/periods", headers=h)).json()
            rows = pls if isinstance(pls, list) else (pls.get("periods") or [])
            period_id = max((r.get("id", 0) for r in rows), default=0)
        if not period_id:
            raise RuntimeError("取不到任何会计期间，先用 seed_perf.py 造数")

        leaves, parents = _walk_tree(subs)
        if not leaves:
            raise RuntimeError("账套里没有末级科目")
        # 选子科目最多的那个父科目：只有它才够格切出 3/8 档
        wide = max(parents, key=lambda n: len(n["children"])) if parents else None
        if wide is None or len(wide["children"]) < 8:
            print("⚠️  没有子科目 ≥8 的父科目，明细账多科目档将退化为标准表规模。"
                  "（用 seed_perf.py --widen-to 扩充科目表）")

        base_period = period_id
        # 注意分隔符：subject_id 后面已经有 ? 了，这里必须用 &。
        q = f"&period_id={base_period}"
        scenarios = [
            Scenario("ledger-1", "GET",
                     f"/api/books/{book_id}/ledger/detail"
                     f"?subject_id={leaves[0]['id']}{q}"),
        ]
        if wide:
            kids = wide["children"]
            for n in (3, 8, 32):
                if len(kids) >= n:
                    lo, hi = kids[0]["id"], kids[n - 1]["id"]
                    scenarios.append(Scenario(
                        f"ledger-{n}", "GET",
                        f"/api/books/{book_id}/ledger/detail?subject_id={wide['id']}"
                        f"&from_subject_id={lo}&to_subject_id={hi}{q}"))
        scenarios += [
            Scenario("vouchers-subject", "GET",
                     f"/api/books/{book_id}/vouchers?subject_id={leaves[0]['id']}"
                     f"&page_size=50"),
            Scenario("vouchers-all", "GET",
                     f"/api/books/{book_id}/vouchers?page_size=50"),
            Scenario("login", "POST", "/api/auth/login",
                     json_body={"username": self._user, "password": self._pass}),
        ]
        return scenarios

    @staticmethod
    def _unused_period_hint() -> None:
        """占位：账套的 ``years`` 只有年度汇总（year/total/closed/vouchers），
        **没有期间列表**，所以期间一律走 ``/api/books/{id}/periods`` 取。"""
        raise NotImplementedError

    # ----------------------------------------------------------------- 写场景

    async def make_write_scenarios(self, base: str, entries: int) -> list[Scenario]:
        """造**写**场景，打在一个自建的临时账套上。

        **为什么不直接打演示账套。** 写场景会真的落数据。打别人的账套意味着
        压测把数据搞脏了、跑完还得手工收拾 —— 而「忘了收拾」这种情况一定会发生。
        所以：新建一个专用账套，压完删掉。

        **为什么每次都新建而不是复用。** 版本号触发器会让并发写串行，
        而复用的账套里上一轮的数据会让缓存命中率、索引体积都随轮次漂移 ——
        第二轮的数不能和第一轮比。**同一个账号下同名账套只能有一个**，
        所以用时间戳命名。
        """
        h = {"Authorization": f"Bearer {self.token}"}
        name = f"压测写入-{int(time.time())}"
        r = await self.client.post(f"{base}/api/books", headers=h, json={
            "company_name": f"压测写入公司-{int(time.time())}", "name": name,
            "start_year": 2026, "start_month": 1})
        if r.status_code >= 400:
            raise RuntimeError(f"建压测写入账套失败：{r.status_code} {r.text[:200]}")
        book_id = r.json()["id"]

        pls = (await self.client.get(f"{base}/api/books/{book_id}/periods",
                                     headers=h)).json()
        rows = pls if isinstance(pls, list) else (pls.get("periods") or [])
        if not rows:
            raise RuntimeError("新建账套没有期间")
        period = rows[0]

        subs = (await self.client.get(f"{base}/api/books/{book_id}/subjects",
                                      headers=h)).json()
        leaves, _ = _walk_tree(subs)
        if len(leaves) < 2:
            raise RuntimeError("新账套里没有末级科目可用")

        dr, cr = leaves[0]["id"], leaves[1]["id"]

        def voucher_body(n_lines: int) -> dict:
            """n_lines 条分录的借贷各半，借贷必须相等否则护栏会拒。"""
            per = Decimal("100.00")
            half = n_lines // 2
            entries = [{"subject_id": dr, "amount": str(per), "summary": "压测",
                        "direction": "DEBIT"} for _ in range(half)]
            entries += [{"subject_id": cr, "amount": str(per), "summary": "压测",
                         "direction": "CREDIT"} for _ in range(n_lines - half)]
            return {
                # **不能用 period["code"]**：它是 "2026-01" 这样的期间编码，
                # 不是日期（后端 Pydantic 直接 422，报「input is too short」）。
                # 真实日期要从 year / month 拼。
                "voucher_date": f"{period.get('year')}-{int(period.get('month')):02d}-01",
                "entries": entries, "remark": "压测写入",
                # **刻意不指定 voucher_no**：写死一个编号，并发下必然撞
                # uq_voucher_period_no，几十个请求全部 400 —— 测出来的
                # 「写很慢」其实是唯一约束在挡路，不是写路径慢。
                # 不给编号由服务端发号，那才是真实用户路径，
                # 而且取号本身就是这条路径的一部分。
            }

        self._write_book_id = book_id
        self._write_period_id = period["id"]

        return [
            Scenario("write-voucher", "POST",
                     f"/api/books/{book_id}/vouchers",
                     json_body=voucher_body(entries), weight=1),
            Scenario("write-voucher-bulk", "POST",
                     f"/api/books/{book_id}/vouchers",
                     json_body=voucher_body(500), weight=1),
        ]

    async def cleanup_write_book(self, base: str) -> None:
        """删掉临时账套。

        **复用 `seed_demo.wipe_book` 而不是自己写。** 它按正确的依赖顺序删，
        而且是**按规矩过护栏**的：先解锁期间、再把已记账凭证退回未记账，
        然后才删 —— 触发器明确放行的状态跳转只有这一个。
        自己写一份「直接 DELETE」会在压测账套没结账时就撞
        ``trg_posted_voucher_guard``，而绕过触发器去删是这个项目明令禁止的
        （见 ``wipe_book`` 自己的注释）。

        也没有走 HTTP：这个系统**没有** `DELETE /api/books/{id}`，账套在产品上
        能不能删本身是有争议的，不该为了压测临时加一个。
        """
        bid = getattr(self, "_write_book_id", None)
        if not bid:
            return
        try:
            from app.core.db import SessionLocal

            # 按**路径**加载而不是 ``from seed_demo import wipe_book``：
            # 后者只在「把本文件当脚本直接跑」时才成立（那时脚本所在目录
            # 自动在 sys.path 上）。被当成模块导入时就 ModuleNotFoundError，
            # 而那正好是清理会失败、却没人看见的时刻。
            spec = importlib.util.spec_from_file_location(
                "cwq_seed_demo", Path(__file__).with_name("seed_demo.py"))
            seed_demo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(seed_demo)

            db = SessionLocal()
            try:
                seed_demo.wipe_book(db, bid)
            finally:
                db.close()
            print(f"临时写入账套已删除（book_id={bid}）")
        except Exception as e:                     # noqa: BLE001
            # 清理失败**不许**把压测结果一起废掉：数字已经量到了，
            # 报出来让人手工收一下就行。
            print(f"⚠️  临时账套 {bid} 自动清理失败（{e}），请手工清理")

    async def run(self, scenarios: list[Scenario], duration: float) -> None:
        weighted: list[Scenario] = []
        for s in scenarios:
            weighted += [s] * s.weight
        deadline = time.monotonic() + duration
        stop = asyncio.Event()

        async def worker() -> None:
            while time.monotonic() < deadline and not stop.is_set():
                sc = weighted[self._rr % len(weighted)]
                self._rr += 1
                r = self.results.setdefault(sc.name, Result(sc.name))
                t0 = time.perf_counter()
                try:
                    resp = await self.client.request(
                        sc.method, sc.path, json=sc.json_body,
                        headers={"Authorization": f"Bearer {self.token}"})
                    dt = (time.perf_counter() - t0) * 1000
                    if resp.status_code < 400:
                        r.record(dt)
                    else:
                        r.fail(resp.status_code)
                except (httpx.HTTPError, OSError):
                    r.fail(0)

        await asyncio.gather(*(worker() for _ in range(self.concurrency)))


# --------------------------------------------------------------------- 报告


def report(results: dict[str, Result], wall: float, sysm: dict, cpu: int) -> str:
    lines = []
    lines.append(f"{'场景':<20}{'样本':>7}{'失败':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'最大':>9}{'RPS':>8}")
    lines.append("-" * 77)
    for name, r in sorted(results.items()):
        if not r.latencies and r.errors:
            lines.append(f"{name:<20}{0:>7}{r.errors:>6}   （全部失败）")
            continue
        v = sorted(r.latencies)
        rps = r.n_ok / wall if wall else 0
        lines.append(
            f"{name:<20}{r.n_ok:>7}{r.errors:>6}"
            f"{pct(v, .50):>8.1f}m{pct(v, .95):>8.1f}m{pct(v, .99):>8.1f}m"
            f"{(v[-1] if v else 0):>8.1f}m{rps:>8.1f}")
    lines.append("-" * 77)
    lines.append(f"耗时 {wall:.1f}s · 服务 RSS 峰值 {sysm['rss_mb_max']} MB / "
                 f"均值 {sysm['rss_mb_avg']} MB")
    lines.append(f"服务 CPU 峰值 {sysm['cpu_pct_max']}% / 均值 {sysm['cpu_pct_avg']}%"
                 f"（{cpu} 核机器）· 发起点 CPU 峰值 {cpu_self_peak:.0f}%")
    if sysm["samples"] and sysm["cpu_pct_max"] > cpu * 80:
        lines.append("⚠️  服务 CPU 已逼近机器核数，延迟数字受资源争抢影响，"
                     "不能当作一台干净机器的表现。")
    return "\n".join(lines)


cpu_self_peak = 0.0


# --------------------------------------------------------------------- 主流程


async def amain(args: argparse.Namespace) -> int:
    global cpu_self_peak
    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency * 2)
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=timeout) as client:
        login = await client.post("/api/auth/login", json={
            "username": args.username, "password": args.password})
        if login.status_code != 200:
            print(f"❌ 登录失败 HTTP {login.status_code}: {login.text[:200]}")
            print(f"   先造数：.venv/bin/python scripts/seed_perf.py --profile {args.profile}")
            return 1
        token = login.json()["access_token"]

        bench = Bench(client, token, args.concurrency)
        bench._user, bench._pass = args.username, args.password

        # 写场景**单独一档**：它们真的落数据，混进读场景的循环里会
        # 把两边都测坏（读场景的命中率被写搅掉，写场景又被读的耗时掩盖）。
        # 而且要一个专属账套。
        if args.write:
            write_scenarios = await bench.make_write_scenarios(base, args.write_entries)
            print("写场景（自建临时账套，跑完删除）: "
                  + ", ".join(s.name for s in write_scenarios))
            if args.scenario:
                write_scenarios = [s for s in write_scenarios
                                   if s.name in args.scenario] or write_scenarios
            try:
                if args.warmup > 0:
                    print("写场景热身中（不计入）…")
                    await bench.run(write_scenarios, args.warmup)
                    bench.results.clear()
                pids_w = ([int(p) for p in args.server_pid.split(",") if p.strip()]
                          or find_server_pids(args.server_pattern))
                sampler_w = Sampler(pids_w)
                sampler_w.start()
                cpu_before_w = self_cpu_seconds()
                t1 = time.monotonic()
                await bench.run(write_scenarios, args.duration)
                wall_w = time.monotonic() - t1
                # **必须 stop 之后才 summary** —— 采样线程还在跑的时候取，
                # 拿到的是半截数据，而且线程会一直活着到进程退出。
                await sampler_w.stop()
                sysm_w = sampler_w.summary()
            finally:
                await bench.cleanup_write_book(base)

            print(f"\n{report(bench.results, wall_w, sysm_w, os.cpu_count() or 1)}")
            if any(r.errors for r in bench.results.values()):
                print(f"\n失败明细: " + ", ".join(
                    f"{n}={dict(r.statuses)}" for n, r in bench.results.items() if r.errors))
            print(
                "\n口径提醒：写吞吐受磁盘与 PG 落盘影响远大于读，"
                "不可与读场景横向比。这组数字的用途是**看版本号触发器的锁代价**："
                "同一账套的并发写会在 book 行上串行，语句级触发器让它是"
                "「每条语句一次」而不是「每行一次」。\n"
                "  write-voucher       每张 2 行 —— 锁开销的基线\n"
                "  write-voucher-bulk  每张 500 行 —— 批量写形态，与导入同一量级")
            if args.json:
                Path(args.json).write_text(json.dumps(
                    {"mode": "write", "concurrency": args.concurrency,
                     "wall": wall_w, "scenarios": {
                         n: {"n_ok": r.n_ok, "errors": r.errors,
                             "p50": round(pct(sorted(r.latencies), .50), 1),
                             "p95": round(pct(sorted(r.latencies), .95), 1),
                             "p99": round(pct(sorted(r.latencies), .99), 1)}
                         for n, r in bench.results.items()}},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"JSON 已写入 {args.json}")
            return 0

        scenarios = await bench.discover(base, args.book_name, args.period_id)
        if args.scenario:
            scenarios = [s for s in scenarios if s.name in args.scenario]
            if not scenarios:
                print(f"❌ 没有匹配的场景。可用：{[s.name for s in scenarios]}")
                return 1

        print(f"目标 {base} · 并发 {args.concurrency} · 时长 {args.duration}s · "
              f"热身 {args.warmup}s")
        print("场景: " + ", ".join(f"{s.name}×{s.weight}" for s in scenarios))

        if args.warmup > 0:
            print("热身中（不计入）…")
            await bench.run(scenarios, args.warmup)
            bench.results.clear()

        pids = ([int(p) for p in args.server_pid.split(",") if p.strip()]
                or find_server_pids(args.server_pattern))
        if not pids:
            print("⚠️  没找到被测服务进程，RSS/CPU 采样将为空"
                  f"（用 --server-pid 指定，或调整 --server-pattern='{args.server_pattern}'）")
        sampler = Sampler(pids)
        sampler.start()

        cpu_before = self_cpu_seconds()
        t0 = time.monotonic()
        await bench.run(scenarios, args.duration)
        wall = time.monotonic() - t0
        cpu_self_peak = (self_cpu_seconds() - cpu_before) / wall * 100 if wall else 0
        await sampler.stop()

        sysm = sampler.summary()
        total_err = sum(r.errors for r in bench.results.values())
        print()
        print(report(bench.results, wall, sysm, os.cpu_count() or 1))
        if total_err:
            print(f"\n失败明细: " + ", ".join(
                f"{n}={dict(r.statuses)}" for n, r in bench.results.items() if r.errors))

        if args.json:
            payload = {
                "base_url": base, "concurrency": args.concurrency,
                "duration": wall, "profile": args.profile,
                "system": sysm, "server_pids": pids,
                "generator_cpu_pct": round(cpu_self_peak, 1),
                "cpu_count": os.cpu_count(),
                "scenarios": {
                    n: {"n_ok": r.n_ok, "errors": r.errors,
                        "p50": round(pct(sorted(r.latencies), .50), 1),
                        "p95": round(pct(sorted(r.latencies), .95), 1),
                        "p99": round(pct(sorted(r.latencies), .99), 1),
                        "max": round(max(r.latencies), 1) if r.latencies else 0,
                        "statuses": dict(r.statuses)}
                    for n, r in bench.results.items()},
            }
            Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nJSON 已写入 {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="财务记账系统并发压测")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--username", default="perf_probe")
    p.add_argument("--password", default="perf-probe-password-2026")
    p.add_argument("--book-name", default="压测账套01")
    p.add_argument("--profile", default="medium", help="仅用于报错提示")
    p.add_argument("--period-id", type=int, default=None, help="默认取账套最后一个期间")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--warmup", type=float, default=3.0)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--scenario", action="append", default=None,
                   help="只跑指定场景（可重复）。默认全跑")
    p.add_argument("--write", action="store_true",
                   help="跑**写**场景（自建临时账套，跑完删除）。"
                        "与读场景互斥：写会真的落数据，混在一起两边都测不准")
    p.add_argument("--write-entries", type=int, default=2, metavar="N",
                   help="write-voucher 场景每张凭证的分录数（默认 2）")
    p.add_argument("--server-pid", default="", help="逗号分隔的被测进程 pid")
    p.add_argument("--server-pattern", default="uvicorn app.main:app")
    p.add_argument("--json", default="", help="把结果写成 JSON")
    args = p.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
