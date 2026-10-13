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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"postgresql+psycopg://cwq:{DEFAULT_DB_PASSWORD}@localhost:15432/cwq"
    db_echo: bool = False

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

    default_timezone: str = "Asia/Shanghai"

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

    if _db_password(settings.database_url) == DEFAULT_DB_PASSWORD:
        problems.append(
            f"数据库口令仍是默认值（{DEFAULT_DB_PASSWORD!r}）—— "
            "同样公开在源码里")

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
    ]
    log.info("生产配置校验通过：%s", "；".join(checks))
    return checks


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
