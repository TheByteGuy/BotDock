from __future__ import annotations

from conftest import setup_body, PASSWORD


def test_health_and_state_public(client):
    r = client.get("/api/health")
    assert r.json() == {"status": "ok", "version": "0.1.0"}
    assert client.get("/api/auth/state").json() == {"setup_required": True, "authenticated": False,
                                                    "setup_code_required": True}


def test_protected_routes_need_session(client):
    for method, path in [("get", "/api/bots"), ("get", "/api/system"), ("get", "/api/events"),
                         ("post", "/api/auth/logout")]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, (path, r.text)
        assert r.json() == {"detail": "Not authenticated"}


def test_setup_login_logout_flow(client):
    r = client.post("/api/auth/setup", json=setup_body(client, "short"))
    assert r.status_code == 400 and "8" in r.json()["detail"]
    r = client.post("/api/auth/setup", json=setup_body(client, PASSWORD))
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookie = r.headers["set-cookie"]
    assert "botdock_session=" in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "Path=/" in cookie
    assert client.get("/api/auth/state").json() == {"setup_required": False, "authenticated": True,
                                                    "setup_code_required": False}
    assert client.post("/api/auth/setup", json=setup_body(client, PASSWORD)).status_code == 409
    assert client.get("/api/bots").json() == {"bots": []}

    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/bots").status_code == 401
    assert client.post("/api/auth/login", json={"password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/bots").status_code == 200


def test_csrf_header_required(authed):
    r = authed.post("/api/auth/logout", headers={"X-BotDock": ""})
    assert r.status_code == 403
    assert "detail" in r.json()
    del authed.headers["X-BotDock"]
    assert authed.post("/api/auth/login", json={"password": PASSWORD}).status_code == 403
    assert authed.patch("/api/bots/b_00000000", json={}).status_code == 403
    assert authed.get("/api/bots").status_code == 200  # GET needs no header


def test_login_rate_limit(client):
    client.post("/api/auth/setup", json=setup_body(client, PASSWORD))
    client.post("/api/auth/logout")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 429
    assert "detail" in r.json()


def test_change_password_revokes_other_sessions(make_client):
    a = make_client()
    a.post("/api/auth/setup", json=setup_body(a, PASSWORD))
    b = make_client()  # second app on the same data dir == second browser session
    assert b.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    r = a.post("/api/auth/password", json={"current_password": "wrong-one", "new_password": "newpassword1"})
    assert r.status_code == 400
    r = a.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "newpassword1"})
    assert r.status_code == 200
    assert a.get("/api/bots").status_code == 200
    assert b.get("/api/bots").status_code == 401
    assert b.post("/api/auth/login", json={"password": "newpassword1"}).status_code == 200


def test_security_headers_and_errors(authed):
    r = authed.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    r = authed.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}
    r = authed.post("/api/auth/login", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


def test_seed_password_from_env(make_client):
    c = make_client(password="seeded-password")
    assert c.get("/api/auth/state").json()["setup_required"] is False
    assert c.post("/api/auth/login", json={"password": "seeded-password"}).status_code == 200


def test_frontend_fallback(authed):
    r = authed.get("/")
    assert r.status_code in (200, 503)
    r = authed.get("/some/deep/route")
    assert r.status_code in (200, 503)
    assert authed.get("/api/system").json()["bot_count"] == 0
