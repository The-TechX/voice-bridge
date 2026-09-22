# voice-bridge

Dockerized voice transport for Jarvis. Voice Bridge owns browser push-to-talk, local STT forwarding, streaming TTS delivery, and push-alert audio. The conversational agent runtime is external.

## Current architecture

```text
Browser PTT -> Voice Bridge -> local Whisper STT -> Jarvis agent /turn/stream
                                                   |
                                                   +-> text deltas/commentary
                                                           |
                                                           v
                                              ElevenLabs WebSocket TTS
                                                           |
                                                     PCM 24 kHz
                                                           |
                                              Voice Bridge WS /ws
                                                           |
                                              Desktop Web Audio playback
```

Tool commentary is emitted by the conversational agent as a separate event and is sent immediately to the same ElevenLabs streaming session before the tool executes. Normal response deltas are buffered lightly before being sent to ElevenLabs to preserve natural speech.

## Audio paths

Conversational turns use one ElevenLabs WebSocket session per turn and stream raw PCM 24 kHz to the browser. Desktop Web Audio is the reference client for this path.

Alerts and the legacy `POST /speak` endpoint remain on Fish Audio MP3 and use HTMLAudio playback. They are intentionally separate from conversational streaming.

The iOS web/PWA client is currently best-effort. iOS may suspend Web Audio after microphone capture; the target mobile client is native iOS, so no iOS-specific Web Audio keep-alive hacks are maintained in this bridge.

## Configuration

Copy `.env.example` to `.env`. Secrets must stay outside the repository. ElevenLabs is mounted read-only from `ELEVENLABS_API_KEY_PATH`; Fish remains optional for alerts and `/speak`.

Important settings:

```dotenv
AGENT_URL=http://host.docker.internal:8781
STT_URL=http://cme-whisper-stt:8000
ELEVENLABS_API_KEY_PATH=/home/ubuntu/elevenlabsapikey
ELEVENLABS_MODEL=eleven_flash_v2_5
ELEVENLABS_VOICE_ID=8mBRP99B2Ng2QwsJMFQl
ELEVENLABS_STREAM_WORDS=7
```

## Run

```bash
docker compose up -d --build
curl http://127.0.0.1:8765/health
```

The container listens on port 8000 internally and defaults to `127.0.0.1:8765` on the host.

## Main endpoints

- `GET /health` — runtime/configuration state.
- `POST /stt/transcribe` — forwards recorded audio to local STT with Spanish selected.
- `POST /agent/turn/stream` — streams the agent response into ElevenLabs and broadcasts PCM to connected clients.
- `POST /agent/turn` — non-streaming agent bridge retained for compatibility.
- `WS /ws` — binary audio channel. Conversational audio is PCM; message/alert audio is preceded by `voice-message` metadata and remains MP3.
- `POST /speak` — legacy/utility Fish Audio MP3 synthesis.
- `POST /messages` and `/messages/*` — persistent push-alert flow.

## Browser client

PTT uses MediaRecorder with periodic data emission plus a final `requestData()` before stop so the uploaded recording is complete. Conversational PCM is scheduled sequentially through Web Audio. Alert MP3 continues through a persistent HTMLAudio element.

Desktop Chromium is the current reference environment. Native iOS is the intended mobile architecture; the PWA is not treated as the long-term iOS audio runtime.

## Security

Do not commit API keys, VAPID private keys, `.env`, or runtime data. The ElevenLabs key is read from a read-only mounted file by default. Persistent push state lives under `data/voice_bridge.db`.
