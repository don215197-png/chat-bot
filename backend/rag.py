import os
import threading

import chromadb
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Retrieval-Augmented Generation: embeds uploaded documents and serves the
# user's own chunks back into /chat as context. Everything runs locally —
# sentence-transformers embeds in-process (no API key) and Chroma stores the
# vectors on disk (no external server).
#
# Isolation model: ONE Chroma collection per user, named after the user id, so
# retrieval for user A can never see user B's chunks — querying "A" only ever
# searches A's collection. (Chroma ids are [a-zA-Z0-9._-] by design, and our
# user ids are 32-hex-char uuids, which are valid collection names.)
#
# Heavy objects (the embedding model, the Chroma client) are created ONCE and
# cached at module scope — never per request. Loading is done lazily on first
# use rather than at import so that importing this module (e.g. the existing
# non-RAG test path) never has to pull the model. Set CHROMA_DIR to a directory
# to persist vectors on disk (defaults to ./chroma_data next to this file); set
# it to an empty string to use an in-memory client (used by the test suite).
# ---------------------------------------------------------------------------

MODEL_NAME = "all-MiniLM-L6-v2"
CHROMA_DIR = os.getenv("CHROMA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data"))

_model = None
_client = None
_init_lock = threading.Lock()


def _get_model():
    # Loaded exactly once per process and cached; concurrent first-calls race
    # on the lock and the second one just reuses the cached instance. The ~90MB
    # model includes the tokenizer, so loading is a one-time ~seconds cost.
    global _model
    if _model is None:
        with _init_lock:
            if _model is None:
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def _get_client():
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                if CHROMA_DIR:
                    os.makedirs(CHROMA_DIR, exist_ok=True)
                    _client = chromadb.PersistentClient(path=CHROMA_DIR)
                else:
                    _client = chromadb.EphemeralClient()
    return _client


def _collection(user_id):
    # get_or_create is idempotent (cosine space is fixed when created).
    return _get_client().get_or_create_collection(
        user_id, metadata={"hnsw:space": "cosine"}
    )


def chunk_text(text, chunk_size=500, overlap=50):
    """Split text into overlapping character chunks.

    Each chunk is chunk_size characters; consecutive chunks re-serve the last
    overlap characters of the previous one so a sentence straddling a boundary
    is still seen whole by the model. A text that fits in one chunk (or empty
    input) returns as-is; whitespace-only gaps are dropped.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    text = text or ""
    if len(text) <= chunk_size:
        return [text] if text else []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end == len(text):
            break
        start = end - overlap
    return chunks


def embed_and_store(document_id, user_id, text):
    """Chunk, embed, and store one document under the user's collection.

    Returns the number of chunks stored. Embedding happens in-process with
    sentence-transformers; no data leaves the machine.
    """
    if not document_id or not user_id:
        raise ValueError("document_id and user_id are required")

    chunks = chunk_text(text)
    if not chunks:
        return 0

    model = _get_model()
    embeddings = model.encode(chunks, normalize_embeddings=True).tolist()

    collection = _collection(user_id)
    collection.add(
        ids=[f"{document_id}:{i}" for i in range(len(chunks))],
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{"document_id": document_id} for _ in chunks],
    )
    return len(chunks)


def retrieve_relevant_chunks(user_id, query, top_k=3):
    """Return up to top_k of the user's most similar chunks to query (strings).

    Scoped strictly to the requesting user's own collection — another user's
    documents simply do not exist under this collection name. Empty for a
    blank query or when the user has no documents yet.
    """
    query = (query or "").strip()
    if not query or not user_id:
        return []

    collection = _collection(user_id)
    if collection.count() == 0:
        return []

    model = _get_model()
    query_embedding = model.encode([query], normalize_embeddings=True).tolist()
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(top_k, collection.count()),
    )
    documents = (results or {}).get("documents")
    return documents[0] if documents else []


def delete_document_chunks(user_id, document_id):
    """Remove one document's stored chunks from the user's collection.

    Best-effort cleanup paired with DELETE /documents/: the embedding rows are
    only metadata to Chroma and must not block a file deletion if the store is
    mid-operation.
    """
    if not user_id or not document_id:
        return
    try:
        _collection(user_id).delete(where={"document_id": document_id})
    except Exception:
        pass


def delete_collection(user_id):
    """Drop a whole user's collection (not currently wired to an endpoint, but
    useful for hard resets and future account deletion)."""
    if not user_id:
        return
    try:
        _get_client().delete_collection(user_id)
    except Exception as e:
        if "not found" not in str(e).lower():
            raise