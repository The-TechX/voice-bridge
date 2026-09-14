# voice-bridge

Minimal, Dockerized voice bridge for experimenting with web voice interfaces and pluggable agent backends.

## Increment 1 — Web STT

Current flow:

```text
Microphone -> Browser Web Speech API -> transcript in the web app
```

The backend only serves the application in this increment. There is intentionally no TTS, Fish Audio, agent runtime, LLM, or external STT API yet.

### Why browser STT first?

It validates microphone permissions, UX, continuous recognition, partial/final transcripts, Docker deployment, and Tailscale access without introducing API keys or usage costs.

### Run

```bash
cp .env.example .env
# Set BIND_ADDR to CME-01's Tailscale IPv4.
docker compose up -d --build
```

Open:

```text
http://<CME_TAILSCALE_IP>:8765
```

### Browser support

This first increment uses `SpeechRecognition` / `webkitSpeechRecognition`, so Chromium-based browsers such as Chrome or Edge are the intended test clients.

### Endpoints

- `GET /` — web app
- `GET /health` — service health

### Next increments

Planned architecture can replace browser STT with a server-side provider without changing the rest of the voice bridge:

```text
Browser microphone -> audio stream -> STT adapter -> agent runtime -> TTS adapter -> browser audio
```
