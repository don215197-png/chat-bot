import os
import json
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

OPENCODE_API_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY")

def parse_ai_error(response: requests.Response) -> str:
    """Parse error response from AI provider."""
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

class ChatHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "http://localhost:5173")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/health":
            self.send_json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/chat":
            self.handle_chat()
        elif parsed_path.path == "/health":
            self.send_json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def handle_chat(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            data = json.loads(body)
            message = data.get("message", "").strip()
        except json.JSONDecodeError:
            self.send_json_response(400, {"detail": "Invalid JSON"})
            return

        if not message:
            self.send_json_response(400, {"detail": "Message cannot be empty"})
            return

        headers = {"Content-Type": "application/json"}
        # OpenCode API works without auth for free model; invalid key causes 401
        # Only add Authorization if explicitly needed in future

        payload = {
            "model": "hy3-free",
            "messages": [
                {"role": "system", "content": "You are a helpful AI assistant."},
                {"role": "user", "content": message}
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }

        try:
            response = requests.post(OPENCODE_API_URL, json=payload, headers=headers, timeout=30)

            if not response.ok:
                error_detail = parse_ai_error(response)
                self.send_json_response(502, {"detail": error_detail})
                return

            ai_data = response.json()

            if not ai_data.get("choices") or not ai_data["choices"][0].get("message", {}).get("content"):
                self.send_json_response(502, {"detail": "Empty response from AI provider"})
                return

            answer = ai_data["choices"][0]["message"]["content"]
            self.send_json_response(200, {"answer": answer})

        except requests.exceptions.Timeout:
            self.send_json_response(504, {"detail": "Request to AI provider timed out"})
        except requests.exceptions.RequestException as e:
            self.send_json_response(503, {"detail": f"Failed to connect to AI provider: {str(e)}"})
        except (KeyError, IndexError, ValueError) as e:
            self.send_json_response(502, {"detail": f"Invalid response format from AI provider: {str(e)}"})

    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "http://localhost:5173")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8000), ChatHandler)
    print("Backend server running on http://localhost:8000")
    server.serve_forever()