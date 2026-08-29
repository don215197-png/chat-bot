import os
import sys
import json
import threading

import pytest

# Point the whole test run at an isolated, throwaway database created before
# database.server are imported (both read DATABASE_PATH at import time).
TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_chatbot.db")
os.environ["DATABASE_PATH"] = TEST_DB
os.environ.pop("OPENCODE_API_KEY", None)

import database  # noqa: E402
import server as server_module  # noqa: E402
import requests  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    # Every test starts from clean tables so test files are order-independent.
    database.init_db(drop_existing=True)
    yield


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    # Chat/publish burst buckets and the daily counters are module globals;
    # reset them so a test that fills a bucket cannot starve a later one.
    server_module._rate_logs.clear()
    server_module._daily_counts.clear()
    yield
    server_module._rate_logs.clear()
    server_module._daily_counts.clear()


@pytest.fixture(autouse=True)
def no_upstream(monkeypatch):
    # Default guard: never let a test reach the real OpenCode API (network is
    # unavailable/blocked in CI). Each test can override with its own
    # monkeypatch.setattr(server_module, "request_with_retry", ...).
    monkeypatch.setattr(server_module, "request_with_retry",
                        lambda func, *a, **k: (FakeResponse(), None))


@pytest.fixture()
def server_url():
    srv = server_module.ReusableThreadingServer(("127.0.0.1", 0), server_module.ChatHandler)
    srv.daemon_threads = True  # handler threads must not block shutdown
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


class _Api:
    """Tiny HTTP helper: registers/login a caller and attaches the session token
    to every authed request."""

    def __init__(self, base):
        self.base = base
        self.token = None

    def request(self, method, path, data=None, headers=None):
        req_headers = {"Content-Type": "application/json"}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        if headers:
            req_headers.update(headers)
        body = json.dumps(data) if data is not None else None
        return requests.request(method, self.base + path, headers=req_headers, data=body)

    def register(self, email="alice@example.com", password="password123"):
        res = self.request("POST", "/auth/register", {"email": email, "password": password})
        if res.ok:
            self.token = res.json()["token"]
        return res

    def login(self, email="alice@example.com", password="password123"):
        res = self.request("POST", "/auth/login", {"email": email, "password": password})
        if res.ok:
            self.token = res.json()["token"]
        return res


@pytest.fixture()
def api(server_url):
    return _Api(server_url)


class FakeResponse:
    """Stand-in for the upstream OpenAI-style response used by /chat tests so
    tests never hit the real network."""

    def __init__(self, content="Hello!", status_code=200):
        self.content = content
        self.ok = status_code < 400
        self.status_code = status_code

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.fixture()
def fake_chat(monkeypatch):
    def _set(content="Hello!", status_code=200):
        fake = FakeResponse(content=content, status_code=status_code)
        monkeypatch.setattr(server_module, "request_with_retry", lambda func, *a, **k: (fake, None))
        return fake
    return _set