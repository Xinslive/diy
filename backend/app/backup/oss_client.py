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
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

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


# ---------------------------------------------------------------------------
# 内网 Endpoint：`-internal` 只能在阿里云 VPC 里访问
# ---------------------------------------------------------------------------
#
# 阿里云 OSS 控制台上「内网 Endpoint」就贴在「公网 Endpoint」旁边，**只差一个
# `-internal`**，而配错的代价很不成比例：
#
#   * 每个对象先干等一个**连接超时**才报错，一轮几十本就是几十分钟；
#   * 报错里只有一个没人认识的主机名（``xxx.oss-cn-heyuan-internal.aliyuncs.com``），
#     读不出「不是我配错了格式，是这台机器根本到不了那个网段」。
#
# 而这台机器确实到不了：``-internal`` 解析出来的是 VPC 里的地址（在本机上还会
# 被隧道软件劫持进 CGNAT 段），公网可达性为零。所以凡是把这个端点报给人看的
# 地方，都要补一句人话 —— 报错文案的首要职责是让人**不用查 DNS 就知道下一步**。
INTERNAL_ENDPOINT_MARK = "-internal.aliyuncs.com"

INTERNAL_ENDPOINT_NOTE = (
    "注意：这个 endpoint 带 -internal，是阿里云的**同地域内网**地址，"
    "只有在阿里云 VPC 内部才能访问。如果后端不在阿里云 VPC 里（通常如此），"
    "请改成不带 -internal 的公网 Endpoint。"
)


def is_internal_endpoint(endpoint: str) -> bool:
    """这个 endpoint 是不是只在 VPC 内可达的内网地址。"""
    return normalize_endpoint(endpoint or "").lower().endswith(
        INTERNAL_ENDPOINT_MARK)


def _note_for(bucket) -> str:
    """按这个 Bucket 用的端点决定要不要补内网提示。

    端点由 :func:`make_bucket` 挂在句柄上：报错发生在 ``put_bytes`` /
    ``iter_objects`` / ``delete_object`` 里，而这三处只有 ``bucket``，
    没有 cfg —— 让它们各自再读一遍配置，就多了一处会走样的地方。
    """
    endpoint = getattr(bucket, "_cwq_endpoint", "") or ""
    return INTERNAL_ENDPOINT_NOTE if is_internal_endpoint(endpoint) else ""


# ---------------------------------------------------------------------------
# scheme：**只走 https**
# ---------------------------------------------------------------------------
#
# **这里曾经把备份制品放在明文上。** 原来的归一化把 ``https://`` / ``http://``
# 一起剥掉，交给 oss2 的是裸 host；而 oss2 看到没有 scheme 会**自己补
# ``http://``**（``oss2/api.py::_normalize_endpoint``）。于是配置里明明写着
# ``https://oss-cn-hangzhou.aliyuncs.com``，实际发出去的是
# ``http://...``：备份正文（全账套凭证、金额、用户口令散列）明文过网，
# ``Authorization`` 头里的 AccessKey ID 与请求签名也明文过网 —— 而那个签名
# 是针对该请求算的，捕获之后可以**重放同一个 PUT**，覆盖 OSS 上的备份对象。
#
# 归一化因此**保留 scheme**：缺省按 :data:`DEFAULT_ENDPOINT_SCHEME` 处理，
# 显式写了 ``http://`` 的**拒绝**（见 :func:`ensure_secure_endpoint`）。
# 拒绝而不是「告警后放行」：告警是给人看的，而这一条拦的是**制品过网**，
# 放行的话每一次备份都还在明文上走。
DEFAULT_ENDPOINT_SCHEME = "https://"

#: 显式写了 ``http://`` 时给运维看的文案。必须自带「改成什么」，
#: 否则这条报错只会被当成「配置坏了」。
INSECURE_ENDPOINT_NOTE = (
    "OSS endpoint 必须是 https://。备份制品里是全账套凭证、金额与用户密码散列，"
    "而 Authorization 头里的 AccessKey ID 与请求签名也会跟着明文过网 —— "
    "捕获后可重放同一个 PUT，覆盖 OSS 上的备份对象。\n"
    "  请把 endpoint 改成带 https:// 的形式，例如："
    "https://oss-cn-shanghai.aliyuncs.com"
)


# ---------------------------------------------------------------------------
# last_modified：SDK 给的类型不止一种
# ---------------------------------------------------------------------------
#
# **这一层不是洁癖，是必需的。** oss2 2.19.1 的列表接口
# （``xml_utils.parse_list_objects_v2``）是这样赋值的：
#
#     SimplifiedObjectInfo(..., iso8601_to_unixtime(_find_tag(contents_node, 'LastModified')), ...)
#
# 而 ``iso8601_to_unixtime`` 返回 ``calendar.timegm`` 的结果 —— **int 纪元秒**。
# 同一个字段、同一个 SDK：``head_object`` 那条路给的是 datetime，列表这条路给 int。
#
# 代价不是「类型不优雅」：:attr:`RemoteObject.last_modified` 声明的是 datetime，
# 下游就按声明去用（``lm.tzinfo``），于是**每一轮清理都崩** —— 崩在备份已经
# 全部上传完之后，形态是「备份失败了」，而数据其实一份不差。
#
# 归一放在**边界**这一处：下游（判龄）不必为每一种 SDK 版本的分叉各写一遍。
def normalize_last_modified(raw: Any) -> Optional[datetime]:
    """把 SDK 给的 last_modified 归一成带时区的 datetime（认不出来就返回 None）。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        # naive 的一律按 UTC 解：oss2 是从 HTTP 的 Last-Modified 头取的，那是 UTC。
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str):
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return normalize_last_modified(datetime.fromisoformat(text))
        except ValueError:
            return None
    # 认不出来的形状一律当「没有」：判龄按「刚写下」处理（多留一份），
    # 好过在这里抛一个把整轮备份带下水的异常。
    return None


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
    """把用户填的 endpoint 归一化成 oss2 要的形状（**带 scheme**）。

    界面上有人会照着阿里云控制台抄 ``https://oss-cn-hangzhou.aliyuncs.com``，
    有人会抄不带协议的。两种都要能活 —— 归一化放在这里，而不是让用户去猜
    哪一种才对。

    **scheme 必须留下**（审计 P1-1）。原来这里把 ``https://`` 与 ``http://``
    一起剥掉，交给 oss2 的是裸 host，而 oss2 看到没有 scheme 会**自己补
    ``http://``**（``oss2/api.py::_normalize_endpoint``）—— 配置里明明写着
    ``https://``，实际发出去的是明文 ``http://``。

    缺省按 :data:`DEFAULT_ENDPOINT_SCHEME`（https）处理；显式写了 ``http://``
    的**原样保留**，由 :func:`ensure_secure_endpoint` 拒绝 —— 归一化只管
    「形状」，「能不能这么用」是另一件事。

    **只去协议、不去区域**：``oss-cn-hangzhou.aliyuncs.com`` 里的
    ``cn-hangzhou`` 是寻址的一部分，去掉它会连到内网地址上去。
    """
    ep = (endpoint or "").strip()
    if not ep:
        return ""
    lowered = ep.lower()
    if lowered.startswith("https://"):
        scheme, rest = "https://", ep[len("https://"):]
    elif lowered.startswith("http://"):
        scheme, rest = "http://", ep[len("http://"):]
    else:
        scheme, rest = DEFAULT_ENDPOINT_SCHEME, ep
    return scheme + rest.rstrip("/")


def ensure_secure_endpoint(endpoint: str) -> str:
    """归一化 endpoint，并**拒绝明文 http://**。

    抛 :class:`OssError`（消息自带「改成什么」），不静默降级也不放行 ——
    放行的代价是**每一次备份都在明文上走**，而它不会报错。
    """
    ep = normalize_endpoint(endpoint)
    if not ep:
        raise OssError(
            "还没有配置 OSS endpoint。请到「系统管理 → 数据安全」填写，"
            "格式为 https://oss-cn-shanghai.aliyuncs.com。"
        )
    if ep.lower().startswith("http://"):
        raise OssError(INSECURE_ENDPOINT_NOTE)
    return ep


def make_bucket(cfg: dict[str, Any], *, timeout: tuple[float, float] = OSS_TIMEOUT):
    """按生效配置建一个 Bucket 句柄。

    ``timeout`` 是 ``(建连秒, 读写秒)``，默认 :data:`OSS_TIMEOUT`。**不写它就
    等于默认 60 秒** —— 理由见文件头那段。探针类调用（:func:`check_connection`）
    要显式传短一点的 :data:`OSS_PROBE_TIMEOUT`。

    **endpoint 在这里被校验成 https**（:func:`ensure_secure_endpoint`）——
    这是所有出网路径的**唯一收口**：``put_bytes`` / ``iter_objects`` /
    ``delete_object`` / ``check_connection`` 全都经过 ``make_bucket``，
    所以不存在「某条路忘了校验」。

    拿到句柄之后**必须**配一个 :func:`close_bucket`，否则连接池不会关。
    """
    endpoint = ensure_secure_endpoint(cfg.get("endpoint", ""))
    oss2 = _import_oss2()
    note = INTERNAL_ENDPOINT_NOTE if is_internal_endpoint(endpoint) else ""
    try:
        auth = oss2.Auth(cfg["access_key_id"].strip(), cfg["access_key_secret"].strip())
        bucket = oss2.Bucket(auth, endpoint, cfg["bucket"].strip(),
                             connect_timeout=timeout)
    except oss2.exceptions.OssError as exc:
        raise OssError(f"连接 OSS 失败（{endpoint}）：{exc}{note}") from exc
    except KeyError as exc:
        raise OssError(f"OSS 配置缺字段 {exc}，请到「系统管理 → 数据安全」检查。") from exc
    # 挂上去，好让后面 put/列/删 报错时能判断该不该补内网提示（见 _note_for）。
    try:
        bucket._cwq_endpoint = endpoint
    except AttributeError:      # pragma: no cover - 用了 __slots__ 的替身
        pass
    return bucket


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
        raise OssError(f"上传失败：{key} → {exc}{_note_for(bucket)}") from exc


def put_file(bucket, key: str, path: str) -> str:
    """**从文件**上传一个对象，返回它的 ETag。

    **为什么要有它。** 备份的产物是「边导边写」的 gzip 文件，导出那一层的
    设计意图是「内存与账套大小无关」。但上传环节如果用 ``put_bytes``，
    就得先把整个文件读进内存 —— 于是**备份任务跑在 API 进程里，与在线请求
    共享同一个进程的内存**：一本大账套的快照会把在线请求的内存吃掉。

    这里走 oss2 的**按文件**上传（``put_object_from_file``）：oss2 自己
    打开文件、流式读，内存里只有一块分片。**代价要说清楚**：oss2 的这个接口
    会把文件读进它的分片缓存，单个分片仍在内存里，但**不再与账套大小成正比**。

    **第二个参数是「本地文件路径」，不是文件句柄。**
    oss2 的签名是 ``put_object_from_file(key, filename)`` —— 它自己会
    ``open(filename)``。传句柄进去，oss2 拿句柄当路径用，抛的是
    ``TypeError: expected str, bytes or os.PathLike object, not BufferedReader``。

    这个错已经犯过一次：``put_file`` 是 2026-10-23 的安全审查整改里新加的
    （那个提交之前走 ``put_bytes``，一切正常），加进来就把句柄传了进去 ——
    于是**离站备份从那天起每天失败，一个对象都没传上去**。更糟的是测试是绿的：
    替身照着实现写（收句柄），而不是照 SDK 的契约写（收路径），两边一起错，
    错得整整齐齐。

    ETag 的形状与 ``put_bytes`` 一样可能是非 MD5 的十六进制串（分片上传），
    真正的完整性校验仍然走我们自己算的 SHA256（``sys_backup_run.checksum``）。
    """
    oss2 = _import_oss2()
    try:
        return bucket.put_object_from_file(key, path).etag
    except FileNotFoundError as exc:
        raise OssError(f"上传失败：本地文件不存在 {path}") from exc
    except oss2.exceptions.OssError as exc:
        raise OssError(f"上传失败：{key} → {exc}{_note_for(bucket)}") from exc


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
                               last_modified=normalize_last_modified(
                                   getattr(obj, "last_modified", None)))
    except oss2.exceptions.OssError as exc:
        raise OssError(f"列举 {prefix} 失败：{exc}{_note_for(bucket)}") from exc


def delete_object(bucket, key: str) -> None:
    oss2 = _import_oss2()
    try:
        bucket.delete_object(key)
    except oss2.exceptions.NoSuchKey:
        # 别人刚清过，或上一轮重试删过了。不算失败 —— 重试一个已达成的结果
        # 本来就不该报错，否则一次并发清理会报出一堆假错。
        return
    except oss2.exceptions.OssError as exc:
        raise OssError(f"删除失败：{key} → {exc}{_note_for(bucket)}") from exc


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
