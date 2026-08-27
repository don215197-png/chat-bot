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
- **Communication**: REST API with JSON

## Project Structure

```
ai-chatbot/
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main chat component
│   │   ├── App.css       # Chat styling
│   │   ├── main.jsx      # Entry point
│   │   └── index.css     # Global styles
│   ├── package.json
│   └── vite.config.js
├── backend/
│   ├── server.py         # HTTP server with /chat endpoint
│   ├── requirements.txt  # Python dependencies
│   ├── .env              # Environment variables (API key)
│   └── .gitignore
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

Create a `.env` file in the `backend/` directory:

```env
# OpenCode API Key (get from https://opencode.ai)
# Leave empty if the API doesn't require authentication
OPENCODE_API_KEY=
```

The `.env` file is already in `.gitignore` to keep credentials secure.

## Running the Application

### Start Backend (Terminal 1)

```bash
cd backend
python server.py
```

Server runs on `http://localhost:8000`

### Start Frontend (Terminal 2)

```bash
cd frontend
npm run dev
```

Frontend runs on `http://localhost:5173`

## API Flow

1. User types message in React frontend
2. Frontend sends `POST /chat` to backend with JSON body:
   ```json
   { "message": "user message" }
   ```
3. Backend forwards request to OpenCode API:
   ```json
   {
     "model": "hy3-free",
     "messages": [
       { "role": "system", "content": "You are a helpful AI assistant." },
       { "role": "user", "content": "user message" }
     ],
     "temperature": 0.7,
     "max_tokens": 1000
   }
   ```
4. OpenCode API returns AI response
5. Backend returns to frontend:
   ```json
   { "answer": "AI response" }
   ```
6. Frontend displays response in chat UI

## Features

- ✅ Clean modern chat UI with user/AI message bubbles
- ✅ Loading indicator while waiting for response
- ✅ Enter key to send message
- ✅ Send button disabled while loading
- ✅ Error handling with user-friendly messages
- ✅ CORS enabled for development
- ✅ API credentials kept server-side only
- ✅ Responsive design (mobile-friendly)

## Error Handling

The backend handles:
- OpenCode API errors (HTTP status codes)
- Request timeouts (30 seconds)
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

## Security Notes

- Never commit `.env` file to version control
- API key stays on backend only
- Frontend communicates only with local backend
- CORS restricted to `http://localhost:5173` in development