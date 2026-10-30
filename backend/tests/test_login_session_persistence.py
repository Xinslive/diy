"""登录状态：**不设时间上限**，以及「登出」必须是真终点。

**这个文件钉的是两件事。**

1. **登录令牌没有到期时间。** 产品决定：用户要的是「登录之后别老是被踢下线」，
   而不是「把 12 小时改成 30 天」—— 后者只是把同一个问题推迟一个月。
   所以令牌的载荷里**压根没有 ``exp``**，它的终点只有三个：登出、
   改口令/停用（账号级）、密钥轮换。

2. **登出会真的吊销那一枚令牌。** 这是第 1 件事的**必要配套**：
   令牌一旦没有到期时间，「登出」就不能只是客户端把手里的令牌丢掉 ——
   那枚令牌仍然有效，而有效期是**永远**。没有这一步，
   「不要限制登录状态保持时间」就变成了「登录状态永远保持，连退出都保留」。

**为什么「单枚」而不是「这个账号的令牌全作废」。** 账号级撤销是
``sys_user.token_version``（``tv``），但它会把这个人**所有设备**的会话
一起踢掉 —— 那是「退出全部设备」的语义，不该由「点一下退出登录」触发。
所以吊销的粒度是 ``jti``（一枚令牌一个标识）。

**为什么必须有反向测试。** 「登出即失效」这条用例有可能因为别的什么原因
变绿（比如账号被停用了、令牌版本被翻了），于是它看起来在保护什么，
实际上保护的是别的东西。所以最后一条用例**把吊销判定摘掉**，
断言同一个被吊销的令牌会立刻恢复可用 —— 只有这样，上面那条用例
钉的才是「吊销判定」本身。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select as sa_select

from app.core import deps as deps_module
from app.core.config import settings
from app.core.security import create_access_token
from app.models import SysTokenRevocation


def _login(client, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败：{r.text}"
    return r.json()["access_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------- 一、令牌没有到期时间


def test_登录回执里没有到期时刻(client, plain_user):
    """``expires_at`` 为 ``None`` —— **不是 0**：0 是一个谎话（1970 年到期）。"""
    r = client.post("/api/auth/login",
                    json={"username": plain_user.username, "password": "staff123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_at"] is None, f"默认不该有到期时刻，实际 {body['expires_at']}"


def test_令牌载荷里没有exp(client, plain_user):
    """**不是「exp 设成一个遥远的将来」** —— 那种做法只是把「被踢下线」
    推迟几年，过期机制还在，将来还得再改一次。真正的不限时是**没有到期时间**。"""
    token = _login(client, plain_user.username, "staff123")
    payload = jwt.decode(token, settings.jwt_secret,
                          algorithms=[settings.jwt_algorithm], issuer=settings.jwt_issuer)
    assert "exp" not in payload, f"载荷里不该有 exp，实际 {payload.get('exp')}"
    assert payload.get("iat"), "iat 必须留着 —— 没有 exp 时只有它能回答「什么时候签的」"


def test_很久以前签发的令牌今天仍然能用(client, db, plain_user):
    """**这条是「不要限制登录状态保持时间」的字面意思。**

    造一枚「三个月前签发、且没有到期时间」的令牌（iat 在过去 90 天），
    今天拿来访问 —— 必须照样能用。

    这条不能靠「等三个月」来验：只能把签发时刻写进过去。
    """
    issued_at = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())
    payload = {"sub": str(plain_user.id), "username": plain_user.username,
               "iat": issued_at, "jti": "a" * 32, "iss": settings.jwt_issuer,
               "tv": int(plain_user.token_version or 0)}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 200, f"90 天前签的令牌不该失效：{r.status_code} {r.text}"


def test_限时那条路仍然可配(monkeypatch):
    """**把时限收回来只需要改一个配置**，不必改代码。"""
    from app.core import security

    monkeypatch.setattr(security.settings, "access_token_minutes", 30, raising=False)
    monkeypatch.setattr(security.settings, "admin_token_minutes", 0, raising=False)
    token, expires = create_access_token(1, "someone", is_admin=False)
    payload = jwt.decode(token, settings.jwt_secret,
                          algorithms=[settings.jwt_algorithm], issuer=settings.jwt_issuer)
    assert "exp" in payload
    assert expires is not None


# ------------------------------------------------------- 二、登出是真终点


def test_登出之后这枚令牌立刻失效(client, plain_user):
    """**端到端**：登出 → 同一个令牌再来 → 401。

    401 的措辞也钉住：必须是**吊销**那一支的措辞
    （「该会话已登出」），不能是「账号已停用」或「令牌版本对不上」——
    那两支会让这条用例在真正出问题时仍然变绿。
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 200

    r = client.post("/api/auth/logout", headers=_hdr(token))
    assert r.status_code == 200, r.text
    assert r.json().get("revoked") is True, "登出应当把这枚令牌标为已吊销"

    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 401, f"登出之后这枚令牌还能用：{r.status_code}"
    assert "已登出" in r.json()["detail"], f"401 的措辞不对：{r.json()['detail']}"


def test_登出只吊销这一枚_另一台设备照常能用(client, plain_user):
    """**单枚**吊销：同一个账号在两台设备上登录，登出其中一台，
    另一台**必须还能用**。

    这条是「吊销粒度」的全部意义：账号级撤销（``token_version``）
    会把两台一起踢掉，那不该由「点一下退出」触发。
    """
    token_a = _login(client, plain_user.username, "staff123")
    token_b = _login(client, plain_user.username, "staff123")
    assert token_a != token_b, "两次登录应当拿到两枚不同的令牌"

    assert client.post("/api/auth/logout", headers=_hdr(token_a)).status_code == 200

    r = client.get("/api/auth/me", headers=_hdr(token_a))
    assert r.status_code == 401, "被登出的那一枚不该还能用"
    r = client.get("/api/auth/me", headers=_hdr(token_b))
    assert r.status_code == 200, f"另一台设备被连坐了：{r.status_code} {r.text}"


def test_吊销对下一次请求立刻生效(client, db, plain_user):
    """**不做跨请求缓存** —— 吊销必须对下一次请求就生效。

    生产是**两个 uvicorn worker**（``deploy/systemd/cwq-api.service``）：
    只在进程内记一份吊销名单，A 机器登出、B 机器上的同一枚令牌就还能用。
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 200

    # 库里确实有这一行（换一条连接查 —— 证明它落库了，不是只在内存里）。
    jti = jwt.decode(token, settings.jwt_secret,
                     algorithms=[settings.jwt_algorithm], issuer=settings.jwt_issuer)["jti"]
    rows = db.scalars(sa_select(SysTokenRevocation).where(
        SysTokenRevocation.jti == jti)).all()
    assert len(rows) == 1, f"吊销记录应当落库且只有一行，实际 {len(rows)} 行"
    assert rows[0].user_id == plain_user.id
    assert rows[0].reason == "LOGOUT"

    # 账号本身没被动过 —— 所以下面那个 401 只可能来自吊销判定。
    db.refresh(plain_user)
    assert plain_user.active is True
    assert int(plain_user.token_version or 0) == 0, "登出不该动账号级撤销旋钮"

    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 401


def test_重复登出是幂等的(client, plain_user):
    """**登出天然该是幂等的。** 第二次不该报 401，更不该 500。

    （第二个登出请求带的令牌已经被吊销了 —— 所以这个接口不能挂
    ``get_current_user``，否则第二次会撞 401。）
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 200
    r = client.post("/api/auth/logout", headers=_hdr(token))
    assert r.status_code == 200, f"重复登出不该报错：{r.status_code} {r.text}"
    assert r.json().get("ok") is True


def test_登出留痕仍在(client, db, admin_user, plain_user):
    """**审计不能因为「现在会真吊销」而丢。**
    「最后一个离开的人是谁」往往就是出事之后的第一条线索。

    审计本身是**留痕**，不是撤销 —— 所以审计行仍然写着，哪怕那一枚令牌
    此刻已经不能用了（用它查审计必然 401，所以用管理员的令牌查）。
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 200

    admin_token = _login(client, admin_user.username, "admin123")
    r = client.get("/api/system/audit-logs", headers=_hdr(admin_token))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list), f"审计接口应当回一个列表，实际 {type(rows)}"
    actions = [row.get("action") for row in rows]
    assert "LOGOUT" in actions, f"审计里应当有一条 LOGOUT，实际 {actions[:10]}"
    logout_rows = [row for row in rows if row.get("action") == "LOGOUT"]
    # 审计表的 entity_id 是**字符串**列（它要能记「不存在的用户」这种名字）。
    assert str(logout_rows[0].get("entity_id")) == str(plain_user.id), \
        f"LOGOUT 审计应当记着是谁登出的：{logout_rows[0]}"
    assert logout_rows[0].get("after", {}).get("token_revoked") is True, \
        f"审计里应当记着「这枚令牌已作废」：{logout_rows[0]}"


def test_登出之后其它入口照样进不去(client, db, plain_user, book):
    """吊销判定在**鉴权入口**上，不是在某一个接口上 ——
    所以每一个要令牌的地方都该挡住。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    token = _login(client, plain_user.username, "staff123")
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 200

    for path in ("/api/auth/me", f"/api/books/{book.id}/subjects"):
        r = client.get(path, headers=_hdr(token))
        assert r.status_code == 401, f"{path} 竟然放行了已登出的令牌：{r.status_code}"


# ------------------------------------------------------- 三、账号级撤销仍然有效


def test_改密之后旧令牌仍然立刻作废(client, db, admin_user, plain_user):
    """**没有 exp 之后，账号级撤销这条更关键了** —— 它是「口令泄露 →
    重置口令」这条最常用处置真正生效的地方。

    （这条与 ``tests/test_token_revocation.py`` 里的那条同源；
    这里补的是「令牌永不过期**之后**它依然有效」这一层。）
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 200

    admin_token = _login(client, admin_user.username, "admin123")
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=_hdr(admin_token),
                     json={"password": "newpass456"})
    assert r.status_code == 200, r.text

    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 401, f"改完口令旧令牌还能用：{r.status_code}"


def test_停用账号之后令牌立刻作废(client, db, admin_user, plain_user):
    """停用是账号级的另一条路：它查 ``active``，与令牌有没有 exp 无关。"""
    token = _login(client, plain_user.username, "staff123")
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 200

    admin_token = _login(client, admin_user.username, "admin123")
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=_hdr(admin_token),
                     json={"active": False})
    assert r.status_code == 200, r.text

    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 401, f"停用之后旧令牌还能用：{r.status_code}"


# ------------------------------------------------------- 四、反向测试（证明上面那条有牙）


def test_把吊销判定摘掉之后被吊销的令牌就恢复可用(monkeypatch, client, plain_user):
    """**反向测试**：证明「登出即失效」钉的确实是**吊销判定本身**，
    而不是别的什么（账号被停用、令牌版本被翻了、用户查不到…）。

    做法：把 ``app.core.deps`` 里的吊销查询换成一个**永远查不到**的替身，
    同一个被吊销的令牌必须立刻恢复可用。

    **如果上面那条用例是因为别的原因变绿的，这一条就会红** ——
    这正是它存在的意义。
    """
    token = _login(client, plain_user.username, "staff123")
    assert client.post("/api/auth/logout", headers=_hdr(token)).status_code == 200
    assert client.get("/api/auth/me", headers=_hdr(token)).status_code == 401

    # 摘掉判定：deps 里的查询换成一个匹配不到任何行的版本。
    monkeypatch.setattr(
        deps_module, "select",
        lambda *a, **k: sa_select(SysTokenRevocation.id).where(
            SysTokenRevocation.jti == "__never_matches__"))

    r = client.get("/api/auth/me", headers=_hdr(token))
    assert r.status_code == 200, (
        "摘掉吊销判定之后被吊销的令牌仍然 401 —— 那说明拒绝它的不是吊销判定，"
        "上面那条用例钉错了东西")
