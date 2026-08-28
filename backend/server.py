import os
import json
import time
import requests
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

OPENCODE_API_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY")

# OPTIONAL shared secret for protecting an internet-facing backend. When set,
# /chat and /chat/stream require "Authorization: Bearer <key>". Unset (empty)
# for local/personal use disables the check.
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "").strip()

DEFAULT_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
# Extend the allowlist at deploy time without editing code.
# Comma-separated list, e.g. ALLOWED_ORIGINS=https://myapp.com,https://www.myapp.com
ALLOWED_ORIGINS = DEFAULT_ALLOWED_ORIGINS + [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

MAX_HISTORY = 20
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/health":
            self.send_json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def _is_authorized(self):
        # When a shared secret is configured, require a matching
        # "Authorization: Bearer <key>" header so random internet traffic
        # cannot consume the backend's bandwidth/compute.
        if not BACKEND_API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        _, _, token = auth.partition(" ")
        return token == BACKEND_API_KEY

    def do_POST(self):
        try:
            parsed_path = urlparse(self.path)
            if parsed_path.path in ("/chat", "/chat/stream"):
                if not self._is_authorized():
                    self.send_json_response(401, {"detail": "Unauthorized"})
                    return
                self.handle_chat(stream=(parsed_path.path == "/chat/stream"))
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_json_response(500, {"detail": f"Internal server error: {str(e)}"})
            except Exception:
                pass

    def handle_chat(self, stream=False):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ""
            data = json.loads(body) if body else {}
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

        payload_messages = [
            {"role": "system", "content": "You are a helpful AI assistant. Respond directly with your answer or code. Do NOT output internal thoughts or meta commentary."},
            *cleaned_messages
        ]

        payload = {
            "model": "hy3-free",
            "messages": payload_messages,
            "temperature": 0.7,
            "max_tokens": 16384,
            "stream": stream
        }

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
                self.handle_stream_response(response)
            else:
                ai_data = response.json()
                if not ai_data.get("choices") or not ai_data["choices"][0].get("message", {}).get("content"):
                    self.send_json_response(502, {"detail": "Empty response from AI provider"})
                    return
                answer = ai_data["choices"][0]["message"]["content"]
                self.send_json_response(200, {"answer": answer})

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

    def handle_stream_response(self, response):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._send_cors_headers()
        self.end_headers()

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
                                    self.wfile.write(f"data: {json.dumps({'content': content})}\n\n".encode('utf-8'))
                                    self.wfile.flush()
                                elif reasoning:
                                    self.wfile.write(f"data: {json.dumps({'thinking': True})}\n\n".encode('utf-8'))
                                    self.wfile.flush()
                        except json.JSONDecodeError:
                            pass
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            self.send_stream_error(f"Stream error: {str(e)}")

    def send_stream_error(self, error_msg):
        try:
            self.wfile.write(f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n".encode('utf-8'))
            self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_json_response(self, status_code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

class ReusableThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    # Bind address is overridable so the backend can sit behind a reverse proxy.
    port = int(os.getenv("PORT", "8000"))
    server = ReusableThreadingServer(("0.0.0.0", port), ChatHandler)
    print(f"Backend server running on http://0.0.0.0:{port}")
    server.serve_forever()