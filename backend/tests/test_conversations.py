from conftest import _Api

def _create_conversation(api, title="My conversation"):
    if not api.token:
        api.register()
    res = api.request("POST", "/conversations", {"title": title})
    assert res.status_code == 201
    return res.json()


def test_create_and_list_conversation(api):
    conv = _create_conversation(api)
    res = api.request("GET", "/conversations")
    assert res.status_code == 200
    ids = [c["id"] for c in res.json()["conversations"]]
    assert conv["id"] in ids


def test_conversation_scoped_to_owner(api, server_url):
    conv = _create_conversation(api)
    # A second account must not see, read, rename, or delete it.
    other = _Api(server_url)
    other.register("bob@example.com", "password123")
    res = other.request("GET", "/conversations")
    assert all(c["id"] != conv["id"] for c in res.json()["conversations"])

    res = other.request("GET", f"/conversations/{conv['id']}/messages")
    assert res.status_code == 404
    res = other.request("PATCH", f"/conversations/{conv['id']}", {"title": "hijacked"})
    assert res.status_code == 404
    res = other.request("DELETE", f"/conversations/{conv['id']}")
    assert res.status_code == 404

    # Original owner still owns it.
    res = api.request("GET", "/conversations")
    assert conv["id"] in [c["id"] for c in res.json()["conversations"]]


def test_chat_persists_user_and_assistant(api, fake_chat, server_url):
    conv = _create_conversation(api)
    res = api.request("POST", "/chat", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "Hello there"}],
    })
    assert res.status_code == 200
    assert res.json()["answer"] == "Hello!"

    res = api.request("GET", f"/conversations/{conv['id']}/messages")
    messages = res.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Hello there"
    assert messages[1]["content"] == "Hello!"


def test_chat_replay_does_not_duplicate(api, fake_chat):
    # A failed turn is retried with the same user message: the persisted user
    # row must survive both sends without duplication or orphaned rows.
    fake_chat(content="")
    conv = _create_conversation(api)
    payload = [{"role": "user", "content": "hi"}]
    for _ in range(2):
        res = api.request("POST", "/chat", {"conversation_id": conv["id"], "messages": payload})
        assert res.status_code == 502  # empty upstream -> no assistant persisted

    messages = api.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "hi"


def test_edit_resend_replaces_tail(api, fake_chat, server_url):
    # Editing the user message sends a shorter, different list; the superseded
    # assistant reply must be removed, not stacked.
    conv = _create_conversation(api)
    api.request("POST", "/chat", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "first question"}],
    })
    fake_chat(content="Second answer!")
    res = api.request("POST", "/chat", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "edited question"}],
    })
    assert res.status_code == 200
    messages = api.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    assert [m["content"] for m in messages] == ["edited question", "Second answer!"]


def test_stream_returns_assistant_message_id(api, monkeypatch):
    # The streaming path must persist the reply and hand back its message id so
    # later site publishes can be attributed across reloads.
    import json

    class StreamingFake:
        ok = True
        status_code = 200

        def iter_lines(self, decode_unicode=True):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "Site "}}]})
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "content"}}]})
            yield "data: [DONE]"

        def close(self):
            pass

    import server as server_module
    monkeypatch.setattr(server_module, "request_with_retry", lambda func, *a, **k: (StreamingFake(), None))

    conv = _create_conversation(api)
    res = api.request("POST", "/chat/stream", {
        "conversation_id": conv["id"],
        "messages": [{"role": "user", "content": "build a site"}],
    })
    assert res.status_code == 200

    message_id = None
    for line in res.text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            payload = json.loads(line[6:])
            if "assistant_message_id" in payload:
                message_id = payload["assistant_message_id"]
    assert message_id

    messages = api.request("GET", f"/conversations/{conv['id']}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["id"] == message_id


def test_chat_bad_conversation_id_rejected(api, fake_chat):
    api.register()
    res = api.request("POST", "/chat", {
        "conversation_id": "not-a-valid-id",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert res.status_code == 400


def test_chat_empty_messages_rejected(api):
    api.register()
    res = api.request("POST", "/chat", {"messages": []})
    assert res.status_code == 400