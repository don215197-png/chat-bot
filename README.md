# AI Chatbot

A full-stack AI chatbot with React frontend and Python backend, using OpenCode's hy3-free model. Accounts, conversations, messages and published sites are persisted in SQLite on the backend; the browser only holds a session token.

## Architecture

```
React Frontend (Vite) → POST /auth/login → Python Backend (http.server) → SQLite
                     └── POST /chat (Bearer session) ─────────────────────→ OpenCode API → hy3-free
```

- **Frontend**: React + Vite, clean modern chat UI; login gate + session token in `localStorage`
- **Backend**: Python built-in HTTP server (no external framework dependencies), SQLite via stdlib `sqlite3`
- **Auth**: register / login / logout; sessions table; passwords hashed with PBKDF2-SHA256 (salted)
- **AI Provider**: OpenCode OpenAI-compatible API
- **Model**: hy3-free
- **Communication**: REST API with JSON, SSE streaming for `/chat/stream`

## Architecture & Security Decisions

This project deliberately trades framework velocity for understanding. Every
non-obvious choice below is explained in the code as well; this is the summary.

- **Raw `http.server` instead of a framework (Flask/FastAPI).** The backend is a
  hand-rolled `ThreadingHTTPServer` handler — routing, body parsing, CORS,
  streaming, and validation are all explicit Python. That is the point: it shows
  how HTTP actually works end-to-end (request line → headers → body → response,
  plus SSE framing) with zero framework dependencies. **Trade-off:** we hand-
  maintain what Django/Starlette give for free, so there is less at-the-keyboard
  velocity, and correctness relies on the test suite. It also makes the switch
  to a framework (a FastAPI v2 port is a natural next step) a demonstration of
  fluency rather than a mystery.
- **CORS echoes only the allowlisted origin.** The server never returns
  `Access-Control-Allow-Origin: *` because credentialed requests must not be
  echoed back to an unverified origin. The `Origin` header is matched against an
  explicit allowlist (dev origins always; prod via `ALLOWED_ORIGINS`); anything
  else gets **no** CORS headers at all.
- **Sessions are DB-backed, not stateless JWTs.** A login inserts a random
  32-hex-char token into the `sessions` table with an expiry. **Why not JWT:**
  server-side sessions are immediately revocable (logout is a row delete), need
  no signing-key management, and can't be replayed after logout. **Trade-off:**
  one indexed DB read per request — fine for a single SQLite instance, and the
  main thing to revisit (Redist/db cache) if you scale out.
- **Passwords: PBKDF2-SHA256, 200k iterations, per-user random salt.** The salt
  and work factor live inside the stored string (`pbkdf2_sha256$iters$salt$hash`),
  so the format is self-describing and the factor can be raised later without a
  schema change. Implemented with only `hashlib` — no extra dependency, and a
  unit test pins the round-trip and salt uniqueness.
- **Per-IP in-memory rate limiting** (sliding window + daily cap) on the chat and
  publish routes, returning `429` + `Retry-After`. **Known trade-off (deliberate,
  not an accident):** buckets live in process memory — a restart resets them and
  multiple workers share nothing, so it is not a defensive wall on a large
  deployment. That is acceptable for a single-instance demo; scaling horizontally
  means moving the buckets to Redis (the interface is already isolated enough to
  swap). `TRUST_PROXY_HEADERS` (reading `X-Forwarded-For`) is off by default
  because trusting it without a proxy that strips the header lets clients spoof
  their IP and dodge the limiter.
- **IDOR is prevented in the database, not the client.** Every per-user read is a
  SQL `WHERE user_id = ?` including the id in the WHERE clause — a conversation or
  site from another account is simply not fetched (404), even when the attacker
  guesses a valid id. Ownership checks are never "filter out rows the client
  didn't ask for".
- **Path traversal is blocked before filesystem access.** Site ids must match
  `^[A-Za-z0-9_-]{1,64}$` before they are ever joined onto the site directory, so
  `..`, `/`, `%2e%2e` and friends resolve to nothing at all. The public tests
  assert traversal attempts return 404.
- **Published sites are sandboxed, not sanitized.** The page the model generated
  is inherently untrusted UI/JS. Trying to sanitize it to "safe HTML" is a
  whack-a-mole against arbitrary JavaScript and layout tricks; instead the raw
  file is served with a `Content-Security-Policy: sandbox allow-scripts
  allow-forms` (plus `nosniff`), so even opened directly it runs without
  same-origin privileges. Sandboxing is the honest boundary; sanitizing only-side
  would be false confidence.
- **The frontend is stateless about history.** The browser holds only
  `{ token, email }` (a personal secret — treat it like a cookie). Conversations,
  messages, and published-site links all live server-side, so history survives
  logout → login and is naturally per-account.
- **Publish attribution ties sites to exact replies.** The stream's final SSE
  frame carries `assistant_message_id`; a later publish stores its URL back on
  that stored message (only if the message belongs to the same user), so the
  "Published ✓" state survives reloads.
- **SQLite for storage.** Single-writer semantics, zero configuration, real
  files. Concurrent writes are serialized by SQLite's own locking with short
  transactions. This is consciously sized for a single-instance app; multi-user
  production routing means a move to Postgres + a connection pool (data access is
  isolated in `database.py`, so the blast radius of that change is contained).

## Project Structure

```
.
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main chat component (auth screen + chat UI)
│   │   ├── App.css       # Chat styling
│   │   ├── main.jsx      # Entry point
│   │   └── index.css     # Global styles
│   ├── package.json
│   ├── vite.config.js
│   ├── .env.example      # Documented frontend env vars
│   └── Dockerfile        # Build + static serve (nginx)
├── backend/
│   ├── server.py         # HTTP server: /auth, /conversations, /chat, /chat/stream, /sites
│   ├── database.py       # SQLite schema + access helpers
│   ├── requirements.txt  # Python dependencies
│   ├── pytest.ini        # pytest configuration
│   ├── tests/            # pytest suite (auth, conversations, sites, rate limiting)
│   ├── .env.example      # Documented backend env vars
│   ├── generated_sites/  # Published site files (gitignored, created at runtime)
│   └── Dockerfile        # Python runtime image
└── README.md
```

## Installation

### Backend

```bash
cd backend
python3 -m venv venv                 # Windows: python -m venv venv
source venv/bin/activate             # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` has only three packages — `requests`, `python-dotenv`, and
`pytest` — because the server itself is stdlib-only (`http.server` + `sqlite3`).
The `venv/` directory is gitignored. To leave the virtualenv later, run `deactivate`.

### Frontend

```bash
cd frontend
npm install
```

## Environment Variables

The backend reads these from `backend/.env` (create it by copying `backend/.env.example`; `load_dotenv()` loads it at startup). The frontend reads `VITE_`-prefixed vars from `frontend/.env.local` (copy `frontend/.env.example`).

### Backend (`backend/.env`)

```env
# OpenCode API key — hy3-free is free and needs NO key; leave empty.
# Only set if the provider changes to a paid model requiring auth.
OPENCODE_API_KEY=

# Port to listen on (default 8000). Useful behind a reverse proxy.
PORT=8000

# Extra CORS-allowed origins (comma-separated) for deployed frontends.
# The dev origins http://localhost:5173 and http://127.0.0.1:5173 are always allowed.
# Example: ALLOWED_ORIGINS=https://myapp.com,https://www.myapp.com
ALLOWED_ORIGINS=

# ---------------------------------------------------------------------------
# Rate limiting (Phase 2). Buckets are per-IP, in-memory, and per-process:
# restarting the backend resets every counter, and multiple workers share no
# state. Tune CHAT_* for chat, PUBLISH_* for site publication.
# ---------------------------------------------------------------------------
# Seize a per-IP cap from the X-Forwarded-For header instead of the raw socket
# peer. ONLY enable behind a trusted reverse proxy that strips/replaces that
# header from clients, otherwise anyone can spoof it. Default: off.
TRUST_PROXY_HEADERS=false

# Chat: max requests per IP in a sliding window. Defaults: 20 per 60s.
CHAT_MAX_REQUESTS=20
CHAT_WINDOW_SECONDS=60

# Publish: max sites per IP in a sliding window, plus a flat UTC daily cap.
# Defaults: 5 per 600s, 50 per day.
PUBLISH_MAX_REQUESTS=5
PUBLISH_WINDOW_SECONDS=600
PUBLISH_DAILY_LIMIT=50
```

Defaults for the remaining knobs (set them as `DATABASE_PATH`, `SESSION_TTL_SECONDS`
in `.env` if you need to change): the SQLite file lives at `backend/chatbot.db`;
sessions last 30 days; published sites are capped at 2 MB and retained 7 days.

Exceeding any limit returns `429` with a `Retry-After` header (seconds) and a
`{ "detail": ..., "retry_after": <seconds> }` body. `/health`, `GET /sites/<id>`
and the management endpoints are not rate-limited; only the write/chat routes
are.

### Frontend (`frontend/.env.local`)

```env
# Backend base URL. Defaults to http://localhost:8000 for local dev.
VITE_API_URL=http://localhost:8000
```

`.env` files are gitignored to keep secrets (and your deployment config) out of version control.

## Running the Application (Development)

### Start Backend (Terminal 1)

```bash
cd backend
python server.py
```

You should see `Backend server running on http://0.0.0.0:8000`. First run
creates `chatbot.db` (SQLite) and `generated_sites/` automatically
(`database.init_db()`). Verify it's alive at http://localhost:8000/health →
`{"status": "ok"}`.

### Start Frontend (Terminal 2)

```bash
cd frontend
npm run dev
```

Frontend runs on `http://localhost:5173`.

## Running in Production

Backend and frontend can be deployed as containers (each ships a `Dockerfile`).

### Backend container

```bash
cd backend
docker build -t ai-chatbot-backend .
docker run -d --name ai-chatbot-backend \
  -p 8000:8000 \
  -e ALLOWED_ORIGINS=https://your-frontend-domain.com \
  -v ai-chatbot-data:/app/data \
  ai-chatbot-backend
```

> Mount a volume over the app's data paths so the SQLite database (`chatbot.db`)
> and your published sites (`generated_sites/`) survive container restarts.

### Frontend container

```bash
cd frontend
docker build -t ai-chatbot-frontend \
  --build-arg VITE_API_URL=https://your-backend-domain.com \
  .
docker run -d --name ai-chatbot-frontend -p 8080:80 ai-chatbot-frontend
```

> The frontend image serves the static `dist/` build with nginx. You could equally
> serve `dist/` from any static host (S3/CDN) — nothing in it is server-rendered.

### Reverse proxy note

The backend listens on `0.0.0.0:<PORT>`. Put it behind a reverse proxy (nginx, Caddy,
Cloudflare) that terminates TLS, and add your public frontend origin to `ALLOWED_ORIGINS`
so CORS allows it.

### Deploying a public backend

`hy3-free` needs no paid API key, so an open backend is not a billing risk — it is only
about your own bandwidth/compute. Since there are real accounts and sessions, keep the
deployment honest: force HTTPS at the reverse proxy, add your public origin to
`ALLOWED_ORIGINS`, and consider TLS-terminating so session tokens and passwords are
never sent in clear text. There is no account-recovery/email-verification flow — users
must remember their password (reset is out of scope).

## API Flow

1. User registers (`POST /auth/register`) or logs in (`POST /auth/login`) on the frontend.
   The backend mints a session token (a row in the `sessions` table); the frontend keeps
   only `{ token, email }` in `localStorage`.
2. The frontend loads the user's conversations (`GET /conversations`) — all owned by the
   account, persisted in SQLite (no `localStorage` history).
3. User types a message. For a brand-new chat the frontend first creates it:
   `POST /conversations { "title": "..." }` → `{ "id": "..." }`.
4. Frontend sends `POST /chat/stream` to backend with the session Bearer token:
   ```json
   {
     "messages": [ { "role": "user", "content": "user message" } ],
     "conversation_id": "<conversation id>",
     "stream": true
   }
   ```
5. Backend validates the token + conversation ownership, trims history (last 20 messages),
   forwards to OpenCode API:
   ```json
   {
     "model": "hy3-free",
     "messages": [
       { "role": "system", "content": "You are a helpful AI assistant. …" },
       { "role": "user", "content": "user message" }
     ],
     "temperature": 0.7,
     "max_tokens": 16384,
     "stream": true
   }
   ```
   The system prompt instructs the model that any website request must be
   delivered as **exactly one self-contained ```` ```html ```` code block** —
   inline CSS in `<style>`, inline JS in `<script>`, external libraries only via
   CDN, and no build/install steps (the file works when opened directly).
6. The user and assistant messages are persisted to SQLite as the stream streams
   through. The final SSE frame carries
   `data: {"assistant_message_id": "<id>"}` so the client can attribute a later
   publish to the exact stored reply.
7. Frontend renders the streamed answer in the chat UI; on reload, history and
   any published-site links come back from the backend.

## Generated-site Hosting

When an assistant reply contains a ```` ```html ```` block, the chat UI shows
**Preview** (rendered in a sandboxed iframe) and **Code** (syntax-highlighted
raw HTML) tabs, plus a **Publish** button that stores the page server-side and
links a shareable URL.

### Endpoints

- `POST /sites` - store a generated page. Body: `{ "html": "<...>", "message_id": "<optional>" }`.
  Requires a session (`Authorization: Bearer <session token>`). Validates non-empty body and a
  **2 MB size cap** (`SITE_MAX_BYTES`). If `message_id` matches an assistant message that
  belongs to the same user, the published URL is stored back on that message so it survives
  reloads. Returns `201` with `{ "id": "<token_urlsafe(8)>", "url": "/sites/<id>" }`. Every
  publish creates a **new** id — published pages are immutable and never overwritten.
- `GET /sites/<id>` - **public, unauthenticated** read. Returns the raw HTML
  (`Content-Type: text/html`) with `X-Content-Type-Options: nosniff` and a
  `Content-Security-Policy: sandbox allow-scripts allow-forms` header so an
  untrusted generated page runs in a sandbox even when opened directly. Returns
  `404` for unknown ids (paths are strictly validated, no traversal).

### Managing your sites

Published pages are owned by the **user account** that published them (rows in the SQLite
`sites` table). Listing and deleting require the same session token used everywhere else.

- `GET /sites` - list **your own** sites, newest first, as
  `{ "sites": [{ "id", "url", "title", "size_bytes", "created_at" }] }`.
  Without a valid token → `401 { "detail": "Authentication required" }`. With a
  token that owns nothing → `{ "sites": [] }`. Site titles come from the page's
  `<title>` (or fall back to the id).
- `DELETE /sites/<id>` - unpublish one of your sites. `200 { "deleted": id }`
  on success; `401` without a token; `403` for a token that does not own the
  site; `404` if the site does not exist. Deleting permanently removes the file
  and its database row, so the public URL stops working immediately.
- **"My Sites"** panel in the sidebar: expand it to list your published pages
  (title, relative time, size), open any in a new tab, and delete one with a
  click (optimistic - the row hides instantly and is restored if the backend
  rejects). Chat messages that still show a "Publish" affordance for a deleted
  site automatically go back to the unpublished state.

Site metadata (owner user id, created time, size, title, originating message) lives in
SQLite, not in `index.json` — there is no separate metadata file to maintain.

### Retention

Sites are plain files under `backend/generated_sites/`. They are cleaned up at
startup when older than `SITE_TTL_SECONDS` (default **7 days**) — set by those
constants in `backend/server.py`. The directory is gitignored.

## Features

- ✅ Clean modern chat UI with user/AI message bubbles
- ✅ Loading indicator while waiting for response
- ✅ Streaming responses with markdown rendering
- ✅ Enter key to send message; Send button disabled while loading
- ✅ Error handling with user-friendly messages and retry/dismiss
- ✅ Account auth: register / login / logout, sessions, PBKDF2-hashed passwords
- ✅ Multi-conversation chat: new/rename/delete conversations, active-chat switching, **per-account history persisted in SQLite** (no `localStorage` chat data)
- ✅ Conversations and messages survive logout → login (per-user, isolated between accounts)
- ✅ Inline sidebar rename and two-step delete of conversations (persisted server-side)
- ✅ Dark/light theme toggle (the only per-browser local setting)
- ✅ Offline detection
- ✅ CORS restricted to an allowlist (configurable via `ALLOWED_ORIGINS`)
- ✅ API credentials (outbound OpenCode key) kept server-side only
- ✅ Responsive design (mobile-friendly)
- ✅ Generated-site in-chat **Preview** (sandboxed iframe) and **Code** tabs for ```` ```html ```` replies
- ✅ One-click **Publish** to `/sites/<id>` with a shareable URL; published link survives reloads (attributed to the stored message)
- ✅ Per-IP rate limiting on chat and publish routes (`429` + `Retry-After`, sliding window + daily cap)
- ✅ **My Sites** panel: list, open, and unpublish the sites you published (account-owned)
- ✅ 7-day retention + 2 MB size cap on published sites
- ✅ `pytest` suite covering auth, conversation ownership, chat persistence/replay, sites, rate-limiting boundaries, and security paths (traversal, password round-trip, IDOR)

## Error Handling

The backend handles:
- OpenCode API errors (HTTP status codes)
- Request timeouts (retries with backoff)
- Empty responses from AI provider
- Invalid request format
- Network connection failures
- Rate-limit overruns (`429` + `Retry-After`; the chat UI shows "try again in X min.")

## Testing

### API

Register a user, then use the returned session token for all authenticated calls:

```bash
# Sign up (or POST /auth/login for an existing user)
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "supersecret"}'
# → { "token": "<session token>", "email": "you@example.com" }

curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <session token>" \
  -d '{"messages": [{"role": "user", "content": "Hello, AI!"}], "conversation_id": "<conversation id>", "stream": false}'
```

Examples may omit `conversation_id` (a throw-away conversation) or `stream: false`
would be an answer; with `stream: true` you get SSE lines plus a final
`data: {"assistant_message_id": "<id>"}` frame. Conversations, their messages,
and published sites all require the session token.

### Automated tests

```bash
cd backend
python -m pytest -q   # 37 tests: auth, conversations, chat persistence/replay, sites, rate limits, security paths
```

The suite spins up a real `http.server` on an ephemeral port and uses a scratch
SQLite database per test, with the upstream OpenCode API mocked out.

## Security Notes

- Never commit `.env` files to version control
- API key stays on backend only
- Passwords are stored PBKDF2-SHA256-salted (never plaintext); session tokens are random 32-char ids in SQLite with an expiry
- Conversations and messages are scoped per user — one account can never read or write another's data
- Frontend communicates only with the configured backend origin
- CORS restricted to an allowlist in development and production
- Markdown output is sanitized with DOMPurify before rendering
- Generated-site previews render in a sandboxed iframe (`allow-scripts allow-forms`, no same-origin privileges); published pages are served with `nosniff` + a CSP sandbox directive
- Session token (`ai-chatbot-session` in `localStorage`) is a personal secret — treat it like a cookie; anyone holding it can act as your account. Log out when done on a shared machine
- In-memory rate limiting is per-process: restarting the backend resets the counters, and multiple workers share no state
