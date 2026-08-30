import os
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------------------
# PostgreSQL persistence for user accounts, conversations, messages, sessions
# and published-site ownership. Access goes through a psycopg3 connection pool
# (one checkout per operation, released straight back), so concurrent requests —
# ThreadingHTTPServer runs each on its own thread — never block each other:
# MVCC gives readers a consistent snapshot while writers proceed, and short
# transactions serialize naturally under the pool's cap.
#
# Point DATABASE_URL at any PostgreSQL (local container, CI service, managed
# host). server.py's HTTP/AI logic is fully agnostic to the storage engine —
# only this module knows the database.
# ---------------------------------------------------------------------------

DEFAULT_DATABASE_URL = "postgresql://chatbot:chatbot@localhost:5433/chatbot"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  seq BIGSERIAL,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  published_url TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published_sites (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  title TEXT,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  filename TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_providers (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  api_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sites_user ON published_sites(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at);
"""

_pool = None
_pool_guard = threading.Lock()


def _get_pool():
    # Lazy creation so importing the module (e.g. from tests, or while Postgres
    # is still warming up) never fails; the first connect() performs the
    # handshake. dict_row factory makes every row a plain dict (row["col"]).
    global _pool
    if _pool is None:
        with _pool_guard:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row},
                )
    return _pool


def connect():
    return _get_pool().getconn()


def close(conn, broken=False):
    # Return the connection to the pool; if it is broken (e.g. the server
    # restarted under us, or psycopg_pool 3.3 dropped the putconn(close=) flag
    # this module once passed), discard it instead. Any uncommitted/aborted
    # transaction is rolled back so a caught error can never poison reuse.
    if conn is None:
        return
    discard = broken or conn.broken or conn.closed
    if not discard:
        try:
            conn.rollback()
        except Exception:
            discard = True
    try:
        if discard:
            conn.close()
        else:
            _get_pool().putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _execute(conn, sql, params=()):
    # psycopg3 runs SQL on a Cursor, not the Connection; each helper wraps the
    # checkout in a short-lived cursor (the row_factory from the pool applies),
    # leaving the connection itself open for the caller to commit/release.
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _fetchone(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def init_db(drop_existing=False):
    conn = connect()
    try:
        if drop_existing:
            for table in (
                "user_providers",
                "published_sites",
                "documents",
                "messages",
                "conversations",
                "sessions",
                "users",
            ):
                _execute(conn, f"DROP TABLE IF EXISTS {table}")
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                _execute(conn, stmt)
        conn.commit()
    finally:
        close(conn)


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid.uuid4().hex


# ---- users / sessions ------------------------------------------------------

def create_user(email, password_hash):
    conn = connect()
    try:
        user_id = new_id()
        _execute(
            conn,
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (%s, %s, %s, %s)",
            (user_id, email, password_hash, utcnow_iso()),
        )
        conn.commit()
        return user_id
    finally:
        close(conn)


def get_user_by_email(email):
    conn = connect()
    try:
        return _fetchone(conn, "SELECT * FROM users WHERE email = %s", (email,))
    finally:
        close(conn)


def get_user_by_id(user_id):
    conn = connect()
    try:
        return _fetchone(conn, "SELECT * FROM users WHERE id = %s", (user_id,))
    finally:
        close(conn)


def create_session(user_id):
    conn = connect()
    try:
        token = new_id()
        now = utcnow_iso()
        expires = datetime.fromtimestamp(time.time() + SESSION_TTL_SECONDS, tz=timezone.utc).isoformat()
        _execute(
            conn,
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (token, user_id, now, expires),
        )
        conn.commit()
        return token
    finally:
        close(conn)


def get_user_by_session(token):
    if not token:
        return None
    conn = connect()
    try:
        return _fetchone(
            conn,
            """SELECT u.* FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = %s AND s.expires_at > %s""",
            (token, utcnow_iso()),
        )
    finally:
        close(conn)


def delete_session(token):
    conn = connect()
    try:
        _execute(conn, "DELETE FROM sessions WHERE token = %s", (token,))
        conn.commit()
    finally:
        close(conn)


# ---- conversations ---------------------------------------------------------

def create_conversation(user_id, title=""):
    conn = connect()
    try:
        conversation_id = new_id()
        now = utcnow_iso()
        _execute(
            conn,
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (conversation_id, user_id, title, now, now),
        )
        conn.commit()
        return {
            "id": conversation_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        close(conn)


def get_conversation(conversation_id):
    conn = connect()
    try:
        return _fetchone(conn, "SELECT * FROM conversations WHERE id = %s", (conversation_id,))
    finally:
        close(conn)


def list_user_conversations(user_id):
    conn = connect()
    try:
        return _fetchall(
            conn,
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,),
        )
    finally:
        close(conn)


def update_conversation_title(conversation_id, title):
    conn = connect()
    try:
        _execute(
            conn,
            "UPDATE conversations SET title = %s, updated_at = %s WHERE id = %s",
            (title, utcnow_iso(), conversation_id),
        )
        conn.commit()
    finally:
        close(conn)


def touch_conversation(conversation_id):
    conn = connect()
    try:
        _execute(
            conn,
            "UPDATE conversations SET updated_at = %s WHERE id = %s",
            (utcnow_iso(), conversation_id),
        )
        conn.commit()
    finally:
        close(conn)


def delete_conversation(conversation_id):
    conn = connect()
    try:
        _execute(conn, "DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
        _execute(conn, "DELETE FROM conversations WHERE id = %s", (conversation_id,))
        conn.commit()
    finally:
        close(conn)


# ---- messages --------------------------------------------------------------

def list_messages(conversation_id):
    conn = connect()
    try:
        return _fetchall(
            conn,
            "SELECT id, role, content, published_url, created_at FROM messages "
            "WHERE conversation_id = %s ORDER BY seq",
            (conversation_id,),
        )
    finally:
        close(conn)


def add_message(conversation_id, role, content, published_url=None):
    conn = connect()
    try:
        message_id = new_id()
        _execute(
            conn,
            "INSERT INTO messages (id, conversation_id, role, content, published_url, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (message_id, conversation_id, role, content, published_url, utcnow_iso()),
        )
        conn.commit()
        return message_id
    finally:
        close(conn)


def truncate_messages_from(conversation_id, keep_count):
    # Removes every persisted message at or beyond keep_count (in insertion
    # order). Used when an edit/retry resends a shorter message list so
    # superseded AI replies don't linger in history.
    conn = connect()
    try:
        stale = _fetchall(
            conn,
            "SELECT id FROM messages WHERE conversation_id = %s "
            "ORDER BY seq OFFSET %s",
            (conversation_id, keep_count),
        )
        for r in stale:
            _execute(conn, "DELETE FROM messages WHERE id = %s", (r["id"],))
        conn.commit()
    finally:
        close(conn)


def sync_messages(conversation_id, submitted):
    # Align the persisted messages with what the client sent: find the longest
    # matching prefix (role+content in order), drop the stale tail beyond it
    # (a superseded AI reply or an edited message), then insert whatever new
    # messages the client introduced. Duplicate/replayed tails (e.g. a retry
    # after a failed upstream call) match the prefix and are skipped.
    persisted = list_messages(conversation_id)
    i = 0
    while i < len(persisted) and i < len(submitted):
        p, s = persisted[i], submitted[i]
        if p["role"] == s.get("role") and p["content"] == s.get("content", ""):
            i += 1
        else:
            break
    if i < len(persisted):
        truncate_messages_from(conversation_id, i)
    for msg in submitted[i:]:
        add_message(conversation_id, msg.get("role", "user"), msg.get("content", ""))
    touch_conversation(conversation_id)


def get_message(message_id):
    conn = connect()
    try:
        return _fetchone(
            conn,
            "SELECT m.id, m.conversation_id, m.role, m.content, m.published_url, "
            "       c.user_id AS user_id "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = %s",
            (message_id,),
        )
    finally:
        close(conn)


def attach_published_url(message_id, url):
    conn = connect()
    try:
        _execute(conn, "UPDATE messages SET published_url = %s WHERE id = %s", (url, message_id))
        conn.commit()
    finally:
        close(conn)


# ---- published sites -------------------------------------------------------

def create_published_site(site_id, user_id, title, size_bytes):
    conn = connect()
    try:
        _execute(
            conn,
            "INSERT INTO published_sites (id, user_id, title, size_bytes, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (site_id, user_id, title, size_bytes, utcnow_iso()),
        )
        conn.commit()
    finally:
        close(conn)


def get_published_site(site_id):
    conn = connect()
    try:
        return _fetchone(conn, "SELECT * FROM published_sites WHERE id = %s", (site_id,))
    finally:
        close(conn)


def list_user_sites(user_id):
    conn = connect()
    try:
        return _fetchall(
            conn,
            "SELECT id, title, size_bytes, created_at FROM published_sites "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
    finally:
        close(conn)


def delete_published_site(site_id):
    conn = connect()
    try:
        _execute(conn, "DELETE FROM published_sites WHERE id = %s", (site_id,))
        conn.commit()
    finally:
        close(conn)


# ---- uploaded documents (RAG sources) --------------------------------------

def create_document(document_id, user_id, filename):
    conn = connect()
    try:
        _execute(
            conn,
            "INSERT INTO documents (id, user_id, filename, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (document_id, user_id, filename, utcnow_iso()),
        )
        conn.commit()
    finally:
        close(conn)


def get_document(document_id):
    conn = connect()
    try:
        return _fetchone(conn, "SELECT * FROM documents WHERE id = %s", (document_id,))
    finally:
        close(conn)


def list_user_documents(user_id):
    conn = connect()
    try:
        return _fetchall(
            conn,
            "SELECT id, filename, created_at FROM documents "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
    finally:
        close(conn)


def delete_document(document_id):
    conn = connect()
    try:
        _execute(conn, "DELETE FROM documents WHERE id = %s", (document_id,))
        conn.commit()
    finally:
        close(conn)


# ---- per-user AI provider (bring-your-own-key) ------------------------------

def get_user_provider(user_id):
    conn = connect()
    try:
        return _fetchone(
            conn,
            "SELECT api_url, api_key, model, updated_at FROM user_providers WHERE user_id = %s",
            (user_id,),
        )
    finally:
        close(conn)


def set_user_provider(user_id, api_url, api_key, model):
    conn = connect()
    try:
        _execute(
            conn,
            "INSERT INTO user_providers (user_id, api_url, api_key, model, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "api_url = EXCLUDED.api_url, api_key = EXCLUDED.api_key, "
            "model = EXCLUDED.model, updated_at = EXCLUDED.updated_at",
            (user_id, api_url, api_key, model, utcnow_iso()),
        )
        conn.commit()
    finally:
        close(conn)


def delete_user_provider(user_id):
    conn = connect()
    try:
        _execute(conn, "DELETE FROM user_providers WHERE user_id = %s", (user_id,))
        conn.commit()
    finally:
        close(conn)