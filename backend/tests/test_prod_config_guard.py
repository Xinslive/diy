"""生产配置的启动硬校验（审计 P3-04）。

审计的原话是「**区分不等于阻断**」：``/api/health`` 早就靠 secret 是否
以 ``dev-`` 开头来判断环境了，说明团队知道要区分，但那个判断只影响
健康检查的返回值 —— 带着默认 secret 的进程照样正常启动、正常签发令牌。

对一套记账系统来说这意味着：任何拿到默认配置启动的人，
签出来的 token 全世界都能伪造（HS256 是对称签名，密钥就是源码里的
那一串）。

这些配置**没有被利用过**，这里要修的是「不该能启动」而不是「已出事」。
所以用例直接调校验函数，而不是真的起进程 —— 校验逻辑必须能脱离
FastAPI 启动单独测。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, validate_production_settings

GOOD_SECRET = "a" * 48
# 库口令 16 位（审查 L-08：生产校验现在要求库口令 ≥16 字符，且**按真实
# 口令算**而不是按连接串里的百分号编码字面量算）。
GOOD_DB = "postgresql+psycopg://cwq:Str0ngP%40ssXYZwAB@db.internal:5432/cwq"


def _prod(**kw):
    """一份**合规**的生产配置。

    ``trusted_proxies`` 与 ``mcp_allowed_hosts`` 也在里面：这两项现在是
    **阻断**级的（审查 §3.44 / §4.3-2），留空就起不来 —— 所以「合规」的
    基准配置必须把它们配上（豁免口见 ``*_WARN_ONLY``，另有专门的文件
    ``tests/test_audit_fix_prod_config_gates.py`` 逐项验）。
    """
    base = dict(jwt_secret=GOOD_SECRET, database_url=GOOD_DB,
                trusted_proxies="127.0.0.1",
                mcp_allowed_hosts="acct.example.com:*")
    base.update(kw)
    return Settings(**base)


def _check(s):
    """按**生产**环境校验。

    必须显式传 env="prod"：conftest 里设了 APP_ENV=test，不写的话
    detect_env 会返回 test，校验直接跳过 —— 下面这些用例就全成了
    「DID NOT RAISE」。这是本文件第一个版本的真实经历。
    """
    return validate_production_settings(s, env="prod")


# --------------------------------------------------------------------- 正常放行


def test_合规生产配置通过校验():
    _check(_prod())          # 不抛异常即通过


def test_默认配置在没人显式声明环境时也必须被拦下():
    """**这条是整个修复的核心。**

    审计说的「区分不等于阻断」具体就是这个形态：旧口径是
    「secret 以 dev- 开头 ⇒ dev」，而默认 secret 恰好就叫
    dev-secret-change-me-before-deploy —— 配置自己给自己开了免检，
    于是带默认密钥启动一路畅通。

    所以必须验证：什么都不配（app_env 取默认 prod）时，默认配置被拦。
    显式 app_env="" 是为了不让 conftest 里的 APP_ENV=test 混进来。
    """
    with pytest.raises(ValueError):
        validate_production_settings(
            Settings(jwt_secret="dev-secret-change-me-before-deploy", app_env=""))


def test_dev环境不校验():
    """开发机用默认 secret 是正常的 —— 拦掉它只会让人学会用绕过开关。"""
    s = Settings(jwt_secret="dev-secret-change-me-before-deploy")
    validate_production_settings(s, env="dev")
    validate_production_settings(s, env="test")


# --------------------------------------------------------------------- 必须拦下


def test_默认secret拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(jwt_secret="dev-secret-change-me-before-deploy"))
    assert "JWT" in str(e.value).upper() or "secret" in str(e.value).lower()


def test_secret太短拒绝启动():
    """HS256 的密钥长度直接决定暴力破解的成本。太短等于没设。"""
    with pytest.raises(ValueError):
        _check(_prod(jwt_secret="short"))


@pytest.mark.parametrize("secret,why", [
    ("dev-secret-change-me-before-deploy", "官方默认值"),
    ("changeme", "常见的占位符"),
    ("secret", "secret 字样"),
    ("your-secret-here", "模板占位符"),
])
def test_弱口令清单里的secret一律拒绝(secret, why):
    with pytest.raises(ValueError):
        _check(_prod(jwt_secret=secret))


def test_数据库URL带默认口令拒绝启动():
    """config.py 里的默认口令是 cwq123，写死在源码里。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(
            database_url="postgresql+psycopg://cwq:cwq123@localhost:15432/cwq"))
    assert "口令" in str(e.value) or "密码" in str(e.value)


def test_一次报出所有问题():
    """配置错了就让人一项项试错，是把人往「干脆全用默认值」推。
    该一次说清哪儿不对。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(
            jwt_secret="dev-secret-change-me-before-deploy",
            database_url="postgresql+psycopg://cwq:cwq123@localhost:5432/cwq"))
    msg = str(e.value)
    assert msg.count("\n") >= 2 or "；" in msg or "," in msg, (
        f"应当一次列出全部问题，实际：{msg}")


# --------------------------------------------------------------------- 健康检查口径


def test_健康检查的环境判定与校验用的是同一个口径():
    """原来健康检查靠 secret 是否以 dev- 开头判断环境 —— 那只是**显示**。

    如果校验用另一套口径，就会出现「校验说你是 prod、健康检查说你是
    dev」这种自相矛盾，排查时很难看出来。
    """
    from app.core.config import detect_env

    # 显式指定优先
    assert detect_env(_prod(), env="dev") == "dev"
    assert detect_env(_prod(), env="prod") == "prod"
    # **没声明就是 prod**：判定「像不像开发环境」等于让配置自己给自己
    # 开免检，而默认配置恰好就长成 dev-secret-* 的样子。
    #
    # 这里必须显式 app_env=""：conftest 设了 APP_ENV=test，pydantic-settings
    # 会把它读进 Settings，不覆盖的话读到的就不是默认值了。
    assert detect_env(Settings(jwt_secret=GOOD_SECRET, app_env="")) == "prod"
    assert detect_env(Settings(jwt_secret="dev-secret-change-me-before-deploy",
                               app_env="")) == "prod"
    assert detect_env(Settings(app_env="dev")) == "dev"


def test_校验会记入日志(caplog):
    """运维要能从日志确认「这次启动到底过了哪些检查」。"""
    import logging

    with caplog.at_level(logging.INFO, logger="app.core.config"):
        _check(_prod())
    assert caplog.records, "配置校验结果没有写进日志"


# --------------------------------------------------------------------- 日志级别


def test_日志级别默认INFO且可配置():
    """P3-14：级别以前完全取决于外部环境怎么配。"""
    assert _prod().log_level == "INFO"
    assert _prod(log_level="WARNING").log_level == "WARNING"
    # 小写也要认 —— 部署脚本里手写 "debug" 的人比你想象的多
    assert _prod(log_level="debug").log_level == "DEBUG"


def test_生产环境开DEBUG拒绝启动():
    """DEBUG + DB_ECHO 会把带绑定参数的 SQL 连同账套数据打进日志。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(log_level="DEBUG"))
    assert "LOG_LEVEL" in str(e.value)


def test_dev环境允许DEBUG():
    """开发机要能开调试，否则「本地能复现、日志里看不到」两件事会互相打架。"""
    s = _prod(log_level="DEBUG")
    validate_production_settings(s, env="dev")


def test_非法日志级别直接拒绝构造():
    """静默回退到 INFO 会把「我以为在打 DEBUG」藏起来：没人会去看少了什么。"""
    with pytest.raises(Exception) as e:
        Settings(jwt_secret=GOOD_SECRET, log_level="TRACE")
    assert "LOG_LEVEL" in str(e.value)


# --------------------------------------------------------------------- DB_ECHO


def test_生产环境开DB_ECHO拒绝启动():
    """**DB_ECHO 自己就该拦，而不是靠 LOG_LEVEL 顺便拦。**

    `test_生产环境开DEBUG拒绝启动` 看着像已经覆盖了这件事，其实没有：
    那条拦的是「LOG_LEVEL=DEBUG」，而 `echo` 是交给 SQLAlchemy engine 的，
    与 Python 的日志级别是**两套机制**。所以 `LOG_LEVEL=INFO + DB_ECHO=true`
    能从那条校验下面完整走过去。

    走下去之后会发生什么：正常启动、没有任何警告，然后安静地把带绑定参数的
    SQL —— 也就是全公司每一张凭证的金额 —— 一行行写进日志文件。
    这种失效没有任何症状可观察，所以必须在启动前挡住。
    """
    with pytest.raises(ValueError) as e:
        _check(_prod(db_echo=True))
    assert "DB_ECHO" in str(e.value)


def test_DB_ECHO不是靠DEBUG那条顺带拦住的():
    """把这条钉住，是因为「看着已经拦了」正是当初漏掉它的原因。

    上面的 `test_生产环境开DEBUG拒绝启动` 会让人以为 DB_ECHO 已经管住了。
    真正要守住的是：**即便日志级别是正常的 INFO**，开 DB_ECHO 也必须被拒。
    """
    s = _prod(db_echo=True, log_level="INFO")
    assert s.log_level == "INFO", "前提写错了：这条用的就是 INFO"
    with pytest.raises(ValueError):
        _check(s)


# ------------------------------------------------------- JWT 算法白名单（P2-11）


def test_生产环境JWT_ALGORITHM必须是HMAC家族():
    """密钥的长度、弱口令、默认值都查了，**唯独没查算法**。

    现在补上。`none` 是最坏的一种：它意味着「签名不参与验证」。
    当前 PyJWT 在 `algorithms=["none"]` 下会拒绝无签名 token，所以这还
    不是活着的洞；但它是「一次重构就变成真漏洞」的那类缺口 ——
    哪天有人改成按 token 自带的 alg 选算法，这里就是最后一道拦。
    """
    for bad in ("none", "None", "RS256", "ES256", "PS256", "hs256", ""):
        with pytest.raises(ValueError) as e:
            _check(_prod(jwt_algorithm=bad))
        assert "JWT_ALGORITHM" in str(e.value), f"报错里没点名 JWT_ALGORITHM：{bad}"


def test_HMAC算法白名单放行():
    for ok in ("HS256", "HS384", "HS512"):
        _check(_prod(jwt_algorithm=ok))      # 不抛异常即通过


def test_算法这条不靠dev环境顺带通过():
    """dev/test 会整体跳过生产校验 —— 所以「算法不合法但环境是 dev」
    仍然能构造出来，这不构成问题；这里要钉住的是**生产**下确实拦得住。"""
    s = _prod(jwt_algorithm="none")
    assert s.jwt_algorithm == "none", "前提写错了：这条用的确实是非法算法"
    with pytest.raises(ValueError):
        _check(s)


def test_dev环境允许DB_ECHO():
    """开发机要能看 SQL，否则「本地复现不了」和「库里明明是这样」会打架。"""
    validate_production_settings(_prod(db_echo=True), env="dev")


def test_DB_ECHO默认关闭且可配置():
    assert _prod().db_echo is False
    assert _prod(db_echo=True).db_echo is True


def test_DB_ECHO与DEBUG是两个独立问题():
    """两个都开时，错误里**两条**都要出现 —— 少说一条，运维就只改一条，
    改完再发一次还是起不来，然后开始怀疑这个校验是不是坏了。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(db_echo=True, log_level="DEBUG"))
    msg = str(e.value)
    assert "DB_ECHO" in msg
    assert "LOG_LEVEL" in msg



# --------------------------------------------------------------------- 审查 H-01
#
# 管理员邮箱 MFA 的验证链接基址。
#
# **这一条为什么进 problems 而不是 warning**：留空时宿主退回 ``request.base_url``
# —— 那是**客户端给的 Host 头**。于是攻击者拿到管理员口令后，带
# ``Host: evil.example`` 登录一次，系统会向管理员邮箱发出一封**域名正规、
# 系统自己发出**的邮件，但链接指向攻击者的域名；管理员点开 → 令牌落到对方
# 服务器 → 对方换到管理员访问令牌。**二次验证反而成了投递通道。**
#
# 这与 JWT 密钥同一档：一次配置缺失 = 一条完整的管理员接管链。


def test_MFA开着而链接基址为空时拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(admin_mfa_enabled=True, admin_mfa_link_base=""))
    assert "ADMIN_MFA_LINK_BASE" in str(e.value)


def test_MFA链接基址不是https时拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(admin_mfa_enabled=True, admin_mfa_link_base="http://acct.example.com"))
    assert "ADMIN_MFA_LINK_BASE" in str(e.value)


def test_MFA链接基址配对了就放行():
    _check(_prod(admin_mfa_enabled=True, admin_mfa_link_base="https://acct.example.com"))


def test_MFA没开时不要求配链接基址():
    """MFA 默认关着 —— 为一个没开的功能拒绝启动，是拿可用性换一个不存在的风险。"""
    _check(_prod(admin_mfa_enabled=False, admin_mfa_link_base=""))


def test_dev环境不要求MFA链接基址():
    validate_production_settings(
        _prod(admin_mfa_enabled=True, admin_mfa_link_base=""), env="dev")


# --------------------------------------------------------------------- 审查 L-03
#
# MCP 的 Host 白名单（DNS 重绑定防护）。
#
# **口径变了**：现在是「MCP 开着就必须配白名单」（审查 §4.3-2）。原来
# 「默认不拦」的理由是「``.env.example`` 里 ``MCP_ALLOWED_HOSTS=`` 本来
# 就是空的（而 ``MCP_ENABLED=true``），默认就拦的话照抄示例的部署会直接
# 起不来」—— 而 ``MCP_ENABLED`` 默认 true 意味着**照抄示例的部署正好落在
# 「MCP 开着 + 白名单空」这个形态里**，启动闸门对它是完全沉默的。
#
# 想要豁免的部署用 ``MCP_ALLOWED_HOSTS_WARN_ONLY=true``（本机直连、不对外
# 的部署确实不需要白名单）。逐项的断言在
# ``tests/test_audit_fix_prod_config_gates.py``。


def test_MCP强制模式下没配Host白名单就拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(mcp_enabled=True, mcp_enforce_allowed_hosts=True,
                     mcp_allowed_hosts=""))
    assert "MCP_ALLOWED_HOSTS" in str(e.value)


def test_MCP非强制模式下没配Host白名单现在也拒绝启动():
    """**口径变了**：``MCP_ENFORCE_ALLOWED_HOSTS`` 不再是开关本身。

    「MCP 开着 + 白名单空」是**默认形态**（``MCP_ENABLED`` 默认 true），
    所以它现在**一律**拒绝启动 —— 不再取决于那个默认 false 的开关。
    豁免口是 ``MCP_ALLOWED_HOSTS_WARN_ONLY=true``。
    """
    with pytest.raises(ValueError) as e:
        _check(_prod(mcp_enabled=True, mcp_enforce_allowed_hosts=False,
                     mcp_allowed_hosts=""))
    assert "MCP_ALLOWED_HOSTS" in str(e.value)


# --------------------------------------------------------------------- 审查 M-13
#
# ``.env.example`` 里的 ``APP_ENV``。
#
# **它原来写的是 ``APP_ENV=dev``** —— 于是「``cp .env.example .env`` 然后直接
# 上生产」这条最常见的路径，会把**全部**生产配置校验一起关掉：弱 JWT 密钥
# 能签发任意用户的令牌、``/docs`` 与 ``/openapi.json`` 对外暴露完整路由表、
# CORS 中间件被装上。三条防线同时失效，而进程不会给任何提示。
#
# 下面这条把「示例文件里那个键必须是空的」钉死。


def test_env_example里的APP_ENV必须留空():
    """留空 = 走 config.py 的默认 prod = fail-closed（审查 M-13）。

    这条用例守的是**文件内容**，不是运行时行为 —— 因为风险恰恰在于
    「照抄示例直接上线」这条路径：只要示例里写着 ``APP_ENV=dev``，
    一次 ``cp`` 就是一次自我免检。
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    text = example.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    assert values.get("APP_ENV", "") == "", (
        f".env.example 里的 APP_ENV={values.get('APP_ENV')!r} —— "
        "照抄示例上线会把全部生产配置校验一起关掉。留空（= 默认 prod）。")


# --------------------------------------------------------------------- 审查 L-08
#
# 数据库口令的长度下限。
#
# **长度必须按"真实口令"量，而不是按连接串里的字面量量。**
# 连接串里的口令是**百分号编码**的，而百分号编码能把一个字符撑成三个 ——
# 于是 ``%41%41%41%41%41%41``（真实口令 6 个字符）在字面上是 18 个字符，
# 能轻松越过长度下限。而百分号编码是**任何含特殊字符的口令都必须用**的形式，
# 不是刻意构造的边角情况。


def test_库口令长度按真实口令算而不是按编码后的字面量():
    from app.core.config import _db_password

    assert _db_password("postgresql+psycopg://cwq:%41%41%41%41%41%41@h:5432/d") == "AAAAAA"
    assert _db_password("postgresql+psycopg://cwq:Str%40ngP%40ss@h:5432/d") == "Str@ngP@ss"
    assert _db_password("postgresql+psycopg://cwq:cwq123@h:5432/d") == "cwq123"


def test_百分号编码撑长度的口令过不了长度下限():
    """6 个字符的真实口令，编码成 18 个字面量 —— 仍然必须被拦。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(database_url="postgresql+psycopg://cwq:%41%41%41%41%41%41@h:5432/d"))
    assert "数据库密码" in str(e.value)


def test_够长的库口令放行():
    _check(_prod(database_url="postgresql+psycopg://cwq:Str0ngP%40ssXYZwAB@h:5432/d"))
