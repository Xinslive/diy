"""阿里云 OSS 客户端：建连接、传对象、列对象、删对象。

**oss2 是延迟导入的。** 后端 web 进程本身不需要 OSS —— 它只在有人点「立即
备份」或定时脚本跑起来时才用到。让整个应用因为一个备份用的 SDK 没装而
起不来，是把一个「备份坏了」放大成「系统打不开」。延迟导入 + 下面的报错文案，
让缺依赖这件事只影响备份，并且说清楚缺的是什么。

（对照 ``python-multipart`` 的处理：那个**必须**装在顶层的 FastAPI 靠它解析
上传请求，缺了是「收到请求才 500」。两种情况的取舍相反，判断依据是
「缺了会打掉多少东西」。）
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 超时：不显式写，就用 SDK 默认的 60 秒（oss2.defaults.connect_timeout）
# ---------------------------------------------------------------------------
#
# **为什么必须显式写，以及那个 60 错在哪。**
#
# oss2 把 ``connect_timeout`` 原样当成 ``requests`` 的 ``timeout=`` 用
# （api.py：``self.timeout = defaults.get(connect_timeout, defaults.connect_timeout)``，
# 之后每次 ``session.do_request(req, timeout=self.timeout)``）。所以：
#
#   * 它的默认值是 **60 秒**，与「这一次调用要做的事」毫无关系 —— 列一页对象
#     和传一个几百 MB 的账套快照拿到的是同一个数。
#   * 它是 requests 的**标量**超时，也就是「建连 60 秒 **且** 读 60 秒」。
#     连接被墙住时，每个操作都先硬等 60 秒才报错，一轮备份 N 次调用就是 N×60s。
#   * oss2 **没有重试**（2.19.1 里没有任何 retry 逻辑）。所以这一层的超时是
#     「多久承认失败」的唯一闸门 —— 它不是保护，是**上限**。
#
# 实测过：把 ``put_object`` 指向黑洞地址 10.255.255.1，**恰好 60.0 秒**后
# 抛 RequestError(ConnectTimeoutError)。所以「一次调用会无限挂住」这个假设是
# 错的（别再照那个假设写结论），但「一次失败要等一分钟」是真的。
#
# 拆成 (建连, 读写) 是因为这两件事的合理时长差一个数量级：TCP+TLS 握手在同
# 机房是毫秒级的，10 秒还不成就是网络不通；而一个大账套的 PUT 可能要几十秒。
# requests 接受二元组，oss2 原样透传（上面那行就是唯一的转换点）。
OSS_CONNECT_TIMEOUT = 10
OSS_IO_TIMEOUT = 60
OSS_TIMEOUT = (OSS_CONNECT_TIMEOUT, OSS_IO_TIMEOUT)

# 「测试连接」是**给人点的按钮**，等一分钟才报错是不能接受的。
# 它只写一个 17 字节的探针对象再删掉，正常链路是毫秒级。
OSS_PROBE_TIMEOUT = (5, 15)


class OssError(Exception):
    """OSS 调用失败。消息必须能让人不看源码就知道下一步做什么。"""


def _import_oss2():
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的路径
        raise OssError(
            "缺少 oss2 依赖。备份功能需要它：请在 backend 目录执行 "
            "`pip install -r requirements.txt`（或 requirements.lock）后重启。"
        ) from exc
    return oss2


def normalize_endpoint(endpoint: str) -> str:
    """把用户填的 endpoint 归一化成 oss2 要的形状（``host[:port]``，无 scheme）。

    界面上有人会照着阿里云控制台抄 ``https://oss-cn-hangzhou.aliyuncs.com``，
    有人会抄不带协议的。两种都要能活 —— 归一化放在这里，而不是让用户去猜
    哪一种才对。**只去协议，不去区域**：``oss-cn-hangzhou.aliyuncs.com`` 里的
    ``cn-hangzhou`` 是寻址的一部分，去掉它会连到内网地址上去。
    """
    ep = (endpoint or "").strip()
    for scheme in ("https://", "http://"):
        if ep.lower().startswith(scheme):
            ep = ep[len(scheme):]
            break
    return ep.rstrip("/")


def make_bucket(cfg: dict[str, Any], *, timeout: tuple[float, float] = OSS_TIMEOUT):
    """按生效配置建一个 Bucket 句柄。

    ``timeout`` 是 ``(建连秒, 读写秒)``，默认 :data:`OSS_TIMEOUT`。**不写它就
    等于默认 60 秒** —— 理由见文件头那段。探针类调用（:func:`check_connection`）
    要显式传短一点的 :data:`OSS_PROBE_TIMEOUT`。

    拿到句柄之后**必须**配一个 :func:`close_bucket`，否则连接池不会关。
    """
    oss2 = _import_oss2()
    endpoint = normalize_endpoint(cfg.get("endpoint", ""))
    try:
        auth = oss2.Auth(cfg["access_key_id"].strip(), cfg["access_key_secret"].strip())
        return oss2.Bucket(auth, endpoint, cfg["bucket"].strip(),
                           connect_timeout=timeout)
    except oss2.exceptions.OssError as exc:
        raise OssError(f"连接 OSS 失败（{endpoint}）：{exc}") from exc
    except KeyError as exc:
        raise OssError(f"OSS 配置缺字段 {exc}，请到「系统管理 → 数据安全」检查。") from exc


def close_bucket(bucket) -> None:
    """显式关掉底层 ``requests.Session`` 的连接池。

    **为什么不能靠 GC。** 每个 Bucket 自带一个 Session，它的连接池里是**真的
    TCP 连接**；不动它的话，池子要等 urllib3 的 ``clear_stale_connections``
    按 keep-alive 超时（服务端侧的空闲上限）才回收。实测在 worker 上看到过一条
    长期挂着 ``CLOSE_WAIT`` 的连接，目的地正是 OSS 端点
    （``oss-cn-heyuan.aliyuncs.com`` 解析到 ``47.113.155.25``）。

    CLOSE_WAIT 的含义是「对端已经发了 FIN，本地还没 close」—— 也就是说
    **本地这一侧还在占着一个 fd**。一轮备份几十次调用、一天一次，攒下来是
    「fd 越来越多、而日志里什么都看不出来」：不报错、不掉帧，只是慢慢逼近
    ``LimitNOFILE``，然后以 ``OSError: [Errno 24] Too many open files`` 的形式
    在最不相干的地方炸掉。

    所以这里**吞掉关闭时的异常**：关不掉连接池不是备份失败的原因，
    让它把一次已经成功的备份报成失败更糟。
    """
    session = getattr(bucket, "session", None)
    if session is None:
        return                      # 测试替身没有 session，正常
    try:
        session.close()
    except Exception:  # noqa: BLE001 - 见上面那段说明
        pass


@contextlib.contextmanager
def oss_bucket(cfg: dict[str, Any], *, timeout: tuple[float, float] = OSS_TIMEOUT):
    """建 Bucket、**用完一定关**。

    和 :func:`close_bucket` 配对的便捷写法。注意它内部仍然走
    :func:`make_bucket`，所以测试里替换 ``make_bucket`` 依然有效。
    """
    bucket = make_bucket(cfg, timeout=timeout)
    try:
        yield bucket
    finally:
        close_bucket(bucket)


@dataclass(frozen=True)
class RemoteObject:
    """一个远端对象的**元数据**（不下载内容）。"""
    key: str
    size: int
    last_modified: datetime | None


def put_bytes(bucket, key: str, data: bytes) -> str:
    """上传一个对象，返回它的 ETag。

    **传的是内存里的字节而不是文件名。** 上传成功后 ETag 若是 ``"D8E..."``
    这样的非 MD5（十六进制 32 位），那不是「文件算错了」，而是 oss2 走了
    分片上传 —— 分片对象的 ETag 由分片校验值拼成，形状本来就不同。
    把它当成校验失败去报警，会让每一次大备份都变成一次假警。真正的完整性
    校验走我们自己算的 SHA256（记在 ``sys_backup_run.checksum``）。
    """
    oss2 = _import_oss2()
    try:
        return bucket.put_object(key, data).etag
    except oss2.exceptions.OssError as exc:
        raise OssError(f"上传失败：{key} → {exc}") from exc


def iter_objects(bucket, prefix: str) -> Iterator[RemoteObject]:
    """列出前缀下的全部对象。

    **不加 delimiter。** 加了 ``delimiter='/'`` 就只回一层「目录」，
    而我们要清理的对象全在二级目录（``daily/book-3/xxx.json.gz``）——
    结果是「列出来是空的，于是什么都删不掉」，而且日志上看不出异常。
    """
    oss2 = _import_oss2()
    try:
        for obj in oss2.ObjectIteratorV2(bucket, prefix=prefix):
            yield RemoteObject(key=obj.key, size=int(obj.size or 0),
                               last_modified=obj.last_modified)
    except oss2.exceptions.OssError as exc:
        raise OssError(f"列举 {prefix} 失败：{exc}") from exc


def delete_object(bucket, key: str) -> None:
    oss2 = _import_oss2()
    try:
        bucket.delete_object(key)
    except oss2.exceptions.NoSuchKey:
        # 别人刚清过，或上一轮重试删过了。不算失败 —— 重试一个已达成的结果
        # 本来就不该报错，否则一次并发清理会报出一堆假错。
        return
    except oss2.exceptions.OssError as exc:
        raise OssError(f"删除失败：{key} → {exc}") from exc


def check_connection(cfg: dict[str, Any]) -> str:
    """连通性检查。**用列 Bucket 的方式而不是 HEAD Bucket。**

    HEAD Bucket 在权限模型下容易被拒：一个只有 ``oss:PutObject`` 的 AK
    （正是备份该用的那种最小权限）列不了 Bucket，于是「测试连接」永远是红的，
    用户会以为配置错了去要更大的权限 —— 而那等于把写权限之外的东西也给了出去。
    列前缀（``prefix`` 本身，列不到东西也没关系）才是与「上传」同一条授权路径。
    """
    prefix = (cfg.get("prefix") or "").strip("/")
    key = f"{prefix}/.healthcheck" if prefix else ".healthcheck"
    probe = b"cwq oss healthcheck\n"
    # 短超时 + 用完关池：这个调用是**给人点的按钮**，等一分钟才报错不可接受，
    # 而一个只用来判断通不通的连接留着不放就是白占 fd。
    with oss_bucket(cfg, timeout=OSS_PROBE_TIMEOUT) as bucket:
        # 写一个极小的探测对象再删掉：只做 HEAD/GET 的话，AK 到底能不能写
        # 根本没被验证过，而「能不能写」正是这里唯一要紧的问题。
        put_bytes(bucket, key, probe)
        delete_object(bucket, key)
    return f"可写入 {cfg['bucket'].strip()}/{key.rsplit('/', 1)[0] or ''}"
