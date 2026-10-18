from tests.plugin_fixtures import plugin_env


def test_live_grant_revoke_and_admin_is_not_implicit_user(plugin_env, client, auth, plain_auth, plain_user, admin_user):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install", files={"file": ("x.cwp", raw)}, headers=auth).status_code == 200
    restart()
    url = f"/api/plugins/{pid}/hello"
    for headers in (plain_auth, auth):
        r = client.get(url, headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "PLUGIN_NOT_FOUND_OR_UNAVAILABLE"
        assert pid not in r.text
    for _ in range(2):
        assert client.post(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    r = client.get("/api/plugins/manifest", headers=plain_auth)
    assert r.headers["cache-control"] == "private, no-store"
    assert [p["id"] for p in r.json()["plugins"]] == [pid]
    assert client.get(url, headers=plain_auth).json() == {"hello": "plugin"}
    assert client.get(url, headers=auth).status_code == 404
    assert client.delete(f"/api/system/plugins/{pid}/users/{plain_user.id}", headers=auth).status_code == 200
    assert client.get(url, headers=plain_auth).status_code == 404
    assert client.get("/api/plugins/manifest", headers=plain_auth).json() == {"plugins": []}
    assert client.put(f"/api/system/plugins/{pid}/users", headers=auth, json={"user_ids": [-1]}).status_code == 400
    assert client.put(f"/api/system/plugins/{pid}/users", headers=plain_auth, json={"user_ids": [plain_user.id]}).status_code == 403
    assert client.put(f"/api/system/plugins/{pid}/users", headers=auth, json={"user_ids": [admin_user.id]}).status_code == 200
    assert client.get(url, headers=auth).status_code == 200
    assert client.post(f"/api/system/plugins/{pid}/disable", headers=auth).json()["status"] == "pending_restart"
    assert client.get(url, headers=auth).status_code == 503
    restart()
    assert client.get("/api/system/plugins", headers=auth).json()["plugins"][0]["runtime_status"] == "DISABLED"
