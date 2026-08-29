import database


def test_register_success(api):
    res = api.register("carol@example.com", "password123")
    assert res.status_code == 201
    assert "token" in res.json()
    assert res.json()["email"] == "carol@example.com"


def test_register_normalizes_email_case(api):
    res = api.register("Carol@Example.COM", "password123")
    assert res.status_code == 201
    assert res.json()["email"] == "carol@example.com"


def test_register_duplicate_email(api):
    assert api.register("dup@example.com", "password123").status_code == 201
    # Same email, case-insensitively different casing -> conflict.
    res = api.register("DUP@example.com", "password456")
    assert res.status_code == 409


def test_register_rejects_invalid_email(api):
    res = api.register("not-an-email")
    assert res.status_code == 400


def test_register_rejects_short_password(api):
    res = api.register("short@example.com", "tiny")
    assert res.status_code == 400


def test_login_success(api):
    api.register("login@example.com", "password123")
    api.token = None
    res = api.login("login@example.com", "password123")
    assert res.status_code == 200
    assert "token" in res.json()


def test_login_wrong_password(api):
    api.register("login2@example.com", "password123")
    api.token = None
    res = api.login("login2@example.com", "wrong-password")
    assert res.status_code == 401


def test_login_unknown_email(api):
    res = api.login("ghost@example.com", "password123")
    assert res.status_code == 401


def test_logout_invalidates_session(api, server_url):
    api.register("logout@example.com", "password123")
    res = api.request("POST", "/auth/logout")
    assert res.status_code == 200
    # The session token is now dead: a protected endpoint must reject it.
    res = api.request("GET", "/conversations")
    assert res.status_code == 401


def test_password_storage_is_hashed():
    hashed = server_hash_password("password123")
    assert hashed != "password123"
    assert hashed.startswith("pbkdf2_sha256$")


def test_password_hash_roundtrip():
    # A correct password verifies; a wrong one does not, even when it differs
    # by a single character. Salts are random, so the same password hashes to a
    # different value each time.
    stored = server_hash_password("s3cret!")
    assert server_verify_password("s3cret!", stored)
    assert not server_verify_password("wrong", stored)
    assert not server_verify_password("secRet!", stored)
    assert server_hash_password("s3cret!") != stored


def server_verify_password(password, stored):
    import server
    return server.verify_password(password, stored)


def server_hash_password(password):
    # Pulled through server's own implementation to assert format/hashing.
    import server
    return server.hash_password(password)


def test_protected_endpoints_require_auth(api):
    for method, path in [
        ("GET", "/conversations"),
        ("POST", "/conversations"),
        ("GET", "/sites"),
        ("DELETE", "/sites/abc"),
        ("PATCH", "/conversations/abc"),
    ]:
        data = {"title": "x"} if method in ("POST", "PATCH") else None
        res = api.request(method, path, data=data)
        assert res.status_code == 401, f"{method} {path} should require auth but got {res.status_code}"


def test_expired_session_is_rejected(api):
    api.register("exp@example.com", "password123")
    # Rewind the session's expiry into the past, then confirm it is refused.
    api.token = None
    res = api.request("POST", "/auth/login", {"email": "exp@example.com", "password": "password123"})
    token = res.json()["token"]
    conn = database.connect()
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token = ?",
            (token,),
        )
        conn.commit()
    finally:
        database.close(conn)
    api.token = token
    res = api.request("GET", "/conversations")
    assert res.status_code == 401