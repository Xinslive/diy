"""全部「逃生口」环境变量名 —— 系统里**唯一**的一份清单。

「逃生口」= 那些**默认阻断、但可以显式放行**的开关：
它们的默认行为是「不合格就拒绝启动 / 拒绝部署」，
打开之后变成「只告警、继续跑」。

为什么单独一个文件、为什么必须是**一份**
--------------------------------------------
这份清单同时被三处用到：

1. **应用侧**：:mod:`app.core.config` 读进 Settings 的那三个 ——
   启动闸门会查它们**在持久化配置（``.env``）里是不是开着**；
2. **部署侧**：``deploy/deploy.sh`` 自己读的那几个 ——
   它们只对**单次运行**生效（从进程环境读，不落盘）；
3. **CI**：断言随产品分发的生产配置模板里**没有**任何逃生口被置真，
   以及「``deploy.sh`` 里出现的每个 ``CWQ_*`` 名字都登记过」。

三处各写一份必然分叉 —— 漏一个 = 生产姿态被悄悄削弱，
而且**没有任何地方会报错**：漏掉的那个既不告警也不阻断，
它只是安静地让一道闸失效。

所以这里只放**数据**，不放逻辑：任何一处要回答「这是不是一个逃生口」，
都从这里取。``app/core/config.py`` 在导入时会断言
「清单与 Settings 字段一一对应」，多一个少一个都直接起不来。
"""
from __future__ import annotations

#: 应用侧的逃生口：读进 ``Settings``，默认 ``False``（= 阻断）。
#:
#: 置 ``true`` 的效果是「把一道 fail-closed 的闸降级成只告警」：
#:
#:   * ``TRUSTED_PROXIES_WARN_ONLY``      —— TRUSTED_PROXIES 为空时不再拒绝启动；
#:   * ``MCP_ALLOWED_HOSTS_WARN_ONLY``    —— MCP Host 白名单为空时不再拒绝启动；
#:   * ``BACKUP_ENCRYPTION_KEY_WARN_ONLY``—— 备份加密密钥为空时不再拒绝启动。
APP_SIDE_ESCAPE_HATCHES = (
    "TRUSTED_PROXIES_WARN_ONLY",
    "MCP_ALLOWED_HOSTS_WARN_ONLY",
    "BACKUP_ENCRYPTION_KEY_WARN_ONLY",
)

#: 每个应用侧逃生口对应的「**正式配法**」—— 报错时直接告诉运维该怎么改。
#:
#: 报错信息里只说「不许开」而不说「那该怎么配」，等于把判断又丢回给运维；
#: 而这几个的正式配法都是明确的（配好就不用豁免了）。
APP_SIDE_ESCAPE_HATCH_FIXES = {
    "TRUSTED_PROXIES_WARN_ONLY":
        "把 TRUSTED_PROXIES 配成反向代理地址（如 127.0.0.1）",
    "MCP_ALLOWED_HOSTS_WARN_ONLY":
        "把 MCP_ALLOWED_HOSTS 配成宿主名清单（DNS 重绑定防护）",
    "BACKUP_ENCRYPTION_KEY_WARN_ONLY":
        "把 BACKUP_ENCRYPTION_KEY 配成备份加密密钥（OSS 上的备份制品就是密文）",
}

#: 部署侧的逃生口：``deploy.sh`` 自己读，默认关（= 阻断）。
#:
#: 打开之后的效果：
#:
#:   * ``CWQ_ALLOW_ANY_HOST``         —— 跳过「本机是不是那台被授权的生产机」整道守卫；
#:   * ``CWQ_HOST_IP``                —— **注入**守卫用来判本机身份的 IP（见下）；
#:   * ``CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES`` —— TRUSTED_PROXIES 为空时不再阻断部署；
#:   * ``CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR``    —— 应用根不是 /opt/cwq 时不再阻断；
#:   * ``CWQ_DEPLOY_SKIP_FRONTEND_CHECK``           —— 跳过前端可达性检查。
#:
#: **为什么 ``CWQ_HOST_IP`` 也在名单里**（它长得像个参数，其实是逃生口）：
#: 目标机守卫判「本机是不是那台被授权的机器」，靠的是**本机出口 IP**。
#: 而这个变量**直接替换掉那个判据** —— 注成被授权的 IP，标记文件那道与
#: 内置名单那道就都过了。于是「以为在 A 机、实际在 B 机」这个场景
#: （两个库各自推进、调度任务被领两遍）又被放行了一次。
#: 它存在的理由是沙箱里 curl 是桩、取不到真 IP，测试要验「识别成某台机时
#: 的行为」才需要能注入。**正因为它能绕过生产守卫，deploy.sh 在用它的时候会
#: 打一条告警**：豁免本来就是设计成安静放行的，而这一条偏偏安静不得。
DEPLOY_SIDE_ESCAPE_HATCHES = (
    "CWQ_ALLOW_ANY_HOST",
    "CWQ_HOST_IP",
    "CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES",
    "CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR",
    "CWQ_DEPLOY_SKIP_FRONTEND_CHECK",
)

#: **不是**逃生口的一档：部署侧还有这三个 ``CWQ_*``。
#:
#: 之所以要把它们**显式登记**（而不是「不在清单里就算了」），是因为
#: CI 有一条断言：「deploy.sh 里出现的每个 ``CWQ_*`` 名字都必须在这里
#: 登记过」。没有这条断言的话，将来新增一个部署侧开关时很容易忘了登记 ——
#: 而漏登记的开关不会被任何检查覆盖，它就成了一道**没人看得见的、永远开着
#: 的口子**。
#:
#:   * ``CWQ_DEPLOY_REQUIRE_BACKUP_ARTIFACT=1`` —— 它只把「备份制品必须存在」
#:     这一档**升级**成阻断，是**收紧**不是放宽；
#:   * ``CWQ_CDN_UPLOAD=1`` —— 打开「发布时把静态资源上传到 CDN」这个
#:     **附加动作**，不放松任何一道闸（缺脚本时它反而会拒绝）；
#:   * ``CWQ_DEPLOY_TARGET_FILE`` —— 发布目标文件的位置，是**参数**不是开关。
DEPLOY_SIDE_NON_ESCAPE_SWITCHES = (
    "CWQ_DEPLOY_REQUIRE_BACKUP_ARTIFACT",
    "CWQ_CDN_UPLOAD",
    "CWQ_DEPLOY_TARGET_FILE",
)

#: 部署侧的**参数**：它们只提供一个值，不改变任何一道闸的开合。
#:
#: 登记它们不是为了说「它们有风险」，而是为了让「deploy.sh 里出现的每个
#: ``CWQ_*`` 名字都登记过」这条断言是**完备的** —— 断言只有在覆盖全部名字时
#: 才拦得住「新增一个开关忘了登记」。
#:
#:   * ``CWQ_API_BASE`` / ``CWQ_ASSET_BASE`` / ``CWQ_ORIGIN`` —— 前端装配用的
#:     基址与源站地址；
#:   * ``CWQ_FRONTEND_HEALTH_HOST`` / ``_SCHEME`` / ``_PORT`` —— 前端可达性检查
#:     去哪里探；
#:   * ``CWQ_RETIRED_HOSTS`` —— 已下线机器的名单。**目前是脚本内写死的常量**
#:     （不是环境变量），登记在这里是为了让名字清单没有例外。
DEPLOY_SIDE_PARAMETERS = (
    "CWQ_API_BASE",
    "CWQ_ASSET_BASE",
    "CWQ_ORIGIN",
    "CWQ_FRONTEND_HEALTH_HOST",
    "CWQ_FRONTEND_HEALTH_PORT",
    "CWQ_FRONTEND_HEALTH_SCHEME",
    "CWQ_RETIRED_HOSTS",
)

#: 全部逃生口（两份合起来）。
#:
#: **应用侧**的三个由后端的启动闸门把关：它们写在**持久化配置**（``.env``）里
#: 时**拒绝启动** —— 逃生口只允许对**单次运行**生效（进程环境给一次），
#: 写进配置文件它就从「临时放宽」变成了「这台机器的常态」，而豁免本来
#: 就是设计成安静放行的，没有任何人会再想起它。
ESCAPE_HATCH_ENV_VARS = APP_SIDE_ESCAPE_HATCHES + DEPLOY_SIDE_ESCAPE_HATCHES

#: 部署侧**全部**开关名（逃生口 + 非逃生口），供 CI 断言「没有漏登记的」。
DEPLOY_SIDE_SWITCHES = DEPLOY_SIDE_ESCAPE_HATCHES + DEPLOY_SIDE_NON_ESCAPE_SWITCHES

#: 部署侧**全部** ``CWQ_*`` 名字（开关 + 参数）。
#:
#: CI 的断言扫的是「deploy.sh 里出现的每个 ``CWQ_*`` 名字」，所以这份清单
#: 必须**覆盖每一个**，否则断言就漏了它 —— 而漏登记的逃生口正是「没人看得见
#: 的、永远开着的口子」。
DEPLOY_SIDE_ALL_NAMES = DEPLOY_SIDE_SWITCHES + DEPLOY_SIDE_PARAMETERS
