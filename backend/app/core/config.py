"""全局配置。所有配置项均可用环境变量覆盖（见 .env.example）。

另含**生产配置的启动硬校验**（审计 P3-04）。审计的原话是「区分不等于
阻断」：``/api/health`` 早就靠 secret 是否以 ``dev-`` 开头判断环境了，
说明团队知道要区分，但那个判断只影响健康检查的返回值 —— 带默认
secret 的进程照样正常启动、正常签发令牌。

对一套记账系统来说这意味着：任何拿到默认配置启动的人，签出来的
token 全世界都能伪造（HS256 是对称签名，密钥就是源码里那一串）。

说明边界：这套配置**没有被利用过**，这里要修的是「不该能启动」，
不是「已经出过事」。
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("app.core.config")

# config.py 里的默认值，也就是源码里人人可见的那一串
DEFAULT_JWT_SECRET = "dev-secret-change-me-before-deploy"
# 默认数据库口令，同样写死在源码里
DEFAULT_DB_PASSWORD = "cwq123"
# HS256 下密钥长度直接决定伪造 token 的成本。太短的密钥等于没设。
MIN_SECRET_BYTES = 32

# 常见的占位符。这些串即使长度够，也说明根本没配。
WEAK_SECRETS = frozenset({
    "changeme", "change-me", "secret", "your-secret-here",
    "please-change-me", "todo", "none", "null", "password",
})

# 日志级别白名单（审计 P3-14）。
#
# 不用 logging._nameToLevel：那是个私有字典，Python 版本间会变，
# 而这里是要拿它做**启动校验**的依据。
LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")
# 生产环境禁止的级别：DEBUG 会把 SQLAlchemy 的 SQL、绑定参数连同账套数据
# 一并打进日志，而日志是被采集、被转发、被存盘的。
PROD_FORBIDDEN_LOG_LEVELS = ("DEBUG",)

# JWT 签名算法白名单（审计 P2-11）。
#
# ``jwt_algorithm`` 是 ``.env`` 里能改的项，而 ``validate_production_settings``
# 当初把密钥的长度、弱口令、默认值都查了，**唯独没查它** ——
# 于是「配对了强度、配错了算法」是一道没人拦的门。
#
# 现在的风险是潜在而不是现实的：PyJWT 在 ``algorithms=["none"]`` 下拒绝
# 无签名 token（密钥非空时抛 InvalidKeyError，空串时抛 InvalidSignatureError），
# 而 HS256 用一个 ≥32 字节的密钥去验 RS256 签的 token 也会因算法不符而失败。
# 换句话说**当前不可利用**。但它是「一次重构就会变成真漏洞」的那类缺口：
# 哪天有人把 ``security.py`` 的解码改成「按 token 自带的 alg 选算法」，
# 这道本该在这里拦下的门就再也拦不住了。
#
# 所以显式白名单：HMAC 家族（对称）三项。
ALLOWED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")

# TRUSTED_PROXIES 的最小前缀长度（审计 P3）。
#
# 这份白名单只用来决定「敢不敢读 X-Forwarded-For」。代理部署里就是几个地址，
# 没有理由写成 /8 以外的宽网段；而 0.0.0.0/0 会让限流彻底失效（见
# trusted_proxy_networks 里的说明），所以在这里给一条下限。
_MIN_PROXY_PREFIXLEN = 8


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"postgresql+psycopg://cwq:{DEFAULT_DB_PASSWORD}@localhost:15432/cwq"
    db_echo: bool = False

    # ------------------------------------------------ SQLAlchemy 连接池容量
    #
    # 原来 ``create_engine`` 只传了 ``pool_pre_ping``，四个池参数全是 QueuePool
    # 的默认值，而全仓（排除 .venv / node_modules）grep ``pool_size`` /
    # ``max_overflow`` / ``pool_timeout`` / ``pool_recycle`` **零命中** ——
    # 也就是说「连接池容量」在这套系统里从来没被决定过，只是碰巧吃到了
    # 库的默认值。把它们变成配置项不是为了调大，而是让「当前值」变成一个
    # **写下来的、可以被讨论的决定**。
    #
    # **下面三个的默认值逐字等于 QueuePool 的默认值**（5 / 10 / 30），
    # 一个都没改。改默认值会让「打了一次补丁之后并发行为悄悄变了」，
    # 而这次要解决的是「不可配置」，不是「容量不够」——
    # 后者要拿业务峰值说话，而那件事还没有数据。
    #
    # 调大会付出什么（这一段是给「想调大的人」看的）：
    #   PostgreSQL 的 ``max_connections`` 默认 100，而**每个进程各有一套池**。
    #   systemd 与 Docker 都跑 2 个 worker 进程，于是
    #       连接数 = 进程数 × (pool_size + max_overflow)
    #             = 2 × (5 + 10) = 30          ← 当前默认
    #   越过 ``max_connections`` 的表现不是「变慢」，而是**新连接直接被拒**
    #   （``FATAL: sorry, too many clients already``）：应用侧看到的是连接
    #   拿不到，排查方向会整个跑偏到「数据库慢」上，而数据库其实好好的。
    #   反方向同样有代价：每个 PG 连接在服务端是一个独立进程，池越大，
    #   一次性 fork 出来的 backend 就越多，PG 自己的内存也吃得越多。
    db_pool_size: int = 5
    # 单进程允许超出的**临时**连接数。默认 10 = QueuePool 默认。
    # 调大的代价比 pool_size 更容易被忽略：overflow 里的连接在用完归还时
    # 是**直接关闭**、不回池的。把 max_overflow 调大等于把「复用连接」
    # 换成「反复建连」，而建连要走 TCP + 认证往返，比复用贵一个量级 ——
    # 也就是说这条参数调到很大时，它会同时把并发能力和连接效率都换掉。
    db_pool_max_overflow: int = 10
    # 从池里等一个连接的最长秒数。默认 30 = QueuePool 默认。
    # 调**小**的代价：并发一上来就开始抛 ``QueuePool limit ...`` 超时，
    # 而这个错误和「数据库慢」在日志里长得几乎一样；调**大**的代价是
    # 请求会一直挂着直到客户端/网关先超时，而连接还留在池里被下一个请求
    # 拿走 —— 等于用更长的挂起时间换了一个更晚才暴露的问题。
    db_pool_timeout: int = 30
    #
    # 连接回收时间（秒）。**这是四项里唯一给了新默认值的一个。**
    #
    # 原来没配 = -1 = **永不回收**。这件事平时不暴露：连接被对面悄悄掐掉时，
    # ``pool_pre_ping`` 会在**取用时**发现死连接并重建，看起来「已经有兜底」。
    # 但 pre_ping 只在取用时检查 —— 一条躺在池里的**空闲**连接什么时候死，
    # 完全取决于对面（云 PG 的连接代理、负载均衡、云厂商 NAT/防火墙的 idle
    # timeout）。后果是：闲置一阵之后的**第一批**请求会集体走一遍
    # 「发现死连接 → 丢弃 → 重连」。在月结开头那种「大家憋了一早上、
    # 第一分钟一起点」的场景里，它表现为一次莫名其妙的整体变慢，
    # 而且只慢一次、事后极难复现（这时候连接已经是新的了）。
    #
    # 取 300 的理由：它必须**短于**云 PG / NAT 常见的 idle 断开阈值
    # （通常落在 300~600s 这一档），否则就成了「等断开之后再去检查」；
    # 同时 300s 又远大于正常请求间隔，绝大多数连接在被回收之前早已被
    # 反复复用 —— 也就是说这一项**不改变正常路径的行为**，只兜住空闲路径。
    #
    # 调小的代价：连接重建变频繁，每次都是一次完整的建连 + 认证往返，
    # 低 QPS 下会稳定地多花 CPU。调大到超过对端的 idle 断开阈值，
    # 等于把这一项关掉，也就回到原来那个问题了。
    db_pool_recycle: int = 300

    # ---- 账簿聚合的跨请求缓存（2026-10 性能收尾 P0）----
    #
    # **这两个值都不是「多久算过期」。** 正确性由数据库里的
    # ``book.data_version`` 保证（任何写都 +1，缓存键里带它），
    # 所以这里调大调小都**不影响对错**，只影响命中率与内存。
    #
    # ledger_cache_maxsize 是**硬上界**：每个 worker 各自一份缓存，
    # 而 worker 不重启、账却越记越多，没有上界的话内存会一路涨。
    # 一次全账套聚合在压测规模下是 7,608 组，所以上界不能给大。
    ledger_cache_maxsize: int = 16
    # TTL 只是让长期没人访问的键早点让位。它**不承担**过期语义。
    ledger_cache_ttl_seconds: float = 60.0

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720
    # 管理员令牌有效期上限（审计 P3-04 建议 3：令牌有效期按角色区分，管理员更短）。
    # 取「与 access_token_minutes 的较小者」而不是直接生效 ——
    # 有人把全局配成 30 分钟时，不该被管理员上限反而拉长。
    admin_token_minutes: int = 240

    # 登录失败锁定（审计 P3-04 建议 4）。
    login_max_failures: int = 5
    login_lock_minutes: int = 15

    # ------------------------------------------------ IP 维度软节流（审计 P2-11 的另一半）
    #
    # 上面的账号锁定只防「盯死一个账号」。轮换用户名就能把同一个攻击源摊到
    # 上万个账号上：每次只锁一个，锁定几乎不生效，而审计里留下的是成千上万条
    # 互不相干的 LOGIN_FAILURE，事后看不出「这些来自同一个源」。
    # 下面这一组补的是**按来源聚合**的判断。
    #
    # **为什么默认是软节流（429 + 递增延迟 + 留痕），而不是「IP 锁死」。**
    # 办公网、家庭宽带、大客户出口、运营商 NAT 后面可能是几百上千人共用一个
    # IP。按 IP 硬锁等于把这几百人一起关在门外 —— 把防护变成了故障。
    # 更糟的是它**可被主动触发**：拿到一个属于己方公司的 IP，从公司网络里反复
    # 失败几次，这个 IP 在锁定期内对全公司不可用。也就是说硬锁不是「防住攻击者」，
    # 而是「把攻击者手里的武器换成 DoS 放大器」。
    # 所以：超阈值只回 429，**不锁账号、不锁 IP**；账号锁定的原有语义一字未动。
    #
    # 阈值刻意取得比账号阈值（5）高一个数量级：账号锁定针对的是「有人在打这个
    # 账号」，而共享出口 IP 上几十个人在月结时输错口令是**正常业务噪声**。
    # 宁可让攻击者多试几次（每次仍要付 PBKDF2 的 20ms），也不能把一整个办公室
    # 挡在门外 —— 这是这条参数唯一的取舍方向。
    login_ip_throttle_enabled: bool = True
    login_ip_max_failures: int = 30
    # 滑动窗口长度（秒）。与 login_lock_minutes 的默认 15 分钟对齐，
    # 两个旋钮读起来是同一件事。
    login_ip_window_seconds: int = 900

    # 递增延迟：超阈值后每多失败一次多睡 step，上限 max。
    #
    # **默认上限只给到 1 秒，这是有意的。** 服务端 sleep 会占住一个 worker
    # 线程，而「占住 worker」正是上面刚否掉 DoS 放大器的同一种东西 ——
    # 软节流之所以不伤共享 IP，正是因为它不把请求挡在门外。更大的惩罚交给
    # **Retry-After 响应头**（随超阈值次数递增），由讲规矩的客户端自己去退避，
    # 代价不落在服务端资源上。
    login_ip_delay_step_ms: int = 200
    login_ip_delay_max_ms: int = 1000
    # Retry-After 的上限（秒）。与窗口同长：窗口一过就该重新给额度。
    login_ip_retry_after_max_seconds: int = 900

    # **默认关闭**，且下面两行注释比这个默认值更重要：
    #
    # 这是一个把「攻击者关在门外」和「无辜的人关在门外」绑成同一个动作的开关。
    # 它适合的场景是**登录页完全对外暴露、且出口 IP 一人一户**的部署；
    # 在 NAT 后面它是纯粹的可用性风险。仓库通篇的口径是 fail-closed，
    # 但 fail-closed 不等于「用一个可用性风险换一个安全风险」——
    # 这里两项都收益有限，所以默认站在不用它的那一侧，要用请显式开。
    login_ip_hard_lock: bool = False
    login_ip_hard_lock_minutes: int = 15

    # 节流状态行留多久后可以被清理（小时）。
    #
    # **必须有 TTL**：这张表是按 IP 一行、只增不减的，写入方是每一次登录失败。
    # 没有清理它就是一个只涨不落的表 —— 而 P2-23 批的正是那种「定义了却没人
    # 回收」的状态。ttl 取窗口的 9 倍（24h / 15min），保证「窗口内不删、
    # 窗口过后多久才回收」这两个语义都不会被清理动作抢跑。
    # 见 auth.py::purge_expired_login_ip_state（运维入口 + 登录路径上的顺带清理）。
    login_ip_state_ttl_hours: int = 24

    # 可信反向代理（逗号分隔的 IP 或 CIDR）。**默认空 = 一个都不信。**
    #
    # 取真实客户端 IP 通常要读 X-Forwarded-For / X-Real-IP，但**无条件信任
    # 这两个头等于把限流交给攻击者**：他每次请求换一个头就能换一个桶，
    # 于是 IP 维度限流对他是完全不存在的。fail-closed 的做法是：默认只认
    # TCP 对端（request.client.host），只有当对端落在这份白名单里才去读
    # 代理头。deploy/nginx/cwq.conf 已经透传这两个头，所以配了
    # TRUSTED_PROXIES=127.0.0.1 就能拿到真实 IP；**不配就退回到「按对端计」**，
    # 此时 nginx 后面的所有请求会共用 127.0.0.1 一个桶（见 validate_production_settings
    # 里的启动告警）。
    trusted_proxies: str = ""

    # Excel 导入的容量上限（审计 P2-02）。
    #
    # Nginx 把单请求限在 50MB，那只挡住了「一个特别大的请求」，挡不住
    # 「很多个不算大但解压后很大」的请求 —— xlsx 本身就是个 zip，
    # 1MB 的文件完全可能解压成几百 MB 的 XML。原实现是
    # ``file.file.read()`` 无上限读进内存，解析器再把每一张凭证、每一条分录、
    # 每一条错误都累积进列表，最后整包塞进 ``import_batch.payload``。
    # 于是「上传」这一个动作就足以吃掉一个进程的内存，而暂存表会一直长。
    import_max_bytes: int = 20 * 1024 * 1024            # 上传文件的字节数
    # xlsx 是 zip：这两个上限防的是解压炸弹（几百 KB 的文件解压成几百 MB）。
    # 1) 按 zip 目录里的声明大小累加，不真正解压就能判断；
    # 2) 按实际解析出来的行数与凭证数兜底 —— 声明大小可以撒谎，行数不会。
    import_max_unzipped_bytes: int = 200 * 1024 * 1024
    import_max_rows: int = 20_000
    import_max_vouchers: int = 5_000
    # 未确认的暂存批次保留多久后自动清掉（确认/作废时 payload 当场清空，
    # 但人上传完忘了点确认的批次会一直在）。见 importer.purge_stale_batches。
    import_batch_ttl_days: int = 7

    # 发票原件与解析结果的本地留存目录。数据库只保存相对文件名，便于迁移部署。
    invoice_storage_dir: str = "data/invoices"
    # 发票上传上限（审计 P2-12）。
    #
    # 原来是 100MB，而网关的 ``client_max_body_size`` 是 21m —— 两者矛盾，
    # 且**小的那个先生效**：21~100MB 的发票批次会先被 nginx 完整接收、
    # 写进临时文件，然后才被应用层拒掉。写死的那个 100MB 从来没真正生效过，
    # 只是让配置与 .env.example 的说明看起来比实际宽松。
    #
    # 这里改成与 import_max_bytes 同档（20MB），让**配置里的数字等于实际能传的**。
    # 注意这是零能力损失：100MB 本来就被 21m 的网关挡在门外。
    #
    # 要调大必须两边一起改（和 import_max_bytes 同一条规矩）：
    # 这里放开的同时把 deploy/nginx/cwq.conf 的 client_max_body_size 提上去。
    invoice_max_bytes: int = 20 * 1024 * 1024
    # 外部 PaddleOCR API。未同时配置 URL 和 token 时，发票功能仍可处理文本型 PDF，
    # 但不会尝试 OCR 兜底。
    invoice_ocr_api_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    invoice_ocr_api_token: str = ""
    invoice_ocr_model: str = "PaddleOCR-VL-1.6"
    invoice_ocr_timeout_seconds: int = 600
    invoice_ocr_poll_seconds: float = 5.0
    invoice_ocr_optional_payload: str = (
        '{"useDocOrientationClassify":false,"useDocUnwarping":false,'
        '"useChartRecognition":false}'
    )

    # 这里曾经有一个 ``default_timezone: str = "Asia/Shanghai"``，现在**没有**了
    # （审计 P2-23）。它定义了但全仓零引用，而三个真正需要时区的时源已经各自
    # 收敛到唯一的正确写法：
    #   * 写入侧一律 UTC aware（``importer.committed_at``、
    #     ``vouchers.posted_at`` 用 ``datetime.now(timezone.utc)``），
    #     列是 ``DateTime(timezone=True)``，psycopg 按会话 TimeZone 还原；
    #   * 服务端取「此刻」一律 ``func.now()``（``periods.py`` 原先的第三时源），
    #     时区由数据库会话决定，不经过 Python。
    #
    # **为什么删掉而不是接上**：留着一个看起来能配、实际什么都不做的配置项，
    # 比没有更危险 —— 运维设了 ``DEFAULT_TIMEZONE=UTC`` 会合理地相信行为变了，
    # 而什么都不会变；而真要「接上」，得让某段代码按它做时区换算，那是在已经
    # 统一成 UTC 的路径上重新引入一个换算点。
    #
    # 部署时区仍然由**进程/数据库会话**的 TZ 决定（docker-compose.yml 与
    # deploy/systemd 里的 TZ=Asia/Shanghai），这一层不归配置项管。

    # 运行环境。**默认 prod** —— 也就是「按最坏情况处理」。
    #
    # 原来的判定是「secret 以 dev- 开头 ⇒ dev」，而默认 secret 恰恰就是
    # dev-secret-change-me-before-deploy：于是默认值自己宣布自己是开发环境，
    # 校验永远跳过 —— 这正是审计说的「区分不等于阻断」的具体形态。
    # 反过来做：只有**显式**声明 APP_ENV=dev 才跳过校验。忘了配就启动
    # 失败并给出改法，这比默默带着默认密钥上线好。
    app_env: str = "prod"

    # 自助注册开关。默认**关闭**（审计 P3-04 第 2 条）：对外暴露时
    # 开放自助注册等于把系统变成任何人都能建号的公共资源，而记账系统
    # 的门槛应该是「管理员开号」。公司内部要开，显式置 true。
    registration_enabled: bool = False

    # 日志级别（审计 P3-14）。
    #
    # 以前根日志级别完全取决于进程外面怎么配（systemd / gunicorn / shell），
    # 于是「这个环境到底在打什么」是个要人去翻部署配置才能回答的问题，
    # 而默认在 root 上没有 handler 时 logging 只会 WARNING ——
    # 审计里点名的「护栏 4xx 零日志」（P2-24）就是这么来的。
    #
    # 默认 INFO：启动校验、生产配置校验、登录审计这些运维必须看得见的事
    # 在 INFO 及以上；SQL 回显、SQLAlchemy echo 那些调试内容一律不默认开。
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        """非法级别**拒绝启动**，不是静默回退到 INFO。

        静默回退看着更「宽容」，实际是把「我以为在打 DEBUG」这件事藏起来 ——
        排查时人只会去看代码，不会去看日志里少了什么。报错信息里列出合法取值，
        改法直接写在屏幕上。
        """
        v = (v or "").strip().upper()
        if v not in LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL={v!r} 不是合法日志级别，可选：{'、'.join(LOG_LEVELS)}")
        return v


def detect_env(settings: "Settings", env: str | None = None) -> str:
    """环境判定。健康检查与启动校验必须用**同一个**口径。

    显式 APP_ENV 说了算，没配就按 prod 处理 —— 理由见 ``app_env`` 字段
    的注释：判定「像不像开发环境」等于让配置自己给自己开免检，
    而默认配置恰好就长成开发环境的样子。
    """
    if env:
        return env
    return (getattr(settings, "app_env", "") or "prod").strip().lower() or "prod"


def _db_password(url: str) -> str:
    m = re.search(r"://[^:/@]*:([^@]*)@", url or "")
    return m.group(1) if m else ""


def trusted_proxy_networks(raw: str | None = None) -> tuple:
    """把 ``TRUSTED_PROXIES`` 解析成 ip_network 元组；解析不了的项**跳过并告警**。

    返回空元组表示「一个代理都不信」，这是默认状态，也是刻意的：
    信任来源判定错了会让人**低估**暴露面（「我配了代理所以 IP 是真的」），
    而这条判断反过来用（拿它当限流的绕过手段）时，误信就是真的被绕过。

    跳过而不是抛异常：运维写错一个条目不该让服务起不来，那会造成「改完配置
    才发现系统登不进去」这种更糟的故障 —— 跳过只损失一条告警，且
    ``validate_production_settings`` 会在启动时把没解析出来的条目打出来。
    """
    import ipaddress

    if raw is None:
        raw = settings.trusted_proxies
    nets = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            log.warning("TRUSTED_PROXIES 里的 %r 不是合法 IP 或 CIDR，已忽略（不生效）", item)
            continue
        # P3：给网段加一条下限。``0.0.0.0/0`` 与 ``::/0`` 在这里是**语法合法**
        # 的，但它把「信任代理」变成了「信任所有人」——
        # 而这份白名单唯一的作用就是决定敢不敢读 X-Forwarded-For。
        # 读了就等于采信客户端能随便写的那个头，于是 ``_throttle_ip``
        # （登录失败的 IP 限流）被彻底绕过：换个头就能换一个限流桶。
        #
        # 与其让它「配了就生效」地废掉限流，不如在这里拒掉并说清楚原因。
        # 单条白名单本来就不该超过 /8 —— 代理在部署里就那么几个地址。
        if net.prefixlen < _MIN_PROXY_PREFIXLEN:
            log.error(
                "TRUSTED_PROXIES 里的 %r 覆盖了几乎整个地址空间（/%d），已忽略。"
                "信任这么宽的对端等于采信客户端伪造的 X-Forwarded-For，"
                "登录 IP 限流会因此失效。请写具体的代理地址。",
                item, net.prefixlen,
            )
            continue
        nets.append(net)
    return tuple(nets)


def is_trusted_proxy(host: str | None, raw: str | None = None) -> bool:
    """这个 TCP 对端是不是我们自己部署的反向代理。

    **只拿它做一件事**：决定「能不能读 X-Forwarded-For / X-Real-IP」。
    对端不在白名单里时一律读 socket 对端 —— 那才是攻击者唯一伪造不了的东西。
    """
    import ipaddress

    nets = trusted_proxy_networks(raw)
    if not nets or not host:
        return False
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    return any(addr in net for net in nets)


def validate_production_settings(settings: "Settings", env: str | None = None) -> list[str]:
    """非 dev 环境下校验配置，不合格**拒绝启动**。

    返回通过项（写进启动日志，让运维能确认这次到底检了什么）。
    dev 环境直接放行 —— 拦掉开发机只会让人学会用绕过开关。

    一次报出全部问题，而不是让人一项项试错：配置错了就挨个猜着改，
    很容易干脆全用默认值上线。
    """
    actual = detect_env(settings, env)
    # 只有 dev/test 跳过。写成 ``== "dev"`` 的话，APP_ENV=test 会被当成
    # 生产环境拦停 —— 而测试用的 secret 本来就不该满足生产要求。
    if actual in ("dev", "test"):
        return [f"{actual} 环境，跳过生产配置校验"]

    problems: list[str] = []
    secret = settings.jwt_secret or ""

    if secret == DEFAULT_JWT_SECRET or secret.startswith("dev-"):
        problems.append(
            f"JWT_SECRET 仍是默认值（{DEFAULT_JWT_SECRET!r}）—— "
            "这个值公开在源码里，任何人都能伪造任意用户的 token")
    if len(secret.encode()) < MIN_SECRET_BYTES:
        problems.append(
            f"JWT_SECRET 只有 {len(secret.encode())} 字节，少于 {MIN_SECRET_BYTES} 字节 —— "
            "HS256 是对称签名，密钥短到这个程度，伪造 token 的成本可以忽略")
    if secret.strip().lower() in WEAK_SECRETS:
        problems.append(f"JWT_SECRET 是常见占位符 {secret.strip()!r}，等于没设")

    # P2-11：密钥查够了，算法这一项不能是唯一的例外。
    # ``none`` 与任何非 HMAC 的算法名都要在这里拦下 —— 理由见 ALLOWED_JWT_ALGORITHMS。
    if settings.jwt_algorithm not in ALLOWED_JWT_ALGORITHMS:
        problems.append(
            f"JWT_ALGORITHM={settings.jwt_algorithm!r} 不在白名单里 "
            f"（{'、'.join(ALLOWED_JWT_ALGORITHMS)}）—— "
            "改成 none 或非 HMAC 算法等于关掉签名校验")

    if _db_password(settings.database_url) == DEFAULT_DB_PASSWORD:
        problems.append(
            f"数据库口令仍是默认值（{DEFAULT_DB_PASSWORD!r}）—— "
            "同样公开在源码里")

    # P3-14：级别配置对了不代表级别**安全**。生产开 DEBUG 时，
    # DB_ECHO 一旦也开着，SQLAlchemy 会把带绑定参数的 SQL 全打进日志 ——
    # 也就是把全公司的凭证金额复制进日志系统，而日志通常比数据库备份还多人能读。
    if settings.log_level in PROD_FORBIDDEN_LOG_LEVELS:
        problems.append(
            f"LOG_LEVEL={settings.log_level} 不允许用于生产环境 —— "
            "调试级内容（若同时 DB_ECHO=true，还包括全量 SQL 与账套数据）"
            "不该进生产日志。排查请临时开，看完就改回来。")

    # 上面那条只挡住了「DEBUG + DB_ECHO」这一个组合。**DB_ECHO 自己就该拦。**
    # 它不需要 DEBUG 级别照样生效：echo 是交给 SQLAlchemy engine 的，
    # 与 Python 的日志级别是两套机制。所以 LOG_LEVEL=INFO + DB_ECHO=true
    # 能从上一条下面完整走过去 —— 校验通过、正常启动，然后安静地把
    # 全公司每一张凭证的金额写进日志文件，而没有任何一行警告。
    #
    # 这正是「级别配对了不代表内容安全」的另一半：上一条管的是日志级别，
    # 这一条管的是引擎开关，两个都得单独看。
    if settings.db_echo:
        problems.append(
            "DB_ECHO=true 不允许用于生产环境 —— 它会把带绑定参数的 SQL 全量"
            "写进日志，也就是把全公司的凭证金额复制进日志系统"
            "（日志通常比数据库备份还多人能读）。"
            "排查请临时开，看完就改回来。")

    if problems:
        raise ValueError(
            "生产配置校验不通过，拒绝启动：\n  - "
            + "\n  - ".join(problems)
            + "\n  改法：生成一个随机长串 openssl rand -hex 32，"
              "写进部署环境的 JWT_SECRET；数据库口令一并换掉。"
              "若确实只想在开发环境跑，设 APP_ENV=dev。")

    checks = [
        f"JWT_SECRET 非默认值且 ≥{MIN_SECRET_BYTES} 字节",
        "数据库口令非默认值",
        f"日志级别 {settings.log_level}（生产不允许 DEBUG）",
    ]
    log.info("生产配置校验通过：%s", "；".join(checks))

    # **告警而不是拒绝启动。** 这条部署形态（nginx → 127.0.0.1:8000，见
    # deploy/nginx/cwq.conf）下，TRUSTED_PROXIES 留空意味着所有请求的 TCP
    # 对端都是 127.0.0.1 —— IP 维度节流于是退化成「全站一个桶」，任何人的
    # 连续输错都可能把所有人一起挡一会儿。软节流不至于锁死谁，但这是运维
    # 该知道的一件事。
    #
    # 为什么不当成 problem（拒绝启动）：默认配置已经在挡着一大批真实部署，
    # 把它们全部变成「升级之后起不来」是拿可用性换一个已经由 429 兜着的风险。
    # 宁可用一条启动告警把话说清楚。
    if not trusted_proxy_networks(settings.trusted_proxies):
        log.warning(
            "TRUSTED_PROXIES 为空：IP 维度登录节流只按 TCP 对端计。"
            "若本服务在反向代理后面（nginx → 127.0.0.1:8000），"
            "所有用户会共用同一个来源地址，节流会误伤；"
            "请设 TRUSTED_PROXIES=127.0.0.1（按实际对端）后重启。"
            "不设也能工作 —— 节流是软节流，只回 429，不锁账号。")
    elif not settings.login_ip_throttle_enabled:
        log.warning(
            "LOGIN_IP_THROTTLE_ENABLED=false：IP 维度登录节流已关闭，"
            "轮换用户名的爆破将不受任何聚合限制（这是显式的 fail-open）。")

    return checks


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
