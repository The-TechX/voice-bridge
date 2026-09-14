import os
from pathlib import Path
from typing import Set

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="voice-bridge", version="0.2.0")
STATIC_DIR = Path(__file__).parent / "static"
FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL = "s2.1-pro-free"

clients: Set[WebSocket] = set()


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    reference_id: str | None = None


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "increment": "fish-tts-channel",
        "tts_configured": bool(os.getenv("FISH_API_KEY")),
        "connected_clients": len(clients),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)
        await websocket.close()


async def broadcast_audio(audio: bytes) -> int:
    delivered = 0
    dead: list[WebSocket] = []

    for websocket in list(clients):
        try:
            await websocket.send_bytes(audio)
            delivered += 1
        except Exception:
            dead.append(websocket)

    for websocket in dead:
        clients.discard(websocket)

    return delivered


@app.post("/speak")
async def speak(request: SpeakRequest):
    api_key = os.getenv("FISH_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="FISH_API_KEY is not configured")

    payload: dict[str, str] = {
        "text": request.text,
        "format": "mp3",
    }
    reference_id = request.reference_id or os.getenv("FISH_REFERENCE_ID")
    if reference_id:
        payload["reference_id"] = reference_id

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": FISH_MODEL,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(FISH_TTS_URL, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] or f"Fish Audio returned HTTP {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Fish Audio request failed: {exc}") from exc

    delivered = await broadcast_audio(response.content)

    return {
        "status": "ok",
        "model": FISH_MODEL,
        "format": "mp3",
        "bytes": len(response.content),
        "delivered_to": delivered,
    }
