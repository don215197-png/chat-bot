import server as server_module


def _fill_chat_bucket(api):
    # The chat burst limit defaults to 20 / 60s. Fire 20 requests to fill it.
    for _ in range(server_module.CHAT_MAX_REQUESTS):
        res = api.request("POST", "/chat", {"messages": [{"role": "user", "content": "hi"}]})
        # Auth happens after the rate check, so a missing login still consumes a slot.
        assert res.status_code in (200, 401)


def test_chat_burst_is_rate_limited(api, monkeypatch):
    monkeypatch.setattr(server_module, "request_with_retry",
                        lambda func, *a, **k: (_Fake(), None))
    api.register("rate@example.com", "password123")
    _fill_chat_bucket(api)
    res = api.request("POST", "/chat", {"messages": [{"role": "user", "content": "over"}]})
    assert res.status_code == 429
    assert res.headers.get("Retry-After") is not None
    body = res.json()
    assert body["retry_after"] is not None


def test_chat_burst_boundary(api, monkeypatch):
    # Boundary behaviour: exactly CHAT_MAX_REQUESTS are allowed, the next one
    # in the same window is rejected. Guards against off-by-one limiter bugs
    # that would either throttle too early or never throttle at all.
    monkeypatch.setattr(server_module, "request_with_retry",
                        lambda func, *a, **k: (_Fake(), None))
    api.register("boundary@example.com", "password123")
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    for _ in range(server_module.CHAT_MAX_REQUESTS):
        assert api.request("POST", "/chat", payload).status_code == 200
    assert api.request("POST", "/chat", payload).status_code == 429


def test_publish_uses_separate_bucket(api):
    # Filling the chat bucket must not starve the publish endpoint: separate
    # windows/limits per action type.
    api.register("sep@example.com", "password123")
    server_module._rate_logs.clear()
    for _ in range(server_module.CHAT_MAX_REQUESTS):
        api.request("POST", "/chat", {"messages": [{"role": "user", "content": "hi"}]})
    res = api.request("POST", "/sites", {"html": "<html>ok</html>"})
    assert res.status_code == 201


def test_publish_daily_limit(api, monkeypatch):
    monkeypatch.setattr(server_module, "CHAT_MAX_REQUESTS", 100000)
    monkeypatch.setattr(server_module, "PUBLISH_DAILY_LIMIT", 2)
    api.register("daily@example.com", "password123")
    assert api.request("POST", "/sites", {"html": "<html>1</html>"}).status_code == 201
    assert api.request("POST", "/sites", {"html": "<html>2</html>"}).status_code == 201
    res = api.request("POST", "/sites", {"html": "<html>3</html>"})
    assert res.status_code == 429
    assert res.json()["retry_after"] is not None


class _Fake:
    ok = True
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "hi"}}]}