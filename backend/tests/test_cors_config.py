"""CORS 的装配口径（动静分离用）。

两条线：

* **解析**（``cors_origin_list``）：白名单怎么写、怎么被归一化；
* **装配**（``cors_middleware_options``）：什么情况下装中间件、装成什么样。

**为什么单独测这些**：跨源部署一旦配错，症状是「全站请求都失败、控制台一条
CORS 错误」，而服务端日志里**一个请求都没有**（预检被浏览器拦在门外）。
排查方向天然会跑偏到后端逻辑上。而这里每一处配置都对应一个具体症状，
值得钉住。

不测「真的发一个跨源请求」：那要起完整应用 + 造 Origin 头 + 走预检，
成本高而覆盖面不比现在大。
"""
from __future__ import annotations

import re

from app.core.config import Settings, cors_origin_list
from app.core.cors import cors_middleware_options

GOOD_SECRET = "a" * 48
GOOD_DB = "postgresql+psycopg://cwq:Str0ngP%40ssXYZwAB@db.internal:5432/cwq"


def _settings(**kw):
    base = dict(jwt_secret=GOOD_SECRET, database_url=GOOD_DB)
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------- 解析


def test_空白名单解析成空():
    """空 = 一个都不给（调用方据此关闭 CORS）。"""
    assert cors_origin_list("") == ([], [])
    assert cors_origin_list("   ") == ([], [])


def test_精确_origin进精确列表():
    origins, patterns = cors_origin_list("https://cdn.example.com")
    assert origins == ["https://cdn.example.com"]
    assert patterns == []


def test_结尾斜杠被抹掉():
    """浏览器的 Origin 头**没有结尾斜杠**。

    带着斜杠写就永远匹配不上，而且**不报错** —— 症状是「配了白名单还是
    跨源失败」，排查时极容易跑去查后端路径、nginx 配置。
    """
    origins, _ = cors_origin_list("https://cdn.example.com/")
    assert origins == ["https://cdn.example.com"]


def test_逗号分隔且忽略空白():
    origins, _ = cors_origin_list(" https://a.example.com , https://b.example.com ,")
    assert origins == ["https://a.example.com", "https://b.example.com"]


def test_通配子域转成正则():
    _, patterns = cors_origin_list("https://*.example.com")
    assert len(patterns) == 1
    assert re.fullmatch(patterns[0], "https://cdn.example.com")
    assert re.fullmatch(patterns[0], "https://a.b.example.com")
    # **不跨 /**：不能把路径也匹配进去
    assert not re.fullmatch(patterns[0], "https://cdn.example.com/some/path")


def test_不合法的条目被丢掉而不是静默放过():
    """写错的 origin 条目等于没配 —— 白名单因此短一截，却看不出来。"""
    origins, patterns = cors_origin_list("https://ok.example.com, not-an-origin, ftp://x.example.com")
    assert origins == ["https://ok.example.com"]
    assert patterns == []


def test_星号单独处理():
    origins, patterns = cors_origin_list("*")
    assert origins == ["*"]
    assert patterns == []


# --------------------------------------------------------------------- 装配


def test_生产默认不装中间件():
    """**同源部署完全不受影响** —— 这是默认形态。"""
    assert cors_middleware_options(_settings(), env="prod") is None


def test_开发环境装且允许本机端口():
    opts = cors_middleware_options(_settings(), env="dev")
    assert opts is not None
    assert opts["allow_origin_regex"]
    assert opts["allow_credentials"] is False


def test_生产配了白名单才装():
    opts = cors_middleware_options(_settings(cors_allow_origins="https://cdn.example.com"), env="prod")
    assert opts is not None
    assert opts["allow_origins"] == ["https://cdn.example.com"]
    assert opts["allow_credentials"] is False


def test_放行_Authorization_头():
    """预检必须放行 Authorization。

    不放行的症状特别误导人：**所有接口都不通，而服务端日志里一个请求都没有**
    （预检被浏览器拦掉了，根本没到应用）。
    """
    opts = cors_middleware_options(_settings(cors_allow_origins="https://cdn.example.com"), env="prod")
    assert "Authorization" in opts["allow_headers"]
    assert "Content-Type" in opts["allow_headers"]


def test_暴露_Content_Disposition():
    """跨域时导出文件名会丢。

    前端 downloadFile() 靠 content-disposition 取文件名，而跨源时浏览器
    只让 JS 看到「被暴露」的响应头 —— 不暴露的话后端给的好名字被丢掉，
    所有导出都叫「导出.xlsx」，而且**不报错**，只是名字不对。
    """
    opts = cors_middleware_options(_settings(cors_allow_origins="https://cdn.example.com"), env="prod")
    assert "Content-Disposition" in opts["expose_headers"]


def test_预检结果有缓存期():
    """前端每个请求都带 Authorization 头 → 每个请求前都跟一次预检。

    不缓存的话跨源部署下 API 请求数翻倍；小带宽服务器的出向流量按字节算，
    那一跳是纯浪费。
    """
    opts = cors_middleware_options(_settings(cors_allow_origins="https://cdn.example.com"), env="prod")
    assert opts["max_age"] >= 300


def test_只有通配规则时也装():
    opts = cors_middleware_options(_settings(cors_allow_origins="https://*.example.com"), env="prod")
    assert opts is not None
    assert opts["allow_origins"] == []
    assert opts["allow_origin_regex"]
