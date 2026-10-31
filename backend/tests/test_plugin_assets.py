from app.plugins.manifest import plugin_digest
from app.plugins.security import asset_token
from tests.plugin_fixtures import plugin_env


def test_plugin_assets_need_a_ticket_and_are_not_manifest_or_business_api(plugin_env, client, auth, plain_auth, plain_user):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    digest = plugin_digest(manifest)
    base = f"/api/plugins/assets/{pid}/{digest}/frontend"

    # 匿名请求一律 401：资产不再是可以被任何人匿名触发的路径。
    assert client.get(base + "/index.js").status_code == 401
    assert client.get(base + "/_t/bogus/index.js").status_code == 401
    assert client.get(f"{base}/_t/{asset_token(pid, digest)}/index.js").status_code == 200

    token = asset_token(pid, digest)
    r = client.get(f"{base}/_t/{token}/index.js")
    assert r.status_code == 200, r.text
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers
    assert client.get(f"{base}/_t/{token}/backend/main.py").status_code == 404
    assert client.post(f"{base}/_t/{token}/index.js", headers=auth).status_code == 405
    assert client.get("/api/plugins/manifest").status_code == 401
    assert client.get(f"/api/plugins/{pid}/hello").status_code == 401

    # 别的包的票据不能用来读这个包。
    other = asset_token("com.other.plugin", digest)
    assert client.get(f"{base}/_t/{other}/index.js").status_code == 401

    # ------------------------------------------------------------------
    # **带查询串照样能取到资产** —— 本地开发装插件后插件页打不开的那道闸门。
    #
    # Vite 的 dev server 会把「URL 要到运行时才知道」的动态 import 改写成
    # `__vite__injectQuery(url, 'import')`，浏览器实际请求的是
    # `…/frontend/index.js?import`。从前这条路由把「有任何查询串」当非法
    # 请求直接 404，插件页于是永远停在「扩展暂不可用」。
    #
    # 查询串**不参与定位也不参与鉴权**（定位靠路径、鉴权靠路径里的票据），
    # 所以这里必须放行 —— 但**放行查询串不等于放行别的文件**：路径仍然
    # 只认签名清单里 frontend/ 底下那几份。
    # ------------------------------------------------------------------
    assert client.get(f"{base}/_t/{token}/index.js?import").status_code == 200
    # 查询串换不走要发的文件：带 ?file=backend/main.py 拿到的仍是 index.js。
    r = client.get(f"{base}/_t/{token}/index.js?file=backend/main.py")
    assert r.status_code == 200, r.text
    assert "Signed plugin" in r.text
    # 带查询串时，票据与路径的闸门一样不松。
    assert client.get(f"{base}/_t/bogus/index.js?import").status_code == 401
    assert client.get(f"{base}/_t/{other}/index.js?import").status_code == 401
    assert client.get(f"{base}/_t/{token}/backend/main.py?import").status_code == 404
    # 票据段缺失照样 401（没有票据段：匿名请求一律拒绝）。
    assert client.get(f"{base}/index.js?import").status_code == 401

    # 已授权用户从 manifest 拿到的 URL 自带票据，可以直接取到资产。
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    payload = client.get("/api/plugins/manifest", headers=plain_auth).json()
    entry = payload["plugins"][0]["entry"]
    assert entry.startswith(f"/api/plugins/assets/{pid}/{digest}/frontend/")
    assert client.get(entry).status_code == 200
    # 开发服务器给它加个 ?import 也不该坏（见上面的注释）。
    assert client.get(entry + "?import").status_code == 200
