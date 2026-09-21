import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

import httpx
from fastapi import FastAPI, File, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

app = FastAPI(title="voice-bridge", version="0.4.0")
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("VOICE_BRIDGE_DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "voice_bridge.db"
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "/app/data/vapid_private.pem")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:push@tchx.dev")
FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL = "s2.1-pro-free"
AGENT_URL = os.getenv("AGENT_URL", "").rstrip("/")
AGENT_TIMEOUT_SECONDS = float(os.getenv("AGENT_TIMEOUT_SECONDS", "60"))
STT_URL = os.getenv("STT_URL", "").rstrip("/")
STT_TIMEOUT_SECONDS = float(os.getenv("STT_TIMEOUT_SECONDS", "30"))

clients: Set[WebSocket] = set()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    reference_id: str | None = None


class AgentTurnRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: dict[str, str]


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="TCHX Voice", min_length=1, max_length=80)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT
            )
            """
        )


@app.on_event("startup")
async def startup() -> None:
    init_db()


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/health")
async def health():
    with db_connect() as conn:
        subscriptions = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'pending'").fetchone()[0]
    return {
        "status": "ok",
        "increment": "agent-session-bridge",
        "agent_configured": bool(AGENT_URL),
        "stt_configured": bool(STT_URL),
        "tts_configured": bool(os.getenv("FISH_API_KEY")),
        "push_configured": bool(VAPID_PUBLIC_KEY and Path(VAPID_PRIVATE_KEY).exists()),
        "connected_clients": len(clients),
        "push_subscriptions": subscriptions,
        "pending_messages": pending,
    }


@app.post("/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...)):
    if not STT_URL:
        raise HTTPException(status_code=503, detail="STT_URL is not configured")

    request_started = time.perf_counter()
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="Audio payload is empty")

    filename = file.filename or "speech.audio"
    content_type = file.content_type or "application/octet-stream"
    try:
        async with httpx.AsyncClient(timeout=STT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{STT_URL}/transcribe",
                params={"language": "es"},
                files={"file": (filename, audio, content_type)},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] or f"STT returned HTTP {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"STT request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="STT returned invalid JSON") from exc

    text = str(payload.get("text", "")).strip()
    elapsed_ms = round((time.perf_counter() - request_started) * 1000, 1)
    return {"status": "ok", "text": text, "stt": payload, "timing": {"stt_ms": elapsed_ms}}


@app.post("/agent/turn")
async def agent_turn(request: AgentTurnRequest):
    request_started = time.perf_counter()
    if not AGENT_URL:
        raise HTTPException(status_code=503, detail="AGENT_URL is not configured")

    try:
        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{AGENT_URL}/turn",
                json={"session_id": request.session_id, "text": request.text},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] or f"Agent returned HTTP {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Agent request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"status": "ok"}
    elapsed_ms = round((time.perf_counter() - request_started) * 1000, 1)
    return {"status": "ok", "agent": payload, "timing": {"agent_ms": elapsed_ms}}


def pop_speech_chunks(buffer: str, final: bool = False) -> tuple[list[str], str]:
    chunks: list[str] = []
    buffer = buffer.strip()
    # Only split on completed sentences. Mid-sentence comma chunks sound
    # unnatural because each Fish request is synthesized independently.
    while buffer:
        match = re.search(r"(?<=[.!?])\s+", buffer)
        if not match:
            break
        candidate = buffer[:match.start()].strip()
        buffer = buffer[match.end():].strip()
        if candidate:
            chunks.append(candidate)
    if final and buffer:
        chunks.append(buffer)
        buffer = ""
    return chunks, buffer


@app.post("/agent/turn/stream")
async def agent_turn_stream(request: AgentTurnRequest):
    if not AGENT_URL:
        raise HTTPException(status_code=503, detail="AGENT_URL is not configured")

    started = time.perf_counter()
    buffer = ""
    output = ""
    chunk_count = 0
    first_audio_ms = None
    tts_total_ms = 0.0
    model_ms = None
    ttft_ms = None

    try:
        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST",
                f"{AGENT_URL}/turn/stream",
                json={"session_id": request.session_id, "text": request.text},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("type") == "delta":
                        delta = str(event.get("text", ""))
                        output += delta
                        buffer += delta
                        chunks, buffer = pop_speech_chunks(buffer)
                        for chunk in chunks:
                            tts_started = time.perf_counter()
                            audio = await synthesize_audio(chunk)
                            tts_total_ms += round((time.perf_counter() - tts_started) * 1000, 1)
                            await broadcast_audio(audio)
                            chunk_count += 1
                            if first_audio_ms is None:
                                first_audio_ms = round((time.perf_counter() - started) * 1000, 1)
                    elif event.get("type") == "done":
                        model_ms = event.get("model_ms")
                        ttft_ms = event.get("ttft_ms")

        chunks, buffer = pop_speech_chunks(buffer, final=True)
        for chunk in chunks:
            tts_started = time.perf_counter()
            audio = await synthesize_audio(chunk)
            tts_total_ms += round((time.perf_counter() - tts_started) * 1000, 1)
            await broadcast_audio(audio)
            chunk_count += 1
            if first_audio_ms is None:
                first_audio_ms = round((time.perf_counter() - started) * 1000, 1)
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Streaming agent request failed: {exc}") from exc

    return {
        "status": "ok",
        "agent": {"output": output, "spoken": True},
        "timing": {
            "ttft_ms": ttft_ms,
            "model_ms": model_ms,
            "tts_total_ms": round(tts_total_ms, 1),
            "first_audio_ms": first_audio_ms,
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            "chunks": chunk_count,
        },
    }


@app.get("/push/public-key")
async def push_public_key():
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Web Push is not configured")
    return {"public_key": VAPID_PUBLIC_KEY}


@app.post("/push/subscribe")
async def subscribe(request: PushSubscriptionRequest):
    p256dh = request.keys.get("p256dh")
    auth = request.keys.get("auth")
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Invalid push subscription")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth
            """,
            (request.endpoint, p256dh, auth, now_iso()),
        )
    return {"status": "ok"}


@app.delete("/push/subscribe")
async def unsubscribe(request: PushSubscriptionRequest):
    with db_connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (request.endpoint,))
    return {"status": "ok"}


def load_subscriptions() -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    return [
        {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
        for row in rows
    ]


def delete_subscription(endpoint: str) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def send_web_push(subscription: dict, payload: dict) -> bool:
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=300,
            timeout=15,
        )
        return True
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {404, 410}:
            delete_subscription(subscription["endpoint"])
        return False


@app.post("/messages")
async def create_message(request: MessageRequest):
    message_id = uuid.uuid4().hex
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO messages(id, title, text, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (message_id, request.title, request.text, now_iso()),
        )

    payload = {
        "type": "voice-message",
        "message_id": message_id,
        "title": request.title,
        "body": "Nuevo mensaje de voz. Toca para escuchar.",
        "url": f"/?message={message_id}",
    }
    live_delivered = 0
    if clients:
        try:
            audio = await synthesize_audio(request.text)
            live_delivered = await broadcast_message_audio(message_id, audio)
        except HTTPException:
            live_delivered = 0

    subscriptions = load_subscriptions()
    results = await asyncio.gather(
        *(asyncio.to_thread(send_web_push, subscription, payload) for subscription in subscriptions)
    )
    return {
        "status": "ok",
        "message_id": message_id,
        "live_delivered": live_delivered,
        "push_subscriptions": len(subscriptions),
        "push_delivered": sum(1 for result in results if result),
    }


@app.get("/messages/pending")
async def pending_message():
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, title, text, created_at FROM messages WHERE status = 'pending' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"message": None}
    return {"message": dict(row)}


@app.get("/messages/{message_id}")
async def get_message(message_id: str):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, title, text, status, created_at, delivered_at FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    return dict(row)


async def synthesize_audio(text: str, reference_id: str | None = None) -> bytes:
    api_key = os.getenv("FISH_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="FISH_API_KEY is not configured")

    payload: dict[str, str] = {"text": text, "format": "mp3"}
    resolved_reference_id = reference_id or os.getenv("FISH_REFERENCE_ID")
    if resolved_reference_id:
        payload["reference_id"] = resolved_reference_id

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
    return response.content


@app.get("/messages/{message_id}/audio")
async def message_audio(message_id: str):
    with db_connect() as conn:
        row = conn.execute("SELECT text FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    audio = await synthesize_audio(row["text"])
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.post("/messages/{message_id}/ack")
async def acknowledge_message(message_id: str):
    with db_connect() as conn:
        result = conn.execute(
            "UPDATE messages SET status = 'delivered', delivered_at = ? WHERE id = ?",
            (now_iso(), message_id),
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"status": "ok"}


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


async def broadcast_message_audio(message_id: str, audio: bytes) -> int:
    delivered = 0
    dead: list[WebSocket] = []
    metadata = json.dumps({"type": "voice-message", "message_id": message_id})
    for websocket in list(clients):
        try:
            await websocket.send_text(metadata)
            await websocket.send_bytes(audio)
            delivered += 1
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        clients.discard(websocket)
    return delivered


@app.post("/speak")
async def speak(request: SpeakRequest):
    request_started = time.perf_counter()
    tts_started = time.perf_counter()
    audio = await synthesize_audio(request.text, request.reference_id)
    tts_ms = round((time.perf_counter() - tts_started) * 1000, 1)
    broadcast_started = time.perf_counter()
    delivered = await broadcast_audio(audio)
    broadcast_ms = round((time.perf_counter() - broadcast_started) * 1000, 1)
    total_ms = round((time.perf_counter() - request_started) * 1000, 1)
    return {
        "status": "ok",
        "model": FISH_MODEL,
        "format": "mp3",
        "bytes": len(audio),
        "delivered_to": delivered,
        "timing": {"tts_ms": tts_ms, "broadcast_ms": broadcast_ms, "speak_total_ms": total_ms},
    }
