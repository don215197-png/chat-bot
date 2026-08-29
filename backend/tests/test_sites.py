import json


def _publish(api, html="<html><body>Hello world</body></html>", message_id=None):
    data = {"html": html}
    if message_id:
        data["message_id"] = message_id
    return api.request("POST", "/sites", data)


def test_publish_requires_auth(api):
    res = _publish(api)
    assert res.status_code == 401


def test_publish_and_public_read(api, server_url):
    api.register("site@example.com", "password123")
    res = _publish(api)
    assert res.status_code == 201
    payload = res.json()
    assert payload["url"].startswith("/sites/")

    # The published site is readable fully logged-out (that is the share URL).
    public = _public(server_url, payload["url"])
    assert public.status_code == 200
    assert public.headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Hello world" in public.text


def _public(server_url, path):
    import requests
    return requests.get(server_url + path)


def test_list_sites_shows_only_owned(api, server_url):
    alice = api
    alice.register("alice@example.com", "password123")
    site = _publish(alice).json()

    bob = _Api(server_url)
    bob.register("bob@example.com", "password123")
    bob_sites = bob.request("GET", "/sites").json()["sites"]
    assert all(s["id"] != site["id"] for s in bob_sites)

    alice_sites = alice.request("GET", "/sites").json()["sites"]
    assert any(s["id"] == site["id"] for s in alice_sites)


class _Api:
    def __init__(self, base):
        self.base = base
        self.token = None

    def register(self, email, password):
        res = self.request("POST", "/auth/register", {"email": email, "password": password})
        self.token = res.json()["token"]
        return res

    def request(self, method, path, data=None, headers=None):
        import requests
        req_headers = {"Content-Type": "application/json"}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        if headers:
            req_headers.update(headers)
        body = json.dumps(data) if data is not None else None
        return requests.request(method, self.base + path, headers=req_headers, data=body)


def test_delete_own_site(api, server_url):
    api.register("del@example.com", "password123")
    site = _publish(api).json()
    res = api.request("DELETE", site["url"])
    assert res.status_code == 200
    assert _public(server_url, site["url"]).status_code == 404
    assert all(s["id"] != site["id"] for s in api.request("GET", "/sites").json()["sites"])


def test_cannot_delete_others_site(api, server_url):
    alice = api
    alice.register("alice@example.com", "password123")
    site = _publish(alice).json()
    bob = _Api(server_url)
    bob.register("bob@example.com", "password123")
    res = bob.request("DELETE", site["url"])
    assert res.status_code == 403
    # Site still exists for the owner.
    assert _public(server_url, site["url"]).status_code == 200


def test_delete_unknown_site_404(api):
    api.register("nobody@example.com", "password123")
    res = api.request("DELETE", "/sites/does-not-exist")
    assert res.status_code == 404


def test_publish_rejects_garbage_id(api):
    api.register("bad@example.com", "password123")
    res = api.request("DELETE", "/sites/../../../../etc/passwd")
    assert res.status_code == 404
    res = api.request("DELETE", "/sites/..%2f..%2fetc%2fpasswd")
    assert res.status_code == 404


def test_publish_empty_html_rejected(api):
    api.register("empty@example.com", "password123")
    res = api.request("POST", "/sites", {"html": "   "})
    assert res.status_code == 400


def test_publish_over_size_limit(api):
    api.register("big@example.com", "password123")
    big_html = "<html>" + ("x" * (2 * 1024 * 1024)) + "</html>"
    res = api.request("POST", "/sites", {"html": big_html})
    assert res.status_code == 413


def test_publish_attaches_url_to_message(api, fake_chat):
    api.register("attrib@example.com", "password123")
    conv = api.request("POST", "/conversations", {"title": "site chat"}).json()
    api.request("POST", "/chat", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "build a site"}],
    })
    messages = api.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")

    res = api.request("POST", "/sites", {
        "html": "<html><body>linked</body></html>",
        "message_id": assistant["id"],
    })
    assert res.status_code == 201
    assert res.json()["message_id"] == assistant["id"]

    messages = api.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    updated = next(m for m in messages if m["id"] == assistant["id"])
    assert updated["published_url"] == res.json()["url"]


def test_publish_ignores_message_id_from_other_user(api, server_url):
    alice = api
    alice.register("alice@example.com", "password123")
    bob = _Api(server_url)
    bob.register("bob@example.com", "password123")
    conv = alice.request("POST", "/conversations", {"title": "t"}).json()
    alice.request("POST", "/chat", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "hi"}],
    })
    messages = alice.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")

    # Bob publishes with Alice's assistant message id: attribution must be
    # skipped (it is not Bob's message) but the publish itself succeeds.
    res = _publish(bob, message_id=assistant["id"])
    assert res.status_code == 201
    assert "message_id" not in res.json()
    message = alice.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    updated = next(m for m in message if m["id"] == assistant["id"])
    assert updated["published_url"] is None