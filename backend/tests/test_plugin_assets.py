from tests.plugin_fixtures import plugin_env


def test_immutable_assets_are_public_but_not_manifest_or_business_api(plugin_env, client, auth):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    from app.plugins.manifest import plugin_digest
    base = f"/api/plugins/assets/{pid}/{plugin_digest(manifest)}/"
    r = client.get(base + "frontend/index.js")
    assert r.status_code == 200, r.text
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers
    assert client.get(base + "backend/main.py").status_code == 404
    assert client.get(base + "frontend/index.js?token=bad").status_code == 404
    assert client.post(base + "frontend/index.js", headers=auth).status_code == 405
    assert client.get("/api/plugins/manifest").status_code == 401
    assert client.get(f"/api/plugins/{pid}/hello").status_code == 401
