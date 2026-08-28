# AI Chatbot

A full-stack AI chatbot with React frontend and Python backend, using OpenCode's hy3-free model.

## Architecture

```
React Frontend (Vite) → POST /chat → Python Backend (http.server) → OpenCode API → hy3-free
```

- **Frontend**: React + Vite, clean modern chat UI
- **Backend**: Python built-in HTTP server (no external framework dependencies)
- **AI Provider**: OpenCode OpenAI-compatible API
- **Model**: hy3-free
- **Communication**: REST API with JSON, SSE streaming for `/chat/stream`

## Project Structure

```
.
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main chat component
│   │   ├── App.css       # Chat styling
│   │   ├── main.jsx      # Entry point
│   │   └── index.css     # Global styles
│   ├── package.json
│   ├── vite.config.js
│   ├── .env.example      # Documented frontend env vars
│   └── Dockerfile        # Build + static serve (nginx)
├── backend/
│   ├── server.py         # HTTP server with /chat endpoints
│   ├── requirements.txt  # Python dependencies
│   ├── .env.example      # Documented backend env vars
│   └── Dockerfile        # Python runtime image
└── README.md
```

## Installation

### Backend

```bash
cd backend
pip install -r requirements.txt
```

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

# OPTIONAL shared secret. If set, /chat and /chat/stream require:
#   Authorization: Bearer <BACKEND_API_KEY>
# Leave empty for local/personal use.
BACKEND_API_KEY=
```

### Frontend (`frontend/.env.local`)

```env
# Backend base URL. Defaults to http://localhost:8000 for local dev.
VITE_API_URL=http://localhost:8000

# OPTIONAL: only set if the backend requires BACKEND_API_KEY.
VITE_BACKEND_API_KEY=
```

`.env` files are gitignored to keep secrets (and your deployment config) out of version control.

## Running the Application (Development)

### Start Backend (Terminal 1)

```bash
cd backend
python server.py
```

Server runs on `http://localhost:8000` (or the `PORT` you set).

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
  -e BACKEND_API_KEY=your-shared-secret \
  ai-chatbot-backend
```

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

### Protecting a public backend (optional)

`hy3-free` needs no paid API key, so an open backend is not a billing risk — it is only
about your own bandwidth/compute. If you deploy publicly, set `BACKEND_API_KEY` and the
matching `VITE_BACKEND_API_KEY` so the backend rejects anonymous `401` requests.

## API Flow

1. User types message in React frontend
2. Frontend sends `POST /chat/stream` to backend with JSON body:
   ```json
   { "messages": [ { "role": "user", "content": "user message" } ], "stream": true }
   ```
3. Backend validates and trims history (last 20 messages), forwards to OpenCode API:
   ```json
   {
     "model": "hy3-free",
     "messages": [
       { "role": "system", "content": "You are a helpful AI assistant." },
       { "role": "user", "content": "user message" }
     ],
     "temperature": 0.7,
     "max_tokens": 16384,
     "stream": true
   }
   ```
4. OpenCode API returns AI response (SSE when streaming)
5. Backend returns to frontend; frontend renders the streamed answer in the chat UI

## Features

- ✅ Clean modern chat UI with user/AI message bubbles
- ✅ Loading indicator while waiting for response
- ✅ Streaming responses with markdown rendering
- ✅ Enter key to send message; Send button disabled while loading
- ✅ Error handling with user-friendly messages and retry/dismiss
- ✅ Chat history persisted to `localStorage`
- ✅ Dark/light theme toggle
- ✅ Offline detection
- ✅ CORS restricted to an allowlist (configurable via `ALLOWED_ORIGINS`)
- ✅ Optional shared-secret auth for public deployments
- ✅ API credentials kept server-side only
- ✅ Responsive design (mobile-friendly)

## Error Handling

The backend handles:
- OpenCode API errors (HTTP status codes)
- Request timeouts (retries with backoff)
- Empty responses from AI provider
- Invalid request format
- Network connection failures

## Testing

Test the API directly:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, AI!"}'
```

Expected response:
```json
{"answer": "Hello! How can I help you today?"}
```

If `BACKEND_API_KEY` is set, add the header:
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <BACKEND_API_KEY>" \
  -d '{"message": "Hello, AI!"}'
```

## Security Notes

- Never commit `.env` files to version control
- API key stays on backend only
- Frontend communicates only with the configured backend origin
- CORS restricted to an allowlist in development and production
- Markdown output is sanitized with DOMPurify before rendering
- Optional `BACKEND_API_KEY` protects public deployments from anonymous access
