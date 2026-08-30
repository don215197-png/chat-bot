import os
import sys
import json
import threading
from urllib.parse import urlsplit

import pytest

# Point the whole test run at a throwaway PostgreSQL database, created before
# database/server are imported (both read env vars at import time). If the test
# database does not exist yet (e.g. the first local run against a fresh
# `docker compose up db`), connect to the server's maintenance database and
# create it — CI's postgres service just ships it pre-created via POSTGRES_DB.
TEST_DB = "chatbot_test"
TEST_DB_PORT = os.environ.get("TEST_DB_PORT", "5433")
os.environ["DATABASE_URL"] = f"postgresql://chatbot:chatbot@localhost:{TEST_DB_PORT}/{TEST_DB}"
os.environ.pop("OPENCODE_API_KEY", None)
# In-memory Chroma for the whole test run: isolated per process, nothing written
# to disk. Set before server/rag are imported (rag reads it at import time).
os.environ["CHROMA_DIR"] = ""

import psycopg  # noqa: E402

import database  # noqa: E402
import server as server_module  # noqa: E402
import requests  # noqa: E402


def _ensure_test_database():
    url = os.environ["DATABASE_URL"]
    dbname = urlsplit(url).path.lstrip("/")
    try:
        conn = psycopg.connect(url)
        conn.close()
        return
    except psycopg.OperationalError:
        pass
    # Same credentials, but connected to the always-present 'postgres' db.
    maintenance = url.rsplit("/", 1)[0] + "/postgres"
    admin = psycopg.connect(maintenance)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()


_ensure_test_database()


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