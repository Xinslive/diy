"""账簿聚合的**跨请求**缓存（2026-10 性能收尾 P0）。

`LedgerEngine` 本来就只做单次请求内的记忆化：它构造时读一次库、
算完就扔。多人同时看同一个账套时，每个请求都会把同一份聚合重算一遍 ——
压测（`压测基线报告_2026-10-15.md`）里一次明细账的 85.9ms 有 77.7ms 花在这上面。

本模块让这份计算可以跨请求复用。**它只有一件事要做对：什么时候作废。**

------------------------------------------------------------------
为什么不用 TTL
------------------------------------------------------------------
TTL 会让「用户刚记完一张凭证，刷新总账看到的还是记之前的数」成为可能，
而且在 TTL 窗口内一直是这样。README 的核心承诺是不存在「某个报表和总账
对不上」这种事 —— 对记账系统来说这不是「稍微不准」，是「刚保存的东西没生效」，
用户会反复点保存、怀疑系统坏了，甚至照着旧数做决定。**缓存上不能打这个折。**

------------------------------------------------------------------
用的是「数据库里一个单调递增的整数」
------------------------------------------------------------------
任何影响聚合的写（凭证/分录/科目/期初/期间）都在**同一个事务里**把
``book.data_version`` +1 —— 由 PostgreSQL 触发器做，见迁移 0015。
引擎构造时**先读版本号**，再拿它当缓存键的一部分。

于是保证的是：

* 写事务提交后，**任何在此之后开始的请求**一定读到新数据（read-your-writes）；
* 多个 worker 之间天然一致 —— 版本号在库里，不在各进程手里，
  缓存只是各自的一份副本，**不是一个需要保持一致的共享状态**；
* 版本号只增不减，所以旧版本的键一旦失效就再也不会被读到
  （缓存不删除也没关系，只是占着位置直到被 LRU 挤出去）。

------------------------------------------------------------------
顺序是硬约束
------------------------------------------------------------------
**版本号必须在任何聚合查询之前读。**

反序（先查数据、后读版本号）会把「旧数据」缓存进「新版本」的键 ——
那是有害的：那个键之后会被一直命中，直到下一次写入为止。
正序（先读版本号）最多把「新数据」缓存进「旧版本」的键 —— 无害，
因为版本号只增不减，那个键永远不会再被使用。

**不要为了「省一次查询」把这个顺序换掉。** 它省下来的是一次主键查询，
换来的是最难查的一类 bug（余额对不上，且无法复现）。

------------------------------------------------------------------
缓存什么
------------------------------------------------------------------
只缓存**从库里读出来的层**：期间表、科目树/祖先链、期初汇总、发生额聚合。
**不缓存纯算术层**（上卷、`Figures` 派生）—— 它们没有 IO，留在实例内即可，
放进跨请求缓存只会白白占内存并让失效变复杂。

------------------------------------------------------------------
边界：必须有上界
------------------------------------------------------------------
压测记录了峰值内存 O(科目数 × 期间数)：317 末级 × 24 期间 = 7,608 组。
无界的进程内缓存在长驻进程里会一路涨（worker 不重启，账却越记越多）。
所以这里用有界 LRU + TTL：

* **LRU 上界**是硬约束 —— 内存不因为运行时间长而无限增长；
* **TTL 只是兜底**，不是正确性机制。正确性由版本号保证，
  所以 TTL 设成多少都不影响正确性，它只是让长期无人访问的键早点让位。
  **不要把 TTL 当成「多久算过期」来理解** —— 那个语义由版本号负责。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable


class BoundedTTLCache:
    """线程安全的有界 LRU + TTL 缓存。

    用锁而不是「假定单线程」：uvicorn/gunicorn 虽然默认单线程跑同步端点，
    但 ``scripts/loadtest.py`` 与将来的异步端点都可能并发取用同一个进程内缓存。
    临界区只有几次字典操作，锁竞争可以忽略；不锁的后果是
    ``OrderedDict`` 的 move_to_end 与 popitem 之间被插队，可能抛 KeyError。
    """

    def __init__(self, maxsize: int = 16, ttl_seconds: float = 60.0) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正数 —— 没有上界的缓存会在长驻进程里一路涨")
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get_or_set(self, key: Hashable, produce: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                expires_at, value = entry
                if now < expires_at:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return value
                # 过期只影响「何时让位」，不影响正确性：版本号已经在键里了。
                self._data.pop(key, None)
            self.misses += 1

        # **produce 必须在锁外调用。** 它会去查库，锁内查库意味着
        # 一个慢查询会把所有并发请求堵在同一个锁上 —— 那比不缓存还糟。
        value = produce()

        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)   # 挤掉最久没用的
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "size": len(self._data)}


# 进程内单例。每个 worker 一份 —— 与 nginx upstream keepalive 缓存同理，
# 那也是每 worker 一份。**这不是缺陷**：正确性由数据库里的版本号保证，
# 两份缓存各自独立不会产生任何可见差异，只是命中率各算各的。
#
# 刻意不引入 Redis/Memcached：那会给这个目前零外部依赖的系统
# 增加一个必须运维的组件，而收益（跨 worker 命中）远小于代价。
_CACHES: dict[str, BoundedTTLCache] = {}
_CACHES_LOCK = threading.Lock()


def cache_for(name: str, maxsize: int = 16, ttl_seconds: float = 60.0) -> BoundedTTLCache:
    """按名字取（并按需创建）一个进程内缓存。"""
    with _CACHES_LOCK:
        c = _CACHES.get(name)
        if c is None:
            c = BoundedTTLCache(maxsize=maxsize, ttl_seconds=ttl_seconds)
            _CACHES[name] = c
        return c


def clear_all() -> None:
    """清空所有进程内缓存。测试用它来保证用例之间不互相看到数据。"""
    with _CACHES_LOCK:
        caches = list(_CACHES.values())
    for c in caches:
        c.clear()
