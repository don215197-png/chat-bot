import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AI Chatbot Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENCODE_API_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY")

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    answer: str

class ErrorResponse(BaseModel):
    detail: str

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

@app.post("/chat", response_model=ChatResponse, responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}})
async def chat(request: ChatRequest):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    headers = {
        "Content-Type": "application/json",
    }
    # OpenCode API works without auth for free model; invalid key causes 401
    # Only add Authorization if explicitly needed in future
    
    payload = {
        "model": "hy3-free",
        "messages": [
            {"role": "system", "content": "You are a helpful AI assistant."},
            {"role": "user", "content": request.message.strip()}
        ],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    
    try:
        response = requests.post(
            OPENCODE_API_URL,
            json=payload,
            headers=headers,
            timeout=30
        )
        
        if not response.ok:
            error_detail = parse_ai_error(response)
            raise HTTPException(status_code=502, detail=error_detail)
        
        data = response.json()
        
        if not data.get("choices") or not data["choices"][0].get("message", {}).get("content"):
            raise HTTPException(status_code=502, detail="Empty response from AI provider")
        
        return {"answer": data["choices"][0]["message"]["content"]}
    
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Request to AI provider timed out")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Failed to connect to AI provider: {str(e)}")
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"Invalid response format from AI provider: {str(e)}")

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)