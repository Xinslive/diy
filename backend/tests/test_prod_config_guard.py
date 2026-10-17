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
GOOD_DB = "postgresql+psycopg://cwq:Str0ngP%40ss@db.internal:5432/cwq"


def _prod(**kw):
    base = dict(jwt_secret=GOOD_SECRET, database_url=GOOD_DB)
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

