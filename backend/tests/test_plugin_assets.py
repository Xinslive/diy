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
    assert client.get(f"{base}/_t/{token}/index.js?token=bad").status_code == 404
    assert client.post(f"{base}/_t/{token}/index.js", headers=auth).status_code == 405
    assert client.get("/api/plugins/manifest").status_code == 401
    assert client.get(f"/api/plugins/{pid}/hello").status_code == 401

    # 别的包的票据不能用来读这个包。
    other = asset_token("com.other.plugin", digest)
    assert client.get(f"{base}/_t/{other}/index.js").status_code == 401

    # 已授权用户从 manifest 拿到的 URL 自带票据，可以直接取到资产。
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    payload = client.get("/api/plugins/manifest", headers=plain_auth).json()
    entry = payload["plugins"][0]["entry"]
    assert entry.startswith(f"/api/plugins/assets/{pid}/{digest}/frontend/")
    assert client.get(entry).status_code == 200
