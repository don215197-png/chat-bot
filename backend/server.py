import os
import json
import math
import re
import secrets
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque
import requests
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dotenv import load_dotenv

import database

load_dotenv()

OPENCODE_API_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY")

DEFAULT_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
# Extend the allowlist at deploy time without editing code.
# Comma-separated list, e.g. ALLOWED_ORIGINS=https://myapp.com,https://www.myapp.com
ALLOWED_ORIGINS = DEFAULT_ALLOWED_ORIGINS + [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

MAX_HISTORY = 20
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5

# Per-IP rate limiting. Buckets are in-memory and per-process (documented in the
# README): restarting the backend resets all counters, and multi-worker
# deployments share no state. Limits are configurable via environment variables
# for a deployment behind a shared NAT where a flat per-IP cap would be too
# tight; TRUST_PROXY_HEADERS keys off a real per-user identifier instead.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes")
CHAT_MAX_REQUESTS = int(os.getenv("CHAT_MAX_REQUESTS", "20"))
CHAT_WINDOW_SECONDS = int(os.getenv("CHAT_WINDOW_SECONDS", "60"))
PUBLISH_MAX_REQUESTS = int(os.getenv("PUBLISH_MAX_REQUESTS", "5"))
PUBLISH_WINDOW_SECONDS = int(os.getenv("PUBLISH_WINDOW_SECONDS", "600"))
PUBLISH_DAILY_LIMIT = int(os.getenv("PUBLISH_DAILY_LIMIT", "50"))

_rate_lock = threading.Lock()
# Sliding-window log per IP, keyed by (window_seconds, max_requests) so the
# chat and publish buckets never collide.
_rate_logs = defaultdict(lambda: defaultdict(deque))
# publish daily cap: ip -> {"2024-01-01": count} (one rolling day per IP).
_daily_counts = defaultdict(dict)

# Generated-site hosting: the HTML *content* is a flat file, ownership and
# metadata live in SQLite (published_sites rows). Raw GET /sites/<id> is
# public; management (list/delete) requires login.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITES_DIR = os.path.join(BASE_DIR, "generated_sites")
SITE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB body cap
SITE_TTL_SECONDS = 7 * 24 * 60 * 60  # retention policy: 7 days
_SITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 200_000

def _ensure_sites_dir():
    os.makedirs(SITES_DIR, exist_ok=True)

def _valid_site_id(site_id):
    # Strict allowlist so an arbitrary request path can never escape the
    # generated_sites directory (path traversal / directory listings).
    return bool(site_id) and bool(_SITE_ID_RE.match(site_id))

def _site_path(site_id):
    if not _valid_site_id(site_id):
        return None
    return os.path.join(SITES_DIR, f"{site_id}.html")

def site_title_from_html(html):
    # Best-effort <title> for the management list; falls back to the id.
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:80] or None

def cleanup_expired_sites(ttl=SITE_TTL_SECONDS):
    # Retention: delete generated sites older than TTL. Runs at startup and is
    # cheap enough to leave as-is for the tree's small size. Errors on a single
    # file are ignored so one bad file cannot take the server down. Site rows
    # whose file is gone are pruned here too, keeping SQLite authoritative.
    _ensure_sites_dir()
    now = time.time()
    for filename in os.listdir(SITES_DIR):
        if not filename.endswith(".html"):
            continue
        path = os.path.join(SITES_DIR, filename)
        site_id = filename[:-len(".html")]
        expired = True
        try:
            if now - os.path.getmtime(path) > ttl:
                os.remove(path)
            else:
                expired = False
        except OSError:
            pass  # missing/unreadable file counts as expired
        if expired:
            try:
                database.delete_published_site(site_id)
            except Exception:
                pass

def client_ip(handler):
    # Resolve the caller's IP for rate limiting. When the backend sits behind a
    # reverse proxy almost every request shares the proxy's socket address, so
    # TRUST_PROXY_HEADERS (opt-in) prefers the leftmost X-Forwarded-For value —
    # the real peer. Default is the raw socket peer so no untrusted header can
    # spoof the bucket, at the cost of treating a shared NAT as one client.
    if TRUST_PROXY_HEADERS:
        forwarded = handler.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip() if forwarded else ""
        if first:
            return first
    return handler.client_address[0]

def check_rate_limit(ip, max_requests, window_seconds):
    # Sliding-window log. Returns (allowed, retry_after_seconds). The retry_after
    # is how long the caller must wait before the oldest entry falls out of the
    # window, used for both the Retry-After header and the 429 body.
    now = time.time()
    key = (window_seconds, max_requests)
    with _rate_lock:
        history = _rate_logs[key][ip]
        while history and now - history[0] >= window_seconds:
            history.popleft()
        if len(history) >= max_requests:
            retry_after = int(math.ceil(window_seconds - (now - history[0]))) if history else window_seconds
            return False, max(retry_after, 1)
        history.append(now)
        return True, None

def check_daily_limit(ip, limit):
    # Publish cap per UTC day. Keeps legacy days behind the current one so the
    # dict never grows beyond ~1 entry per IP. Returns (allowed, reset_seconds).
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _rate_lock:
        if limit <= 0:
            return True, None
        entry = _daily_counts[ip]
        today = entry.get(day, 0)
        if today >= limit:
            next_day_ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400
            return False, max(int(math.ceil(next_day_ts - now)), 1)
        entry[day] = today + 1
        for old_day in list(entry.keys()):
            if old_day != day:
                del entry[old_day]
        return True, None

# ---- authentication ---------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "$".join(["pbkdf2_sha256", str(PBKDF2_ITERATIONS), salt.hex(), digest.hex()])

def verify_password(password, stored):
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False

def should_retry(error, response=None):
    if isinstance(error, requests.exceptions.Timeout):
        return True
    if isinstance(error, requests.exceptions.ConnectionError):
        return True
    if response is not None and 500 <= response.status_code < 600:
        return True
    return False

def request_with_retry(func, *args, **kwargs):
    last_error = None
    last_response = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = func(*args, **kwargs)
            if should_retry(None, response):
                last_response = response
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                return response, None
            return response, None
        except requests.exceptions.RequestException as e:
            last_error = e
            if should_retry(e):
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
            raise
    return last_response, last_error

def parse_ai_error(response: requests.Response) -> str:
    try:
        error_data = response.json()
        if "error" in error_data:
            err = error_data["error"]
            if isinstance(err, dict):
                return err.get("message", f"AI provider error: {response.status_code}")
            return str(err)
    except (ValueError, KeyError):
        pass
    return f"AI provider error: {response.status_code}"

def validate_messages(messages):
    if not isinstance(messages, list) or len(messages) == 0:
        return False, "Messages array cannot be empty"
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            return False, f"Message {i} must have 'role' and 'content'"
        if msg["role"] not in ("user", "assistant"):
            return False, f"Message {i} role must be 'user' or 'assistant'"
    if messages[-1]["role"] != "user":
        return False, "Last message must be from user"
    return True, None

def clean_reasoning_leak(content):
    # Strip an old leaked reasoning preamble from a freshly completed reply
    # before it is stored/rendered. Mirrors the client-side strip; applied here
    # too so the persisted copy is exactly what the user sees.
    head = content[:150]
    if "The user " in head or "As an AI " in head:
        if "<!DOCTYPE" in content:
            idx = content.find("<!DOCTYPE")
            if idx != -1:
                return content[idx:]
        if "```" in content:
            idx = content.find("```")
            if idx != -1:
                return content[idx:]
    return content

class ChatHandler(BaseHTTPRequestHandler):
    def _get_origin(self):
        # Only return the request's Origin if it is explicitly allowlisted.
        # Returns None for any other (or missing) origin so the response is
        # sent WITHOUT permissive CORS headers, blocking cross-site calls with
        # credentials. No wildcard fallback: credentials must never be echoed
        # back to an unverified origin.
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            return origin
        return None

    def _send_cors_headers(self):
        origin = self._get_origin()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def _get_authenticated_user(self):
        # Bearer session token -> user row, or None. Every endpoint that needs
        # a logged-in user calls this first and returns 401 when it is None.
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return database.get_user_by_session(token.strip())

    def _require_user(self):
        user = self._get_authenticated_user()
        if user is None:
            self.send_json_response(401, {"detail": "Please log in to continue"})
        return user

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        if path == "/health":
            self.send_json_response(200, {"status": "ok"})
        elif path == "/conversations":
            user = self._require_user()
            if user:
                self.handle_list_conversations(user)
        elif path.startswith("/conversations/"):
            self.handle_get_conversation(path[len("/conversations/"):])
        elif path == "/sites":
            user = self._require_user()
            if user:
                self.handle_list_sites(user)
        elif path.startswith("/sites/"):
            self.handle_get_site(path[len("/sites/"):])
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            if path == "/auth/register":
                self.handle_register()
            elif path == "/auth/login":
                self.handle_login()
            elif path == "/auth/logout":
                self.handle_logout()
            elif path in ("/chat", "/chat/stream"):
                # Rate limit before auth: the check is cheap and the buckets
                # protect the backend from burst traffic even when a caller is
                # anonymous.
                ip = client_ip(self)
                allowed, retry_after = check_rate_limit(ip, CHAT_MAX_REQUESTS, CHAT_WINDOW_SECONDS)
                if not allowed:
                    self.send_json_response(429, {"detail": "Too many chat requests. Please wait and try again.", "retry_after": retry_after}, retry_after=retry_after)
                    return
                user = self._require_user()
                if user:
                    self.handle_chat(user, stream=(path == "/chat/stream"))
            elif path == "/conversations":
                user = self._require_user()
                if user:
                    self.handle_create_conversation(user)
            elif path == "/sites":
                # Publishing writes to disk, so protect it with the same session
                # auth used by /chat. Reading a published site (GET) is public.
                ip = client_ip(self)
                allowed, retry_after = check_rate_limit(ip, PUBLISH_MAX_REQUESTS, PUBLISH_WINDOW_SECONDS)
                if not allowed:
                    self.send_json_response(429, {"detail": "Too many sites published. Please wait and try again.", "retry_after": retry_after}, retry_after=retry_after)
                    return
                allowed, retry_after = check_daily_limit(ip, PUBLISH_DAILY_LIMIT)
                if not allowed:
                    self.send_json_response(429, {"detail": "Daily publishing limit reached. Please come back tomorrow.", "retry_after": retry_after}, retry_after=retry_after)
                    return
                user = self._require_user()
                if user:
                    self.handle_create_site(user)
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_json_response(500, {"detail": f"Internal server error: {str(e)}"})
            except Exception:
                pass

    def do_PATCH(self):
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            if path.startswith("/conversations/"):
                self.handle_rename_conversation(path[len("/conversations/"):])
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_json_response(500, {"detail": f"Internal server error: {str(e)}"})
            except Exception:
                pass

    def do_DELETE(self):
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            if path.startswith("/conversations/"):
                self.handle_delete_conversation(path[len("/conversations/"):])
            elif path.startswith("/sites/"):
                self.handle_delete_site(path[len("/sites/"):])
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_json_response(500, {"detail": f"Internal server error: {str(e)}"})
            except Exception:
                pass

    def _read_json_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
        return json.loads(body) if body else {}

    # ---- auth handlers -----------------------------------------------------

    def handle_register(self):
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "")
        if not _EMAIL_RE.match(email):
            self.send_json_response(400, {"detail": "Invalid email address"})
            return
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            self.send_json_response(400, {"detail": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"})
            return
        if database.get_user_by_email(email):
            self.send_json_response(409, {"detail": "Email already registered"})
            return
        user_id = database.create_user(email, hash_password(password))
        token = database.create_session(user_id)
        self.send_json_response(201, {"token": token, "email": email})

    def handle_login(self):
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "") or ""
        user = database.get_user_by_email(email)
        if user is None or not verify_password(password, user["password_hash"]):
            self.send_json_response(401, {"detail": "Invalid email or password"})
            return
        token = database.create_session(user["id"])
        self.send_json_response(200, {"token": token, "email": user["email"]})

    def handle_logout(self):
        auth = self.headers.get("Authorization", "")
        _, _, token = auth.partition(" ")
        if token.strip():
            database.delete_session(token.strip())
        self.send_json_response(200, {"status": "ok"})

    # ---- conversation handlers ---------------------------------------------

    def handle_list_conversations(self, user):
        conversations = database.list_user_conversations(user["id"])
        self.send_json_response(200, {"conversations": conversations})

    def handle_create_conversation(self, user):
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return
        title = str(data.get("title", "") or "").strip()
        conversation = database.create_conversation(user["id"], title=title[:100])
        self.send_json_response(201, conversation)

    def handle_get_conversation(self, rest):
        # /conversations/<id>/messages
        if not rest.endswith("/messages"):
            self.send_error(404)
            return
        conversation_id = rest[:-len("/messages")]
        user = self._require_user()
        if not user:
            return
        if not _UUID_RE.match(conversation_id):
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        conversation = database.get_conversation(conversation_id)
        if conversation is None or conversation["user_id"] != user["id"]:
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        messages = database.list_messages(conversation_id)
        self.send_json_response(200, {
            "conversation": {
                "id": conversation["id"],
                "title": conversation["title"],
                "created_at": conversation["created_at"],
                "updated_at": conversation["updated_at"],
            },
            "messages": messages,
        })

    def handle_rename_conversation(self, conversation_id):
        user = self._require_user()
        if not user:
            return
        if not _UUID_RE.match(conversation_id):
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        conversation = database.get_conversation(conversation_id)
        if conversation is None or conversation["user_id"] != user["id"]:
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return
        title = str(data.get("title", "") or "").strip()
        database.update_conversation_title(conversation_id, title[:100])
        self.send_json_response(200, {"id": conversation_id, "title": title})

    def handle_delete_conversation(self, conversation_id):
        user = self._require_user()
        if not user:
            return
        if not _UUID_RE.match(conversation_id):
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        conversation = database.get_conversation(conversation_id)
        if conversation is None or conversation["user_id"] != user["id"]:
            self.send_json_response(404, {"detail": "Conversation not found"})
            return
        database.delete_conversation(conversation_id)
        self.send_json_response(200, {"deleted": conversation_id})

    # ---- site handlers -----------------------------------------------------

    def handle_create_site(self, user):
        # Receive a raw HTML document and store it under a random, unguessable
        # id, owned by the logged-in user. Each publish creates a new id and
        # never overwrites an existing site, so published pages are immutable
        # once created. Optional message_id links the published URL back to the
        # originating chat message so it survives reloads.
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return

        html = data.get("html")
        if not isinstance(html, str) or not html.strip():
            self.send_json_response(400, {"detail": "html cannot be empty"})
            return
        size = len(html.encode('utf-8'))
        if size > SITE_MAX_BYTES:
            limit_mb = SITE_MAX_BYTES // (1024 * 1024)
            self.send_json_response(413, {"detail": f"html exceeds the {limit_mb}MB size limit"})
            return

        site_id = secrets.token_urlsafe(8)
        _ensure_sites_dir()
        try:
            with open(_site_path(site_id), "w", encoding="utf-8") as f:
                f.write(html)
        except OSError as e:
            self.send_json_response(500, {"detail": f"Failed to store site: {str(e)}"})
            return

        database.create_published_site(
            site_id,
            user["id"],
            title=site_title_from_html(html) or site_id,
            size_bytes=size,
        )
        payload = {"id": site_id, "url": f"/sites/{site_id}"}

        message_id = data.get("message_id")
        if isinstance(message_id, str) and _UUID_RE.match(message_id):
            message = database.get_message(message_id)
            if message and message["user_id"] == user["id"]:
                database.attach_published_url(message_id, payload["url"])
                payload["message_id"] = message_id
        self.send_json_response(201, payload)

    def handle_get_site(self, site_id):
        # Public, unauthenticated read of a stored site. Serves the raw HTML
        # (not the JSON envelope) so the file can be opened directly / linked to.
        path = _site_path(site_id)
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            self.send_error(404)
            return

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "public, max-age=3600")
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def handle_list_sites(self, user):
        # Management list: sites owned by the logged-in user, newest first.
        # Raw GET /sites/<id> stays public and unauthenticated (that is what the
        # shared URL uses).
        rows = database.list_user_sites(user["id"])
        sites = [
            {
                "id": row["id"],
                "url": f"/sites/{row['id']}",
                "created_at": row["created_at"],
                "size_bytes": row["size_bytes"],
                "title": row["title"],
            }
            for row in rows
            if os.path.isfile(_site_path(row["id"]))
        ]
        self.send_json_response(200, {"sites": sites})

    def handle_delete_site(self, site_id):
        # Owners may unpublish their own site by id. SQLite is the source of
        # truth for ownership: only the owning account may delete. 404 when the
        # site is unknown/already gone, 403 when it belongs to another user.
        user = self._require_user()
        if not user:
            return
        if not _valid_site_id(site_id):
            self.send_json_response(404, {"detail": "Site not found"})
            return
        site = database.get_published_site(site_id)
        if site is None and not os.path.isfile(_site_path(site_id)):
            self.send_json_response(404, {"detail": "Site not found"})
            return
        if site is None:
            # Orphaned file (missing DB row): error out, don't delete.
            self.send_json_response(404, {"detail": "Site not found"})
            return
        if site["user_id"] != user["id"]:
            self.send_json_response(403, {"detail": "Not allowed to delete this site"})
            return
        try:
            os.remove(_site_path(site_id))
        except OSError as e:
            self.send_json_response(500, {"detail": f"Failed to delete site: {str(e)}"})
            return
        database.delete_published_site(site_id)
        self.send_json_response(200, {"deleted": site_id})

    # ---- chat --------------------------------------------------------------

    def handle_chat(self, user, stream=False):
        try:
            data = self._read_json_body()
        except Exception:
            self.send_json_response(400, {"detail": "Invalid JSON body"})
            return

        messages = data.get("messages")
        if messages is None:
            message = data.get("message", "").strip()
            if not message:
                self.send_json_response(400, {"detail": "Message cannot be empty"})
                return
            messages = [{"role": "user", "content": message}]

        is_valid, error_detail = validate_messages(messages)
        if not is_valid:
            self.send_json_response(400, {"detail": error_detail})
            return

        # Optional conversation binding: when provided the user+assistant turns
        # are persisted under this conversation. The client re-sends its full
        # message list (already loaded from the server), so sync keeps the DB
        # aligned even across retry/edit that truncate history.
        conversation_id = data.get("conversation_id")
        conversation_set = False
        if conversation_id:
            if not isinstance(conversation_id, str) or not _UUID_RE.match(conversation_id):
                self.send_json_response(400, {"detail": "Invalid conversation_id"})
                return
            conversation = database.get_conversation(conversation_id)
            if conversation is None or conversation["user_id"] != user["id"]:
                self.send_json_response(404, {"detail": "Conversation not found"})
                return
            database.sync_messages(conversation_id, messages)
            conversation_set = True

        # Trim for the model payload (the sync above saw the FULL list).
        if len(messages) > MAX_HISTORY:
            messages = messages[-MAX_HISTORY:]

        cleaned_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "assistant" and content:
                # Remove old leaked reasoning preamble from past turns
                if "The user " in content[:150] or "As an AI " in content[:150]:
                    if "<!DOCTYPE" in content:
                        idx = content.find("<!DOCTYPE")
                        content = content[idx:]
                    elif "```" in content:
                        idx = content.find("```")
                        content = content[idx:]
            cleaned_messages.append({"role": role, "content": content})

        headers = {"Content-Type": "application/json"}
        if OPENCODE_API_KEY and OPENCODE_API_KEY.strip():
            headers["Authorization"] = f"Bearer {OPENCODE_API_KEY.strip()}"

        system_prompt = (
            "You are a helpful AI assistant. Respond directly with your answer or code. "
            "Do NOT output internal thoughts or meta commentary.\n\n"
            "When the user asks you to create or design a website, web page, landing page, "
            "or any HTML/CSS page, output your design as EXACTLY ONE self-contained ```html "
            "fenced code block containing the complete, ready-to-open page. Rules:\n"
            "- All CSS goes inline in a single <style> tag inside <head> using plain CSS. "
            "Use CSS custom properties (--name: value) for reusable colors/spacing so the page "
            "is easy to restyle.\n"
            "- All JavaScript goes inline in a single <script> tag placed just before </body>.\n"
            "- External libraries are allowed only via a CDN <script src=\"https://...\"> tag.\n"
            "- Never tell the user to install, compile, or run a build step. The page must work "
            "as-is when the HTML file is opened in a browser and served as static HTML."
        )
        payload_messages = [
            {"role": "system", "content": system_prompt},
            *cleaned_messages
        ]

        payload = {
            "model": "hy3-free",
            "messages": payload_messages,
            "temperature": 0.7,
            "max_tokens": 16384,
            "stream": stream
        }

        def persist_assistant(content):
            if conversation_set:
                message_id = database.add_message(conversation_id, "assistant", content)
                database.touch_conversation(conversation_id)
                return message_id
            return None

        try:
            def make_request():
                return requests.post(OPENCODE_API_URL, json=payload, headers=headers, timeout=(15, 120), stream=stream)

            response, error = request_with_retry(make_request)

            if error:
                if stream:
                    self.send_stream_error(f"Failed to connect to AI provider after retries: {str(error)}")
                else:
                    self.send_json_response(503, {"detail": f"Failed to connect to AI provider after retries: {str(error)}"})
                return

            if not response.ok:
                error_detail = parse_ai_error(response)
                if stream:
                    self.send_stream_error(error_detail)
                else:
                    self.send_json_response(502, {"detail": error_detail})
                return

            if stream:
                content = self.handle_stream_response(response, persist=persist_assistant)
            else:
                ai_data = response.json()
                if not ai_data.get("choices") or not ai_data["choices"][0].get("message", {}).get("content"):
                    self.send_json_response(502, {"detail": "Empty response from AI provider"})
                    return
                content = ai_data["choices"][0]["message"]["content"]
                self.send_json_response(200, {"answer": content})
                if content:
                    persist_assistant(clean_reasoning_leak(content))

        except requests.exceptions.Timeout:
            if stream:
                self.send_stream_error("Request to AI provider timed out after retries")
            else:
                self.send_json_response(504, {"detail": "Request to AI provider timed out after retries"})
        except requests.exceptions.RequestException as e:
            if stream:
                self.send_stream_error(f"Failed to connect to AI provider: {str(e)}")
            else:
                self.send_json_response(503, {"detail": f"Failed to connect to AI provider: {str(e)}"})
        except (KeyError, IndexError, ValueError) as e:
            if stream:
                self.send_stream_error(f"Invalid response format from AI provider: {str(e)}")
            else:
                self.send_json_response(502, {"detail": f"Invalid response format from AI provider: {str(e)}"})

    def handle_stream_response(self, response, persist=None):
        # Streams the upstream SSE through to the client. Returns the assembled
        # assistant text (reasoning-stripped) or "" when nothing was emitted.
        # When a conversation is bound and a reply was collected, the reply is
        # persisted first and its message id is sent as a final SSE frame so the
        # client can attribute later actions (e.g. publishing a generated site)
        # to the exact stored message across reloads.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._send_cors_headers()
        self.end_headers()

        collected = []
        try:
            for line in response.iter_lines(decode_unicode=True):
                if line:
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            if chunk.get("choices"):
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content")
                                reasoning = delta.get("reasoning_content")
                                if content:
                                    collected.append(content)
                                    self.wfile.write(f"data: {json.dumps({'content': content})}\n\n".encode('utf-8'))
                                    self.wfile.flush()
                                elif reasoning:
                                    self.wfile.write(f"data: {json.dumps({'thinking': True})}\n\n".encode('utf-8'))
                                    self.wfile.flush()
                        except json.JSONDecodeError:
                            pass
            # Persist the completed reply before the [DONE] terminator so the
            # message id can ride in the same response.
            assembled = "".join(collected)
            if persist:
                cleaned = clean_reasoning_leak(assembled)
                if cleaned:
                    message_id = persist(cleaned)
                    if message_id:
                        self.wfile.write(f"data: {json.dumps({'assistant_message_id': message_id})}\n\n".encode('utf-8'))
                        self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, OSError):
            # Client disconnected mid-stream (tab closed, fetch aborted, or the
            # client-side timeout fired). Stop pulling further chunks from the
            # upstream response so we don't keep consuming bandwidth.
            response.close()
            return ""
        except Exception as e:
            self.send_stream_error(f"Stream error: {str(e)}")
            return ""
        return "".join(collected)

    def send_stream_error(self, error_msg):
        try:
            self.wfile.write(f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n".encode('utf-8'))
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_json_response(self, status_code, data, retry_after=None):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

class ReusableThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    database.init_db()
    # Enforce the generated-site retention policy (delete sites older than TTL).
    # Runs once at boot; SITE_TTL_SECONDS (default 7 days) governs how long a
    # published URL stays live.
    cleanup_expired_sites()
    # Bind address is overridable so the backend can sit behind a reverse proxy.
    port = int(os.getenv("PORT", "8000"))
    server = ReusableThreadingServer(("0.0.0.0", port), ChatHandler)
    print(f"Backend server running on http://0.0.0.0:{port}")
    server.serve_forever()