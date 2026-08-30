import json

import database
import rag
import server as server_module


# ---- pure unit tests: chunking -------------------------------------------

def test_chunk_text_splits_with_overlap():
    text = "x" * 2000
    chunks = rag.chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) == 5
    assert chunks[0] == text[0:500]
    assert chunks[1] == text[450:950]  # re-serves the overlap tail
    assert chunks[4] == text[1800:2000]
    assert text[:500] in "".join(chunks)


def test_chunk_text_short_and_empty():
    assert rag.chunk_text("hello") == ["hello"]
    assert rag.chunk_text("") == []
    assert rag.chunk_text(None) == []


# ---- retrieval isolation (direct rag calls, no HTTP) -----------------------

def test_retrieve_scoped_to_own_user():
    alice = database.new_id()
    bob = database.new_id()
    rag.embed_and_store(database.new_id(), alice, "Recipe for cranberry compote from 1847")
    rag.embed_and_store(database.new_id(), bob, "Famous quote from atlas shrugged")

    alice_results = rag.retrieve_relevant_chunks(alice, "cranberry compote")
    assert any("cranberry" in c for c in alice_results)
    assert not any("atlas" in c for c in alice_results)

    bob_results = rag.retrieve_relevant_chunks(bob, "atlas shrugged")
    assert any("atlas" in c for c in bob_results)
    assert not any("cranberry" in c for c in bob_results)


def test_delete_document_chunks_removes_embeddings():
    user_id = database.new_id()
    doc_id = database.new_id()
    needle = "unique_needle_word_for_delete_test"
    rag.embed_and_store(doc_id, user_id, needle)
    assert rag.retrieve_relevant_chunks(user_id, needle) != []

    rag.delete_document_chunks(user_id, doc_id)
    assert rag.retrieve_relevant_chunks(user_id, needle) == []


# ---- /documents endpoints -------------------------------------------------

def _upload(api, filename="notes.txt", text="Default document content"):
    return api.request("POST", "/documents", {"filename": filename, "text": text})


class _Api:
    def __init__(self, base):
        self.base = base
        self.token = None

    def register(self, email, password):
        res = self.request("POST", "/auth/register", {"email": email, "password": password})
        self.token = res.json()["token"]
        return res

    def request(self, method, path, data=None):
        req_headers = {"Content-Type": "application/json"}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(data) if data is not None else None
        import requests
        return requests.request(method, self.base + path, headers=req_headers, data=body)


def test_upload_document_requires_auth(api):
    res = _upload(api)
    assert res.status_code == 401


def test_upload_and_list_documents(api):
    api.register("docs@example.com", "password123")
    res = _upload(api, filename="notes.txt", text="Important notes about widgets")
    assert res.status_code == 201
    payload = res.json()
    assert payload["id"]
    assert payload["filename"] == "notes.txt"

    documents = api.request("GET", "/documents").json()["documents"]
    assert any(d["id"] == payload["id"] and d["filename"] == "notes.txt" for d in documents)


def test_document_ownership(api, server_url):
    alice = api
    alice.register("alice@example.com", "password123")
    doc = _upload(alice, filename="a.txt", text="Alice's secret formula").json()

    bob = _Api(server_url)
    bob.register("bob@example.com", "password123")
    bob_docs = bob.request("GET", "/documents").json()["documents"]
    assert all(d["id"] != doc["id"] for d in bob_docs)
    assert bob.request("GET", "/documents/" + doc["id"]).status_code == 403
    assert bob.request("DELETE", "/documents/" + doc["id"]).status_code == 403

    # The owner can read and delete their own document.
    assert alice.request("GET", "/documents/" + doc["id"]).status_code == 200
    assert alice.request("DELETE", "/documents/" + doc["id"]).status_code == 200
    remaining = alice.request("GET", "/documents").json()["documents"]
    assert all(d["id"] != doc["id"] for d in remaining)


def test_delete_unknown_document_404(api):
    api.register("none@example.com", "password123")
    assert api.request("DELETE", "/documents/" + database.new_id()).status_code == 404
    assert api.request("DELETE", "/documents/not-a-uuid").status_code == 404


def test_upload_rejects_bad_payloads(api):
    api.register("bad@example.com", "password123")
    assert _upload(api, text="").status_code == 400
    assert api.request("POST", "/documents", {"text": "only text"}).status_code == 400
    assert api.request("POST", "/documents", {"filename": "f.txt"}).status_code == 400
    assert api.request("POST", "/documents", {"filename": "f.txt", "text": "   "}).status_code == 400


# ---- use_rag injection in /chat --------------------------------------------

class _FakeUpstream:
    ok = True
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "mock reply"}}]}


def _passthrough_upstream(monkeypatch, captured):
    def fake_call(payload, headers, stream):
        captured["payload"] = payload
        return _FakeUpstream()

    monkeypatch.setattr(server_module, "request_with_retry", lambda func, *a, **k: (func(), None))
    monkeypatch.setattr(server_module, "call_openai", fake_call)


def test_chat_injects_retrieved_context(api, monkeypatch):
    api.register("rag@example.com", "password123")
    user_id = database.get_user_by_session(api.token)["id"]
    needle = "Cranberry compote recipe from 1847 with molasses"
    rag.embed_and_store(database.new_id(), user_id, needle)

    captured = {}
    _passthrough_upstream(monkeypatch, captured)

    res = api.request("POST", "/chat", {
        "messages": [{"role": "user", "content": needle}],
        "use_rag": True,
    })
    assert res.status_code == 200
    system_prompt = captured["payload"]["messages"][0]["content"]
    assert needle in system_prompt


def test_chat_without_rag_has_no_context(api, monkeypatch):
    api.register("norag@example.com", "password123")

    captured = {}
    _passthrough_upstream(monkeypatch, captured)

    res = api.request("POST", "/chat", {
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert res.status_code == 200
    system_prompt = captured["payload"]["messages"][0]["content"]
    assert "retrieved context" not in system_prompt