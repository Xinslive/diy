"""CORS 中间件的装配口径（动静分离用）。

单独一个模块的原因：**这一段必须能脱离 FastAPI 启动单独测**。
写在 ``app/main.py`` 的模块级代码里时，测试只能靠 reload 整个模块来触发
不同配置 —— 而 reload 会换掉 ``app.main.app`` 这个对象，别的用例拿到的
引用就指向另一个应用了（而且 reload 一遍要建一次 MCP 服务、装一次插件
运行时）。把「算参数」抽成纯函数之后，测试直接调它，生产也只是调它。

判口径与全项目一致：``detect_env``（见 app/core/config.py）。
"""
from __future__ import annotations

from typing import Any

from app.core.config import cors_origin_list, detect_env


def cors_middleware_options(settings: Any, env: str | None = None) -> dict[str, Any] | None:
    """算出 CORS 中间件的参数；**返回 None 表示「不装这个中间件」**。

    ------------------------------------------------------------------
    开发/测试环境
    ------------------------------------------------------------------
    装，且允许本机任意端口（``allow_origin_regex``）。

    原来这段是**无条件**加的，且带着一条通配正则
    ``http://(localhost|127\\.0\\.0\\.1):\\d+`` + ``allow_credentials=True``：
    生产部署下，任何跑在本机 localhost 任意端口上的页面都能带着凭据
    跨源读本系统的任意响应。令牌存在 localStorage 而非 cookie，
    所以恶意源拿不到令牌、拿不到实质数据 —— 危害有限，但白名单
    「任何本机端口」这件事本身就不该出现在生产。

    **不带凭据**：本项目**全仓没有任何 Cookie 依赖**（没有 set_cookie、
    没有 ``request.cookies``）—— 令牌走 ``Authorization`` 头 + 前端
    localStorage，**不带凭据照样工作**。而浏览器对「带凭据的跨源请求」
    有额外的限制（预检更严、不能通配），去掉它对开发没有影响，
    对「本机任意端口」这个过宽的口径是实打实的收紧。

    ------------------------------------------------------------------
    生产：按配置开
    ------------------------------------------------------------------
    **默认空 = 不装。** 动静分离之后前端可能不在 API 的源站上（静态资源
    挂 CDN、页面也在 CDN、API 留在源站），这时浏览器发出的请求是**跨源**
    的，不开 CORS 的话全站请求都会失败（控制台报 CORS 错误，不是某个
    接口坏）。

    但「默认开」是更糟的选择：那等于把一个**平时用不到**的中间件挂到每个
    生产实例上，白白多一轮预检往返，还把「谁能跨源读我」这件事的默认值从
    「没有人」变成「看配置写得对不对」。所以：**默认空 = 不开**（同源部署
    完全不受影响），只有真的把前端放到别的源上时才显式配
    ``CORS_ALLOW_ORIGINS``。
    """
    actual = detect_env(settings, env)
    if actual in ("dev", "test"):
        return {
            "allow_origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
            "allow_credentials": False,
            "allow_origin_regex": r"http://(localhost|127\.0\.0\.1):\d+",
            "allow_methods": ["*"],
            "allow_headers": ["*"],
        }

    origins, patterns = cors_origin_list(settings.cors_allow_origins)
    if not origins and not patterns:
        return None

    return {
        "allow_origins": origins,
        # Starlette 的 allow_origin_regex 是**单个**正则（用 fullmatch 匹配），
        # 所以多条通配规则要合成一条 —— 用 | 连接。
        "allow_origin_regex": ("|".join(patterns) if patterns else None),
        # 不带凭据：本项目没有任何 Cookie 依赖，令牌走 Authorization 头。
        # 带凭据还会让浏览器对预检更严（不允许通配），对这里没有好处。
        "allow_credentials": False,
        "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        # Authorization 是**必须**列出来的：浏览器的跨源预检只把
        # 「CORS 非简单请求头」放进 Access-Control-Request-Headers，
        # 服务端没在 Allow-Headers 里放行它，预检就失败 —— 表现是
        # 「所有接口 401/无响应」，但服务端日志里**一个请求都没有**
        # （预检被浏览器直接拦掉了，根本没到应用）。
        "allow_headers": ["Authorization", "Content-Type", "Accept", "X-Requested-With"],
        # **Content-Disposition 必须暴露**：前端的 downloadFile() 靠它取
        # 导出文件名（res.headers['content-disposition']）。跨源时浏览器
        # 只让 JS 看到「被暴露」的那几个响应头 —— 不暴露的话这个头读不到，
        # 表现是「所有导出都叫 导出.xlsx」（后端给的好名字被丢掉），
        # 而且不报错，只是名字不对，很难联想到是 CORS 的问题。
        "expose_headers": ["Content-Disposition"],
        # 预检结果缓存 10 分钟。前端每次请求都带 Authorization 头，
        # 也就是说**每个请求之前都跟着一次预检**（OPTIONS）—— 不缓存的话
        # 跨源部署下 API 请求数直接翻倍，而小带宽服务器的出向流量是按
        # 字节算的，那一跳是纯浪费。
        "max_age": 600,
    }
