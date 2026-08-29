import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# SQLite persistence for user accounts, conversations, messages, sessions and
# published-site ownership. One short-lived connection per operation against
# the default file DB (ThreadingHTTPServer runs each request on its own thread,
# so a shared connection would need cross-thread guarding). Tests may point
# DATABASE_PATH at ":memory:", which requires a single shared connection —
# guarded by a lock — since each :memory: connection is its own empty database.
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "chatbot.db"))

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

CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sites_user ON published_sites(user_id, created_at);
"""

_memory_conns = []
_conn_guard = threading.Lock()


def _raw_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if DB_PATH != ":memory:":
        # WAL lets readers proceed while a writer holds the lock (file DBs only;
        # journal_mode is meaningless for :memory:). busy_timeout makes a brief
        # lock contention retry for up to 5s instead of failing with "database
        # is locked" — SQLite serializes writers, so this tunes the wait rather
        # than removing serialization.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    return conn


def connect():
    # A shared, locked connection for the in-memory DB; a fresh per-call
    # connection for file-backed databases.
    if DB_PATH == ":memory:":
        with _conn_guard:
            if not _memory_conns:
                _memory_conns.append(_raw_connect())
            return _memory_conns[0]
    return _raw_connect()


def close(conn):
    if DB_PATH != ":memory:":
        try:
            conn.close()
        except Exception:
            pass


def init_db(drop_existing=False):
    conn = connect()
    try:
        if drop_existing:
            for table in (
                "published_sites",
                "messages",
                "conversations",
                "sessions",
                "users",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        close(conn)


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid.uuid4().hex


def _row_to_dict(row):
    return dict(row) if row is not None else None


# ---- users / sessions ------------------------------------------------------

def create_user(email, password_hash):
    conn = connect()
    try:
        user_id = new_id()
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, password_hash, utcnow_iso()),
        )
        conn.commit()
        return user_id
    finally:
        close(conn)


def get_user_by_email(email):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def get_user_by_id(user_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def create_session(user_id):
    conn = connect()
    try:
        token = new_id()
        now = utcnow_iso()
        expires = datetime.fromtimestamp(time.time() + SESSION_TTL_SECONDS, tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
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
        row = conn.execute(
            """SELECT u.* FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, utcnow_iso()),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def delete_session(token):
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        close(conn)


# ---- conversations ---------------------------------------------------------

def create_conversation(user_id, title=""):
    conn = connect()
    try:
        conversation_id = new_id()
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
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
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def list_user_conversations(user_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        close(conn)


def update_conversation_title(conversation_id, title):
    conn = connect()
    try:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, utcnow_iso(), conversation_id),
        )
        conn.commit()
    finally:
        close(conn)


def touch_conversation(conversation_id):
    conn = connect()
    try:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (utcnow_iso(), conversation_id),
        )
        conn.commit()
    finally:
        close(conn)


def delete_conversation(conversation_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()
    finally:
        close(conn)


# ---- messages --------------------------------------------------------------

def list_messages(conversation_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, role, content, published_url, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        close(conn)


def add_message(conversation_id, role, content, published_url=None):
    conn = connect()
    try:
        message_id = new_id()
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, published_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, content, published_url, utcnow_iso()),
        )
        conn.commit()
        return message_id
    finally:
        close(conn)


def truncate_messages_from(conversation_id, keep_count):
    # Removes every persisted message at or beyond keep_count (in order). Used
    # when an edit/retry resends a shorter message list so superseded AI replies
    # don't linger in history.
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at, rowid LIMIT -1 OFFSET ?",
            (conversation_id, keep_count),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM messages WHERE id = ?", (r["id"],))
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
        row = conn.execute(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.published_url, "
            "       c.user_id AS user_id "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def attach_published_url(message_id, url):
    conn = connect()
    try:
        conn.execute("UPDATE messages SET published_url = ? WHERE id = ?", (url, message_id))
        conn.commit()
    finally:
        close(conn)


# ---- published sites -------------------------------------------------------

def create_published_site(site_id, user_id, title, size_bytes):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO published_sites (id, user_id, title, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (site_id, user_id, title, size_bytes, utcnow_iso()),
        )
        conn.commit()
    finally:
        close(conn)


def get_published_site(site_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM published_sites WHERE id = ?", (site_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        close(conn)


def list_user_sites(user_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, size_bytes, created_at FROM published_sites "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        close(conn)


def delete_published_site(site_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM published_sites WHERE id = ?", (site_id,))
        conn.commit()
    finally:
        close(conn)